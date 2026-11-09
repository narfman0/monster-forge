#!/usr/bin/env python3
"""Turnaround views for multi-view image-to-3D (docs/mesh-quality.md).

From the chosen reference, Wan 2.2 TI2V 5B (ComfyUI core) films a half orbit around the creature twice: once from
ref.png and once from its mirror image (whose frames are mirrored back, so the camera goes round the other way).
BiRefNet masks every frame. The azimuth of each frame is read off a single-view mesh of the same reference:
its silhouettes from orbit cameras are matched to the frame masks, with a monotonic orbit (dynamic programming)
so the front/back silhouette ambiguity of a mirror-symmetric body does not flip the answer. The frames nearest
+90 (left), -90 (right) and 180 (back) become the extra views, framed like Pixal3DMultiViewConditioning's rig
(the same scale in every view, the widest extent 1/1.1 of the frame, on black).

Two halves:
  * `generate(fg, mon, d, seed)` runs inside forge.py (stdlib only): the ComfyUI graphs, frames into
    <d>/turnaround/{a,b}/.
  * `python turnaround.py analyze <d> --mesh <hi.glb>` (numpy, trimesh, Pillow: forge.PIL_PYTHON) picks and
    frames the views into <d>/turnaround/{front,left,back,right}.png and writes turnaround.json.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

WAN = {"unet": "wan2.2_ti2v_5B_fp16.safetensors", "clip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
       "vae": "wan2.2_vae.safetensors"}
SIZE, LENGTH, STEPS, CFG, SHIFT = 704, 81, 20, 5.0, 8.0
NEGATIVE = ("static, still image, blurry, low quality, deformed, extra limbs, multiple creatures, text, watermark, "
            "scenery, background objects, the creature moves, walking, running, camera zoom, shadow")
VIEW_AZ = {"front": 0.0, "left": 90.0, "back": 180.0, "right": -90.0}


def prompt_for(mon: dict) -> str:
    # 2026-11-08 goblin probes: "the camera smoothly orbits halfway around it ... ends directly behind it" left the
    # figure static on 3 of 3 seeds; this wording orbited on the seeds tried (the direction varies by seed)
    return (f"3D turnaround of a {mon['name'].lower()} figure that stands perfectly still. The camera circles around "
            "the figure: it starts in front, moves to the side, and finishes behind the figure, looking at its back. "
            "Plain light grey studio background, even flat lighting, constant distance, eye level.")


def video_graph(image_name: str, prompt: str, seed: int, prefix: str, mirror: bool) -> dict:
    img = ["img", 0]
    g = {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": WAN["unet"], "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": WAN["clip"], "type": "wan", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": WAN["vae"]}},
        "img": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": NEGATIVE}},
        "shift": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["unet", 0], "shift": SHIFT}},
        "sample": {"class_type": "KSampler", "inputs": {
            "model": ["shift", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["lat", 0],
            "seed": seed, "steps": STEPS, "cfg": CFG, "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["dec", 0], "filename_prefix": prefix + "/f"}},
        "bg_model": {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": "birefnet.safetensors"}},
        "bg": {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["bg_model", 0], "image": ["dec", 0]}},
        "mask_img": {"class_type": "MaskToImage", "inputs": {"mask": ["bg", 0]}},
        "save_mask": {"class_type": "SaveImage", "inputs": {"images": ["mask_img", 0], "filename_prefix": prefix + "/m"}},
    }
    if mirror:
        g["flip"] = {"class_type": "ImageFlip", "inputs": {"image": ["img", 0], "flip_method": "y-axis: horizontally"}}
        img = ["flip", 0]
    else:  # the reference's own mask, full size
        g["ref_bg"] = {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["bg_model", 0], "image": ["img", 0]}}
        g["ref_mask"] = {"class_type": "MaskToImage", "inputs": {"mask": ["ref_bg", 0]}}
        g["save_ref_mask"] = {"class_type": "SaveImage", "inputs": {"images": ["ref_mask", 0],
                                                                      "filename_prefix": prefix + "/ref_mask"}}
    g["lat"] = {"class_type": "Wan22ImageToVideoLatent", "inputs": {
        "vae": ["vae", 0], "width": SIZE, "height": SIZE, "length": LENGTH, "batch_size": 1, "start_image": img}}
    return g


MAX_VIDEOS = 4
SIDES = "abcdef"


def generate(fg, mon: dict, d: Path, seed: int, est_mesh: Path, max_videos: int = MAX_VIDEOS) -> dict:
    """Orbit videos (+ masks) into <d>/turnaround/<side>/ until the left, right and back views are found (the
    analysis runs after each video), alternating the reference and its mirror image, seed + k for video k.
    Returns {seconds, videos, seed, prompt, analyze_seconds}."""
    import shutil
    import subprocess
    out = d / "turnaround"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    sub = f"monster-forge/{mon['source']}/{d.name}"
    name = fg.upload_file(d / "ref.png", sub)
    prompt = prompt_for(mon)
    t0, t_an, rep = time.time(), 0.0, {}
    for k in range(max_videos):
        side, mirror = SIDES[k], k % 2 == 1
        dd = out / side
        dd.mkdir(parents=True, exist_ok=True)
        g = video_graph(name, prompt, seed + k, f"{sub}/turnaround_{side}", mirror)
        if (out / "ref_mask.png").exists():  # the reference mask once
            for nid in ("ref_bg", "ref_mask", "save_ref_mask"):
                g.pop(nid, None)
        o = fg.run_prompt(g, f"{mon['id']} turnaround video {k + 1} ({'mirrored ' if mirror else ''}reference)")
        for i, im in enumerate(o["save"]["images"]):
            fg.fetch_output(im, dd / f"f{i:03d}.png")
        for i, im in enumerate(o["save_mask"]["images"]):
            fg.fetch_output(im, dd / f"m{i:03d}.png")
        if "save_ref_mask" in o:
            fg.fetch_output(o["save_ref_mask"]["images"][0], out / "ref_mask.png")
        (dd / "video.json").write_text(json.dumps({"mirror": mirror, "seed": seed + k}) + "\n")
        t1 = time.time()
        proc = subprocess.run([fg.PIL_PYTHON, "-I", str(Path(__file__).resolve()), "analyze", str(d), "--mesh",
                               str(est_mesh)], capture_output=True, text=True, timeout=900)
        t_an += time.time() - t1
        if proc.returncode != 0:
            raise RuntimeError(f"{mon['id']}: turnaround analyze failed: {proc.stderr.strip()[-800:]}")
        rep = json.loads((out / "turnaround.json").read_text())
        found = [v for v, f in rep["picks"].items() if f]
        fg.log(f"{mon['id']}: turnaround video {k + 1}: reach {rep['tracks'][side]['reach']} deg, views {found}")
        if len(found) == 3:
            break
    try:  # Wan (9.5 GB) and its text encoder stay resident otherwise: ComfyUI's RSS hit 26 GB on the pilot
        fg.http("POST", "/free", json.dumps({"unload_models": True, "free_memory": True}).encode(),
                {"Content-Type": "application/json"})
    except Exception:
        pass
    return {"seconds": round(time.time() - t0, 1), "analyze_seconds": round(t_an, 1), "videos": k + 1,
            "seed": seed, "prompt": prompt, "frames": LENGTH, "size": SIZE}


# ------------------------------------------------------------------ analysis (numpy side)
def _np():
    import numpy as np
    return np


def load_points(path: str, max_pts: int = 120000):
    """Surface samples of a mesh (Pixal3D raw frame: glTF Y up, the reference camera on +Z)."""
    import numpy as np
    import trimesh
    m = trimesh.load(path, force="mesh")
    n = min(max_pts, max(20000, len(m.faces)))
    pts, _ = trimesh.sample.sample_surface(m, n, seed=0)
    return np.asarray(pts, float)


def _rot(az: float, el: float):
    np = _np()
    a, e = np.radians(az), np.radians(el)
    ry = np.array([[np.cos(-a), 0, np.sin(-a)], [0, 1, 0], [-np.sin(-a), 0, np.cos(-a)]])
    rx = np.array([[1, 0, 0], [0, np.cos(e), -np.sin(e)], [0, np.sin(e), np.cos(e)]])
    return rx @ ry


def project(P, az=0.0, el=0.0, fov=30.0):
    """2D image-plane points (x right, y up) of world points seen from an orbit camera: az > 0 = the camera moved
    to the viewer's right (Pixal3D's "left" view), el > 0 = looking down."""
    np = _np()
    Q = P @ _rot(az, el).T
    if not fov:
        return Q[:, :2]
    dist = 0.55 / np.tan(np.radians(fov) / 2)
    z = dist - Q[:, 2]
    return Q[:, :2] / z[:, None] * dist


def splat(xy, res=128, pad=1.1, close=2):
    """Bbox-normalised silhouette (aspect kept, longer side = res / pad) from projected points."""
    np = _np()
    from scipy import ndimage
    lo, hi = xy.min(0), xy.max(0)
    c, h = (lo + hi) / 2, (hi - lo).max() / 2 * pad
    px = ((xy - c) / h * 0.5 + 0.5) * res
    ix = np.clip(px[:, 0].astype(int), 0, res - 1)
    iy = np.clip((res - 1 - px[:, 1]).astype(int), 0, res - 1)
    im = np.zeros((res, res), bool)
    im[iy, ix] = True
    im = ndimage.binary_closing(im, iterations=close, border_value=0)
    return ndimage.binary_fill_holes(im)


def mask_norm(mask, res=128, pad=1.1):
    """Bbox-normalised silhouette of an image mask (same framing as splat)."""
    np = _np()
    from PIL import Image
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return np.zeros((res, res), bool)
    cy, cx = (ys.min() + ys.max()) / 2, (xs.min() + xs.max()) / 2
    h = max(ys.max() - ys.min(), xs.max() - xs.min()) / 2 * pad
    im = Image.fromarray((mask * 255).astype("uint8")).crop((int(cx - h), int(cy - h), int(cx + h), int(cy + h)))
    return np.asarray(im.resize((res, res), Image.BILINEAR)) > 127


def iou(a, b) -> float:
    u = (a | b).sum()
    return float((a & b).sum() / u) if u else 0.0


def read_mask(path, thr=127):
    np = _np()
    from PIL import Image
    return np.asarray(Image.open(path).convert("L")) > thr


def fit_camera(P, ref_mask):
    """(fov, el, iou) of the reference camera that best explains the reference mask (az 0)."""
    rm = mask_norm(ref_mask)
    best = (30.0, 0.0, -1.0)
    for fov in (15, 25, 35, 45, 55, 65):
        for el in (-10, 0, 10, 20, 30):
            v = iou(splat(project(P, 0, el, fov)), rm)
            if v > best[2]:
                best = (float(fov), float(el), v)
    return best


MIN_VIEW_IOU = 0.65  # silhouette IoU a picked frame needs at its azimuth
FLOW_MIN = 20.0  # |summed relative flow| (px) below which the direction is left to the silhouettes


def orbit_direction(side_dir: Path, mirror: bool, frames: int = 60) -> float:
    """Summed horizontal optical flow (Farneback) inside the creature, relative to its silhouette's centroid, over
    the first `frames` frames (as filmed, mirrored back when `mirror`). The near surface moves against the
    camera: > 0 (features drift right) = the camera went to the viewer's left (azimuth decreasing, the right
    side comes into view), < 0 = azimuth increasing. Front/back and +-90 silhouettes of a mirror-symmetric body
    are near twins (the goblin's left and right profiles scored the same), so the sign comes from here."""
    np = _np()
    import cv2
    fr, ms = sorted(side_dir.glob("f*.png")), sorted(side_dir.glob("m*.png"))
    tot = 0.0
    for t in range(0, min(len(fr) - 2, frames), 2):
        a, b = (cv2.imread(str(fr[i]), cv2.IMREAD_GRAYSCALE) for i in (t, t + 2))
        ma, mb = (cv2.imread(str(ms[i]), cv2.IMREAD_GRAYSCALE) > 127 for i in (t, t + 2))
        if mirror:
            a, b, ma, mb = a[:, ::-1].copy(), b[:, ::-1].copy(), ma[:, ::-1], mb[:, ::-1]
        inner = ma & mb
        if inner.sum() < 100:
            continue
        f = cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 4, 21, 3, 5, 1.1, 0)
        tot += float(f[..., 0][inner].mean() - (np.nonzero(mb)[1].mean() - np.nonzero(ma)[1].mean()))
    return tot


