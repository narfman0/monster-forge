"""ComfyUI on Beam (RTX 4090) for monster-forge: a plain reverse proxy to a ComfyUI subprocess.

Deploy:   beam deploy deploy/beam/app.py:comfy -n monster-forge-comfy
URL:      beam deployment ... / `tools/beam_comfy.py url`  (needs the workspace token in the
          Authorization header; tools/beam_comfy.py wraps that for COMFY_URL)

Models live on the Beam volume `tcg-mint-models` (mounted at /volumes/tcg-mint-models); the
folder map below puts every ComfyUI model folder on it, including geometry_estimation and
background_removal that the mesh stage needs. ComfyUI core ships the TRELLIS.2 / Pixal3D nodes;
only ComfyUI_IPAdapter_plus is a custom node (the paint stage).
"""
from beta9 import Image, Volume, asgi

COMFY_REF = "v0.36.0"  # 0.37 added a required MoGeInference refine_steps input the workflows do not set
PATHS_YAML = """\
vol:
  base_path: /volumes/tcg-mint-models/
  checkpoints: checkpoints
  diffusion_models: diffusion_models
  vae: vae
  clip_vision: clip_vision
  controlnet: controlnet
  ipadapter: ipadapter
  loras: loras
  text_encoders: text_encoders
  upscale_models: upscale_models
  geometry_estimation: geometry_estimation
  background_removal: background_removal
"""

image = (
    Image(python_version="python3.12", base_image="docker.io/nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04")
    .add_commands([
        "apt-get update && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 ca-certificates && rm -rf /var/lib/apt/lists/*",
        f"git clone --depth 1 --branch {COMFY_REF} https://github.com/comfyanonymous/ComfyUI /root/comfy/ComfyUI",
        "git clone --depth 1 https://github.com/cubiq/ComfyUI_IPAdapter_plus /root/comfy/ComfyUI/custom_nodes/ComfyUI_IPAdapter_plus",
        "pip install --no-cache-dir torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128",
        "pip install --no-cache-dir -r /root/comfy/ComfyUI/requirements.txt fastapi httpx uvicorn",
    ])
)


def _start_comfy():
    import os
    import subprocess
    import time
    import urllib.request

    cfg = "/root/comfy/ComfyUI/mf-paths.yaml"
    with open(cfg, "w") as f:
        f.write(PATHS_YAML)
    subprocess.Popen(
        ["python", "main.py", "--listen", "127.0.0.1", "--port", "8188",
         "--extra-model-paths-config", cfg, "--disable-auto-launch"],
        cwd="/root/comfy/ComfyUI", env={**os.environ, "PYTHONUNBUFFERED": "1"})
    t0 = time.time()
    while time.time() - t0 < 600:
        try:
            urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=3).read()
            return
        except Exception:
            time.sleep(2)
    raise RuntimeError("ComfyUI did not come up in 600 s")


@asgi(
    name="monster-forge-comfy",
    gpu="RTX4090",
    cpu=8,
    memory="48Gi",
    image=image,
    volumes=[Volume(name="tcg-mint-models", mount_path="/volumes/tcg-mint-models")],
    on_start=_start_comfy,
    timeout=3600,
    keep_warm_seconds=900,
    concurrent_requests=16,
)
def comfy(context):
    import httpx
    from fastapi import FastAPI, Request, Response

    app = FastAPI()
    client = httpx.AsyncClient(base_url="http://127.0.0.1:8188", timeout=httpx.Timeout(600.0))

    # Must come BEFORE the catch-all: Beam's runner adds its own /health after the app is built,
    # so the proxy would swallow the gateway's readiness probe (ComfyUI has no /health -> 404 ->
    # the container is never marked ready and every request hangs).
    @app.get("/health")
    async def health():
        return Response(status_code=200)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def proxy(path: str, request: Request):
        r = await client.request(
            request.method, "/" + path, params=request.query_params,
            content=await request.body(),
            headers={k: v for k, v in request.headers.items()
                     if k.lower() in ("content-type", "accept")})
        return Response(r.content, r.status_code,
                        {k: v for k, v in r.headers.items() if k.lower() == "content-type"})

    return app
