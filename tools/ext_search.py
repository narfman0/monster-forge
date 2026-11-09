#!/usr/bin/env python3
"""Find and rank downloadable Sketchfab models for a roster of characters.

  python3 tools/ext_search.py --source <src> [ids] [--per 4] [--no-download]

Reads curation/<src>/roster.json from the private extra repo ({series, characters: {id: {name, queries?}}}),
searches Sketchfab (downloadable models only), keeps the best few per character, downloads them to
~/.cache/mf-ext/sketchfab/ (same cache as ext_import.py) and inspects each GLB header (triangles, skin joints,
textures, humanoid-looking bone names). Writes ~/.cache/mf-ext/cands/<src>.json: {id: [candidate, ...]} best first.
Ranking favours: a skeleton with a humanoid joint set, textures, 5k-120k triangles, name match, likes.
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge  # noqa: E402,F401
import sources  # noqa: E402
from ext_import import fetch_glb  # noqa: E402

API = "https://api.sketchfab.com/v3/search"
OUT = Path("~/.cache/mf-ext/cands").expanduser()
PROP = re.compile(r"(?i)\b(sword|katana|nichirin|blade|statue|gun|rifle|pistol|weapon|book of|keyblade|diorama|logo|cosplay|bust|figure|poster|room|house|ship|ring|hammer|axe|spear|fighter|aircraft|plane|jet fighter|wallpaper|map|scene|stage)\b|'s (sword|katana|nichirin)")
HUMANOID = re.compile(r"(?i)hip|pelvis|spine|chest|neck|head|shoulder|collar|clavicle|upper_?arm|fore_?arm|elbow|wrist|hand|thigh|knee|calf|foot|ankle|\barm\b|leg")


def get(url: str):
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "monster-forge"}), timeout=40) as r:
                return json.load(r)
        except Exception:
            time.sleep(2 + 3 * i)
    return {}


def search(q: str, n: int = 24) -> list:
    d = get(f"{API}?{urllib.parse.urlencode({'type': 'models', 'q': q, 'downloadable': 'true', 'count': n})}")
    return d.get("results", [])


def header(path: Path) -> dict:
    b = path.read_bytes()
    n = struct.unpack_from("<I", b, 12)[0]
    g = json.loads(b[20:20 + n])
    skins = g.get("skins", [])
    names = [g["nodes"][j].get("name", "") for s in skins for j in s["joints"]] if skins else []
    hum = sum(1 for x in names if HUMANOID.search(x))
    return {"skins": len(skins), "joints": max((len(s["joints"]) for s in skins), default=0), "humanoid_joints": hum,
            "images": len(g.get("images", [])), "materials": len(g.get("materials", [])), "meshes": len(g.get("meshes", []))}


def score(c: dict, tokens: list) -> float:
    s = 0.0
    title = c["name"].lower()
    s += 3.0 * sum(1 for t in tokens if t in title) / max(len(tokens), 1)
    t = c["tris"]
    s += 1.0 if 5000 <= t <= 120000 else (0.3 if t <= 400000 else -1)
    s += min(c.get("likes", 0), 200) / 200
    h = c.get("hdr") or {}
    if h:
        s += 2.5 if h["humanoid_joints"] >= 12 else (0.8 if h["skins"] else 0)
        s += 1.0 if h["images"] else 0
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--per", type=int, default=4)
    ap.add_argument("--dl", type=int, default=3, help="download the top N per character")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("ids", nargs="*")
    a = ap.parse_args()
    ro = json.loads(sources.curation_path(a.source, "roster").read_text())
    series = ro["series"]
    OUT.mkdir(parents=True, exist_ok=True)
    outp = OUT / f"{a.source}.json"
    res = json.loads(outp.read_text()) if outp.exists() else {}
    for cid, ch in ro["characters"].items():
        if a.ids and cid not in a.ids:
            continue
        qs = ch.get("queries") or [f"{ch['name']}", f"{ch['name']} {series}"]
        tokens = [t for t in re.findall(r"[a-z]+", ch["name"].lower()) if len(t) > 2]
        seen, cands = set(), []
        for q in qs:
            for r in search(q):
                uid = r["uid"]
                if uid in seen:
                    continue
                seen.add(uid)
                lic = (r.get("license") or {}).get("label") or ""
                tags = " ".join(t.get("name", "") for t in r.get("tags", [])).lower()
                cands.append({"uid": uid, "tags": tags[:300], "name": r["name"], "author": (r.get("user") or {}).get("displayName"), "license": lic,
                              "tris": r.get("faceCount", 0), "likes": r.get("likeCount", 0), "anim": r.get("animationCount", 0),
                              "url": r.get("viewerUrl")})
        sm = [w.lower() for w in ro.get("series_match", [])]
        need = [m.lower() for m in ch.get("match", [])] or tokens[:2]

        def ok(c):
            name, hay = c["name"].lower(), c["name"].lower() + " " + c["tags"]
            if PROP.search(c["name"]) or not all(t in name or t in c["tags"] for t in need):
                return False
            full = len(tokens) > 1 and all(t in name for t in tokens)
            return full or not sm or any(w in hay for w in sm)
        cands = [c for c in cands if ok(c)]
        cands.sort(key=lambda c: -score(c, tokens))
        cands = cands[:a.per + 2]
        if not a.no_download:
            for c in cands[:a.dl]:
                try:
                    p = fetch_glb(c["uid"])
                    c["file"] = str(p)
                    c["hdr"] = header(p)
                except Exception as e:  # noqa: BLE001
                    c["err"] = str(e)[:80]
            cands = [c for c in cands if "err" not in c or True]
            cands.sort(key=lambda c: -score(c, tokens))
        res[cid] = cands[:a.per]
        top = res[cid][0] if res[cid] else None
        print(f"{cid:20} {len(seen):3} hits {len(cands):2} named | best: " + (f"{top['name'][:38]} {top['tris']}t sk={top.get('hdr', {}).get('skins')} j={top.get('hdr', {}).get('humanoid_joints')} img={top.get('hdr', {}).get('images')} [{top['license'][:12]}]" if top else "NONE"), flush=True)
        outp.write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
