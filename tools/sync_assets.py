#!/usr/bin/env python3
"""Keep out/ in sync with the private host (srv), which holds the authoritative copy, and
publish a static, multi-source viewer there.

    python3 -I tools/sync_assets.py sync                    # pull, push every local source, rebuild the site
    python3 -I tools/sync_assets.py pull [--sources mha]    # srv -> here (every source srv has, by default)
    python3 -I tools/sync_assets.py push --sources mha      # here -> srv, then rebuild the site
    python3 -I tools/sync_assets.py site                    # rebuild only the site (indexes, viewer copy)
    ... --dest srv:/media/data/other/monster-forge (the default) --dry-run

This is the PRIVATE counterpart of tools/publish.py. publish.py builds the public SRD-only
Pages site from released monsters; this copies everything under out/<source>/ (every stage's
files, reference images, rig and anim output, not just released monsters) for every source,
plus out/props/ and a read-only copy of viewer/. Non-SRD sources (WotC IP, third-party IP)
are fine here because the host is LAN-only; never point --dest at anything public.

Every machine that generates (this box, ai) pulls what it lacks and pushes what it made.
Both directions use rsync -u (a file newer at the receiver is kept) and never delete; push
--delete removes files under dest/out/<s>/ that no longer exist here, for a source this
machine owns and has cleaned up (stale clips would otherwise come back on the next pull).

Layout under --dest (rsync; local path or host:path):

    site/index.html              redirect to viewer/?source=all (every source, no filter)
    site/sources.html            landing page listing the sources
    site/viewer/                 viewer/ (index.html, app.js, style.css, vendor/)
    site/sources.json            [{source, total, stages, generated_at}]
    site/sources/<s>/index.json  the dev index (viewer.build_index) with dev-only fields cut
    site/sources/all/index.json  every source in one index; ids are <source>/<id> (sources
                                 may share ids), the source tag (srd, planescape, mha ...)
                                 is the filter
    site/props.json              the props under out/props/ with a model.glb
    out/<s>/... out/props/...    the assets (never in git)

The viewer is opened at /viewer/?source=<s>[#<id>]; viewer/app.js's static mode reads
/sources/<s>/index.json and the index's absolute /out/<s>/<id>/ paths. The site is built
from every source this machine has a registry for; a source it has no out/ tree for keeps
the index already on the host. The nginx side is deploy/srv/ (compose + nginx.conf).
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
DEFAULT_DEST = "srv:/media/data/other/monster-forge"
DEV_ONLY = ("has_override", "job")  # keep errors / hi_faces: this copy is for review
ORDER = ("srd-5.2.1",)  # first in the combined index; the rest alphabetical
RSYNC = ("rsync", "-a", "-u", "--human-readable", "--stats", "--exclude=__pycache__", "--exclude=*.tmp")


def discover() -> list[str]:
    """Sources with an out/ tree here."""
    return sorted(p.name for p in OUT_ROOT.iterdir() if p.is_dir() and not p.name.startswith("_") and p.name != "props")


def remote(dest: str) -> tuple[str | None, str]:
    host, sep, path = dest.partition(":")
    return (host, path) if sep and "/" not in host else (None, dest)


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def dest_dirs(dest: str) -> list[str]:
    """What the host has under out/ (sources, props)."""
    host, path = remote(dest)
    cmd = ["ls", "-1", f"{path}/out"]
    r = run(["ssh", host, " ".join(cmd)] if host else cmd)
    if r.returncode:
        sys.exit(f"cannot list {dest}/out: {r.stderr.strip()}")
    return sorted(n for n in r.stdout.split() if not n.startswith("_"))


def dest_sources(dest: str) -> list[str]:
    """Sources the host has under out/."""
    return [n for n in dest_dirs(dest) if n != "props"]


def dest_index(dest: str, source: str) -> dict | None:
    """The index the host already serves for a source (None when it has none)."""
    with tempfile.TemporaryDirectory(prefix="mf-idx-") as tmp:
        r = run(["rsync", "-a", f"{dest}/site/sources/{source}/index.json", f"{tmp}/"])
        p = Path(tmp) / "index.json"
        return json.loads(p.read_text()) if not r.returncode and p.is_file() else None


def ordered(sources) -> list[str]:
    return sorted(set(sources), key=lambda s: (s not in ORDER, ORDER.index(s) if s in ORDER else 0, s))


def registries() -> list[str]:
    """Every source this checkout has a registry for (the public repo and the extra repo)."""
    dirs = [viewer.REGISTRY_DIR, viewer.sources.extra_dir() / "registry"]
    found = {p.stem for d in dirs if d.is_dir() for p in d.glob("*.json") if not p.stem.endswith(".tags")}
    found.discard("props")
    return ordered(found)


def merge_all(indexes: list[dict]) -> dict:
    """One index over every source: entries keep their fields, ids become <source>/<id>."""
    monsters, plans, paired = [], [], {}
    totals: dict[str, int] = {}
    by_plan: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    tags: dict[str, dict] = {}
    for idx in indexes:
        s = idx["source"]
        for e in idx["monsters"]:
            monsters.append({**e, "id": f"{s}/{e['id']}"})
        summ = idx.get("summary") or {}
        for k, v in (summ.get("stages") or {}).items():
            totals[k] = totals.get(k, 0) + v
        for plan, row in (summ.get("by_body_plan") or {}).items():
            acc = by_plan.setdefault(plan, {})
            for k, v in row.items():
                acc[k] = acc.get(k, 0) + v
        plans += [p for p in idx.get("body_plans") or [] if p not in plans]
        for k, v in (idx.get("paired") or {}).items():
            paired.setdefault(k, v)
        ti = idx.get("tag_index") or {}
        for g in ti.get("groups") or []:
            acc = groups.setdefault(g["id"], {**g, "tags": []})
            acc["tags"] += [t for t in g["tags"] if t not in acc["tags"]]
        for t, d in (ti.get("tags") or {}).items():
            tags.setdefault(t, d)
    return {
        "source": "all",
        "sources": [i["source"] for i in indexes],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "stages": list(viewer.STAGES),
        "body_plans": plans,
        "summary": {"total": len(monsters), "stages": totals, "by_body_plan": by_plan},
        "monsters": monsters,
        "paired": paired,
        "static": True,
        "props": "/props.json",
        "tag_index": {"groups": list(groups.values()), "tags": tags} if groups else None,
    }


def props_listing() -> dict:
    """site/props.json: the registry's props that have a model, at their /out/props/<id>/ dirs."""
    reg = viewer.load_json(viewer.REGISTRY_DIR / "props.json").get("props", [])
    out = []
    for p in reg:
        pd = OUT_ROOT / "props" / p["id"]
        if not (pd / "model.glb").is_file():
            continue
        rec = {k: v for k, v in p.items() if k not in ("reference_prompt", "variants")}
        rec["dir"] = f"/out/props/{p['id']}/"
        rec["lods"] = viewer.load_json(pd / "lods.json").get("lods") or {}
        rec["grip_offset"] = (viewer.load_json(pd / "manifest.json").get("prop") or {}).get("grip_offset")
        out.append(rec)
    return {"source": "props", "count": len(out), "props": out}


