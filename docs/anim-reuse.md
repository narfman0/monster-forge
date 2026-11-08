# Reusing CC0 humanoid animation for bipeds

Status: opt-in in the pipeline (override `anim_source: "ual"`, then `forge.py promote <id>`); run on all 50
rigged bipeds (sheets judged, below). Not yet loaded in the viewer or Godot.

## Why
Our rigs come from SkinTokens: per-mesh skeletons (`bone_N`, 30-34 bones) with only the socket bones
named (`hand_l/r`, `grip_l/r`, `head`, `back`, `hip_l/r`). Clips are procedural per monster. Nothing
is shared, so packaged clips cannot be applied directly.

## Source
Quaternius Universal Animation Library 1 (120+ clips) and 2 (130+), CC0, one universal humanoid rig,
glTF/FBX/OBJ. Free "Standard" zip via itch.io (name your own price, $0): the download needs a browser,
it cannot be curl'd. Put the zip in `~/.cache/monster-forge-scratch/anim-packs/ual/` (unpack in its own
empty dir). Check the licence text in the pack before relying on it. Mixamo is not CC0 (no redistribution).

## Approach
1. Per rigged biped, map our bones to the pack's humanoid names (hips, spine, head, arms, legs) from
   bone chains + the named sockets. Store as `rig_humanoid_map` in the override or a sidecar json.
2. Keep our rig and weights untouched; retarget pack clips onto our armature in Blender (bone rotation
   transfer in rest-pose space, hip translation scaled by leg length ratio).
3. Output `anims/<clip>.glb` like the procedural clips, so the viewer and Godot need no change.
4. Check grip sockets on attack clips; giants/brutes need scaling and foot-slide checks.

## Spike
3-4 bipeds (archmage, an orc/goblin, a giant, ape-like arms). Judge in the viewer: idle, walk, run,
attack, death. Decide go/no-go before wiring it into `forge.py anim`.

## Pack contents (checked 2026-11-06)
Unpacked under `~/.cache/monster-forge-scratch/anim-packs/ual1|ual2/`. Both are CC0 1.0 (License.txt).
`Unreal-Godot/UAL*_Standard.glb` (in place) and `_RM.glb` (root motion baked): 43 clips each, 65 joints,
same rig: root, pelvis, spine_01-03, neck_01, Head, clavicle/upperarm/lowerarm/hand_l|r, 5 fingers x4,
thigh/calf/foot/ball_l|r.
- UAL1: Idle_Loop, Walk_Loop, Jog_Fwd_Loop, Sprint_Loop, Death01, Hit_Chest, Hit_Head, Punch_Cross/Jab,
  Sword_Attack/Idle, Spell_Simple_*, Crouch, Swim, Dance, Roll, Jump_*, Sitting_*, Pistol_* (not wanted).
- UAL2: Sword_Regular_A/B/C/Combo, Sword_Heavy_Combo, Sword_Block, Melee_Hook, Hit_Knockback, Idle_Shield,
  OverhandThrow, Zombie_Idle/Walk/Scratch (undead!), Walk_Carry, Slide, Climb, Consume, ...
Use the non-RM files (we move the monster in the game). Map: idle=Idle_Loop, walk=Walk_Loop, run=Sprint/Jog,
attack=Sword_*/Punch_*, hit=Hit_Chest/Knockback, death=Death01, cast=Spell_Simple_Shoot, undead walk=Zombie_*.

## Bone map (tools/bonemap.py, 2026-11-06)
`python3 -I tools/bonemap.py <id>... | --all [--json f] [--show]`: pure Python, reads only the glb's JSON
chunk, writes `out/srd-5.2.1/<id>/bonemap.json` ({our_bone: ual_bone}, confidence, problems, skipped
extras, `ual_missing`). Rules are in the module docstring. Extra bones are skipped (rest local pose,
follow the parent), never interpolated; missing UAL bones fold into the next mapped child
(spine_02 -> spine_03, neck_01 -> Head). All rigs face -Z with hand_l on -X (checked on every one);
UAL faces +Z with left on +X, so the retarget turns UAL 180 deg about Y and maps sides by name.

Result over the 50 rigged bipeds/winged bipeds: 13 ok, 36 partial, 1 fail.
- fail: medusa: the rig stage named the root bone `head` and no chain reaches the floor (scrambled rig).
- partial, all essential bones mapped: 26 lack only neck_01 (the head bone sits right on the chest
  bone), 6 lack neck_01 + spine_02, 3 only spine_02 (2-bone spine), cultist also has no toe bones
  (ball dropped). half-dragon: 3-bone shins (digitigrade, one joint skipped per leg) and its tail
  also reaches the floor (rejected by the mirror test).
