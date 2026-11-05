# Monster-forge: rigging and animation research (2026-11-03)

Sources: GitHub READMEs and API metadata (via `gh api`), arXiv pages, vendor docs. Last-push dates are from the GitHub API. Unverified items are marked as such.

## 1. Verdict on the pasted claims

| Claim | Verdict |
|---|---|
| "UniRig etc. are fundamentally humanoid and glitch on quadrupeds" | **False.** The UniRig README says it targets "humans, animals, objects" and was trained on Articulation-XL2.0 and Rig-XL (14k Objaverse and VRoid rigs) ([repo](https://github.com/VAST-AI-Research/UniRig)). Its successor SkinTokens/TokenRig ships a *giraffe* as its CLI example ([repo](https://github.com/VAST-AI-Research/SkinTokens)). Puppeteer ships a deer example. RigAnything's demo is Spyro the dragon. One real caveat: UniRig's README warns that skinning degrades when the skeleton misses tail or wing bones. Only Make-It-Animatable is humanoid-only, because it fits a Mixamo template. |
| "Only Tripo v2.5 and Meshy 6 handle quadruped/hexapod/octopod/avian/serpentine/aquatic" | **Half true for Tripo, false for Meshy.** Tripo rig v2.5 (`v2.5-20260210`) does have 7 rig types: biped, quadruped, hexapod, octopod, avian, serpentine, aquatic. v3.0 (`v3.0-20260909`) adds "others" ([Tripo rig docs](https://developers.tripo3d.ai/en/models/rig)). Meshy's **API** rigging docs say it "works well only with standard humanoid (bipedal) characters" ([docs](https://docs.meshy.ai/en/api/rigging)). The web app adds "Quadruped Dog" (manual markers) and a "Smart Rig (beta)" that cannot use the animation library ([webapp docs](https://docs.meshy.ai/en/webapp/guides/3d-model/rigging)). Anything World also covers quadruped, bird, fish and snake categories. |
| "Text-to-motion doesn't work for creatures" | **Outdated.** UniMate (SIGGRAPH Asia 2026, MIT, code plus checkpoints released 2026-09) does text→motion on arbitrary rigged skeletons, including quadruped, avian, marine, insectoid and serpentine rigs. It has a "bring your own rig" preprocessor (2026-10-05) ([repo](https://github.com/Friedrich-M/UniMate)). AnyTop (MIT) is skeleton-conditioned and generates unconditioned or in-painted motion. TopX (arXiv 2610.34321, Oct 2026) claims text→motion for unseen skeletons, but no code is verified. The authors call UniMate "an early step … many motions and skeletons still fail." So it works, but you will need to curate the output. |
| "Animation is done via preset retargeting or video-to-video overlays" | **Incomplete.** Other routes exist: (a) text→skeletal motion (UniMate), (b) **video→skeletal mocap for arbitrary rigs** (MoCapAnything V2, MIT, outputs BVH-ready rotations; Puppeteer's video-guided rig optimisation), (c) text→vertex animation (AnimateAnyMesh, FBX/ABC export), (d) procedural IK (Godot 4.6 IK modifiers, Blender Spider Walker). Tripo's non-biped presets are thin: 16 "universal" presets, with ids like `preset:quadruped:walk` and `preset:serpentine:march` ([animation docs](https://developers.tripo3d.ai/en/models/animation)). |

## 2. Tool table

| Tool | What | Body plans | Output | License | VRAM | Last push |
|---|---|---|---|---|---|---|
| [SkinTokens/TokenRig](https://github.com/VAST-AI-Research/SkinTokens) | auto-rig (UniRig successor) | arbitrary (Art-XL2.0 + VRoid + ModelsResource, GRPO-refined for OOD) | skeleton + skin weights, GLB | MIT | ≥14 GB (stated) | 2026-05-12 |
| [UniRig](https://github.com/VAST-AI-Research/UniRig) | auto-rig | humans/animals/objects | skeleton + skin, FBX/GLB | MIT | ≥8 GB | 2026-06-04 |
| [ComfyUI-UniRig](https://github.com/PozzettiAndrea/ComfyUI-UniRig) | ComfyUI wrapper | — | — | GPL-3.0 | — | 2026-08-24 |
| [Aero-Ex/ComfyUI-SkinTokens](https://github.com/Aero-Ex/ComfyUI-SkinTokens), [NoBlender variant](https://github.com/stevelittlefish/ComfyUI-SkinTokens-NoBlender), [skin-tokens.cpp](https://github.com/localai-org/skin-tokens.cpp) | wrappers / GGML port | — | — | MIT / MIT / Apache-2.0 | — | 2026-08 / 09 / 10-30 |
| [Puppeteer](https://github.com/Seed3D/Puppeteer) (ByteDance Seed) | rig + video-guided animation | arbitrary (Art-XL2.0, 59k) | skeleton, skin, FBX export, **skeletal** anim optimised from video | Apache-2.0 | n/s | 2025-09-19 |
| [MagicArticulate](https://github.com/Seed3D/MagicArticulate) | skeleton gen (Puppeteer predecessor) | arbitrary | skeleton (skin in Puppeteer) | Apache-2.0 | 4.6 GB | 2025-09-14 |
| [RigAnything](https://github.com/Isabella98Liu/RigAnything) | auto-rig | template-free, diverse | rigged GLB | **Adobe Research, non-commercial** | n/s | 2025-10-23 |
| [Anymate](https://github.com/yfde/Anymate) | dataset + baselines | 230k assets | joints, connectivity, skin | no license file | n/s | 2025-08-28 |
| [Make-It-Animatable](https://github.com/jasongzy/Make-It-Animatable) | rig | **humanoid (Mixamo template)** | rigged FBX | MIT | n/s | 2026-09-08 |
| [Articulate-AnyMesh](https://github.com/UMass-Embodied-AGI/Articulate-Anymesh) | articulated *objects* (doors, drawers) | not creatures | URDF-like joints | none | — | 2025-05-13 |
| [UniMate](https://github.com/Friedrich-M/UniMate) | **text→motion, any skeleton** | biped, quadruped, avian, marine, insectoid, serpentine, rigid objects | joint rotations → GLB/FBX | MIT (Truebones data not redistributed) | small (flan-t5-base + DiT); n/s | 2026-10-08 |
| [AnyTop](https://github.com/Anytop2025/Anytop) | skeleton-conditioned motion diffusion | Truebones Zoo (~70 species) | BVH-like | MIT | 1 GPU | 2026-04-11 |
| [MoCapAnything V2](https://github.com/animotionlab26/MocapAnything) | **video→rotations for any rig** | humans, quadrupeds, birds, reptiles, multi-leg, limbless, objects | BVH-ready rotations | MIT (code + weights) | n/s; free HF ZeroGPU demo | 2026-09-04 |
| [AnimateAnyMesh](https://github.com/JarrentWu1031/AnimateAnyMesh) | text→mesh animation | any mesh | **vertex trajectories**, FBX/ABC | Apache-2.0 | n/s | 2026-06-05 |
| [Animate3D](https://github.com/yanqinJiang/Animate3D) / [L4GM](https://github.com/nv-tlabs/L4GM-official) / [DreamMesh4D](https://github.com/WU-CVGL/DreamMesh4D) | 4D gen | any | Gaussians / per-frame deformation | Apache-2.0 | high | 2024-10 / 2025-01 / — |
| [AnimaX](https://github.com/anima-x/anima-x) | video-pose diffusion → skeletal | any articulated | joints + IK | Apache-2.0 | — | **code not released** (TODO) |
| OmniMotionGPT ([repo](https://github.com/USRC-SEA/OmniMotionGPT)), arXiv [2512.10352](https://arxiv.org/abs/2512.10352) (OmniZoo), TopX [2610.34321](https://arxiv.org/abs/2610.34321) | animal text→motion | SMAL quadrupeds / any | — | MIT / no code / no code verified | — | 2024-07 / — / — |

n/s = not stated.

## 3. Rigging options

**Local ML (primary path).** Use SkinTokens/TokenRig (14 GB, fits 16 GB if ComfyUI isn't holding VRAM) or UniRig (8 GB). `--use_skeleton` re-skins a hand-edited skeleton, which helps when tails or wings come out wrong. Output bone names are generic (`bone_0…`), so UniMate's `rig_preprocess` uses an LLM to label joints, with a human review step. Puppeteer has the best video→animation story, but it pins torch 2.1.1/cu118 and bpy 4.2.

**Blender-native (deterministic fallback).** Blender 5.2.2 on this machine ships Rigify with `metarigs/Animals/{bird,cat,horse,shark,wolf}.py` plus `Basic/basic_quadruped.py` (verified at `/usr/share/blender/5.2/scripts/addons_core/rigify/metarigs/`). No free addon auto-fits a metarig to an arbitrary mesh. Auto-Rig Pro's "Smart" is **bipeds only**; for quadrupeds it needs manual reference bones ([ARP product page](https://superhivemarket.com/products/auto-rig-pro)). **Landmark fitting is scriptable.** Because monster-forge generates the meshes, you know their canonical pose and facing. Approach: (a) voxelise the mesh or take its medial axis, (b) find extremities from the bounding box and convex-hull extreme points (feet are the lowest clusters, the head is the +Y extreme, the tail is the −Y extreme), or render orthographic views and use a 2D keypoint model, (c) map those landmarks to metarig bone heads and tails, scaling chains proportionally, (d) `rigify.generate`, then parent with automatic weights. This is cheapest for quadrupeds, birds and sharks, where the metarig matches the body plan. Another option is to snap UniRig output joints onto Rigify bones, using Rigify only for its controls.

## 4. Animation sources

- **a. Mocap libraries.** Truebones Zoo (75+ animals, FBX/BVH) is royalty-free including commercial games, but redistribution of the FBX/BVH is prohibited and credit is requested ([Gumroad](https://truebones.gumroad.com/l/skZMC)). Its "free code" promo reportedly ended Dec 2025, so it is now low-cost or pay-what-you-want. This is the best real animal motion source, and UniMate, AnyTop and MoCapAnything all train on it. CMU has no animals. UniML3D (UniMate) and DyMesh (AnimateAnyMesh) are on Hugging Face; check per-source licenses. Objaverse-derived data has mixed CC licenses.
- **b. Text→motion.** **UniMate is usable today** for a walk/idle/attack/fall set on your own rigged GLB. It runs at 60 frames and is real-time. Generate 3 seeds per prompt and pick one. Prompts take the form "An object walks forward." AnyTop needs BVH examples of the skeleton, so it suits motion in-painting more than prompting. Everything else is paper-only.
- **c. Video→animation.** MoCapAnything V2 takes video plus your rig and outputs **skeletal BVH rotations**, which are Godot-usable. Pair it with a generated or reference video (e.g., Wan 2.2 locally). Puppeteer is also skeletal. AnimateAnyMesh exports vertex animation; FBX/ABC point caches import poorly to Godot as blendshapes, so it is only OK for oozes or blobs. Animate3D, L4GM and DreamMesh4D output Gaussians or deformation fields and are not game-usable.
- **d. Procedural.** Godot 4.6 restored IK: `TwoBoneIK3D`, `ChainIK3D`, `SplineIK3D`, `FABRIK3D`, `CCDIK3D` and others on `SkeletonModifier3D` ([Godot blog](https://godotengine.org/article/inverse-kinematics-returns-to-godot-4-6/)). Combine with a gait phase script for spiders and centipedes, and `SplineIK3D` for snakes and tentacles. In Blender 5.2, Spider Walker is free and procedural, and bakes to an Action ([80.lv](https://80.lv/articles/free-blender-tool-for-procedural-spider-walks)).
- **e. Retargeting.** Godot's retargeting is built around `SkeletonProfileHumanoid`. A custom `SkeletonProfile` is possible but awkward, so bake clips onto each monster's own skeleton in Blender and skip runtime retargeting. Blender options: Keemap (GPL-3.0, last push 2023-02, works with any rig via bone mapping), a Rokoko addon fork for Blender 5.x ([abaqDev](https://github.com/abaqDev/Rokoko-Blender-Addon), LGPL), and ARP Remap (paid). For cross-species AI transfer, MoCapAnything's retarget and UniMate's zero-shot cross-topology transfer beat older SAME/R2ET-style work, which is humanoid-centric.

## 5. Cloud fallback pricing (per asset, rig + 5 clips)

| Service | Rig | Anim | Per asset | 330 | 2000 | Notes |
|---|---|---|---|---|---|---|
| Tripo API ($0.01/credit) | 25 cr | 10 cr/clip | **$0.75** (+$0.30 to $0.40 if Tripo also generates the mesh) | ~$250 | ~$1,500 | 7 creature types; only ~16 non-biped presets ([pricing](https://developers.tripo3d.ai/en/pricing)) |
| Meshy API (~$0.02/credit, third-party estimate) | 5 cr | 3 cr/action | ~$0.40 | ~$130 | ~$800 | API rig is **humanoid only**; quadruped via web app ([pricing](https://docs.meshy.ai/en/api/pricing)) |
| Anything World | 5 cr/model | incl. | ~$1.25 (2023 pricing; reverify) | ~$410 | ~$2,500 | quadruped, insect, bird, fish, snake categories ([FAQ](https://anything-world.gitbook.io/quickstart/animate-anything-quickstart/faq)) |
| Hyper3D Rodin | no confirmed auto-rig API | — | — | — | — | T/A-pose generation only |
| Mixamo | free | free | $0 | — | — | humanoid only |

## 6. Per-body-plan strategy (turn-based RPG minimum set)

Core set for every plan: **idle, move, attack1, (attack2/cast), hit, death**. Turn-based play tolerates in-place moves with root motion stripped.

| Body plan | Rig | Animation | Notes |
|---|---|---|---|
| Biped (humanoid, giants, goblins) | UniRig/SkinTokens, or Rigify human fit | Retarget free humanoid libraries (Mixamo download is free; Quaternius CC0 sets) in Blender | Solved problem |
| Quadruped (wolves, owlbear, dragons on ground) | SkinTokens, or Rigify wolf/horse/cat by landmark fit | UniMate text→motion plus Truebones clips retargeted via Keemap; MoCapAnything from dog/horse video | Dragons get extra wing chains |
| Avian / flying (griffon, wyvern, bats) | SkinTokens; Rigify bird | UniMate ("flies/flaps"), Truebones birds; idle hover loop | Treat flying monsters as hovering in combat |
| Serpentine (nagas, purple worm) | Rigify spine chain or SkinTokens | Godot `SplineIK3D` / sine-driven procedural, baked; UniMate serpentine | Procedural is most reliable |
| Aquatic (sharks, kraken) | Rigify shark; tentacles as chains | Procedural sine swim; Truebones fish; UniMate marine | |
| Insectoid / multi-limb (ankheg, spiders, 8-limb) | SkinTokens; verify leg count | Spider Walker (Blender) or Godot IK gait, baked; attack from UniMate | Gait from IK; hand-key attack poses |
| Amorphous / ooze | 3 to 5 bone blob, or no skeleton | Shape keys or AnimateAnyMesh vertex anim → bake to blendshapes; or shader wobble | Simplest done in shader |
| Floating / no-limb (eye tyrant, will-o-wisp) | Root plus few bones (eyestalks as chains) | Procedural bob/rotate/scale; eyestalks via `ChainIK3D` noise | No ML needed |
| Swarm | No per-creature rig | Godot particles or MultiMesh of a 1 to 2 bone critter | Death = particles disperse |
| Plant / object-like (shambling mound, mimic) | SkinTokens (handles objects); or 2 to 4 bones | UniMate "object" prompts; procedural sway | |

## 7. Install pitfalls on this box

- **Don't install these into ComfyUI's env** (Py3.12, torch 2.14 cu130). Use one `uv` venv per tool, since each pins a different torch.
- **RTX 5070 Ti is Blackwell (sm_120)**, which needs CUDA ≥12.8 builds. Puppeteer (torch 2.1.1/cu118) and AnyTop (Python 3.8) **will not run as pinned**. Bump to torch ≥2.7 cu128 and expect breakage in pytorch3d/flash-attn.
- **No `nvcc` on PATH**, so flash-attn, pytorch3d and torch-scatter cannot compile from source. Use prebuilt wheels matched to torch/CUDA/Python, or patch to SDPA. UniRig's README warns that flash_attn install errors are likely.
- **`bpy` pip versions are tied to Python** (bpy 4.2 needs Py3.11). Prefer running the system Blender 5.2.2 headless (`blender -b --python`) for import/export steps.
- Fedora's Blender prints an OCIO 2.5 vs 2.4.2 config error and falls back. This is harmless for rigging but fix it before rendering previews.
- SkinTokens needs about 14 GB, so free the GPU from ComfyUI/Wan first.
- UniMate's full UniML3D is 88 GB. You only need the checkpoint (`hf download Linzhan/UniMate --include ...`) plus `rig_preprocess` for your own rigs. Truebones meshes are not included.
- Godot: export GLB with each clip as a separate NLA action. Avoid vertex caches.

## 8. Recommendation

Local first: **SkinTokens (or UniRig) → LLM joint labelling → UniMate text→motion and MoCapAnything video→motion → Blender clean-up/bake → GLB**. Use procedural IK for serpents, multi-leg creatures, floaters and swarms. Keep a Rigify landmark-fit script as the deterministic fallback for the four common plans. Reserve Tripo (~$0.75/asset, ~$250 for 330 monsters) for assets where the local rig fails a sanity check.
