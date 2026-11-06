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