def orbit_track(scores, grid, step_max, sgn_only=None):
    """Monotonic orbit through the frames: az[0] = 0, each frame moves 0..step_max grid steps one way. Returns
    (path indices, mean score) for the better of the two directions (or for `sgn_only`). scores: T x len(grid)."""
    np = _np()
    T, G = scores.shape
    z = int(np.argmin(np.abs(grid)))
    best = None
    for sgn in ((sgn_only,) if sgn_only else (1, -1)):
        dp = np.full((T, G), -1e9)
        bp = np.zeros((T, G), int)
        dp[0, z] = scores[0, z]
        for t in range(1, T):
            for g in range(G):
                lo, hi = (g - step_max, g) if sgn > 0 else (g, g + step_max)
                lo, hi = max(lo, 0), min(hi, G - 1)
                k = lo + int(np.argmax(dp[t - 1, lo:hi + 1]))
                dp[t, g], bp[t, g] = dp[t - 1, k] + scores[t, g], k
        g = int(np.argmax(dp[-1]))
        path = [g]
        for t in range(T - 1, 0, -1):
            g = bp[t, g]
            path.append(g)
        path = path[::-1]
        tot = float(dp[-1].max()) / T
        if best is None or tot > best[1]:
            best = (path, tot)
    return best


def analyze(d: Path, mesh: str) -> dict:
    np = _np()
    from PIL import Image
    tdir = d / "turnaround"
    P = load_points(mesh)
    ref = Image.open(d / "ref.png").convert("RGB")
    ref_mask = read_mask(tdir / "ref_mask.png")
    fov, el, ref_iou = fit_camera(P, ref_mask)
    grid = np.arange(-200, 201, 5, dtype=float)
    sils = [splat(project(P, a, el, fov)) for a in grid]
    tracks, frames = {}, []
    mirrored = {}
    for side in SIDES:
        if not (tdir / side / "video.json").exists():
            continue
        mirrored[side] = json.loads((tdir / side / "video.json").read_text())["mirror"]
        cache = tdir / side / "track.json"  # a video is analysed once per estimate mesh
        tr = json.loads(cache.read_text()) if cache.exists() else {}
        if tr.get("mesh") != str(mesh) or "frames" not in tr:
            ms = sorted((tdir / side).glob("m*.png"))
            masks = [read_mask(p) for p in ms]
            if mirrored[side]:  # filmed from the mirrored reference: mirror back
                masks = [m[:, ::-1] for m in masks]
            sc = np.array([[iou(mask_norm(m), s) for s in sils] for m in masks])
            flow = orbit_direction(tdir / side, mirrored[side])
            sgn = (-1 if flow > 0 else 1) if abs(flow) >= FLOW_MIN else None
            path, tot = orbit_track(sc, grid, step_max=3, sgn_only=sgn)
            azs = [float(grid[g]) for g in path]
            tr = {"mesh": str(mesh), "mean_iou": round(tot, 3), "az": azs, "reach": max(abs(a) for a in azs),
                  "flow": round(flow, 1), "iou": [round(float(sc[t, g]), 3) for t, g in enumerate(path)],
                  "frames": [{"side": side, "t": t, "az": float(grid[g]), "iou": float(sc[t, g]),
                              "area": float(masks[t].mean())} for t, g in enumerate(path)]}
            cache.write_text(json.dumps(tr) + "\n")
        frames += tr["frames"]
        tracks[side] = {k: v for k, v in tr.items() if k not in ("frames", "mesh")}
    picks = {}
    # sides first; the back then prefers a video that gave a side view: each video invents its own back (the
    # goblin's third video added a blue cape the first did not have) and mixing them makes conflicting views
    for view, target in (("left", 90.0), ("right", -90.0), ("back", 180.0)):
        def err(f):
            e = abs(((f["az"] - target + 180) % 360) - 180)
            return e
        # a frame the mesh's silhouette does not explain is not trusted (wolf: its "right" at IoU 0.57 was the
        # rear three-quarter view; the camera had passed the tail while the silhouettes lagged)
        cand = [f for f in frames if err(f) <= (25 if view == "back" else 20) and f["area"] > 0.005
                and f["iou"] >= MIN_VIEW_IOU]
        if not cand:
            picks[view] = None
            continue
        if view == "back":
            used = {f["side"] for f in (picks.get("left"), picks.get("right")) if f}
            cand = [f for f in cand if f["side"] in used] or cand
        e0 = min(err(f) for f in cand)
        near = [f for f in cand if err(f) <= e0 + 5]
        picks[view] = max(near, key=lambda f: f["iou"])
        picks[view]["az_err"] = err(picks[view])
    # framing: ref pixels; frames are the reference scaled to SIZE
    k = ref.size[0] / SIZE
    views = {"front": (ref, ref_mask)}
    for v, f in picks.items():
        if f is None:
            continue
        im = Image.open(tdir / f["side"] / f"f{f['t']:03d}.png").convert("RGB")
        m = read_mask(tdir / f["side"] / f"m{f['t']:03d}.png")
        if mirrored[f["side"]]:
            im, m = im.transpose(Image.FLIP_LEFT_RIGHT), m[:, ::-1]
        im = im.resize(ref.size, Image.LANCZOS)
        m = np.asarray(Image.fromarray((m * 255).astype("uint8")).resize(ref.size, Image.BILINEAR)) > 127
        views[v] = (im, m)
    boxes = {}
    for v, (im, m) in views.items():
        ys, xs = np.nonzero(m)
        boxes[v] = (xs.min(), ys.min(), xs.max(), ys.max())
    M = max(max(b[2] - b[0], b[3] - b[1]) for b in boxes.values())
    cy = float(np.mean([(b[1] + b[3]) / 2 for b in boxes.values()]))
    S = 1024
    scale = S / (1.1 * M)
    out = {}
    for v, (im, m) in views.items():
        b = boxes[v]
        cx = (b[0] + b[2]) / 2
        arr = np.asarray(im, float) * m[..., None]
        src = Image.fromarray(arr.astype("uint8"))
        w = int(round(im.size[0] * scale))
        src = src.resize((w, w), Image.LANCZOS)
        canvas = Image.new("RGB", (S, S), (0, 0, 0))
        canvas.paste(src, (int(round(S / 2 - cx * scale)), int(round(S / 2 - cy * scale))))
        canvas.save(tdir / f"{v}.png")
        out[v] = f"turnaround/{v}.png"
    rep = {"mesh": str(mesh), "fov": fov, "el": el, "ref_iou": round(ref_iou, 3), "tracks": tracks,
           "picks": {v: (None if f is None else {k2: (round(x, 3) if isinstance(x, float) else x)
                                                  for k2, x in f.items()}) for v, f in picks.items()},
           "views": out, "scale": round(scale, 4)}
    (tdir / "turnaround.json").write_text(json.dumps(rep, indent=1) + "\n")
    return rep


