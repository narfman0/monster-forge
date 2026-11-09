#!/usr/bin/env python3
"""Review verdicts: the viewer's review mode writes them, re-rolls read them.

The verdicts live in each monster's override file, data/overrides/<source>/<id>.json, under
the key `review` (validated by tools/overrides.py):

    "review": {"date": "2026-11-08", "ref": "ok", "mesh": "bad", "paint": "ok", "rig": "ok",
               "clips": {"walk": "ok", "attack_melee": "bad"}, "note": "jaw melts at LOD1"}

Untouched aspects are left out. The viewer writes them with `tools/viewer.py --review`
(POST /api/review/<id>, viewer/README.md "Review mode"); this tool summarises them:

    python3 -I tools/review.py report                 # print + write out/<source>/_review.md
    python3 -I tools/review.py ids --bad mesh         # ids, comma-joined, for batch.py --ids
    python3 -I tools/review.py ids --bad mesh,paint --plan quadruped
    python3 -I tools/review.py ids --bad clip:walk --lines > ~/.cache/rerun.txt
    python3 -I tools/batch.py run --ids "$(python3 -I tools/review.py ids --bad mesh)" --stages ref,mesh,qa
    python3 -I tools/batch.py run --ids @~/.cache/rerun.txt --stages rig,anim

Aspects for --bad / --ok: ref, mesh, paint, rig, clips (any clip), clip:<name>, any
(any of them). Several (comma list or repeated) match when any one does. --unreviewed
lists monsters with no verdict at all, --incomplete those missing a verdict on something
they have (a stage done, a clip found), --noted those with a note.

The file is edited in place: other keys keep their order and text; only the `review`
member is replaced, added at the end or removed (a file left empty is deleted). The file's
mtime is kept, because tools/batch.py dates ref_pick pins and prompt edits by it (a verdict
is not forge input and must not make an old pin look freshly chosen).
Stdlib only.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overrides  # noqa: E402  (tools/overrides.py: the `review` validator, path_for)
import sources  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT / "out"
KEY = "review"
ASPECTS = overrides.REVIEW_ASPECTS  # ref mesh paint rig
VERDICTS = overrides.REVIEW_VERDICTS  # ok bad
ORDER = ("date",) + ASPECTS + ("clips", "note")


def set_overrides_dir(d: str | Path) -> None:
    """Point every override read/write (this module, tools/overrides.py) at another directory
    (tests: a scratch copy of data/overrides/<source>/)."""
    base = Path(d).expanduser().resolve()
    overrides.path_for = lambda source, mid: base / f"{mid}.json"


def today() -> str:
    return time.strftime("%Y-%m-%d")


def clean(body) -> dict:
    """The review object to store from a client body: None / "" / {} values dropped, keys in
    ORDER, clip verdicts sorted by name, the note stripped. {} when nothing is left. Raises
    ValueError on anything overrides._review rejects."""
    if not isinstance(body, dict):
        raise ValueError("review must be a JSON object")
    b = {k: v for k, v in body.items() if v is not None and v != "" and v != {}}
    if isinstance(b.get("clips"), dict):
        b["clips"] = {c: v for c, v in sorted(b["clips"].items()) if v is not None}
        if not b["clips"]:
            del b["clips"]
    if isinstance(b.get("note"), str):
        b["note"] = b["note"].strip()
        if not b["note"]:
            del b["note"]
    err = overrides._review(b)
    if err:
        raise ValueError(f"review: {err}")
    return {k: b[k] for k in ORDER if k in b}


def has_verdict(rv: dict | None) -> bool:
    return bool(rv) and (any(k in rv for k in ASPECTS) or bool(rv.get("clips")) or bool(rv.get("note")))


def _members(text: str) -> list[tuple[str, int, int, int]]:
    """Top-level members of a JSON object text: (key, key_start, value_start, value_end)."""
    dec = json.JSONDecoder()
    ws = " \t\r\n"
    i = 0
    while i < len(text) and text[i] in ws:
        i += 1
    if i >= len(text) or text[i] != "{":
        raise ValueError("not a JSON object")
    i += 1
    out = []
    while True:
        while text[i] in ws:
            i += 1
        if text[i] == "}":
            return out
        if text[i] == ",":
            i += 1
            continue
        ks = i
        key, i = dec.raw_decode(text, i)
        while text[i] in ws:
            i += 1
        if text[i] != ":":
            raise ValueError(f"expected ':' at {i}")
        i += 1
        while text[i] in ws:
            i += 1
        vs = i
        _, i = dec.raw_decode(text, i)
        out.append((key, ks, vs, i))


def _dump_member(value) -> str:
    """value as a 2-space-indented member of a top-level object (the overrides.save layout)."""
    return json.dumps(value, indent=2).replace("\n", "\n  ")


def splice(text: str, review: dict | None) -> str:
    """The override file text with its `review` member replaced (review), added at the end
    or removed (review falsy); every other byte of the file is kept."""
    mem = _members(text)
    hit = [m for m in mem if m[0] == KEY]
    if hit:
        _, ks, vs, ve = hit[0]
        if review:
            return text[:vs] + _dump_member(review) + text[ve:]
        idx = mem.index(hit[0])
        if idx > 0:  # drop from the end of the previous value (its comma) to the end of this one
            return text[:mem[idx - 1][3]] + text[ve:]
        if len(mem) > 1:  # first member: drop up to the next key
            return text[:ks] + text[mem[1][1]:]
        return "{}\n"
    if not review:
        return text
    if not mem:
        return "{\n  " + json.dumps(KEY) + ": " + _dump_member(review) + "\n}\n"
    end = mem[-1][3]
    return text[:end] + ",\n  " + json.dumps(KEY) + ": " + _dump_member(review) + text[end:]


def load(source: str, mid: str) -> dict:
    """The stored review of one monster ({} when none or unreadable)."""
    try:
        rv = overrides.load(source, mid).get(KEY)
    except (OSError, ValueError):
        return {}
    return rv if isinstance(rv, dict) else {}


def load_all(source: str, ids) -> dict[str, dict]:
    """{id: review} for the ids with a stored review."""
    out = {}
    for mid in ids:
        p = overrides.path_for(source, mid)
        if not p.is_file():
            continue
        rv = load(source, mid)
        if rv:
            out[mid] = rv
    return out


def save(source: str, mid: str, body: dict, date: str | None = None) -> dict:
    """Write one monster's review (cleaned, dated today) into its override file, preserving
    the rest of the file. Returns the stored review ({} = removed). Raises ValueError."""
    rv = clean(body)
    rv.pop("date", None)
    if has_verdict(rv):
        rv = {"date": date or today(), **rv}
        rv = {k: rv[k] for k in ORDER if k in rv}
    else:
        rv = {}
    p = overrides.path_for(source, mid)
    text = p.read_text() if p.is_file() else ""
    if text.strip():
        json.loads(text)  # refuse to touch a file that does not parse
        new = splice(text, rv)
    else:
        new = splice("{}\n", rv)
    data = json.loads(new)
    errs = overrides.validate(data)
    if errs:
        raise ValueError("; ".join(errs))
    if data.get(KEY, {}) != rv:
        raise ValueError("splice check failed")
    if new == text:
        return rv
    if not data:
        p.unlink(missing_ok=True)
        return rv
    p.parent.mkdir(parents=True, exist_ok=True)
    st = p.stat() if p.is_file() else None
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(new)
    if st:  # keep the file's mtime: tools/batch.py dates ref_pick pins and prompt edits by it
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    tmp.replace(p)
    return rv


# ---------------------------------------------------------------- summaries
def applicable(source: str, monsters: list[dict]) -> dict[str, dict]:
    """{id: {"aspects": [aspects whose stage is done], "clips": [clips found]}}, from the
    viewer's own stage rules (what the page shows is what can be judged)."""
    import viewer  # lazy: the viewer imports this module
    out = {}
    for rec in monsters:
        d = viewer.OUT_ROOT / source / rec["id"]
        man = viewer.load_json(d / "manifest.json") if d.is_dir() else {}
        st = viewer.stage_status(d, man)
        exp = list(rec.get("animation_set") or [])
        found = st["found_clips"]
        clips = [c for c in exp if c in found] + [c for c in found if c not in exp]
        out[rec["id"]] = {"aspects": [a for a in ASPECTS if st["stages"][a]], "clips": clips}
    return out


