# Monster registry

`srd-5.2.1.json` and `props.json` are generated. Do not edit them by hand. Rebuild them with:

```sh
python3 -I tools/build_registry.py
```

## Inputs

| file | role |
|---|---|
| `~/workspace/godot-srd-addon/addons/srd/data/srd-5.2.1/monsters.json` | Source stat blocks (read-only). Override the path with `--src`. |
| `data/body_plan_overrides.json` | Hand-curated `slug -> {body_plan, confidence, rig_template?, note?}`. Flat keys are srd-5.2.1; other sources under `sources: {source_id: {slug: ...}}` (`docs/body-plans.md`) |
| `data/body_plan_reviewed.json` | `{source_id: [slug, ...]}`: slugs whose rule-based plan was checked by hand (confidence becomes 1.0) |
| `data/visual_profiles.json` | `slug -> {desc, scale_m?}`: visual description for the reference prompt, plus an optional scale override. Same per-source layout as the overrides; an mm-2024 entry may hold only `scale_m` |
| `data/equipment.json` | `props`: one spec per held prop. `monsters`: `source -> slug -> {wields, hands?, arms?, armed_look?, aka?, note?}`, hand-reviewed against each stat block (`docs/equipment.md`) |

The build fails if any curated slug is unknown, if a monster has no visual
profile, if a body plan is outside the enum, or if a slug is in both the
override file and the reviewed list. It also fails if a monster with hands, or
with a weapon in its stat block, is missing from `data/equipment.json`, if a
wielded prop has no spec, or if an `unarmed_reference` monster's description
names a held weapon.

## File layout

```json
{
  "source": "srd-5.2.1",
  "license": "CC-BY-4.0 ...",
  "count": 330,
  "body_plans": ["biped", "..."],
  "monsters": [ { record }, ... ]
}
```

## Record schema

| field | type | notes |
|---|---|---|
| `id` | string | Source slug. Unique within a source. Later tiers may need `source:id` to stay unique. |
| `name` | string | Display name, as in the source (the source spells one name `GrayOoze`) |
| `source` | string | `srd-5.2.1` (later: `mm-2024`, etc.) |
| `page` | int | Page in the source PDF |
| `size` | string | Primary size category: tiny / small / medium / large / huge / gargantuan. For swarms, the size of the swarm as a whole. |
| `size_options` | string[] | Every allowed size (e.g. `["medium","small"]` for NPC humanoids) |
| `type` | string | Creature type (lowercase). Swarms use the unit's type. |
| `tags` | string[] | Type tags (`devil`, `chromatic`, `goblinoid`, ...). Swarms get `swarm`. |
| `swarm_unit_size` | string | Swarms only: size of one unit (e.g. `tiny`) |
| `cr` | string | Challenge rating as written (`"1/4"`, `"24"`) |
| `speeds` | object | `walk`, `fly`, `swim`, `climb`, `burrow` in feet, plus `hover: true` |
| `body_plan` | enum | See `docs/body-plans.md` |
| `body_plan_confidence` | float 0-1 | 1.0 = verified and unambiguous. <0.8 = depictions disagree or the anatomy fits no plan well. |
| `body_plan_source` | enum | `override` (curated), `rule_reviewed` (rule result confirmed by hand), `rule` (unreviewed heuristic: expect this for new tiers) |
| `body_plan_rule_guess` | enum | Present when an override changed the rule's answer. Useful for tuning the rules. |
| `body_plan_note` | string | Why an edge case was classified the way it was |
| `rig_template` | string | `rigify.<metarig>` or `custom.<template>`. See `docs/body-plans.md`. |
| `animation_set` | string[] | Clip names to author or generate. See below. |
| `scale_m` | float | Approximate largest body dimension in metres: standing height for upright bodies, nose-to-tail length for horizontal ones. Wingspan is not included. Size-category default (tiny 0.5, small 1, medium 1.8, large 3, huge 5, gargantuan 10) unless `visual_profiles.json` overrides it. All 330 SRD entries are hand-set. |
| `hands` | bool | Has graspable hands that the rig exposes as hand bones. Defaults to true for the upright plans. See `docs/equipment.md`. |
| `wields` | string[] | Held prop ids (keys of `props.json`). Duplicates mean several copies: the marilith has six `longsword`. |
| `shield` | bool | `"shield"` is in `wields` |
| `sockets` | string[] | Attachment bones the rig must provide: `hand_r`, `hand_l` (or `hand_r1..n`, `hand_l1..n`), plus `back`, `hip_l`, `hip_r`, `head` for armed creatures |
| `unarmed_reference` | bool | Draw the reference image with empty, open hands: the creature has hands and either wields something or is usually drawn armed |
| `arms` | int | Only present when it is not 2 (glabrezu 4, marilith 6) |
| `wields_aka` | object | Stat-block names for props, e.g. `{"greatsword": "Flame Sword"}` |
| `equipment_note` | string | Why an equipment edge case was decided the way it was |
| `reference_prompt` | string | Text-to-image prompt for a reference sheet. See below. |

