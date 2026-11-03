# monster-forge plan (2026-11-03)

Goal: 100% of D&D monsters as low-poly (2k-8k tris), textured, rigged, animated
GLB assets of reasonable quality, SRD first. Research backing this plan:
`research-mesh-texture.md`, `research-rig-animate.md`, `body-plans.md`, `corpus-plan.md`.

## Verdict on the pasted claims

- "Local riggers are humanoid-only": false. UniRig and SkinTokens (MIT) rig humans,
  animals and objects; UniMate does text-to-motion on arbitrary skeletons;
  MoCapAnything V2 does video-to-skeletal mocap for any rig. All local, all MIT.
- "Only Tripo/Meshy handle quadrupeds": Tripo yes (7 rig types), Meshy API is humanoid-only.
- Cloud fallback cost: Tripo about $0.75 per rigged+animated asset (about $250 for the SRD).

## Pipeline (one monster = one job)

| Stage | Tool | Output |
|---|---|---|
| 1 Reference | Z-Image-Turbo int8 or FLUX.2-klein-4B (Apache-2.0); juggernautXL + controlnet-union for pose control | `ref_front.png`, `ref_side.png` (A-pose for uprights, side view otherwise) |
| 2 Matte | core RemoveBackground (BiRefNet) | cut-out |
| 3 Shape+PBR | core Pixal3D (or TRELLIS.2) int8 at 512; 1024 for hero monsters | high-poly mesh + PBR |
| 4 Low-poly | core RemeshMesh 256-384 -> DecimateMesh (2k-8k by size) -> UnwrapMesh | low-poly UV mesh |
| 5 Bake | core BakeTextureFromVoxel, BakeNormalMapFromMesh, BakeAmbientOcclusion at 1-2K | albedo/MR/normal/AO |
| 6 QA | headless Blender: scale to `scale_m`, origin at feet, +Y up/-Z forward, tri budget, turntable PNG | `model.glb` + `thumb.png` |
| 7 Rig | SkinTokens (fallback UniRig); Rigify landmark-fit for biped/quadruped/bird/shark | skeleton + weights |
| 8 Animate | UniMate text-to-motion; MoCapAnything from Wan 2.2 reference video; procedural IK for serpentine/insectoid/amorphous/floating | clips from `animation_set` |
| 9 Export | Blender bake + glTF (separate) + FBX | Godot-ready |

Stages 1-6 run through the existing ComfyUI (core nodes only, no custom 3D packs).
Fork template `3d_pixal3d_trellis2_image_to_model.json`; drive it over the 8188 API.
Stages 7-8 run in their own `uv` venvs (Blackwell needs CUDA 12.8+ wheels; no nvcc on
PATH, so avoid anything that compiles flash-attn/pytorch3d).

Throughput estimate: 2-3 min per monster for stages 1-6, so about 11-17 h for the SRD
before re-rolls; rigging/animation unmeasured.

## Per-body-plan animation strategy

| Body plan | Rig | Clips |
|---|---|---|
| biped, winged_biped, centauroid, merfolk_naga | SkinTokens or Rigify human | free humanoid libraries (Mixamo/Quaternius CC0) retargeted; UniMate |
| quadruped, winged_quadruped | SkinTokens or Rigify wolf/horse/cat | UniMate; MoCapAnything from animal video; wing chains added for dragons |
| avian | SkinTokens or Rigify bird | UniMate flap/hover; hover idle in combat |
| serpentine, aquatic | spine chain / Rigify shark | procedural sine + Godot SplineIK3D, baked |
| insectoid, tentacled, radial | SkinTokens, leg count verified | IK gait (Blender Spider Walker / Godot IK), hand-posed attacks |
| amorphous, floating, swarm, plant_object | minimal root/bob rig or shader | shader/procedural wobble, bob, scale pulses |

## Milestones

- M0 (done): registry, taxonomy, research.
- M1 Pilot (done, see `pilot-report.md`): download models (20.8 GB), run 6 monsters spanning body plans
  (goblin, wolf, owlbear, giant spider, harpy, young red dragon) through stages 1-6,
  review in Blender, tune remesh/decimate/bake settings.
- M2 Batch SRD meshes: `tools/forge.py` job runner with resume, per-monster manifest,
  contact sheet for review, re-roll list.
- M3 Rig pilot: SkinTokens venv; rig the 6 pilot monsters; Rigify fallback script.
- M4 Animate pilot: UniMate + procedural clips; Godot import test in godot-dnd.
- M5 Full SRD rig+animate; publish SRD pack.
- M6 Monster Manual 2024 and later tiers (name list from community data, no bulk downloads).

## Equipment and sockets

User rule: intelligent creatures that wield weapons must be modelled with **empty hands** and
proper wrists and palms, so weapons, shields and held items can be attached as separate props at
runtime. They are never baked into the mesh. Worn gear (armour, clothing, belts, quivers,
helmets) may stay baked in. Natural weapons (claws, bites, horns) are part of the body.

The registry carries `hands`, `wields`, `shield`, `sockets` and `unarmed_reference` per
monster. Reference images for `unarmed_reference` records are drawn with open, empty hands.
`data/registry/props.json` lists each held prop, built through the same pipeline at 500-1500
tris. Sockets are bones (`hand_r`, `hand_l`, `back`, `hip_l`, `hip_r`, `head`), and the rig
stage must provide named hand and wrist bones for every `hands: true` record. Details in
`equipment.md`.

## Review and quality gates

Every asset gets a manifest with seed, settings, tri count, and a turntable thumbnail.
A contact sheet per batch is reviewed; failures go to a re-roll list with prompt tweaks.
Never commit art outputs.
