# ComfyUI on Beam (remote GPU for forge.py)

`app.py` deploys ComfyUI v0.36.0 on an RTX 4090 as the Beam ASGI endpoint `monster-forge-comfy`. It scales to zero
(`keep_warm_seconds=900`), cold start is about 60 s. Models come from the Beam volume `tcg-mint-models`
(mounted at `/volumes/tcg-mint-models`; the folder map in `app.py` includes `geometry_estimation` and
`background_removal`, which stock ComfyUI does not map). Only ComfyUI_IPAdapter_plus is a custom node.

## Deploy

    uv tool install beam-client        # needs ~/.beam/config.ini
    cd deploy/beam && beam deploy app.py:comfy -n monster-forge-comfy

A deploy takes about 7 min (image build). Each deploy makes a new version with a new URL: stop the old
ones (`beam deployment stop <id>`).

## Use from forge.py

    export COMFY_URL=https://app.beam.cloud/asgi/monster-forge-comfy/v<N>   # path URL from the gateway
    export COMFY_TOKEN=<workspace token from ~/.beam/config.ini>
    python3 tools/forge.py ref|mesh|qa ... 

`forge.http()` sends the token as a bearer header and retries GETs on 502/503/504 (cold start). Only the
ComfyUI stages (`ref`, `mesh`, `paint`) go remote; `qa`, `lod`, `rig` and `anim` run locally in Blender and
need `PIL_PYTHON` set to a Python with Pillow for `contact`.

## Pitfalls (each cost a deploy)

- ComfyUI 0.37 adds a required `refine_steps` input to `MoGeInference`; the workflows do not set it. Stay on v0.36.0.
- Beam's runner adds `/health` after the app is built. The catch-all proxy must define `/health` first, or the
  container is never marked ready and every request hangs.
- Uploading models: `beam cp <local> beam://<volume>/<path>` (a bare `<volume>/<path>` is read as local).
- `beam deploy` prints a subdomain URL that did not answer here; use the path URL
  (`GetUrlRequest(url_type="path")` through the gateway).
- Not the RTX 5070 Ti box the README describes: VRAM peaks (about 10 GB for the mesh stage) are the remote 4090's.
