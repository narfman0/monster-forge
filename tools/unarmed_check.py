"""Score reference candidates for a weapon / object held in a hand vs empty hands.

Weapons are runtime props (docs/equipment.md), so an `unarmed_reference` monster's mesh
must not bake one in; the empty-hands prompt alone held in 2 of 4 goblin candidates (a
bow, a dagger in the fist). forge.py's ref stage runs this on the candidates and picks the
most empty-handed one unless `ref_pick` is pinned (docs/references.md).

Method (zero-shot, no training):
1. Hands: DWPose (ComfyUI's comfyui_controlnet_aux checkpoint, run on the whole frame
   because its person detector misses goblins) gives the hand keypoints; each hand is
   cropped square at 1.8x its keypoint box (at least 12% of the image side). Without hand
   keypoints: its wrist, else the A-pose ControlNet image's wrists (`--pose-image`), else
   the two sides of the body at mid height (`hand_source` says which).
2. google/siglip-so400m-patch14-384 (Apache-2.0, ~3.5 GB in the Hugging Face cache)
   scores each crop against "a hand holding <weapon>" (13 weapons) and empty-hand
   prompts. SigLIP logits are independent sigmoids, so a hand's score is
       max(logit, holding prompts) - max(logit, empty-hand prompts)
   and a candidate's score is its highest hand. Above THRESHOLD (2.5) reads as armed.
   On the goblin, bandit and guard candidates the two armed goblins score 3.3, the
   ten empty-handed ones -2.2 to 1.5 (docs/references.md).
3. `whole`: the same margin on the whole image and four crops with "<name> holding ..."
   prompts. Recorded only: a quiver or a sheathed sword on the body pushes it up (every
   goblin candidate scores 3.7-9.2), so it does not decide.

    <comfy venv python> -I tools/unarmed_check.py --name "a goblin" ref_0.png ref_1.png ...

prints one JSON line: {"model", "threshold", "name", "candidates": [{"image", "score",
"armed", "top", "hand_source", "hands": [{"box", "score", "top"}], "whole"}]}. GPU when
there is one (~2 GB with DWPose), else CPU; ~10 s for 4 candidates including loading.
"""
import argparse
import contextlib
import json
import sys

MODEL = "google/siglip-so400m-patch14-384"
THRESHOLD = 2.5
WEAPONS = ["a dagger", "a sword", "a bow", "a crossbow", "a spear", "an axe", "a club", "a mace", "a staff",
           "a wand", "a shield", "a whip", "a weapon"]
# whole-image prompts: only used when no hand is found (see score())
ARMED = ["{name} holding {w} in its hand"]
EMPTY = ["{name} with empty open hands, holding nothing", "{name} with bare empty hands"]
# (left, top, right, bottom) as fractions of the image
CROPS = [(0, 0, 1, 1), (0, 0, 0.6, 1), (0.4, 0, 1, 1), (0, 0, 1, 0.67), (0, 0.33, 1, 1)]
HAND_ARMED = ["a hand holding {w}"]
HAND_EMPTY = ["an empty open hand holding nothing", "an empty clawed hand", "a bare hand", "a hand"]
HAND_PAD, HAND_MIN = 1.8, 0.12
AUX = "/home/narfman0/workspace/ComfyUI/custom_nodes/comfyui_controlnet_aux/src"


def pose_wrists(path: str) -> list:
    """Wrist positions (fractions of the image) in an OpenPose COCO-18 skeleton image as
    tools/pose_skeleton.py draws it: of the arm keypoint dots (colours 2-7), the one per
    side farthest from the neck dot (colour 1)."""
    import numpy as np
    from PIL import Image
    colors = [(255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0), (85, 255, 0), (0, 255, 0), (0, 255, 85)]
    a = np.array(Image.open(path).convert("RGB")).astype(int)
    dots = []
    for c in colors:
        ys, xs = np.nonzero(np.abs(a - c).sum(axis=2) < 6)
        dots.append((float(xs.mean()), float(ys.mean())) if len(xs) else None)
    if dots[0] is None:
        return []
    nx, ny = dots[0]
    out = []
    for side in (-1, 1):
        cand = [d for d in dots[1:] if d is not None and (d[0] - nx) * side > 0]
        if cand:
            x, y = max(cand, key=lambda d: (d[0] - nx) ** 2 + (d[1] - ny) ** 2)
            out.append((x / a.shape[1], y / a.shape[0]))
    return out


