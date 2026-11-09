# Mesh quality: turnaround views, multi-view Pixal3D and mesh candidates (2026-11-08)

The mesh stage used to make one Pixal3D mesh from one reference view. Single-view image-to-3D invents the back
and the underside, and it fuses parts that touch in the picture. This page covers two additions to `forge.py mesh`
and the pilot that measured them:

1. **Turnaround views + multi-view Pixal3D.** From `ref.png`, Wan 2.2 TI2V 5B films the camera circling the
   creature; the frames nearest the left, right and back views feed Pixal3D's multi-view model
   (`Pixal3DMultiViewConditioning`, `pixal3d_multiview_int8_convrot`) next to the reference as the front.
2. **Mesh candidates with an auto-pick.** The stage can roll N meshes (seeds) per conditioning mode, score each
   one (`tools/mesh_score.py`) and keep the best; `mesh_pick` pins one.

Both are off by default (`mesh_n` 1, `mesh_views` single: the stage is byte-for-byte the old one). The pilot
below did **not** make multi-view the default: the Wan turnarounds are not consistent enough, and on the six
pilots the multi-view meshes were never better than the best single-view one. Candidates help a little and are
cheap. Recommendation and the re-roll list: the end of this page.

## Turnaround views (`tools/turnaround.py`)

Everything runs on what was already installed in ComfyUI (core nodes only, no new packages):

| step | node / model |
|---|---|
| orbit video | `Wan22ImageToVideoLatent` + `KSampler` (uni_pc, 20 steps, cfg 5, shift 8), `wan2.2_ti2v_5B_fp16`, `umt5_xxl_fp8_e4m3fn_scaled`, `wan2.2_vae`; 81 frames at 704 x 704, start image `ref.png` |
| the other direction | the same graph on `ImageFlip(ref.png)`; its frames are mirrored back |
| masks | `RemoveBackground` (BiRefNet) on every decoded frame and on `ref.png` |

Prompt: "3D turnaround of a <name> figure that stands perfectly still. The camera circles around the figure: it
starts in front, moves to the side, and finishes behind the figure, looking at its back. Plain light grey studio
background, even flat lighting, constant distance, eye level." Wording matters: "the camera smoothly orbits halfway
around it ... ends directly behind it" left the goblin standing still on 3 of 3 seeds, this one orbited on 3 of 3.
The direction (left or right) varies with the seed, so the stage does not rely on it.

