"""LOD chain for one monster, in headless Blender.

Run by tools/forge.py (stage `lod`), after QA:
    blender -b --factory-startup -P tools/blender_lods.py -- '<job json>'

LOD0 is model.glb itself (the importance-weighted bake, scaled, oriented and leveled by
QA). LOD1..n are collapse decimations of LOD0 that keep its UV layout, so every LOD uses
LOD0's material and textures (one material set per monster):

- the GLB is imported as is: glTF splits vertices along UV seams, so every seam is an open
  boundary that the collapse preserves (boundary edges carry a large quadric weight) and
  every corner keeps its exact UV. On the harpy this gave cleaner LOD2/LOD3 than merging
  the seams first (`merge`: true), which let collapses drag UVs across charts (skin-
  coloured slivers on the wings) and stopped at a topology floor above the LOD3 target;
- Decimate (Collapse, triangulate) to max(min_tris, ratio x LOD0 tris), ratios of LOD0,
  with the vertices within 1% of the height above the ground locked (feet stay planted);
- shading is smooth (the baked normal map carries the detail);
- export with the same glTF settings as QA, so scale, orientation and origin match
  model.glb exactly. By default the LOD files carry the material (same name, same
  factors) but no images (`embed_textures` false): an engine binds LOD0's material, and the
  viewer reuses the loaded LOD0 material. `embed_textures` true writes self-contained
  files (about 1x the texture bytes again per LOD).

Job keys: src (model.glb), out_dir, ratios {"1": 0.5, ...} (else `lod_ratios` from
data/overrides/<source>/<id>.json when id + source are given, else 0.5 / 0.25 / 0.1), min_tris (300),
embed_textures (false), merge (false), report (lods.json path).
"""
import json
import os
import sys
import time

import bpy

job = json.loads(sys.argv[sys.argv.index("--") + 1])
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import overrides  # noqa: E402  (tools/overrides.py)

if not job.get("ratios"):
    _r = None
    if job.get("id") and job.get("source"):
        _r = overrides.load(job["source"], job["id"]).get("lod_ratios")
    job["ratios"] = {str(i + 1): r for i, r in enumerate(_r or overrides.LOD_RATIOS)}
ratios = {int(k): float(v) for k, v in job["ratios"].items()}
min_tris = int(job.get("min_tris", 300))
embed = bool(job.get("embed_textures", False))
out_dir = job.get("out_dir") or os.path.dirname(job["src"])
t0 = time.time()


def load():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=job["src"], merge_vertices=bool(job.get("merge", False)))
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if len(meshes) != 1:
        raise SystemExit(f"expected one mesh in {job['src']}, got {len(meshes)}")
    return meshes[0]


def tris(me):
    return sum(len(p.vertices) - 2 for p in me.polygons)


def bounds(obj):
    import numpy as np
    co = np.empty(len(obj.data.vertices) * 3)
    obj.data.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3) @ np.array(obj.matrix_world.to_3x3()).T + np.array(obj.matrix_world.translation)
    return [[round(float(x), 4) for x in co.min(0)], [round(float(x), 4) for x in co.max(0)]]


obj = load()
base_tris = tris(obj.data)
report = {"src": os.path.basename(job["src"]), "embed_textures": embed, "min_tris": min_tris,
          "lods": {"0": {"file": os.path.basename(job["src"]), "tris": base_tris, "ratio": 1.0,
                         "bounds": bounds(obj)}}}
for lod in sorted(ratios):
    if lod == 0:
        continue
    obj = load()
    target = max(min_tris, int(round(ratios[lod] * base_tris)))
    dst = os.path.join(out_dir, f"model_lod{lod}.glb")
    if target >= base_tris:
        report["lods"][str(lod)] = {"file": None, "tris": base_tris, "ratio": ratios[lod],
                                    "skipped": "target >= LOD0"}
        continue
    # Blender's ratio is over faces; iterate a couple of times to land near the target.
    ratio = target / base_tris
    # Lock the ground contact (vertices within 1% of the height of y=0) so the collapse
    # cannot lift or sink the feet; the vertex-group weight is "may be decimated".
    zs = [v.co.z for v in obj.data.vertices]
    zmin, h = min(zs), max(zs) - min(zs)
    vg = obj.vertex_groups.new(name="decimate")
    vg.add([v.index for v in obj.data.vertices if v.co.z > zmin + 0.01 * h], 1.0, "REPLACE")
    vg.add([v.index for v in obj.data.vertices if v.co.z <= zmin + 0.01 * h], 0.0, "REPLACE")
    mod = obj.modifiers.new("dec", "DECIMATE")
    mod.vertex_group = vg.name
    mod.vertex_group_factor = 1.0
    mod.decimate_type = "COLLAPSE"
    mod.use_collapse_triangulate = True
    for _ in range(4):
        mod.ratio = ratio
        got = tris(obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data)
        if abs(got - target) <= 0.03 * target:
            break
        ratio = min(1.0, ratio * target / max(got, 1))
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=mod.name)
    me = obj.data
    for v in me.vertices:  # QEM placement can push a vertex near the feet under the ground
        if v.co.z < zmin:
            v.co.z = zmin
    if me.has_custom_normals:
        bpy.ops.mesh.customdata_custom_splitnormals_clear()
    me.shade_smooth()
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.export_scene.gltf(filepath=dst, export_format="GLB", use_selection=True, export_yup=True,
                              export_apply=True, export_image_format="AUTO" if embed else "NONE")
    report["lods"][str(lod)] = {"file": os.path.basename(dst), "tris": tris(me), "ratio": ratios[lod],
                                "target": target, "bounds": bounds(obj),
                                "bytes": os.path.getsize(dst)}
report["seconds"] = round(time.time() - t0, 1)
with open(job["report"], "w") as f:
    json.dump(report, f, indent=2)
print("LODS_OK", json.dumps(report))
