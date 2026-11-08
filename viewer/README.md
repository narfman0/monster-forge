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
| `POST /api/run/<id>` | `{"stages": ["qa", "paint", "lod"], "force": true}`: queue `forge.py run <id> --stages ... --force` (stages: ref, mesh, qa, paint, lod, rig, anim) |
| `GET /api/jobs[?id=<id>&tail=N]` | queue and recent jobs: status (`queued`, `running`, `done`, `failed`, `cancelled`), rc, times, log tail |
| `POST /api/jobs/<n>/cancel` | drop a queued job or terminate the running forge process |
| `GET /api/download` | every rigged monster: `{monsters: {<id>: {files, bytes, zip}}, zip_all}` |
| `GET /api/download/<id>` | one monster's game files: `{id, zip, files: [{name, url, bytes}]}` (urls under `/out/`) |
| `GET /api/download/<id>.zip` | those files as one zip, folder `<id>/`: `model_rigged.glb`, `model_rigged_lod*.glb`, `anims/*.glb` + `*.json`, `manifest.json`, `lods.json` (what godot-dnd's `sync_monsters.py` reads) |
| `GET /api/download/all.zip` | every rigged monster in one zip (streamed, several GB) |

The download routes take `?lods=0` to leave out the LOD files. They are GETs, so a LAN
client of `--host 0.0.0.0` can use them too (`curl -OJ http://<host>:8787/api/download/owlbear.zip`).
The details panel links the selected monster's zip.

The server binds 127.0.0.1 only and answers loopback clients only. POSTs must carry
`Content-Type: application/json`, a `Host` of `127.0.0.1:<port>` or `localhost:<port>` and
no foreign `Origin` (so other web pages cannot drive the job queue), and every `<id>` must
be a registry id of the served source. `--out-root` serves another out tree for testing
(re-run jobs still write to `out/`).

On a phone or a window narrower than 820 px (or a phone on its side) the layout stacks: the 3D view
on top, then a tab strip switching between the monster list and the clips/overrides/details panel (the
stage bars and key help are hidden; the toolbar scrolls sideways).

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
| `paint` | `textures/base_color_painted.png` exists **and** manifest has a `paint` stage entry |
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

`summary` holds per-stage totals (plus `anim_complete`) and per-body-plan counts. Stage
order: ref, mesh, qa, paint, lod, rig, anim.

Other per-entry fields the page uses: `lods` (from `lods.json`), `hi_faces` (triangles of
the RAW tier), and in `files`: `hi` (`model_hi_tex.glb` or `model_hi.glb`), `hi_textured`,
`hi_bytes`, `base_color_painted`, `base_color_voxel` (paths under `textures/` or null).
`hi_faces` is read from the GLB's JSON chunk only (accessor counts, no buffers) and cached
per file by mtime and size, so the index stays cheap; if the header cannot be read it falls
back to `manifest.mesh.weighted_decimate.faces_in`.

Socket fields: `sockets` (the registry's list of socket bones the rig must provide),
`rig_sockets` (`manifest.rig.sockets`, `{socket: original bone}`) and `sockets_missing`
(registry sockets not in `rig_sockets`). The two rig fields are `null` until the `rig`
stage is true, then an object and a list (empty when nothing is placed or missing).

Diagnostics: `rig_quality` (below, with `grounded` = `manifest.rig.grounded`: `shift_m`,
`turned_deg`, `feet_low_m`, `min_z_m`, or `null` when the feet already stood on the ground)
and `ref_unarmed`, the ref stage's unarmed check (`docs/references.md`): `scores` (one per
`ref_<i>.png`, from `manifest.ref.unarmed_scores`), `armed` (indices over `threshold`),
`best`, `in_use`, `warning` / `warning_pick` (`ref.unarmed.warning`, `.warning_pick`) and
`picked_by`; `null` when the check never ran. Both are optional: the page shows nothing for
a missing or `null` value.

## LODs, RAW tier and auto LOD

`model.glb` (or `model_rigged.glb`) is LOD0; `model_lod<k>.glb` (listed in `lods.json`, see `docs/lods.md`) share
its UVs and material and carry no images. The LOD geometry is swapped in under the loaded
LOD0 material, as an engine would. All LODs of the selected monster are preloaded right
after LOD0 (they are small), so switching is instant.

**RAW** is the undecimated remeshed high-poly the LOD0 decimation starts from
(`model_hi.glb`, ~0.3-1.3 M triangles, 12+ MB, no UVs or colour). It is loaded only when
RAW is picked, shown as clay (neutral grey PBR under the same lights, normals computed in
the page) and is never chosen by auto mode. It comes out of the mesh stage in the
generator's raw frame, so the page re-applies QA's transform from `manifest.qa`
(`yaw_deg`, `level.normal_before` when `level.enabled`, `scale_factor`, then centre on x/z
and drop to y=0); the HUD shows `aligned: qa`. A file already in the final frame (height,
floor and width within a few % of LOD0) is used as is.

Filename convention: a **textured** high-poly, if a later stage ever writes one, goes in
`out/<source>/<id>/model_hi_tex.glb`, with its own material. The viewer prefers it over
`model_hi.glb` (index `files.hi`, `files.hi_textured: true`) and renders it with its own
material instead of clay. Either frame works (raw or final, see above); exporting it in
the final frame (QA's frame, like `model.glb`) is preferred.

The toolbar's mode selector is `auto | RAW | LOD0 | LOD1 | LOD2 | LOD3` (triangle counts in
small type) and a `⚙` settings button. The selected mode is filled; in auto mode the
level currently drawn is outlined. Clicking a level pins it (manual mode), `auto` (key `a`)
returns to auto, `l` pins the next of LOD0-LOD3. The mode is kept in localStorage
(`mf.lodMode`) and so sticks across monsters and reloads.

**Auto LOD by screen coverage.** At 10 Hz the page takes LOD0's bounding sphere (radius r,
centre following the turntable), its distance d from the camera, and computes the
projected diameter as a fraction of the viewport height:

    coverage = tan(asin(r / d)) / tan(fov / 2)        (>= 1000% shown as "inside")