def build_index(source: str, dest: str) -> dict | None:
    """This machine's index for a source, or the host's when there is no out/ tree here."""
    if not (OUT_ROOT / source).is_dir():
        idx = dest_index(dest, source)
        print(f"  {source}: no out/{source} here, " + ("keeping the host's index" if idx else "skipped (no index on the host)"))
        return idx
    try:
        idx = viewer.build_index(source)
    except FileNotFoundError as e:
        print(f"  skip {source}: {e}")
        return None
    wields = {m["id"]: m.get("wields") or [] for m in viewer.load_json(viewer.sources.registry_path(source)).get("monsters", [])}
    for e in idx["monsters"]:
        for k in DEV_ONLY:
            e.pop(k, None)
        e["wields"] = wields.get(e["id"], [])
    idx["static"] = True
    idx["props"] = "/props.json"
    return idx


def build_site(site: Path, sources: list[str], dest: str) -> list[dict]:
    site.mkdir(parents=True)
    shutil.copytree(VIEWER, site / "viewer", ignore=shutil.ignore_patterns("__pycache__"))
    indexes = [i for i in (build_index(s, dest) for s in sources) if i]
    if not indexes:
        return []
    for idx in [*indexes, merge_all(indexes)]:
        d = site / "sources" / idx["source"]
        d.mkdir(parents=True)
        (d / "index.json").write_text(json.dumps(idx, indent=None) + "\n")
    (site / "props.json").write_text(json.dumps(props_listing(), indent=None) + "\n")
    listing = []
    for idx in indexes:
        summ = idx.get("summary") or {}
        listing.append({"source": idx["source"], "total": summ.get("total", len(idx["monsters"])),
                        "stages": summ.get("stages", {}), "generated_at": idx["generated_at"]})
    (site / "sources.json").write_text(json.dumps(listing, indent=1) + "\n")
    total = sum(e["total"] for e in listing)
    rows = [f'<li><a href="viewer/?source=all">everything</a> &middot; {total} monsters '
            f'&middot; filter by source with the source tags</li>']
    for e in listing:
        st = e["stages"]
        done = ", ".join(f"{k} {st[k]}" for k in viewer.STAGES if st.get(k))
        rows.append(f'<li><a href="viewer/?source={html.escape(e["source"])}">{html.escape(e["source"])}</a>'
                    f' &middot; {e["total"]} monsters &middot; {html.escape(done) or "nothing built yet"}'
                    f' <small>(index {html.escape(e["generated_at"])})</small></li>')
    (site / "sources.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>monster-forge (private)</title>'
        '<style>body{font:16px system-ui;background:#161a22;color:#dde;max-width:46em;margin:3em auto;padding:0 1em}'
        'a{color:#8cf}li{margin:.6em 0}small{color:#889}</style>'
        '<h1>monster-forge</h1><p>Private, LAN-only. Not for publication: the non-SRD sources are third-party IP.</p>'
        f'<ul>{"".join(rows)}</ul>\n')
    (site / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>monster-forge (private)</title>'
        '<meta http-equiv="refresh" content="0; url=viewer/?source=all">'
        '<a href="viewer/?source=all">every source</a> &middot; <a href="sources.html">by source</a>\n')
    return listing


