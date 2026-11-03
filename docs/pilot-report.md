# M1 pilot report (2026-11-03)

Stages 1-6 (reference image -> matte -> Pixal3D -> low-poly -> bake -> Blender QA) ran
end to end on all 6 pilot monsters with core ComfyUI nodes only. Nothing failed and there
were no VRAM or RAM errors. Review sheet: `out/srd-5.2.1/_contact_pilot.png` (git-ignored).

## What was built

| file | role |
|---|---|
| `workflows/ref_image.api.json` | juggernautXL_v9 txt2img, 1024², dpmpp_2m/karras, 30 steps, CFG 5.5, clutter/multi-subject/text/NSFW negatives |
| `workflows/image_to_lowpoly.api.json` | API fork of `3d_pixal3d_trellis2_image_to_model`: BiRefNet -> ImageCropToMask(1.1) -> MoGe-2 FoV -> Pixal3D int8 at 512 -> RemeshMesh -> DecimateMesh(qem) -> MeshSmoothNormals -> UnwrapMesh(pec) -> BakeTextureFromVoxel + BakeNormalMapFromMesh + BakeAmbientOcclusion -> ApplyTextureToMesh -> SaveGLB, plus SaveImage per map and PreviewAny face counts |
| `tools/forge.py` | stdlib runner: `ref`, `mesh`, `qa`, `run`, `contact`; resumable, per-monster `manifest.json` |
| `tools/blender_qa.py` | headless Blender: join, orient, scale, origin at feet, stats, glTF export, 4-view EEVEE strip |
| `tools/contact_sheet.py` | labelled grid of thumbnails (Pillow; runs in the ComfyUI venv) |
| `data/prompt_tweaks.json` | per-monster prompt overrides for re-rolls (owlbear so far) |

Downloaded into `~/workspace/ComfyUI/models/` (20.8 GB in total, all from Comfy-Org on HF; every name matches the template and the loader combos):

| file | size |
|---|---|
| `diffusion_models/pixal3d_int8_convrot.safetensors` | 5.58 GB |
| `diffusion_models/pixal3d_multiview_int8_convrot.safetensors` | 5.58 GB |
| `diffusion_models/trellis_2_int8_convrot.safetensors` | 5.25 GB |
| `vae/trellis_2_shape_vae_bf16.safetensors` | 1.10 GB |
| `vae/trellis_2_texture_vae_bf16.safetensors` | 0.95 GB |
| `clip_vision/dino_v3_L_naf_fp32.safetensors` | 1.22 GB |
| `geometry_estimation/moge_2_vitl_normal_fp16.safetensors` | 0.66 GB |
| `background_removal/birefnet.safetensors` | 0.44 GB |

## Results

Wall times are seconds. `ref` covers 3 candidates (1 for the goblin), `mesh` is the full
ComfyUI 3D graph, and `qa` is Blender. Times include model swaps, because each monster
alternates SDXL and Pixal3D.

| monster | plan / size | ref | mesh | qa | hi-poly faces | remesh faces | target | tris | dims m (W, L, H) | facing |
|---|---|---|---|---|---|---|---|---|---|---|
| goblin-warrior | biped / small | 12 | 34 | 1.8 | 1.54M | 645K | 2000 | 1732 | 0.91, 0.47, 1.00 | front view |
| wolf | quadruped / medium | 22 | 24 | 1.7 | 944K | 381K | 3000 | 3010 | 0.50, 1.50, 0.90 | PCA, auto |
| owlbear | quadruped / large | 24 | 30 | 1.7 | 2.79M | 1.06M | 5000 | 4931 | 2.32, 2.80, 2.40 | ambiguous -> camera |
| giant-spider | insectoid / large | 18 | 22 | 1.7 | 1.64M | 360K | 5000 | 4992 | 4.77, 3.00, 3.61 | ambiguous -> camera |
| harpy | winged_biped / medium | 18 | 22 | 1.8 | 1.08M | 345K | 3000 | 2965 | 2.14, 0.65, 1.70 | front view |
| young-red-dragon | winged_quadruped / large | 18 | 22 | 1.7 | 1.00M | 383K | 5000 | 4988 | 5.05, 6.30, 5.02 | PCA, auto |

