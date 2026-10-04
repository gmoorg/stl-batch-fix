"""Read a 3MF project into placed (world-coordinate) parts, plates and settings.

Reading only — no analysis.  Written for Bambu Studio projects, which add
`Metadata/model_settings.config` (part subtypes, plate assignment, per-object
setting overrides) and `Metadata/project_settings.config` (the effective
print settings as JSON) to the core 3MF package.  A plain 3MF without them is
still readable: every build item becomes one instance on plate 1, every part
is printable material, and the settings are empty.

Geometry conventions, from the 3MF core spec and checked against a real
Bambu project (`fae.3mf`, 2026-10-04):

- A transform is 12 numbers, row-major 4x3, applied to ROW vectors:
  `p' = [x y z 1] @ M`.  A component's transform is applied first, then the
  build item's, so the combined matrix is `component @ item`.
- Bambu's per-part `matrix` metadata in model_settings.config REPEATS the
  component transform; applying both would place the part twice.  It is
  ignored here.
- A Bambu part is identified by its component's `objectid`, which equals the
  `<part id>` in model_settings.config.  Matching is by that identity only:
  guessing by order could turn a negative volume into printable material, so
  an unmatched part is reported, never assumed normal.
- `instance_id` in a plate's `<model_instance>` is the index of the build
  item among the build items for that object, in document order.

Mesh files can be large (one sample object file is 121 MB of XML), so every
model file is parsed with a streaming `iterparse` that discards each vertex
and triangle element as soon as it is read.
"""

from __future__ import annotations

import json
import math
import posixpath
import xml.etree.ElementTree as ET
import zipfile
from array import array
from dataclasses import dataclass, field

import numpy as np

#: Model unit -> millimetres, from the 3MF core spec.
UNIT_SCALE = {'micron': 0.001, 'millimeter': 1.0, 'centimeter': 10.0,
              'inch': 25.4, 'foot': 304.8, 'meter': 1000.0}

#: Bambu part subtypes.  Only NORMAL is printable material.
NORMAL = 'normal_part'
NEGATIVE = 'negative_part'

_PATH_ATTR = '{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path'
_ROOT_MODEL = '3D/3dmodel.model'
_MAX_DEPTH = 16


class ReadError(Exception):
    """The file cannot be read as a 3MF project; the message says where."""


@dataclass(frozen=True)
class Part:
    """One mesh of an instance, in world millimetres.

    subtype     Bambu subtype (`normal_part`, `negative_part`,
                `modifier_part`, `support_blocker`, `support_enforcer`), or
                None when the part could not be matched to its metadata
    overrides   per-part setting overrides from model_settings.config
    """

    name: str
    subtype: str | None
    vertices: np.ndarray
    faces: np.ndarray
    overrides: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Instance:
    """One placed copy of an object on a plate.

    plate       Bambu plate number; 0 when the instance is on no plate
    overrides   per-object setting overrides from model_settings.config
    """

    object_id: str
    instance_id: int
    name: str
    plate: int
    printable: bool
    parts: tuple[Part, ...]
    overrides: dict[str, str] = field(default_factory=dict)

    @property
    def normal_parts(self) -> tuple[Part, ...]:
        return tuple(p for p in self.parts if p.subtype == NORMAL)


@dataclass(frozen=True)
class Project:
    path: str
    settings: dict[str, str]
    instances: tuple[Instance, ...]
    is_bambu: bool


@dataclass
class _Object:
    """One `<object>` resource: a mesh, or a list of components."""

    vertices: np.ndarray | None = None
    faces: np.ndarray | None = None
    components: list[tuple[str, str, np.ndarray]] = field(default_factory=list)


@dataclass
class _ModelFile:
    scale: float
    objects: dict[str, _Object]
    items: list[tuple[str, np.ndarray, bool]]


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def parse_transform(text: str | None) -> np.ndarray:
    """A 3MF transform string as a 4x4 row-vector affine matrix.

    Missing means identity.  Anything that is not 12 finite numbers, or whose
    3x3 part is singular, is refused: a part silently placed by a broken
    matrix would be analysed somewhere it does not print.
    """
    matrix = np.eye(4)
    if text is None or not text.strip():
        return matrix
    try:
        values = [float(v) for v in text.split()]
    except ValueError:
        raise ReadError(f'transform is not numeric: {text!r}') from None
    if len(values) != 12 or not all(math.isfinite(v) for v in values):
        raise ReadError(f'transform needs 12 finite numbers: {text!r}')
    matrix[:, :3] = np.array(values).reshape(4, 3)
    if abs(np.linalg.det(matrix[:3, :3])) < 1e-12:
        raise ReadError(f'transform is singular: {text!r}')
    return matrix


def _apply(matrix: np.ndarray, vertices: np.ndarray) -> np.ndarray:
    return vertices @ matrix[:3, :3] + matrix[3, :3]