def sheet(d: Path, dst: Path) -> None:
    """Reference, the framed views and each video's track (az per frame)."""
    from PIL import Image, ImageDraw
    tdir = d / "turnaround"
    rep = json.loads((tdir / "turnaround.json").read_text())
    W = 256
    s = Image.new("RGB", (W * 5, W + 20), "white")
    dr = ImageDraw.Draw(s)
    im = Image.open(d / "ref.png").convert("RGB"); im.thumbnail((W, W)); s.paste(im, (0, 20))
    dr.text((2, 2), "ref", fill="black")
    for i, v in enumerate(("front", "left", "back", "right")):
        p = tdir / f"{v}.png"
        if p.exists():
            im = Image.open(p).convert("RGB"); im.thumbnail((W, W)); s.paste(im, (W * (i + 1), 20))
        f = rep["picks"].get(v) or {}
        dr.text((W * (i + 1) + 2, 2), f"{v} {f.get('side', '')}{f.get('t', '')} az {f.get('az', '')} iou {f.get('iou', '')}",
                fill="black")
    s.save(dst)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["analyze", "sheet"])
    ap.add_argument("dir")
    ap.add_argument("--mesh")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.cmd == "analyze":
        r = analyze(Path(a.dir), a.mesh)
        print(json.dumps({k: r[k] for k in ("fov", "el", "ref_iou", "picks")}))
    else:
        sheet(Path(a.dir), Path(a.out))
