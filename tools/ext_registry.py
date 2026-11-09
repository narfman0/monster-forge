#!/usr/bin/env python3
"""Build registry/<source>.json (extra repo) from curation/<source>/roster.json, for the external-model path.

  python3 tools/ext_registry.py --source <src> [--tags]

roster.json: {series, license?, characters: {id: {name, height_m?, size?, reference_prompt?, prompt_extra?}}}.
A character with a reference_prompt is one to GENERATE (no usable existing model): its record carries the prompt,
and overrides/<source>/<id>.json gets the style and prompt_extra for the ref stage (tools/forge.py ref/mesh/rig). Every character becomes a medium
biped humanoid record in the same schema as the SRD registry (animation set idle/walk/attack_melee/hit/death, hand
sockets), so ext_import.py / ext_rig.py / forge.py promote / the viewer treat it like any other source. There is
no reference_prompt (models come from existing assets, not generation). Existing records keep their hand edits
(scale_m, tags) unless --reset. --tags also writes curation/<source>/tags.json from the first source that has one.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sources  # noqa: E402

BASE = {"size": "medium", "size_options": ["medium"], "type": "humanoid", "cr": "0", "speeds": {"walk": 30},
        "body_plan": "biped", "body_plan_confidence": 1.0, "body_plan_source": "override", "rig_template": "rigify.basic_human",
        "animation_set": ["idle", "walk", "attack_melee", "hit", "death"], "hands": True, "wields": [], "shield": False,
        "sockets": ["hand_r", "hand_l", "back", "hip_l", "hip_r", "head"], "unarmed_reference": False}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--tags", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    cur = sources.extra_dir() / "curation" / a.source
    ro = json.loads((cur / "roster.json").read_text())
    path = sources.extra_dir() / "registry" / f"{a.source}.json"
    old = {m["id"]: m for m in json.loads(path.read_text())["monsters"]} if path.exists() and not a.reset else {}
    mons = []
    for cid, ch in ro["characters"].items():
        m = dict(BASE, id=cid, name=ch["name"], source=a.source, page=0, tags=[a.source, "anime", "character"],
                 scale_m=ch.get("height_m", 1.7))
        if ch.get("reference_prompt"):
            m["reference_prompt"] = ch["reference_prompt"]
        if cid in old:
            m.update({k: old[cid][k] for k in ("scale_m", "tags") if k in old[cid]})
        mons.append(m)
    doc = {"source": a.source, "license": ro.get("license", f"Third-party IP ({ro['series']}): personal use only, never publish, never push to a public remote."),
           "note": f"Roster for {ro['series']} built by tools/ext_registry.py from curation/{a.source}/roster.json; models are existing assets (tools/ext_import.py).",
           "count": len(mons), "body_plans": ["biped"], "monsters": mons}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    (sources.extra_dir() / "overrides" / a.source).mkdir(parents=True, exist_ok=True)
    for cid, ch in ro["characters"].items():
        if ch.get("prompt_extra"):
            of = sources.extra_dir() / "overrides" / a.source / f"{cid}.json"
            ov = json.loads(of.read_text()) if of.exists() else {}
            ov.update({"style": ro.get("style", "anime-flat-v1"), "prompt_extra": ch["prompt_extra"]})
            of.write_text(json.dumps(ov, indent=2, ensure_ascii=False) + "\n")
    print(f"{a.source}: {len(mons)} records -> {path}")
    if a.tags:
        tj = cur / "tags.json"
        if not tj.exists():
            ref = next((p for p in sorted((sources.extra_dir() / "curation").glob("*/tags.json")) if p.parent.name != a.source), None)
            if ref:
                d = json.loads(ref.read_text())
                old_src = next(iter(d.get("sources", {})), None)  # the template's own source id; swap it for this one
                if old_src:
                    d = json.loads(json.dumps(d).replace(f'"{old_src}"', f'"{a.source}"'))
                    d["tags"][a.source]["description"] = f"{ro['series']} characters (third-party IP, personal use only; the private registry)."
                tj.write_text(json.dumps(d, indent=1, ensure_ascii=False))
                print(f"  tags curation from {ref.parent.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
