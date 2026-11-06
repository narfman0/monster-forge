# Reusing CC0 humanoid animation for bipeds

Status: plan, not started. Spike runs after the tier-1 paint pass (Blender competes with paint for RAM).

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

## Retarget spike (2026-11-06, uncommitted)
`tools/retarget_ual.py <id>... [--clips ...]` runs `tools/blender_retarget_ual.py` on the local Blender
(run on the ai box) and writes `anims_ual/<clip>.glb` (same layout as `anims/`: armature only, one
action named after the clip), `<clip>.json`, `metrics.json` (the procedural clips' metrics beside them)
and sheets in `~/.cache/monster-forge-scratch/retarget/<id>_{move,action}.png` (ours front/side, UAL
side, procedural side; left limbs blue, right orange, a green test blade on grip_r). ~13 s per monster.
Clips: idle=Idle_Loop, walk=Walk_Loop, run=Sprint_Loop, attack=Sword_Attack, hit=Hit_Chest,
death=Death01, cast=Spell_Simple_Enter+Shoot+Exit (chained; Shoot alone starts with the arm up).
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
Open: grip_r's rest frame is not UAL's sword frame, so the test blade points sideways at rest and
down-forward in the cast; robes tear on big leg motion (archmage run/attack); clips start far from
the idle (walk 0.3-0.8 m) so the game must crossfade; not yet loaded in the viewer or Godot.
