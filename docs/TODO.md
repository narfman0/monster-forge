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
- [x] Procedural in-place `walk` for all legged plans (`tools/blender_walk.py`, planted IK feet).
      Loose parts below the hips (owlbear tail feathers, harpy feather tips, dragon tail)
      dip up to 0.2 m through the ground, because the crouch lowers them with the body.
- [ ] Remaining clips: idle, attack, hit, death; `fly` and `breath` for the dragon, `fly` for
      the harpy. A library retarget needs a bone map, since SkinTokens bones are `bone_N`.
- [ ] UniMate text-to-motion for attacks (own uv venv under tools/).
- [ ] Bake to NLA actions -> glTF with separate animations; `anims/<clip>.glb` per viewer/README.md.

### E. Godot import test
- [ ] godot-dnd has no 3D scenes yet (Godot 4.7): add a test scene first.
- [ ] Load each pilot: AnimationPlayer with the 5 clips, LOD switch.

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
