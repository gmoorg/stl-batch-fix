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
    transform   the build item's 4x4 row-vector matrix in millimetres:
                world = [object coordinates, 1] @ transform
    """

    object_id: str
    instance_id: int
    name: str
    plate: int
    printable: bool
    parts: tuple[Part, ...]
    overrides: dict[str, str] = field(default_factory=dict)
    transform: np.ndarray = field(default_factory=lambda: np.eye(4))

    @property
    def normal_parts(self) -> tuple[Part, ...]:
        return tuple(p for p in self.parts if p.subtype == NORMAL)


@dataclass(frozen=True)
class Project:
    """A read 3MF.

    root_member     zip member holding the build (usually 3D/3dmodel.model)
    root_in_mm      the root model's unit is millimetres
    used_ids        every object id in every model file read, plus every part
                    id in model_settings.config — what a new id must avoid
    """

    path: str
    settings: dict[str, str]
    instances: tuple[Instance, ...]
    is_bambu: bool
    root_member: str = _ROOT_MODEL
    root_in_mm: bool = True
    used_ids: frozenset[str] = frozenset()


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
        used = set()
        for member in archive.namelist():
            if member.startswith('3D/') and member.endswith('.model'):
                used |= set(reader.model(member).objects)
        if config is not None:
            for _, part_meta in config[0].values():
                used |= set(part_meta)
    return Project(path, settings, tuple(instances), config is not None,
                   root_member, root.scale == 1.0, frozenset(used))


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
    return Instance(object_id, instance_id, name, plate, printable, tuple(parts), overrides,
                    item)


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


# ------------------------------------------------------------------ writing

_CORE_NS = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
_PROD_NS = 'http://schemas.microsoft.com/3dmanufacturing/production/2015/06'
_RELS_MEMBER = '3D/_rels/3dmodel.model.rels'
_SETTINGS_MEMBER = 'Metadata/model_settings.config'
_MODEL_REL_TYPE = 'http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel'


@dataclass(frozen=True)
class Addition:
    """A new printable part for one object, in WORLD millimetres.

    It is converted to object coordinates through the instance's build
    transform, so the object must have exactly one instance.
    """

    instance: Instance
    name: str
    vertices: np.ndarray
    faces: np.ndarray


def add_parts(project: Project, destination: str, additions: list[Addition]) -> None:
    """Write a copy of `project` with each addition as a new normal part.

    The source file is never modified, and `destination` must not exist: the
    copy is built in a temporary file beside it, re-read to check that every
    addition is present and placed, and only then linked into place.

    Only Bambu projects whose root objects are component wrappers, in
    millimetres, are supported.  The XML is edited as text so everything not
    being changed stays byte-for-byte as Bambu wrote it (a parser round-trip
    would rewrite namespace prefixes); every edit point must be found exactly
    once, or nothing is written.
    """
    import os
    import tempfile
    import uuid
    from xml.sax.saxutils import quoteattr

    if not project.is_bambu:
        raise ReadError('only Bambu Studio projects can be extended')
    if not project.root_in_mm:
        raise ReadError('the root model is not in millimetres')
    source = os.path.realpath(project.path)
    target = os.path.realpath(destination)
    if source == target or os.path.exists(destination):
        raise ReadError(f'refusing to write {destination}: it exists or is the source')
    counts: dict[str, int] = {}
    for instance in project.instances:
        counts[instance.object_id] = counts.get(instance.object_id, 0) + 1
    for addition in additions:
        if counts.get(addition.instance.object_id) != 1:
            raise ReadError(f'object {addition.instance.object_id} has several instances; '
                            'a part added to it cannot fit all of them')
    if len({a.instance.object_id for a in additions}) != len(additions):
        raise ReadError('at most one addition per object')

    with zipfile.ZipFile(project.path) as archive:
        members = {info.filename: info for info in archive.infolist()}
        root_text = archive.read(project.root_member).decode('utf-8')
        rels_text = archive.read(_RELS_MEMBER).decode('utf-8') if _RELS_MEMBER in members else None
        settings_text = archive.read(_SETTINGS_MEMBER).decode('utf-8')
        if rels_text is None:
            raise ReadError(f'{_RELS_MEMBER} is missing')

        prefix = _namespace_prefix(root_text, _PROD_NS)
        next_id = 1 + max((int(i) for i in project.used_ids if i.isdigit()), default=0)
        new_members = {}
        for addition in additions:
            object_id = addition.instance.object_id
            new_id = str(next_id)
            next_id += 1
            member = f'3D/Objects/support_{new_id}.model'
            if member in members or member in new_members:
                raise ReadError(f'{member} already exists')
            local = _to_object(addition.instance.transform, addition.vertices)
            new_members[member] = _mesh_model(new_id, local, addition.faces, str(uuid.uuid4()))
            component_uuid = uuid.uuid4()

            def component(core, member=member, new_id=new_id, uid=component_uuid):
                # `core` is the prefix the object element uses for the core
                # namespace, so the new element lands in the same namespace.
                return (f'<{core}component {prefix}:path="/{member}" objectid="{new_id}" '
                        f'{prefix}:UUID="{uid}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>')
            root_text = _insert_in_object(root_text, object_id, 'components', component,
                                          project.root_member)
            rels_text = _insert_once(
                rels_text, '</Relationships>',
                f' <Relationship Target="/{member}" Id="rel-support-{new_id}" '
                f'Type="{_MODEL_REL_TYPE}"/>\n', _RELS_MEMBER)
            part = (f'    <part id="{new_id}" subtype="{NORMAL}" uuid="{uuid.uuid4()}">\n'
                    f'      <metadata key="name" value={quoteattr(addition.name)}/>\n'
                    f'      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
                    f'      <mesh_stat face_count="{len(addition.faces)}" edges_fixed="0" '
                    f'degenerate_facets="0" facets_removed="0" facets_reversed="0" '
                    f'backwards_edges="0"/>\n    </part>\n  ')
            settings_text = _insert_in_object(settings_text, object_id, None,
                                              lambda core, part=part: part,
                                              _SETTINGS_MEMBER, len(addition.faces))

        replaced = {project.root_member: root_text, _RELS_MEMBER: rels_text,
                    _SETTINGS_MEMBER: settings_text}
        directory = os.path.dirname(target) or '.'
        handle, staged = tempfile.mkstemp(dir=directory, prefix='.3mf-', suffix='.part')
        os.close(handle)
        try:
            # mkstemp makes the file private (0600); give it the permissions
            # any newly created file would get, as the published link keeps them.
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(staged, 0o666 & ~umask)
            with zipfile.ZipFile(staged, 'w', zipfile.ZIP_DEFLATED) as out:
                for name, info in members.items():
                    data = replaced[name].encode('utf-8') if name in replaced else archive.read(name)
                    out.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
                for name, text in new_members.items():
                    out.writestr(name, text.encode('utf-8'))
            _verify(staged, project, additions)
            _publish(staged, target)
        finally:
            os.unlink(staged)


def _publish(staged: str, target: str) -> None:
    """Put the finished file at `target`, never replacing an existing file.

    A hard link is atomic and refuses an existing name.  Filesystems without
    hard links (FAT, some network shares) get an exclusive create and a copy.
    """
    import errno
    import os
    import shutil
    try:
        os.link(staged, target)
        return
    except FileExistsError:
        raise ReadError(f'refusing to write {target}: it exists') from None
    except OSError as error:
        if error.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV):
            raise
    try:
        with open(staged, 'rb') as source, open(target, 'xb') as out:
            shutil.copyfileobj(source, out)
    except FileExistsError:
        raise ReadError(f'refusing to write {target}: it exists') from None
    except BaseException:
        if os.path.exists(target):
            os.unlink(target)
        raise


def _to_object(transform: np.ndarray, world: np.ndarray) -> np.ndarray:
    """World millimetres -> the object's own coordinates (inverse build item)."""
    return (np.asarray(world, dtype=np.float64) - transform[3, :3]) @ np.linalg.inv(transform[:3, :3])


