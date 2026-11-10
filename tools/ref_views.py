#!/usr/bin/env python3
"""Clean turnaround views of a reference character with Qwen-Image-Edit (ComfyUI), for multi-view meshing and projection.

  python3 -I tools/ref_views.py <id> --source <src> [--pick N] [--views back,left,right,front_left] [--seed S] [--steps 20]

Reads out/<src>/<id>/ref.png (or ref_<N>.png with --pick), asks the edit model for the same character seen from
each requested side ("same character, same outfit, same colours, flat cel shading, plain white background"), and
writes out/<src>/<id>/views/<view>.png plus views.json. A video model that orbits the camera drifts in colour and
shape; an edit model keeps the design, so the views can be used as-is.

Needs in ComfyUI: diffusion_models/qwen_image_edit_2509_fp8_e4m3fn.safetensors, text_encoders/qwen_2.5_vl_7b_fp8_scaled
.safetensors, vae/qwen_image_vae.safetensors (Comfy-Org/Qwen-Image-Edit_ComfyUI and Qwen-Image_ComfyUI on Hugging Face,
Apache-2.0). COMFY_URL / COMFY_TOKEN as for forge.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge  # noqa: E402

UNET = "qwen_image_edit_2509_fp8_e4m3fn.safetensors"
CLIP = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
VAE = "qwen_image_vae.safetensors"

VIEWS = {
    "clean": "Redraw this exact character, front view, same pose, same face, same hair, same outfit and colours, "
             "but remove all paint splatter, spray, smoke, particles and decorations around and behind her. Keep her hair purple.",
    "back": "Show this exact character from directly behind, seen from the back, standing in the same A-pose. "
            "Keep the same hair colour and length, the same outfit and every colour; show the back of the hair and clothes.",
    "left": "Show this exact character in a pure side view, facing left, same A-pose, same outfit and colours.",
    "right": "Show this exact character in a pure side view, facing right, same A-pose, same outfit and colours.",
    "front_left": "Show this exact character turned three-quarters to the left, same A-pose, same outfit and colours.",
    "front_right": "Show this exact character turned three-quarters to the right, same A-pose, same outfit and colours.",
}
STYLE = (" Full body, head to boots, flat cel-shaded anime style with clean outlines, plain white background, "
         "no text, no extra objects, no shadow.")


def graph(image: str, prompt: str, seed: int, steps: int, cfg: float, prefix: str) -> dict:
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "img": {"class_type": "LoadImage", "inputs": {"image": image}},
        "scale": {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["img", 0], "upscale_method": "lanczos", "megapixels": 1.0, "resolution_steps": 8}},
        "pos": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "prompt": prompt + STYLE, "vae": ["vae", 0], "image1": ["scale", 0]}},
        "neg": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "prompt": "", "vae": ["vae", 0], "image1": ["scale", 0]}},
        "lat": {"class_type": "VAEEncode", "inputs": {"pixels": ["scale", 0], "vae": ["vae", 0]}},
        "aura": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet", 0], "shift": 3.0}},
        "ks": {"class_type": "KSampler", "inputs": {"model": ["aura", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["lat", 0],
                                                    "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["dec", 0], "filename_prefix": prefix}},
    }


def frame(d: Path, ref: Path) -> dict:
    """Mask the white background of ref + views/*.png and frame them like tools/turnaround.py (one scale, the widest
    extent 1/1.1 of a 1024 frame, on black) into turnaround/{front,left,back,right}.png + turnaround.json, so
    `forge.py mesh --mesh-views mv` reuses them instead of generating a video turnaround. Needs numpy, scipy, Pillow."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    def cut(path: Path):
        im = Image.open(path).convert("RGB")
        a = np.asarray(im).astype(int)
        bg = a.min(axis=2) >= 246
        lab, _ = ndimage.label(bg)
        edge = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
        fg = ~np.isin(lab, list(edge))
        fg = ndimage.binary_opening(fg, iterations=2)
        lab2, n = ndimage.label(fg)
        if n > 1:  # keep the figure: the biggest piece plus pieces that touch its dilated box (hair strands, boots)
            sizes = ndimage.sum(fg, lab2, range(1, n + 1))
            big = int(np.argmax(sizes)) + 1
            near = ndimage.binary_dilation(lab2 == big, iterations=6)
            keep = [i + 1 for i, z in enumerate(sizes) if i + 1 == big or (z >= 0.002 * sizes.max() and near[lab2 == i + 1].any())]
            fg = np.isin(lab2, keep)
        return im, ndimage.binary_fill_holes(fg)

    parts = {"front": ref, **{v: d / "views" / f"{v}.png" for v in ("left", "back", "right") if (d / "views" / f"{v}.png").exists()}}
    cut_ = {v: cut(p) for v, p in parts.items()}
    boxes = {}
    for v, (im, m) in cut_.items():
        ys, xs = np.nonzero(m)
        boxes[v] = (xs.min(), ys.min(), xs.max(), ys.max(), im.size)
    M = max(max(b[2] - b[0], b[3] - b[1]) for b in boxes.values())
    cy = float(np.mean([(b[1] + b[3]) / 2 for b in boxes.values()]))
    S = 1024
    tdir = d / "turnaround"
    tdir.mkdir(exist_ok=True)
    out = {}
    for v, (im, m) in cut_.items():
        b = boxes[v]
        scale = S / (1.1 * M)
        arr = (np.asarray(im, float) * m[..., None]).astype("uint8")
        w, h = im.size
        src = Image.fromarray(arr).resize((int(round(w * scale)), int(round(h * scale))), Image.LANCZOS)
        canvas = Image.new("RGB", (S, S), (0, 0, 0))
        canvas.paste(src, (int(round(S / 2 - (b[0] + b[2]) / 2 * scale)), int(round(S / 2 - cy * scale))))
        canvas.save(tdir / f"{v}.png")
        out[v] = f"turnaround/{v}.png"
    rep = {"views": out, "source": "ref_views.py (Qwen-Image-Edit)", "ref_crc": forge._file_crc(d / "ref.png"),
           "generate": {"model": UNET}, "fov": 20.0, "el": 0.0, "ref_iou": None, "picks": {}}
    (tdir / "turnaround.json").write_text(json.dumps(rep, indent=1) + "\n")
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("id")
    ap.add_argument("--source", required=True)
    ap.add_argument("--pick", type=int)
    ap.add_argument("--views", default="back,left,right")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--cfg", type=float, default=4.0)
    ap.add_argument("--frame-only", action="store_true", help="skip generation: mask and frame the existing views/ + ref")
    a = ap.parse_args()
    d = forge.OUT_ROOT / a.source / a.id
    ref = d / (f"ref_{a.pick}.png" if a.pick is not None else "ref.png")
    if a.frame_only:
        frame(d, d / "ref.png")
        return 0
    name = forge.upload_file(ref, "monster-forge/views", f"{a.source}_{a.id}_ref.png")
    out = d / "views"
    out.mkdir(exist_ok=True)
    rec = {"ref": ref.name, "seed": a.seed, "steps": a.steps, "cfg": a.cfg, "model": UNET}
    for v in a.views.split(","):
        res = forge.run_prompt(graph(name, VIEWS[v], a.seed, a.steps, a.cfg, f"monster-forge/views/{a.source}/{a.id}/{v}"), f"{a.id} view {v}")
        forge.fetch_output(res["save"]["images"][0], out / f"{v}.png")
        forge.log(f"{a.id}: {v} -> {out / (v + '.png')}")
    (out / "views.json").write_text(json.dumps(rec, indent=1) + "\n")
    frame(d, d / "ref.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