def _parse_model(stream, name: str) -> _ModelFile:
    """Stream one model file.

    Each finished vertex/triangle is read into a flat array and its parent is
    cleared immediately, so the retained XML stays proportional to nesting
    depth rather than to mesh size.
    """
    scale = 1.0
    objects: dict[str, _Object] = {}
    items: list[tuple[str, np.ndarray, bool]] = []
    stack: list[ET.Element] = []
    object_id = None
    coords = array('d')
    indices = array('q')
    current = _Object()
    try:
        for event, elem in ET.iterparse(stream, events=('start', 'end')):
            tag = _local(elem.tag)
            if event == 'start':
                stack.append(elem)
                if tag == 'model':
                    unit = elem.get('unit', 'millimeter')
                    if unit not in UNIT_SCALE:
                        raise ReadError(f'{name}: unknown unit {unit!r}')
                    scale = UNIT_SCALE[unit]
                elif tag == 'object':
                    object_id = elem.get('id')
                    coords, indices, current = array('d'), array('q'), _Object()
                continue
            stack.pop()
            if tag == 'vertex':
                coords.extend((float(elem.get('x')), float(elem.get('y')),
                               float(elem.get('z'))))
                stack[-1].clear()
            elif tag == 'triangle':
                indices.extend((int(elem.get('v1')), int(elem.get('v2')),
                                int(elem.get('v3'))))
                stack[-1].clear()
            elif tag == 'component':
                current.components.append((elem.get(_PATH_ATTR) or '',
                                           elem.get('objectid'),
                                           parse_transform(elem.get('transform'))))
            elif tag == 'mesh':
                current.vertices = np.frombuffer(coords, dtype=np.float64).reshape(-1, 3)
                current.faces = np.frombuffer(indices, dtype=np.int64).reshape(-1, 3)
            elif tag == 'object':
                if object_id is None or object_id in objects:
                    raise ReadError(f'{name}: missing or duplicate object id {object_id!r}')
                objects[object_id] = current
                elem.clear()
            elif tag == 'item':
                items.append((elem.get('objectid'),
                              parse_transform(elem.get('transform')),
                              elem.get('printable', '1') != '0'))
    except ET.ParseError as error:
        raise ReadError(f'{name}: XML error: {error}') from None
    except (TypeError, ValueError) as error:
        raise ReadError(f'{name}: bad vertex or triangle: {error}') from None
    return _ModelFile(scale, objects, items)


def _model_path(base: str, path: str) -> str:
    """A component's `p:path` as a zip member name (it is package-absolute)."""
    if not path:
        return base
    return posixpath.normpath(path.lstrip('/'))


class _Reader:
    def __init__(self, archive: zipfile.ZipFile):
        self.archive = archive
        self.files: dict[str, _ModelFile] = {}

    def model(self, member: str) -> _ModelFile:
        if member not in self.files:
            try:
                with self.archive.open(member) as stream:
                    self.files[member] = _parse_model(stream, member)
            except KeyError:
                raise ReadError(f'missing model file {member}') from None
        return self.files[member]

    def geometry(self, member: str, object_id: str, matrix: np.ndarray,
                 visiting: tuple = ()) -> tuple[np.ndarray, np.ndarray]:
        """Every mesh under one object, transformed and concatenated."""
        key = (member, object_id)
        if key in visiting:
            raise ReadError(f'component cycle through object {object_id} in {member}')
        if len(visiting) >= _MAX_DEPTH:
            raise ReadError(f'components nested deeper than {_MAX_DEPTH} at object {object_id}')
        model = self.model(member)
        obj = model.objects.get(object_id)
        if obj is None:
            raise ReadError(f'{member}: missing object {object_id}')
        vertices, faces = [], []
        if obj.vertices is not None:
            if len(obj.faces) and (obj.faces.min() < 0 or obj.faces.max() >= len(obj.vertices)):
                raise ReadError(f'{member}: object {object_id} has a triangle index out of range')
            vertices.append(_apply(matrix, obj.vertices * model.scale))
            faces.append(obj.faces)
        for path, child, transform in obj.components:
            v, f = self.geometry(_model_path(member, path), child,
                                 _in_mm(transform, model) @ matrix, visiting + (key,))
            vertices.append(v)
            faces.append(f)
        offset = 0
        for i, v in enumerate(vertices):
            faces[i] = faces[i] + offset
            offset += len(v)
        if not vertices:
            return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
        return np.concatenate(vertices), np.concatenate(faces)


def _in_mm(matrix: np.ndarray, model: _ModelFile) -> np.ndarray:
    """A transform written in `model`'s unit, acting on millimetre coordinates.

    Vertices are converted to millimetres as they are read, so the 3x3 part
    is unit-free; only the translation is in the file's unit.
    """
    converted = matrix.copy()
    converted[3, :3] *= model.scale
    return converted


def _metadata(elem: ET.Element) -> dict[str, str]:
    return {m.get('key'): m.get('value', '') for m in elem.findall('metadata')
            if m.get('key') is not None}