**Throughput: about 45-60 s per monster for stages 1-6**, against an estimate of 2-3 min.
That puts the 330 SRD meshes at about 5 h before re-rolls. A single SDXL image takes about 6 s
once the model is loaded, and Pixal3D 512 plus post-processing plus 1K bakes takes 22-34 s. The
hero variant (Trellis2UpsampleStage cascade to 1024, 2K textures, 7.7k tris) took 44 s on the
dragon and gave a 4.95M-face hi-poly mesh. ComfyUI's resident RAM was then 23 of 30 GB: it fits,
but there is little headroom.

All exported GLBs have baseColor, metallicRoughness, normal and occlusion textures at 1024²,
+Y up, the model facing -Z, feet at y=0, centred on X/Z, and are sized so that height
(upright plans) or nose-to-tail length (others) equals `scale_m`. Every mesh is one
connected part after welding UV seams. There are 5-110 non-manifold edges from QEM
decimation; this is harmless for rendering, but worth a cleanup pass before rigging.

## Visual quality by body plan

- **biped (goblin warrior):** poor at the 2000 budget. Big triangles cut across the face,
  armour and weapons, and the baked albedo smears (green skin patches on the leather). At
  4.5k tris it is good, with a readable face, ears, pauldrons, both weapons and the skirt. The
  SDXL reference also looks Yoda-like and is not in an A-pose (juggernaut ignores "A-pose"). The
  thin weapons survive decimation as blades, which is fine for a game asset.
- **quadruped (wolf):** good at 3k. Clean silhouette, legs separated, tail intact. The
  "low-poly game asset" phrase makes SDXL paint faceted low-poly shading, and that gets baked
  into the albedo. It is acceptable for this look, but the phrase should be dropped if a smooth
  painted look is wanted.
- **quadruped hybrid (owlbear):** the mesh is good, and the reference was the hard part.
  The registry prompt gave a plain giant owl 3 times out of 3. A bear-weighted tweak gave
  plain bears, and an owl-weighted one gave owl heads only. The third tweak
  (`data/prompt_tweaks.json`) gave a credible four-legged owl-headed beast, but grey and yellow
  instead of brown. **Hybrids need a stronger prompt-following model** (Z-Image-Turbo or
  FLUX.2-klein, per the research doc) or per-monster tweaks.
- **insectoid (giant spider):** good. All 8 legs come through as separate, thin, unbroken
  limbs at 5k tris. SDXL drew a front view although the prompt asked for a side view. The
  hidden back side is plausible but generic.
- **winged_biped (harpy):** good. Wings, horns and boots are clean at 3k tris. Of the 3
  candidates, one was a vulture and one was near-nude despite the prompt. NSFW negatives
  were added afterwards, so the harpy needs manual review in batch runs.
- **winged_quadruped (young red dragon):** very good, the best of the set. The membranes are
  thin and intact, the tail is curled with a spade, and the horns are distinct. Note that
  `scale_m` is the nose-to-tail length (6.3 m), so the raised wings make the model 5 m tall.

The front view of each strip is the first tile. Facing checks out for all six.

## Settings tuned (and why)

