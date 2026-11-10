#!/usr/bin/env python3
"""Sharp face for the projection texture: a Qwen-Image-Edit redraw of the head, aligned by geometry (no feature detection).

  ~/.cache/mf-venv/bin/python tools/ref_head.py <src> <id> [--denoise 1.0] [--seed 5]   (after tools/ref_project.py has run once)

The head camera (paint/cameras.json, `head`) has the same orientation as the `front` camera, so the head view is an exact
crop and scale of the aligned front image paint/front_gen.png. That crop (paint/head_up.png, upscaled) is what Qwen redraws
sharply with ref.png as the design; the redraw is then refined onto the crop with a small ECC affine step (clean image to clean
image, near identity) and written as paint/head_gen_final.png plus a feathered weight mask paint/head_weight_final.png.
tools/ref_project.py bakes them as the `head` view on its next run. Needs COMFY_URL, the Qwen weights (see tools/ref_views.py),
numpy, opencv, Pillow (the venv python). Denoise 1.0 gives the cleanest face.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge  # noqa: E402
from ref_views import CLIP, UNET, VAE  # noqa: E402

PROMPT = ("Image 1 is a small blurry crop of an anime character's head and shoulders. Image 2 is the finished design of the same character. "
          "Redraw image 1 as a clean, sharp, high-resolution flat cel-shaded anime close-up. Keep exactly the same framing, pose, head position, "
          "face shape, eye positions, hair outline, colours and clothing as image 1, and make the eyes, eyebrows, mouth and lines crisp, "
          "with the eye details of image 2. Plain white background, crisp thin dark outlines, no shadow, no text.")


def crop_box(cams: dict) -> list[float]:
    """Pixel box (x0, y0, x1, y1) in the front view that the head view shows (same orientation, orthographic)."""
    f = next(v for v in cams["views"] if v["name"] == "front")
    h = next(v for v in cams["views"] if v["name"] == "head")
    r, u = np.asarray(f["right"]), np.asarray(f["up"])
    d = np.asarray(h["center"]) - np.asarray(f["center"])  # head camera origin relative to the front camera origin
    W, sf, sh = f["width"], f["ortho_scale"], h["ortho_scale"]

    def fx(xh):
        return (((xh / W - 0.5) * sh + d @ r) / sf + 0.5) * W

    def fy(yh):
        return (0.5 - ((0.5 - yh / W) * sh + d @ u) / sf) * W

    return [fx(0), fy(0), fx(W), fy(W)]


def fit_to_head(box: list[float], mask) -> list[float]:
    """QA's head box can sit low on a character with long hair or a hat: lift the (square) box so its top is just above the
    silhouette top of the front view (`mask`: bool array, the front render's silhouette)."""
    ys, _ = np.nonzero(mask)
    side = box[2] - box[0]
    top = float(ys.min()) - 0.04 * side
    dy = top - box[1]
    return [box[0], box[1] + dy, box[2], box[3] + dy]


def graph(img: str, ref: str, den: float, seed: int, prefix: str) -> dict:
    sc = {"megapixels": 1.0, "resolution_steps": 8, "upscale_method": "lanczos"}
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "i1": {"class_type": "LoadImage", "inputs": {"image": img}}, "i2": {"class_type": "LoadImage", "inputs": {"image": ref}},
        "s1": {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["i1", 0], **sc}},
        "s2": {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["i2", 0], **sc}},
        "pos": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "prompt": PROMPT, "vae": ["vae", 0], "image1": ["s1", 0], "image2": ["s2", 0]}},
        "neg": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "prompt": "", "vae": ["vae", 0], "image1": ["s1", 0], "image2": ["s2", 0]}},
        "lat": {"class_type": "VAEEncode", "inputs": {"pixels": ["s1", 0], "vae": ["vae", 0]}},
        "aura": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet", 0], "shift": 3.0}},
        "ks": {"class_type": "KSampler", "inputs": {"model": ["aura", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["lat", 0], "seed": seed,
                                                    "steps": 20, "cfg": 4.0, "sampler_name": "euler", "scheduler": "simple", "denoise": den}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["dec", 0], "filename_prefix": prefix}},
    }


