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
