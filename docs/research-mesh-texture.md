# monster-forge: local mesh + texture pipeline research (2026-11-03)

## TL;DR

**The local ComfyUI install already has everything needed.** At ComfyUI head (Sep 16 2026), the core `comfy_extras` include native TRELLIS.2 and Pixal3D nodes (`nodes_trellis2.py`), Hunyuan3D 2.0/2.1 shape nodes, and a full mesh post-process suite (`nodes_mesh_postprocess.py`): RemeshMesh, DecimateMesh (QEM), UnwrapMesh, BakeTextureFromVoxel (PBR), BakeNormalMapFromMesh, BakeAmbientOcclusion, ApplyTextureToMesh and Save3D. All of it is pure PyTorch, with **no flash-attn, spconv, kaolin, nvdiffrast, cumesh or flexgemm builds**. That sidesteps every Blackwell / torch 2.14 / cu130 compile problem. The shipped template `3d_pixal3d_trellis2_image_to_model.json` (dated 2026-08-25, in `.venv/.../comfyui_workflow_templates_json/templates/`) is already an image → textured, UV'd, baked GLB pipeline. The plan is to fork it, not to write a new one.

## Recommended primary stack

1. **Reference image:** Z-Image-Turbo int8 (Apache-2.0, ComfyUI template `image_z_image_turbo_int8`, about 12 GB download) or FLUX.2-klein-4B (Apache-2.0). Use juggernautXL_v9 plus controlnet-union promax (openpose/depth) only when we need pose control. Generate 2–4 candidates per monster and pick one automatically (CLIP/VLM score plus a mask-quality check).
2. **Background removal:** core `RemoveBackground` with BiRefNet (MIT). Then `ImageCropToMask`.
3. **Shape + PBR:** **Pixal3D** (MIT code and weights, TencentARC, SIGGRAPH 2026, built on TRELLIS.2) or **TRELLIS.2** (MIT), both int8 "convrot" from Comfy-Org. They share the DINOv3-L encoder and the TRELLIS.2 shape/texture VAEs. Pixal3D adds MoGe-2 FOV estimation for pixel-aligned geometry, and it has a **multi-view variant** (`Pixal3DMultiViewConditioning`: front/left/back/right). Run at 512 base. Add the 1024 upsample only for "hero" monsters, because 1536 spills past 16 GB.
4. **Low-poly:** core `RemeshMesh` (udf, res 256–384, smooth_iters 2), then `DecimateMesh` (2k–8k by CR/size), then `MeshSmoothNormals`, then `UnwrapMesh` (pec, 1024–2048).
5. **Bake:** `BakeTextureFromVoxel` (base color plus metallic/roughness, 1–2K), `BakeNormalMapFromMesh` (high-poly is the pre-decimation mesh) and `BakeAmbientOcclusion`. Then `ApplyTextureToMesh` and save the GLB.
6. **Blender 5.2 headless QA pass (bpy script):** import the GLB, apply scale, put the origin at the feet, face forward, tris→quads (`tris_convert_to_quads`), check the tri budget and non-manifold count, render a turntable thumbnail, and export glTF Separate + FBX.

**Why this stack:** it is fully MIT/Apache (DINOv3 caveat below), all in core ComfyUI, scriptable over the 8188 API, and verified on a 5070 Ti 16 GB by a third party (digitalgarden.si, Oct 19 2026: 512 shape, remesh 256, decimate to 19.7k tris, 1024 atlas).

## Fallback stack

- **Shape:** Hunyuan3D 2.1 via core nodes (`3d_hunyuan3d-v2.1.json`, single 4.9 GB checkpoint, about 10 GB VRAM). It is strong on organic forms, but core gives **geometry only**. Its license is the Tencent Community License: **not valid in EU/UK/South Korea**, and above 1M MAU you must ask Tencent.
- **Texture for non-TRELLIS meshes:** MV-Adapter SDXL multiview (Apache-2.0; `huanngzh/ComfyUI-MVAdapter`, stale since 2025-06), projected and baked in Blender. Alternatives are Hunyuan3D-2.1 Paint via `visualbruno/ComfyUI-Hunyuan3d-2-1` (it needs compiled custom_rasterizer / mesh_painter and is nominally 21 GB VRAM, so 16 GB is risky) or the official TRELLIS.2 `app_texturing.py`, which textures a supplied mesh but needs the full CUDA-extension build.
- **Retopo:** Blender QuadriFlow (`bpy.ops.object.quadriflow_remesh(target_faces=N)`) or Instant Meshes (CLI, BSD-style, unmaintained since 2022) for quad-dominant output, then a Cycles "selected-to-active" bake of normal and base color.

