# Rig and anim stages (2026-11-04)

`forge.py rig` gives LOD0 a skeleton and skin weights and writes the game files.
`forge.py anim` writes clips on that skeleton. Both run after `lod`. The tool research is in
`research-rig-animate.md`, and the per-pilot results are in `TODO.md` ("Rig findings").

```sh
python3 -I tools/forge.py rig wolf                 # skip if the rigged files exist
python3 -I tools/forge.py rig wolf --force         # rebuild the game files from rig/chosen.glb
python3 -I tools/forge.py rig wolf --rig-reroll    # new SkinTokens rolls (--rig-rolls N, default 4)
python3 -I tools/forge.py anim wolf --force        # rebuild every clip in the animation_set
```

## Rig stage

Code: `tools/rig.py` (driver, score weights), `tools/blender_rig.py` (facing check,
roll measurements and pose test, export; headless Blender), `tools/rig_topology.py` (finds
legs, arms, head, tail, wings, spine and the centre line from bone positions).

Part finders that took some tuning (`rig_topology.py`):

- `find_arms`: per side the shortest chain off the upper body that hangs below its root and
  is not a leg. Chains that branch off the head or run through it (hair, ears, a jaw, the
  neck) are not arms, nor is a hanging chain whose tip reaches the ground band (a tail, a
  skirt). A side with no hanging chain takes a raised one (the merfolk's sword arm) if it
  reaches less than `WING_REACH` (0.4) x the height sideways, the reach `find_wings` needs
  for a wing; a side with a hanging arm never looks at raised chains, so the harpy's arms and
  wings are what they were. On the merfolk the left "arm" had been a hair chain off the
  head, which also put `back` on the head bone.
- `find_head` (upright): the highest bone on the centre line, a short one giving way to its
  parent. On the merfolk it takes `bone_5`, which owns the skull and face (3458 vertices,
  z 1.39-1.80 of 1.80); its child above owns only back hair.
- `find_tail(..., ground=True)` (the clips): when no horizontal tail of 4+ bones exists, a
  chain of 2+ bones that leaves the hips backwards and slopes down into the ground band
  (the giant eagle's 2-bone tail fan).
- `find_snout` (horizontal): the bone reaching farthest forward above a third of the
  height, but not a wing: bones more than `SNOUT_SIDE` (0.15) x the height off the centre
  line on a chain that reaches `WING_REACH` x the height sideways are skipped. The giant
  eagle's left wing tip (`bone_11`, 0.6 m ahead of the beak) had been its head, so the
  lunge led with a wing; it is `bone_5` (the beak) now. No other pilot's head changes. For
  avian the clips' neck also starts above the bone the wings hang from (`bone_3`), so a
  head turn no longer swings the wings.

**SkinTokens rolls.** SkinTokens (`tools/skintokens/`, its own uv venv) predicts a
skeleton and skin weights from `model.glb`. It samples without a seed, so every run gives a
different skeleton, and some runs crash (`assert res.skin_pred is not None`). The stage
runs N rolls (default 4, 240 s timeout each) into `rig/roll_<i>.glb`, after waiting for an
idle ComfyUI queue and asking ComfyUI to unload its models: SkinTokens needs about 14 GB of
VRAM. A roll takes 20-30 s. Failed rolls are logged and skipped; the stage fails only when
every roll fails.

**Facing check.** Before rolling, the stage measures which way `model.glb` points
(`blender_rig.py` mode `facing`, about 3 s), independently of QA's auto-facing, because a
mesh that does not face +Y in Blender (glTF -Z) gets a scrambled skeleton:

- **Body axis:** the vertical plane the surface is most mirror-symmetric about (scan over
  180 degrees, refined to 0.5). Paired wings, legs and horns vote for the true axis instead
  of pulling it sideways, which is what fooled QA's footprint PCA on the dragon.
- **Head end:** horizontal plans compare the surface in the top 40% of the height near the
  centre plane at each end (the head end is higher; wings are excluded by the centre
  band). Upright plans: the soles reach farther forward than the ankles. Insectoids and
  ambiguous ends (`sign_confidence` < 0.2) report only how far the axis is off.
- **Confidence:** how much better the best mirror plane is than a typical one,
  `clip((contrast - 0.1) / 0.3)`. A blob, or a curled or twisted pose, scores low.
- Second opinions in the report: the ground footprint's long axis (legged horizontal
  plans) and the bearing of QA's `head_region` from the body centre.

`manifest.rig.facing` = `{method, angle_deg, confidence, ok, head_known, bearing_deg,
suggested_yaw, ...}`; the full report is `rig/facing.json`. When `angle_deg` > 30 with
confidence >= 0.5 the stage logs a warning and refuses to roll (it raises, `manifest.rig`
gets `error` plus `facing`): pin `yaw` (the log prints `suggested_yaw` = QA's yaw plus the
measured bearing) and re-run `qa`. The `rig_facing_check` override set to `false` rolls
anyway. Low confidence only warns. `--force` without rolling records the check and never
refuses.

| mesh | angle off +Y | confidence | result |
|---|---|---|---|
| goblin-warrior, harpy, giant-spider, wolf | 0-0.5 | 1.0 | ok |
| owlbear | 4.0 (head end unknown) | 0.69 | ok |
| young-red-dragon (yaw pinned) | 6.5 | 0.75 | ok |
| young-red-dragon turned 65 degrees (QA's auto-yaw error) | 58.5 | 0.76 | refused |
| owlbear turned -50, wolf 35 / 90, goblin 45, harpy -35, spider 40 | the turn (+ the mesh's own offset) to within 1 | as unturned | refused |
| young-red-dragon_exp_curled (old mesh, 3 scrambled rolls) | 15.0 | 0.38 | warns: asymmetric pose |

The curled dragon does point roughly forward (head at +Y in a top view); its pose is
twisted with the wings spread fore and aft, so its rolls failed for another reason. The
check flags it only as low confidence; the roll score below is what catches its rolls.

**Scoring (v2).** Each roll is measured in Blender (`blender_rig.py` mode `score`, about
1 s) and scored in `rig.score()`: `score = -sum(weighted terms)`, 0 is perfect. The
weights are `rig.WEIGHTS`; one missing structural part costs 0.5, so any one of them
outweighs the small terms, which then rank complete rolls.

| term | weight | measure |
|---|---|---|
| `symmetry` | 5 | joints vs their mirror image across the centre line, / height (v1) |
| `dead_bones` | 0.02 | per bone with no vertex, or shorter than 2% of the height (v1) |
| `legs` | 0.5 | per leg missing or extra vs the plan (`FEET_BY_PLAN`; insectoid 6-10, legs + pedipalps) |
| `leg_pairs` | 0.3 | per foot without a twin on the other side of the mesh's centre line |
| `feet_mirror` | 1 | feet vs their mirror images, / height |
| `spine` | 0.5 | per torso bone short of 3: hip (hind legs' branch) to shoulder (front legs' or arms' branch), `rig_topology.find_spine` |
| `head` | 0.5 | no head (`find_head` upright, `find_snout` horizontal) |
| `tail` | 0.125 | per tail bone short of 4 (`find_tail`, quadruped and winged quadruped) |
| `wings` | 0.5 | per wing side missing (`find_wings`, winged plans) |
| `arms` | 0.5 | per arm side missing (upright plans with hands) |
| `joints` | 0.02 | per joint outside the plan's band (`rig.JOINTS`) |
| `outside` | 2 | share of joints more than 5% of the height outside the surface (joints only: glTF has no bone tails, the importer guesses them) |
| `bleed` | 1 | weight on vertices more than 0.3 x height from their bone, plus side bones' weight on the far side of the centre line |
| `torn` | 10 | pose test: mean share of moving edges stretched past 2x |
| `stretch` | 0.25 | pose test: log2 of the worst edge stretch past 8x |
| `stray` | 2 | pose test: mean share of vertices 0.3 x height from the bent part that moved more than 2% |
| `follow` | 0.5 | pose test, per bend: 1 - median(skin motion / rigid motion) of the part's own skin |

**Pose test.** Every part found is bent by 30 degrees at its root bone, one at a time:
legs, arms and the neck swing about X, the tail about Z, wings flap about Y. The skinned
mesh is compared with rest (edges, far vertices, the part's own skin). It catches a head
bone that moves no skin (owlbear roll 3), legs whose weights drag the belly, torn wing
roots, and an arm skinned to the wing behind it (harpy).

A roll that leaves any vertex unweighted scores -10. Old reports without the quality
terms fall back to the v1 score (`rig.score_v1`, also stored as `score_v1`). The best roll
is copied to `rig/chosen.glb`; when it still has structural `problems` (any of legs,
leg_pairs, spine, head, tail, wings, arms) the stage logs a warning. Every
`rig/roll_<i>.json` holds the measurements, the per-bend pose results, `parts` (bone names
per part), `score`, `score_v1`, `terms` and `problems`; `manifest.rig.rolls` holds them
without the per-bone lists. After finalize the kept `chosen.glb` is measured too
(`rig/chosen.json`, `manifest.rig.quality`), also when it was not rolled now, so a
hand-fixed rig shows its terms. `/api/index.json` carries both as `rig_quality`
(`tools/viewer.py` `rig_quality()`: `facing`, `score`, `score_v1`, `terms`, `problems`,
`joints`, `picked`, and per roll `file`, `score`, `score_v1`, `joints`, `problems`).

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

**Standing on the feet** (`blender_rig.ground_feet`, finalize, legged plans except
insectoids). QA grounds the mesh on its lowest vertex. When that is not a foot, so the
lowest vertex the legs own (max weight) is more than `FEET_GAP` (3 cm) up, and the skin
below the feet belongs to chains that leave the body at a branch (not the root or the
trunk), the game files are lowered onto the feet and each such chain is turned up at its
root, about the horizontal axis across it, until its skin clears the ground by `CLEAR`
(0.02) x the height. The pose is baked into LOD0 and the rigged LODs (weighted from LOD0 in
the old rest pose first) and applied as the new rest pose, so no repaint is needed;
`rig/chosen.glb` and its quality score keep the QA pose. Recorded in `rig.json` and
`manifest.rig.grounded` (`shift_m`, `turned_deg` per chain, `feet_low_m`, `min_z_m`).
Giant eagle: lowered 0.355 m, tail fan `bone_28` turned 33.9 degrees and the left feather
lobe `bone_30` 3.7; feet at z = 0, every LOD's lowest vertex within 1.4 cm of it, height
4.16 -> 3.81 m. The other pilots' feet already stand on the ground (the harpy's 4 cm gap
is hip feathers skinned to the root, which cannot be turned, so it is left as it is).

**`rig_reweight` override** (`tools/rig_reweight.py`, run by `blender_rig.py` finalize before
the sockets): a list of chains, each `{bones: [root..tip], reach, bias, blend, strip, fit,
hand_room, far}`. Vertices within `reach` of the chain and nearer to it (x `bias`) than to
any other bone go to their two nearest chain bones (a `blend` band keeps some old weight);
`strip` removes the chain's weights elsewhere; `far` drops weights of bones farther than
that from the vertex in and around the region (bleed across the body); `fit` first moves
the chain's joints to the centroid of the limb's cross-section at each end, and
`hand_room` stops the tip bone that far short of the limb's end so the 4%-height hand bone
`add_sockets` adds there spans the fist. Only the game files change: `rig/chosen.glb` and
its quality score keep SkinTokens' weights, and every `rig --force` re-applies it. The
harpy uses `[{bones: [bone_18, bone_19], reach 0.15, fit, hand_room 0.068}]`: SkinTokens
skinned its right arm to the wing chains (the left wing's bone_12/bone_13 included) and put
bone_18/bone_19's weights on the right thigh, and the arm bones ran along the outside of the
arm. After: `hand_r`/`grip_r` 1.3/0.2 cm from the skin (5.5/8.0 before), 3.3/3.0 cm from the
fist centre (10/12 before); bone_18/bone_19 own 197/108 vertices (0/0); edges stretched
past 4x over the sword cut 175 -> 77, over `fly` 211 -> 223 (right side 129 -> 139, the arm
now stays at the side while the wing beats; it used to fly up into the wing).

A successful `rig` or `anim` run drops that stage's entries from `manifest.errors` (which
forge.py appends on every failure), so a fixed monster no longer reads as broken.
The viewer's `sockets` toggle draws each socket bone with its axes, and `prop` hangs a
placeholder sword on `hand_r` and a bow on `hand_l` (`viewer/README.md`).

### Known limits

- Roll scores on the rolls kept on disk (v1 -> v2, picked first): goblin roll_1 -0.05 ->
  -0.38 (rolls 2-3 lose 0.8 for a loincloth chain taken for a third leg); dragon roll_1
  -0.49 -> -0.71 over roll_2 -0.78 (v1 would have taken roll_2), roll_4 -4.0 (one wing,
  1/3 legs, torn); owlbear roll_1 -3.1 over the kept roll_3 -4.0 (no roll has a spine;
  roll_3's head bone moves no skin); harpy roll_1 -1.5 (no SkinTokens roll has wings);
  the curled dragon's scrambled rolls -3.1 to -7.1. The hand-built harpy `chosen.glb`
  scores -3.5: its wing tips hang near the ground and are counted as legs.
- `find_legs` rejects a foot that fans into several toes from an ankle above 9% of the
  height (owlbear roll_2, which is also the only owlbear roll with a back chain to the
  hind legs), so such a roll loses 1.0 for "missing" legs, and the anim stage would fail
  on it too.
- No weights heatmap thumb yet; the overlay sheets used to check the score (mesh + bones
  coloured by part, front/side/top) were made by a scratch script.
- **Facing matters.** A mesh that does not face -Z gets a scrambled skeleton. The young red
  dragon went from scrambled to clean once its `yaw` was pinned. The facing check above
  catches a turned body; it cannot tell a twisted pose from a turned one (low confidence,
  warning only).
- No wing bones on the harpy in 4 of 4 rolls. Its wings are hand-added chains, re-skinned
  with `--use_skeleton` and re-weighted with `tools/blender_wing_weights.py`; clean up to
  about +/-20 degrees. The stage does not do this yet, so other winged bipeds will hit the
  same gap. `--use_skeleton`'s `trim` step deletes joints with no weights, so new bones
  need placeholder weights in the input.
- The owlbear has no spine (its legs hang off a back root); the wolf has no real tail
  chain. The owlbear's tail (3 bones) is too short for the clips' 4.
- Added hand bones point straight up in the bind pose, not along the forearm, so a prop
  attached with no offset stands upright from the wrist (the harpy's `hand_r` and `hand_l`
  in the viewer). Renamed hand bones keep SkinTokens' orientation. The prop-space
  convention in `docs/equipment.md` assumes +Y along the closed fist; a grip pose and a
  hand-aligned socket are still to do.
- Loose mesh parts weight fine; manifold cleanup is not needed before rigging.

## Anim stage

Code: `tools/anim.py` (driver), `tools/blender_clips.py` (the gait, headless Blender).

The stage builds every clip in the monster's `animation_set` that has a procedural
builder, from the skeleton alone (`rig_topology`: legs, arms, snout, neck/spine, tail,
wings, fangs). It needs `model_rigged.glb`. The shared framework: foot IK on the foot bone
(planted cycles, feet held level), chest-parented arm IK for the sword cut and bow draw,
FK layers about armature axes, and a per-frame measurement of the skinned mesh that lowers
or raises the body so every vertex stays within 3 cm of the ground and standing feet reach
their targets (a stride or lunge is rebuilt smaller when they cannot: `stride_scale`,
`body_scale` in the report). Clips: `idle`, `walk`, `attack_melee` (biped sword cut over the
right shoulder, quadruped lunge and bite, spider front-leg strike), `attack_ranged` (bow
draw), `hit`, `death` (bipeds topple backward, quadrupeds roll onto the right side, spider
legs fold; the last frame lies on the ground and holds), `fly` (hover with wing flap),
`breath`. Loops start and end on the same pose; all clips start at t=0 and stay in place.

Output: `anims/<clip>.glb` (armature only, one clip per file, 30 fps), `anims/<clip>.json`
and `anims/clips.json` (reports), `manifest.anim` (`clips`, `missing`, per-clip report,
`parts`, seconds). All six pilots are `anim_complete`; the stage takes 5-11 s per monster.
`viewer/README.md` ("Clips the anim stage writes", "Animations in the page") has the file
convention the viewer and the index follow.

A tail that lies or rests on the ground is turned up at its root in the floor clips
(`tail_raise_max_deg` in the report) instead of the body being raised off it. A tail fan
found by `find_tail(ground=True)` droops below its root, so a turn lifts it unevenly and
overshoots: for such a fan the settle passes also ease the turn back down while it floats
more than `FLOOR_TOL`, the last pass only lifts. Giant eagle walk (a scratch job; its record
has no `walk`): lowest vertex -0.085 -> +0.001 m, body raise 0.092 -> 0, stride 0.5 -> 0.8,
fan turned up to 25.7 degrees; its idle, lunge and hit no longer lift the body either (the
lunge runs at full size, was 0.8). Since the rig stage stands the eagle on its feet (above)
its fan rests 8 cm above the ground and the floor clips barely turn it (0-6 degrees); idle,
lunge, hit and death stay within 1.8 cm of the ground, fly hovers at 0.32 m as before.

**Idle height** (`anim.idle_lift`, override `idle_lift`). The game moves a monster with
`walk`, else `swim`, else `fly` (godot-dnd `Monster.move_clip()`). A monster whose
`animation_set` has no `walk` only swims or flies, so its idle is held where that clip holds
the body (`match_move`): a `lift` move clip (legless swim, the ghost's fly) gives the idle
the same constant body lift, a `hover` one (a bird's fly) the same lowest-vertex height,
and a legged flier idles in the air on a slow, small beat of its fly clip (0.6 of the flap,
2.5 s) instead of standing on nothing. Monsters that walk keep a floor idle (harpy, dragon:
they land). The override takes metres of lift, `"match_move"`, or `false` for the floor.
Body lift idle / move, before -> after: giant octopus 0.062 / 0.341 -> 0.341 / 0.341 (lowest
vertex 0.00 -> 0.28, swim 0.21); merfolk 0 / 0.18 -> 0.18 / 0.18 (swim pitches the body, so
its lowest vertex is 0.43); giant eagle lowest vertex 0.00 / 0.32 -> 0.32 / 0.32; ghost and
hunter shark already matched (0.18, 0.74).

**Lift clips that would touch the ground.** `lift` keeps the body at one height, but a pose
can still push a part through the floor (the ghost's hit tilts its robe train to -1.3 cm):
those frames, and only those, are raised by the dip (ghost hit: lowest vertex -0.013 -> 0,
lift 0.18 -> at most 0.193 m).

Known limits: the owlbear walk and lunge run at half size. The kept skeleton
(`rig/chosen.glb` = roll_1 since score v2) skins the tail fan to the tail chain (`bone_13`);
the fan on a hind shin was reported on roll_3 (`rig/chosen_roll3_backup.glb`), kept before score v2. At full stride the front
paws' non-sole vertices dip 5-14 cm and the standing feet fall 0.093 m short of their
targets (limit 0.035), so a re-weight of the fan would not help. The harpy's dip is not a
tail either: hip feathers skinned half to the root and half to the right thigh hang to
the ground (and the wing tips to 5 cm), so its body is raised about 0.1 m and the walk runs
at half stride; its wings hide most of the sword cut. The spider's death rests on curled
legs rather than its belly.

## Body plans (2026-11-04)

One pilot per plan the six pilots did not cover took the whole chain (`forge.py run`, paint
included). `rig.RIG_STRATEGY` says which plans skip SkinTokens for a generated skeleton
(`blender_rig.py` mode `fallback`, `manifest.rig.tool` = `fallback:<kind>`, the build report in
`rig/fallback_build.json`, its measurements in `rig/fallback.json`); `--rig-reroll` on such a
plan still rolls SkinTokens first and logs the scores for the record. `anim.LEGLESS` plans get the
"legless" builders in `blender_clips.py` instead of the foot-IK framework; avian and
centauroid stay on the legged framework (centauroid adds the arms via `anim.ARMED`).

| plan | pilot | rig | clips | result |
|---|---|---|---|---|
| serpentine | constrictor-snake | `fallback:spine_chain` (16 bones). SkinTokens: 7-13 joint star, no chain (4 of 4) | `walk`/`swim` slither (curvature wave head -> tail, mid-body root holds place and heading), `idle`, `attack_melee` strike (front quarter rears and strikes), `hit`, `death` (front slumps). Ground `contact` | works; the coiled mesh's touching coils needed a weight fix (below) |
| floating | ghost | SkinTokens roll 3 (34 joints): a biped under the robe, legs to the hem, both arms, `hand_r/l` | `idle`, `fly` (lean + hem trailing), `attack_melee` (both arms reach, FK-aimed), `hit`, `death` (falls forward onto the ground; the robe train, flat on the floor behind the feet at rest, folds back at the hem's first bone below 0.15 x the height so it lies flat instead of standing up as a 0.9 m sheet: last-frame top 1.28 -> 0.78 m, body raise 0.30 -> 0.18). All but death `lift`: a constant 0.1 x height above the ground in every clip | works |
| avian | giant-eagle | SkinTokens roll 3 (32 joints): both wing chains, 2 legs, no spine (`problems` spine, leg_pairs) | legged framework: `idle`, `fly` (both wings flap), `attack_melee` (lunge), `hit` (no body tilt for avian), `death` (pitches forward onto its breast, wings fold back). The 2-bone tail fan resting on the ground is turned up instead of the body being raised (Anim stage) | works; `walk` (not in this record's set) builds clean in scratch. The head is the beak (`find_snout` skips wings) and the rig stage stands it on its feet (Rig stage) |
| amorphous | black-pudding | `fallback:vertical` (4 bones, one straight line through the upper body's bbox centre). SkinTokens: 21-24 joints, 0.83-0.88 bleed | squash and stretch = the root's scale (`vscale`: on the bone axis closest to world up), lean = chain about X. `idle` pulse, `walk` ooze, `attack_melee` engulf (rear up, surge and flatten forward), `hit`, `death` (flattens into a puddle 1.5x wide). Ground `contact` | works |
| plant_object | violet-fungus | `fallback:vertical` (5 bones). SkinTokens roll 3 was clean but rooted at the cap with the stalk hanging down | stalk sway `idle`, `walk` shuffle (rock), `attack_melee`/`attack_special` (coil back, whip forward), `hit`, `death` (topples onto its right side: the root bone, which owns the base clump, stays planted and the stalk breaks over at the next two bones (0.7 / 0.3 of the angle) by the angle that brings the cap onto the ground, bisected in `fit_angle`: 83.6 degrees; the base used to roll over with it and stand on its side). Limbs (tendrils, branches) found off the stalk swing too when the skeleton has them | works (this mesh has no tendrils) |
| tentacled | giant-octopus | `fallback:tentacle_hub`: root + body + 8 tentacles x 5 bones (tips = local maxima of geodesic distance from the mantle). SkinTokens rolls 2-3 also gave a usable hub (58-67 joints), but unnamed | `idle` (phase-shifted curls), `swim`/`fly` (all pulse together, `lift`), `walk` (crawl wave), `attack_melee` (two front tentacles rear and lash), `hit`, `death` (splay flat, body flattens) | works |
| aquatic | hunter-shark | SkinTokens roll 2 (32 joints): spine head to tail, pectoral fins, tail lobes | `swim`/`idle`/`walk` carangiform wave (grows toward the tail), `attack_melee` (dart + head snap), `hit`, `death` (rolls belly-up, sinks to the ground). All but death `lift` 0.25 x height | works |
| centauroid | centaur-trooper (proxy: a horse and rider) | SkinTokens roll 3 (66 joints): both arms, head, spine; 3 legs (the reference prances, one front hoof lifted) | stage refuses: 3 of 4 feet. Built in scratch with feet 3: all six clips (sword cut and bow draw on the rider's arms, quadruped walk and death) | strategy verified on a proxy; no centaur mesh yet |
| merfolk_naga | merfolk-skirmisher | SkinTokens (52 joints): spine, tail chain, arms with fingers. Both arms are out sideways, the right one raised: `find_arms` takes them since the raised-arm fallback (sockets `hand_l` bone_16, `hand_r` bone_28, `back` on the chest bone_3, `head` bone_5; score -1.40 -> -0.80, no problems) | `idle`, `swim` (body pitched 55 degrees, tail kicks about X, `lift`), `walk` (tail slithers sideways), `attack_melee`/`attack_ranged` (biped sword cut on the right arm, also for the throw), `hit`, `death` (topples onto its right side; the left arm, on top, drops against the body instead of pointing at the sky, the right one swings up past the head) | works |
| swarm | swarm-of-rats | `fallback:root` (1 bone, all weights 1) | root only: `idle` bob, `walk`/`fly`/`swim` scurry (lift for fly/swim), `attack_melee` surge, `hit`, `death` (scatter and sink: 1.7x wide, 0.3 of the height, a slight slew; a squash to 0.15 flattened the rats) | works; the pilot mesh is a 1.4 m column of rats (the reference draws a wall of them). The batch should model one unit per swarm and leave the swarm to the game (particles or instances) |

Fallback skeletons (`blender_rig.py`):

- `spine_chain`: the two surface points farthest apart along the welded edge graph are the tips;
  joints are the centroids of bands of geodesic distance; the higher (else thicker) end is the
  head. Bones `spine_00` (tail) .. `head`, the root mid-body. Weights linear along the body.
  Touching coils fuse the surface, so the geodesic band runs across into the next coil: a vertex
  farther than 2.5 x the tube radius from its band's bone takes the position along the chain of
  the bone nearest in space (939 of the snake's vertices). Without that, rearing the head lifted
  whole coils into the air.
- `vertical`: a straight chain up from the base through the upper body's bbox centre; weights
  linear in height. Slice centroids wander with a puddle or drips and bend the chain.
- `tentacle_hub`: see above; vertices beyond 0.35 x the farthest geodesic distance go to the
  tentacle whose path is nearest along the surface, blended into the body at the base.
- `root`: one bone.

The pose score was written for SkinTokens rolls; on fallbacks `bleed` and `outside` count by
design (a vertical chain owns a whole puddle), so the fallback scores (-0.15 to -3.1) are not
comparable with roll scores. Symmetry is not scored for the plans with no mirror plane
(`rig.FACING_ADVISORY`), the head not for `rig.HEADLESS`, wings are scored for avian except
`custom.theropod`, arms and head for floating (`rig.UPRIGHT_BODY`).

Facing: the check is advisory (recorded, never refuses) for serpentine, amorphous,
plant_object, swarm and tentacled (`rig.FACING_ADVISORY`), and `blender_rig.NO_HEAD_CUE`
(those plus floating) reports only the axis: the ghost's robe train trails behind and read as
toes pointing backwards (175 degrees "off").

QA: `forge.MAX_SCALE_PLANS` (floating, amorphous, plant_object, swarm, tentacled) scale
`scale_m` to the largest dimension (`blender_qa.py` scale_axis `max`); a ghost scaled by its
depth came out about 3x too tall.

Ground modes (`Clip.ground`): `floor` (legged), `hover` (fly: lowest vertex `HOVER` above the
ground, per clip), `rest` (death), `contact` (legless: lowest vertex on the ground every frame),
`lift` (floating, aquatic, swim: the same constant height in every clip, so clips do not pop
against each other; a frame whose pose dips below the ground is raised by the dip), `none`
(a rooted fall: `Clip.fit` bisects the clip's fall angle until the last frame's lowest
vertex not owned by the planted root bone touches the ground). Bone scale layers:
`Clip.scale`, keyed with the rotations.
