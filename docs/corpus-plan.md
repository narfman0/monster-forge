# Corpus plan

Goal: a low-poly, game-ready asset (mesh, texture, rig, animation clips) for
every official D&D 5e monster. We work in tiers so the openly licensed set
ships first and the pipeline is proven before the IP-encumbered sets.

> All counts outside Tier 1 are **approximate**, drawn from general knowledge
> as of late 2026, and need checking against the books before planning
> capacity. Count stat blocks: one creature can have several (e.g. dragon
> ages, NPC variants).

## Tier 1: SRD 5.2.1 (CC-BY-4.0) — 330 stat blocks, done

- Registry: `data/registry/srd-5.2.1.json` (330 records, all body plans
  hand-verified).
- Includes the animal/beast appendix (~95 beasts), 40 true dragons (10 colours
  x 4 ages), 7 swarms, and the generic NPCs (bandit, mage, knight, ...).
- **Licence:** CC-BY-4.0. Generated assets may be redistributed publicly,
  including commercially. Every distribution needs this attribution:

  > This work includes material from the System Reference Document 5.2.1
  > ("SRD 5.2.1") by Wizards of the Coast LLC, available at
  > https://www.dndbeyond.com/srd. The SRD 5.2.1 is licensed under the
  > Creative Commons Attribution 4.0 International License, available at
  > https://creativecommons.org/licenses/by/4.0/legalcode.

- "Dungeons & Dragons" and the D&D logos are trademarks and are **not**
  covered by CC-BY. A public pack may say it is "compatible with fifth
  edition" or "5E compatible" but must not use D&D branding.

## Tier 2: Monster Manual (2024) — ~500 stat blocks, ~170-200 not in the SRD

- The 2024 MM advertises "more than 500" stat blocks, about 85 of them new
  creatures. It also has a beast/animal appendix.
- Nearly all of the SRD 5.2.1's 330 entries are 2024 MM stat blocks. That
  leaves **roughly 170-200 MM-only stat blocks**.
- Notable MM-only creatures are WotC product identity: beholder (and death
  tyrant, spectator), mind flayer, githyanki, githzerai, yuan-ti, displacer
  beast, umber hulk, carrion crawler, kuo-toa, slaadi, intellect devourer,
  and the new 2024 creatures.
- Many reuse Tier 1 rigs: naga-like creatures are `merfolk_naga`, `biped` or
  `serpentine`; six-legged cats are `quadruped` + 2 tentacle chains; floating
  eye tyrants are `floating` + eye-stalk chains. Their curation lives in the
  separate non-SRD repo (`tools/sources.py`, `MONSTER_FORGE_EXTRA`).
- Before generating this tier, extend the overrides and reviewed lists for a
  `mm-2024` source. The rules alone will misclassify about 40% of the
  non-humanoid entries, as they did for the SRD.

## Tier 3: other 2024-era official books (2024-2026)

| book | year | monster content (rough) |
|---|---|---|
| Player's Handbook (2024) | 2024 | No bestiary. About 15 spell/summon stat blocks (Aberrant/Beast/Celestial/... Spirit, Giant Insect, Otherworldly Steed, Animated Object, ...). Some are in the SRD spell list. Low priority: they are usually rendered as effects. |
| Dungeon Master's Guide (2024) | 2024 | Effectively no new monster stat blocks (Greyhawk gazetteer, lair/rules guidance). Verify. |
| Dragon Delves | 2025 | Dragon-themed adventure anthology. About 20-40 stat blocks (dragon variants, draconic creatures, NPCs). |
| Eberron: Forge of the Artificer | 2025 | Mostly player and setting content. A small bestiary of about 10-30 (warforged, Eberron-specific creatures). |
| Forgotten Realms: Heroes of Faerûn | 2025 | Player book. Few or no monsters. |
| Forgotten Realms: Adventures in Faerûn | 2025 | DM book with a regional bestiary. About 40-80 stat blocks. |
| 2026 releases | 2026 | Check the 2026 WotC catalogue (setting books and adventures). Not catalogued here. |

Tier 3 total: roughly 100-200 new stat blocks, many of them NPC variants on
the `biped` rig. Only official WotC books are in scope. Third-party 5E books
(e.g. *Monsters of Drakkenheim*, Kobold Press *Tome of Beasts*) are excluded.

