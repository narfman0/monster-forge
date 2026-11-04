#!/usr/bin/env python3
"""Build the public static viewer (GitHub Pages) from the released monsters.

    python3 -I tools/publish.py                         # -> ../monster-forge-viewer
    python3 -I tools/publish.py --dest /tmp/site --clean
    python3 -m http.server 8000 --directory ../monster-forge-viewer   # look at it

A monster is released when its out/<source>/<id>/manifest.json has `"released": true`
(tools/release.py sets it). For each released monster this copies the game files
(model_rigged.glb or model.glb, the rigged LODs, a lods.json naming them, anims/*.glb,
thumb.png and a trimmed manifest.json) to <dest>/<source>/<id>/, the props the released
monsters wield (out/props/<id>/model.glb + LODs + a trimmed manifest) to <dest>/props/<id>/,
and bakes <dest>/index.json with tools/viewer.py's build_index (same per-entry shape as the
dev /api/index.json, dev-only fields stripped) plus <dest>/props.json. It copies viewer/
(index.html with a static-mode marker, app.js, style.css, vendor/) and writes a README with
the CC-BY-4.0 SRD attribution from README.md.

Never deletes anything outside <dest>. --clean empties <dest> first, keeping .git, CNAME
and .github; it refuses a non-empty directory that was not made by this script.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import viewer  # noqa: E402  (tools/viewer.py: build_index / stage_status, the dev index logic)

ROOT = viewer.ROOT
OUT_ROOT = viewer.OUT_ROOT
VIEWER = viewer.VIEWER
MARKER = ".monster-forge-publish"
KEEP_ON_CLEAN = {".git", "CNAME", ".github", MARKER}
GH_FILE_LIMIT = 100 * 1024 * 1024   # GitHub refuses files over 100 MB
GH_SITE_LIMIT = 1024 * 1024 * 1024  # Pages: published site up to ~1 GB
# Static-mode marker injected into the published index.html (viewer/app.js looks for it, so
# the public page never asks for /api/).
STATIC_META = '<meta name="monster-forge-static" content="index.json">'
# Per-entry fields of the dev index that only make sense next to the repo: dropped.
DEV_ONLY = ("has_override", "job", "hi_faces", "errors")
DEV_ONLY_FILES = ("hi", "hi_textured", "hi_bytes")


def attribution() -> list[str]:
    """The SRD attribution and trademark paragraphs from README.md 'License and attribution'
    (the blockquote and everything after it), as plain-text paragraphs."""
    text = (ROOT / "README.md").read_text()
    m = re.search(r"^## License and attribution\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not m:
        raise SystemExit("README.md has no '## License and attribution' section")
    paras = [p.strip() for p in m.group(1).strip().split("\n\n") if p.strip()]
    quote = next((i for i, p in enumerate(paras) if p.startswith(">")), None)
    if quote is None:
        raise SystemExit("README.md attribution blockquote not found")
    out = []
    for p in paras[quote:]:
        p = " ".join(line.lstrip("> ").strip() for line in p.splitlines())
        p = re.sub(r"\s*\(see `[^`]*`\)", "", p).replace("`", "")
        out.append(p)
    return out


def trim_manifest(man: dict) -> dict:
    """What the public page shows: identity, sizes, LOD tris, rig sockets, clip list."""
    out = {k: man[k] for k in ("id", "name", "source", "size", "body_plan", "scale_m", "tri_count",
                                "has_skeleton", "released") if k in man}
    qa = man.get("qa") if isinstance(man.get("qa"), dict) else {}
    if qa.get("dims_m"):
        out["qa"] = {"dims_m": qa["dims_m"]}
    lod = man.get("lod") if isinstance(man.get("lod"), dict) else {}
    if lod:
        out["lod"] = {k: lod[k] for k in ("ratios", "tris") if k in lod}
    rig = man.get("rig") if isinstance(man.get("rig"), dict) else {}
    if rig:
        out["rig"] = {k: rig[k] for k in ("tool", "joints", "sockets") if k in rig}
    anim = man.get("anim") if isinstance(man.get("anim"), dict) else {}
    if anim:
        a = {k: anim[k] for k in ("clips", "missing", "embedded_clips") if k in anim}
        for c in anim.get("clips") or []:
            if isinstance(anim.get(c), dict):
                a[c] = {k: anim[c][k] for k in ("seconds", "frames", "loop") if k in anim[c]}
        out["anim"] = a
    return out


def trim_prop_manifest(man: dict) -> dict:
    out = {k: man[k] for k in ("id", "name", "category", "grip", "length_m", "grip_socket", "stow_socket",
                                "tri_count") if k in man}
    prop = man.get("prop") if isinstance(man.get("prop"), dict) else {}
    if prop.get("grip_offset"):
        out["prop"] = {"grip_offset": prop["grip_offset"]}  # app.js turns grip_* props by this
    return out


class Publisher:
    def __init__(self, dest: Path, dry: bool):
        self.dest, self.dry = dest, dry
        self.written: dict[str, int] = {}  # dest-relative path -> bytes

    def copy(self, src: Path, rel: str) -> None:
        dst = self.dest / rel
        if not self.dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        self.written[rel] = src.stat().st_size

    def write(self, rel: str, text: str) -> None:
        data = text.encode()
        dst = self.dest / rel
        if not self.dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        self.written[rel] = len(data)

    def write_json(self, rel: str, obj, indent: int | None = 1) -> None:
        self.write(rel, json.dumps(obj, indent=indent) + "\n")


def safe_dest(dest: Path) -> None:
    """Refuse destinations that would put the repo or a home directory at risk."""
    if dest in {Path("/"), Path.home(), *ROOT.parents} or ROOT in (dest, *dest.parents):
        raise SystemExit(f"refusing --dest {dest}: the repo, inside it, a parent of it or a home directory")


def clean(dest: Path) -> None:
    if not dest.exists():
        return
    kids = [p for p in dest.iterdir() if p.name not in KEEP_ON_CLEAN]
    if kids and not (dest / MARKER).exists():
        raise SystemExit(f"--clean: {dest} is not empty and has no {MARKER} (not made by publish.py); "
                         "empty it yourself or pick another --dest")
    for p in kids:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", default=str(ROOT.parent / "monster-forge-viewer"),
                    help="output directory (default ../monster-forge-viewer); created if missing")
    ap.add_argument("--source", default="srd-5.2.1")
    ap.add_argument("--clean", action="store_true", help="empty --dest first (keeps .git, CNAME, .github)")
    ap.add_argument("--dry-run", action="store_true", help="report what would be written, write nothing")
    ap.add_argument("--top", type=int, default=12, help="how many of the biggest files to list")
    args = ap.parse_args()

    dest = Path(args.dest).expanduser().resolve()
    safe_dest(dest)
    if dest.exists() and not dest.is_dir():
        raise SystemExit(f"--dest {dest} is not a directory")
    src_root = OUT_ROOT / args.source
    if not (viewer.REGISTRY_DIR / f"{args.source}.json").exists():
        raise SystemExit(f"no registry for source {args.source!r}")
    if args.clean and not args.dry_run:
        clean(dest)
    pub = Publisher(dest, args.dry_run)
    if not args.dry_run:
        dest.mkdir(parents=True, exist_ok=True)

    reg = {m["id"]: m for m in viewer.load_json(viewer.REGISTRY_DIR / f"{args.source}.json").get("monsters", [])}
    props_reg = {p["id"]: p for p in viewer.load_json(viewer.REGISTRY_DIR / "props.json").get("props", [])}

    # The dev index, built by the dev viewer's own code, then cut down to the released ones.
    full = viewer.build_index(args.source)
    released, skipped = [], []
    for entry in full["monsters"]:
        d = src_root / entry["id"]
        man = viewer.load_json(d / "manifest.json") if d.is_dir() else {}
        if man.get("released") is not True:
            continue
        if not entry["files"]["model"]:
            skipped.append((entry["id"], "no model file"))
            continue
        released.append((entry, man, d))

    # Props the released monsters wield (registry `wields`), when tools/props.py made them.
    wanted: dict[str, list[str]] = {}
    for entry, _man, _d in released:
        for pid in reg[entry["id"]].get("wields") or []:
            wanted.setdefault(pid, []).append(entry["id"])
    props_out = []
    for pid, users in sorted(wanted.items()):
        pd = OUT_ROOT / "props" / pid
        if not (pd / "model.glb").is_file():
            skipped.append((f"prop {pid}", "no out/props/<id>/model.glb"))
            continue
        pub.copy(pd / "model.glb", f"props/{pid}/model.glb")
        lods = viewer.load_json(pd / "lods.json").get("lods") or {}
        lod_out = {}
        for k, v in sorted(lods.items()):
            f = v.get("file") if isinstance(v, dict) else None
            if f and (pd / f).is_file():
                if f != "model.glb":
                    pub.copy(pd / f, f"props/{pid}/{f}")
                lod_out[k] = {"file": f, "tris": v.get("tris"), "ratio": v.get("ratio")}
        if lod_out:
            pub.write_json(f"props/{pid}/lods.json", {"lods": lod_out})
        pman = viewer.load_json(pd / "manifest.json")
        pub.write_json(f"props/{pid}/manifest.json", trim_prop_manifest(pman))
        rec = {k: v for k, v in props_reg.get(pid, {"id": pid}).items() if k not in ("reference_prompt", "used_by",
                                                                                       "monster_count", "variants")}
        rec["used_by"] = sorted(users)
        rec["dir"] = f"props/{pid}/"
        rec["lods"] = lod_out
        rec["grip_offset"] = ((pman.get("prop") or {}).get("grip_offset"))
        props_out.append(rec)
    published_props = {p["id"] for p in props_out}

    # Monsters: files + index entries
    entries = []
    for entry, man, d in released:
        mid = entry["id"]
        rel = f"{args.source}/{mid}/"
        e = {k: v for k, v in entry.items() if k not in DEV_ONLY}
        files = {k: v for k, v in entry["files"].items() if k not in DEV_ONLY_FILES}
        pub.copy(d / files["model"], rel + files["model"])
        for k, v in e["lods"].items():
            if v["file"] != files["model"]:
                pub.copy(d / v["file"], rel + v["file"])
        if e["lods"]:  # lods.json naming the files that were published (the rigged chain)
            pub.write_json(rel + "lods.json", {"lods": e["lods"]})
        for name in e["external_clips"]:
            pub.copy(d / "anims" / f"{name}.glb", f"{rel}anims/{name}.glb")
        if files.get("thumb"):
            pub.copy(d / files["thumb"], rel + files["thumb"])
        pub.write_json(rel + "manifest.json", trim_manifest(man))
        # Files that stay home: pipeline images and textures.
        for k in ("base_color_painted", "base_color_voxel", "thumb_head", "thumb_ground", "crop", "ref"):
            files[k] = None
        files["refs"] = []
        files["manifest"] = "manifest.json"
        e["files"] = files
        e["dir"] = rel
        e["rig_quality"] = None  # rig-stage diagnostics (roll scores, problem lists): dev only
        e["wields"] = [p for p in reg[mid].get("wields") or [] if p in published_props]
        entries.append(e)

    totals = {s: sum(1 for e in entries if e["stages"][s]) for s in viewer.STAGES}
    totals["anim_complete"] = sum(1 for e in entries if e["anim_complete"])
    by_plan: dict[str, dict] = {}
    for e in entries:
        plan = by_plan.setdefault(e.get("body_plan") or "?", {"total": 0, **{s: 0 for s in viewer.STAGES}})
        plan["total"] += 1
        for s in viewer.STAGES:
            plan[s] += bool(e["stages"][s])
    attrib = attribution()
    index = {
        "source": full["source"],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "stages": full["stages"],
        "body_plans": full["body_plans"],
        "summary": {"total": len(entries), "stages": totals, "by_body_plan": by_plan},
        "monsters": entries,
        "static": True,
        "registry_total": len(full["monsters"]),
        "license": "CC-BY-4.0",
        "attribution": attrib,
        "props": "props.json",
    }
    pub.write_json("index.json", index, indent=None)
    pub.write_json("props.json", {"source": "props", "count": len(props_out), "props": props_out})

    # The viewer itself, with the static-mode marker in index.html.
    html = (VIEWER / "index.html").read_text()
    if STATIC_META not in html:
        html = html.replace('<meta charset="utf-8">', '<meta charset="utf-8">\n' + STATIC_META, 1)
    pub.write("index.html", html)
    for name in ("app.js", "style.css"):
        pub.copy(VIEWER / name, name)
    for p in sorted((VIEWER / "vendor").rglob("*")):
        if p.is_file():
            pub.copy(p, str(p.relative_to(VIEWER)))
    pub.write(".nojekyll", "")
    pub.write(MARKER, "made by monster-forge tools/publish.py; --clean empties this directory\n")
    names = "\n".join(f"- {e['name']} (`{e['id']}`, {e['size']} {e['type']}, CR {e['cr']})" for e in entries)
    readme = f"""# monster-forge viewer