def refine(gen, ref):
    """ECC affine of `gen` onto `ref` (float gray, blurred): (2x3 matrix, correlation); identity if it does not converge."""
    import cv2
    a = cv2.GaussianBlur(ref, (0, 0), 3)
    b = cv2.GaussianBlur(gen, (0, 0), 3)
    M = np.eye(2, 3, dtype=np.float32)
    try:
        cc, M = cv2.findTransformECC(a, b, M, cv2.MOTION_AFFINE, (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 300, 1e-6), None, 5)
    except cv2.error:
        return np.eye(2, 3, dtype=np.float32), 0.0
    return M, float(cc)


def main() -> int:
    import cv2
    from PIL import Image
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("id")
    ap.add_argument("--denoise", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=5)
    a = ap.parse_args()
    d = forge.OUT_ROOT / a.src / a.id
    pdir = d / "paint"
    cams = json.loads((pdir / "cameras.json").read_text())
    from PIL import Image as _I
    fm = np.asarray(_I.open(pdir / "front_mask.png").convert("L")) > 127
    box0 = crop_box(cams)
    x0, y0, x1, y1 = fit_to_head(box0, fm)
    # the head camera that bakes the redraw must move by the same amount (ref_project reads paint/head_shift.json, in front-view
    # pixels, y down), or the redraw lands off the face; cumulative so a re-run does not undo it
    sp = pdir / "head_shift.json"
    old = json.loads(sp.read_text())["dy_px"] if sp.exists() else 0.0
    sp.write_text(json.dumps({"dy_px": old + (y0 - box0[1])}) + "\n")
    front = Image.open(pdir / "front_gen.png").convert("RGB")
    crop = front.crop((int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1)))).resize((1024, 1024), Image.LANCZOS)
    crop.save(pdir / "head_up.png")
    img = forge.upload_file(pdir / "head_up.png", "monster-forge/head", f"{a.src}_{a.id}_headup.png")
    ref = forge.upload_file(d / "ref.png", "monster-forge/head", f"{a.src}_{a.id}_ref.png")
    out = forge.run_prompt(graph(img, ref, a.denoise, a.seed, f"monster-forge/head/{a.src}/{a.id}/h"), f"{a.id} head")
    forge.fetch_output(out["save"]["images"][0], pdir / "head_gen_qwen.png")
    gen = Image.open(pdir / "head_gen_qwen.png").convert("RGB").resize((1024, 1024), Image.LANCZOS)

    def gray(im):
        return np.asarray(im.convert("L"), np.float32) / 255.0

    M, cc = refine(gray(gen), gray(crop))
    if cc < 0.85:  # the redraw does not line up with the crop (different framing, a second face): keep the texture without a face redraw
        for f in ("head_gen_final.png", "head_weight_final.png"):
            (pdir / f).unlink(missing_ok=True)
        forge.log(f"{a.id}: head redraw SKIPPED, ECC correlation {cc:.3f} < 0.85")
        return 0
    warped = cv2.warpAffine(np.asarray(gen), M, (1024, 1024), flags=cv2.INTER_LANCZOS4 | cv2.WARP_INVERSE_MAP, borderValue=(255, 255, 255))
    Image.fromarray(warped).save(pdir / "head_gen_final.png")
    yy, xx = np.mgrid[0:1024, 0:1024].astype("float32") / 1024.0
    q = np.sqrt(((xx - 0.5) / 0.44) ** 2 + ((yy - 0.52) / 0.50) ** 2)  # feathered ellipse: the face, with a little hair and collar
    w = (np.clip((1.0 - q) / 0.25, 0, 1) * 255).astype("uint8")
    Image.fromarray(w).convert("RGB").save(pdir / "head_weight_final.png")
    forge.log(f"{a.id}: head redraw aligned, ECC correlation {cc:.3f}, warp {np.round(M, 3).tolist()}")
    (pdir / "head.json").write_text(json.dumps({"crop_box": [x0, y0, x1, y1], "ecc_cc": cc, "warp": M.tolist(), "denoise": a.denoise, "seed": a.seed}, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
