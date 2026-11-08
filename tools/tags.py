#!/usr/bin/env python3
"""Monster tags: rules over the registry + curated data/tags.json -> data/tags.computed.json.

    python3 -I tools/tags.py compute [--source srd-5.2.1] [--pipeline]   # validate, write the computed file
    python3 -I tools/tags.py check   [--source ...]    # validate + the computed file is up to date (exit 1 if not)
    python3 -I tools/tags.py report  [--source ...]    # coverage: tag -> count, monsters without a family, key lists
    python3 -I tools/tags.py show <id> [...]           # one monster's tags by group
    python3 -I tools/tags.py list <tag> [<tag> ...]    # ids that carry every tag given (AND)

Content tags come from, in order (docs/tags.md):
  1. the registry record's own `tags` (inputs: chromatic, devil, swarm, wizard ...),
  2. rules on registry fields: source, type (type-<type>), size, CR band, body plan, movement
     (speeds), true-dragon colour/age from the id, `dragon`/`draconic` split, humanoid -> npc,
  3. curated `members` (tag -> ids) in data/tags.json,
  4. `implies` on tag definitions (big-cat -> feline -> mammal ...), to a fixed point,
  5. curated per-id `ids` {add, remove} (remove wins), then `aquatic` drops `amphibious`.
Pipeline tags (rig-ual, rig-quad-canon, paired) depend on out/<source>/<id>/manifest.json:
pipeline_tags() computes them (the viewer index calls it); they are kept apart from content tags.

Definitions (groups, tags, implies, source -> tag) live in data/tags.json for every source; curated
membership is per source: data/tags.json for the SRD, the non-SRD checkout's
curation/<source>/tags.json otherwise (tools/sources.py). The computed file is
data/tags.computed.json for the SRD, registry/<source>.tags.json in the non-SRD checkout otherwise.
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sources  # noqa: E402  (tools/sources.py)

ROOT = Path(__file__).resolve().parent.parent
DEFS_PATH = ROOT / "data" / "tags.json"
OUT_ROOT = ROOT / "out"
TAG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ONE_OF = ("source", "type", "size", "cr", "body-plan")   # exactly one tag of each per monster
RULE_ONLY = ONE_OF + ("pipeline",)                        # never curated
PLAN_TAG = {"aquatic": "aquatic-body"}                    # body plans whose kebab name means something else
DRAGON_COLOURS = ("black", "blue", "green", "red", "white", "brass", "bronze", "copper", "gold", "silver")
DRAGON_AGES = {"wyrmling": "wyrmling", "young": "young-dragon", "adult": "adult-dragon", "ancient": "ancient-dragon"}
DRAGON_ID = re.compile(r"^(?:(young|adult|ancient)-)?(%s)-dragon(?:-(wyrmling))?$" % "|".join(DRAGON_COLOURS))


# ------------------------------------------------------------------ loading
def load_defs() -> dict:
    return json.loads(DEFS_PATH.read_text())


def load_curation(source: str) -> dict:
    """{members, ids, no_family} for one source (data/tags.json for the SRD)."""
    p = DEFS_PATH if source in sources.LOCAL_SOURCES else sources.curation_path(source, "tags")
    try:
        d = json.loads(Path(p).read_text())
    except FileNotFoundError:
        d = {}
    return {"members": {k: v for k, v in (d.get("members") or {}).items() if not k.startswith("_")},
            "ids": {k: v for k, v in (d.get("ids") or {}).items() if not k.startswith("_")},
            "no_family": list((d.get("no_family") or {}).get("ids") or [])}


def load_registry(source: str) -> list[dict]:
    return json.loads(sources.registry_path(source).read_text()).get("monsters", [])


def computed_path(source: str, write: bool = False) -> Path:
    if source in sources.LOCAL_SOURCES:
        return ROOT / "data" / "tags.computed.json"
    return sources.registry_path(source, write=write).with_suffix(".tags.json")


# ------------------------------------------------------------------ rules
def cr_band(cr) -> str:
    v = Fraction(str(cr))
    return "cr-0" if v == 0 else "cr-low" if v <= 4 else "cr-mid" if v <= 10 else "cr-high" if v <= 16 else "cr-epic"


def movement(speeds: dict) -> set[str]:
    out = set()
    walk = speeds.get("walk") or 0
    for k, t in (("fly", "flying"), ("swim", "swimming"), ("burrow", "burrowing"), ("climb", "climbing")):
        if speeds.get(k):
            out.add(t)
    if speeds.get("hover"):
        out.add("hovering")
    swim = speeds.get("swim") or 0
    if swim:
        out.add("aquatic" if walk <= 10 or swim >= 3 * walk else "amphibious")
    return out


def rule_tags(rec: dict, defs: dict) -> set[str]:
    t = set(rec.get("tags") or [])
    src = defs["sources"].get(rec.get("source"))
    if src:
        t.add(src)
    if rec.get("type"):
        t.add(f"type-{rec['type']}")
    if rec.get("size"):
        t.add(rec["size"])
    if rec.get("cr") is not None:
        t.add(cr_band(rec["cr"]))
    if rec.get("body_plan"):
        t.add(PLAN_TAG.get(rec["body_plan"], rec["body_plan"].replace("_", "-")))
    t |= movement(rec.get("speeds") or {})
    m = DRAGON_ID.match(rec["id"])
    if m and rec.get("type") == "dragon":
        age, colour, wyrm = m.groups()
        t |= {f"{colour}-dragon", DRAGON_AGES[wyrm or age or "adult"], "dragon"}
    if rec.get("type") == "dragon" and not ({"dragon", "chromatic", "metallic"} & t):
        t.add("draconic")
    if rec.get("type") == "humanoid":
        t.add("npc")
    return t


def close_implies(t: set[str], defs: dict) -> set[str]:
    todo = list(t)
    while todo:
        for j in (defs["tags"].get(todo.pop()) or {}).get("implies", []):
            if j not in t:
                t.add(j)
                todo.append(j)
    return t


def compute(source: str, defs: dict | None = None, cur: dict | None = None,
            recs: list[dict] | None = None) -> dict[str, list[str]]:
    """{id: [content tags]} in group/definition order."""
    defs = defs or load_defs()
    cur = cur or load_curation(source)
    recs = recs if recs is not None else load_registry(source)
    by_tag: dict[str, set[str]] = {}
    for tag, ids in cur["members"].items():
        for i in ids:
            by_tag.setdefault(i, set()).add(tag)
    out = {}
    for rec in recs:
        mid = rec["id"]
        fix = cur["ids"].get(mid) or {}
        t = rule_tags(rec, defs) | by_tag.get(mid, set()) | set(fix.get("add") or [])
        t = close_implies(t, defs) - set(fix.get("remove") or [])
        if "aquatic" in t:
            t.discard("amphibious")
        out[mid] = order(t, defs)
    return out


def order(tags, defs: dict) -> list[str]:
    gi = {g["id"]: i for i, g in enumerate(defs["groups"])}
    ti = {t: i for i, t in enumerate(defs["tags"])}
    return sorted(tags, key=lambda t: (gi.get((defs["tags"].get(t) or {}).get("group"), 99), ti.get(t, 1 << 20), t))


def pipeline_tags(man: dict, paired_ok: bool = True) -> list[str]:
    """Pipeline tags from one manifest. paired_ok: the source has a paired library with entries."""
    rig = man.get("rig") if isinstance(man.get("rig"), dict) else {}
    anim = man.get("anim") if isinstance(man.get("anim"), dict) else {}
    sh = anim.get("ual_shared") if isinstance(anim.get("ual_shared"), dict) else {}
    out = []
    if rig.get("tool") == "ual":
        out.append("rig-ual")
    if ((rig.get("canon") or {}).get("status")) in ("ok", "partial"):
        out.append("rig-quad-canon")
    if paired_ok and sh.get("hip_scale") and sh.get("clips"):
        out.append("paired")
    return out


def paired_available(source: str, out_root: Path = OUT_ROOT) -> bool:
    try:
        lib = json.loads((out_root / source / "_shared" / "paired" / "library.json").read_text())
    except (OSError, ValueError):
        return False
    return bool(lib.get("paired"))


def compute_pipeline(source: str, ids, out_root: Path = OUT_ROOT) -> dict[str, list[str]]:
    ok = paired_available(source, out_root)
    out = {}
    for mid in ids:
        try:
            man = json.loads((out_root / source / mid / "manifest.json").read_text())
        except (OSError, ValueError):
            continue
        t = pipeline_tags(man, ok)
        if t:
            out[mid] = t
    return out


def tag_index(defs: dict | None = None, used: set[str] | None = None) -> dict:
    """What a page needs to draw the facet: groups in order with their tags, and per-tag
    description and group. used: only these tags (default all)."""
    defs = defs or load_defs()
    groups = []
    for g in defs["groups"]:
        ts = [t for t, d in defs["tags"].items() if d.get("group") == g["id"] and (used is None or t in used)]
        if ts:
            groups.append({"id": g["id"], "label": g.get("label", g["id"]), "description": g.get("description", ""),
                           "tags": ts})
    tags = {t: {"group": d["group"], "description": d.get("description", "")}
            for t, d in defs["tags"].items() if used is None or t in used}
    return {"groups": groups, "tags": tags}


# ------------------------------------------------------------------ validation
def validate(source: str, defs: dict, cur: dict, recs: list[dict], tags: dict[str, list[str]]) -> list[str]:
    errs = []
    groups = {g["id"] for g in defs["groups"]}
    tdefs = defs["tags"]
    ids = {r["id"] for r in recs}
    for t, d in tdefs.items():
        if not TAG_RE.match(t):
            errs.append(f"tag {t!r}: not lowercase-kebab")
        if d.get("group") not in groups:
            errs.append(f"tag {t!r}: unknown group {d.get('group')!r}")
        if not d.get("description"):
            errs.append(f"tag {t!r}: no description")
        for j in d.get("implies", []):
            if j not in tdefs:
                errs.append(f"tag {t!r}: implies unknown tag {j!r}")
    if source not in defs["sources"]:
        errs.append(f"source {source!r} has no tag in data/tags.json `sources`")
    for t, members in cur["members"].items():
        if t not in tdefs:
            errs.append(f"members: unknown tag {t!r}{suggest(t, tdefs)}")
        elif tdefs[t]["group"] in RULE_ONLY:
            errs.append(f"members: {t!r} is a {tdefs[t]['group']} tag (rules only)")
        if len(set(members)) != len(members):
            errs.append(f"members[{t!r}]: duplicate ids")
        for i in members:
            if i not in ids:
                errs.append(f"members[{t!r}]: unknown id {i!r}{suggest(i, ids)}")
    for i, fix in cur["ids"].items():
        if i not in ids:
            errs.append(f"ids: unknown id {i!r}{suggest(i, ids)}")
        for k in fix:
            if k not in ("add", "remove", "why"):
                errs.append(f"ids[{i!r}]: unknown key {k!r} (add, remove, why)")
        for t in (fix.get("add") or []) + (fix.get("remove") or []):
            if t not in tdefs:
                errs.append(f"ids[{i!r}]: unknown tag {t!r}{suggest(t, tdefs)}")
            elif tdefs[t]["group"] in RULE_ONLY:
                errs.append(f"ids[{i!r}]: {t!r} is a {tdefs[t]['group']} tag (rules only)")
    for i in cur["no_family"]:
        if i not in ids:
            errs.append(f"no_family: unknown id {i!r}{suggest(i, ids)}")
    for r in recs:
        for t in r.get("tags") or []:
            if t not in tdefs:
                errs.append(f"{r['id']}: registry tag {t!r} is not defined in data/tags.json")
    nofam = set(cur["no_family"])
    for mid, ts in tags.items():
        by = {}
        for t in ts:
            if t not in tdefs:
                errs.append(f"{mid}: unknown tag {t!r}{suggest(t, tdefs)}")
                continue
            by.setdefault(tdefs[t]["group"], []).append(t)
        for g in ONE_OF:
            if len(by.get(g, [])) != 1:
                errs.append(f"{mid}: needs exactly one {g} tag, has {by.get(g, [])}")
        if by.get("pipeline"):
            errs.append(f"{mid}: pipeline tags in content tags: {by['pipeline']}")
        fam = by.get("family", [])
        if not fam and mid not in nofam:
            errs.append(f"{mid}: no family tag (add it to a family in `members`, or to `no_family` after review)")
        if fam and mid in nofam:
            errs.append(f"{mid}: listed in no_family but has {fam}: drop it from no_family")
    return errs


def suggest(word: str, pool) -> str:
    import difflib
    m = difflib.get_close_matches(word, list(pool), n=1)
    return f" (did you mean {m[0]!r}?)" if m else ""


# ------------------------------------------------------------------ CLI
def computed_doc(source: str, defs: dict, tags: dict[str, list[str]], pipeline: dict | None) -> dict:
    used = {t for ts in tags.values() for t in ts} | {t for ts in (pipeline or {}).values() for t in ts}
    used |= {t for t, d in defs["tags"].items() if d["group"] == "pipeline"}
    counts = {}
    for ts in list(tags.values()) + list((pipeline or {}).values()):
        for t in ts:
            counts[t] = counts.get(t, 0) + 1
    idx = tag_index(defs, used)
    for t, d in idx["tags"].items():
        d["count"] = counts.get(t, 0)
    doc = {"_doc": "Generated by tools/tags.py compute from data/tags.json + the registry; do not edit (docs/tags.md).",
           "source": source, "groups": idx["groups"], "tags": idx["tags"], "monsters": tags}
    if pipeline is not None:
        doc["pipeline"] = pipeline
    return doc


def dumps(doc: dict) -> str:
    """One monster / group per line: diff-friendly."""
    lines = ["{"]
    items = list(doc.items())
    for n, (k, v) in enumerate(items):
        end = "," if n < len(items) - 1 else ""
        if isinstance(v, dict) and k in ("tags", "monsters", "pipeline"):
            inner = [f" {json.dumps(kk)}: {json.dumps(vv)}" for kk, vv in v.items()]
            lines.append(f" {json.dumps(k)}: {{\n" + ",\n".join(" " + s for s in inner) + f"\n }}{end}")
        elif isinstance(v, list) and k == "groups":
            lines.append(f" {json.dumps(k)}: [\n" + ",\n".join("  " + json.dumps(g) for g in v) + f"\n ]{end}")
        else:
            lines.append(f" {json.dumps(k)}: {json.dumps(v)}{end}")
    return "\n".join(lines) + "\n}\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["compute", "check", "report", "show", "list"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--source", default="srd-5.2.1")
    ap.add_argument("--pipeline", action="store_true", help="compute: add pipeline tags from out/ manifests")
    a = ap.parse_args()
    defs, cur, recs = load_defs(), load_curation(a.source), load_registry(a.source)
    tags = compute(a.source, defs, cur, recs)
    errs = validate(a.source, defs, cur, recs, tags)
    if errs:
        print(f"{len(errs)} tag error(s):", file=sys.stderr)
        for e in errs:
            print("  " + e, file=sys.stderr)
        return 1
    if a.cmd == "compute":
        pipe = compute_pipeline(a.source, tags) if a.pipeline else None
        p = computed_path(a.source, write=True)
        p.write_text(dumps(computed_doc(a.source, defs, tags, pipe)))
        print(f"wrote {p.relative_to(ROOT) if p.is_relative_to(ROOT) else p}: {len(tags)} monsters, "
              f"{len({t for ts in tags.values() for t in ts})} tags in use")
        return 0
    if a.cmd == "check":
        p = computed_path(a.source)
        try:
            have = json.loads(p.read_text())
        except (OSError, ValueError):
            print(f"{p}: missing or unreadable: run tools/tags.py compute", file=sys.stderr)
            return 1
        want = computed_doc(a.source, defs, tags, have.get("pipeline"))
        if have.get("monsters") != want["monsters"] or have.get("groups") != want["groups"]:
            print(f"{p}: out of date: run tools/tags.py compute", file=sys.stderr)
            return 1
        print(f"ok: {len(tags)} monsters, {len(defs['tags'])} tags defined")
        return 0
    if a.cmd == "show":
        for mid in a.args:
            if mid not in tags:
                print(f"{mid}: unknown id{suggest(mid, tags)}")
                continue
            by = {}
            for t in tags[mid]:
                by.setdefault(defs["tags"][t]["group"], []).append(t)
            print(mid + ": " + " | ".join(f"{g}: {' '.join(ts)}" for g, ts in by.items()))
        return 0
    if a.cmd == "list":
        unknown = [t for t in a.args if t not in defs["tags"]]
        if unknown:
            print("unknown tag(s): " + ", ".join(f"{t}{suggest(t, defs['tags'])}" for t in unknown), file=sys.stderr)
            return 1
        hits = [m for m, ts in tags.items() if set(a.args) <= set(ts)]
        print(f"{len(hits)}: " + " ".join(hits))
        return 0
    report(defs, cur, tags)
    return 0


def report(defs: dict, cur: dict, tags: dict[str, list[str]]) -> None:
    counts = {}
    for ts in tags.values():
        for t in ts:
            counts[t] = counts.get(t, 0) + 1
    n = len(tags)
    print(f"{n} monsters, {len(defs['tags'])} tags defined, {len(counts)} in use, "
          f"{sum(map(len, tags.values())) / n:.1f} tags per monster\n")
    for g in defs["groups"]:
        ts = [t for t, d in defs["tags"].items() if d["group"] == g["id"]]
        if g["id"] == "pipeline":
            print(f"[{g['id']}] {' '.join(ts)}  (from out/ manifests; not in content tags)")
            continue
        print(f"[{g['id']}] " + "  ".join(f"{t} {counts.get(t, 0)}" for t in ts))
    unused = [t for t, d in defs["tags"].items() if d["group"] != "pipeline" and not counts.get(t)]
    if unused:
        print("\nunused tags: " + " ".join(unused))
    fam = {t for t, d in defs["tags"].items() if d["group"] == "family"}
    broad = {"mammal", "bird", "reptile", "fish", "insect", "hybrid", "serpent"}
    only_broad = sorted(m for m, ts in tags.items() if fam & set(ts) and not (fam & set(ts)) - broad
                        and m not in cur["no_family"])
    print(f"\nno family (reviewed, data/tags.json no_family): {len(cur['no_family'])}\n  " + " ".join(sorted(cur["no_family"])))
    print(f"\nonly a broad family ({', '.join(sorted(broad))}): {len(only_broad)}\n  " + " ".join(only_broad))
    for t in ("dragon", "draconic", "dinosaur"):
        hits = [m for m, ts in tags.items() if t in ts]
        print(f"\n{t} ({len(hits)}):\n  " + " ".join(hits))


if __name__ == "__main__":
    sys.exit(main())