and picks LOD0 above 60%, LOD1 above 30%, LOD2 above 12%, else LOD3 (defaults). A ±10%
hysteresis band stops flicker: it only moves to a finer level once coverage is 10% above
that level's switch point, and to a coarser one once it is 10% below the current level's
point. The toolbar readout shows the level drawn and the coverage (hover: pixels, switch
points); in manual mode it also shows what auto would pick (`LOD1 (manual) · cov 12% →
auto LOD3`) so the switch points can be tuned against the view. The bounding sphere is a
conservative size: for the 1.7 m harpy (r = 1.33 m, fov 40°) auto drops to LOD3 at about 34 m.

The `⚙` popover edits the switch points (percent) and the hysteresis; they are stored in
localStorage (`mf.lodRatios`, `mf.lodHyst`). A monster's `lod_screen_ratios` override
(three decreasing fractions, e.g. `[0.5, 0.25, 0.08]`) takes precedence for that monster;
the readout then shows `✎` and the popover names it.

**Wire on shaded** (`W`) draws a line overlay on the visible level, RAW included. It
shares the mesh's geometry, and three.js builds the wireframe index only on the first
draw, so nothing is built until it is switched on. On a skinned mesh the overlay is a
second `SkinnedMesh` bound to the same skeleton and bind matrix, so the lines deform with
the clip. `wireframe` (`w`) is the plain
wireframe material.

**Albedo toggle (paint stage).** When both `textures/base_color_painted.png` and
`textures/base_color_voxel.png` exist, an `albedo: painted | voxel` switch appears and
swaps the base colour map on LOD0's material (so LOD1-3 follow) at runtime, with the
sampler settings of the embedded map. The choice is kept (`mf.albedo`, default painted).
Without those files nothing changes.

The HUD's last line is the rig stage's verdict from the index `rig_quality` (manifest
`rig.quality` and `rig.facing`): the kept roll's score, its problem list (missing spine,
tail, wings, legs...), how far the mesh faces off -Z and, when the rig stage lowered the
game files onto the feet (`rig.grounded`), `grounded <shift_m> m` (giant eagle:
`grounded 0.355 m`). `rig_facing_check` in the overrides form (default true) lets the
stage roll anyway when the check is wrong.

**Reference candidates.** The images panel shows every `ref_<i>.png` (the one the mesh was
made from ticked). For `unarmed_reference` monsters each caption carries the unarmed
check's hand score, and candidates over the threshold are marked `armed` (red caption and
border); a note under the strip gives the threshold, `picked_by` and any warning (every
candidate armed, or the kept pick armed). Goblin warrior: `ref_0 · 3.35 armed`,
`ref_1 · 3.33 armed`, `ref_2 ✓ · -1.54`, `ref_3 · -0.82`.