- Not failures, but worth knowing: 17 rigs have no `head` socket (head found structurally); in 15
  the arm runs on through a finger and hand_l/r is the last finger joint, so UAL hand_* goes to the
  wrist 2-4 joints up (the grip socket still rides along). In SkinTokens' 4-joint arms the wrist pick
  is a length-share guess: flesh-golem's (forearm 0.49 m, hand 0.39 m) is the least certain.
- Unverified: nothing has been retargeted yet; the maps are judged from rest positions only.

Spike candidates (no giant is rigged yet; flesh-golem, 2.4 m, is the largest rigged biped):
| monster | status | joints | differences from UAL |
|---|---|---|---|
| archmage | ok | 34 | 1:1 on all 22; skipped: 2 pelvis->thigh bones, skull tip, heel bones. Robe: SkinTokens' ankles sit 0.15 m behind the knees |
| goblin-warrior | partial | 40 | 2-bone spine (spine_02 folded), no neck; 1.0 m tall, tests scale. Alternative with a full map: bugbear-warrior (ok, 59 joints, 2.1 m, wrist 4 finger joints above the hand socket) |
| flesh-golem (giant stand-in) | partial | 56 | no neck; wrist 4 joints above the hand socket, long-forearm guess (see above) |
| ape | partial | 30 | no neck (head on the chest bone), otherwise 1:1; long arms (hands at 0.84 m of 1.7 m), crouched legs |

## Retarget spike (2026-11-06)
`tools/retarget_ual.py <id>... [--clips ...]` runs `tools/blender_retarget_ual.py` on the local Blender
(run on the ai box) and writes `anims_ual/<clip>.glb` (same layout as `anims/`: armature only, one
action named after the clip), `<clip>.json`, `metrics.json` (the procedural clips' metrics beside them)
and with `--sheets spike` sheets in `~/.cache/monster-forge-scratch/retarget/<id>_{move,action}.png` (ours
front/side, UAL side, procedural side; left limbs blue, right orange, a green test blade on grip_r).
~13 s per monster. Spike clips: idle=Idle_Loop, walk=Walk_Loop, run=Sprint_Loop, attack=Sword_Attack,
hit=Hit_Chest, death=Death01, cast=Spell_Simple_Enter+Shoot+Exit (chained; Shoot alone starts with the
arm up); the current names and picks are in the table below.
What the method needed, found on the sheets:
- Deltas are measured from UAL's Idle_Loop frame 0, not its T-pose: from the T-pose UAL's stance is
  added onto ours (ape: thigh 0.81 forward, foot turned out 67 deg, one foot floating). Idle frame 0
  is now our rest pose.
- Arms match UAL's bone directions (rest A-pose vs T); legs and spine keep our stance (delta) while
  upright and blend to UAL's directions as the trunk tilts 25-65 deg: a rest offset keeps its size
  under rotation, so a thigh 19 deg off UAL's stood up out of the ground once lying (golem, ape).
- Pelvis offset scaled by leg length; per-frame ground fix from the feet joints; then, against the
  mesh, arms/head swing up (max 60 deg) and the whole body lifts so nothing is >1% of height under
  the floor (our heads, torsos and long arms went 0.17-0.44 m under on the death).
Open items from the spike, resolved in the next section: grip frame, start/end far from the idle, clip
names, pipeline wiring.

## Pipeline and batch (2026-11-06)
- **Grip roll.** UAL has no weapon bone; a fist's handle runs along the knuckle line, so UAL's sword
  axis is pinky_01 -> index_01 (fixed in UAL's hand frame, 78 deg off the hand axis in every clip).
  Per rig and hand, one constant correction in UAL's idle reference pose: roll about the forearm so
  grip +Y lies in that plane, then swing the rest (cap 45 deg): blade = UAL's sword axis on every
  frame. Before 4-57 deg off; after 0 deg on 89 of 100 hands, 1.5-11 deg on 11 (6 rigs; ape 11,
  swing capped).
  Sheets: blade forward-up at idle, over the shoulder in the wind-up, forward in the cast (was down).
  `metrics.json` / `ual.json` `grip_fix`.
- **Blend-in.** One-shots get an eased lead-in from the idle's first frame (0.2 s; hit 0.1 s) and a
  lead-out back to it (0.25 s; hit 0.2 s; none for death): `from_idle_m` and `to_idle_m` are 0 for
  attack_melee, hit and cast, death starts at 0 (was 0.05-0.75 m). Loops start at the frame of their
  cycle nearest the idle (`phase_frame`): walk 0.37 m median from the idle's first frame (max 0.71;
  spike 0.29-0.78), run 0.50 m (max 0.94; Sprint was 0.47-1.23). UAL's walk never passes through a
  feet-together stance, so walk and run still need the game's crossfade.
- **Names.** Files carry the anim stage's names, so a promoted clip is a drop-in. `run` is extra (no
  `animation_set` has it; the viewer shows it outlined). Per record (`retarget_ual.plan_clips`):

