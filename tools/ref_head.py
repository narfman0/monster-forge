#!/usr/bin/env python3
"""Sharp face for the projection texture: a Qwen-Image-Edit redraw of the mesh's head close-up.

  ~/.cache/mf-venv/bin/python tools/ref_head.py <src> <id> [denoise=1.0] [tag=h10]

Renders the head close-up with paint.py's head camera, has Qwen-Image-Edit redraw it with ref.png as the design, and writes
headtest/head_gen_<tag>.png. Copy the one you want to paint/head_gen_qwen.png; tools/ref_project.py aligns it by the iris
centres, limits it to a face ellipse (paint/head_weight.png) and bakes it. Denoise 1.0 gives the cleanest face; 0.75 keeps
the mesh's soft face. Needs COMFY_URL, the Qwen weights (see tools/ref_views.py)."""
import sys, json, math, shutil, os
from pathlib import Path
sys.path.insert(0, str(Path.home() / "workspace/monster-forge/tools"))
import forge, paint
from ref_views import UNET, CLIP, VAE

src, cid = sys.argv[1], sys.argv[2]
den = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
tag = sys.argv[4] if len(sys.argv) > 4 else "head1"
mon = forge.load_registry(src)[cid]
d = forge.OUT_ROOT / src / cid; pdir = d / "paint"; tex = d / "textures"
man = forge.load_manifest(d); qa = man["qa"]
pdir.mkdir(exist_ok=True)
shutil.copyfile(tex / "base_color.png", tex / "base_color_voxel.png")
hb = paint.head_box(qa, mon["body_plan"])
hd = paint.head_view_dir(qa, mon["body_plan"])
base = math.degrees(math.atan2(-hd[0], hd[1]) - math.atan2(-paint.fwd_vec(qa)[0], paint.fwd_vec(qa)[1]))
views = [{"name": "head", "yaw": paint.VIEW_SPECS["head"][0] + base, "elev": paint.VIEW_SPECS["head"][1], "hint": "front view", "head": True}]
request = {"views": views, "size": 1024, "fwd": paint.fwd_vec(qa), "head_box": hb}
hp = d / "headtest"; hp.mkdir(exist_ok=True)
paint.blender(forge, {"mode": "views", "src": str(d / "model.glb"), "voxel_tex": str(tex / "base_color_voxel.png"), "paint_dir": str(hp), **request})
img = forge.upload_file(hp / "head_color.png", "monster-forge/head", f"{src}_{cid}_head.png")
ref = forge.upload_file(d / "ref.png", "monster-forge/head", f"{src}_{cid}_ref.png")
P = ("Image 1 is a rough 3D render of a character's head and shoulders seen from the front. Image 2 is the finished design of the same character. "
     "Redraw image 1 as a clean, sharp, flat cel-shaded anime close-up that matches the face and hair of image 2 exactly: the same large eyes with highlights, "
     "the same eyebrows, nose, mouth, blunt bangs, hair colour and collar. Keep the exact framing, head position and silhouette of image 1. "
     "Plain white background, crisp thin dark outlines, no shadow, no text.")
sc = {"megapixels": 1.0, "resolution_steps": 8, "upscale_method": "lanczos"}
g = {
    "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
    "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": "default"}},
    "vae": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
    "i1": {"class_type": "LoadImage", "inputs": {"image": img}}, "i2": {"class_type": "LoadImage", "inputs": {"image": ref}},
    "s1": {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["i1", 0], **sc}},
    "s2": {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["i2", 0], **sc}},
    "pos": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "prompt": P, "vae": ["vae", 0], "image1": ["s1", 0], "image2": ["s2", 0]}},
    "neg": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["clip", 0], "prompt": "", "vae": ["vae", 0], "image1": ["s1", 0], "image2": ["s2", 0]}},
    "lat": {"class_type": "VAEEncode", "inputs": {"pixels": ["s1", 0], "vae": ["vae", 0]}},
    "aura": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet", 0], "shift": 3.0}},
    "ks": {"class_type": "KSampler", "inputs": {"model": ["aura", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["lat", 0], "seed": 5,
                                                "steps": 20, "cfg": 4.0, "sampler_name": "euler", "scheduler": "simple", "denoise": den}},
    "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}},
    "save": {"class_type": "SaveImage", "inputs": {"images": ["dec", 0], "filename_prefix": f"monster-forge/head/{src}/{cid}/{tag}"}},
}
out = forge.run_prompt(g, f"{cid} head {tag}")
forge.fetch_output(out["save"]["images"][0], hp / f"head_gen_{tag}.png")
forge.log(f"{cid}: head {tag} -> {hp / f'head_gen_{tag}.png'}")