def hand_boxes(img, det, wrists=()) -> tuple[list, str]:
    """Square crops around each hand: DWPose's hand keypoints (HAND_PAD x their box, at
    least HAND_MIN of the image side) or its wrist; with neither, the A-pose ControlNet's
    wrists (`wrists`, fractions) at 30% of the side, else the two sides of the body at mid
    height. Returns (boxes, source)."""
    import numpy as np
    W, H = img.size
    out = []
    for po in (det.detect_poses(np.array(img)) if det is not None else []):
        body = po.body.keypoints
        for hand, wrist in ((po.right_hand, body[4]), (po.left_hand, body[7])):
            pts = [(k.x, k.y) for k in (hand or []) if k is not None and k.score > 0.3]
            if len(pts) >= 5:
                xs, ys = [x for x, _ in pts], [y for _, y in pts]
                cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
                side = max(HAND_PAD * max(max(xs) - min(xs), max(ys) - min(ys)), HAND_MIN * max(W, H))
            elif wrist is not None and wrist.score > 0.3:
                cx, cy, side = wrist.x, wrist.y, 0.25 * max(W, H)
            else:
                continue
            out.append((int(max(0, cx - side / 2)), int(max(0, cy - side / 2)),
                        int(min(W, cx + side / 2)), int(min(H, cy + side / 2))))
    if out:
        return out, "dwpose"
    if wrists:
        side = 0.3 * max(W, H)
        return [(int(max(0, x * W - side / 2)), int(max(0, y * H - side / 2)), int(min(W, x * W + side / 2)),
                 int(min(H, y * H + side / 2))) for x, y in wrists], "pose_image"
    return [(0, int(0.3 * H), W // 2, int(0.85 * H)), (W // 2, int(0.3 * H), W, int(0.85 * H))], "sides"


def prompts(name: str) -> tuple[list[str], list[str]]:
    armed = [t.format(name=name, w=w) for t in ARMED for w in WEAPONS]
    empty = [t.format(name=name) for t in EMPTY]
    return armed, empty


def score(paths: list[str], name: str, hands: bool = True, wrists=()) -> dict:
    import torch
    from PIL import Image
    from transformers import AutoModel, AutoProcessor

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if dev == "cuda" else torch.float32
    model = AutoModel.from_pretrained(MODEL, dtype=dtype).to(dev).eval()
    proc = AutoProcessor.from_pretrained(MODEL)
    det = None
    if hands:
        try:
            sys.path.insert(0, AUX)
            with contextlib.redirect_stdout(sys.stderr):
                from custom_controlnet_aux.dwpose import DwposeDetector
                det = DwposeDetector.from_pretrained("hr16/DWPose-TorchScript-BatchSize5", "yzd-v/DWPose",
                                                     det_filename=None,  # pose on the whole frame: one figure
                                                     pose_filename="dw-ll_ucoco_384_bs5.torchscript.pt",
                                                     torchscript_device=dev)
        except Exception as e:  # no DWPose: the fallback crops (pose image wrists or body sides)
            print(f"unarmed_check: DWPose unavailable ({e})", file=sys.stderr)

    def text(ts):
        tin = proc(text=ts, padding="max_length", return_tensors="pt").to(dev)
        t = model.get_text_features(**tin)
        t = getattr(t, "pooler_output", t)
        return t / t.norm(dim=-1, keepdim=True)

    def margin(crops, ta, te):
        pin = proc(images=crops, return_tensors="pt").to(dev)
        e = model.get_image_features(pixel_values=pin["pixel_values"].to(dtype))
        e = getattr(e, "pooler_output", e)
        e = e / e.norm(dim=-1, keepdim=True)
        sc, bias = model.logit_scale.exp().float(), model.logit_bias.float()
        la = (e @ ta.T).float() * sc + bias
        le = (e @ te.T).float() * sc + bias
        return (la.max(dim=1).values - le.max(dim=1).values).tolist(), la.argmax(dim=1).tolist()

    armed, empty = prompts(name)
    out = {"model": MODEL, "threshold": THRESHOLD, "name": name, "candidates": []}
    with torch.no_grad():
        ta, te = text(armed), text(empty)
        ha = text([t.format(w=w) for t in HAND_ARMED for w in WEAPONS])
        he = text(HAND_EMPTY)
        for p in paths:
            im = Image.open(p).convert("RGB")
            w, h = im.size
            crops = [im.crop((int(a * w), int(b * h), int(c * w), int(d * h))) for a, b, c, d in CROPS]
            views, _ = margin(crops, ta, te)
            c = {"image": p, "whole": round(max(views), 3)}
            if hands:
                with contextlib.redirect_stdout(sys.stderr):  # DWPose prints timings
                    boxes, c["hand_source"] = hand_boxes(im, det, wrists)
                hm, htop = margin([im.crop(b) for b in boxes], ha, he)
                c["hands"] = [{"box": list(b), "score": round(m, 3), "top": WEAPONS[t]}
                              for b, m, t in zip(boxes, hm, htop)]
                best = max(c["hands"], key=lambda x: x["score"])
                c.update(score=best["score"], top=best["top"])
            else:
                c.update(score=c["whole"], hand_source="whole")
            c["armed"] = c["score"] > THRESHOLD
            out["candidates"].append(c)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="+")
    ap.add_argument("--name", default="a fantasy character", help="subject words for the prompts")
    ap.add_argument("--pose-image", help="the A-pose ControlNet image the refs were drawn with (wrist fallback)")
    ap.add_argument("--no-hands", action="store_true", help="whole-image views only")
    a = ap.parse_args()
    wrists = pose_wrists(a.pose_image) if a.pose_image else []
    print(json.dumps(score(a.images, a.name, not a.no_hands, wrists)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