def complete(rv: dict, app: dict) -> bool:
    return all(a in rv for a in app["aspects"]) and all(c in (rv.get("clips") or {}) for c in app["clips"])


def bad_aspects(rv: dict) -> list[str]:
    return [a for a in ASPECTS if rv.get(a) == "bad"] + [f"clip:{c}" for c, v in (rv.get("clips") or {}).items()
                                                        if v == "bad"]


def match(rv: dict, wants: list[str], verdict: str) -> bool:
    for w in wants:
        if w == "any":
            if any(rv.get(a) == verdict for a in ASPECTS) or verdict in (rv.get("clips") or {}).values():
                return True
        elif w == "clips":
            if verdict in (rv.get("clips") or {}).values():
                return True
        elif w.startswith("clip:"):
            if (rv.get("clips") or {}).get(w[5:]) == verdict:
                return True
        elif rv.get(w) == verdict:
            return True
    return False


def parse_aspects(vals) -> list[str]:
    out = [a.strip() for v in vals or [] for a in v.split(",") if a.strip()]
    for a in out:
        if a not in ASPECTS + ("clips", "any") and not (a.startswith("clip:") and overrides.REVIEW_CLIP.match(a[5:])):
            raise SystemExit(f"unknown aspect {a!r}: use {', '.join(ASPECTS)}, clips, clip:<name> or any")
    return out


