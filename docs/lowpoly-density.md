# Low-poly density: budgets and importance-weighted decimation (2026-11-03)

Problem: the M1 low-poly meshes had far too few triangles on faces. The harpy (2965 tris,
uniform GPU decimation of a 345K-face remesh) had about 15 triangles on the whole face, a
long collapse edge across the cheek that read as a gash, and a mis-baked normal-map
triangle beside it. The face texture was blurry too (1024 atlas for a 1.7 m body).

## What ComfyUI offers

- `DecimateMesh` (`comfy_extras/nodes_mesh_postprocess.py`) is uniform. Its options are
  `midpoint` / `qem` placement plus line-quadric and feature-edge (crease) quadric weights.
  There is no per-vertex weight or importance input, and `QEMConfig` has none either.
- Meshes can be loaded back from disk: `Load3DAdvanced` (file under `input/3d/`) ->
  `Get3DComponents` (`nodes_mesh_io.py`) parses GLB/GLTF/OBJ/STL into a MESH. Load3D has
  no change check, so a changed file needs a new name, or ComfyUI serves the cached mesh.
- `SaveGLB` saves any MESH, so the remesh output can be written out. It carries no
  texture: colour lives in the Trellis voxels and is baked by `BakeTextureFromVoxel`.
- `BakeNormalMapFromMesh.cage_distance` is a fraction of the bbox diagonal. The old 0.05
  is about 14 cm on the harpy, enough to grab across features. The new default is 0.015.

## Pipeline chosen

Blender decimates, ComfyUI bakes. This keeps the voxel colour bake, which is the best
colour source available; a Blender-only bake would need a textured intermediate mesh.