**Which frame is which view.** A single-view mesh of the same reference (candidate `s0`) gives the azimuth: its
silhouettes from orbit cameras (5 degree steps, the reference's FOV and elevation fitted to the reference mask)
are compared (IoU of bbox-normalised masks) with every frame's mask. A mirror-symmetric body has the same
silhouette from the front and the back (and from 30 and 150 degrees), so the azimuths are read as a monotonic
orbit by dynamic programming (starting at 0, at most 15 degrees per frame, one direction), which keeps the back
half of the orbit from folding onto the front half. Its left and right profiles are near twins too (the goblin's
mirrored video matched the right side as well as the left), so the direction comes from optical flow: the
surface nearest the camera moves against the camera, so the summed horizontal Farneback flow inside the
creature, relative to its silhouette's centroid, over the first 60 frames is > 0 when the camera went to the
viewer's left (azimuth decreasing) and < 0 when it went right (goblin: +118 and -146 px). Below 20 px the
silhouettes decide. The frames nearest +90 (left), -90 (right) and 180 (back,
within 25 degrees; sides within 20) are kept. Videos are made until all three are found (at most 4: the
reference, its mirror, then the next seeds).

**Framing.** `Pixal3DMultiViewConditioning` assumes a fixed orbit rig: every view the same scale, the widest
extent 1/1.1 of the frame, elevation 0, horizontal FOV 20. `turnaround.py` puts each view on black (RGB x mask)
at one common scale (the largest extent over the views = 1024 / 1.1 px), vertically on the views' common centre,
horizontally on each view's own bbox centre.

## Multi-view mesh graph

`forge.build_mesh_graph(..., views=...)` (`mv_graph`): the single-view nodes (`load_image`, background removal,
`ImageCropToMask`, MoGe FOV) go; four `LoadImage` nodes feed `Pixal3DMultiViewConditioning` (`fov` 20); the UNet is
`pixal3d_multiview_int8_convrot.safetensors`. Everything after the conditioning (512 sampling, the 1024 upsample,
remesh, weighted decimation, bakes) is the same graph as before.

## Mesh candidates and the score (`tools/mesh_score.py`)

`forge.py mesh <id> --mesh-n 3 --mesh-views both` (overrides `mesh_n`, `mesh_views`, `mesh_pick`; `--n` on the
`mesh` command means `--mesh-n`):

- `single`: candidates `s0..s<n-1>`, seed + 10 k.
- `mv`: one single-view mesh `s0` for the turnaround (not pickable), then `mv0..mv<n-1>`.
- `both`: `s0..` and `mv0..`, all pickable.

Each candidate runs generation + remesh only (`hi_poly_graph`) and is saved as `mesh_cand/<tag>_hi.glb`. The
picked one then goes through the normal stage (weighted decimation and bakes); when it was the last one rolled,
ComfyUI serves its generation from cache, otherwise it is regenerated from the same seed (deterministic: `s0`
reproduced the SRD goblin's `model_hi.glb` face for face, 492,828 faces). A turnaround that finds no usable
view falls back to the single-view candidates (logged, `mesh.turnaround.error`).

Score (0-1, weights in `WEIGHTS`), read off the high-poly mesh in Pixal3D's raw frame:

| part | weight | what |
|---|---|---|
| sil | 0.35 | silhouette IoU against the reference's BiRefNet mask from the reference camera (FOV and elevation fitted) |
| sym | 0.20 | the best vertical mirror plane's error (mean reflected nearest-neighbour distance / bbox diagonal) and its contrast (median over bearings / best: the facing check's mirror-plane confidence); front-view plans want the plane at x = 0. This is what separates the lumpy multi-view meshes (goblin contrast 3.2 vs 4.5) |
| legs | 0.25 | connected parts of horizontal sections at 6-22 % of the height vs the plan's leg count (biped 2, quadruped 4, insectoid 6-8) |
| arms | 0.05 | bipeds: sections at 45-60 % of the height with 3+ parts (both arms free of the torso) |
| clean | 0.15 | floating shards: pieces under 5 % of the surface besides the largest (a coat or wing meshed as its own shell is a part, not a shard) |

Parts that do not apply (legs for a floating plan) drop out and the rest are re-weighted. The manifest records
`mesh.candidates` (tag, kind, seed, score, parts, metrics, seconds), `mesh.mesh_pick` ({tag, index, by: score |
pinned}), `mesh.candidate_plan`, `mesh.turnaround` (views, picked frames, azimuths, seconds) and `mesh.views`.

## Pilot (2026-11-08): goblin-warrior, ogre, wolf, young-red-dragon, giant-spider, owlbear

`forge.py mesh <id> --mesh-n 3 --mesh-views both` in a scratch copy of the pipeline (`out/` untouched), on the
SRD references. Sheets (in `~/.cache/monster-forge-scratch/mvmesh/sheets/`):

- `cmp_<id>.png`: reference and the four framed views, then front / side / back / top of the old SRD hi-poly, the
  best single-view and the best multi-view candidate (hi-poly), the old textured low-poly and the new one.
- `cand_<id>.png`: every candidate (hi-poly, four views) with its score parts.
- `vid_goblin_a.png`, `vid_goblin_b.png`, `vid_wolf_a.png`, `vid_wolf_b.png`, `vid_dragon_a.png`,
  `vid_spider_a.png`: orbit video strips. `probe_goblin*.png`, `wan_goblin_*.png`: the prompt probes.
- `rerank_top24.png`: the re-roll list below, reference + old hi-poly.

| monster | turnaround | multi-view vs single-view (by eye) | auto-pick (final score) |
|---|---|---|---|
| goblin-warrior (biped) | 3 videos; left, right, back found; each video invents its own back (one a blue cape, one a green hood) | worse: lumpy, blurred face, layered plates on the head; the back follows whichever video gave it | s2 (s0 = s2 by eye) |
| ogre (biped) | 2 videos; all views, consistent | about even: rounder belly and more back muscle (it follows the side views), a little lumpier | s2 |
| wolf (quadruped, 3/4 reference) | 4 videos; the camera passed the tail fast, the silhouette azimuths lagged: "left" and "right" were rear three-quarter views, no back | broken: body twice as wide, a second muzzle | s0 |
| young-red-dragon (winged) | 4 videos; wings smear and leave the frame by frame 60, no frame fits the mesh (IoU < 0.65) | none made (fallback) | s2 (the three are near twins) |
| giant-spider (insectoid) | 4 videos; the reference is seen from above (fitted elevation hit the 30 degree scan limit, IoU 0.57), no frame fits | none made (fallback) | s2 (near twins) |
| owlbear (quadruped, 3/4) | 4 videos; sides found, no back | worse: bloated egg-shaped body, a stray lump under it | s0 |

Judged on what the task asked: **back/underside plausibility**: single-view backs were already plausible on all
six (coat, muscles, feathers); the multi-view backs copy whatever the video invented, which differs per video.
**Limb separation**: no change on the bipeds (the leg and arm slices match), worse on the two quadrupeds.
**Silhouette match**: multi-view slightly lower on every pilot (sil 0.79-0.84 vs 0.81-0.93). **Style drift**:
the Wan frames are brighter and more saturated, and the backs grow hair, hoods and capes; the texture of the
multi-view goblin came out acid green.

The single-view candidates differ little: the dragon's and the spider's three are near twins; the goblin's s1
and s2 carry a separate coat shell and the ogre's s1 a few shards. The score (after two fixes found on the goblin:
a big separate shell is not a shard, and the symmetry part must not saturate) picked the candidate I would have
picked on every pilot, and never a visibly worse one.

