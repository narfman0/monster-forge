# monster-forge viewer

Local progress dashboard and asset/animation viewer. No build step, no frameworks.

```sh
python3 -I tools/viewer.py                 # http://127.0.0.1:8787/, opens a browser
python3 -I tools/viewer.py --port 9000 --source srd-5.2.1 --no-open
python3 -I tools/viewer.py --port 8790 --no-open --out-root out/_viewtest   # test tree
```

`tools/viewer.py` (stdlib only) serves:

| path | what |
|---|---|
| `/` | this directory (`index.html`, `app.js`, `style.css`, `vendor/`) |
| `/out/...` | the repo's `out/` tree (models, images, manifests), `Cache-Control: no-cache` |
| `/api/index.json` | rebuilt from the registry + `out/` on every request |
| `GET /api/override/<id>` | effective overrides: per field `default`, `override`, `value`, `stage`, `help` |
| `POST /api/override/<id>` | JSON object of fields (`null` = unset); validated, written to `data/overrides/<source>/<id>.json` (removed when empty) |
| `POST /api/run/<id>` | `{"stages": ["qa", "lod"], "force": true}`: queue `forge.py run <id> --stages ... --force` |
| `GET /api/jobs[?id=<id>&tail=N]` | queue and recent jobs: status (`queued`, `running`, `done`, `failed`, `cancelled`), rc, times, log tail |
| `POST /api/jobs/<n>/cancel` | drop a queued job or terminate the running forge process |

The server binds 127.0.0.1 only and answers loopback clients only. POSTs must carry
`Content-Type: application/json`, a `Host` of `127.0.0.1:<port>` or `localhost:<port>` and
no foreign `Origin` (so other web pages cannot drive the job queue), and every `<id>` must
be a registry id of the served source. `--out-root` serves another out tree for testing
(re-run jobs still write to `out/`).

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
| `lod` | `lods.json` exists and every file it lists (`lods.<k>.file`) exists |
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

## LODs

`model.glb` is LOD0; `model_lod<k>.glb` (listed in `lods.json`, see `docs/lods.md`) share
its UVs and material and carry no images. The toolbar shows `LOD0`-`LOD3` buttons with
triangle counts; `l` cycles. The LOD geometry is swapped in under the loaded LOD0
material, and the chosen LOD sticks when moving to the next monster.

## Overrides and re-runs

The right panel's **overrides** section is a form over
`data/overrides/<source>/<id>.json`: each field shows the override (yellow label) or, when
empty, the default as a placeholder. `save` writes the file, `×` resets one field to its
default and saves, `revert` drops unsaved edits. After a save the re-run checkboxes tick
the earliest stage the changed fields affect and everything after it.

`queue re-run` runs `forge.py run <id> --stages <ticked> --force` as a background
subprocess of the viewer: one job at a time, FIFO, the log streamed into the panel and
written to `out/<source>/<id>/forge_job.log`. The monster list shows a spinner for the
running job and a ring for queued ones (`✎` marks monsters with an override file). When a
job finishes the index is refreshed and the selected monster reloads. Jobs live in the
viewer process: restarting the viewer forgets the queue (a running forge process keeps
going). `forge.py` waits for an idle ComfyUI queue before submitting, so jobs coexist
with other ComfyUI clients.

| field | type | stage | meaning |
|---|---|---|---|
| `tri_budget` | int 300-200000 | mesh | LOD0 triangle budget (default by size: 5000/8000/12000/15000/20000/25000) |
| `vg_factor` | float 0-2 | mesh | weighted decimation vertex_group_factor (0.5) |
| `upsample` | 0 or 1024 | mesh | Pixal3D cascade, default 1024 (sharper voxel colour, faces); 0 = 512 only |
| `texture_size` | 512/1024/2048/4096 | mesh | bake and UV atlas size |
| `normal_cage` | float 0.001-0.2 | mesh | normal bake band, fraction of the bbox diagonal (0.015) |
| `ref_pick` | int 0-15 | mesh | which `ref_<i>.png` feeds the mesh stage (default: the manifest's pick) |
| `seed_mesh` | int | mesh | Pixal3D seed |
| `prompt_extra` | string | ref | appended to the reference prompt |
| `negative_extra` | string | ref | appended to the negative prompt |
| `style` | string | ref | art style name or `data/*.json` path, `none` for unstyled |
| `seed_ref` | int | ref | reference seed |
| `yaw` | float deg | qa | rotation about up, replaces auto facing |
| `forward` | `-Z` / `+Z` | qa | glTF facing |
| `level` | bool | qa | stance leveling (default on for legged plans) |
| `level_tilt` | float 0-60 | qa | largest tilt leveling may apply (20; insectoid 40) |
| `lod_ratios` | decreasing floats in (0,1) | lod | LOD1.. ratios of LOD0 (0.5, 0.25, 0.1) |
| `notes` | string | - | why the override exists |

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
`c`: cycle clips · `w`: wireframe · `t`: textures · `l`: next LOD · `f`: frame camera · `/`: search ·
Esc: leave search / close image.