1. ComfyUI pass 1: generation + RemeshMesh, `SaveGLB` of the remesh -> `model_hi.glb`
   (kept in the monster's out dir).
2. `tools/blender_decimate.py` (headless, ~5-15 s):
   - **un-pinch**: the UDF remesh leaves ~900-1900 edges shared by 4 faces (two sheets
     touching in one voxel). Blender's collapse never touches non-manifold edges; they
     kept dense fans and ate the budget (7.9k faces on 3.0k verts), and the first bake on
     such a mesh coincided with a ComfyUI crash. The faces around each edge are paired
     into sheets by solid wedge and each sheet gets its own vertex copy. Edges this
     cannot resolve are split. The decimated mesh has 0 non-manifold edges.
   - **importance** per vertex = 0.6 curvature (mean dihedral angle, smoothed 6 rings,
     normalised median..p97) + 0.7 head (sphere around the face, found like the QA head
     close-up: front-most surface in the upper body; plan-dependent) + 0.35 extremity
     (thin by inward ray cast and far from the centroid). A plain "top 20% of the height"
     head band failed on the harpy: the horns took it.
   - Decimate (Collapse) with a vertex group of `1 - 0.9 * importance` (Blender reads the
     weight as "may be decimated"; 0 locks the vertex), `vertex_group_factor` 0.5.
   - **topology floor**: Blender's collapse keeps topology, so meshes with many handles
     have a floor (goblin: ~2.8k faces), and near it the collapse degenerates into
     shards. The script probes the floor on a copy; if the target is under 1.6 x the
     floor it writes nothing and `forge.py` falls back to the GPU `DecimateMesh`
     (`decimator_used` in the manifest says which ran).
   - `importance.png` (front + side heatmap) and `decimate.json` go in the out dir.
3. ComfyUI pass 2: the same graph with `DecimateMesh` replaced by Load3DAdvanced ->
   Get3DComponents; the upload name carries the mesh's crc32. Everything upstream is
   cached, so only unwrap and the bakes run (10-30 s).

The old path is `forge.py mesh <id> --decimator comfy`.

## Experiments (harpy medium, goblin small; same seeds)

Variants: **baseline** (M1: GPU decimate, 3000/2000 tris, 1K, cage 0.05), **budget**
(GPU decimate, x2.5 tris = 7500/5000, 2K for medium, cage 0.015), **weighted** (Blender
weighted at the baseline counts, 1K, cage 0.05), **weighted+budget** (Blender weighted at
the budget counts, 2K for medium, cage 0.015). Head tris = triangles with the centroid in
a fixed cube around the face (harpy 0.408 m / face 0.245 m; goblin 0.445 m / face
0.267 m), the same cube for every variant.

| variant | harpy tris | head | face | goblin tris | head | face |
|---|---|---|---|---|---|---|
| baseline | 2965 | 202 | 63 | 1732 | 606 | 232 |
| budget | 7424 | 517 | 171 | 4939 | 1758 | 614 |
| weighted | 2930 | 243 | 86 | 1766 (fallback) | 551 | 170 |
| weighted+budget, f 0.5 | 7430 | 1161 | 501 | 4911 | 1664 | 546 |
| weighted+budget, f 1.0 | 7430 | 1455 | 662 | 4911 | 1698 | 563 |

The goblin at 2000 is under 1.6 x its topology floor (2834), so "weighted" fell back to
the GPU decimator. A forced Blender run at 2000 turned the goblin into shards, which is
why the fallback exists.

Timings for an uncontended run: weighted mesh stage 45-50 s (generate 12 s, Blender
8-15 s, bake 10-30 s) against 22-44 s for the GPU path. The extra Blender pass costs
about 10-20 s per monster.

### Verdict (from the 4-up images; I looked at all of them)

- The **budget increase does most of the work.** The harpy gash and the bad normal
  triangle are gone at 7.4k uniform, and the goblin's face goes from smeared shards to
  readable eyes, nose and mouth.
- **Weighting is a clear gain on the harpy** (2.9x the face triangles at the same total,
  nose and cheek shape follow the high-poly better). On the goblin it is roughly neutral:
  its head is half the model, and armour, the bow and claws win on curvature. The feet
  and claws come out slightly cleaner.
- Full-body views show no regressions from weighting: the wings, horns, bow and armour
  all survive.
- f 1.0 adds face triangles over f 0.5 with little visible gain. **f 0.5 is the default.**
- The face texture is still soft on the harpy even at 2K. That comes from the voxel
  colour resolution (Pixal3D 512), not the atlas: `--upsample 1024` is the lever there.

Images (git-ignored): `out/srd-5.2.1/_lowpoly_cmp/{harpy,goblin}_4up_head.png`,
`*_4up_body.png`, `*_factor_head.png`, `pilot_heads.png`.

## Defaults now

| setting | old | new |
|---|---|---|
| decimator | GPU `DecimateMesh` (uniform) | Blender importance-weighted (`--decimator comfy` for old) |
| tris tiny / small / medium / large / huge / gargantuan | 2000 / 2000 / 3000 / 5000 / 6500 / 8000 | 3000 / 5000 / 7000 / 9000 / 12000 / 15000 |
| texture + UV atlas | 1024 | 2048 for medium and up, 1024 tiny/small |
| normal cage | 0.05 | 0.015 |
| vertex_group_factor | — | 0.5 |
| QA | 4-view strip | + `thumb_head.png` (shaded + wireframe head close-up, 1024x512), `head_region` in qa.json |

## Pilot rerun with the new defaults

| monster | target | tris | head-box tris | topology floor | mesh s | non-manifold (QA weld) | parts |
|---|---|---|---|---|---|---|---|
| goblin-warrior | 5000 | 4911 | 1662 | 2822 | 39 | 263 | 51 |
| wolf | 7000 | 6991 | 2465 | 216 | 199* | 27 | 8 |
| owlbear | 9000 | 8875 | 3382 | 4202 | 461* | 375 | 90 |
| giant-spider | 9000 | 8993 | 2880 | 813 | 231* | 21 | 3 |
| harpy | 7000 | 6930 | 1058 | 1225 | 51 | 204 | 50 |
| young-red-dragon | 9000 | 8976 | 5199 | 418 | 53 | 72 | 17 |

\* These ran while another client's SDXL jobs shared the ComfyUI server (RAM and swap
were full). The uncontended runs took 39-53 s. Head-box counts use each model's own QA
box, so compare them within a monster, not across monsters.

## Caveats

- **Parts and non-manifold edges.** The un-pinched sheets touch without being connected.
  After QA's UV-seam weld they show up as 21-375 non-manifold edges and 3-90 loose parts
  (M1 had 1 part and 5-110 edges). This is harmless for rendering and for skinning by
  weight painting, but the M3 manifold cleanup (remove doubles, then a voxel or
  `FillHoles` pass) should run before rigging.
- **Floor fallback.** Tiny monsters with weapons or straps can fall under 1.6 x the floor
  at 3000 tris and get the uniform decimator. Check `decimator_used` in a batch.
- **QA facing** is borderline on the wolf (end-area ratio 1.14, against a 1.25
  threshold). It is now pinned with `qa wolf --yaw 116.93`.
- **Head close-up framing.** Upright plans frame the face from the nose rule. Head-end
  plans frame the bounding box of the front 30%, which includes the wings on the dragon.
