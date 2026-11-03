#!/usr/bin/env python3
"""Build the monster-forge asset registry for SRD 5.2.1.

Reads the godot-srd-addon monsters.json (read-only) plus two hand-curated
files in data/ and writes data/registry/srd-5.2.1.json.

  data/body_plan_overrides.json  slug -> {body_plan, confidence, rig_template?, note?}
  data/body_plan_reviewed.json   {source: [slugs whose rule result was hand-confirmed]}
  data/visual_profiles.json      slug -> {desc, scale_m?}
  data/equipment.json            prop specs + per-monster hands/wields (hand-reviewed)

Also writes data/registry/props.json (one entry per held weapon/shield/implement).

Run:  python3 -I tools/build_registry.py [--src PATH] [--out PATH]
Stdlib only.
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = Path.home() / "workspace/godot-srd-addon/addons/srd/data/srd-5.2.1/monsters.json"
SOURCE_ID = "srd-5.2.1"

BODY_PLANS = [
    "biped", "quadruped", "avian", "winged_biped", "winged_quadruped",
    "serpentine", "aquatic", "insectoid", "amorphous", "floating", "swarm",
    "plant_object", "centauroid", "merfolk_naga", "tentacled", "radial",
]

# Default rig template per body plan (see docs/body-plans.md).
DEFAULT_RIG = {
    "biped": "rigify.basic_human",
    "quadruped": "rigify.basic_quadruped",
    "avian": "rigify.bird",
    "winged_biped": "custom.human_winged",
    "winged_quadruped": "custom.dragon",
    "serpentine": "custom.spine_chain",
    "aquatic": "rigify.shark",
    "insectoid": "custom.hexapod",
    "amorphous": "custom.blob",
    "floating": "custom.ghost",
    "swarm": "custom.swarm",
    "plant_object": "custom.rigid_prop",
    "centauroid": "custom.centaur",
    "merfolk_naga": "custom.naga",
    "tentacled": "custom.tentacle_hub",
    "radial": "custom.radial_tripod",
}

# Quadruped rig refinement by slug token.
QUAD_RIG_TOKENS = {
    "rigify.horse": {"horse", "pony", "mule", "camel", "elk", "deer", "unicorn", "warhorse", "goat"},
    "rigify.wolf": {"wolf", "dog", "jackal", "hyena", "worg", "hound", "mastiff"},
    "rigify.cat": {"cat", "lion", "tiger", "panther", "saber"},
}

# Name-token rules, checked before type rules.
TOKEN_RULES = [
    ({"snake", "serpent", "worm", "naga"}, "serpentine", 0.9),
    ({"spider", "centipede", "scorpion", "beetle", "wasp", "crab", "ankheg"}, "insectoid", 0.9),
    ({"shark", "piranha", "whale", "seahorse"}, "aquatic", 0.9),
    ({"octopus", "kraken", "roper"}, "tentacled", 0.9),
    ({"eagle", "hawk", "owl", "raven", "vulture", "bat", "roc"}, "avian", 0.85),
]

SIZE_ORDER = ["tiny", "small", "medium", "large", "huge", "gargantuan"]
SIZE_SCALE_M = {"tiny": 0.5, "small": 1.0, "medium": 1.8, "large": 3.0, "huge": 5.0, "gargantuan": 10.0}

UPRIGHT_PLANS = {"biped", "winged_biped", "centauroid", "merfolk_naga"}
# Plans whose bodies have graspable hands unless data/equipment.json says otherwise.
HAND_PLANS = UPRIGHT_PLANS

# Rule-based weapon detection from gear and attack names, used to cross-check the
# hand-curated wields lists (and as the fallback for unreviewed tiers). Specific
# names first; the generic ones only fire when nothing specific matched.
WEAPON_WORDS = [
    ("hand crossbow", "hand-crossbow"), ("light crossbow", "light-crossbow"),
    ("heavy crossbow", "heavy-crossbow"), ("light hammer", "light-hammer"),
    ("greatsword", "greatsword"), ("longsword", "longsword"), ("shortsword", "shortsword"),
    ("scimitar", "scimitar"), ("rapier", "rapier"), ("dagger", "dagger"), ("sickle", "sickle"),
    ("greataxe", "greataxe"), ("battleaxe", "battleaxe"), ("handaxe", "handaxe"),
    ("greatclub", "greatclub"), ("morningstar", "morningstar"), ("warhammer", "warhammer"),
    ("maul", "maul"), ("flail", "flail"), ("mace", "mace"), ("club", "club"),
    ("quarterstaff", "quarterstaff"), ("glaive", "glaive"), ("halberd", "halberd"),
    ("pike", "pike"), ("trident", "trident"), ("javelin", "javelin"), ("spear", "spear"),
    ("lance", "lance"), ("harpoon", "harpoon"), ("whip", "whip"), ("fork", "fork"),
    ("longbow", "longbow"), ("shortbow", "shortbow"), ("sling", "sling"), ("net", "net"),
    ("pistol", "pistol"), ("musket", "musket"), ("wand", "wand"), ("shield", "shield"),
]
WEAPON_GENERIC = [("sword", "longsword"), ("blade", "longsword"), ("bow", "longbow"),
                  ("axe", "greataxe"), ("hammer", "warhammer"), ("cudgel", "club"),
                  ("staff", "quarterstaff")]
# Words a reference description must not contain when the creature is drawn unarmed.
HELD_WORDS = re.compile(
    r"\b(swords?|longswords?|greatswords?|shortswords?|scimitars?|rapiers?|cutlass(es)?|"
    r"daggers?|sickles?|axes?|greataxes?|battleaxes?|handaxes?|clubs?|greatclubs?|maces?|"
    r"mauls?|morningstars?|flails?|hammers?|warhammers?|spears?|pikes?|glaives?|halberds?|"
    r"tridents?|javelins?|lances?|harpoons?|whips?|forks?|staffs?|staves|quarterstaffs?|"
    r"bows?|longbows?|shortbows?|crossbows?|slings?|nets?|pistols?|wands?|shields?|"
    r"weapons?|holding|wielding)\b", re.I)


def tokens(slug):
    return set(slug.split("-"))


def parse_sizes(raw):
    """'medium or small' -> ['medium', 'small']"""
    return [s for s in re.split(r"\s+or\s+|,\s*", (raw or "").lower()) if s in SIZE_ORDER]


def parse_swarm(type_line):
    """'Medium Swarm of Tiny Beasts, Unaligned' -> (size, type, unit_size, alignment)"""
    m = re.match(r"(\w+) Swarm of (\w+) (\w+?)s?, (.+)$", type_line)
    if not m:
        raise ValueError(f"unparsed swarm type_line: {type_line!r}")
    size, unit, typ, align = m.groups()
    return size.lower(), typ.lower(), unit.lower(), align.lower()


def rule_body_plan(slug, typ, speed, is_swarm):
    """Heuristic classification. Returns (plan, confidence)."""
    t = tokens(slug)
    walk = speed.get("walk", 0) or 0
    fly = speed.get("fly", 0) or 0
    swim = speed.get("swim", 0) or 0
    if is_swarm:
        return "swarm", 0.95
    if typ == "ooze":
        return "amorphous", 0.95
    if typ == "dragon" and fly and "dragon" in t and not t & {"half", "pseudo"}:
        return "winged_quadruped", 0.95
    for toks, plan, conf in TOKEN_RULES:
        if t & toks:
            return plan, conf
    if typ in ("humanoid", "giant"):
        return "biped", 0.9
    if typ == "beast":
        if fly and walk <= 10:
            return "avian", 0.8
        if swim and walk <= 5:
            return "aquatic", 0.8
        return "quadruped", 0.75
    if typ == "undead" and speed.get("hover"):
        return "floating", 0.8
    if fly and typ in ("fiend", "celestial", "fey", "elemental", "construct"):
        return "winged_biped", 0.5
    return "biped", 0.5


def rig_for(plan, slug):
    if plan == "quadruped":
        for rig, toks in QUAD_RIG_TOKENS.items():
            if tokens(slug) & toks:
                return rig
    return DEFAULT_RIG[plan]


def iter_actions(d):
    for sec in ("actions", "bonus_actions", "legendary_actions"):
        for a in d.get(sec) or []:
            yield a


def attack_kinds(d):
    kinds = set()
    for a in iter_actions(d):
        k = (a.get("attack") or {}).get("kind")
        text = a.get("text", "")
        if k == "melee_or_ranged" or "Melee or Ranged Attack" in text:
            kinds |= {"melee", "ranged"}
        elif k == "ranged" or "Ranged Attack Roll" in text:
            kinds.add("ranged")
        elif k == "melee" or "Melee Attack Roll" in text:
            kinds.add("melee")
    return kinds


def animation_set(plan, speed, d):
    walk = speed.get("walk", 0) or 0
    fly = speed.get("fly", 0) or 0
    swim = speed.get("swim", 0) or 0
    burrow = speed.get("burrow", 0) or 0
    clips = ["idle"]
    # Primary locomotion: "fly" replaces "walk" for pure flyers and floaters,
    # "swim" replaces it for aquatic bodies and creatures that barely move on land.
    if fly and (walk <= 10 or plan == "floating"):
        clips.append("fly")
    elif plan == "aquatic" or (swim and (walk <= 10 or swim >= 3 * walk)):
        clips.append("swim")
    else:
        clips.append("walk")
        if fly:
            clips.append("fly")
        if swim and swim >= walk:
            clips.append("swim")
    if burrow and burrow >= walk / 2:
        clips.append("burrow")
    kinds = attack_kinds(d)
    if "melee" in kinds:
        clips.append("attack_melee")
    if "ranged" in kinds:
        clips.append("attack_ranged")
    names = [a.get("name", "") for a in iter_actions(d)]
    if any("Breath" in n for n in names):
        clips.append("breath")
    if any(n.startswith("Spellcasting") for n in names):
        clips.append("cast")
    if not kinds and "breath" not in clips and "cast" not in clips:
        # No attacks at all (shrieker, seahorse): one generic special/emote clip
        # if the creature has any action or reaction to animate.
        if names or d.get("reactions"):
            clips.append("attack_special")
    clips += ["hit", "death"]
    return list(dict.fromkeys(clips))


def weapon_names(text):
    t = text.lower()
    found = [w for k, w in WEAPON_WORDS if re.search(rf"\b{k}s?\b", t)]
    if not found:
        found = [w for k, w in WEAPON_GENERIC if re.search(rf"\b{k}s?\b", t)]
    return found


def derive_wields(d):
    """Weapon prop ids named by gear or attack actions (unordered, deduplicated)."""
    out = []
    for g in d.get("gear") or []:
        out += weapon_names(re.sub(r"\s*\(.*?\)", "", g))
    for a in iter_actions(d):
        if (a.get("attack") or {}) or "Attack Roll" in a.get("text", ""):
            out += weapon_names(re.sub(r"\s*\(.*?\)", "", a.get("name", "")))
    return list(dict.fromkeys(out))


def sockets_for(hands, arms, armed):
    """Attachment points the rig must provide (bone names, see docs/equipment.md)."""
    if not hands:
        return []
    pairs = max(1, arms // 2) if arms > 4 else 1
    if pairs == 1:
        socks = ["hand_r", "hand_l"]
    else:
        socks = [f"hand_{side}{i}" for i in range(1, pairs + 1) for side in "rl"]
    if armed:
        socks += ["back", "hip_l", "hip_r", "head"]
    return socks


def reference_prompt(desc, plan, size, typ, is_swarm):
    if plan in UPRIGHT_PLANS:
        pose = "A-pose, front view"
    elif plan == "floating":
        pose = "neutral hovering pose, front view"
    elif plan in ("winged_quadruped", "avian"):
        pose = "neutral standing stance with wings partly spread, side view"
    elif plan == "aquatic":
        pose = "neutral swimming pose, side view"
    elif plan == "serpentine":
        pose = "body in a loose S-curve, side view"
    else:
        pose = "neutral stance, side view"
    subject = desc
    if is_swarm:
        subject = f"{desc}; show one representative individual creature plus a small loose cluster"
    return (f"{subject}. {size} {typ}. Low-poly game asset, full body, {pose}, "
            f"plain gray background, clean silhouette, flat even lighting.")


def prop_prompt(spec):
    """Same shape as the monster prompts, so the house style's strip/suffix rules apply."""
    return (f"{spec['desc']}. Single {spec['category'].replace('_', ' ')} prop, no hands, no "
            f"character. Low-poly game asset, the whole object, standing vertical, "
            f"flat side view, plain gray background, clean silhouette, flat even lighting.")