| setting | template | pilot | reason |
|---|---|---|---|
| shape pipeline | 512 -> 1536 cascade | 512 only (`--upsample 1024` for heroes) | 1536 spills past 16 GB; 512 is already detailed enough for a 2-8k tri budget |
| RemeshMesh resolution | 768 | 256 | RAM; 256 still yields 345K-1.06M faces. 320 gave 1.04M on the goblin and no visible gain after decimation |
| RemeshMesh smooth_iters | 20 | 2 | as in the research doc; the decimation hides the DC staircase |
| drop_inverted / drop_enclosed | false | true | Pixal3D output had no inner shell (1.05M vs 1.04M), so this is cheap insurance only |
| DecimateMesh | 700k, midpoint | size table, qem | qem and midpoint gave the same count; qem is kept for placement quality |
| decimate retarget | — | up to 3 re-runs when the result is outside 85-105% of target | the GPU parallel collapse overshoots non-linearly (2000 -> 1169, 3500 -> 3042 on the same mesh); the re-run is cheap because ComfyUI caches upstream nodes |
| bake size / UV res | 4096 / 4096 | 1024 / 1024 | per brief; 2048 only for heroes or huge+ |
| AO max_distance | 0.71 | 0.15 | 0.71 of the bbox diagonal darkened everything (mean AO 90/255) |
| BakeTextureFromVoxel reference_mesh | raw shape mesh | raw shape mesh | tested none and the remesh output: no visible difference |
| thumbnails | — | EEVEE (sun + grey world), 4 x 512² | Workbench ignores normal/MR maps and looked muddy |

Facing heuristic (QA): upright and floating plans face the camera (front-view prompts). For
other plans the long axis is the PCA axis of the footprint, and the head is the end with
more surface area in the top 40% of the height. If the two ends are within 1.25x of each
other, the model is assumed to face the camera. This was right for all 6, but on two of them
(owlbear and spider) only because those references happened to be front or three-quarter views.
`forge.py qa <id> --yaw <deg>` overrides it and is persisted in the manifest.

## Recommended defaults for M2

1. **Done (see `docs/lowpoly-density.md`): higher budgets and importance-weighted
   decimation.** Budgets are now tiny 3000, small 5000, medium 7000, large 9000,
   huge 12000 and gargantuan 15000. Medium and up get 2K textures, and the normal cage is
   0.015. Blender decimation weighted by curvature, a face prior and an extremity prior
   replaces the uniform GPU DecimateMesh (`--decimator comfy` keeps the old path). QA
   writes `thumb_head.png`, which the viewer shows. All 6 pilots have been regenerated
   with these defaults.
2. Keep Pixal3D 512 and remesh 256. Use `--upsample 1024` for huge/gargantuan and named
   "hero" monsters: it adds about 20 s and visibly sharpens horns and textures. The face
   texture softness left after the density fix comes from the voxel colour resolution, so
   this is the remaining lever.
3. **Replace juggernaut for references, or add pose control.** It ignores "A-pose" and "side
   view", produces Yoda-like goblins, and fails on hybrids. Download Z-Image-Turbo int8 or
   FLUX.2-klein-4B before the batch. Otherwise use controlnet-union openpose/depth with
   per-body-plan pose templates.
4. Generate 3 candidates and add an automatic pick. A CLIP or VLM score plus a BiRefNet mask
   check (one subject, not touching the border) would have rejected the vulture and nude
   harpies and the pure-owl or pure-bear owlbears. Until then, budget a human pass over a
   reference contact sheet; 6 refs per screen works well.
5. Drop "Low-poly game asset" from the reference prompt template, or make it per-style. It
   bakes faceted shading into the albedo.
6. Add a manifold cleanup step (Blender `remove_doubles` plus fill holes) before rigging (M3),
   and keep tri counts after cleanup. This matters more now: un-pinching the remesh for
   weighted decimation leaves touching sheets (3-90 loose parts and 21-375 non-manifold
   edges after the QA weld).
7. Godot note: Godot's `MODEL_FRONT` convention is +Z. Exports face -Z per the plan;
   `forge.py qa --forward +Z` flips it if the rig/animation stage prefers +Z.

## Reproduce

```sh
python3 -I tools/forge.py run goblin-warrior wolf owlbear giant-spider harpy young-red-dragon --n 3
python3 -I tools/forge.py mesh harpy --pick 2 --force && python3 -I tools/forge.py qa harpy --force
python3 -I tools/forge.py contact goblin-warrior wolf owlbear giant-spider harpy young-red-dragon --name contact_pilot
```

Pilot picks: wolf 1, owlbear 1 (third prompt tweak), giant-spider 1, harpy 2, dragon 1,
goblin 0. Seeds are crc32(id); each manifest records them.
