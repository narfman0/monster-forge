#!/usr/bin/env python3
"""Turn rigged external models to face +Y (the way the SRD monsters and the viewer expect).

  PIL_PYTHON=... python3 tools/ext_facing.py --source <src> [ids] [--dry]

QA's automatic facing looks at the static mesh and is wrong for some rips (a character that stands on its side
in the viewer). The rigged model knows better: the toes point where the feet point. For each record this
measures foot->ball in model_rigged.glb (Blender, headless), and when the toes are more than 25 degrees off +Y it
adds the missing turn to the QA yaw (forge.py qa --yaw N --force) and re-rigs with tools/ext_rig.py. Records
whose feet have no foot/ball bones are skipped and reported.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge  # noqa: E402

HERE = Path(__file__).resolve().parent
PROBE = """
import sys, bpy, json
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=sys.argv[-1])
a = next(o for o in bpy.data.objects if o.type == "ARMATURE"); M = a.matrix_world
v = []
for s in "lr":
    f, b = a.pose.bones.get("foot_" + s), a.pose.bones.get("ball_" + s)
    if f and b:
        d = (M @ b.head) - (M @ f.head); v.append([d.x, d.y])
print("TOES" + json.dumps(v))
"""


def toes(glb: Path) -> list[list[float]]:
    probe = Path(os.environ.get("TMPDIR", "/tmp")) / "ext_facing_probe.py"
    probe.write_text(PROBE)
    r = subprocess.run([forge.BLENDER, "-b", "--factory-startup", "-P", str(probe), "--", str(glb)], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.startswith("TOES"):
            return json.loads(line[4:])
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("ids", nargs="*")
    a = ap.parse_args()
    for d in sorted((forge.OUT_ROOT / a.source).iterdir()):
        cid = d.name
        if cid.startswith("_") or (a.ids and cid not in a.ids) or not (d / "model_rigged.glb").exists():
            continue
        t = toes(d / "model_rigged.glb")
        if not t:
            print(f"{cid:24} no foot/ball bones, skipped")
            continue
        x, y = (sum(p[i] for p in t) / len(t) for i in (0, 1))
        delta = (90.0 - math.degrees(math.atan2(y, x)) + 180.0) % 360.0 - 180.0
        if abs(delta) <= 25:
            print(f"{cid:24} faces +Y ({delta:+.0f} deg off), kept")
            continue
        delta = round(delta / 5.0) * 5.0
        qa = json.loads((d / "qa.json").read_text())
        yaw = (float(qa.get("yaw_deg") or 0.0) + delta) % 360.0
        print(f"{cid:24} toes at {math.degrees(math.atan2(y, x)):+.0f} deg: yaw {qa.get('yaw_deg')} -> {yaw:.0f}", flush=True)
        if a.dry:
            continue
        env = dict(os.environ)
        subprocess.run([sys.executable, str(HERE / "forge.py"), "qa", cid, "--source", a.source, "--yaw", str(yaw), "--force"], env=env, capture_output=True)
        subprocess.run([sys.executable, str(HERE / "ext_rig.py"), "--source", a.source, cid], env=env, capture_output=True)
        t2 = toes(d / "model_rigged.glb")
        if t2:
            x2, y2 = (sum(p[i] for p in t2) / len(t2) for i in (0, 1))
            print(f"{'':24} now {math.degrees(math.atan2(y2, x2)):+.0f} deg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
