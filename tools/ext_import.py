#!/usr/bin/env python3
"""Use an existing, externally made model as a record's mesh instead of generating one.

    python3 tools/ext_import.py --source mha                 # every id in curation/<source>/external.json
    python3 tools/ext_import.py --source mha deku todoroki   # some of them
    python3 tools/ext_import.py --source mha --list

curation/<source>/external.json (in the extra repo, never in this one) maps record id to
    {"provider": "sketchfab", "uid": "...", "title", "author", "url", "license", "license_url",
     "tri_budget": 40000 (optional), "drop": ["name substring", ...] (optional),
     "pose": "REST"|"POSE" (optional; POSE when the skinned rest pose is mangled), "note"}.

For each id: downloads the glb through the Sketchfab API (token in ~/.config/sketchfab/token or
$SKETCHFAB_TOKEN_FILE, never printed; cached in ~/.cache/mf-ext/sketchfab/<uid>.glb), keeps the
original as out/<source>/<id>/ext/source.glb, flattens it with tools/blender_ext_prep.py into
model_raw.glb (the generated one is kept once as model_raw_gen.glb), writes manifest["ext"] with
the attribution, and runs the qa stage so model.glb, the thumbnails and qa.json are fresh.
Everything downstream (lod, rig, anim, viewer) then treats it like any other mesh. The licence
terms differ per model: CC-BY needs the attribution recorded here; check before any release.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge  # noqa: E402
import sources  # noqa: E402

CACHE = Path(os.environ.get("MF_EXT_CACHE", "~/.cache/mf-ext/sketchfab")).expanduser()


def token() -> str:
    p = Path(os.environ.get("SKETCHFAB_TOKEN_FILE", "~/.config/sketchfab/token")).expanduser()
    if not p.is_file():
        raise SystemExit(f"no Sketchfab token at {p} (Settings > Password & API on sketchfab.com)")
    return p.read_text().strip()


def fetch_glb(uid: str) -> Path:
    dest = CACHE / f"{uid}.glb"
    if dest.is_file() and dest.stat().st_size > 1000:
        return dest
    CACHE.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(f"https://api.sketchfab.com/v3/models/{uid}/download",
                                 headers={"Authorization": f"Token {token()}"})
    url = json.load(urllib.request.urlopen(req, timeout=30))["glb"]["url"]
    urllib.request.urlretrieve(url, dest)
    return dest


def import_one(source: str, mon: dict, ext: dict, args) -> None:
    d = forge.OUT_ROOT / source / mon["id"]
    d.mkdir(parents=True, exist_ok=True)
    src = fetch_glb(ext["uid"])
    (d / "ext").mkdir(exist_ok=True)
    shutil.copy2(src, d / "ext" / "source.glb")
    raw, gen = d / "model_raw.glb", d / "model_raw_gen.glb"
    if raw.exists() and not gen.exists() and not (forge.load_manifest(d).get("ext")):
        raw.rename(gen)  # keep the generated mesh once
    job = {"src": str(src), "dst": str(raw), "tri_budget": ext.get("tri_budget", 40000), "drop": ext.get("drop"), "pose": ext.get("pose", "REST")}
    proc = subprocess.run([forge.BLENDER, "-b", "--factory-startup", "-P",
                           str(Path(__file__).with_name("blender_ext_prep.py")), "--", json.dumps(job)],
                          capture_output=True, text=True, timeout=900)
    info_p = Path(str(raw) + ".json")
    if proc.returncode or not info_p.exists():
        sys.stderr.write(proc.stdout[-3000:] + proc.stderr[-3000:])
        raise SystemExit(f"{mon['id']}: blender prep failed")
    info = json.loads(info_p.read_text())
    info_p.unlink()
    man = forge.load_manifest(d)
    man["ext"] = {k: ext[k] for k in ("provider", "uid", "title", "author", "url", "license", "license_url", "note")
                  if k in ext} | {"tris_source": info["tris_in"], "tris_flat": info["tris_out"],
                                  "materials": info["materials"], "imported": forge.time.strftime("%Y-%m-%d")}
    man.pop("mesh", None)
    forge.save_manifest(d, man)
    forge.log(f"{mon['id']}: external {ext['uid'][:8]} {info['tris_in']} -> {info['tris_out']} tris, "
              f"{info['meshes']} meshes, {info['materials']} materials")
    r = subprocess.run([sys.executable, str(Path(__file__).with_name("forge.py")), "qa", mon["id"],
                        "--source", source, "--force"], capture_output=True, text=True)
    sys.stdout.write("".join(l + "\n" for l in r.stderr.splitlines() + r.stdout.splitlines() if "qa done" in l or "rror" in l))
    if r.returncode:
        raise SystemExit(f"{mon['id']}: qa failed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("ids", nargs="*")
    args = ap.parse_args()
    table = json.loads(sources.curation_path(args.source, "external").read_text())
    reg = forge.load_registry(args.source)
    ids = args.ids or sorted(k for k in table if not k.startswith("_"))
    if args.list:
        for i in ids:
            t = table[i]
            print(f"{i:16} {t['provider']}:{t['uid'][:8]} {t.get('license')} | {t.get('title')} by {t.get('author')}")
        return 0
    for i in ids:
        if i not in table or i not in reg:
            raise SystemExit(f"{i}: not in external.json / registry")
        import_one(args.source, reg[i], table[i], args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