## Animation clips

Every set starts with `idle` and ends with `hit`, `death`. Between them come:

- **Locomotion** (one primary clip):
  - `fly` replaces `walk` when the creature flies and its walk speed is 10 ft
    or less, or when its plan is `floating`.
  - `swim` replaces `walk` for `aquatic` creatures, and when walk is 10 ft or
    less, or swim is at least 3x walk.
  - Otherwise `walk`, plus `fly` if it can fly, plus `swim` if swim is at
    least walk.
- `burrow` when the burrow speed is at least half the walk speed.
- `attack_melee` / `attack_ranged`: from attack rolls in actions, bonus
  actions and legendary actions. "Melee or Ranged" gives both.
- `breath`: any action named "... Breath ..." (dragons, hell hound, gorgon,
  mephits, ...).
- `cast`: has a Spellcasting action.
- `attack_special`: the creature has no attack, breath or spell but does have
  some action or reaction (shrieker's shriek, seahorse's dash).

## Reference prompts

Each prompt is built as `<desc>. <Size> <type>. Low-poly game asset, full body, <pose>, plain gray background, clean silhouette, flat even lighting.`

The pose depends on the plan:

| plans | pose |
|---|---|
| upright (`biped`, `winged_biped`, `centauroid`, `merfolk_naga`) | "A-pose, front view". A-pose reads correctly only from the front, and image-to-3D works best from front views of upright bodies. |
| `floating` | "neutral hovering pose, front view" |
| `avian`, `winged_quadruped` | "neutral standing stance with wings partly spread, side view" |
| `aquatic` | "neutral swimming pose, side view" |
| `serpentine` | "body in a loose S-curve, side view" |
| everything else | "neutral stance, side view" |

Swarm prompts ask for one representative unit plus a small cluster.

For `unarmed_reference` records, the description mentions only worn gear
("scabbard at the hip", "arrow quiver on the back"), never a held weapon.
`forge.py ref` adds the house style's `unarmed_suffix` and `unarmed_negative`
for these records.

Descriptions are written from the creatures' canonical traits. They do not
describe or imitate any specific piece of published art.

## mm-2024.json

Stubs for the 173 Monster Manual (2024) stat blocks that are not in the SRD
(no `srd52` flag), same record schema as above, `source: "mm-2024"`. Built by

```sh
python3 -I tools/import_names.py [--src PATH] [--check-srd PATH]
```

from a local 5etools checkout (`~/workspace/5etools-src/data/bestiary/bestiary-xmm.json`
by default; read as data, never vendored). Only name, page, size, type, tags, CR,
speeds and the names of attacks and gear are used; body plan, rig, animation set,
wields and sockets come from the `build_registry.py` rules. `--check-srd` also
writes a table of size/type/speed/CR differences between the `srd52` blocks and
`srd-5.2.1.json` (report only).

**Curation.** The `mm-2024` entries in `body_plan_overrides.json` and
`visual_profiles.json` (under `sources`), `body_plan_reviewed.json` and
`equipment.json["monsters"]` cover the records: 92 overrides, 77 reviewed rule
results and 4 rule guesses left at 0.5, every hand or weapon record reviewed for
equipment, 146 hand-written descriptions and 162 hand scales. Records without a
description use a template (lowercased name, the plan's body phrase when
confidence >= 0.8, natural weapons from attack names, worn gear). The importer
prints what is still on rule guesses, templates or size-default scales.

**Licence.** The names and stats are derived from WotC IP (Monster Manual 2024),
not the CC-BY SRD: personal use only. Do not publish this file or assets
generated from it.

## props.json

One entry per distinct held prop (32 for SRD 5.2.1). Each entry has its
category, grip, length, triangle budget, default sockets, the monsters that use
it, the stat-block names it goes by, and a reference prompt. The schema is in
`docs/equipment.md`.