| ours | UAL | when |
|---|---|---|
| idle | Idle_Loop | default |
| walk | Walk_Loop | default |
| run | Jog_Fwd_Loop | always offered; Sprint_Loop tears more and starts farther from the idle |
| attack_melee | Sword_Attack | a `hand_r` prop (blade, axe, bludgeon, light blade); Sword_Regular_A(+_Rec) drops into a deep crouch, not better |
| attack_melee | Punch_Cross | unarmed (claws, slams); Punch_Jab is a smaller version of it |
| attack_melee | (procedural) | polearm or staff in `hand_r`: UAL has no thrust |
| hit | Hit_Chest | Hit_Knockback is a full knock-down (sinks 0.05-0.17 m), wrong for a hit |
| death | Death01 | |
| cast | Spell_Simple_Enter + Shoot + Exit | records with `cast` |
| idle / walk / attack_melee | Zombie_Idle_Loop / Zombie_Walk_Fwd_Loop / Zombie_Scratch | mummy, ghoul, ghast (ids with "zombie"); no run |
| fly, swim, breath, burrow, attack_ranged | - | procedural only (UAL has no bow draw; OverhandThrow untried) |

- **Gate.** Per clip `anims_ual/ual.json` says `ual` or `procedural` (keep the procedural clip; for run,
  no clip): edge stretch p99.9 above max(1.6 x, +3) the procedural clip's or above 7 (loops) / 10
  (one-shots); lowest vertex > 2% of the height under the floor; idle/walk feet > 4% of the height
  higher than the procedural clip's. It errs toward procedural but misses some robe tears that read at
  ~4.5, the same as a clean knight walk, so the visual calls are in the override `anim_ual_exclude`.
- **Pipeline.** Override `anim_source: "ual"` (bipeds, winged bipeds): `forge.py anim <id>` runs the
  procedural clips as before, then `retarget_ual.run` on the same machine's Blender (no ssh; ~15 s) into
  `anims_ual/`. `anims/` is untouched until **`forge.py promote <id>`** (= `retarget_ual.py --promote`):
  each clip the gate passes is copied into `anims/` (+ its .json), the procedural file it replaces moves
  to `anims_proc/` (kept on a second promote), `manifest.anim.source[clip] = "ual"`.
  `retarget_ual.py --unpromote <id>` restores them (checked byte-identical on bandit). Batch:
  `retarget_ual.py --all --jobs 8` (50 rigs in 108 s on ai), `--regate` redoes ual.json from metrics.

Batch over the 50 ok/partial bipeds (medusa skipped), sheets
`~/.cache/monster-forge-scratch/retarget/batch_00..24.png` (2 monsters each: side x6, front x3 per clip):
- No explosions, no left/right swaps (blue left, orange right in every front view), feet planted on idle
  and walk, deaths lie on the floor. The 2-bone-spine (cultist, dryad, goblin, hobgoblins, mage, ghoul,
  guard, chain-devil, half-dragon) and neck-less maps look no different from full maps; the folded
  bend is not visible at this size.
- Gate result after the exclusions (clips using UAL / offered): idle 50/50, hit 50/50, walk 29/50,
  death 28/50, attack_melee 19/46, cast 11/12, run 11/47.
