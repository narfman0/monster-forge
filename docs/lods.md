# LOD chain, stance leveling and per-monster overrides (2026-11-03)

## LOD chain

Every monster ships as a chain of meshes that share one material set:

| file | tier | how | triangles |
|---|---|---|---|
| `model.glb` | LOD0, detail | importance-weighted Blender decimation of `model_hi.glb`, ComfyUI bake, QA (scale, facing, leveling) | size budget (below) |
| `model_lod1.glb` | LOD1 | collapse decimation of LOD0 | 0.5 x LOD0 |
| `model_lod2.glb` | LOD2 | collapse decimation of LOD0 | 0.25 x LOD0 |
| `model_lod3.glb` | LOD3 | collapse decimation of LOD0 | 0.1 x LOD0, at least 300 |
| `lods.json` | index | `{"lods": {"0": {"file", "tris", "ratio", "bounds"}, ...}}` | |

LOD0 budget by size (raised now that LOD0 is the detail tier and the far tiers carry the
runtime cost): tiny 5000, small 8000, medium 12000, large 15000, huge 20000, gargantuan
25000. `tri_budget` in the monster's override file replaces it.

LOD0 is also generated with the Pixal3D **1024 upsample cascade by default** (`upsample`
1024; `upsample: 0` in an override or `--upsample 0` gives the plain 512 pipeline). The
mesh stage samples system RAM, swap, ComfyUI's RSS and VRAM every second and records the
peaks and the wall time in `manifest.mesh.resources`; if a 1024 run fails (OOM, ComfyUI
error, timeout) it retries once at 0 and records `manifest.mesh.upsample_fallback`.
An explicit `--upsample` is never second-guessed.

`tools/blender_lods.py` (stage `lod`, run after `qa`):

- imports `model.glb` as it is. glTF splits vertices along UV seams, so every seam is an
  open boundary, which the collapse keeps (boundary quadrics are heavily weighted), and
  each face corner keeps its exact UV. All LODs therefore sample LOD0's atlas correctly.
  Merging the seams first was worse on the harpy: collapses dragged UVs across charts
  (skin-coloured slivers on the wings) and LOD3 stopped at a topology floor (988 tris for
  a 693 target);
- locks the vertices within 1% of the height above the ground (the feet stay planted;
  any vertex the collapse pushes under y=0 is snapped back);
- iterates the Decimate ratio until the result is within 3% of the target;
- exports with QA's glTF settings, so scale, +Y up, -Z forward and the origin at the feet
  are identical to `model.glb` (`lods.json` records the bounds of every tier).

The LOD files contain the geometry, UVs and the material slot (same name and factors),
but **no images** by default: the engine binds LOD0's material, which is what "one
material set per monster" means, and it keeps a 3-LOD chain at about 3-8% extra disk
instead of 3x the texture bytes. The viewer swaps the geometry under the already loaded
LOD0 material. `"embed_textures": true` in the job writes self-contained files.