def _mesh_model(object_id: str, vertices: np.ndarray, faces: np.ndarray, uid: str) -> str:
    vertex_xml = '\n'.join(f'     <vertex x="{x:.9g}" y="{y:.9g}" z="{z:.9g}"/>'
                           for x, y, z in vertices.tolist())
    triangle_xml = '\n'.join(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>'
                             for a, b, c in faces.tolist())
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<model unit="millimeter" xml:lang="en-US" xmlns="{_CORE_NS}" '
            f'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
            f'xmlns:p="{_PROD_NS}" requiredextensions="p">\n'
            ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
            ' <resources>\n'
            f'  <object id="{object_id}" p:UUID="{uid}" type="model">\n'
            '   <mesh>\n    <vertices>\n' + vertex_xml + '\n    </vertices>\n'
            '    <triangles>\n' + triangle_xml + '\n    </triangles>\n'
            '   </mesh>\n  </object>\n </resources>\n <build/>\n</model>\n')


def _namespace_prefix(text: str, namespace: str) -> str:
    import re
    found = re.findall(r'xmlns:([A-Za-z_][\w.-]*)\s*=\s*["\']' + re.escape(namespace) + r'["\']',
                       text)
    if len(found) != 1:
        raise ReadError('root model must declare the 3MF production namespace exactly once')
    return found[0]


