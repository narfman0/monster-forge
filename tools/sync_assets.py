#!/usr/bin/env python3
"""Mirror out/ and a static, multi-source viewer to a private host (the homelab nginx on srv).

    python3 -I tools/sync_assets.py --dest srv:/media/data/other/monster-forge
    python3 -I tools/sync_assets.py --dest srv:/media/data/other/monster-forge --sources mha --dry-run

This is the PRIVATE counterpart of tools/publish.py. publish.py builds the public SRD-only
Pages site from released monsters; this copies everything under out/<source>/ (every stage's
files, reference images, rig and anim output, not just released monsters) for every source
it is given, plus a read-only copy of viewer/. Non-SRD sources (WotC IP, third-party IP) are
fine here because the host is LAN-only; never point --dest at anything public.

Layout written under --dest (rsync; local path or host:path):

    site/index.html              landing page listing the sources
    site/viewer/                 viewer/ (index.html, app.js, style.css, vendor/)
    site/sources.json            [{source, total, stages, generated_at}]
    site/sources/<s>/index.json  the dev index (viewer.build_index) with dev-only fields cut
    out/<s>/...                  rsync of the repo's out/<s>/ (the assets, never in git)

The viewer is opened at /viewer/?source=<s>[#<id>]; viewer/app.js's static mode reads
/sources/<s>/index.json and the index's absolute /out/<s>/<id>/ paths. Nothing is deleted
under out/ unless --delete is given. The nginx side is deploy/srv/ (compose + nginx.conf).
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import viewer  # noqa: E402  (tools/viewer.py: build_index, the dev index logic)

ROOT = viewer.ROOT
OUT_ROOT = viewer.OUT_ROOT
VIEWER = viewer.VIEWER
DEV_ONLY = ("has_override", "job")  # keep errors / hi_faces: this copy is for review


def discover() -> list[str]:
    return sorted(p.name for p in OUT_ROOT.iterdir() if p.is_dir() and not p.name.startswith("_") and p.name != "props")


def build_site(site: Path, sources: list[str]) -> list[dict]:
    site.mkdir(parents=True)
    shutil.copytree(VIEWER, site / "viewer")
    listing = []
    for s in sources:
        try:
            idx = viewer.build_index(s)
        except FileNotFoundError as e:
            print(f"skip {s}: {e}")
            continue
        for e in idx["monsters"]:
            for k in DEV_ONLY:
                e.pop(k, None)
        idx["static"] = True
        d = site / "sources" / s
        d.mkdir(parents=True)
        (d / "index.json").write_text(json.dumps(idx, indent=None) + "\n")
        summ = idx.get("summary") or {}
        listing.append({"source": s, "total": summ.get("total", len(idx["monsters"])),
                        "stages": summ.get("stages", {}), "generated_at": idx["generated_at"]})
    (site / "sources.json").write_text(json.dumps(listing, indent=1) + "\n")
    rows = []
    for e in listing:
        st = e["stages"]
        done = ", ".join(f"{k} {st[k]}" for k in viewer.STAGES if st.get(k))
        rows.append(f'<li><a href="viewer/?source={html.escape(e["source"])}">{html.escape(e["source"])}</a>'
                    f' &middot; {e["total"]} monsters &middot; {html.escape(done) or "nothing built yet"}'
                    f' <small>(index {html.escape(e["generated_at"])})</small></li>')
    (site / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>monster-forge (private)</title>'
        '<style>body{font:16px system-ui;background:#161a22;color:#dde;max-width:46em;margin:3em auto;padding:0 1em}'
        'a{color:#8cf}li{margin:.6em 0}small{color:#889}</style>'
        '<h1>monster-forge</h1><p>Private, LAN-only. Not for publication: the non-SRD sources are third-party IP.</p>'
        f'<ul>{"".join(rows)}</ul>\n')
    return listing


def rsync(src: str, dst: str, dry: bool, delete: bool = False, extra: tuple[str, ...] = ()) -> None:
    cmd = ["rsync", "-a", "--human-readable", "--stats", "--exclude=__pycache__", "--exclude=*.tmp", *extra]
    if delete:
        cmd.append("--delete")
    if dry:
        cmd.append("--dry-run")
    r = subprocess.run([*cmd, src, dst], capture_output=True, text=True)
    tail = [l for l in r.stdout.splitlines() if l.startswith(("Number of regular files transferred", "Total transferred file size", "Total file size"))]
    print(f"  rsync {dst}: rc={r.returncode} " + "; ".join(tail))
    if r.returncode:
        sys.exit(r.stderr.strip() or f"rsync failed ({r.returncode})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", required=True, help="host:/path or a local directory (the compose project dir)")
    ap.add_argument("--sources", help="comma list (default: every directory under out/ except props and _*)")
    ap.add_argument("--delete", action="store_true", help="also remove files under dest/out/<s>/ that no longer exist here")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sources = args.sources.split(",") if args.sources else discover()
    dest = args.dest.rstrip("/")
    with tempfile.TemporaryDirectory(prefix="mf-site-") as tmp:
        site = Path(tmp) / "site"
        listing = build_site(site, sources)
        if not listing:
            raise SystemExit("no source had an index")
        print(f"site: {', '.join(e['source'] + ' (' + str(e['total']) + ')' for e in listing)}")
        rsync(f"{site}/", f"{dest}/site/", args.dry_run, delete=True)
    for e in listing:
        s = e["source"]
        print(f"assets: out/{s}")
        rsync(f"{OUT_ROOT / s}/", f"{dest}/out/{s}/", args.dry_run, delete=args.delete)
    props = OUT_ROOT / "props"
    if props.is_dir():
        print("assets: out/props")
        rsync(f"{props}/", f"{dest}/out/props/", args.dry_run, delete=args.delete)
    print(f"done {time.strftime('%H:%M:%S')}{' (dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