## Overrides and re-runs

The right panel's **overrides** section is a form over
`data/overrides/<source>/<id>.json`: each field shows the override (yellow label) or, when
empty, the default as a placeholder. `save` writes the file, `×` resets one field to its
default and saves, `revert` drops unsaved edits. After a save the re-run checkboxes (ref,
mesh, qa, paint, lod, rig, anim) tick the earliest stage the changed fields affect and
everything after it. Fields with a client-side check (`idle_lift`, `rig_reweight`) get a red
border and the error as a tooltip while the text is invalid, and `save` refuses with the
error in the status line (the server validates again with `tools/overrides.py`).

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
| `ref_unarmed_check` | bool | ref | score the candidates for a held weapon and pick the first empty-handed one unless `ref_pick` is pinned (default true for `unarmed_reference` records; `docs/references.md`) |
| `lod_ratios` | decreasing floats in (0,1) | lod | LOD1.. ratios of LOD0 (0.5, 0.25, 0.1) |
| `rig_reweight` | JSON list of 1-8 chains | rig | geometric re-weight before sockets: `[{"bones": [root..tip], "reach", "bias", "blend", "strip", "fit", "hand_room", "far"}]` (textarea, checked against the ranges in `tools/overrides.py`; `docs/rig-anim.md`) |
| `rig_facing_check` | bool | rig | refuse to roll when `model.glb` looks misfaced (default true) |
| `idle_lift` | number 0-20, `match_move` or `false` | anim | idle height: metres of lift, where the swim/fly clip holds the body, or the floor (text box offering the two keywords; unset = `match_move` when the `animation_set` has no `walk` but swims or flies, else `false`) |
| `anim_source` | `procedural` / `ual` | anim | bipeds: `ual` also retargets the Quaternius UAL clips into `anims_ual/` with a per-clip quality gate; `forge.py promote <id>` copies the passing ones into `anims/` (`docs/anim-reuse.md`). Default `procedural` |
| `anim_ual_exclude` | JSON list of clip names | anim | with `anim_source: ual`: clips that stay procedural whatever the gate says (a visual call) |
| `paint` | object (JSON in the form) | paint | multiview paint settings (`enabled`, `views`, `denoise`, ...; see `tools/overrides.py`) |
| `lod_screen_ratios` | 3 decreasing floats in (0, 4] | - (viewer only) | auto LOD switch points, fraction of viewport height for LOD0/LOD1/LOD2; unset = viewer settings |
| `notes` | string | - | why the override exists |

## Animations in the page

The toolbar's animation selector works like the LOD one: `rest` (key `0`, the bind pose),
then the registry `animation_set` in order, then any other clip (outlined). Missing clips
are greyed out, so the bar shows what a monster still needs. Keys `1`-`9` pick the nth
enabled clip button. A monster opens on `walk` if it has one, else `move`, `fly`, `idle`,
then the first usable clip.

- Clips embedded in the loaded GLB play directly through an `AnimationMixer`.
- External `anims/<clip>.glb` files are loaded and their clips bound onto the main
  model by node/bone name: tracks whose target name exists in `model.glb` are kept, the
  rest are dropped (hover a clip to see the match ratio). No retargeting between
  different skeletons is attempted, so export clips on the same skeleton (same bone names)
  as `model.glb`. A file holding several clips shows them as `<file>:<clip>`.
- The expected `animation_set` is shown as chips; missing clips are dashed and grey,
  clips outside the set are outlined in yellow.

### Clips the anim stage writes

`forge.py anim <id>` (`tools/anim.py` -> `tools/blender_clips.py`, one headless Blender run)
builds every clip of the registry `animation_set` it can make procedurally from the
skeleton and writes each as its own armature-only `anims/<clip>.glb` (one Action named
after the clip, 30 fps, starting at t=0) plus `anims/<clip>.json` (its report) and
`anims/clips.json` (the whole run, with the parts it found):

