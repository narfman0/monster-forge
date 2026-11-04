#!/usr/bin/env python3
"""Import the Monster Manual (2024) stat blocks that are not in the SRD as registry stubs.

Reads a local 5etools bestiary file (bestiary-xmm.json; WotC IP, local planning use
only, never vendored into this repo) and writes data/registry/mm-2024.json with the
same record schema as srd-5.2.1.json, for every stat block without the `srd52` flag.
Only canonical facts are carried over (name, page, size, type, tags, CR, speeds, and
the names of attacks and gear); no rules or flavour text. The reference prompt
description is written by a template from those facts.

Body plans, rig templates, animation sets, wields and sockets reuse the rule engine in
tools/build_registry.py. Every record is unreviewed (`body_plan_source: "rule"`) unless
data/body_plan_overrides.json or data/body_plan_reviewed.json["mm-2024"] knows the slug;
data/equipment.json["monsters"]["mm-2024"] and data/visual_profiles.json are used when
they have an entry for the slug.

  --check-srd PATH  also compare the `srd52` stat blocks' size/type/speeds/CR against
                    data/registry/srd-5.2.1.json and write the mismatches to PATH
                    (markdown). The SRD registry is never modified.

Run:  python3 -I tools/import_names.py [--src PATH] [--out PATH] [--check-srd PATH]
Stdlib only.
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_registry as br  # noqa: E402  (tools/build_registry.py)

ROOT = br.ROOT
DEFAULT_SRC = Path.home() / "workspace/5etools-src/data/bestiary/bestiary-xmm.json"
SOURCE_ID = "mm-2024"
SIZE_LETTERS = {"T": "tiny", "S": "small", "M": "medium", "L": "large", "H": "huge", "G": "gargantuan"}
SPEED_KEYS = ("walk", "burrow", "climb", "fly", "swim")

TAG_RE = re.compile(r"\{@(\w+)\s*([^}]*)\}")
ATKR = {"m": "Melee Attack Roll:", "r": "Ranged Attack Roll:", "m,r": "Melee or Ranged Attack Roll:"}
ATK_KIND = {"m": "melee", "r": "ranged", "m,r": "melee_or_ranged"}

# Attack-name words -> what they show on the body (for the template description).
NATURAL = [
    (r"\bbites?\b|\bjaws?\b|\bfangs?\b", "toothed jaws"), (r"\bclaws?\b|\brend\b", "claws"),
    (r"\btalons?\b", "talons"), (r"\bbeak\b", "beak"), (r"\btentacles?\b", "tentacles"),
    (r"\bgore\b|\bhorns?\b|\bram\b", "horns"), (r"\bstingers?\b|\bsting\b", "stinger"),
    (r"\btail\b", "heavy tail"), (r"\bhoo(f|ves)\b", "hooves"), (r"\bpincers?\b", "pincers"),
    (r"\bpseudopods?\b", "pseudopods"), (r"\btusks?\b", "tusks"), (r"\bwings?\b", "wings"),
    (r"\bslams?\b|\bfists?\b", "heavy fists"), (r"\bspikes?\b|\bquills?\b", "spikes"),
    (r"\beye rays?\b", "eye stalks"), (r"\bmandibles?\b", "mandibles"),
]
PLAN_PHRASE = {
    "biped": "upright two-legged body with two arms",
    "quadruped": "four-legged body",
    "avian": "bird-like body with two legs and wings",
    "winged_biped": "upright two-legged body with a pair of wings on its back",
    "winged_quadruped": "four-legged body with a pair of large wings",
    "serpentine": "long limbless serpentine body",
    "aquatic": "streamlined swimming body with fins",
    "insectoid": "segmented body on many jointed legs",
    "amorphous": "shapeless gelatinous body",
    "floating": "hovering legless body",
    "swarm": "",
    "plant_object": "rooted, mostly stationary body",
    "centauroid": "humanoid torso on a four-legged lower body",
    "merfolk_naga": "humanoid torso on a long serpent tail",
    "tentacled": "body with many long tentacles",
    "radial": "radially symmetric body",
}
# Gear that shows on the body without being a held prop.
WORN = re.compile(r"armor|mail|breastplate|plate|shirt|robe|cloak|helm|amulet|holy symbol|"
                  r"pouch|component|backpack|quiver|crown|ring|boots|gloves|bracers", re.I)


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def strip_tags(s):
    """'{@atkr m}' -> attack words, '{@damage 1d6}' -> '1d6', '{@spell Fly|XPHB}' -> 'Fly'."""
    def sub(m):
        tag, body = m.group(1), m.group(2)
        if tag == "atkr" or tag == "atk":
            return ATKR.get(body.strip(), "Attack Roll:")
        if tag == "recharge":
            return f"(Recharge {body or 6})"
        return body.split("|")[0]
    prev = None
    while prev != s:
        prev, s = s, TAG_RE.sub(sub, s)
    return s


def flat_text(entries):
    out = []
    for e in entries or []:
        if isinstance(e, str):
            out.append(e)
        elif isinstance(e, dict):
            out.append(flat_text(e.get("entries") or e.get("items") or []))
    return " ".join(out)


def to_action(a):
    raw = flat_text(a.get("entries"))
    act = {"name": strip_tags(a.get("name", "")), "text": strip_tags(raw)}
    m = re.search(r"\{@atkr? ([^}]*)\}", raw)
    if m and m.group(1).strip() in ATK_KIND:
        act["attack"] = {"kind": ATK_KIND[m.group(1).strip()]}
    return act


def speed_of(sp):
    """5etools speed object -> {walk, burrow, climb, fly, swim, hover} in feet."""
    out, hover = {}, bool(sp.get("canHover"))
    for k in SPEED_KEYS:
        v = sp.get(k)
        if isinstance(v, dict):
            hover |= "hover" in (v.get("condition") or "")
            v = v.get("number")
        if v:
            out[k] = v
    ch = sp.get("choose")
    if isinstance(ch, dict):  # "climb or fly 20 ft. (DM's choice)": list both
        for k in ch.get("from", []):
            out.setdefault(k, ch.get("amount"))
    if hover and "fly" in out:
        out["hover"] = True
    return out


def cr_of(cr):
    return cr.get("cr") if isinstance(cr, dict) else cr


def adapt(m):
    """5etools monster -> (slug, name, page, d, is_swarm, unit_size), where d has the
    godot-srd-addon shape the build_registry.py helpers read."""
    t = m.get("type")
    tags, unit = [], None
    if isinstance(t, dict):
        unit = SIZE_LETTERS.get(t.get("swarmSize")) if t.get("swarmSize") else None
        tags = [x if isinstance(x, str) else x.get("tag") for x in t.get("tags") or []]
        t = t.get("type")
        if isinstance(t, dict):  # {"choose": ["celestial", "fiend"]}: first choice
            t = t["choose"][0]
    sizes = [SIZE_LETTERS[s] for s in m.get("size") or []]
    sizes.sort(key=br.SIZE_ORDER.index, reverse=True)  # SRD writes "medium or small"
    gear = []
    for g in m.get("gear") or []:
        g = g if isinstance(g, str) else g.get("item", "")
        gear.append(g.split("|")[0].title())
    d = {
        "sizes": sizes, "type": (t or "").lower(), "tags": ["swarm"] if unit else tags,
        "speed": speed_of(m.get("speed") or {}), "cr": cr_of(m.get("cr")), "gear": gear,
        "traits": [to_action(a) for a in m.get("trait") or []],
        "actions": [to_action(a) for a in m.get("action") or []],
        "bonus_actions": [to_action(a) for a in m.get("bonus") or []],
        "reactions": [to_action(a) for a in m.get("reaction") or []],
        "legendary_actions": [to_action(a) for a in m.get("legendary") or []],
    }
    # Spellcasting blocks: SRD lists them as an action named "Spellcasting".
    sec = {"action": "actions", "bonus": "bonus_actions", "reaction": "reactions"}
    for s in m.get("spellcasting") or []:
        key = sec.get(s.get("displayAs", "trait"), "traits")
        d[key].append({"name": strip_tags(s.get("name", "Spellcasting")), "text": ""})
    return slugify(m["name"]), m["name"], m.get("page"), d, bool(unit), unit


def natural_features(d):
    names = " ".join(a["name"].lower() for a in br.iter_actions(d))
    return list(dict.fromkeys(p for rx, p in NATURAL if re.search(rx, names)))


def template_desc(name, plan, conf, d, unarmed):
    """One-line visual description from canonical traits only (no source prose)."""
    parts = [name.lower()]
    if conf >= 0.8 and PLAN_PHRASE.get(plan):
        parts.append(PLAN_PHRASE[plan])
    feats = natural_features(d)
    if feats:
        parts.append(", ".join(feats))
    worn = [g.lower() for g in d.get("gear") or [] if WORN.search(g)]
    if worn:
        parts.append("wearing " + ", ".join(worn))
    if unarmed:  # empty-hands reference: no held-weapon words outside the name
        parts[1:] = [p for p in parts[1:] if not br.HELD_WORDS.search(p)]
    return ", ".join(parts)


def plural(typ):
    return typ[:-1] + "ies" if typ.endswith("y") else typ + "s"


def build_record(m, ctx):
    slug, name, page, d, is_swarm, unit = adapt(m)
    sizes, typ, speed = d["sizes"], d["type"], d["speed"]
    if not sizes or not typ:
        sys.exit(f"{slug}: could not determine size/type")
    size = sizes[0]
    rule_plan, rule_conf = br.rule_body_plan(slug, typ, speed, is_swarm)
    ov = ctx["overrides"].get(slug)
    if ov:
        plan, conf, src = ov["body_plan"], ov["confidence"], "override"
        rig, note = ov.get("rig_template") or br.rig_for(plan, slug), ov.get("note")
    else:
        plan, conf = rule_plan, rule_conf
        src = "rule_reviewed" if slug in ctx["reviewed"] else "rule"
        conf = 1.0 if slug in ctx["reviewed"] else conf
        rig, note = br.rig_for(plan, slug), None

    derived = br.derive_wields(d)
    eq = ctx["equip"].get(slug)
    curated = eq is not None
    if eq is None:
        eq = {"wields": [w for w in derived if w in ctx["props"]]}
    wields = list(eq.get("wields", []))
    hands = eq.get("hands", plan in br.HAND_PLANS) or bool(wields)
    arms = eq.get("arms", 2 if hands else 0)
    armed = bool(wields) or bool(eq.get("armed_look"))
    unarmed_ref = hands and armed

    vis = ctx["visuals"].get(slug)
    desc = vis["desc"] if vis else template_desc(name, plan, conf, d, unarmed_ref)
    scale = (vis or {}).get("scale_m", br.SIZE_SCALE_M[size])

    rec = {
        "id": slug, "name": name, "source": SOURCE_ID, "page": page,
        "size": size, "size_options": sizes, "type": typ, "tags": d["tags"],
        "cr": d["cr"], "speeds": speed,
        "body_plan": plan, "body_plan_confidence": conf, "body_plan_source": src,
        "rig_template": rig, "animation_set": br.animation_set(plan, speed, d),
        "scale_m": scale, "hands": hands, "wields": wields, "shield": "shield" in wields,
        "sockets": br.sockets_for(hands, arms, armed), "unarmed_reference": unarmed_ref,
        "reference_prompt": br.reference_prompt(
            desc, plan, size.capitalize(), f"swarm of {unit} {plural(typ)}" if is_swarm else typ, is_swarm),
    }
    if unit:
        rec["swarm_unit_size"] = unit
    if note:
        rec["body_plan_note"] = note
    if src == "override" and plan != rule_plan:
        rec["body_plan_rule_guess"] = rule_plan
    if arms not in (0, 2):
        rec["arms"] = arms
    if eq.get("aka"):
        rec["wields_aka"] = eq["aka"]
    if eq.get("note"):
        rec["equipment_note"] = eq["note"]
    review = {
        "equipment": not curated and (plan in br.HAND_PLANS or bool(derived)),
        "unknown_props": [w for w in derived if w not in ctx["props"]],
        "hand_profile": vis is None,
        "held_word_in_name": unarmed_ref and bool(br.HELD_WORDS.search(name)),
    }
    return rec, d, review


def check_srd(srd_monsters, srd_path, out_path):
    """Size/type/speeds/CR of the `srd52` blocks vs the SRD registry -> markdown table."""
    reg = {r["id"]: r for r in json.loads(srd_path.read_text())["monsters"]}
    rows, seen = [], set()
    for m in srd_monsters:
        slug, name, _, d, is_swarm, unit = adapt(m)
        r = reg.get(slug)
        if r is None:
            rows.append((slug, "id", "(not in SRD registry)", name))
            continue
        seen.add(slug)
        for field, mine, theirs in (
                ("size", d["sizes"], r["size_options"]), ("type", d["type"], r["type"]),
                ("cr", d["cr"], r["cr"]), ("speeds", d["speed"], r["speeds"]),
                ("swarm_unit_size", unit, r.get("swarm_unit_size"))):
            if mine != theirs:
                rows.append((slug, field, json.dumps(theirs), json.dumps(mine)))
    for s in sorted(set(reg) - seen):
        rows.append((s, "id", reg[s]["name"], "(no srd52 block with this slug)"))
    lines = ["# SRD 5.2.1 registry vs 5etools bestiary-xmm.json (srd52 blocks)", "",
             f"{len(srd_monsters)} srd52 blocks, {len(seen)} matched by slug, {len(rows)} mismatches.", "",
             "| id | field | srd-5.2.1.json | 5etools |", "|---|---|---|---|"]
    lines += [f"| {a} | {b} | `{c}` | `{e}` |" for a, b, c, e in rows]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out", type=Path, default=ROOT / "data/registry/mm-2024.json")
    ap.add_argument("--check-srd", type=Path, metavar="PATH",
                    help="write the srd52 vs srd-5.2.1.json mismatch table here")
    args = ap.parse_args()

    blocks = json.loads(args.src.read_text())["monster"]
    for m in blocks:
        if "_copy" in m:
            sys.exit(f"{m['name']}: _copy entries are not supported")
    srd = [m for m in blocks if m.get("srd52")]
    mm = [m for m in blocks if not m.get("srd52")]

    equipment = json.loads((ROOT / "data/equipment.json").read_text())
    ctx = {
        "overrides": json.loads((ROOT / "data/body_plan_overrides.json").read_text()),
        "reviewed": set(json.loads((ROOT / "data/body_plan_reviewed.json").read_text()).get(SOURCE_ID, [])),
        "visuals": json.loads((ROOT / "data/visual_profiles.json").read_text()),
        "props": equipment["props"],
        "equip": equipment["monsters"].get(SOURCE_ID, {}),
    }
    srd_ids = {r["id"] for r in json.loads((ROOT / "data/registry/srd-5.2.1.json").read_text())["monsters"]}

    records, reviews = [], {}
    for m in mm:
        rec, _, rv = build_record(m, ctx)
        if rec["id"] in srd_ids:
            sys.exit(f"{rec['id']}: MM-only block collides with an SRD id")
        records.append(rec)
        reviews[rec["id"]] = rv
    dupes = [k for k, v in Counter(r["id"] for r in records).items() if v > 1]
    if dupes:
        sys.exit(f"duplicate ids: {dupes}")
    records.sort(key=lambda r: r["id"])

    out = {
        "source": SOURCE_ID,
        "license": "WotC IP (Monster Manual 2024): derived names and stats, personal use only, "
                   "never publish. Not covered by CC-BY-4.0.",
        "note": "Generated by tools/import_names.py from a local 5etools bestiary-xmm.json "
                "(stat blocks without the srd52 flag). Unreviewed rule-based stubs.",
        "count": len(records),
        "body_plans": br.BODY_PLANS,
        "monsters": records,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")

    rel = args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out
    print(f"wrote {len(records)} records -> {rel}  ({len(srd)} srd52 blocks skipped)")
    counts = Counter(r["body_plan"] for r in records)
    for p in br.BODY_PLANS:
        print(f"  {p:18s} {counts.get(p, 0)}")
    print("  sizes: " + ", ".join(f"{s}={n}" for s, n in sorted(
        Counter(r["size"] for r in records).items(), key=lambda kv: br.SIZE_ORDER.index(kv[0]))))
    src = Counter(r["body_plan_source"] for r in records)
    print("  sources: " + ", ".join(f"{k}={v}" for k, v in sorted(src.items())))
    n = lambda f: sum(1 for r in records if f(r))
    print(f"  hands={n(lambda r: r['hands'])} wields={n(lambda r: r['wields'])} "
          f"shield={n(lambda r: r['shield'])} unarmed_reference={n(lambda r: r['unarmed_reference'])} "
          f"winged={n(lambda r: r['body_plan'] in ('winged_biped', 'winged_quadruped', 'avian'))} "
          f"fly={n(lambda r: 'fly' in r['speeds'])} swarm={n(lambda r: r['body_plan'] == 'swarm')}")
    low = [r["id"] for r in records if r["body_plan_confidence"] < 0.8]
    print(f"  low-confidence (<0.8): {len(low)}\n    " + " ".join(low))
    eq = sorted(s for s, v in reviews.items() if v["equipment"])
    print(f"  need a data/equipment.json mm-2024 entry: {len(eq)}\n    " + " ".join(eq))
    unk = {s: v["unknown_props"] for s, v in reviews.items() if v["unknown_props"]}
    if unk:
        print(f"  stat-block weapons with no prop spec: {unk}")
    hp = [s for s, v in reviews.items() if v["hand_profile"]]
    print(f"  template prompts (no visual_profiles.json entry): {len(hp)}")
    hw = sorted(s for s, v in reviews.items() if v["held_word_in_name"])
    if hw:
        print(f"  unarmed_reference but the name has a held-weapon word: {' '.join(hw)}")

    if args.check_srd:
        rows = check_srd(srd, ROOT / "data/registry/srd-5.2.1.json", args.check_srd)
        print(f"srd52 cross-check: {len(rows)} mismatches -> {args.check_srd}")


if __name__ == "__main__":
    main()
