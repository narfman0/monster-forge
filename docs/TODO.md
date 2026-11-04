# TODO (checkpoint 2026-11-04, rig + walk on all 6 pilots)

Decision: take the 6 pilots (goblin-warrior, wolf, owlbear, giant-spider, harpy,
young-red-dragon) all the way to rigged + animated Godot-ready GLBs **before** the
330-monster SRD batch. Batch is gated on all 6 loading and playing clips in godot-dnd.

## State

- All 6 pilots have paint + LODs (viewer: paint 6, lod 6). Sheets in `out/srd-5.2.1/_paint_cmp/`.
  Paint is 150-190 s per monster, peak 13.9 GB VRAM and ~25 GB system RAM.
- SkinTokens runs: ~20-30 s per monster, ~14 GB VRAM (stop ComfyUI first). `scipy` was an
  undeclared dependency, now installed in its venv. No sm_120 or transformers 5 problems.
- `paint.enabled` now defaults to true: it adds ~3 min per monster, ~14-17 h over 330.
- `forge.py rig` and `forge.py anim` work on all 6 (viewer: rig 6, anim 6). Each monster's
  skeleton is `rig/chosen.glb`: `--force` rebuilds the game files from it, only
  `--rig-reroll` rolls again. Art is git-ignored and `qa --force` deletes `rig/`, so the
  harpy's hand-built wing skeleton would need redoing: re-add the chains, skin with
  `--use_skeleton`, run `tools/blender_wing_weights.py`.
- ComfyUI is a user systemd unit: `systemctl --user start|stop comfyui`. Do not
  nohup it from a shell.
- Viewer: `python3 -I tools/viewer.py` -> http://127.0.0.1:8787/

- **Unarmed references need a candidate check.** The empty-hands prompt held in only 2 of 4
  goblin candidates (one hid a bow, one a dagger in the fist); the auto-pick took an armed
  one. The batch needs an automatic pick (silhouette/VLM check for held objects) or `--n 4`
  plus a review step for the 79 `unarmed_reference` monsters.
- **ComfyUI memory ceiling.** Paint pushes ComfyUI to ~17 GB RSS; with `MemoryHigh=16G` in
  `comfyui.service.d/memory.conf` and swap full it stalls and stops answering `/queue`
  (twice on 2026-11-04). Either raise MemoryHigh to ~20G or restart the unit before paint.

## Rig findings (2026-11-04)

Generation is sampled and unseeded (`do_sample=True`), so every run gives a different
skeleton. Re-rolling is cheap; a stage needs N rolls, a scorer and retry on failure.

| pilot | best rig | result |
|---|---|---|
| goblin-warrior | `rig/model_rigged.glb` (30 joints) | good; pose test clean, weapons follow the hands |
| wolf | `rig/model_rigged.glb` (28 joints) | good skeleton; no real tail chain, pose test not run |
| owlbear | `rig/roll_3.glb` (27 joints) | good; pose test clean. No spine, legs hang off a back root. Bone counts varied 18-46 across rolls |
| giant-spider | `rig/model_rigged.glb` (37 joints) | 8 legs, pose test not run. First run died with `assert res.skin_pred is not None`, the retry passed |
| harpy | `rig/wings/model_fixed.glb` (34 joints) | no wing bones in 4 of 4 rolls; uses hand-added chains and fixed weights (below) |
| young-red-dragon | `rig/model_rigged.glb` (67 joints, roll 1) | best rig: spine, neck, 4 legs, tail, both wings. Clean at +/-25 degree flap, neck, leg and tail bends |

- Manifold cleanup is **not** needed for rigging: the loose parts are slivers of mostly 3-9
  verts on one main body, and they weight fine. Only the dragon has a large one (332 verts).
- `--use_skeleton` re-skins hand-edited skeletons, but its `trim` augment deletes joints
  with no weights: give new bones placeholder weights in the input GLB.
- Harpy wings: SkinTokens spreads the wing vertices over forearm, thigh and spine even with
  a wing chain, so the wings tear. `tools/blender_wing_weights.py` re-weights them
  geometrically (`rig/wings/model_fixed.glb`). Clean up to about +/-20 degrees; a steeper
  downstroke stretches the inner wing and drags a long feather tip.