| clip | plans | loop | length |
|---|---|---|---|
| `idle` | all legged | yes | 2.5 s |
| `walk` | all legged; arms swing with the legs, less with a prop in the hand (x0.5 for a sword or club, x0.2 with the grip held on its idle line for a polearm, javelin, bow or two-hander: report `arm_swing`, `grip_cycle_deg`); a hanging tail sways clear of the hind legs (`tail_leg_min_m`) | yes | 0.7-2 s (from the leg length) |
| `attack_melee` | biped: sword cut from `hand_r`; quadruped: lunge bite; insectoid: front-leg stab + bite | once | 0.9-1.1 s |
| `attack_ranged` | two-armed: bow draw (bow at `hand_l`); merfolk_naga: overhand throw from the relaxed `hand_r` arm (1.0 s) | once | 1.6 s |
| `hit` | all legged | once | 0.5 s |
| `death` | all legged; ends on the lying pose (hold the last frame); a hanging tail goes limp toward the ground as the body rolls (`tail_drape`, `tail_z_last`) | once | 1.8 s |
| `fly` | when wing chains are found: wing beat, hover (the lowest vertex `HOVER` above the ground: 0.32 m on a pilot-sized body); without wings (giants, efreeti, nightmare): a levitation, the same hover, legs hanging, a slow bob | yes | 1.2-1.6 s; levitation 2 s |
| `breath` | any skeleton with a head chain: four legs rear with a nose-up pitch (the dragon's), upright bodies lean the torso back; thrust, hold ~1 s | once | 2.2 s |
| `cast` | arms: the casting hand (`hand_r`) rises forward over the head, holds, throws forward, the other hand pushes with it; no arms: head and neck rear, hold, snap forward, the body rises a little, wings lift. Starts and ends on the rest (relaxed-arm) pose; a lifted monster's is held at its idle height | once | 1.2 s |
| `swim` | legged plans: the body held 0.5 x the leg's drop up (`lift`), pitched (nose up 6 degrees; upright bodies lean 35 degrees forward), legs paddle in diagonal pairs (a flutter kick upright, a fore-aft sweep when sprawled), arms dog-paddle, wings folded back, tail sways | yes | 1.8 s |
| `burrow` | legged plans: crouch, the front legs (a spider's front pair, a biped's arms) dig three strokes into the ground, head down, the body sinks by half its height (wings left out). Ends sunk (hold the last frame); the only clip whose vertices go below the ground, `min_z` is reported as it is | once | 1.5 s |

Legless plans (serpentine, floating, amorphous, plant_object, tentacled, aquatic,
merfolk_naga, swarm; `anim.LEGLESS`) get the same clip names from the "legless" builders
in `blender_clips.py`, driven by the body chain, tentacles or arms instead of feet: `walk`
and `swim` are a slither, ooze, crawl wave, tail kick or scurry, `attack_melee` a strike,
engulf, whip, lash or surge, `death` a slump, puddle, topple or belly-up roll. Floating
also has `cast` (the arm or head gesture above) and `swim` (a forward lean with a
breaststroke and hem kick); serpentine has `cast` and `breath` (the front quarter rears and
thrusts), `burrow` (the head dives, the body follows down by half its height) and `fly` (the
slither held `HOVER` up). Still without a builder: `fly` for plant_object (the flying sword),
`attack_ranged` without two arms, and every clip of the radial xorn. Floating,
aquatic and swim clips hover at one constant height for every clip of the monster (`lift`),
except the serpentine swim, which slithers on the ground like its walk; the other legless
clips keep the lowest vertex on the ground (`contact`). Blobs also key
bone scale (squash and stretch). The plan table is in `docs/rig-anim.md`, "Body plans".

Promoted UAL clips (`forge.py promote <id>`, `docs/anim-reuse.md`) replace files in `anims/` under
the same names (plus `run`, which the procedural stage does not make: shown outlined as outside
the set); the procedural files they replace sit in `anims_proc/`, and `manifest.anim.source` says
which clips are `ual`. The page needs no change for them.

Looping clips have identical first and last frames. On legged plans the idle's first frame
is the rest pose, the one-shots start (and, but for death, end) on it, and the walk starts at
the frame of its cycle nearest it; each clip's report has `from_idle_m` (its first frame's
largest vertex move from the idle's first frame) and `from_idle_max_m` (the largest from any
frame of the idle cycle, every 3rd: what an instant switch mid-idle can jump; on the idle,
how far it wanders from its own first frame). Manifest shape:

