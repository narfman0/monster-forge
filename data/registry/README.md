# Monster registry

`srd-5.2.1.json` is generated. Do not edit it by hand. Rebuild it with:

```sh
python3 -I tools/build_registry.py
```

## Inputs

| file | role |
|---|---|
| `~/workspace/godot-srd-addon/addons/srd/data/srd-5.2.1/monsters.json` | Source stat blocks (read-only). Override the path with `--src`. |
| `data/body_plan_overrides.json` | Hand-curated `slug -> {body_plan, confidence, rig_template?, note?}` |
| `data/body_plan_reviewed.json` | `{source_id: [slug, ...]}`: slugs whose rule-based plan was checked by hand (confidence becomes 1.0) |
| `data/visual_profiles.json` | `slug -> {desc, scale_m?}`: visual description for the reference prompt, plus an optional scale override |

The build fails if any curated slug is unknown, if a monster has no visual
profile, if a body plan is outside the enum, or if a slug is in both the
override file and the reviewed list.

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

Descriptions are written from the creatures' canonical traits. They do not
describe or imitate any specific piece of published art.