## Tier 4: legacy 2014-era books

Lower priority. Many of these creatures were revised into the 2024 MM or
*Monsters of the Multiverse*. Deduplicate by creature identity before
generating.

| book | year | stat blocks (rough) |
|---|---|---|
| Monster Manual (2014) | 2014 | ~450 (incl. NPC appendix) |
| Volo's Guide to Monsters | 2016 | ~140 |
| Mordenkainen's Tome of Foes | 2018 | ~140 |
| Mordenkainen Presents: Monsters of the Multiverse | 2022 | ~250 (revised reprints of Volo's + Tome of Foes, which it supersedes) |
| Fizban's Treasury of Dragons | 2021 | ~70 (gem dragons, dragon variants, draconic creatures) |
| Bigby Presents: Glory of the Giants | 2023 | ~40 |
| Boo's Astral Menagerie (Spelljammer) | 2022 | ~65 |
| Morte's Planar Parade (Planescape) | 2023 | ~50 |
| Van Richten's Guide to Ravenloft / The Book of Many Things | 2021 / 2023 | ~40 / ~30 |
| Adventure books (Curse of Strahd, Tomb of Annihilation, Descent into Avernus, ...) | 2014-2024 | 10-60 each |

After deduplication against Tiers 1-3, the legacy tail is probably around
**600-1,000 distinct creatures**. Model reuse is high: palette swaps, NPC
variants, and age/size variants on a shared rig.

## Licensing summary

| set | names/designs | what we can do with assets |
|---|---|---|
| SRD 5.2.1 | CC-BY-4.0 | Public release, including commercial, with attribution. |
| Everything else (Tiers 2-4) | WotC IP (product identity, copyrighted descriptions, trade dress) | Personal/private use (home games, the user's own projects). Do not publish a pack using WotC names or designs. |

A **public** asset pack should contain the SRD set only, plus renamed and
visibly redesigned analogues of popular non-SRD creatures (for example a
generic "floating eye tyrant" instead of the beholder). Reference prompts are
written from creature traits, never from official art, and that should stay
true for every tier. (This is a working policy, not legal advice.)

## Where to get machine-readable monster lists for later tiers

We only need **names, sources, sizes, types and speeds** to classify body
plans. We do not need rules text. Nothing has been downloaded yet.

- **open5e API** (https://open5e.com): SRD content (5.1, and SRD 5.2 in its
  v2 API) plus OGL/CC third-party books. Has **no** non-SRD WotC monsters.
  Useful for cross-checking Tier 1.
- **dnd5eapi.co**: SRD 5.1 only.
- **Foundry VTT `dnd5e` system** compendia: SRD only (5.1/5.2). A clean,
  well-structured cross-check.
- **5e.tools** (community-maintained JSON on GitHub mirrors): covers every
  official book, with name, source, page, size, type and speed per entry.
  This is the practical source for Tier 2-4 **name and source lists**. Its
  data reproduces copyrighted text without a licence, so it is IP-encumbered.
  Use it locally only to derive name, source, size, type and speed for
  planning, and never vendor its data into this repo.
- **D&D Beyond**: authoritative for books the user owns, but there is no
  public API. Manual reference only.

Suggested next step for Tier 2: write a `tools/import_names.py` that turns a
locally supplied JSON (e.g. a 5e.tools bestiary file the user provides) into
`data/registry/mm-2024.json` stubs. It would carry only the fields above,
reuse the same rule engine, emit `body_plan_source: "rule"` for review, and
diff against the SRD registry to list the MM-only creatures.

## Production order

1. Rigs first, by usage: `rigify.basic_human` (97), `custom.dragon` (41),
   `rigify.basic_quadruped` / horse / wolf / cat (59), `custom.human_winged`
   (21), `rigify.bird` + `custom.bat_wing` (16), `custom.spine_chain` (11).
2. Within Tier 1, do pilot creatures for each body plan before bulk
   generation. For example: goblin-warrior, wolf, young-red-dragon, imp,
   giant-eagle, giant-spider, constrictor-snake, gelatinous-cube, ghost,
   centaur-trooper, merfolk-skirmisher, roper, xorn.
3. Dragons: build one mesh topology per colour and scale it across the four
   ages, rather than making 40 separate meshes.
