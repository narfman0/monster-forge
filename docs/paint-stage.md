# Multiview paint stage (2026-11-04)

`forge.py paint` re-textures LOD0's albedo. Pixal3D's voxel colour is soft and low in
detail, especially on faces, so the stage renders the finished mesh from several cameras,
has SDXL repaint each view in the house style with the depth as a control, and projects
the views back into the existing UV layout. It runs after `qa` and before `lod`. Geometry,
UVs, the normal map and every other map stay as QA wrote them; only the base colour image
changes.

Code: `tools/paint.py` (stage driver, ComfyUI graphs, review sheets),
`tools/blender_paint.py` (renders and bake, headless Blender),
`workflows/paint_view.api.json` (one view).

```sh
python3 -I tools/forge.py paint harpy --force      # then: forge.py lod harpy --force (and rig)
python3 -I tools/forge.py run harpy --stages paint,lod,rig --force
python3 -I tools/paint.py compare harpy goblin-warrior     # review sheets
```

## What it does

1. **Views** (`blender_paint.py` mode `views`). Orthographic renders of `model.glb` at
   1024 px from each view: depth (near white, far 0.15 grey, background black), camera-space
   normal, the voxel albedo seen from the camera (unlit, on white) and a coverage mask.
   They go to `paint/<view>_{depth,normal,color,mask}.png`, plus `<view>_zbuf.npy` and
   `cameras.json`. A numpy rasterizer draws these, and the bake uses the same projection,
   so what a camera saw and where a texel lands in that view agree exactly. The renders
   depend only on the geometry and the cameras, so a re-paint with new settings reuses them
   (`qa` and `mesh` delete `paint/`).
2. **Generate** (ComfyUI, one prompt per view). juggernautXL v9 img2img from the voxel
   colour render (`denoise` 0.7), controlnet-union SDXL promax in depth mode on the depth
   render (`cn_strength`), and IP-Adapter Plus (ViT-H) on `ref.png` (`ip_weight`), so the
   colours and costume follow the reference. The prompt is the reference prompt with the
   view words stripped, plus a view hint ("back view, seen from behind", ...). The back
   view adds "face, eyes, mouth" to the negative. Head views use a close-up prompt. Sampler,
   steps, cfg and checkpoint come from the reference run (`manifest.ref`). Output:
   `paint/<view>_gen.png`.
   **What it really fed (found 2026-11-08):** `forge.upload_file` stored every upload as
   `<subfolder>/overwrite` (its form-field loop reused the `name` argument), so the ref, depth,
   normal and init images of a view were one file, the last one uploaded: the view's voxel
   colour render. Every painted texture so far was made that way (img2img from the colour
   render, the depth ControlNet and IP-Adapter also on the colour render, never on the depth map
   or `ref.png`), and the settings below were tuned on it. `upload_file` is fixed; `paint.py`
   still passes the colour render for all four on purpose, so paint output is unchanged. Feeding
   the real depth map and `ref.png` is an open decision (a re-tune and a re-paint pass).
3. **Colour match**. Before the bake, each generated view's per-channel mean and spread
   inside the silhouette are pulled toward the voxel render of the same view
   (`color_match`, 0 = off, 1 = full). This keeps SDXL from drifting the overall hue or
   brightness of a view, which would show as seams between views.
4. **Coverage bake** (mode `bake`). Every texel of LOD0 is projected into every view. A
   view counts for a texel when the texel passes the depth test against that view's
   z-buffer and faces the camera within about 70 degrees. The weight is the cosine,
   faded out near the 70-degree limit, near the silhouette and near depth jumps (where the
   generated image is most likely off the mesh), and scaled up for close-up views (more
   pixels per texel). The views are blended by weight. Texels with little total weight
   fade back to the voxel albedo, and texels no view sees keep it. Painted colour bleeds 3
   texels into the UV gutters so mip-maps do not pick up voxel colour at chart borders.

## Views

`views` unset means by body plan: `front`, `back`, `left`, `right`, `front_left`,
`front_right`, plus `top` for quadruped, winged_quadruped, insectoid and centauroid, plus
`head`, `head_left`, `head_right` when `head_views` is on and QA found a head region
(`manifest.qa.head_region`). The head cameras frame 0.7 times QA's head window, so the face
fills the image. For plans whose head is at the front end (quadrupeds) the head views look
from 35 degrees off the front. All the names are in `VIEW_SPECS` in `tools/paint.py`.

