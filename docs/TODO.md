# TODO (checkpoint 2026-11-04)

Decision: take the 6 pilots (goblin-warrior, wolf, owlbear, giant-spider, harpy,
young-red-dragon) all the way to rigged + animated Godot-ready GLBs **before** the
330-monster SRD batch. Batch is gated on all 6 loading and playing clips in godot-dnd.

## State

- Paint stage committed (`377992d`). harpy and goblin-warrior painted; wolf, owlbear,
  giant-spider, young-red-dragon not yet (the run was killed with the session).
- SkinTokens: cloned to `tools/skintokens/` (gitignored), venv complete
  (py3.11, torch 2.7.0+cu128, flash-attn 2.8.3 prebuilt wheel, transformers 5.22,
  bpy 5.0.1), checkpoints in `tools/skintokens/experiments/`, Qwen3 config in
  `tools/skintokens/models/`. **Never run.**
- ComfyUI is a user systemd unit: `systemctl --user start|stop comfyui`. Do not
  nohup it from a shell.
- Viewer: `python3 -I tools/viewer.py` -> http://127.0.0.1:8787/

## Next (in order)

### A. Pilot hardening
- [ ] `python3 -I tools/forge.py paint wolf owlbear giant-spider young-red-dragon`
- [ ] Review `_paint_cmp` sheets; decide whether `paint.enabled` becomes the default.
- [ ] Write `docs/paint-stage.md` (forge.py/paint.py already reference it).
- [ ] Manifold cleanup before rigging (`remove_doubles` + fill holes) — pilot-report item 6.
- [ ] Lock forward axis (-Z forward, +Y up for SkinTokens/UniMate/Godot); check `qa --forward` default.

### B. M3 Rig
- [ ] First SkinTokens run (needs ~14 GB VRAM: `systemctl --user stop comfyui` first):
      `cd tools/skintokens && .venv/bin/python demo.py --input ../../out/srd-5.2.1/goblin-warrior/model.glb --output ../../out/srd-5.2.1/goblin-warrior/rig/model_rigged.glb --use_transfer`
      Risks: sm_120 flash-attn kernels, transformers 5 vs the >=4.57 the code targets.
- [ ] `forge.py rig` stage: run SkinTokens, write `rig.json` (joint count, symmetry,
      leg count for the spider), weights-heatmap thumb; manifest `rig` key per viewer/README.md.
- [ ] Transfer skin weights to LOD1-3 in Blender so the LOD chain shares one skeleton.
- [ ] Rigify landmark-fit fallback for biped/quadruped if SkinTokens output is bad.

### C. M4 Animate (core set: idle, move, attack, hit, death)
- [ ] goblin: Mixamo/Quaternius CC0 retarget (cheapest, do first).
- [ ] wolf, owlbear: UniMate text-to-motion (own uv venv under tools/).
- [ ] harpy, dragon: UniMate flap + hover idle; dragon ground walk.
- [ ] giant-spider: procedural IK gait in Blender, hand-keyed attack.
- [ ] Bake to NLA actions -> glTF with separate animations; `anims/<clip>.glb` per viewer/README.md.

### D. Godot import test
- [ ] Load each pilot in godot-dnd: AnimationPlayer with the 5 clips, LOD switch.

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
- M2 batch SRD once the pilots are through D.
