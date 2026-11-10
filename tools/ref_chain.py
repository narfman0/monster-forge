#!/usr/bin/env python3
"""Whole generated-character chain for one record, from a picked reference to a promoted, rigged model.

  ~/.cache/mf-venv/bin/python tools/ref_chain.py <src> <id> --pick N [--tex 4096] [--skip-rig]

Steps (each logs; a failure stops with a non-zero exit):
  1. (--web: ref_web.png, a picked 2D image, redrawn as a front A-pose) ref_N.png -> ref.png, Qwen-Image-Edit redraws it clean on white (tools/ref_views.py --views clean) -> ref.png
  2. back, left, right views from that front (tools/ref_views.py), framed into turnaround/
  3. multi-view mesh (forge.py mesh --mesh-views mv; fails if manifest mesh.views != "mv") + qa
  4. texture projection from the clean views (tools/ref_project.py), a Qwen redraw of the head crop for a sharp face (tools/ref_head.py), re-project, lod
  5. SkinTokens rig on the GPU box (rsync out/<src>/<id>/ to $RIG_HOST:workspace/monster-forge, run forge.py rig there, rsync back),
     then forge.py rig --rig-source ual with the skintokens fit and weights; promote
Env: COMFY_URL (the GPU box's ComfyUI), PIL_PYTHON (venv python), RIG_HOST (default 9900x). Run from the repo root.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = os.environ.get("PIL_PYTHON") or sys.executable


def run(args: list[str], env: dict | None = None, tail: int = 3) -> str:
    e = {**os.environ, **(env or {})}
    r = subprocess.run(args, cwd=ROOT, env=e, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    out = (r.stdout + r.stderr).replace("\x1b", "")
    lines = [ln for ln in out.splitlines() if ln.strip() and "opencolor" not in ln.lower()]
    if r.returncode:
        print("\n".join(lines[-12:]))
        raise SystemExit(f"FAILED ({r.returncode}): {' '.join(args)[:160]}")
    return "\n".join(lines[-tail:])


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("id")
    ap.add_argument("--pick", type=int)
    ap.add_argument("--web", action="store_true", help="start from out/<src>/<id>/ref_web.png (a picked 2D image from tools/ref_web.py) instead of a generated ref_N.png")
    ap.add_argument("--tex", default="4096")
    ap.add_argument("--skip-rig", action="store_true")
    a = ap.parse_args()
    d = ROOT / "out" / a.src / a.id
    forge = [PY, "tools/forge.py"]
    t0 = time.time()
    first = "apose" if a.web else "clean"
    shutil.copyfile(d / ("ref_web.png" if a.web else f"ref_{a.pick}.png"), d / "ref.png")  # ref_views frames against ref.png
    run([PY, "tools/ref_views.py", a.id, "--source", a.src, "--views", first])
    shutil.copyfile(d / "views" / f"{first}.png", d / "ref.png")
    log(f"{a.id}: reference cleaned")
    for v in ("back", "left", "right"):
        (d / "views" / f"{v}.png").unlink(missing_ok=True)
    shutil.rmtree(d / "turnaround", ignore_errors=True)
    run([PY, "tools/ref_views.py", a.id, "--source", a.src, "--views", "back,left,right"])
    log(f"{a.id}: views done")
    run(forge + ["mesh", a.id, "--source", a.src, "--mesh-views", "mv", "--force"])
    man = json.loads((d / "manifest.json").read_text())
    tj = json.loads((d / "turnaround" / "turnaround.json").read_text())
    if tj.get("source") != "ref_views.py (Qwen-Image-Edit)" or "back" not in tj.get("views", {}):
        raise SystemExit(f"{a.id}: the mesh stage replaced the Qwen turnaround (source {tj.get('source')}, views {list(tj.get('views', {}))}); "
                         "check that the override has no ref_pick (forge.py mesh copies ref_<pick>.png over the cleaned ref.png)")
    if man["mesh"].get("views") != "mv":
        raise SystemExit(f"{a.id}: mesh is not multi-view ({man['mesh'].get('views')}, {str((man['mesh'].get('turnaround') or {}).get('error'))[:200]})")
    run(forge + ["qa", a.id, "--source", a.src])
    log(f"{a.id}: mesh + qa done, {man['mesh'].get('faces', '?')} faces")
    run([PY, "tools/ref_project.py", a.id, "--source", a.src], env={"TEX_SIZE": a.tex})
    for stale in ("head_gen_final.png", "head_gen_qwen.png", "head_weight_final.png", "head_shift.json"):
        (d / "paint" / stale).unlink(missing_ok=True)
    run([PY, "tools/ref_head.py", a.src, a.id])  # sharp face: redraw of the head crop, aligned geometrically
    run([PY, "tools/ref_project.py", a.id, "--source", a.src], env={"TEX_SIZE": a.tex})
    run(forge + ["lod", a.id, "--source", a.src, "--force"])
    log(f"{a.id}: textured, lod done")
    if not a.skip_rig:
        host = os.environ.get("RIG_HOST", "9900x")
        rel = f"out/{a.src}/{a.id}/"
        for stale in ("rig", "rig_ual", "anims_ual", "anims"):
            shutil.rmtree(d / stale, ignore_errors=True)
        subprocess.run(["ssh", "-n", "-o", "BatchMode=yes", host, f"mkdir -p ~/workspace/monster-forge/{rel}"], check=True)
        subprocess.run(["rsync", "-a", "--delete", rel, f"{host}:workspace/monster-forge/{rel}"], cwd=ROOT, check=True)
        r = subprocess.run(["ssh", "-n", "-o", "BatchMode=yes", host, f"cd ~/workspace/monster-forge && git pull -q --ff-only; cd ../monster-forge-nonsrd && git pull -q --ff-only; "
                            f"cd ../monster-forge && COMFY_URL=http://localhost:8188 PIL_PYTHON=python3 python3 -I tools/forge.py rig {a.id} --source {a.src} --force 2>&1 | tail -2"],
                           capture_output=True, text=True)
        log(f"{a.id}: skintokens: {(r.stdout or r.stderr).strip()[-200:]}")
        subprocess.run(["rsync", "-a", f"{host}:workspace/monster-forge/{rel}", rel], cwd=ROOT, check=True)
        run(forge + ["rig", a.id, "--source", a.src, "--rig-source", "ual", "--force"])
        run(forge + ["promote", a.id, "--source", a.src])
        m = json.loads((d / "anims_ual" / "metrics.json").read_text())["clips"]
        log(f"{a.id}: rigged, worst clip stretch {max(c['edge_stretch_p999'] for c in m.values()):.1f}")
    log(f"{a.id}: chain done in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