def _insert_once(text: str, marker: str, insertion: str, member: str) -> str:
    if text.count(marker) != 1:
        raise ReadError(f'{member}: expected exactly one {marker!r}')
    return text.replace(marker, insertion + marker)


def _insert_in_object(text: str, object_id: str, container: str | None, insertion,
                      member: str, added_faces: int | None = None) -> str:
    """Insert before `</container>` (or `</object>`) inside object `object_id`.

    The object element may carry a namespace prefix and its attributes in any
    order or quoting; it must occur exactly once and not be self-closing.
    `insertion(prefix)` builds the text, given the object's own prefix
    (e.g. 'c:' or '').
    """
    import re
    opens = [m for m in re.finditer(r'<((?:[A-Za-z_][\w.-]*:)?)object\b([^>]*)>', text)
             if re.search(r'\bid\s*=\s*(["\'])' + re.escape(object_id) + r'\1', m.group(2))]
    if len(opens) != 1 or opens[0].group(2).rstrip().endswith('/'):
        raise ReadError(f'{member}: expected exactly one open <object id="{object_id}">')
    prefix = opens[0].group(1)
    start = opens[0].end()
    end = text.find(f'</{prefix}object>', start)
    if end < 0:
        raise ReadError(f'{member}: object {object_id} is not closed')
    body = text[start:end]
    closing = f'</{prefix}{container}>' if container else None
    if closing is not None:
        if body.count(closing) != 1:
            raise ReadError(f'{member}: object {object_id} needs exactly one {closing}')
        body = body.replace(closing, insertion(prefix) + closing)
    else:
        body = body + insertion(prefix)
    if added_faces is not None:
        body = re.sub(r'(<metadata\s+face_count\s*=\s*["\'])(\d+)(["\'])',
                      lambda m: f'{m.group(1)}{int(m.group(2)) + added_faces}{m.group(3)}',
                      body, count=1)
    return text[:start] + body + text[end:]


def _verify(path: str, before: Project, additions: list[Addition]) -> None:
    """Re-read the written copy: the old parts unchanged, each addition placed."""
    after = read(path)
    old = {(i.object_id, i.instance_id): i for i in before.instances}
    added = {a.instance.object_id: a for a in additions}
    for instance in after.instances:
        previous = old[(instance.object_id, instance.instance_id)]
        extra = len(instance.parts) - len(previous.parts)
        if extra != (1 if instance.object_id in added else 0):
            raise ReadError(f'written copy: object {instance.object_id} has {extra} new parts')
        for a, b in zip(previous.parts, instance.parts):
            if a.subtype != b.subtype or not np.allclose(a.vertices, b.vertices, atol=1e-6):
                raise ReadError(f'written copy: object {instance.object_id} changed')
        if instance.object_id in added:
            new = instance.parts[-1]
            want = added[instance.object_id].vertices
            if new.subtype != NORMAL or not np.allclose(new.vertices, want, atol=1e-5):
                raise ReadError(f'written copy: the part added to object {instance.object_id} '
                                'is not where it was meant to be')