```json
"anim": {"clips": ["idle", "walk", "attack_melee", "hit", "death"], "missing": [], "seconds": 4.7,
         "walk": {"seconds": 1.13, "frames": 34, "loop": true, "min_z": -0.0013, "loop_delta": 0.0,
                  "ik_reach_gap": 0.0, "stride_scale": 1.0, "...": "..."},
         "idle": {"...": "..."},
         "parts": {"root": "bone_0", "head": "bone_4", "neck": [], "spine": [], "tail": [], "arms": {},
                   "wings": {}, "legs": [["bone_7", "..."]]}}
```

`clips` lists the files written, `missing` the `animation_set` entries the stage cannot
make for this plan; `clips.json` also has `size` (max of height and length) and
`size_scale` (the threshold scale, 1 for pilot-sized bodies); per clip: `min_z` (lowest
skinned vertex over the clip, metres; below 0 is in the ground, by design only in `burrow`), `min_z_last` (death: 0 = lying on the ground), `loop_delta` (largest
vertex distance between the first and last frame), `ik_reach_gap` (how far a standing
foot falls short of its target), `stride_scale`/`body_scale` (< 1 when the walk or lunge
was toned down to keep a part out of the ground). `parts` are the bones each clip drives.

The model is picked as `model_rigged.glb`, then `model.glb`, then `model_raw.glb`. With a
rig the LOD buttons load `model_rigged_lod<k>.glb`: they share LOD0's joint order, so the
geometry swaps under one skeleton while a clip plays. The HUD flags a mismatch when the
GLB has a skeleton but the manifest has no rig stage, or the reverse.

## Skeleton, sockets and props

- **skeleton** draws three.js's `SkeletonHelper` for the loaded rigged model: one line
  from each bone to its parent (orange at the child, green at the parent), drawn through
  the mesh and following the clip. Hidden in RAW.
- **sockets** (`s`) marks every socket bone, which is any bone not named `bone_<n>`
  (SkinTokens' names; the rig stage renames or adds `hand_r`, `hand_l`, `head`, `back`,
  `hip_l`, `hip_r`, see `docs/equipment.md`). Each marker is a label and three arrows in
  the bone's frame, in prop-space terms: green +Y (along the blade toward the tip), blue -Z
  (the cutting edge, or the way an arrow points), short red +X. The markers copy the bone's
  world position and rotation every frame, so they follow the clip. Sizes scale with the
  model height.
- **prop** (`p`) attaches placeholders as children of the socket bones, as Godot's
  `BoneAttachment3D` would: a 0.9 m sword (grip at the origin, red strip on the -Z edge)
  on `hand_r` and a 1.1 m shortbow (stave bowed toward -Z, string behind) on `hand_l`, when
  those bones exist. They are built for a 1.8 m wielder and scaled by `scale_m / 1.8`, or by
  `rider_height_m / 1.8` when the index entry has it (centauroids and merfolk/naga: the
  override's height of the humanoid rider, e.g. centaur-trooper 1.8; `docs/equipment.md`
  "Prop space"), with no extra offset, so they show where a prop lands from the bone alone. Each goes on the
  grip socket (`grip_r` / `grip_l`, the fist centre in prop space) when the rig has one,
  else on `hand_r` / `hand_l` (the wrist).
- **real props**: `?prop_r=<id>&prop_l=<id>` in the URL (`?prop=<id>` is `prop_r`) makes
  the prop toggle load `out/props/<id>/model.glb` from `tools/props.py` instead of the
  placeholder for that hand, e.g. `/?prop_r=longsword&prop_l=longbow#goblin-warrior`. The
  file is loaded once and cloned per attachment, same sockets and scaling as above; if it
  is missing, the placeholder is used. On a `grip_*` socket the prop is turned by its
  manifest's `prop.grip_offset.rotation_deg_xyz` (bows, crossbows, pistols and shields:
  90 degrees about X so -Z points forward), as Godot would.

The HUD lists the socket bones found in the GLB and the registry sockets missing from it
(and any socket the manifest names that the GLB lacks). The details panel shows
`rig_sockets` with their original bones, the wanted list and `sockets_missing`.

## Static mode (the public copy)

`tools/publish.py` builds a static copy of this page for GitHub Pages from the
**released** monsters (`"released": true` in `manifest.json`; `python3 -I tools/release.py
<id>... [--unset | --list]` sets it):

