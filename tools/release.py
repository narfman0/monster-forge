#!/usr/bin/env python3
"""Set (or clear) the release gate of monsters: `"released": true` in
out/<source>/<id>/manifest.json. tools/publish.py ships only released monsters.

    python3 -I tools/release.py goblin-warrior wolf          # mark released
    python3 -I tools/release.py goblin-warrior --unset       # take it out of the next publish
    python3 -I tools/release.py --list                       # show what is released

Release a monster once it is through rig + anim (and the Godot import check). Nothing
else in the manifest changes; forge.py keeps the flag across re-runs, so re-check a
monster after re-running its stages and --unset it if it got worse.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT / "out"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="monster ids")
    ap.add_argument("--source", default="srd-5.2.1")
    ap.add_argument("--unset", action="store_true", help="remove the flag instead of setting it")
    ap.add_argument("--list", action="store_true", help="list released monsters and exit")
    args = ap.parse_args()
    base = OUT_ROOT / args.source
    if args.list:
        for p in sorted(base.glob("*/manifest.json")):
            try:
                if json.loads(p.read_text()).get("released") is True:
                    print(p.parent.name)
            except (OSError, ValueError):
                pass
        return 0
    if not args.ids:
        ap.error("give monster ids (or --list)")
    rc = 0
    for mid in args.ids:
        p = base / mid / "manifest.json"
        if not p.is_file():
            print(f"{mid}: no {p.relative_to(ROOT)}", file=sys.stderr)
            rc = 1
            continue
        man = json.loads(p.read_text())
        if args.unset:
            man.pop("released", None)
        else:
            man["released"] = True
        tmp = p.with_suffix(".json.tmp")  # same format and atomic replace as forge.py save_manifest
        tmp.write_text(json.dumps(man, indent=2, sort_keys=False) + "\n")
        tmp.replace(p)
        print(f"{mid}: {'unreleased' if args.unset else 'released'}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
