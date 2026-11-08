"""Render the OpenPose (COCO-18) A-pose skeletons used by data/style.json: front view, standard
openpose colours. Needs Pillow:  python -I tools/pose_skeleton.py [outdir=workflows/pose]

apose_openpose*.png: arms 35 degrees from the body, feet under the hips (the house A-pose).
apose_wide_openpose*.png (2026-11-08, docs/references.md "Rig-friendly poses"): arms 45 degrees,
legs splayed so the feet stand well apart (image-to-3D fuses parts that touch in the picture).
`python -I tools/pose_skeleton.py <outdir> --keypoints <file.json> <out.png>` draws a skeleton from
projected keypoints (tools/blender_ref_templates.py writes them beside its biped depth maps).
"""
import json, math, sys
from PIL import Image, ImageDraw
W = 1024
cx = 512
def mk(top, foot, sh_w, hip_w, arm_spread, foot_out=0.0):
    H = foot - top
    y = lambda f: top + f * H
    k = {0: (cx, y(0.08)), 1: (cx, y(0.19)), 2: (cx - sh_w, y(0.205)), 5: (cx + sh_w, y(0.205)),
         8: (cx - hip_w, y(0.52)), 11: (cx + hip_w, y(0.52)),
         9: (cx - hip_w - 4 - foot_out * H * 0.5, y(0.76)), 12: (cx + hip_w + 4 + foot_out * H * 0.5, y(0.76)),
         10: (cx - hip_w - 8 - foot_out * H, y(0.98)), 13: (cx + hip_w + 8 + foot_out * H, y(0.98)),
         14: (cx - 18, y(0.06)), 15: (cx + 18, y(0.06)), 16: (cx - 40, y(0.075)), 17: (cx + 40, y(0.075))}
    # A-pose: arms ~35 degrees from the body
    ang = math.radians(arm_spread); L = 0.17 * H
    for s, (sh, el, wr) in ((-1, (2, 3, 4)), (1, (5, 6, 7))):
        x0, y0 = k[sh]
        k[el] = (x0 + s * L * math.sin(ang), y0 + L * math.cos(ang))
        k[wr] = (x0 + s * 2 * L * math.sin(ang), y0 + 2 * L * math.cos(ang))
    return k
limbs = [[2,3],[2,6],[3,4],[4,5],[6,7],[7,8],[2,9],[9,10],[10,11],[2,12],[12,13],[13,14],[2,1],[1,15],[15,17],[1,16],[16,18]]
colors = [[255,0,0],[255,85,0],[255,170,0],[255,255,0],[170,255,0],[85,255,0],[0,255,0],[0,255,85],[0,255,170],[0,255,255],[0,170,255],[0,85,255],[0,0,255],[85,0,255],[170,0,255],[255,0,255],[255,0,170],[255,0,85]]
def draw(k, path, sw=9):
    img = Image.new("RGB", (W, W), "black"); d = ImageDraw.Draw(img, "RGBA")
    for i, (a, b) in enumerate(limbs):
        (x1, y1), (x2, y2) = k[a-1], k[b-1]
        mx, my = (x1+x2)/2, (y1+y2)/2; length = math.hypot(x2-x1, y2-y1); ang = math.atan2(y2-y1, x2-x1)
        pts = []
        for t in range(0, 360, 10):
            r = math.radians(t); ex, ey = length/2*math.cos(r), sw*math.sin(r)
            pts.append((mx + ex*math.cos(ang) - ey*math.sin(ang), my + ex*math.sin(ang) + ey*math.cos(ang)))
        c = colors[i]; d.polygon(pts, fill=(int(c[0]*.6), int(c[1]*.6), int(c[2]*.6)))
    for i in range(18):
        x, y = k[i]; d.ellipse([x-8, y-8, x+8, y+8], fill=tuple(colors[i]))
    img.save(path)
out = sys.argv[1] if len(sys.argv) > 1 else "workflows/pose"
if "--keypoints" in sys.argv:  # a skeleton from projected keypoints {"0": [x, y], ...} (pixels)
    i = sys.argv.index("--keypoints")
    kp = {int(k): tuple(v) for k, v in json.load(open(sys.argv[i + 1])).items()}
    draw(kp, sys.argv[i + 2])
    sys.exit(0)
draw(mk(110, 950, 85, 50, 35), out + "/apose_openpose.png")
draw(mk(250, 950, 95, 55, 35), out + "/apose_openpose_small.png")  # stocky, shorter (small creatures)
# rig-friendly: arms 45 degrees, feet apart (each ankle 0.08 x height further out than the house pose)
draw(mk(110, 950, 85, 50, 45, 0.08), out + "/apose_wide_openpose.png")
draw(mk(250, 950, 95, 55, 45, 0.08), out + "/apose_wide_openpose_small.png")