## Tool table (licenses and last push verified via GitHub/HF API, 2026-11-03)

| Tool | Output | License | VRAM (16 GB fit) | ComfyUI | Last push |
|---|---|---|---|---|---|
| [TRELLIS.2](https://github.com/microsoft/TRELLIS.2) | O-Voxel mesh + PBR (base/rough/metal/alpha) | MIT (+DINOv3) | Official repo says 24 GB. On a 5070 Ti, peak allocated was 2.6 GB at 512, 5.2 GB at 1024 and 10 GB at 1536 (1536 spills to shared memory) | **Core** (int8) | 2026-07-10 |
| [Pixal3D](https://github.com/TencentARC/Pixal3D) | mesh + PBR, pixel-aligned, multi-view | MIT (GH + HF) | Same as TRELLIS.2; int8 is about 8 GB | **Core** (single + MV) | 2026-09-01 |
| [Hunyuan3D-2.1](https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1) | mesh; Paint = PBR | Tencent Community (geo-restricted) | 10 GB shape / 21 GB paint | Core (shape); [visualbruno wrapper](https://github.com/visualbruno/ComfyUI-Hunyuan3d-2-1) (2026-09-08) | 2025-10-17 |
| Hunyuan3D 2.5 / 3.0 / 3.1 | — | **API only, no open weights** | — | API nodes only | — |
| [TripoSG](https://github.com/VAST-AI-Research/TripoSG) | SDF→mesh, no texture | MIT | ~8 GB | community only | 2025-04-18 |
| [Step1X-3D](https://github.com/stepfun-ai/Step1X-3D) | mesh + SDXL-MV texture | Apache-2.0 | geo ~10 GB, tex ~16+ GB | none maintained | 2025-09-08 |
| [Direct3D-S2](https://github.com/DreamTechAI/Direct3D-S2) | sparse SDF mesh, no texture | MIT | ~10 GB at 512, more at 1024 | community | 2025-09-26 |
| [Hi3DGen / Stable3DGen](https://github.com/Stable-X/Stable3DGen) | normal-bridged mesh, no texture | MIT | ~12 GB | community | 2025-07-02 |
| [UltraShape-1.0](https://github.com/PKU-YuanGroup/UltraShape-1.0) | geometry refiner | other | — | [jtydhr88 wrapper](https://github.com/jtydhr88/ComfyUI-UltraShape1) | 2026-01-06 |
| [Sparc3D](https://github.com/lizhihao6/Sparc3D) | — | **no code/weights** (README + demo only) | — | — | 2025-06-16 |
| [PartCrafter](https://github.com/wgsxm/PartCrafter) | part meshes, untextured | MIT | ~12 GB | no | 2026-04-16 |
| [PartPacker](https://github.com/NVlabs/PartPacker) | dual-volume parts | NVIDIA (non-commercial) | ~10 GB | no | 2025-06-26 |
| [OmniPart](https://github.com/HKU-MMLab/OmniPart) | TRELLIS-based parts + texture | MIT | ~16 GB | [smthemex](https://github.com/smthemex/ComfyUI_OmniPart) (stale) | 2025-11-06 |
| [Hunyuan3D-Part](https://github.com/Tencent-Hunyuan/Hunyuan3D-Part) | P3-SAM segmentation + X-Part | Tencent (geo-restricted) | — | API template only | 2025-12-05 |
| [TripoSplat](https://huggingface.co/VAST-AI/TripoSplat) | Gaussians (not useful) | MIT | — | Core | HF 2026-08-03 |
| [MV-Adapter](https://github.com/huanngzh/MV-Adapter) | SDXL multiview / texture | Apache-2.0 | ~14 GB | [ComfyUI-MVAdapter](https://github.com/huanngzh/ComfyUI-MVAdapter) | 2025-06-26 |
| [UniTEX](https://github.com/YixunLiang/UniTEX) | texture fn, inpaint | Apache-2.0 | >16 GB (FLUX-based) | no | 2026-03-19 |
| [DeepMesh](https://github.com/zhaorw02/DeepMesh) | AR artist mesh | Apache-2.0 | ~10 GB, minutes/mesh | no | 2025-12-17 |
| [MeshAnythingV2](https://github.com/buaacyw/MeshAnythingV2) | AR mesh ≤1600 faces | S-Lab (non-commercial) | 8 GB, ~45 s | 3D-Pack (stale) | 2025-04-28 |
| [BPT](https://github.com/Tencent-Hunyuan/bpt) | AR mesh ≤8k faces | Tencent | ~10 GB | no | 2025-07-30 |
| [TreeMeshGPT](https://github.com/sail-sg/TreeMeshGPT) | AR mesh | MIT | — | no | 2025-05-22 |
| [FastMesh](https://github.com/jhkim0759/FastMesh) | AR mesh | other | — | no | 2025-11-11 |
| [kijai/Hunyuan3DWrapper](https://github.com/kijai/ComfyUI-Hunyuan3DWrapper) | 2.0 shape+paint | Tencent | needs custom_rasterizer build | — | 2026-03-16 |
| [ComfyUI-3D-Pack](https://github.com/MrForExample/ComfyUI-3D-Pack) | many | MIT | pins old torch/CUDA wheels | **avoid** | 2025-12-29 |
| [PozzettiAndrea/ComfyUI-TRELLIS2](https://github.com/PozzettiAndrea/ComfyUI-TRELLIS2), [visualbruno/ComfyUI-Trellis2](https://github.com/visualbruno/ComfyUI-Trellis2) | wrappers | MIT | need cumesh/flexgemm/o-voxel builds | superseded by core | 2026-08-24 / 2026-09-25 |
| [UniRig](https://github.com/VAST-AI-Research/UniRig), [SkinTokens](https://github.com/VAST-AI-Research/SkinTokens) | auto-rig (later phase) | MIT | — | [ComfyUI-UniRig](https://github.com/PozzettiAndrea/ComfyUI-UniRig) (GPL) | 2026-06 / 2026-05 |

The VRAM figures for TripoSG, Step1X, D3D-S2, Hi3DGen, PartCrafter and PartPacker come from their READMEs or memory and were not measured here. TRELLIS.2 numbers are from [dev.to, 5070 Ti, 24 runs](https://dev.to/alicia_lu/trellis2-on-a-16gb-rtx-5070-ti-24-runs-glb-export-and-shared-memory-1ggk).

**Quality for organic creatures (reputation):** the 2026 consensus puts TRELLIS.2 and Pixal3D at the top of the open models for detail and PBR. Pixal3D adheres better to the input image, while TRELLIS.2 can drift in colour on flat illustrations, which matters for painterly references. Hunyuan3D 2.1 has smooth, clean organic geometry. TripoSG and Direct3D-S2 have good geometry but no texture.

## Texturing (Q2)

The texture comes straight from the generator's voxel PBR field, so there is **no multiview projection seams problem**. BakeTextureFromVoxel bakes base color plus metallic/roughness onto the low-poly UVs at 1–2K, and BakeNormalMapFromMesh recovers high-poly detail. This is the cleanest game-ready output available locally.

Hunyuan3D-2.1 Paint also gives true PBR but needs about 21 GB and has the license limits. MV-Adapter, TEXTure and Paint3D-style projection produce baked-in lighting and seams (albedo only, no roughness). UniTEX does not fit in 16 GB. Known weak spots: auto-UV produces many charts (hundreds), so small details soften. Use 2048 bakes for large creatures and 1024 for small ones, and delight with a "flat lighting" reference prompt.

## Low-poly / retopo (Q3)

**Learned retopology is not production-ready.** MeshAnythingV2 is non-commercial and limited to 1.6k faces. DeepMesh, BPT, TreeMeshGPT and FastMesh are slow and fail on complex organic shapes (fused limbs, wings). QuadGPT, QuadLink and TopGen (2026 papers) have no released weights found. **Use deterministic QEM:** core Remesh (watertight, removes inner shells) followed by Decimate. It gives triangle meshes that are fine for engines, and the normal bake carries the detail. If quad-ish topology matters for rigging or deformation, run Blender QuadriFlow on the remeshed mesh at the target count and bake in Cycles. Quad Remesher (paid) is better still but is not automatable for free. Budget by size: Tiny/Small 2–3k, Medium 4k, Large 6k, Huge/Gargantuan 8k tris.

**Part-aware generation:** PartCrafter, OmniPart and PartPacker produce separable parts but are untextured or low quality, and PartPacker is non-commercial. Skip them for v1. For separable limbs, either prompt A-pose with limbs clear of the body, or segment later. For animation, use UniRig or SkinTokens (MIT) on the low-poly.

## Reference images (Q4)

- Prompt template: `full body, single creature, neutral A-pose (limbs apart, wings spread), three-quarter front view, orthographic, flat even lighting, no shadows, plain light grey background, game asset concept, whole body in frame`. Negatives: scenery, multiple creatures, cropped, text, dramatic lighting.
- **Z-Image-Turbo int8:** 8 steps, roughly 5–8 s at 1024² (estimate). It is the best photoreal/stylized fit for 16 GB.
- **FLUX.2-klein-4B** (Apache) is also an *editor*. Use it to make back/side views from the chosen front image, then feed those into Pixal3D multi-view. This gives better hidden-side geometry (backs of dragons, tails).
- Qwen-Image 2512 and Qwen-Edit 2511 are better at prompt adherence but heavier (31 GB+ downloads, int8 edit, offload). HiDream-O1-dev and Krea-2 also exist. FLUX.2-klein-9B is **non-commercial**.
- juggernautXL + controlnet-union (openpose for humanoids, depth for quadrupeds) is a solid zero-download fallback. IPAdapter (installed) keeps a consistent style across the 2000-monster set, but pony/animagine's licenses and anime bias make them poor defaults.

## Throughput (Q5)

These are estimates for the 5070 Ti and should be benchmarked in the first sprint.

| Stage | s/asset |
|---|---|
| 3 reference candidates (Z-Image-Turbo) + BiRefNet | 20–30 |
| Optional 3 extra views (klein-4B edit) | 15–25 |
| Pixal3D/TRELLIS.2 512 (structure + shape + texture) | 25–55 |
| (+1024 upsample, hero only) | +30–60 |
| Remesh 256–384 + decimate + unwrap | 15–40 |
| Bakes 2K (PBR, normal, AO) + save | 20–40 |
| Blender QA/export | 10–20 |
| **Total (typical)** | **~2–3 min** |

At that rate: **330 SRD monsters ≈ 11–17 h**, **500 (MM 2024) ≈ 17–25 h**, **2000+ ≈ 3–4.5 days** of unattended GPU time. Add 30–50% for re-rolls after QA.

## Pipelines worth forking (Q6)

- **ComfyUI template `3d_pixal3d_trellis2_image_to_model`** (core): the best base. Drive it over the API from monster-forge.
- **[AssetFurnace / Bingeljell/image-to-3dlab](https://github.com/Bingeljell/image-to-3dlab)** (Apache-2.0, pushed 2026-11-03, ~1k stars): FLUX.2-klein → BiRefNet → Pixal3D/TRELLIS.2/Hunyuan → "Finish" retopo (~5k faces) → ~40 Blender helper scripts (bake, LOD, rig via SkinTokens, 21 animations) → GLB, with a provenance sidecar. It does not use ComfyUI, but its Blender scripts and license-provenance idea are worth borrowing. It was tested on 24 GB cards.
- **[Modly](https://github.com/lightningpixel/modly)** (desktop app, TRELLIS.2 GGUF extension): reference only.

## Install steps / pitfalls for this environment

1. Download into `~/workspace/ComfyUI/models/` (about 15 GB):
   - `diffusion_models/trellis_2_int8_convrot.safetensors` (HF Comfy-Org/TRELLIS.2)
   - `diffusion_models/pixal3d_int8_convrot.safetensors` and `pixal3d_multiview_int8_convrot.safetensors` (Comfy-Org/Pixal3D)
   - `vae/trellis_2_shape_vae_bf16.safetensors` and `vae/trellis_2_texture_vae_bf16.safetensors` (Comfy-Org/Pixal3D)
   - `clip_vision/dino_v3_L_naf_fp32.safetensors`
   - `geometry_estimation/moge_2_vitl_normal_fp16.safetensors` (Comfy-Org/MoGe)
   - `background_removal/birefnet.safetensors` (Comfy-Org/BiRefNet)
   - Optional: `checkpoints/hunyuan_3d_v2.1.safetensors` and the Z-Image-Turbo int8 template models.
2. Load the template in the UI, run it once, then "Export (API)" into `workflows/`.
3. API pitfall: DynamicCombo inputs (Remesh `sign_mode`, Decimate `placement_mode`) must be sent as flat, prefixed keys, not nested objects. A third party hit this exact failure.
4. **Do not install** ComfyUI-3D-Pack, kijai Hunyuan3DWrapper or the TRELLIS2 wrappers into this venv. They need flash-attn, spconv, kaolin, nvdiffrast, cumesh or custom_rasterizer builds, and there are no wheels for torch 2.14+cu130/py3.12; a failed install can break core. If one is ever needed, use a separate venv or ComfyUI instance.
5. RAM: 30 GB system RAM. Avoid remesh resolution ≥768 and the 700k decimate (template defaults) for batch runs. Use remesh 256–384 and pass the remeshed mesh as the normal-bake high-poly.
6. Stay at 512 (optionally 1024) resolution. 1536 exceeds 16 GB and was observed to hit `CUDA device not ready` at export.
7. Licensing: TRELLIS.2/Pixal3D are MIT, but the DINOv3 encoder is under Meta's DINOv3 License, which AssetFurnace classifies as "commercial-conditional". Review it before selling assets. Avoid Hunyuan weights if anyone involved is in the EU/UK/KR.
