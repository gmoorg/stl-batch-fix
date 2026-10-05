"""Synthetic solids and 3MF archives for the base-layer risk checker tests.

Every solid is a closed height-field slab: a bottom grid whose heights come
from a function, a flat top, and side walls stitching the two.  The expected
answers in the tests follow from that function on paper (an area, a height
range), which is the point — see test_scanner's note on fixtures.
"""

from __future__ import annotations

import io
import json
import zipfile

import numpy as np

CORE_NS = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
PROD_NS = 'http://schemas.microsoft.com/3dmanufacturing/production/2015/06'


def slab(bottom=lambda x, y: np.zeros_like(x), size=(10.0, 10.0), n=(40, 40),
         top=5.0, origin=(0.0, 0.0), z_offset=0.0):
    """A closed solid over [origin, origin+size] with bottom z = bottom(x, y).

    Returns (vertices, faces).  Every edge is shared by exactly two faces.
    """
    nx, ny = n
    xs = np.linspace(origin[0], origin[0] + size[0], nx + 1)
    ys = np.linspace(origin[1], origin[1] + size[1], ny + 1)
    gx, gy = np.meshgrid(xs, ys, indexing='ij')
    bz = bottom(gx, gy) + z_offset
    count = (nx + 1) * (ny + 1)
    lower = np.stack([gx, gy, bz], axis=-1).reshape(-1, 3)
    upper = np.stack([gx, gy, np.full_like(gx, top + z_offset)], axis=-1).reshape(-1, 3)
    vertices = np.concatenate([lower, upper])

    def vid(i, j, layer):
        return layer * count + i * (ny + 1) + j

    faces = []
    for i in range(nx):
        for j in range(ny):
            a, b, c, d = vid(i, j, 0), vid(i + 1, j, 0), vid(i + 1, j + 1, 0), vid(i, j + 1, 0)
            faces += [(a, c, b), (a, d, c)]                 # bottom, facing down
            a, b, c, d = (v + count for v in (a, b, c, d))
            faces += [(a, b, c), (a, c, d)]                 # top, facing up
    ring = ([(i, 0) for i in range(nx)] + [(nx, j) for j in range(ny)]
            + [(i, ny) for i in range(nx, 0, -1)] + [(0, j) for j in range(ny, 0, -1)])
    for k, (i, j) in enumerate(ring):
        i2, j2 = ring[(k + 1) % len(ring)]
        a, b = vid(i, j, 0), vid(i2, j2, 0)
        faces += [(a, b, b + count), (a, b + count, a + count)]
    return vertices, np.array(faces, dtype=np.int64)


def triangles_of(vertices, faces):
    return vertices[faces]


# ------------------------------------------------------------------- 3MF

def _mesh_xml(object_id, vertices, faces):
    v = ''.join(f'<vertex x="{x!r}" y="{y!r}" z="{z!r}"/>' for x, y, z in vertices.tolist())
    t = ''.join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces.tolist())
    return (f'<object id="{object_id}" type="model"><mesh><vertices>{v}</vertices>'
            f'<triangles>{t}</triangles></mesh></object>')


def transform_text(matrix4):
    """A 4x4 row-vector affine as the 12-number 3MF string."""
    return ' '.join(repr(float(v)) for v in np.asarray(matrix4)[:, :3].reshape(-1))