- Use UAL (every gated clip; the gate's fallbacks are right): ape, assassin, azer-sentinel, bandit,
  bandit-captain, bearded-devil, berserker, bugbear-warrior, chain-devil, commoner, doppelganger, druid,
  ettercap, flesh-golem, ghoul, gladiator, gnoll-warrior, goblin-warrior, green-hag, grimlock, harpy,
  hobgoblin-captain, hobgoblin-warrior, mummy, pirate, sahuagin-warrior, satyr, scout, sea-hag, skeleton;
  also archmage, guard, guard-captain, night-hag (robes: the gate keeps only idle, hit, cast, death).
- UAL with exclusions (`anim_ual_exclude`, seen on the sheets): long robes/coats/capes smear on the walk
  and the lunge: cultist, cultist-fanatic, mage, mummy-lord, noble, priest, priest-acolyte, rakshasa
  (coat strips), knight (attack: cape smear, greatsword swung one-handed), bugbear-stalker (attack coat),
  animated-armor (tassets fan out behind the legs on the walk), dryad (dress; cast left forearm kinks),
  barbed-devil (its tails hang off the thighs and sweep with them), ghast (death: a long limb props up
  off the floor), lich (robe; also has no procedural clips yet, so nothing to fall back to).
- Procedural: half-dragon (its tail hangs off the left thigh and loops over the body in run and
  attack; UAL only for idle, hit), medusa (no map).
- Known: ghoul/ghast/mummy hit and death stand up out of the zombie hunch (the lead-in eases it);
  two-handers (berserker, knight, hobgoblin-captain) swing one-handed with the off hand free.
- Unverified: none of these clips has been loaded in the viewer or Godot; judged from Blender
  workbench renders and metrics only. Nothing is promoted in out/ yet.

## Direct UAL rig spike (2026-11-06)
Instead of retargeting onto SkinTokens skeletons: rig the mesh to UAL's own skeleton, so UAL clips play
with no solve. `python3 -I tools/rig_ual.py <id>... [--fit auto|skintokens] [--weights skintokens|heat|transfer]`
runs `tools/blender_rig_ual.py` (local Blender, CPU) and writes `out/srd-5.2.1/<id>/rig_ual/<fit>_<weights>/`
(model_rigged.glb + LODs, `anims/<clip>.glb`, metrics.json) and sheets in
`~/.cache/monster-forge-scratch/rigual/<id>_<fit>_<weights>{,_fit}.png`. Pipeline: see "Direct UAL rig batch" below.
- **Skeleton.** A copy of UAL's armature (65 bones, UAL names) with the joints moved onto the mesh; the
  180 deg turn (UAL faces +Z) is on the Armature node, which no clip keys. Arms, hands and legs turn their
  rest frame by the shortest arc from UAL's direction to ours, spine/neck/head/clavicles keep UAL's
  frame (our hunch rides along as an offset). A clip's local rotations are UAL's (a constant per-bone
  rest conversion); the pelvis height is scaled by leg length. Checked: dir-bone segments point exactly
  where UAL's do on every clip (0.0 deg). Optional floor lift (same rule as the retarget) for deaths.
- **Sockets.** hand_l/hand_r are UAL's own bones; grip_l/r (+Y along UAL's knuckle line, so a blade
  follows the fist like the retarget's grip roll), head, back, hip_l/r added non-deforming. The glb has
  the same layout as model_rigged.glb (Armature root, skinned `monster`, LODs, one action per clip file).
- **Fit "auto"** (no SkinTokens): voxelised mesh, medial Dijkstra paths hand-hand, sole-sole (apex =
  crotch; none = robe), pelvis-head; shoulders from the depth profile, all calibrated by running the same
  detector on UAL's mannequin drooped to our arm angle. Within 2-6% of the height (median) of SkinTokens'
  joints on archmage, ape, flesh-golem. Fails on goblin-warrior: big head (no neck to find), ears and
  spiked forearm guards taken for hands (max 0.6 h off). **Fit "skintokens"**: the mapped SkinTokens joints.
- **Weights.** (c) SkinTokens weights merged by name onto UAL bones: works, but only as well as the joints
  agree (painted around SkinTokens' joints): fine on golem/ape, archmage idle stretch 7.2 vs 4.2 retarget.
  (b) transfer from the conformed UAL mannequin: worst everywhere (stretch 2-6x; the robe and coat follow
  whichever thigh is nearest). (a) bone heat: Blender's heat fails outright on all 4 meshes (not
  manifold); heat on a voxel-remeshed proxy (1.2% h) + nearest-surface transfer works in ~1 s and wins:
  lowest stretch on every clip of every monster, below the retarget on most. Fingers are folded into
  the hand (no finger weights): hands move as rigid fists, which reads fine at sheet size.

Edge stretch p99.9 (heat proxy weights; auto fit, goblin with the SkinTokens fit) vs the retarget route:

| | idle | walk | run | attack | hit | cast | death |
|---|---|---|---|---|---|---|---|
| archmage | 4.6 / 4.2 | 5.0 / 10.5 | 9.2 / 15.5 | 9.2 / 15.0 | 4.6 / 4.4 | 5.1 / 9.4 | 5.6 / 12.4 |
| goblin-warrior | 3.9 / 1.7 | 4.4 / 2.6 | 7.0 / 5.6 | 10.7 / 8.9 | 3.5 / 1.8 | - | 10.8 / 11.0 |
| ape | 1.6 / 1.7 | 1.8 / 4.4 | 2.8 / 8.8 | 2.4 / 3.5 | 1.6 / 2.5 | - | 2.7 / 4.5 |
| flesh-golem | 1.4 / 1.5 | 1.6 / 1.9 | 2.4 / 3.5 | 2.3 / 3.8 | 1.4 / 1.5 | - | 2.4 / 4.2 |

Seen on the sheets: left/right right everywhere; feet planted on idle/walk (lowest vertex within 1% h,
the floor tolerance); deaths lie on the floor (lift 0.3 m on the ape: UAL's mannequin is thinner);
scale follows the mesh (goblin 1.0 m hip_scale 0.42-0.48, golem 2.4 m 1.36-1.41). The ape and the
archmage stand straighter than at rest in the arms/legs (UAL's directions) but keep their hunch. The
archmage robe still smears on run and the lunge (it splits down the middle onto the two thighs), less
than with the retarget; long robes stay an exclusion case. The goblin's tabard follows the right thigh.
Cost: 9-14 s per monster on ai's CPU (rig ~2 s, heat ~1 s, the rest clip export and tiles); the 50
bipeds at --jobs 8 in ~2 min. No GPU. Unverified: not loaded in the viewer or Godot; hands judged only at
sheet size; the auto fit was tried on 4 meshes only.

## Direct UAL rig batch (2026-11-06)
All 84 rigged bipeds/winged bipeds with an ok/partial bonemap (37 newly rigged ones mapped first: 5 ok,
29 partial, 3 fail = earth-elemental, shambling-mound (no hand sockets), goblin-minion (one leg found);
medusa still fails), `--fit skintokens --weights heat`, run on ai (`--jobs 8`, 84 rigs in ~2.7 min, 10-17 s
each, CPU). Outputs `out/srd-5.2.1/<id>/rig_ual/skintokens_heat/` + `rig_ual/verdict.json` (status, reasons,
`procedural_clips`, per-clip edge stretch p99.9 / min_z / floor lift). Sheets: 3 monsters each,
`~/.cache/monster-forge-scratch/rigual/batch_00..27.png` (fit, idle, walk x3, run x2, attack x3, hit, cast,
death x2, side and front).
- Fixes needed on the way: (1) heat on the remeshed proxy failed outright on 36 meshes ("failed to find a
  solution": voxel crumbs and loose prop parts that see no bone); the proxy now drops loose parts under
  2% of its vertices and, if heat still fails, retries at 2% / 3% voxels (all 84 solve, 1 needed 2%: sea-hag).
  (2) heat is no longer tried on the mesh itself first: on priest it "worked" on the non-manifold shells and
  the robe shattered (p99.9 21 -> 12 via the proxy); `heat_direct` keeps the old order. (3) UAL2's actions
  are loaded too (`ual_extra`): the zombie clips (mummy, ghoul, ghast, zombie, ogre-zombie) were missing.
- Checked on all: sockets present with the old rig's set (56 with head/back/hip, 28 hands/grips only, as
  their SkinTokens rigs) and parented right (grip -> hand, head -> Head, back -> spine_03, hip -> pelvis);
  left/right right in every front view; feet planted on idle/walk; deaths on the floor (lift <= 0.9 m).
- **use_ual 67** (41 with every clip; 26 keep some clips procedural, mostly robes/coats/capes on run and
  the lunge: archmage, assassin, animated-armor, bandit-captain, bugbear-stalker, cultist(-fanatic), druid,
  dryad, goblin-warrior, green-hag, guard(-captain), knight, mage, mummy-lord, noble, pirate-captain,
  priest(-acolyte), sea-hag, spy, tough-boss, vampire, werewolf, zombie). Clips on UAL: idle 67/67, hit
  67/67, cast 11/11, walk 59/67, death 55/67, attack 41/61, run 40/62.
- **needs_skintokens 12** (appendage with no UAL bone, heat hangs it on a thigh/spine): tails barbed-devil,
  bearded-devil, dretch, half-dragon, kobold-warrior, rakshasa, wererat, weretiger, sahuagin-warrior (+ fins);
  wings harpy; extra limbs ettercap; chains chain-devil.
- **needs_review 5**: ettin (two heads, each rides a shoulder), lich (robe, and no procedural clips to keep),
  night-hag (robe, stretch 6.6 already at idle), scout (cape takes the right arm and streams sideways), troll
  (a rag strip joins the feet and becomes a sheet on walk/run).
- Pipeline, opt-in: override `rig_source: "ual"` (or `forge.py rig <id> --rig-source ual`) makes the rig
  stage call `rig_ual.stage`: no SkinTokens roll, heat weights, fit `rig_ual_fit` = `auto` (model.glb alone,
  default) or `skintokens` (the existing SkinTokens rig's joints; kept once as `rig/skintokens_rigged.glb`).
  It copies the UAL rig's `model_rigged*.glb` over the game files (painted albedo set), drops the old
  skeleton's `anims/`, `anims_proc/`, `manifest.anim`, writes the clips + `ual.json` (retarget_ual's gate,
  no procedural baseline, `anim_ual_exclude` honoured) into `anims_ual/`; `forge.py anim` then makes the
  procedural clips on the UAL skeleton and skips the retarget; `forge.py promote <id>` as before.
  Tried only on a scratch copy on ai (ape auto, goblin-warrior skintokens; anim + promote ran). Not run on out/.
- Unverified: nothing loaded in the viewer or Godot; judged from workbench sheets at tile size (hands,
  faces, the procedural clips on the UAL skeleton not looked at). Verdicts are not wired into anything.

## Verdicts and the second batch (tools/rig_ual_verdict.py, 2026-11-07)
`python3 -I tools/rig_ual_verdict.py <id>... | --all [--compare] [--no-calls]` writes `rig_ual/verdict.json`
(schema above, plus `rules`: what the rules alone said and their signals) from `rig_ual/skintokens_heat/metrics.json`,
the UAL glb's weights, the SkinTokens rig/bonemap, the registry prompt and the sight calls in
`data/rig_ual_calls.json` ({id: {status?, procedural_clips?, reason, judged}}). Plain Python + numpy, 0.3 s for all.
Rules (module docstring): winged_biped, prompt wings/tentacles/extra arms/chains, a prompt tail with mesh behind
the hips, a SkinTokens chain off the trunk >= 0.4 h (or off the pelvis >= 0.15 h with mesh behind it) ->
needs_skintokens; two heads, missing sockets, idle/walk under the floor, idle stretch >= 6, no metrics ->
needs_review; else use_ual with run+attack_melee (+death > 7) procedural for long garments (robe, vestments, cloak,
cape, coat, dress, tabard, wrappings) and any clip over the 7/10 ceiling. `anim_ual_exclude` is not folded in
(rig_ual.stage applies it to ual.json).
- Against the 71 hand-written verdicts of the first batch (13 of the 84 were deleted with the quiver re-rolls):
  rules alone 65/71 status (92%), 55/71 status + clip list; with the calls 70/71. Rules miss tails heat hangs
  on a thigh without much mesh behind the hips (dretch, rakshasa, sahuagin-warrior: the last two read as
  needs_review from their idle tear), ettercap's extra limbs, troll's rag strip, and the robe/cape clip lists
  beyond run+attack; those are calls. The one difference: lich, needs_review on disk -> use_ual (walk, run,
  attack_melee procedural, like mage): it was review only for having no procedural clips, which it has now.
  Its file is not rewritten; `rig_ual_verdict.py lich` flips it.
- Second batch: the 39 ok/partial bipeds/winged bipeds without a verdict (bonemap re-run on them first: the
  re-rolled meshes have new rigs), `--fit skintokens --weights heat` on ai, 38 rigs in 79 s at `--jobs 8` (CPU);
  imp fails (1-bone spine, no spine_01 for the fit). Sheets `~/.cache/monster-forge-scratch/rigual/batch2_00..12.png`.
  Not rigged: bonemap fail (deva, solar, goblin-minion, incubus: legs; earth-elemental, shambling-mound, treant,
  tarrasque: no hands), medusa (excluded).

All 110 verdicts (on disk; lich counted as needs_review): **use_ual 74** (44 with every clip, 30 keep some
procedural), **needs_skintokens 31**, **needs_review 5**.
- needs_skintokens: wings (winged bipeds) balor, bone-devil, dust/ice/magma/steam-mephit, erinyes, gargoyle,
  homunculus, horned-devil, imp (also: rig failed), nalfeshnee, pit-fiend, planetar, sprite, succubus, vrock,
  harpy; tails barbed-devil, bearded-devil, dretch, half-dragon, kobold-warrior, quasit, rakshasa, sahuagin-warrior
  (+ fins), wererat, weretiger, werewolf (re-roll: a big tail/coat flap on the right thigh); extra limbs ettercap,
  glabrezu (four arms).
- needs_review: cloud-giant (floor-length robe, idle stretch 6.2, every clip but idle/hit smears), ettin (two
  heads), night-hag (robe, idle 6.6), troll (rag strip joins the feet), lich (see above: use_ual by the tool).
- New use_ual: chain-devil (the re-rolled chains are fused spikes, max p99.9 5.1), fire-giant, giant-ape,
  gnoll-warrior, goblin-boss, hobgoblin-warrior, shadow, shield-guardian, skeleton, stone-giant (all clips);
  frost-giant (cloak: walk, run, attack, death procedural), hill-giant (hide cloak: attack), hobgoblin-captain and
  storm-giant (capes: walk, run, attack), scout (cloak: run, attack; the old cape-takes-the-arm call no longer
  holds), wight (rags: attack).

Promote the use_ual set (not run): override per monster, then rig/anim/promote on a machine with Blender (ai):
```
python3 -I - <<'PY'   # rig_source ual, the SkinTokens joints, the verdict's procedural clips as anim_ual_exclude
import json, sys; from pathlib import Path; sys.path.insert(0, "tools"); import overrides
for f in sorted(Path("out/srd-5.2.1").glob("*/rig_ual/verdict.json")):
    v, mid = json.loads(f.read_text()), f.parts[-3]
    if v["status"] == "use_ual":
        o = overrides.load("srd-5.2.1", mid)
        o.update(rig_source="ual", rig_ual_fit="skintokens", anim_ual_exclude=v["procedural_clips"] or None)
        overrides.save("srd-5.2.1", mid, o)
PY
for id in $(python3 -I -c 'import json,glob; print(" ".join(sorted(f.split("/")[2] for f in glob.glob("out/srd-5.2.1/*/rig_ual/verdict.json") if json.load(open(f))["status"] == "use_ual")))'); do
  python3 -I tools/forge.py rig $id --force && python3 -I tools/forge.py anim $id --force && python3 -I tools/forge.py promote $id
done
```
`rig --force` reruns the direct rig (~15 s) and replaces model_rigged*.glb, anims/, manifest.anim (the SkinTokens
rig is kept as rig/skintokens_rigged.glb); `retarget_ual.py --unpromote` undoes only the promote step.
- Unverified: as before, sheets at tile size only (no viewer, no Godot); the rules' thresholds were tuned on the
  same 71 verdicts they are compared with; the new calls are one look per sheet.

## Extra chains: wings, tails, strips, second heads (2026-11-08)
The 31 needs_skintokens / 5 needs_review bipeds and the 8 bonemap failures, tried on the UAL rig with the parts UAL
has no bone for added as extra chains. Scratch runs under `~/.cache/monster-forge-scratch/ualx/<id>/<variant>/`
(sheets `<id>_<variant>.png`, compact rows `m_*.png`; `_root_<variant>/` = a simulated rig + anim + promote, with
`clip_audit.py --root` sheets in `_audit_<variant>/sheets/`).
- **Extra chains** (`blender_rig_ual.py` job `extras`, override `rig_ual_extras`): a SkinTokens chain's whole subtree
  is copied onto the UAL armature (SkinTokens joints and rolls), parented to the UAL bone its nearest mapped ancestor
  maps to (else the nearest trunk/clavicle/upper-arm bone), named so rig_topology finds it: `wing_<side>_00..`,
  `tail_00..` (more: `tail2_..`), `limb_<side><k>_..`, `strip<k>_..`, `head<k>_..`. `"auto"` (`rig_ual.auto_extras`):
  unmapped chains off pelvis/spine/neck/clavicle/upper arm reaching >= 0.12 h; kind from the rig stage's names, else
  shape (out to the side on a winged plan = wing, behind the hips = tail, hanging off the pelvis = strip, else limb).
  `{auto: "head", side}`: a 2-bone chain over the mesh's head on that side (ettin; no SkinTokens chain exists).
  `rig_ual_add_tail`: blender_tail_chain inserts a tail SkinTokens has no chain for, then it is adopted.
- **Weights** (`rig_ual_extra_weights`): `skintokens` (default) = per vertex the SkinTokens weight s on the adopted
  bones stays on them, the UAL heat weights (computed without the extras) take 1 - s; `heat` = bone heat with the
  extras deforming (needed for auto heads); `zone` = SkinTokens weights around the chains too (worse on every test:
  SkinTokens' thigh seams do not match UAL's hips). `rig_ual_weights: "skintokens"` merges SkinTokens weights for the
  whole body (troll, half-dragon, balor did better with it).
- **Motion.** UAL clips key the 65 bones; extras get a layer (`EXTRA_AMP`): tails/limbs sway (a hanging one swings as a
  pendulum), strips swing fore-aft, wings lift a few degrees, heads look about; loops close, one-shots start/end at
  rest, none on death. The anim stage's procedural clips (fly, breath, attack_ranged, the gate's fallbacks) find the
  `wing_`/`tail_` chains by name: fly beats as on SkinTokens (wing-tip travel equal on every winged one moved).
- **Fixes on the way.** rig_topology: a UAL rig's head is `Head` and its arms clavicle..hand by name (structurally the
  head was neck_01 or a wing root and the "arms" a pinky/thumb chain: procedural clips on all UAL rigs, incl. the 75
  promoted, moved non-deforming finger bones and left the arms still; those need `anim --force` + promote to pick it
  up). blender_clips: a tail the topology gate drops also drops the upright-tail layers (crashed the death). fit
  skintokens on a 1-bone spine (imp) interpolates spine_01/02; fit auto no longer needs UAL's own neck. Metrics carry
  `stretch_where` (the worst frame's torn edges by dominant bone).
- Judged against the SkinTokens procedural clips (out/_audit metrics as the gate's baseline: p99.9 <= max(1.6 x, +3)),
  then the sheets. Moved (rig_source ual, rigged/animated/promoted in out/): harpy, magma-, dust-, ice-mephit,
  homunculus, vrock, nalfeshnee, erinyes, planetar, kobold-warrior (auto extras); ettin (two auto heads, heat);
  troll (strips, SkinTokens weights; run procedural); bearded-devil, pit-fiend (heat extras; pit-fiend attack/death
  procedural); quasit (inserted tail; run); ettercap, glabrezu (plain: their extra limbs have no SkinTokens chain
  either); goblin-minion, earth-elemental (fit auto, bonemap failures). shambling-mound (fit auto) was promoted, then
  judged worse on the audit of the result (walk 4.6 vs 1.6, the procedural attack on the UAL skeleton 14 vs 5.9) and
  reverted (`rig --force` from rig/chosen.glb). The stage now keeps rig/skintokens_rigged.glb for every fit (fit auto
  overwrote the SkinTokens game rig), and the rig stage no longer carries `tool: "ual"` over to a SkinTokens rig.
  `rig_ual_verdict.py` reads the overrides' variant (`rig_ual.variant_for`: `<fit>_<weights>[_x]`) and counts an
  appendage flag as answered when an extra chain covers it. Calls with reasons in `data/rig_ual_calls.json`.
- Not moved (reasons in the calls): wings/legs tear at idle (gargoyle: tail welded to the calves; balor, horned-devil,
  bone-devil, steam-mephit, imp, succubus, sprite), inserted tails whose seam tears or that cover only part of the tail
  (barbed-devil, dretch, rakshasa, sahuagin-warrior, wererat, werewolf, weretiger, half-dragon), robes (cloud-giant,
  night-hag, deva, solar), fit auto failures (treant floats, medusa's snake body, incubus' right wing chain),
  shambling-mound (above).
- Unverified: tile-size sheets only, not loaded in the viewer or Godot.

## Shared UAL library (tools/ual_library.py, 2026-11-08)
UAL bipeds (rig_source "ual") no longer carry their own copy of each UAL clip: `forge.py share <id>...` (or
`python3 -I tools/ual_library.py share --all`, after `promote`) keeps one GLB per UAL clip in
`out/srd-5.2.1/_shared/ual/<clip>.glb` + `library.json` and drops the matching files from `anims/` (the same bytes stay
in `anims_ual/`; `ual_library.py unshare <id>` puts them back, checked byte-identical). Pure Python + numpy, ~0.5 s for all.
- **What differs between monsters** (checked on every clip it shares, `ual_library.py check`): blender_rig_ual bakes local
  rotations equal to UAL's own, so rotation channels are identical across monsters (<= 0.001 deg). Translations are each
  bone's constant rest offset (the monster's proportions) except the pelvis: `pelvis(t) = s * u(t) + c + lift(t) * up`,
  u = UAL's pelvis track, s = hip_scale (fitted, = manifest rig.hip_scale), c = a constant in the root bone's frame
  (our pelvis-over-hips vs UAL's, scaled), lift = the bake's floor lift (deaths, some walks; matches each clip's
  `floor_lift_max` exactly). After those the rebuild matches the bake within 0.05 mm on all 373 UAL clips of the 75 UAL
  bipeds; sockets and the root never move. A clip keying other bones off their rest (extra chains) or off the
  rebuild stays per-monster (none today).
- **Library clip**: UAL's clip resampled like `sample_clip()` (30 fps), on UAL1's own skeleton at its rest pose, 64
  rotation channels (all bones but root) + the pelvis translation u, nothing else. Note: Blender imports UAL at the
  scene's 30 fps and sample_clip reads those frames as 24 fps, so every baked UAL clip runs 1.25x longer than UAL
  authored it (Idle_Loop 2.5 s -> 3.125 s, Death01 2.4 s -> 3.0 s); the library replicates that so shared and
  per-monster clips agree. Fixing it means changing UAL_FPS in blender_rig_ual.py and re-baking (then `share` again).
- **manifest.anim.ual_shared** (the contract): `library` ("_shared/ual", relative to out/<source>/), `hip_scale`,
  `pelvis_offset` (c, glTF, root-local), `lift_axis` (root-local up), `fps`, `root_bone`, `pelvis_bone`,
  `clips` {our clip: library clip}, `lift` {our clip: per-frame metres; only clips with a lift >= 0.5 mm},
  `own` {clip: why it stays in anims/: procedural, ...}, `check` (max errors). `anim.clips` still lists every clip.
- **Players**: rotations as they are; pelvis = s * u + c + lift(t) * lift_axis; every other bone at its own rest.
  The viewer (viewer/app.js `sharedClip`) rebuilds the pelvis track per monster (checked in node with three.js r179
  against the anims_ual bakes: <= 0.05 mm on ape, stone-giant, archmage, mummy); `tools/viewer.py` lists them as
  `shared_clips`, the download zip (`game_files`) adds `_shared/ual/` (library.json + the monster's clips),
  `publish.py` copies them. godot-dnd: Skeleton3D.motion_scale = s, c in the root bone's rest, the lift as a
  SkeletonModifier3D (its docs/monsters.md "Shared UAL clips").
- Result (75 UAL bipeds): 373 of 484 anims/ GLBs shared, 36.9 MB -> 11 library files 0.88 MB; 111 GLBs (9.0 MB) stay
  per-monster (attack_ranged 46, attack_melee 31, walk 13, death 11, fly/swim 6, idle 2, cast 1, breath 1: the
  procedural clips). Re-run `share` after any `promote` (promote copies the clips back; rig --force drops ual_shared).