def registry(source: str) -> list[dict]:
    return json.loads(sources.registry_path(source).read_text()).get("monsters", [])


def report_md(source: str, monsters: list[dict], reviews: dict[str, dict], app: dict[str, dict]) -> str:
    total = len(monsters)
    plan_of = {m["id"]: m.get("body_plan", "?") for m in monsters}
    started = [m["id"] for m in monsters if has_verdict(reviews.get(m["id"]))]
    done = [i for i in started if complete(reviews[i], app[i])]
    L = [f"# Review verdicts: {source}", "",
         f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `tools/review.py report` from the `review` key of "
         f"`data/overrides/{source}/<id>.json` (written by the viewer's review mode, viewer/README.md).", "",
         f"- monsters: {total}",
         f"- with any verdict: {len(started)}",
         f"- fully reviewed (every done stage and every found clip judged): {len(done)}",
         f"- with any bad: {sum(1 for i in started if bad_aspects(reviews[i]))}", "",
         "## Counts per aspect", "",
         "| aspect | ok | bad | not judged (of applicable) | applicable (stage done / clip found) |", "|---|---:|---:|---:|---:|"]
    for a in ASPECTS:
        n_app = sum(1 for m in monsters if a in app[m["id"]]["aspects"])
        ok = sum(1 for r in reviews.values() if r.get(a) == "ok")
        bad = sum(1 for r in reviews.values() if r.get(a) == "bad")
        todo = sum(1 for m in monsters if a in app[m["id"]]["aspects"] and a not in reviews.get(m["id"], {}))
        L.append(f"| {a} | {ok} | {bad} | {todo} | {n_app} |")
    clip_names = sorted({c for a in app.values() for c in a["clips"]} |
                        {c for r in reviews.values() for c in (r.get("clips") or {})})
    for c in clip_names:
        n_app = sum(1 for a in app.values() if c in a["clips"])
        ok = sum(1 for r in reviews.values() if (r.get("clips") or {}).get(c) == "ok")
        bad = sum(1 for r in reviews.values() if (r.get("clips") or {}).get(c) == "bad")
        todo = sum(1 for i, a in app.items() if c in a["clips"] and c not in (reviews.get(i, {}).get("clips") or {}))
        L.append(f"| clip:{c} | {ok} | {bad} | {todo} | {n_app} |")
    by_aspect: dict[str, list[str]] = collections.defaultdict(list)
    by_plan: dict[str, list[str]] = collections.defaultdict(list)
    for mid in sorted(reviews):
        bad = bad_aspects(reviews[mid])
        for a in bad:
            by_aspect[a].append(mid)
        if bad:
            by_plan[plan_of.get(mid, "?")].append(f"{mid} ({', '.join(bad)})")
    L += ["", "## Bad, by aspect", ""]
    if not by_aspect:
        L.append("none")
    for a in list(ASPECTS) + sorted(k for k in by_aspect if k.startswith("clip:")):
        if by_aspect.get(a):
            L.append(f"- **{a}** ({len(by_aspect[a])}): {', '.join(by_aspect[a])}")
    L.append("")
    if by_aspect:
        L += ["Re-roll inputs: `python3 -I tools/review.py ids --bad <aspect>` (comma-joined, for "
              "`tools/batch.py run --ids`).", ""]
    L += ["## Bad, by body plan", ""]
    if not by_plan:
        L.append("none")
    for plan in sorted(by_plan):
        L.append(f"- **{plan}** ({len(by_plan[plan])}): {'; '.join(by_plan[plan])}")
    L += ["", "## Notes", ""]
    notes = [(mid, reviews[mid]) for mid in sorted(reviews) if reviews[mid].get("note")]
    if not notes:
        L.append("none")
    for mid, r in notes:
        bad = bad_aspects(r)
        note = " ".join(r["note"].split())
        L.append(f"- **{mid}** ({r.get('date', '?')}{'; bad: ' + ', '.join(bad) if bad else ''}): {note}")
    return "\n".join(L) + "\n"