def build_props(records, specs):
    used = {}
    for r in records:
        for w in r["wields"]:
            u = used.setdefault(w, {})
            u[r["id"]] = u.get(r["id"], 0) + 1
    props = []
    for pid, spec in specs.items():
        users = used.get(pid, {})
        if not users:
            continue
        variants = {}
        for r in records:
            name = (r.get("wields_aka") or {}).get(pid)
            if name:
                variants.setdefault(name, []).append(r["id"])
        sizes = sorted({r["size"] for r in records if r["id"] in users}, key=SIZE_ORDER.index)
        rec = {
            "id": pid,
            "name": pid.replace("-", " "),
            "category": spec["category"],
            "grip": spec["grip"],
            "length_m": spec["length_m"],
            "tri_budget": spec["tris"],
            "grip_socket": spec["grip_socket"],
            "stow_socket": spec["stow_socket"],
            "wielder_sizes": sizes,
            "monster_count": len(users),
            "used_by": dict(sorted(users.items())),
            "reference_prompt": prop_prompt(spec),
        }
        if variants:
            rec["variants"] = variants
        if spec.get("rig"):
            rec["rig"] = spec["rig"]
        props.append(rec)
    props.sort(key=lambda p: (-p["monster_count"], p["id"]))
    return props


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out", type=Path, default=ROOT / "data/registry/srd-5.2.1.json")
    args = ap.parse_args()

    monsters = json.loads(args.src.read_text())
    overrides = json.loads((ROOT / "data/body_plan_overrides.json").read_text())
    visuals = json.loads((ROOT / "data/visual_profiles.json").read_text())
    reviewed = set(json.loads((ROOT / "data/body_plan_reviewed.json").read_text()).get(SOURCE_ID, []))
    equipment = json.loads((ROOT / "data/equipment.json").read_text())
    prop_specs = equipment["props"]
    equip = equipment["monsters"].get(SOURCE_ID, {})
    slugs = {m["slug"] for m in monsters}

    errors = []
    for s in sorted(reviewed & set(overrides)):
        errors.append(f"body_plan_reviewed: {s} is also overridden; drop one")
    for name, table in (("body_plan_overrides", overrides), ("visual_profiles", visuals),
                        ("body_plan_reviewed", dict.fromkeys(reviewed))):
        for s in sorted(set(table) - slugs):
            errors.append(f"{name}: unknown slug {s}")
    for s, ov in overrides.items():
        if ov["body_plan"] not in BODY_PLANS:
            errors.append(f"body_plan_overrides: {s}: bad body_plan {ov['body_plan']}")
    for s in sorted(slugs - set(visuals)):
        errors.append(f"visual_profiles: missing slug {s}")
    for s in sorted(set(equip) - slugs):
        errors.append(f"equipment: unknown slug {s}")
    for s, e in equip.items():
        for w in e.get("wields", []):
            if w not in prop_specs:
                errors.append(f"equipment: {s}: wields {w!r} has no entry in props")
        for w in e.get("aka", {}):
            if w not in e.get("wields", []):
                errors.append(f"equipment: {s}: aka for {w!r}, which it does not wield")
    if errors:
        sys.exit("\n".join(errors))

    records = []
    equip_diffs = []
    for m in monsters:
        slug, d = m["slug"], m["data"]
        speed = {k: v for k, v in d.get("speed", {}).items()}
        is_swarm = "type_line" in d and "Swarm" in d["type_line"]
        if is_swarm:
            size, typ, unit_size, _ = parse_swarm(d["type_line"])
            sizes = [size]
            tags = ["swarm"]
        else:
            sizes = parse_sizes(d.get("size"))
            typ = d.get("type")
            tags = list(d.get("tags") or [])
            unit_size = None
        if not sizes or not typ:
            sys.exit(f"{slug}: could not determine size/type")
        size = sizes[0]

        rule_plan, rule_conf = rule_body_plan(slug, typ, speed, is_swarm)
        ov = overrides.get(slug)
        if ov:
            plan, conf, plan_src = ov["body_plan"], ov["confidence"], "override"
            rig = ov.get("rig_template") or rig_for(plan, slug)
            note = ov.get("note")
        else:
            plan, conf = rule_plan, rule_conf
            plan_src = "rule_reviewed" if slug in reviewed else "rule"
            if slug in reviewed:
                conf = 1.0
            rig = rig_for(plan, slug)
            note = None

        vis = visuals[slug]
        scale = vis.get("scale_m", SIZE_SCALE_M[size])

        derived = derive_wields(d)
        eq = equip.get(slug)
        if eq is None:
            # Unreviewed: plan default for hands, rule-based weapons (new tiers start here).
            eq = {"wields": [w for w in derived if w in prop_specs]}
            if (plan in HAND_PLANS or derived) and SOURCE_ID == "srd-5.2.1":
                errors.append(f"equipment: {slug} ({plan}) is not reviewed in data/equipment.json"
                              + (f" (stat block names {derived})" if derived else ""))
        wields = list(eq.get("wields", []))
        hands = eq.get("hands", plan in HAND_PLANS) or bool(wields)
        arms = eq.get("arms", 2 if hands else 0)
        armed = bool(wields) or bool(eq.get("armed_look"))
        unarmed_ref = hands and armed
        if unarmed_ref and HELD_WORDS.search(vis["desc"]):
            errors.append(f"visual_profiles: {slug} is drawn unarmed but its desc names "
                          f"{HELD_WORDS.search(vis['desc']).group(0)!r}; describe worn gear only")
        if sorted(set(derived)) != sorted(set(wields)):
            equip_diffs.append(f"{slug}: stat block {derived} -> curated {sorted(set(wields))}")

        rec = {
            "id": slug,
            "name": m["name"],
            "source": SOURCE_ID,
            "page": m.get("page"),
            "size": size,
            "size_options": sizes,
            "type": typ,
            "tags": tags,
            "cr": d.get("cr"),
            "speeds": speed,
            "body_plan": plan,
            "body_plan_confidence": conf,
            "body_plan_source": plan_src,
            "rig_template": rig,
            "animation_set": animation_set(plan, speed, d),
            "scale_m": scale,
            "hands": hands,
            "wields": wields,
            "shield": "shield" in wields,
            "sockets": sockets_for(hands, arms, armed),
            "unarmed_reference": unarmed_ref,
            "reference_prompt": reference_prompt(
                vis["desc"], plan, size.capitalize(),
                f"swarm of {unit_size} {typ}s" if is_swarm else typ, is_swarm),
        }
        if unit_size:
            rec["swarm_unit_size"] = unit_size
        if note:
            rec["body_plan_note"] = note
        if plan_src == "override" and plan != rule_plan:
            rec["body_plan_rule_guess"] = rule_plan
        if arms not in (0, 2):
            rec["arms"] = arms
        if eq.get("aka"):
            rec["wields_aka"] = eq["aka"]
        if eq.get("note"):
            rec["equipment_note"] = eq["note"]
        records.append(rec)

    if errors:
        sys.exit("\n".join(errors))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "source": SOURCE_ID,
        "license": "CC-BY-4.0 (System Reference Document 5.2.1, Wizards of the Coast)",
        "count": len(records),
        "body_plans": BODY_PLANS,
        "monsters": records,
    }
    args.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    props = build_props(records, prop_specs)
    props_out = args.out.parent / "props.json"
    props_out.write_text(json.dumps({
        "source": SOURCE_ID,
        "count": len(props),
        "note": "Generated by tools/build_registry.py from data/equipment.json and the wields "
                "lists. Held props are attached at runtime, never baked. See docs/equipment.md.",
        "props": props}, indent=1, ensure_ascii=False) + "\n")

    counts = Counter(r["body_plan"] for r in records)
    print(f"wrote {len(records)} records -> {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")
    for p in BODY_PLANS:
        print(f"  {p:18s} {counts.get(p, 0)}")
    low = [r["id"] for r in records if r["body_plan_confidence"] < 0.8]
    print(f"  low-confidence (<0.8): {len(low)}")
    src = Counter(r["body_plan_source"] for r in records)
    print("  sources: " + ", ".join(f"{k}={v}" for k, v in sorted(src.items())))
    if low:
        print("  " + " ".join(low))
    n = lambda f: sum(1 for r in records if f(r))
    print(f"  hands={n(lambda r: r['hands'])} wields={n(lambda r: r['wields'])} "
          f"shield={n(lambda r: r['shield'])} unarmed_reference={n(lambda r: r['unarmed_reference'])} "
          f"multi-arm={n(lambda r: r.get('arms', 2) > 2)}")
    print(f"wrote {len(props)} props -> {props_out.relative_to(ROOT) if props_out.is_relative_to(ROOT) else props_out}")
    if equip_diffs:
        print(f"  curated wields that differ from the stat-block words ({len(equip_diffs)}):")
        for line in equip_diffs:
            print("    " + line)


if __name__ == "__main__":
    main()