### GPU time (seconds, RTX 5070 Ti, upsample 1024)

| monster | one mesh today (SRD `mesh.seconds`) | single-view candidate (generation + remesh, warm) | turnaround (videos) | multi-view candidate | scoring (CPU) |
|---|---|---|---|---|---|
| goblin-warrior | 63 | 14-16 (106 cold) | 355 (3) | 16-44 | 12 |
| ogre | 97 | 10-18 | 222 (2) | 12-36 | 17 |
| wolf | 89 | 20-28 | 445 (4) | 42-64 | 32 |
| young-red-dragon | 66 | 12-18 | ~460 (4, failed) | - | 16 |
| giant-spider | 58 | 10-18 | ~460 (4, failed) | - | 15 |
| owlbear | 138 | 40-48 | 465 (4) | 42-60 | 29 |

A Wan video is 92-98 s (81 frames, 704 px, 20 steps) plus 15-25 s of analysis; the multi-view model is a second
5.6 GB UNet to load. `--mesh-n 3 --mesh-views both` took 390-925 s per monster against 60-140 s for one mesh;
`--mesh-n 3` single-view adds about 30-90 s (two more generations and the score) to the old stage. ComfyUI's RSS
reached 26 GB with Wan and Trellis both resident; `turnaround.generate` now calls `/free` after the videos.
The batch's mesh timeout (3 x 104 s) is too short for the multi-view modes.

## Recommendation

- **Keep single-view.** Multi-view from Wan turnarounds lost or failed on 5 of 6 pilots and tied on the one with a
  clean front view (ogre), at 4-7x the GPU time. The bottleneck is the views, not Pixal3D's multi-view model:
  Wan 2.2 5B invents a different back per video, drifts in colour, orbits at an uncontrolled speed (direction by
  seed) and breaks up wings and splayed legs. It stays in the stage as an opt-in (`mesh_views mv|both`) for a
  front-view biped with a hidden back worth trying, nothing more. Worth retrying with a real novel-view model
  (an image editor such as FLUX.2-klein, or a multi-view diffusion model with camera control), which was not
  installed and was out of scope (no downloads).
- **`mesh_n` stays 1 by default.** The auto-pick works but the seed spread is small; use `mesh_n` 3 (single-view)
  as the setting for a mesh re-roll, where it costs ~1 extra minute per monster and avoids a shell / shard /
  asymmetric seed. No SRD monster was re-meshed.