def _bambu_config(archive: zipfile.ZipFile):
    """Objects, parts and plates from model_settings.config, or None."""
    try:
        text = archive.read('Metadata/model_settings.config')
    except KeyError:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise ReadError(f'model_settings.config: {error}') from None
    objects = {}
    for obj in root.findall('object'):
        parts = {p.get('id'): (p.get('subtype', NORMAL), _metadata(p))
                 for p in obj.findall('part')}
        objects[obj.get('id')] = (_metadata(obj), parts)
    plates = {}
    for plate in root.findall('plate'):
        number = _integer(_metadata(plate).get('plater_id', '0') or '0', 'plater_id')
        for instance in plate.findall('model_instance'):
            meta = _metadata(instance)
            key = (meta.get('object_id'), _integer(meta.get('instance_id', '0'), 'instance_id'))
            plates[key] = number
    return objects, plates


def _integer(text: str, what: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise ReadError(f'model_settings.config: {what} is not a whole number: {text!r}') from None


def _settings(archive: zipfile.ZipFile) -> dict[str, str]:
    try:
        text = archive.read('Metadata/project_settings.config')
    except KeyError:
        return {}
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ReadError(f'project_settings.config: {error}') from None
    if not isinstance(data, dict):
        raise ReadError('project_settings.config: expected a JSON object')
    settings = {}
    for key, value in data.items():
        if isinstance(value, list):
            value = value[0] if value else ''
        settings[key] = str(value)
    return settings


def _root_member(archive: zipfile.ZipFile) -> str:
    try:
        rels = ET.fromstring(archive.read('_rels/.rels'))
    except (KeyError, ET.ParseError):
        return _ROOT_MODEL
    for rel in rels:
        if rel.get('Type', '').endswith('/3dmodel'):
            return _model_path('', rel.get('Target', _ROOT_MODEL))
    return _ROOT_MODEL


#: Object metadata that is identity, not a setting override.
_NOT_SETTINGS = {'name', 'extruder'}


def read(path: str) -> Project:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        raise ReadError(f'{path}: not a readable 3MF: {error}') from None
    with archive:
        reader = _Reader(archive)
        root_member = _root_member(archive)
        root = reader.model(root_member)
        config = _bambu_config(archive)
        settings = _settings(archive)
        instances = []
        seen: dict[str, int] = {}
        for object_id, item_matrix, printable in root.items:
            if object_id not in root.objects:
                raise ReadError(f'build item refers to missing object {object_id}')
            instance_id = seen.get(object_id, 0)
            seen[object_id] = instance_id + 1
            instances.append(_instance(reader, root_member, root, config, object_id,
                                       instance_id, item_matrix, printable))
    return Project(path, settings, tuple(instances), config is not None)


def _instance(reader, root_member, root, config, object_id, instance_id,
              item_matrix, printable) -> Instance:
    obj = root.objects[object_id]
    item = _in_mm(item_matrix, root)
    meta, part_meta = ({}, {}) if config is None else config[0].get(object_id, ({}, {}))
    name = meta.get('name', f'object {object_id}')
    plate = 1 if config is None else config[1].get((object_id, instance_id), 0)
    # (zip member, object id, transform) per part: the object's own mesh, if
    # it has one, then each direct component.  A component's identity is its
    # objectid, which Bambu writes as the part id.
    sources = []
    if obj.vertices is not None:
        sources.append((root_member, object_id, item, object_id, True))
    for path, child, transform in obj.components:
        sources.append((_model_path(root_member, path), child,
                        _in_mm(transform, root) @ item, child, False))
    parts = []
    for member, source_id, matrix, part_id, own_mesh in sources:
        if own_mesh:
            v, f = _own_mesh(reader, member, source_id, matrix)
        else:
            v, f = reader.geometry(member, source_id, matrix, ((root_member, object_id),))
        if config is None:
            subtype, part_overrides, part_name = NORMAL, {}, name
        elif part_id in part_meta:
            subtype, part_overrides = part_meta[part_id]
            part_name = part_overrides.get('name', name)
        else:
            subtype, part_overrides, part_name = None, {}, f'component {part_id}'
        parts.append(Part(part_name, subtype, v, f,
                          {k: val for k, val in part_overrides.items()
                           if k not in _NOT_SETTINGS and k not in _PART_IDENTITY}))
    overrides = {k: v for k, v in meta.items() if k not in _NOT_SETTINGS}
    return Instance(object_id, instance_id, name, plate, printable, tuple(parts), overrides)


def _own_mesh(reader, member, object_id, matrix):
    """An object's own mesh, without its components."""
    model = reader.model(member)
    obj = model.objects[object_id]
    if len(obj.faces) and (obj.faces.min() < 0 or obj.faces.max() >= len(obj.vertices)):
        raise ReadError(f'{member}: object {object_id} has a triangle index out of range')
    return _apply(matrix, obj.vertices * model.scale), obj.faces.copy()


#: Part metadata that is identity or bookkeeping, not a setting override.
_PART_IDENTITY = {'matrix', 'source_file', 'source_object_id', 'source_volume_id',
                  'source_offset_x', 'source_offset_y', 'source_offset_z'}
