# monster-forge viewer

Local progress dashboard and asset/animation viewer. No build step, no frameworks.

```sh
python3 -I tools/viewer.py                 # http://127.0.0.1:8787/, opens a browser
python3 -I tools/viewer.py --port 9000 --source srd-5.2.1 --no-open
```

`tools/viewer.py` (stdlib only) serves:

| path | what |
|---|---|
| `/` | this directory (`index.html`, `app.js`, `style.css`, `vendor/`) |
| `/out/...` | the repo's `out/` tree (models, images, manifests), `Cache-Control: no-cache` |
| `/api/index.json` | rebuilt from the registry + `out/` on every request |

The page re-fetches the index whenever the tab regains focus, so new forge output shows up
without restarting anything. `#<id>` in the URL selects a monster (`/#owlbear`).

three.js r179 is vendored under `vendor/three/` (MIT, `vendor/three/LICENSE`) with the
same directory layout as the npm package, so addon-relative imports keep working. The
importmap in `index.html` maps `three` and `three/addons/`.

## Stage-status convention

`/api/index.json` derives one boolean per stage for each registry record from
`out/<source>/<id>/`. `forge.py` (and the future rig/anim stages) must follow this so the
viewer reports progress correctly. "Manifest stage entry" means a key in `manifest.json`
whose value is a non-empty object without an `"error"` key.

| stage | true when |
|---|---|
| `ref` | `ref.png` or any `ref_<i>.png` exists |
| `mesh` | `model_raw.glb` (or `model.glb`) exists |
| `qa` | `model.glb` exists **and** manifest has a `qa` stage entry |
| `rig` | manifest has a `rig` stage entry, **or** top-level `"has_skeleton": true` (set this when `model.glb` itself carries the skin) |
| `anim` | at least one clip is found (below) and `manifest.anim` has no `"error"` key |

Found clips are the union of:

1. `manifest.anim.embedded_clips`: names of animations stored inside `model.glb`;
2. `out/<source>/<id>/anims/<clip>.glb`: one file per clip, clip name = file stem;
3. `manifest.anim.clips`: any other clip names the anim stage wants to declare.

Each entry also gets `expected_clips` (the registry `animation_set`), `missing_clips`, and
`anim_complete` (every expected clip found). Suggested manifest shape for the later stages:

```json
"rig":  {"tool": "skintokens", "template": "rigify.wolf", "bones": 42, "seconds": 18.2},
"has_skeleton": true,
"anim": {"embedded_clips": ["idle", "walk"], "clips": [], "seconds": 64.0}
```

`summary` holds per-stage totals (plus `anim_complete`) and per-body-plan counts.

## Animations in the page

- Clips embedded in the loaded GLB play directly through an `AnimationMixer`.
- External `anims/<clip>.glb` files are loaded and their clips bound onto the main
  model by node/bone name: tracks whose target name exists in `model.glb` are kept, the
  rest are dropped (hover a clip to see the match ratio). No retargeting between
  different skeletons is attempted, so export clips on the same skeleton (same bone names)
  as `model.glb`. A file holding several clips shows them as `<file>:<clip>`.
- The expected `animation_set` is shown as chips; missing clips are dashed and grey,
  clips outside the set are outlined in yellow.

The model is picked as `model.glb`, falling back to `model_raw.glb`. The HUD flags a
mismatch when the GLB has a skeleton but the manifest has no rig stage, or the reverse.

## Keys

`j`/`k` or arrows: next/previous monster · space: play/pause · `n`/`N`: next/previous clip ·
`c`: cycle clips · `w`: wireframe · `t`: textures · `f`: frame camera · `/`: search ·
Esc: leave search / close image.