Pilot results (house-style references, upsample 1024, new budgets; LOD stage 1.2-1.4 s;
the three LOD files add 3-4% to `model.glb`'s size):

| monster | budget | LOD0 | LOD1 | LOD2 | LOD3 | mesh s | peak RAM / ComfyUI RSS / VRAM GB |
|---|---|---|---|---|---|---|---|
| goblin-warrior | 8000 | 7951 | 3976 | 1988 | 794 | 52 | 25.5 / 16.0 / 12.4 |
| wolf | 12000 | 11753 | 5876 | 2938 | 1175 | 89 | 26.0 / 16.0 / 12.6 |
| owlbear | 15000 | 14785 | 7391 | 3695 | 1477 | 138 | 24.7 / 14.9 / 12.6 |
| giant-spider | 15000 | 14953 | 7476 | 3738 | 1495 | 58 | 25.4 / 16.2 / 12.4 |
| harpy | 12000 | 11874 | 5936 | 2968 | 1187 | 90 | 26.0 / 16.2 / 13.0 |
| young-red-dragon | 15000 | 14962 | 7481 | 3740 | 1496 | 73 | 25.7 / 16.2 / 12.6 |

No pilot needed the upsample fallback. Swap was full (8 GB) before the runs started and
stayed full; RAM peaked at 26 of 30.5 GB. The wolf's 1024 mesh has a topology floor of
8548 faces, so 12000 is under 1.6 x the floor and it fell back to the uniform GPU
decimator (`decimator_used`).

Quality: LOD1 is indistinguishable at normal viewing distance. LOD2 loses thin spikes and
feather edges. LOD3 is a silhouette with shards on thin sheets (wings, membranes); use it
only past about 30 m on screen.

### Godot 4

Godot 4 already generates LODs on import for every mesh (`meshes/generate_lods`, on by
default, using meshoptimizer) and switches them by screen-space error, so explicit LODs
are optional there: import `model.glb` alone and Godot will build its own chain. The
explicit chain is still useful for engines without auto-LOD (three.js, Bevy, older Unity
pipelines), for a deterministic, reviewable chain that is the same in every engine, and
for the ground lock above (auto-LOD may lift feet off the ground). To use it in Godot,
turn `generate_lods` off for the LOD files and either put them in a
`VisibilityRange`-based setup (one `MeshInstance3D` per LOD with
`visibility_range_begin/end`) or assign them as `ImporterMesh` LODs from an import script.

## Stance leveling (QA)

Pixal3D reconstructs the model in the reference camera's frame. A reference drawn from
above (most "front view" quadrupeds and spiders) comes out pitched, so the back legs
float: the M1 giant spider stood on its front two leg pairs with the abdomen raised
and the rear pair 0.9-1.3 m in the air.

`tools/blender_qa.py` now levels the stance after the facing yaw and before scaling:

1. **Contact clusters**: local height minima (no lower vertex within r horizontally,
   r = 3.5% of the footprint diagonal) in the lower half of the model, merged within r.
   Each cluster has a contact patch (vertices within r and 3% of the height above its
   lowest point).
2. **Tail rule** (quadruped, winged_quadruped): the rear-most cluster is a tail lying on
   the ground, not a foot, when it sits more than 15% of the length behind every other
   cluster. Without it the dragon's tail curl took one of the four foot slots and the
   front feet stayed 0.12-0.14 m up.
   Single-vertex clusters are never feet. Two-legged plans also drop clusters whose patch
   is under 10% of the largest (wing and feather tips); multi-legged plans keep them all,
   because a spider's leg tips are tiny next to its belly and the area filter threw the
   real feet away.
3. **Plane**: k feet per body plan (biped/winged_biped/avian 2, quadruped/
   winged_quadruped/centauroid 4, insectoid 6; other plans are not leveled by default).
   Every plane through three clusters with a tilt under the cap is scored by the mean gap
   of the k clusters nearest to it (distinct limbs only: two clusters are one limb when
   mesh edges connect them below 12% of the height above the cluster, so toes, claws and
   feathers of one paw count once; the owlbear's paws spread 0.6 m)
   after the model is re-dropped onto it, plus 0.02 x tilt in radians. The best plane is
   then refit by least squares through its k feet while that lowers the score. With fewer
   than 3 feet (bipeds) only the pitch through the two lowest clusters is used, and only
   when they lie front/back of each other.
4. The minimal rotation that makes the plane horizontal is applied (its axis is
   horizontal: pitch and roll, never yaw). Scale and the y=0 drop follow as before.

The tilt cap is 20 degrees, 40 for insectoids: the spider needs 33 degrees, and a
top-down reference is the normal case for spiders. `level: false` and `level_tilt` in the
override file (or `forge.py qa --no-level / --level-tilt`) change it per monster.

`qa.json` gets a `level` block: method, tilt applied, cap, k, the normal before leveling,
every cluster (position in metres in the final frame, patch area, foot / tail / contact
flags, gap before and after), `foot_gaps_m` / `foot_gaps_before_m` for the k contact
feet, and a stability check (`com_m`, `stable`: is the area-weighted surface centroid over
the hull of the vertices within 1% of the height of the ground). `stable` is a report
only; it is noisy for wide wings and fluff. QA also renders `thumb_ground.png`: side and front orthographic views with the
camera exactly at ground level over a dark ground slab, with red markers on the contact
feet, so a floating foot shows as a gap above the line.

Pilot results (gaps of the k contact feet in metres; final house-style meshes at 1024):

| monster | method | tilt | before | after | stable |
|---|---|---|---|---|---|
| giant-spider | plane_fit | 38.6 | 0, 0.039, 0.068, 0.606, 0.897, 1.015 | 0, 0.016, 0.212, 0.212, 0.212, 0.467 | no |
| young-red-dragon | plane_fit | 4.4 | 0, 0.080, 0.181, 0.307 | 0, 0, 0, 0.194 | yes |
| wolf | plane_fit | 4.7 | 0, 0.013, 0.034, 0.058 | 0, 0, 0, 0.008 | yes |
| owlbear | plane_fit | 4.6 | 0, 0.060, 0.072, 0.113 | 0, 0.0004, 0.0008, 0.027 | yes |
| goblin-warrior | two_lowest_pitch | 0 | 0, 0.0001 | unchanged | yes |
| harpy | two_lowest_pitch | 0 | 0.041, 0.043 | unchanged | no |

On the M1 (pre-style) spider the same code applied 33 degrees and took the rear pair from
0.91 / 1.27 m to 0.23-0.31 m with the middle pairs on the ground. The spider's legs are
posed individually, so no rigid rotation grounds all of them: the current one stands
level with three legs down and the rest 0.2-0.5 m up (a crouch rather than the rearing
pose it came out of the generator in). The harpy's feet are 4 cm up because its wing
feathers hang lower than its boots; rotation cannot fix that (re-drop is to the lowest
vertex). Contact gaps of a few millimetres are the clusters' lowest points sitting just
above a claw or toe that is the true lowest vertex.

## Per-monster overrides

`data/overrides/<source>/<id>.json` (git-tracked, any subset of the fields) is merged over
the defaults by `forge.py`, `blender_qa.py` and `blender_lods.py`; command-line flags win
over the file. The schema, validation and defaults are in `tools/overrides.py`; the field
table is in `viewer/README.md`. The viewer edits these files and can queue re-runs.

Current files: `harpy.json` (`upsample: 1024`, now also the default; it pins the harpy to
the cascade because its face softness is the voxel colour resolution). The wolf's old
`yaw` pin was not carried over: the house-style wolf faces correctly on its own (end-area
ratio 1.85) and its manifest pin (116.93) is within 3 degrees of the auto facing.
