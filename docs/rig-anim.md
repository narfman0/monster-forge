# Rig and anim stages (2026-11-04)

`forge.py rig` gives LOD0 a skeleton and skin weights and writes the game files.
`forge.py anim` writes clips on that skeleton. Both run after `lod`. The tool research is in
`research-rig-animate.md`, and the per-pilot results are in `TODO.md` ("Rig findings").

```sh
python3 -I tools/forge.py rig wolf                 # skip if the rigged files exist
python3 -I tools/forge.py rig wolf --force         # rebuild the game files from rig/chosen.glb
python3 -I tools/forge.py rig wolf --rig-reroll    # new SkinTokens rolls (--rig-rolls N, default 4)
python3 -I tools/forge.py anim wolf --force        # rebuild anims/walk.glb
```

## Rig stage

Code: `tools/rig.py` (driver), `tools/blender_rig.py` (scoring and export, headless
Blender), `tools/rig_topology.py` (finds legs, arms, head and the centre line from bone
positions).

**SkinTokens rolls.** SkinTokens (`tools/skintokens/`, its own uv venv) predicts a
skeleton and skin weights from `model.glb`. It samples without a seed, so every run gives a
different skeleton, and some runs crash (`assert res.skin_pred is not None`). The stage
runs N rolls (default 4, 240 s timeout each) into `rig/roll_<i>.glb`, after waiting for an
idle ComfyUI queue and asking ComfyUI to unload its models: SkinTokens needs about 14 GB of
VRAM. A roll takes 20-30 s. Failed rolls are logged and skipped; the stage fails only when
every roll fails.

**Scoring.** Each roll is scored in Blender (`rig/roll_<i>.json`):

    score = -5 * symmetry - 0.5 * missing legs - 0.02 * empty bones - 0.02 * zero-length bones

`symmetry` is how far the skeleton is from its own mirror image across the centre line,
relative to the model height. "Missing legs" counts against the plan's minimum foot count
(`FEET_BY_PLAN` in `tools/overrides.py`; insectoids are scored with sprawled legs). Empty
bones own no vertex, and zero-length bones are shorter than 2% of the height. A roll that
leaves any vertex unweighted scores -10. The best roll is copied to `rig/chosen.glb`.

**`rig/chosen.glb` is the source of truth.** Once it exists, `rig --force` (and any
`run --force`) only rebuilds the game files from it, for example after a new paint or new
LODs. Only `--rig-reroll` rolls again and replaces it. A skeleton fixed by hand (the
harpy's wing chains) survives re-runs by being saved as `rig/chosen.glb`. `qa --force` and
`mesh --force` change the geometry and delete `rig/` and `anims/`; `paint` and `lod` delete
only the game files.

**Game files** (mode `finalize`):

- `model_rigged.glb`: the chosen rig with the current albedo (`base_color_painted.png`
  when it exists), sockets added. The viewer loads this file in preference to `model.glb`.
- `model_rigged_lod<k>.glb`: each `model_lod<k>.glb` with weights transferred from LOD0
  (nearest surface point, at most 4 influences, normalised) under the same armature. The
  joint order matches LOD0, so an engine swaps LOD geometry under one skeleton. Like the
  static LODs they carry no images.
- `rig.json` and `manifest.rig`: joints, legs, symmetry, empty bones, `sockets`
  (`{socket: original bone}`), `sockets_missing`, `picked` and the roll scores, seconds.

`model.glb` stays the static source for `paint` and `lod`.

**Sockets.** SkinTokens names every bone `bone_<n>`, so the stage renames or adds the
socket bones the registry's `sockets` field asks for (`docs/equipment.md`):

- `hand_r`, `hand_l` (records with `hands: true`): the hand bone found at the end of each
  arm is renamed. When the arm ends at the forearm, a small non-deforming hand bone is
  added at the forearm's tail.
- `head`: the head bone is renamed.
- `back`: a new non-deforming bone on the spine bone the arms hang from, behind the
  shoulders.
- `hip_l`, `hip_r`: new non-deforming bones on the pelvis at hip height.

Sockets the stage could not place are logged and listed in `manifest.rig.sockets_missing`.
The viewer's `sockets` toggle draws each socket bone with its axes, and `prop` hangs a
placeholder sword on `hand_r` and a bow on `hand_l` (`viewer/README.md`).

### Known limits

- The score sees only symmetry, leg count and dead bones. It picked a 22-joint wolf over a
  28-joint one, and it cannot see a missing spine or tail. An automated pose test and a
  weights heatmap are still to do.
- **Facing matters.** A mesh that does not face -Z gets a scrambled skeleton. The young red
  dragon went from scrambled to clean once its `yaw` was pinned. Check facing before
  blaming the rig.
- No wing bones on the harpy in 4 of 4 rolls. Its wings are hand-added chains, re-skinned
  with `--use_skeleton` and re-weighted with `tools/blender_wing_weights.py`; clean up to
  about +/-20 degrees. The stage does not do this yet, so other winged bipeds will hit the
  same gap. `--use_skeleton`'s `trim` step deletes joints with no weights, so new bones
  need placeholder weights in the input.
- The owlbear has no spine (its legs hang off a back root); the wolf has no real tail
  chain.
- Added hand bones point straight up in the bind pose, not along the forearm, so a prop
  attached with no offset stands upright from the wrist (the harpy's `hand_r` and `hand_l`
  in the viewer). Renamed hand bones keep SkinTokens' orientation. The prop-space
  convention in `docs/equipment.md` assumes +Y along the closed fist; a grip pose and a
  hand-aligned socket are still to do.
- Loose mesh parts weight fine; manifold cleanup is not needed before rigging.

## Anim stage

Code: `tools/anim.py` (driver), `tools/blender_clips.py` (the gait, headless Blender).

What exists now is one procedural clip: an in-place `walk` for every plan with legs
(`FEET_BY_PLAN`) whose `animation_set` has `walk`. It needs `model_rigged.glb`. The legs
come from the skeleton (`rig_topology.find_legs`). Each foot runs a planted cycle on an IK
target: it slides back along the ground for the stance (62% of the cycle) and lifts forward
for the swing, held level, so feet neither skate nor tilt. Bipeds alternate, quadrupeds
use a four-beat walk, more legs ripple along each side. The hips sit low enough to keep
the knees bent and bob with the steps; biped arms swing opposite their leg and a tail
sways. The clip stays in place: moving the character is up to the game.

Output: `anims/walk.glb` (armature only, 30 fps),
`anims/walk.json` (legs, feet bones, phases, arms, tail, cycle seconds), and
`manifest.anim` (`clips`, `walk`, seconds). The cycle is 1.1-1.5 s on the pilots, and the
stage takes about 1 s.

Known limit: loose parts below the hips (owlbear tail feathers, harpy feather tips, the
dragon's tail) dip up to 0.2 m through the ground, because the crouch lowers them with
the body.

More clips (idle, attack, hit, death; fly and breath where the set has them) are being
added. Each is one `anims/<clip>.glb` on the same skeleton, with bone names matching
`model_rigged.glb`; the file stem is the clip name. `viewer/README.md` ("Stage-status
convention" and "Animations in the page") has the convention the viewer and the index
follow.
