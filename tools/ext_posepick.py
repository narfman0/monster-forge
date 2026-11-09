#!/usr/bin/env python3
"""Pick REST or POSE for an imported model by measuring the result, and record it in curation/<src>/external.json.

  python3 tools/ext_posepick.py --source <src> [ids] [--all]

Which Blender pose matches a skinned glTF depends on how the file was exported, and the bounding-box heuristics in
blender_ext_common.choose_pose cannot always tell. So: rig the record under REST and under POSE (tools/ext_rig.py),
read the worst clip's edge_stretch_p999 from anims_ual/metrics.json, keep the pose that stretches less and re-rig
with it. ext_rig.py needs PIL_PYTHON set (a Python with Pillow and numpy). Records that already have a "pose" in external.json are skipped unless --all. A pose that fails to rig
counts as infinitely stretched. Prints one line per record: id, REST max, POSE max, chosen.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge  # noqa: E402
import sources  # noqa: E402

HERE = Path(__file__).resolve().parent


def worst(src: str, cid: str) -> float:
    try:
        clips = json.loads((forge.OUT_ROOT / src / cid / "anims_ual" / "metrics.json").read_text())["clips"]
        return max(v["edge_stretch_p999"] for v in clips.values())
    except Exception:
        return float("inf")


def rig(src: str, cid: str) -> float:
    d = forge.OUT_ROOT / src / cid / "anims_ual" / "metrics.json"
    if d.exists():
        d.unlink()  # a failed rig must not leave the previous pose's numbers behind
    r = subprocess.run([sys.executable, str(HERE / "ext_rig.py"), "--source", src, cid], capture_output=True, text=True)
    if r.returncode:
        print(f"   {cid}: rig failed: {(r.stderr or r.stdout).strip().splitlines()[-1][:140] if (r.stderr or r.stdout).strip() else ''}", file=sys.stderr)
    return worst(src, cid)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("ids", nargs="*")
    a = ap.parse_args()
    path = sources.extra_dir() / "curation" / a.source / "external.json"
    table = json.loads(path.read_text())
    for cid in a.ids or list(table):
        if cid not in table or (table[cid].get("pose") and not a.all):
            continue
        res = {}
        for pose in ("REST", "POSE"):
            table[cid]["pose"] = pose
            path.write_text(json.dumps(table, indent=1, ensure_ascii=False) + "\n")
            res[pose] = rig(a.source, cid)
        best = min(res, key=lambda k: (res[k], k != "POSE"))
        table[cid]["pose"] = best
        path.write_text(json.dumps(table, indent=1, ensure_ascii=False) + "\n")
        if res["REST"] != res["POSE"] or best != "POSE":
            rig(a.source, cid)  # the last run was POSE; redo only when REST won
        print(f"{cid:24} REST {res['REST']:7.2f}  POSE {res['POSE']:7.2f}  -> {best}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
