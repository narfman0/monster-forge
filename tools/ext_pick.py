#!/usr/bin/env python3
"""Pick the best candidate per character from ext_search.py's results and write curation/<src>/external.json.

  python3 tools/ext_pick.py --source <src> [ids] [--uid <char>=<uid8> ...] [--force]

Rule: a skin with >= 12 humanoid joints, at least one texture image, <= 250k triangles; the first such
candidate in ext_search's order (name match, skeleton, textures, size). Entries already in external.json are kept
unless --force; --uid <char>=<uid prefix> chooses a specific candidate by hand. tri_budget: 40k (the stock budget).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sources  # noqa: E402

CANDS = Path("~/.cache/mf-ext/cands").expanduser()


def good(c: dict) -> bool:
    h = c.get("hdr") or {}
    return bool(h and h["skins"] >= 1 and h["humanoid_joints"] >= 12 and h["images"] >= 1 and c["tris"] <= 250000)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--uid", action="append", default=[])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("ids", nargs="*")
    a = ap.parse_args()
    hand = dict(x.split("=", 1) for x in a.uid)
    cands = json.loads((CANDS / f"{a.source}.json").read_text())
    path = sources.extra_dir() / "curation" / a.source / "external.json"
    table = json.loads(path.read_text()) if path.exists() else {}
    for cid, cs in cands.items():
        if (a.ids and cid not in a.ids) or (cid in table and not a.force and cid not in hand):
            continue
        pick = next((c for c in cs if c["uid"].startswith(hand[cid])), None) if cid in hand else next((c for c in cs if good(c)), None)
        if not pick:
            print(f"{cid:22} no usable candidate")
            continue
        table[cid] = {"provider": "sketchfab", "uid": pick["uid"], "title": pick["name"], "author": pick["author"], "url": pick["url"],
                      "license": pick["license"], "tri_budget": 40000}
        print(f"{cid:22} {pick['name'][:46]:46} {pick['tris']:>7}t  [{pick['license'][:12]}] {pick['author']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(table, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