```sh
python3 -I tools/release.py goblin-warrior wolf            # gate: only these go out
python3 -I tools/publish.py --dest ../monster-forge-viewer --clean
python3 -I -m http.server 8797 --directory ../monster-forge-viewer   # check it locally
```

Layout of the destination: `index.html` (with the static marker below), `app.js`,
`style.css`, `vendor/`, `index.json`, `props.json`, `<source>/<id>/` (`model_rigged.glb` or
`model.glb` as LOD0 with embedded textures, `model_rigged_lod1-3.glb`, a `lods.json` naming
them, `anims/<clip>.glb`, `thumb.png`, a trimmed `manifest.json`), `props/<id>/` (`model.glb`,
`model_lod1-3.glb`, `lods.json`, a manifest with `prop.grip_offset`) for the props the
released monsters wield, `README.md` with the CC-BY-4.0 SRD attribution, `.nojekyll`.
`--clean` empties the destination first but keeps `.git`, `CNAME` and `.github`, and refuses
a non-empty directory without the `.monster-forge-publish` marker; nothing outside the
destination is touched. The script prints the size per monster, the total and the biggest
files, and warns past GitHub's 100 MB per file and ~1 GB per site.

`index.json` is `build_index()` from `tools/viewer.py` (same code as `/api/index.json`)
cut down to the released monsters, with the same per-entry shape, except:

- `dir` is relative (`srd-5.2.1/<id>/`), so the page works under any Pages sub-path;
- dropped: `has_override`, `job`, `errors`, `hi_faces`, `files.hi`, `files.hi_textured`,
  `files.hi_bytes` (no RAW tier); `rig_quality` is `null` (rig-stage diagnostics);
  `files.base_color_*`, `thumb_head`, `thumb_ground`, `crop`, `ref` are `null` and
  `files.refs` is `[]` (pipeline images stay home), so neither the unarmed scores nor
  `grounded` show (`ref_unarmed` is copied as is but unused without refs);
- added per entry: `wields` (the registry's wielded props that were published); top level:
  `static: true`, `registry_total`, `license`, `attribution` (paragraphs taken from the
  repo README's "License and attribution" section) and `props` (`props.json`);
- `summary` counts only the published monsters.

**Detection.** The same `app.js` serves both modes. It is static when the page has
`<meta name="monster-forge-static" content="index.json">` (publish.py adds it to the copied
`index.html`), when the URL has `?static=1`, or when `/api/index.json` does not answer with
JSON (any plain static host). The marker means the public page never requests `/api/`, and
the dev page never requests a missing `./index.json`, so neither logs a 404.

In static mode the page reads `./index.json` and `./props.json` and loads everything by
relative path. It hides the stage bars, the body-plan table, the status filter, the stage
dots, the overrides form, the re-run queue and the job polling, and it never offers RAW (a
stored `mf.lodMode` of `raw` falls back to auto). It adds type and CR filters (with counts;
sizes only as present), shows type, size and CR in the list, shows a short details table
(stat-block facts, dims, LOD tris, joints, clips, sockets, props) and puts the attribution
footer under the list. Kept as in dev: model view, wireframe, wire on shaded, textures,
skeleton, sockets, LOD picker with auto LOD, animation selector and playback, `#<id>` links
and the keys.

**Props in static mode.** The prop toggle starts on. Without a query each hand gets the
monster's own wielded prop: the first entry of `wields` whose `props.json` `grip_socket` is
`hand_r` (or `hand_l`), loaded from `props/<id>/model.glb`. The goblin warrior gets the
scimitar and the shortbow; its shield is published too and shows with `?prop_l=shield`.
`?prop_r=` / `?prop_l=` work as in dev but only for published props. A hand with nothing
wielded stays empty: no placeholder sword and bow on a harpy.

## Keys

`j`/`k` or arrows: next/previous monster · space: play/pause · `n`/`N`: next/previous clip ·
`1`-`9`: pick clip · `0`: rest pose · `c`: cycle clips · `w`: wireframe · `W`: wire on shaded · `s`: sockets ·
`p`: placeholder props · `t`: textures · `a`: auto LOD ·
`l`: pin the next LOD · `f`: frame camera · `/`: search · Esc: leave search / close image or
LOD settings. Shortcuts work while a toolbar checkbox or slider has focus, not in text
fields.
