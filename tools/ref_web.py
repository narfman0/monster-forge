#!/usr/bin/env python3
"""Collect 2D reference candidates for a character from the series' Fandom wiki (official art) and Safebooru (fan art).

  ~/.cache/mf-venv/bin/python tools/ref_web.py <src> <id> --wiki <host> --title "<wiki page>" --tag <booru_tag> [--n 8]
  ~/.cache/mf-venv/bin/python tools/ref_web.py <src> <id> --pick <key>      # copy a candidate (e.g. w1, b0) to ref_web.png

Candidates land in out/<src>/<id>/web/ (images, web.json, sheet.png with a label per image). The wiki side lists the page's files,
keeps png/jpg/webp whose title does not look like a screenshot or icon, and adds the page thumbnail; the booru side queries
`<tag> solo standing full_body`. Images are other people's work: personal, LAN-only use, never in the public repo or viewer.
Needs curl, Pillow.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge  # noqa: E402

UA = "Mozilla/5.0 (X11; Linux x86_64) monster-forge-personal"
BOORU = "https://safebooru.org/index.php?page=dapi&s=post&q=index&json=1&limit=100&tags="
GOOD = re.compile(r"(full|body|design|model|render|official|character|main|infobox|anime|manga|colou?r|portrait|art|profile|appearance|outfit|cover|new|final)", re.I)
BAD = re.compile(r"(icon|logo|flag|symbol|stub|signature|chapter|episode|screenshot|screencap|vlcsnap|sword|tsuba|map|banner|gallery|chibi|cosplay|figure|merch|funko|nendoroid|card|puzzle)", re.I)


def curl_json(url: str) -> dict | list:
    r = subprocess.run(["curl", "-s", "-m", "40", "-A", UA, url], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def wiki_images(host: str, title: str, limit: int = 8) -> list[dict]:
    def q(**k: str) -> str:
        return f"https://{host}/api.php?" + urllib.parse.urlencode({"format": "json", **k})
    d = curl_json(q(action="query", titles=title, prop="images", imlimit="200"))
    pages = d.get("query", {}).get("pages", {}) if isinstance(d, dict) else {}
    names = [i["title"] for p in pages.values() for i in p.get("images", [])]
    names = [n for n in names if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) and not BAD.search(n)]
    ranked = sorted(names, key=lambda n: (not GOOD.search(n), n))[:30]
    out = []
    th = curl_json(q(action="query", titles=title, prop="pageimages", pithumbsize="1600"))
    for p in (th.get("query", {}).get("pages", {}) if isinstance(th, dict) else {}).values():
        if p.get("thumbnail"):
            out.append({"name": "wiki-thumb", "url": p["thumbnail"]["source"], "w": p["thumbnail"]["width"], "h": p["thumbnail"]["height"], "kind": "wiki"})
    for i in range(0, len(ranked), 10):
        d = curl_json(q(action="query", titles="|".join(ranked[i:i + 10]), prop="imageinfo", iiprop="url|size"))
        for p in (d.get("query", {}).get("pages", {}) if isinstance(d, dict) else {}).values():
            ii = (p.get("imageinfo") or [{}])[0]
            if ii.get("url") and ii.get("height", 0) >= 500:
                out.append({"name": p["title"][5:], "url": ii["url"], "w": ii["width"], "h": ii["height"], "kind": "wiki"})
    tall = [o for o in out if o["h"] >= 1.2 * o["w"]]
    return (tall + [o for o in out if o["h"] < 1.2 * o["w"]][:2])[:limit]


def booru_images(tag: str, limit: int = 8) -> list[dict]:
    d = curl_json(BOORU + urllib.parse.quote(f"{tag} solo standing full_body").replace("%20", "+"))
    d = d if isinstance(d, list) else []
    bad = {"comic", "chibi", "cosplay", "photo", "monochrome", "sketch"}
    d = [p for p in d if p["height"] >= 900 and p["height"] >= 1.2 * p["width"] and not bad & set(p["tags"].split())]
    d.sort(key=lambda p: (bool({"white_background", "simple_background"} & set(p["tags"].split())), -abs(p["height"] / p["width"] - 1.7)), reverse=True)
    return [{"name": f"booru-{p['id']}", "url": p["sample_url"] if p.get("sample") and p.get("sample_height", 0) >= 1400 else p["file_url"],
             "w": p["width"], "h": p["height"], "kind": "booru"} for p in d[:limit]]


def main() -> int:
    from PIL import Image, ImageDraw
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("id")
    ap.add_argument("--wiki")
    ap.add_argument("--title")
    ap.add_argument("--tag")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--pick")
    a = ap.parse_args()
    d = forge.OUT_ROOT / a.src / a.id / "web"
    d.mkdir(parents=True, exist_ok=True)
    if a.pick:
        im = Image.open(d / f"{a.pick}.png").convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        bg.convert("RGB").save(d.parent / "ref_web.png")
        print(f"{a.id}: {a.pick} -> ref_web.png {im.size}")
        return 0
    cands = []
    if a.wiki and a.title:
        cands += wiki_images(a.wiki, a.title, a.n)
    if a.tag:
        cands += booru_images(a.tag, a.n)
    meta = []
    for i, c in enumerate(cands):
        out = d / f"{c['kind'][0]}{i}.png"
        tmp = Path(str(out) + ".dl")
        cmd = ["curl", "-s", "-L", "-m", "60", "-A", UA] + (["-e", "https://safebooru.org/"] if c["kind"] == "booru" else []) + ["-o", str(tmp), c["url"]]
        subprocess.run(cmd)
        try:
            im = Image.open(tmp)
            im.load()
            im.save(out)
        except Exception:  # noqa: BLE001
            continue
        finally:
            tmp.unlink(missing_ok=True)
        meta.append({**c, "file": out.name, "key": out.stem})
    (d / "web.json").write_text(json.dumps(meta, indent=1) + "\n")
    cols, S = 6, (230, 340)
    rows = max(1, (len(meta) + cols - 1) // cols)
    W = Image.new("RGB", (cols * S[0], rows * (S[1] + 16)), (255, 255, 255))
    dr = ImageDraw.Draw(W)
    for i, m in enumerate(meta):
        im = Image.open(d / m["file"]).convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        im = bg.convert("RGB")
        im.thumbnail(S)
        x, y = (i % cols) * S[0], (i // cols) * (S[1] + 16)
        W.paste(im, (x + (S[0] - im.width) // 2, y + 16))
        dr.text((x + 3, y + 2), f"{m['key']} {m['w']}x{m['h']} {m['name'][:20]}", fill=(255, 0, 0))
    W.save(d / "sheet.png")
    print(f"{a.id}: {len(meta)} candidates -> {d / 'sheet.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