- **Facing matters to SkinTokens.** The re-rolled dragon went from scrambled to clean once
  its head pointed forward. QA's PCA auto-facing followed the wings and left it 65 degrees
  off, so it now has a pinned `yaw`. The batch needs a facing check before rigging.
  Owlbear's skeleton has no spine and it looks a little off-axis from above: re-check its
  yaw. New prompt in `data/prompt_tweaks.json`; old mesh in `young-red-dragon_exp_curled/`.

## Next (in order)

### B. Pilot hardening
- [x] Write `docs/paint-stage.md` (forge.py/paint.py already reference it). Rig and anim: `docs/rig-anim.md`.
- [x] Forward axis: keep -Z (`forge.py` default). Matches Godot node forward; leave
      `use_model_front` off in `look_at()`. Check UniMate's expectation in step E.
- [ ] Any `qa --force` deletes paint and LODs; `paint` deletes LODs. Use `--stages paint,lod`.

### C. M3 Rig
- [x] `forge.py rig`: N rolls, retry, score, keep the winner, rigged LODs, manifest `rig` key.
- [ ] Add wing chains and wing weights to the stage (harpy is by hand; other winged bipeds will
      hit the same gap). The dragon's came from SkinTokens.
- [ ] Facing check before rigging (see Rig findings).
- [ ] The roll score is symmetry and leg count only: it picked a 22-joint wolf over a 28-joint
      one, and it cannot see a missing spine or tail. Add an automated pose test in `tools/`
      and a weights-heatmap thumb.
- [ ] Owlbear: check yaw, re-roll for a spine. Wolf: no tail chain.
- [ ] Rigify landmark-fit fallback for biped/quadruped if SkinTokens output is bad.

### D. M4 Animate (core set: idle, move, attack, hit, death)
- [x] Procedural in-place `walk` for all legged plans (`tools/blender_clips.py`, planted IK feet).
      Loose parts below the hips no longer dip: the skinned mesh is measured every frame
      and the body raised (or a ground-lying tail turned up) to keep every vertex above
      z = -0.03 m. IK now holds the foot bone itself (position + rest orientation): SkinTokens
      feet can hang 0.7 m off the shin's tail (dragon), which made them bob.
- [x] Remaining clips (2026-11-04): idle, attack_melee, hit, death for all 6; `fly` for harpy
      and dragon (wing chains found by `rig_topology.find_wings`), `breath` for the dragon,
      `attack_ranged` (bow draw) for the goblin. All 6 pilots `anim_complete` in the viewer.
      Shape and per-clip numbers in `viewer/README.md` ("Clips the anim stage writes").
      Known: owlbear tail fan is skinned to its right hind shin, so its walk and lunge run
      at half size (`stride_scale` 0.5); harpy walk also half stride (hip feathers touch
      the ground at rest, so the body cannot crouch); harpy wings are clean only to
      about +/-20 degrees, its arms are mostly hidden by the wings in the sword cut.
      A library retarget would still need a bone map, since SkinTokens bones are `bone_N`.
- [ ] UniMate text-to-motion for attacks (own uv venv under tools/).
- [x] Bake to actions -> one armature-only `anims/<clip>.glb` per clip (viewer/README.md).

### E. Godot import test
- [x] godot-dnd has no 3D scenes yet (Godot 4.7): add a test scene first.
      (2026-11-04) godot-dnd now has `tools/sync_monsters.py`, which copies
      `out/<source>/<id>/` into `assets/monsters/`, plus `scenes/monster_lab.tscn`,
      `tools/check_monsters.gd` (headless import test) and `docs/monsters.md`.
