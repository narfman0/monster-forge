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