Static three.js viewer for low-poly, rigged and animated D&D SRD 5.2.1 monsters made by
monster-forge. Open `index.html` through any static web server (GitHub Pages, or
`python3 -m http.server --directory .`); `#<id>` links to a monster, e.g. `#goblin-warrior`.

Generated by `tools/publish.py` on {index['generated_at']}; do not edit by hand, re-run it.

Monsters ({len(entries)} of {len(full['monsters'])}):

{names}

Layout: `index.json` (monster index), `props.json` (held props), `{args.source}/<id>/`
(`model_rigged.glb` = LOD0 with embedded textures, `model_rigged_lod1-3.glb`, `anims/<clip>.glb`
armature-only clips on the same skeleton, `thumb.png`, `manifest.json`), `props/<id>/`
(`model.glb` in prop space: origin at the grip, +Y toward the tip, -Z the edge),
`vendor/three/` (three.js r179, MIT, `vendor/three/LICENSE`).

## License and attribution

The monster assets are derived from the SRD 5.2.1 and are distributed under
CC-BY-4.0 (https://creativecommons.org/licenses/by/4.0/) with this attribution:

{chr(10).join('> ' + p for p in attrib[:1])}

{chr(10).join(attrib[1:])}
"""
    pub.write("README.md", readme)

    # Report
    total = sum(pub.written.values())
    print(f"{'would publish' if args.dry_run else 'published'} {len(entries)} monsters, {len(props_out)} props "
          f"-> {dest}")
    for e in entries:
        size = sum(v for k, v in pub.written.items() if k.startswith(e["dir"]))
        print(f"  {e['id']:<22} {size / 1048576:7.1f} MB  {len(e['external_clips'])} clips, "
              f"LODs {','.join(sorted(e['lods']))}" + (f", props {' '.join(e['wields'])}" if e["wields"] else ""))
    for what, why in skipped:
        print(f"  skipped {what}: {why}")
    print(f"total {total / 1048576:.1f} MB in {len(pub.written)} files")
    print("biggest files:")
    for rel, n in sorted(pub.written.items(), key=lambda kv: -kv[1])[:args.top]:
        print(f"  {n / 1048576:7.2f} MB  {rel}")
    big = [r for r, n in pub.written.items() if n > GH_FILE_LIMIT]
    if big:
        print(f"WARNING: over GitHub's 100 MB per-file limit: {', '.join(big)}")
    if total > GH_SITE_LIMIT:
        print("WARNING: over the ~1 GB GitHub Pages site limit")
    if not args.dry_run:
        stale = sorted(str(p.relative_to(dest)) for p in dest.rglob("*")
                       if p.is_file() and not any(part in KEEP_ON_CLEAN for part in p.relative_to(dest).parts)
                       and str(p.relative_to(dest)) not in pub.written)
        if stale:
            print(f"{len(stale)} file(s) in --dest not from this run (unreleased since?); --clean drops them:")
            for s in stale[:10]:
                print("  " + s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