def cmd_report(args) -> int:
    monsters = registry(args.source)
    reviews = load_all(args.source, [m["id"] for m in monsters])
    md = report_md(args.source, monsters, reviews, applicable(args.source, monsters))
    out = Path(args.out) if args.out else OUT_ROOT / args.source / "_review.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    sys.stdout.write(md)
    print(f"\nwrote {out}", file=sys.stderr)
    return 0


def cmd_ids(args) -> int:
    monsters = registry(args.source)
    reviews = load_all(args.source, [m["id"] for m in monsters])
    bad, ok = parse_aspects(args.bad), parse_aspects(args.ok)
    plans = {p for v in args.plan or [] for p in v.split(",") if p}
    tagged = None
    if args.tag:
        import tags as taglib  # lazy
        want = {t for v in args.tag for t in v.split(",") if t}
        tags = taglib.compute(args.source, None, None, monsters)
        tagged = {i for i, ts in tags.items() if want <= set(ts)}
    app = applicable(args.source, monsters) if args.incomplete else {}
    out = []
    for m in monsters:
        mid, rv = m["id"], reviews.get(m["id"], {})
        if plans and m.get("body_plan") not in plans:
            continue
        if tagged is not None and mid not in tagged:
            continue
        if bad and not match(rv, bad, "bad"):
            continue
        if ok and not match(rv, ok, "ok"):
            continue
        if args.unreviewed and has_verdict(rv):
            continue
        if args.incomplete and complete(rv, app[mid]):
            continue
        if args.noted and not rv.get("note"):
            continue
        out.append(mid)
    if out:
        print("\n".join(out) if args.lines else ",".join(out))
    if args.count:
        print(f"{len(out)} ids", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--source", default="srd-5.2.1")
        p.add_argument("--overrides-dir", help="read another overrides directory (tests)")

    rp = sub.add_parser("report", help="print and write out/<source>/_review.md")
    common(rp)
    rp.add_argument("--out", help="write here instead of out/<source>/_review.md")
    ip = sub.add_parser("ids", help="print ids matching the filters (comma-joined; --lines for one per line)")
    common(ip)
    ip.add_argument("--bad", action="append", help="aspects judged bad: ref, mesh, paint, rig, clips, clip:<name>, any")
    ip.add_argument("--ok", action="append", help="aspects judged ok (same names)")
    ip.add_argument("--unreviewed", action="store_true", help="no verdict at all")
    ip.add_argument("--incomplete", action="store_true", help="a done stage or a found clip without a verdict")
    ip.add_argument("--noted", action="store_true", help="has a note")
    ip.add_argument("--plan", action="append", help="body plans (comma list)")
    ip.add_argument("--tag", action="append", help="content tags, all must match (comma list; docs/tags.md)")
    ip.add_argument("--lines", action="store_true", help="one id per line instead of a comma list")
    ip.add_argument("--count", action="store_true", help="also print the count to stderr")
    args = ap.parse_args(argv)
    if args.overrides_dir:
        set_overrides_dir(args.overrides_dir)
    return {"report": cmd_report, "ids": cmd_ids}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
