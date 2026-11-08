# Monster tags

Every monster carries a set of lowercase-kebab, singular tags (`dragon`, `dinosaur`, `flying`,
`cr-low`) that the viewer filters on, the download API selects by, the public index carries and
godot-dnd copies into `monster.json` (spawn "a random dinosaur").

- `data/tags.json`: the taxonomy (groups, tag definitions with description, group and
  `implies`, source -> source tag) and the SRD's curated membership (`members`, `ids`,
  `no_family`). Hand-edited.
- `tools/tags.py`: rules + curation -> tags, validation, report.
- `data/tags.computed.json`: the result for the SRD (`groups`, `tags` with counts,
  `monsters: {id: [tags]}`). Generated, committed, never hand-edited. Consumed by godot-dnd's
  `tools/sync_monsters.py`; the viewer and `tools/publish.py` compute the same thing live
  through `tools/tags.py`.

```sh
python3 -I tools/tags.py compute          # validate + write data/tags.computed.json
python3 -I tools/tags.py check            # validate + the computed file is current (exit 1 if not)
python3 -I tools/tags.py report           # tag -> count per group, monsters without a family, dragon/draconic/dinosaur lists
python3 -I tools/tags.py show wyvern owlbear
python3 -I tools/tags.py list dragon flying   # ids with every tag given
```

## Why a derived file, not the registry's `tags` field

The registry (`data/registry/srd-5.2.1.json`) is built by `tools/build_registry.py` from the SRD
addon; its `tags` are the stat block's own parenthetical tags (chromatic, devil, goblinoid ...).
Rewriting that field would mix generated tags into a generated file that is rebuilt from
elsewhere, and a rebuild would silently drop them. So the registry tags stay as they are and are
one of the inputs; the merged result is a separate file with one owner (`tags.py`). The viewer,
download API and publish.py do not read the computed file at all: they call `tags.compute()`
directly, so they can never show stale tags. Run `compute` after changing `data/tags.json` or the
registry so godot-dnd sees the change; `check` catches a forgotten run.

## Groups

| group | rule | per monster |
|---|---|---|
| `source` | registry `source` via `sources` in data/tags.json (`srd-5.2.1` -> `srd`) | exactly one |
| `type` | registry `type` -> `type-<type>` (`type-beast`, `type-dragon` ...) | exactly one |
| `family` | curated `members` + rules + `implies` | at least one (or reviewed `no_family`) |
| `dragon-kind` | registry `chromatic` / `metallic`; colour (`red-dragon`) and age (`wyrmling`, `young-dragon`, `adult-dragon`, `ancient-dragon`) from the id of a dragon-type `<age>-<colour>-dragon` / `<colour>-dragon-wyrmling` | true dragons only |
| `role` | registry `wizard` / `cleric` / `druid` + curated `warrior`, `knight`, `criminal`, `cultist`, `civilian`, `leader` | NPC stat blocks, optional |
| `trait` | curated, read off the SRD stat blocks: `spellcaster` (a Spellcasting action), `shapechanger` (Shape-Shift), `legendary` (legendary actions) | optional |
| `movement` | registry `speeds`: `flying`, `hovering`, `swimming`, `burrowing`, `climbing`; `aquatic` (swims and walks 10 ft or less, or swims at least 3x its walk) else `amphibious` (any other swimmer) | optional |
| `size` | registry `size`: `tiny` ... `gargantuan` | exactly one |
| `cr` | registry `cr`: `cr-0`, `cr-low` (1/8-4), `cr-mid` (5-10), `cr-high` (11-16), `cr-epic` (17+) | exactly one |
| `body-plan` | registry `body_plan`, kebab-case (`winged-quadruped`); `aquatic` -> `aquatic-body` (the movement tag owns `aquatic`); `swarm` is also the registry's swarm tag | exactly one |
| `pipeline` | `out/<source>/<id>/manifest.json`: `rig-ual` (rig.tool `ual`), `rig-quad-canon` (rig.canon status ok or partial), `paired` (UAL shared clips with a hip_scale, and the source has a paired library) | not content |

Creature types are prefixed (`type-dragon`) because the families reuse the short names with a
narrower meaning: `dragon` is a true dragon, `giant` a true giant, `elemental` an elemental being.

Pipeline tags depend on the local `out/` tree, so they are kept apart: index entries have
`tags` (content) and `pipeline_tags`; `tags.py compute --pipeline` adds a `pipeline` section to
the computed file, plain `compute` leaves it out (the committed file stays deterministic).
godot-dnd copies content tags only.

## Families, and the calls worth knowing

- `dragon`: true dragons, wyrmling to ancient, chromatic and metallic (40 in the SRD).
  `draconic`: dragon-kin, every other dragon-type creature: pseudodragon, wyvern, dragon turtle,
  half-dragon, kobold (5). `type-dragon` = `dragon` + `draconic`. The rule: a dragon-type monster
  that is chromatic or metallic is `dragon`, any other is `draconic`.