## Outputs

| file | what |
|---|---|
| `textures/base_color_voxel.png` | the voxel albedo (a copy of `textures/base_color.png`, the mesh-stage bake) |
| `textures/base_color_painted.png` | the painted albedo, same size and UV layout |
| `textures/paint_coverage.png` | per-texel paint weight (white = painted, black = voxel) |
| `model.glb` | re-exported with the painted base colour |
| `paint/` | control renders, generated views, `cameras.json`, `prompts.json`, `bake.json` |
| `manifest.paint` | settings, views, seed, `painted_fraction`, `any_view_fraction`, per-view texel counts, phase seconds, peak RAM and VRAM |

Paint deletes the LODs and the rigged game files (`model_rigged*.glb`), because their
material is stale. The skeleton (`rig/chosen.glb`) stays. Re-run `lod` and `rig` after
`paint`; `forge.py run` does this in order.

**Voxel vs painted albedo.** Both images are kept. The viewer shows an
`albedo: painted | voxel` switch when both exist (`viewer/README.md`), so the two can be
compared on the live model with LODs and clips.

## Settings and overrides

The `paint` override in `data/overrides/<source>/<id>.json` is an object; any key left out
uses the default (`PAINT_DEFAULTS` in `tools/overrides.py`, checked by `PAINT_KEYS`):

| key | default | range | meaning |
|---|---|---|---|
| `enabled` | true | bool | part of a default `forge.py run`. An explicit `--stages paint` or `forge.py paint` always runs. |
| `views` | null | list of view names | null = by body plan (above) |
| `denoise` | 0.7 | 0.05-1.0 | img2img strength from the voxel render; 1.0 = text-to-image with the controls only |
| `ip_weight` | 0.7 | 0-1.5 | IP-Adapter weight on `ref.png`; 0 removes the adapter |
| `cn_strength` | 0.7 | 0-2 | depth ControlNet strength |
| `normal_strength` | 0.0 | 0-2 | adds a second, normal-map ControlNet when > 0 |
| `color_match` | 0.5 | 0-1 | per-view colour match to the voxel render |
| `head_views` | true | bool | add the three head close-ups |
| `seed` | null | int | null = derived from the monster id |

Example: `{"paint": {"denoise": 0.6, "head_views": false}}`. Command-line flags override
both for one run: `--paint-denoise`, `--paint-ip-weight`, `--paint-cn-strength`,
`--paint-normal-strength`, `--paint-color-match`, `--paint-views front,back,left,right`.

## Cost

Measured on the pilots (RTX 5070 Ti 16 GB, 30 GB RAM): 145-200 s per monster, most of it
the 9-10 SDXL generations (about 120-145 s), then views (20-65 s) and bake (5-10 s). Peak
VRAM is 12.7-13.9 GB and peak system RAM about 24-28 GB. When less than 6 GB of RAM is
available before generating, the stage asks ComfyUI to unload its models first. Over the
330 SRD monsters, paint adds about 14-17 hours.

## Review sheets

`python3 -I tools/paint.py compare <id>...` (`--tag` adds a suffix) writes to
`out/<source>/_paint_cmp/`:

- `<id>_body.png`: four angles; top row voxel, bottom row painted;
- `<id>_head.png`: voxel and painted side by side with QA's head camera;
- `<id>_views.png`: per view, the voxel render, the depth control and the generated image.

## Verdict

Enabled by default since 2026-11-04 (`PAINT_DEFAULTS["enabled"] = true`). On every pilot
the painted row of the sheets looked better than the voxel row: sharper faces, readable
costume detail, and colours that stay close to the reference. Set `"paint": {"enabled":
false}` for a monster where it does not.

Known limits: about 35-60% of texels end up painted (`painted_fraction`), and the rest is
the voxel albedo (undersides, armpits, the inside of wings), so a seam in sharpness can
show where they meet. Views are generated independently, so small details (straps, eye
colour) can differ between views and blend into a soft average.