- [ ] Load each pilot: AnimationPlayer with the 5 clips, LOD switch. All six pilots load and
      play walk, and the LOD switch works. Re-run when the other clips land:
      `python3 -I tools/sync_monsters.py --all && godot --headless -s res://tools/check_monsters.gd`
      (in godot-dnd). Findings from Godot 4.7.1:
  - Holds: metres, feet at y=0, +Y up, faces **-Z** (all six; `look_at` works with
    `use_model_front=false`), creature's right = +X, `scale_m` = longest body axis.
    Rigged LODs share bone names, order, bind poses and skin with LOD0. Clip tracks
    bind 100% by bone name. Walk clips are in place.
  - Socket != grip: a `BoneAttachment3D` on `hand_r` puts the prop's grip at the wrist,
    with +Y along wrist -> fingers. On the goblin the sword hangs down along the fingers.
    `equipment.md` needs a grip frame: `grip_r`/`grip_l` bones in prop space (origin at
    the fist centre, +Y across the palm), or a documented hand -> grip offset.
  - The harpy's `hand_*` are added bones at the hand bone's tail, 8-9 cm off the mesh,
    while the goblin's are the renamed hand bones (at the wrist). Harpy rig: the skeleton
    is offset +6.8 cm in x, and bone_18/bone_19 (right forearm/hand) have no vertex with
    weight > 0.5, because the right arm is skinned to the wing chain bone_11/bone_12.
  - Walk dips below the ground: lowest vertex over a cycle is owlbear -0.19 m (bone_25),
    dragon -0.10 (tail bone_64), harpy -0.10 (bone_0), wolf -0.05 (foot bone_26).
  - The rigged LOD files' material is named `Material_0.001`, not `Material_0` as lods.md
    says. Harmless in Godot, which applies LOD0's material.
  - Goblin 16:02 build: `textures/base_color_painted.png` is about 60% black (mean RGB
    39/32/20, `paint.painted_fraction` 0.39). Texels no view painted look unfilled
    instead of falling back to the voxel colour, so the goblin renders almost black. The
    15:47 build looked right.
- [x] Round 2 (2026-11-04): all 36 clips bind 100% of tracks in Godot 4.7.1, LODs follow
      the one skeleton, one-shots settle to idle and death holds; props sit in the fist on
      `grip_r`/`grip_l` through the sword cut. godot-dnd commits 8dc462a, 0a13bc6
      (`tools/sync_monsters.py`, `scripts/monster.gd`, `scenes/monster_lab.tscn`,
      `tools/check_monsters.gd`, `docs/monsters.md`).
- [x] Metallic: the Pixal3D metallic bake is noise (255 on goblin and harpy, 0 on wolf), so
      the goblin read as full metal in Godot. QA and rig exports now clamp metallic to 0.
- [ ] Harpy: `grip_r` is 12-27 cm from its hand because the right arm is skinned mostly to
      the wing chain behind it (SkinTokens, and the skeleton sits 7 cm off centre). It
      wields nothing, so not on the batch path; fix with an arm-aware re-skin or a re-roll.
- [ ] Bows and shields need a per-prop carry pose: at rest a bow on `grip_l` lies on its
      side. Options: a `carry` offset per prop in props.json, or stow it on `back`/`hip`
      outside `attack_ranged`. The lab also keeps the sword out during the shot: loadout
      swap per clip is the game's job.
- [ ] Real export (`assets/monsters/*` in the export filter) is untested.

## Later
- **Public SRD viewer (static GitHub Pages repo, e.g. `monster-forge-viewer`).** Gated on
  the local iteration above: only "released" monsters go out.
  - [ ] Define the release gate (manifest flag, e.g. `released: true`, set once a monster
        is through rig + anim + Godot import).
  - [ ] `tools/publish.py`: walk `out/srd-5.2.1/`, copy released monsters (LOD GLBs,
        preview image, `anims/`), bake `index.json` with the same logic as
        `tools/viewer.py` `/api/index.json`, write to the viewer repo.
  - [ ] Viewer `static` mode in `viewer/app.js`: read `./index.json` and relative model
        paths; hide overrides form, re-run queue and progress dashboard; keep model view,
        wireframe/texture, LOD picker, animation playback and `#<id>` deep links.
  - [ ] Public extras: search/filter by type, CR, size from the registry; CC-BY-4.0 SRD
        attribution footer (text in README).
  - [ ] Measure real per-monster size before choosing hosting. Pages limits: ~1 GB site,
        100 MB per file, no Git LFS. If too big: meshopt + KTX2, only LOD1/LOD2, or host
        models in a bucket (R2) and keep only the viewer on Pages.
  - [ ] Check generation-model terms (Pixal3D, SDXL, ControlNet, IP-Adapter weights) allow
        publishing outputs before the repo goes public.
  - Start with the two painted pilots (harpy, goblin-warrior) as test data.
- `tools/import_names.py`: 5etools `bestiary-xmm.json` (~/workspace/5etools-src, local use
  only, never vendored) -> `data/registry/mm-2024.json` stubs. 503 blocks, 330 flagged
  `srd52`, 173 MM-only. Also cross-check SRD registry size/type/speed against it.
- M2 batch SRD once the pilots are through E.