- **Most mesh faults in the SRD come from the reference, not the seed.** A wing hidden behind the body in a
  three-quarter view (young-gold-dragon, the young black / bronze dragons), a robe over the legs (cloud-giant,
  rakshasa, solar, night-hag) or huge hair snakes (medusa) is in the picture; a new mesh seed will not fix it,
  a new reference will (the rig-friendly pose templates, docs/references.md).

### Ranked re-roll list (for approval; nothing re-rolled)

Evidence per monster: clip-audit stretch / limb_in_torso severity (fused limbs show as stretched edges,
`out/srd-5.2.1/_audit/flags.json`), canon wing problems, rig legs under the plan's count, UAL verdict, and
`tools/mesh_score.py` on the current `model_hi.glb` (leg / arm slices, mirror error, shards). Script:
`~/.cache/monster-forge-scratch/mvmesh/bin/rerank.py`; the full table is
`~/.cache/monster-forge-scratch/mvmesh/sheets/rerank.md`. "fix" says what would help: **ref** = a new reference
(pose template / prompt), **seed** = a mesh re-roll with `mesh_n 3` on the same reference.

| # | monster | plan | evidence | fix |
|---|---|---|---|---|
| 1 | young-gold-dragon | winged_quadruped | 0 of 2 wings found, far wing missing in the mesh, shards 0.30 | ref (quad_wings_side) |
| 2 | medusa | biped | stretch sev 10.5, UAL needs_review; hair snakes drawn as huge serpents around the body | ref |
| 3 | adult-black-dragon | winged_quadruped | stretch sev 25, tangled wings/tail | ref |
| 4 | sphinx-of-lore | winged_quadruped | 0 of 2 wings found, shards 0.26 | seed, then ref |
| 5 | young-black-dragon | winged_quadruped | 1 of 2 wings, rig 2 legs, shards 0.15 | ref |
| 6 | young-bronze-dragon | winged_quadruped | 1 of 2 wings, rig 3 legs, asymmetric | ref |
| 7 | bone-devil | winged_biped | stretch 4.1, UAL needs_skintokens, shards | seed |
| 8 | gold-dragon-wyrmling | winged_quadruped | rig 3 legs, shards | seed |
| 9 | ancient-green-dragon | winged_quadruped | rig 2 legs, asymmetric | ref |
| 10 | solar | winged_biped | stretch 9.3, arms fused into the robe | ref (robe) |
| 11 | ancient-bronze-dragon | winged_quadruped | rig 3 legs | ref |
| 12 | night-hag | biped | stretch 6.7, UAL needs_review (robe) | ref (robe) |
| 13 | ancient-white-dragon | winged_quadruped | rig 3 legs, asymmetric | ref |
| 14 | incubus | winged_biped | stretch 4.4, UAL needs_skintokens | ref |
| 15 | giant-scorpion | insectoid | stretch 7.7, leg slices 2 at the floor | seed |
| 16 | cloud-giant | biped | stretch 5.3, arms fused (robe) | ref (robe) |
| 17 | giant-lizard | quadruped | rig 1 leg | ref (quad_low_side) |
| 18 | young-brass-dragon | winged_quadruped | rig 3 legs | ref |
| 19 | rakshasa | biped | stretch 5.2, arms fused (robe) | ref (robe) |
| 20 | ancient-red-dragon | winged_quadruped | stretch 3.9, asymmetric | ref |
| 21 | ancient-copper-dragon | winged_quadruped | rig 3 legs, shards 0.32 | seed |
| 22 | frog | quadruped | rig 2 legs, shards | seed |
| 23 | griffon | winged_quadruped | rig 3 legs, asymmetric | ref |
| 24 | pegasus | winged_quadruped | 1 of 2 wings | ref |
| 25 | red-dragon-wyrmling | winged_quadruped | rig 3 legs, asymmetric | seed |

## Known limits

- The leg slices miscount spiders (legs raised off the floor between the tips: 4 parts for 8 legs) and dragons
  (tail and wing tips on the floor); compare candidates of one monster with each other, not across monsters.
- Turnaround azimuths come from the silhouettes of a single-view mesh; a quadruped seen three-quarter on is the
  weak case (the wolf's frames lagged by ~90 degrees). Picked frames now need IoU >= 0.65 at their azimuth, which
  drops those views rather than feeding wrong ones.