- `dinosaur`: the seven the SRD tags itself (allosaurus, ankylosaurus, archelon, plesiosaurus,
  pteranodon, triceratops, tyrannosaurus rex), including the prehistoric reptiles that are not
  dinosaurs to a palaeontologist. Implies `reptile`.
- `giant` (hill, stone, frost, fire, cloud, storm) vs `giant-kin` (ogre, ogre zombie, troll, troll
  limb, ettin, oni).
- `elemental`: the four elementals and elemental beings (mephits, azer, xorn, salamander, magmin,
  gargoyle, invisible stalker); genies are `genie`.
- Undead subfamilies: `skeleton`, `zombie`, `ghost` (incorporeal: ghost, specter, wraith, shadow,
  will-o'-wisp), `ghoul`, `vampire`, `lich`, `mummy`.
- Animal families are specific with umbrellas implied: `big-cat` -> `feline` -> `mammal`,
  `shark` -> `fish`, `spider` -> `arachnid`, `snake` -> `reptile` + `serpent`, `naga` ->
  `serpent`, `sphinx` -> `hybrid`. `equine` covers mounts (warhorse, pony, mule, pegasus,
  nightmare, unicorn, the warhorse skeleton without `mammal`); `bovine` the minotaur and the
  gorgon (without `mammal`: it is an iron bull).
- `hybrid`: chimeric creatures (griffon, hippogriff, owlbear, chimera, manticore, cockatrice,
  harpy, lamia, drider, centaur, minotaur, sphinxes).
- `npc`: every humanoid-type stat block (rule), with `role` tags for what it does.
- Lycanthropes, hags, nagas, goblinoids, devils, demons, angels, genies, titans as in the stat
  blocks.
- `no_family` (reviewed, one of a kind in the SRD): aboleth, bulette, cloaker, darkmantle,
  doppelganger, ettercap, gibbering mouther, grimlock, homunculus, incubus, medusa, mimic, otyugh,
  rakshasa, roper, rust monster, shield guardian, stirge, succubus, swarm of crawling claws,
  wight. They still have every non-family tag.

Not defined because the SRD has none: `yugoloth`, `orc`. Add them when a source needs them.

## How tags are computed

In order, per monster:

1. the registry record's own `tags`;
2. rules on registry fields (source, type, size, CR band, body plan, movement, dragon colour /
   age, `dragon` / `draconic`, humanoid -> `npc`);
3. curated `members` (`{tag: [ids]}`);
4. `implies` on the tag definitions, to a fixed point;
5. curated `ids` (`{id: {add, remove, why}}`), remove wins; finally `aquatic` drops `amphibious`.

Output order follows the groups, then the definition order in `data/tags.json`.

## Validation (every command runs it; non-zero exit on any error)

- tag names are lowercase-kebab, every tag has a known group and a description, `implies`
  names defined tags;
- every curated tag and id exists (typos get a "did you mean"), no duplicate ids in a list,
  rule-only groups (source, type, size, cr, body-plan, pipeline) cannot be curated;
- every registry tag is defined (a new stat-block tag must be added to `data/tags.json`);
- every monster has exactly one source, type, size, cr and body-plan tag, no pipeline tag
  among its content tags, and at least one family tag unless it is in `no_family`; a
  `no_family` monster that has gained a family is an error too (drop it from the list).

## Adding things

- **A tag**: add it under `tags` in `data/tags.json` with `group`, `description` and maybe
  `implies`; list its members under `members` (or give it a rule in `tools/tags.py` when it
  follows from registry fields); `tags.py compute`; check `tags.py report`.
- **A per-monster fix**: `ids: {"<id>": {"add": [...], "remove": [...], "why": "..."}}`.
- **A group**: add it to `groups` (order = facet order); `ONE_OF` in `tools/tags.py` if every
  monster must have exactly one.
- **A new source**: map it in `sources` (`"mm-2024": "mm-2024"`); its curated
  `members` / `ids` / `no_family` go in the non-SRD checkout, `curation/<source>/tags.json`
  (same keys as data/tags.json), and its computed file is `registry/<source>.tags.json` there
  (`tools/sources.py`). Definitions stay shared in `data/tags.json`, which must not name
  non-SRD monsters.

## Where tags show up

- Viewer (`viewer/README.md`, "Tags"): tag facet with counts, AND filter, `?tag=a,b` in the URL,
  clickable chips in the details panel, search matches tag text.
- Download API: `/api/download?tag=...`, `/api/download/all.zip?tag=...`,
  `/api/download/tag/<tag>.zip`.
- `tools/publish.py`: the static `index.json` keeps `tags`, `pipeline_tags` and a `tag_index` of
  the published monsters' tags.
- godot-dnd: `tools/sync_monsters.py` writes `tags` into each `monster.json` from
  `data/tags.computed.json`; `Monster.ids_with_tags(["dinosaur"])` in `scripts/monster.gd`.
