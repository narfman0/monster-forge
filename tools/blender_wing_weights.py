"""Give hand-added wing chains clean, geometric skin weights.

SkinTokens spreads a wing's vertices over the forearm, thigh and spine even when it is
given a wing chain (docs/TODO.md), so the wing tears on a flap. Here a vertex belongs to a
wing when it is within `reach` of the chain and closer to it than to any other bone, with
the chain's distance scaled by `bias` because a chain runs down the middle of a wide wing. Those vertices go to
their two nearest wing bones by inverse distance; a band around the crossover blends in
the original weights so the join does not crease.

    blender -b --factory-startup -P tools/blender_wing_weights.py -- '<json job>'

job: src, dst (GLB), report (json path), wings: [[bone...], ...] (one chain per wing),
bias (default 1.0), reach (metres from the chain, default 0.4), blend (metres, default 0.04).
"""
import json
import sys

import bpy
import numpy as np

job = json.loads(sys.argv[sys.argv.index("--") + 1])
blend = job.get("blend", 0.04)
bias = job.get("bias", 1.0)
reach = job.get("reach", 0.4)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=job["src"])
arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
me = max((o for o in bpy.data.objects if o.type == "MESH"), key=lambda o: len(o.data.vertices))
for o in list(bpy.data.objects):  # glTF bone display shapes
    if o.type == "MESH" and o is not me:
        bpy.data.objects.remove(o)

V = np.array([(me.matrix_world @ v.co)[:] for v in me.data.vertices])
seg = {b.name: (np.array((arm.matrix_world @ b.head_local)[:]), np.array((arm.matrix_world @ b.tail_local)[:]))
       for b in arm.data.bones}


def seg_dist(name: str) -> np.ndarray:
    a, b = seg[name]
    ab = b - a
    t = np.clip(((V - a) @ ab) / max(float(ab @ ab), 1e-9), 0.0, 1.0)
    return np.linalg.norm(V - (a + t[:, None] * ab), axis=1)


dist = {b: seg_dist(b) for b in seg}
report = {"wings": []}
for wing in job["wings"]:
    d_wing = np.stack([dist[b] for b in wing], axis=1)
    near = d_wing.min(axis=1)
    d_other = np.min([d for b, d in dist.items() if b not in wing], axis=0)
    groups = [me.vertex_groups.get(b) or me.vertex_groups.new(name=b) for b in wing]
    moved = 0
    for v in me.data.vertices:
        i = v.index
        # k = 1 inside the wing, 0 on the body, ramps across `blend` around the crossover
        k = float(np.clip((d_other[i] - bias * near[i]) / blend + 0.5, 0.0, 1.0))
        if k == 0.0 or near[i] > reach:
            continue
        for g in list(v.groups):
            me.vertex_groups[g.group].add([i], g.weight * (1.0 - k), "REPLACE")
        a, b = np.argsort(d_wing[i])[:2]
        wa, wb = 1.0 / (d_wing[i, a] + 1e-4), 1.0 / (d_wing[i, b] + 1e-4)
        groups[a].add([i], k * wa / (wa + wb), "ADD")
        groups[b].add([i], k * wb / (wa + wb), "ADD")
        moved += 1
    report["wings"].append({"bones": wing, "moved_verts": moved})

bpy.ops.object.select_all(action="SELECT")
bpy.ops.export_scene.gltf(filepath=job["dst"], export_format="GLB", use_selection=True, export_yup=True)
with open(job["report"], "w") as f:
    json.dump(report, f, indent=1)