def rsync(src: str, dst: str, dry: bool, delete: bool = False) -> None:
    cmd = list(RSYNC)
    if delete:
        cmd.append("--delete")
    if dry:
        cmd.append("--dry-run")
    r = run([*cmd, src, dst])
    tail = [l for l in r.stdout.splitlines() if l.startswith(("Number of regular files transferred", "Total transferred file size"))]
    print(f"  {src} -> {dst}: rc={r.returncode} " + "; ".join(tail))
    if r.returncode:
        sys.exit(r.stderr.strip() or f"rsync failed ({r.returncode})")


def pull(dest: str, sources: list[str], dry: bool) -> None:
    have = set(dest_dirs(dest))
    for s in [*sources, "props"]:
        if s not in have:
            print(f"pull out/{s}: not on the host, skipped")
            continue
        print(f"pull out/{s}")
        (OUT_ROOT / s).mkdir(parents=True, exist_ok=True)
        rsync(f"{dest}/out/{s}/", f"{OUT_ROOT / s}/", dry)


def push(dest: str, sources: list[str], dry: bool, delete: bool) -> None:
    for s in [*sources, "props"]:
        if not (OUT_ROOT / s).is_dir():
            continue
        print(f"push out/{s}")
        rsync(f"{OUT_ROOT / s}/", f"{dest}/out/{s}/", dry, delete=delete and s != "props")


def publish_site(dest: str, dry: bool) -> None:
    sources = ordered(set(registries()) | set(dest_sources(dest)))
    with tempfile.TemporaryDirectory(prefix="mf-site-") as tmp:
        site = Path(tmp) / "site"
        listing = build_site(site, sources, dest)
        if not listing:
            raise SystemExit("no source had an index")
        print(f"site: {', '.join(e['source'] + ' (' + str(e['total']) + ')' for e in listing)}")
        rsync(f"{site}/", f"{dest}/site/", dry, delete=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("sync", "pull", "push", "site"))
    ap.add_argument("--dest", default=DEFAULT_DEST, help=f"host:/path or a local directory (default {DEFAULT_DEST})")
    ap.add_argument("--sources", help="comma list (pull: default every source on the host; push/sync: every out/<s> here)")
    ap.add_argument("--delete", action="store_true", help="push: also remove files under dest/out/<s>/ that no longer exist here")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dest = args.dest.rstrip("/")
    picked = args.sources.split(",") if args.sources else None
    if args.cmd in ("sync", "pull"):
        pull(dest, picked or dest_sources(dest), args.dry_run)
    if args.cmd in ("sync", "push"):
        if args.cmd == "push" and not picked:
            sys.exit("push needs --sources (the ones this machine made); sync pushes every local source")
        push(dest, picked or discover(), args.dry_run, args.delete)
    if args.cmd != "pull":
        publish_site(dest, args.dry_run)
    print(f"done {time.strftime('%H:%M:%S')}{' (dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