def write_3mf(path, objects, items, *, settings=None, bambu=True, unit='millimeter',
              plates=None, part_matrix=None):
    """Write a 3MF.

    objects  {object_id: dict(name=, parts=[dict(id=, subtype=, mesh=(v, f),
              transform=4x4 or None, overrides={})], overrides={})}
             Bambu layout: each object is a component wrapper around part
             meshes stored in 3D/Objects/object_<id>.model.
             With bambu=False, each object holds its first part's mesh inline.
    items    [(object_id, 4x4 transform, printable)]
    plates   {plate number: [(object_id, instance_id)]}; default all on plate 1
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('_rels/.rels',
                         '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
                         'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        resources = []
        for object_id, spec in objects.items():
            if not bambu:
                v, f = spec['parts'][0]['mesh']
                resources.append(_mesh_xml(object_id, v, f))
                continue
            member = f'3D/Objects/object_{object_id}.model'
            sub = ''.join(_mesh_xml(p['id'], *p['mesh']) for p in spec['parts'])
            archive.writestr(member, f'<?xml version="1.0"?><model unit="{unit}" xmlns="{CORE_NS}">'
                                     f'<resources>{sub}</resources><build/></model>')
            comps = ''.join(
                f'<component p:path="/{member}" objectid="{p["id"]}"'
                + (f' transform="{transform_text(p["transform"])}"' if p.get('transform') is not None else '')
                + '/>' for p in spec['parts'])
            resources.append(f'<object id="{object_id}" type="model"><components>{comps}</components></object>')
        build = ''.join(f'<item objectid="{oid}" transform="{transform_text(m)}" printable="{1 if pr else 0}"/>'
                        for oid, m, pr in items)
        if bambu:
            rels = ''.join(f'<Relationship Target="/3D/Objects/object_{oid}.model" Id="rel-{oid}" '
                           'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
                           for oid in objects)
            archive.writestr('3D/_rels/3dmodel.model.rels',
                             '<?xml version="1.0"?><Relationships xmlns='
                             '"http://schemas.openxmlformats.org/package/2006/relationships">'
                             f'{rels}</Relationships>')
        archive.writestr('3D/3dmodel.model',
                         f'<?xml version="1.0"?><model unit="{unit}" xmlns="{CORE_NS}" xmlns:p="{PROD_NS}">'
                         f'<resources>{"".join(resources)}</resources><build>{build}</build></model>')
        if bambu:
            if plates is None:
                counts = {}
                plates = {1: []}
                for oid, _, _ in items:
                    plates[1].append((oid, counts.get(oid, 0)))
                    counts[oid] = counts.get(oid, 0) + 1
            config = ['<?xml version="1.0"?><config>']
            for object_id, spec in objects.items():
                config.append(f'<object id="{object_id}"><metadata key="name" value="{spec.get("name", object_id)}"/>')
                for key, value in spec.get('overrides', {}).items():
                    config.append(f'<metadata key="{key}" value="{value}"/>')
                for p in spec['parts']:
                    pid = p.get('meta_id', p['id'])
                    config.append(f'<part id="{pid}" subtype="{p.get("subtype", "normal_part")}">'
                                  f'<metadata key="name" value="part{pid}"/>')
                    if part_matrix is not None:
                        config.append(f'<metadata key="matrix" value="{part_matrix}"/>')
                    for key, value in p.get('overrides', {}).items():
                        config.append(f'<metadata key="{key}" value="{value}"/>')
                    config.append('</part>')
                config.append('</object>')
            for number, members in plates.items():
                config.append(f'<plate><metadata key="plater_id" value="{number}"/>')
                for oid, iid in members:
                    config.append(f'<model_instance><metadata key="object_id" value="{oid}"/>'
                                  f'<metadata key="instance_id" value="{iid}"/></model_instance>')
                config.append('</plate>')
            config.append('</config>')
            archive.writestr('Metadata/model_settings.config', ''.join(config))
            archive.writestr('Metadata/project_settings.config', json.dumps({} if settings is None else settings))
    with open(path, 'wb') as handle:
        handle.write(buffer.getvalue())


def translate(x=0.0, y=0.0, z=0.0, scale=1.0, rotate_z_deg=0.0):
    angle = np.radians(rotate_z_deg)
    c, s = np.cos(angle), np.sin(angle)
    m = np.eye(4)
    # Row-vector convention: p' = p @ R + t.
    m[:3, :3] = np.array([[c, s, 0], [-s, c, 0], [0, 0, 1]]) * scale
    m[3, :3] = (x, y, z)
    return m
