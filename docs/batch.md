# SRD batch (2026-11-04)

The 330-monster SRD 5.2.1 batch: what the pipeline covers (the audit), how to run it with
`tools/batch.py`, what to watch, and the order. The 15 body-plan pilots (goblin-warrior,
wolf, owlbear, giant-spider, harpy, young-red-dragon, constrictor-snake, ghost, giant-eagle,
black-pudding, violet-fungus, giant-octopus, hunter-shark, merfolk-skirmisher,
swarm-of-rats) are done through anim and are never re-run by the batch.

## Summary

- 315 monsters to make. 155 are **tier 1** (a pilot's body plan and rig template at a
  pilot size), 113 **tier 2** (same code path as a pilot, a size no pilot had, a swarm
  made as its unit, or the harpy's hand-fixed winged_biped), 33 **tier 3** (a plan variant
  nobody has run), 14 **tier 4 / hold** (the reference stage needs a fix first; running
  them now means re-running everything later). (2026-11-05, after cade6ef: was 146 / 101 /
  31 / 37; the avian prompt, the swarm units and the specter / wraith prompts left the hold
  tier.)
- Estimated time (pilot stage timings, checked by the smoke run below): **~35.1 h** for
  tiers 1-3 (301 monsters: tier 1 18.2 h, tier 2 13.4 h, tier 3 3.6 h), ~36.3 h with the
  hold tier (1.2 h). Paint dominates (160 s, untimed in the smoke run), then the 4 SkinTokens rolls
  (~140 s) and mesh (~70-80 s), each after a 6-10 s ComfyUI restart. A monster is ~7 min
  with SkinTokens, ~5 min with a generated skeleton; `ref,mesh,qa` alone ~2 min (tier 1:
  4.4 h before the 2026-11-05 changes, 4.6 h with the 9 records they added).
- Blocking gaps (fix before those records run; all are reference-stage fixes): the
  serpentine template pilot (10, plus the venomous-snake swarm, whose unit is serpentine)
  and the centauroid pilot (3): the 14 hold records. Fixed in cade6ef and no longer held:
  the avian per-plan prompt (`_plans.avian`, 16), swarm units (`forge.SWARM_UNITS`, 5 of
  6 leave the hold) and the specter / wraith prompts (2) (docs/TODO.md I). The ghoul
  (tier 1) has a working prompt now. Everything else can be fixed after the batch by
  re-running only `rig` and/or `anim` (or only the props), see "Gaps".
- Missing clips the batch will leave (no builder makes them; the anim stage records them
  in `manifest.anim.missing` and the viewer shows `anim_complete` false): since clips2
  (cast, swim, burrow, breath and levitation fly for the legged framework, floating and
  serpentine) only `attack_ranged` without two arms 3 (djinni, manticore, spirit-naga),
  the flying sword's `fly`, the gibbering mouther's `swim` (the amorphous builders have
  none) and all six on the xorn (radial has no clip builder). 2 `attack_special` records
  (shrieker, seahorse) are covered by their plan builders.

## Audit

Regenerate the tables with `python3 -I tools/batch.py audit` (the coverage model is
`classify()`, `buildable_clips()` and `GAPS` in `tools/batch.py`; `buildable_clips()` mirrors
`blender_clips.py` `plan_clips()` / `legless_clips()`, keep them in sync). How it decides:

- **proven**: the record's `(body_plan, rig_template)` is a pilot's. The code keys on the
  plan (`rig.RIG_STRATEGY`, `anim.LEGLESS` / `ARMED`, `forge.MAX_SCALE_PLANS`); the rig
  template is only a label, except `custom.theropod` (the rig score skips wings).
- **same path**: another template of a pilot's plan whose body the same code handles
  (horses and cats like the wolf, crabs and beetles like the spider, feathered winged
  quadrupeds like the dragon). The harpy's winged_biped counts here too: the pilot needed
  hand-built wing chains, so its 20 siblings are not "proven".
- **untested**: a plan variant flagged in docs/TODO.md H or docs/body-plans.md that no
  pilot ran (trees, the rug, the flying sword, the troll limb, elementals, cloaker, wisp,
  otyugh / roper / aboleth, flippered aquatics and seahorses, the lemure, theropods,
  wyvern, myriapods, the winged wasp, hydra, dragon turtle, tarrasque, glabrezu and
  marilith arms, the drider, the xorn).
- **hold**: the reference is known to come out wrong for the plan today (below).
- **Swarms** are judged as their unit (`forge.resolve_swarm_unit`, the record the stages
  see): the unit's plan, rig template and size key the tier, the hold, the gaps, the clips
  and the estimate (a unit costs what its plan costs: SkinTokens rolls or a generated
  skeleton). A swarm is tier 2 at best ("same path as" the unit plan's pilot): bats and
  ravens as giant-eagle (avian), insects as giant-spider (insectoid), piranhas as
  hunter-shark (aquatic). Crawling claws (a hand on `custom.limb_chain`) is untested like
  the troll limb, the venomous snakes are held with the serpentine plan, and the
  swarm-of-rats pilot has no unit.
- Sizes: the pilots are small to large. Tiny, huge and gargantuan records of a proven
  template are bumped to tier 2 (the "proven" column still counts them).

### Coverage per body plan

| plan | records | pilot | proven | same path | untested | hold | rig | expected missing clips |
|---|---:|---|---:|---:|---:|---:|---|---|
| biped | 99 | goblin-warrior | 95 | 1 | 2 | 0 | SkinTokens | - |
| quadruped | 61 | owlbear, wolf | 37 | 20 | 2 | 0 | SkinTokens | - |
| winged_quadruped | 49 | young-red-dragon | 40 | 8 | 0 | 0 | SkinTokens | attack_ranged 1 |
| winged_biped | 21 | harpy | 0 | 20 | 0 | 0 | SkinTokens | - |
| avian | 19 | giant-eagle | 11 | 4 | 3 | 0 | SkinTokens | - |
| insectoid | 15 | giant-spider | 5 | 6 | 3 | 0 | SkinTokens | - |
| serpentine | 11 | constrictor-snake | 0 | 0 | 0 | 10 | fallback:spine_chain | attack_ranged 1 |
| floating | 10 | ghost | 2 | 0 | 7 | 0 | SkinTokens | attack_ranged 1 |
| aquatic | 9 | hunter-shark | 4 | 0 | 4 | 0 | SkinTokens | - |
| tentacled | 7 | giant-octopus | 3 | 0 | 3 | 0 | fallback:tentacle_hub | - |
| plant_object | 7 | violet-fungus | 1 | 0 | 5 | 0 | fallback:vertical | fly 1 |
| amorphous | 7 | black-pudding | 5 | 0 | 1 | 0 | fallback:vertical | swim 1 |
| swarm | 7 | swarm-of-rats | 0 | 4 | 1 | 1 | SkinTokens 4, fallback:vertical 1, fallback:root 1, fallback:spine_chain 1 | - |
| merfolk_naga | 4 | merfolk-skirmisher | 2 | 0 | 1 | 0 | SkinTokens | - |
| centauroid | 3 | - | 0 | 0 | 0 | 3 | SkinTokens | - |
| radial | 1 | - | 0 | 0 | 1 | 0 | SkinTokens | idle 1, walk 1, burrow 1, attack_melee 1, hit 1, death 1 |

Pilots are not counted in proven / same path / untested / hold.

### Per rig template

| plan | rig_template | records | status | tier | note |
|---|---|---:|---|---:|---|
| biped | rigify.basic_human | 1 | pilot | 0 | goblin-warrior |
| biped | rigify.basic_human | 86 | proven | 1 | 86 records - as goblin-warrior |
| biped | rigify.basic_human | 9 | proven | 2 | 9 records - sizes huge, tiny (pilots are small-large) |
| biped | rigify.basic_human | 1 | untested | 3 | glabrezu - 4 arms (two pincers); find_arms takes one pair |
| biped | custom.ghost | 1 | same path | 2 | shadow - shadow: SkinTokens biped like the goblin, robed like the ghost |
| biped | custom.kaiju_tail | 1 | untested | 3 | tarrasque - tarrasque: upright with a heavy ground tail (upright plans have no tail test), gargantuan |
| quadruped | rigify.basic_quadruped | 1 | pilot | 0 | owlbear |
| quadruped | rigify.basic_quadruped | 17 | proven | 1 | 17 records - as owlbear |
| quadruped | rigify.basic_quadruped | 10 | proven | 2 | 10 records - sizes huge, tiny (pilots are small-large) |
| quadruped | rigify.horse | 15 | same path | 2 | 15 records - SkinTokens + the legged framework, as wolf and owlbear |
| quadruped | rigify.wolf | 1 | pilot | 0 | wolf |
| quadruped | rigify.wolf | 10 | proven | 1 | 10 records - as wolf |
| quadruped | rigify.cat | 5 | same path | 2 | cat, lion, panther, saber-toothed-tiger, tiger - SkinTokens + the legged framework, as wolf and owlbear |
| quadruped | custom.turtle | 1 | untested | 3 | dragon-turtle - dragon turtle: shell, gargantuan, swims |
| quadruped | custom.hydra | 1 | untested | 3 | hydra - 5 neck chains; head and neck finders expect one |
| winged_quadruped | custom.dragon | 1 | pilot | 0 | young-red-dragon |
| winged_quadruped | custom.dragon | 19 | proven | 1 | 19 records - as young-red-dragon |
| winged_quadruped | custom.dragon | 21 | proven | 2 | 21 records - sizes gargantuan, huge, tiny (pilots are small-large) |
| winged_quadruped | custom.quadruped_winged | 7 | same path | 2 | 7 records - as the dragon; feathered wings must come out of SkinTokens (the dragon's did, the harpy's did not) |
| winged_quadruped | custom.horse_winged | 1 | same path | 2 | pegasus - pegasus: as the dragon, feathered wings |
| winged_biped | custom.human_winged | 1 | pilot | 0 | harpy |
| winged_biped | custom.human_winged | 20 | proven (hand-fixed) | 2 | 20 records - harpy pilot: SkinTokens gave no wing bones in 4 of 4 rolls; hand-added wing chains + blender_wing_weights.py + rig_reweight |
| avian | rigify.bird | 1 | pilot | 0 | giant-eagle |
| avian | rigify.bird | 7 | proven | 1 | 7 records - as giant-eagle |
| avian | rigify.bird | 4 | proven | 2 | roc, hawk, owl, raven - sizes gargantuan, tiny (pilots are small-large) |
| avian | custom.bat_wing | 4 | same path | 2 | stirge, bat, giant-bat, pteranodon - legged avian framework; membrane wings not yet seen by find_wings |
| avian | custom.theropod | 2 | untested | 3 | allosaurus, tyrannosaurus-rex - allosaurus, T. rex: wingless avian (rig score skips wings) |
| avian | custom.wyvern | 1 | untested | 3 | wyvern - bat wings as forelimbs plus a stinger tail |
| insectoid | custom.arachnid | 1 | pilot | 0 | giant-spider |
| insectoid | custom.arachnid | 3 | proven | 1 | phase-spider, giant-scorpion, giant-wolf-spider - as giant-spider |
| insectoid | custom.arachnid | 2 | proven | 2 | scorpion, spider - sizes tiny (pilots are small-large) |
| insectoid | custom.decapod | 3 | same path | 2 | chuul, crab, giant-crab - crabs, chuul: 8 legs + claws (claws may count as legs, LEG_RANGE 6-10) |
| insectoid | custom.hexapod | 2 | same path | 2 | ankheg, giant-fire-beetle - sprawled legged framework; 6 legs is inside LEG_RANGE |
| insectoid | custom.myriapod | 2 | untested | 3 | remorhaz, giant-centipede - centipede, remorhaz: more legs than LEG_RANGE (6-10) |
| insectoid | custom.multileg_reptile | 1 | same path | 2 | basilisk - basilisk: 8 legs on the sprawled framework |
| insectoid | custom.hexapod_winged | 1 | untested | 3 | giant-wasp - giant wasp: fly-only, insect wings vs find_wings |
| serpentine | custom.spine_chain | 1 | pilot | 0 | constrictor-snake |
| serpentine | custom.spine_chain | 8 | hold | 4 | 8 records - serpentine: the serpentine_side depth template is new; the constrictor-snake pilot is being re-made with it (paint, rig, anim pending) |
| serpentine | custom.serpent_multileg | 1 | hold | 4 | behir - serpentine: the serpentine_side depth template is new; the constrictor-snake pilot is being re-made with it (paint, rig, anim pending) |
| serpentine | custom.spine_chain_winged | 1 | hold | 4 | couatl - serpentine: the serpentine_side depth template is new; the constrictor-snake pilot is being re-made with it (paint, rig, anim pending) |
| floating | custom.elemental_column | 5 | untested | 3 | air-elemental, djinni, fire-elemental, invisible-stalker, water-elemental - air/fire/water elementals, djinni, invisible stalker: a column with arms (TODO H: untested) |
| floating | custom.ghost | 1 | pilot | 0 | ghost |
| floating | custom.ghost | 2 | proven | 1 | specter, wraith - as ghost |
| floating | custom.membrane | 1 | untested | 3 | cloaker - cloaker: a cloak-shaped membrane |
| floating | custom.wisp | 1 | untested | 3 | will-o-wisp - will-o'-wisp: a ball of light |
| aquatic | rigify.shark | 1 | pilot | 0 | hunter-shark |
| aquatic | rigify.shark | 1 | proven | 1 | reef-shark - as hunter-shark |
| aquatic | rigify.shark | 3 | proven | 2 | giant-shark, killer-whale, piranha - sizes huge, tiny (pilots are small-large) |
| aquatic | custom.flipper | 2 | untested | 3 | archelon, plesiosaurus - plesiosaurus, archelon: 4 flipper chains (TODO H: untested) |
| aquatic | custom.spine_chain | 2 | untested | 3 | giant-seahorse, seahorse - seahorses: an upright curled body |
| amorphous | custom.blob | 1 | pilot | 0 | black-pudding |
| amorphous | custom.blob | 5 | proven | 1 | gelatinous-cube, gibbering-mouther, grayooze, mimic, ochre-jelly - as black-pudding |
| amorphous | custom.blob_torso | 1 | untested | 3 | lemure - lemure: blob with a head and arms; the vertical chain ignores the arms |
| plant_object | custom.tree_roots | 2 | untested | 3 | awakened-shrub, awakened-tree - awakened tree and shrub: branches and root legs on a vertical chain |
| plant_object | custom.stalk | 1 | pilot | 0 | violet-fungus |
| plant_object | custom.stalk | 1 | proven | 1 | shrieker-fungus - as violet-fungus |
| plant_object | custom.rigid_prop | 1 | untested | 3 | animated-flying-sword - flying sword: a rigid prop that flies; no plant_object fly clip |
| plant_object | custom.cloth_grid | 1 | untested | 3 | animated-rug-of-smothering - rug of smothering: a flat cloth, the vertical chain is wrong |
| plant_object | custom.limb_chain | 1 | untested | 3 | troll-limb - troll limb: a crawling arm |
| swarm | custom.swarm | 1 | pilot | 0 | swarm-of-rats |
| swarm | custom.swarm | 4 | same path | 2 | swarm-of-bats, swarm-of-insects, swarm-of-piranhas, swarm-of-ravens - made as one bat (avian, custom.bat_wing, tiny): same path as giant-eagle; made as one piranha (aquatic, rigify.shark, tiny): same path as hunter-shark; made as one raven (avian, rigify.bird, tiny): same path as giant-eagle; made as one unit (insectoid, custom.hexapod, tiny): same path as giant-spider |
| swarm | custom.swarm | 1 | untested | 3 | swarm-of-crawling-claws - unit is a crawling hand on custom.limb_chain, untested like the troll limb |
| swarm | custom.swarm | 1 | hold | 4 | swarm-of-venomous-snakes - swarm unit venomous-snake: serpentine: the serpentine_side depth template is new; the constrictor-snake pilot is being re-made with it (paint, rig, anim pending) |
| tentacled | custom.tentacle_hub | 1 | pilot | 0 | giant-octopus |
| tentacled | custom.tentacle_hub | 2 | proven | 1 | darkmantle, octopus - as giant-octopus |
| tentacled | custom.tentacle_hub | 1 | proven | 2 | kraken - sizes gargantuan (pilots are small-large) |
| tentacled | custom.tentacle_hub | 3 | untested | 3 | aboleth, otyugh, roper - fish body with 4 tentacles: tentacle_hub vs a fish spine; tentacled column (TODO H: untested); tentacled on three legs (TODO H: untested) |
| merfolk_naga | custom.naga | 1 | pilot | 0 | merfolk-skirmisher |
| merfolk_naga | custom.naga | 2 | proven | 1 | merrow, salamander - as merfolk-skirmisher |
| merfolk_naga | custom.naga | 1 | untested | 3 | marilith - 6 arms: sockets hand_r1..3 / hand_l1..3, which the rig stage does not place |
| centauroid | custom.centaur | 2 | hold | 4 | centaur-trooper, lamia - centauroid: the centaur-trooper pilot is not through anim (new centaur_34 reference, mesh re-made) |
| centauroid | custom.drider | 1 | hold | 4 | drider - centauroid: the centaur-trooper pilot is not through anim (new centaur_34 reference, mesh re-made) |
| radial | custom.radial_tripod | 1 | untested | 3 | xorn - xorn: no clip builder for the plan (anim stage skips it) |

### Gaps, ranked by records

| # | gap | records | blocks the batch? | re-run after the fix | examples |
|---:|---|---:|---|---|---|
| 1 | winged bodies: QA's auto-yaw takes the body axis from the mirror plane or the thick, low surface, 90% within 15 deg on disk; the rest are refused past 30 deg or held unverified | 88 | no (after) | pin `yaw`, then qa, paint, lod, rig, anim | axe-beak, balor, black-dragon-wyrmling, young-black-dragon, adult-black-dragon, ancient-black-dragon, ...; the batch's facing gate stops these after qa, before paint |
| 2 | unarmed_reference: --n 4 + the unarmed check (thin margin, 2.5 threshold) | 77 | no (after) | ref (re-pick) when a weapon slips through | animated-armor, assassin, azer-sentinel, balor, bandit, bandit-captain, ...; handled by the batch (--n 4); review the candidate strip |
| 3 | huge / gargantuan: 20-25k tri LOD0 never through SkinTokens; paint views are 1024 px whatever the size (anim thresholds are size-relative since clips2) | 49 | no (after) | rig (if SkinTokens fails: tri_budget override, then mesh..), anim --force | awakened-tree, balor, behir, adult-black-dragon, ancient-black-dragon, adult-blue-dragon, ... |
| 4 | untested plan variant (see the plan table) | 36 | no (after) | rig --rig-reroll, anim --force (mesh only if the shape is wrong) | aboleth, air-elemental, animated-flying-sword, animated-rug-of-smothering, awakened-shrub, awakened-tree, ... |
| 5 | tiny: no pilot below 0.95 m (anim thresholds are size-relative since clips2, untested there) | 31 | no (after) | anim --force | swarm-of-crawling-claws, homunculus, imp, pseudodragon, quasit, sphinx-of-wonder, ... |
| 6 | winged_biped wing chains: SkinTokens found none on the harpy (hand-built) | 20 | no (after) | rig --rig-reroll (+ hand chains / wing weights), anim --force | balor, bone-devil, deva, erinyes, gargoyle, homunculus, ...; expect fly missing or torn wings; TODO C has the stage work |
| 7 | avian reference prompt: front view, wings spread to the sides (fixed) | 18 | no (fixed) | - | axe-beak, cockatrice, roc, stirge, wyvern, bat, ...; fixed in cade6ef: prompt_tweaks.json `_plans.avian` (front view, both wings spread; theropods excepted) |
| 8 | serpentine: pilot re-made with the serpentine_side depth template | 11 | **yes** (fix first) | everything (ref) | behir, couatl, grick, guardian-naga, purple-worm, spirit-naga, ...; owned by the pilot stream; release once constrictor-snake is through anim |
| 9 | wields an off-model prop (fork, heavy crossbow, mace) | 10 | no (after) | props.py only (no monster re-run) | cloud-giant, deva, horned-devil, knight, pit-fiend, priest-acolyte, ... |
| 10 | swarms: one unit per swarm (prompt, unit scale_m) (fixed) | 6 | no (fixed) | - | swarm-of-crawling-claws, swarm-of-bats, swarm-of-insects, swarm-of-piranhas, swarm-of-ravens, swarm-of-venomous-snakes; fixed in cade6ef: forge.SWARM_UNITS makes each swarm as one unit (plan, template, size, scale_m); swarm-of-rats keeps its column-of-rats pilot |
| 11 | `attack_ranged` without two arms (spit, tail spikes, rays) | 3 | no (after) | anim --force | djinni, manticore, spirit-naga; floating and serpentine casters, manticore |
| 12 | centauroid: no centaur pilot through anim yet | 3 | **yes** (fix first) | everything (ref) | centaur-trooper, drider, lamia; centaur-trooper in progress |
| 13 | specter, wraith: the ghost's legless-robe prompt tweak (fixed) | 2 | no (fixed) | - | specter, wraith; fixed in cade6ef: per-id specter / wraith tweaks in data/prompt_tweaks.json |
| 14 | multi-armed wielders: hand_r1..n sockets / more than one arm pair | 2 | no (after) | rig --force, anim --force | glabrezu, marilith; glabrezu, marilith |
| 15 | `fly` on a plan with no fly builder (amorphous, plant_object, aquatic, merfolk_naga) | 1 | no (after) | anim --force | animated-flying-sword; the flying sword |
| 16 | `swim` on a plan with no swim builder (amorphous, plant_object) | 1 | no (after) | anim --force | gibbering-mouther; amorphous builders: idle, walk, attack_melee, hit, death |
| 17 | radial: no clip builder for the plan (anim stage skips it) | 1 | no (after) | anim --force | xorn |
| 18 | a floating wielder: the floating attack is a two-arm reach, not a weapon cut | 1 | no (after) | anim --force | djinni; djinni |
| 19 | wields something but the plan or record has no hands | 0 | no (after) | registry |  |

Expected missing clips over all records (animation_set clips no builder makes): `attack_ranged` 3, `fly` 1, `swim` 1, `idle` 1, `walk` 1, `burrow` 1, `attack_melee` 1, `hit` 1, `death` 1.

Tiers: 0 = 15, 1 = 155, 2 = 113, 3 = 33, 4 = 14.

### Notes on the gaps

- **Blocking** means the fix is in `ref` (prompt, pose template, registry scale), so a
  record made before it would have to go through every stage again. These records are
  tier 4 and `batch.py run` leaves them out unless asked (`--tier 4` or `--ids`).
  - Serpentine (10, and swarm-of-venomous-snakes, a serpentine unit): the
    `serpentine_side` depth template (`forge.REF_TEMPLATE_BY_PLAN`) applies to every
    serpentine record, but the constrictor-snake pilot is still being re-made with it.
    Release once it is through anim.
  - Centauroid (3): the centaur-trooper pilot has a real centaur reference now
    (`centaur_34` template) but is not through anim.
  - Fixed (cade6ef), kept in the gap table as "no (fixed)": the avian pose
    (`prompt_tweaks.json` `_plans.avian`: front view, both wings spread; theropods keep
    the registry pose), swarm units (`forge.SWARM_UNITS`: one unit per swarm, the game
    multiplies it) and the specter / wraith copies of the ghost's legless-robe tweak.
- **After** means `forge.py run <id> --stages rig,anim --force` (or `anim --force`, or
  `props.py`) once the code is fixed; the batch's output up to `lod` stays. Biggest ones:
  - Clips: `cast` (48), `swim` (39), `burrow` (18), `breath` (11) and `fly` without wings
    (8 of 9) have builders since clips2 (docs/rig-anim.md); left: `attack_ranged` without
    two arms (3), the flying sword's `fly`, the gibbering mouther's `swim` (amorphous) and
    the xorn (radial, no builder at all).
  - Winged bipeds (20): SkinTokens gave the harpy no wing bones in 4 of 4 rolls; expect the
    same, so `fly` missing or wings tearing (TODO C: wing chains in the stage).
  - Tiny (31, the 6 swarm units included) and huge / gargantuan (49): the anim thresholds
    (`DIP_MAX`, `HOVER`, ...) are
    size-relative outside the pilots' 0.95-6.5 m band since clips2, but no pilot ran there.
    20000-25000 tri LOD0s have never gone through SkinTokens (VRAM, time), and the paint
    stage renders every view at 1024 px whatever the 2048 atlas.
  - Winged bodies (88, with the bat and raven units): QA's PCA auto-facing followed the
    dragon's wings (65 degrees off). Since 2026-11-05 the axis comes from the body
    (docs/rig-anim.md "QA auto-yaw", 44 of 48 within 15 deg); the misses remain.
    The rig stage refuses to roll past 30 degrees; fixing it is a pinned `yaw` and qa,
    which deletes the paint. The batch runs the facing check right after qa and stops the
    monster before paint when it would be refused (`manifest.rig.facing`, an `errors`
    entry with the suggested yaw), so no paint is wasted.
  - Props off model (fork, heavy crossbow, mace; 10 wielders): `props.py` re-rolls only.
  - No record wields a prop without hands. The djinni (floating) wields a scimitar but the
    floating attack is a two-arm reach.
- The 77 non-pilot `unarmed_reference` records get `--n 4` and the unarmed check
  (`docs/references.md`); its empty-hand margin is thin (1.90 against 2.5), so review the
  candidate strip of every armed-looking one in the viewer.

## Running the batch

```sh
python3 -I tools/batch.py audit                        # the tables above
python3 -I tools/batch.py plan                         # ordered work list, pending stages, estimate
python3 -I tools/batch.py plan --tier 1 --plans biped  # a slice
python3 -I tools/batch.py run --tier 1 --dry-run       # what it would do, touches nothing
python3 -I tools/batch.py run --tier 1 --stop-after-failures 10
python3 -I tools/batch.py run --ids bandit,guard --stages ref,mesh,qa
python3 -I tools/batch.py report                       # out/<source>/_batch.md + a summary
python3 -I tools/batch.py facing --stale               # re-run facing gates recorded by older gate code
python3 -I tools/batch.py facing --ids lich,ghast      # the same for named monsters
```

**Edit overrides only for finished monsters, or between passes.** Staleness is evaluated
live: `status()` runs when the pass reaches a monster, and forge's stages read
`data/overrides/` live too, so an override written mid-run for a monster not yet reached
changes the running pass (a prompt edit re-rolls it now, a `ref_pick` re-meshes, a `yaw`
re-runs qa). There is no `--snapshot-overrides`: forge has no way to read overrides from
anywhere but `data/overrides/<source>/`, so a snapshot would only fool the batch's own
staleness checks while the stages still used the live files. Hold an edit back until
the pass has passed the monster (its `_batch.json` entry has the stages of this session),
or until the pass ends.

Selection: `--tier 1`, `1,2` or `1-3` (0 = pilots, 4 = hold), `--plans`, `--ids` (also runs
hold records), `--include-pilots`. `run` with no `--tier` does tiers 1-3. Order: tier, then
the plan's record count (biped, quadruped, winged_quadruped, ...), then size (medium,
large, small, huge, tiny, gargantuan), then id.

What `run` does per monster, stage by stage in `forge.RUN_ORDER` (ref, mesh, qa, paint,
lod, rig, anim), each as `forge.py <stage> <id> --source <source>` in its own process group:

- **Resumable.** A stage runs only when it reads as not done, by the viewer's rules
  (`viewer.stage_status`) plus forge's rig rule (the rigged game files exist). The status
  is re-read after every stage, so what a stage invalidates downstream (paint deletes the
  LODs and rigged files, qa deletes paint) runs again. Paint disabled by override
  (`paint.enabled` false) counts as done; anim for a plan with no clip builder (radial) is
  skipped. A stage that exits 0 but still reads as not done is recorded and the monster
  is left (re-run it with forge `--force`).
- **Stale stages** run again with `--force` (forge skips a stage whose files exist):
  mesh when `ref.png` is newer than `model_raw.glb` (a `forge.py ref --force` re-roll) or
  the override's `ref_pick` is not the candidate the mesh was made from; qa when the
  override's `yaw` is not the one qa ran with (the facing gate's fix); ref when it is
  partial: done means the candidates (`ref_*.png`) AND the pick (`ref.png`, no older than
  the candidates). The viewer counts candidates alone as a reference; the batch does not
  (a ref killed after drawing left no pick and the mesh failed). forge cannot pick from
  kept candidates, so the ref is drawn again (same seed). `plan`, `run --dry-run` and the
  report show them (`stale mesh: ...`, `stale ref: 4 candidates but no ref.png ...`).
- **Stale prompt** (ref re-rolled with `--force` and the monster's own candidate count,
  the larger of `--n` and `manifest.ref.n`; the mesh then follows as stale): after every
  ref stage it runs, the batch stamps `manifest.ref.prompt_fp`, a hash of what forge
  recorded (final positive and negative prompt, `controlnet.template`, `n`). `status()`
  rebuilds the prompt forge would draw now (`current_ref_prompt`: registry prompt, per-plan
  and per-id `data/prompt_tweaks.json`, override `prompt_extra` / `negative_extra` /
  `ref_template` / `style` / `swarm_unit`, the house style and unarmed suffixes; no ComfyUI)
  and re-rolls when the hash differs. A manifest with no stamp (every reference made
  before 2026-11-05) is never stale by this rule, so 150 good references are not redrawn.
  For those, **`--reroll-changed-prompts`** (plan / run) treats the ref as stale when an
  override with a prompt field, or the `prompt_tweaks.json` entry for the id or its plan,
  is newer than `ref.png` AND the prompt built now differs from `manifest.ref.prompt`
  (an override edited only for `yaw` or notes does not re-roll). On the tier-1 review that
  is exactly its 24 re-roll ids (23 stale + warrior-infantry, which has no reference yet):
  `batch.py plan --tier 1 --stages ref,mesh,qa --reroll-changed-prompts`.
- **Stale `ref_pick` pin.** forge honours an override `ref_pick` in the ref stage too
  (`ref.png` = `ref_<pin>`), so after a re-roll the pin names a candidate of a strip
  nobody looked at (pass 3: dryad, medusa, sea-hag meshed the wrong one). After a ref the
  batch re-rolled for a changed prompt (fingerprint or `--reroll-changed-prompts`) it
  stamps `manifest.ref.prompt_reroll`; if the override carries a `ref_pick` and the
  override file is not newer than the new candidates, the pin is stale: the batch points
  `ref.png` and `manifest.ref.picked` at the strip's own pick (the unarmed check's first
  empty-handed candidate, else 0; `picked_by` says the pin was ignored, `stale_pin` keeps
  it), runs the mesh with `forge.py mesh --pick <that>`, records `stale_pin {pin, reason}`
  in `_batch.json` and an event, and `plan` / `report` list the monster under "Pins to
  re-choose". `mesh_stale` then compares the mesh with the strip's own pick, not the pin.
  A pin written after the re-roll (override newer than the candidates) is honoured as
  before (it re-meshes when it differs). Any later write of the override file counts as
  re-pinning, so write the new pin (or drop the old one) after looking at the strip.
  References re-rolled before this rule (no stamp) whose pin was written together with a
  prompt field and is older than the candidates are listed as "unconfirmed" only.
- `ref --n 3` (the pilots' practice; the viewer shows the candidates for a re-pick), and
  at least `--n 4` for `unarmed_reference` records (the unarmed check picks the first
  empty-handed one).
- **Memory gate** before mesh and paint: waits until MemAvailable (`free`'s "available") is
  at least 8 GB (`--min-free-gb`), logging every minute; short at once, it asks ComfyUI to
  `/free`; after 15 min it restarts the unit.
- **ComfyUI restart** (`systemctl --user restart comfyui`, then waits for `/system_stats`,
  6-10 s) before every mesh and paint (`--restart-before`, default `mesh,paint`;
  `--no-restart` to skip): paint pushes ComfyUI to its 16 GB `MemoryHigh` and stalled
  twice; mesh reaches the same ceiling even from a fresh process, and after a ref (which
  leaves ~14 GB resident, 4.6 GB after `/free`) the Trellis2 texture-VAE decode stalled in
  reclaim past the 288 s timeout (smoke run, below). Before every ComfyUI stage (ref, mesh, paint, rig)
  `/queue` must answer within 60 s, else restart, and must be empty within 60 s
  (`QUEUE_BUSY_S`; forge's `wait_idle` would wait forever), else the queue is cleared and
  the unit restarted. While a stage runs a watchdog polls
  `/queue` every 10 s; silent for 60 s means a hung ComfyUI: the stage is killed, the unit
  restarted and the stage retried once.
- **Facing gate** right after qa (so a `ref,mesh,qa` pass knows the refusals before the
  paint pass), else before paint, for monsters the rig stage will roll (not the
  `rig.FACING_ADVISORY` plans, not when `rig_facing_check` is false or `rig/chosen.glb`
  exists): `rig.facing_check` on `model.glb` (Blender only, ~3-4 s). Refused = recorded
  (`_batch.json` stage `facing` with the suggested yaw, a `manifest.errors` entry) and the
  monster is left before paint; a later pass clears that entry (and the `manifest.rig`
  placeholder). `unverified` (head end not named, a disputed axis, or mirror confidence under
  0.5; docs/rig-anim.md "Facing check") is not a refusal and not a pass: ref, mesh and qa go
  on, `_batch.json` keeps the reason and `hint_yaw`, but **the batch will not start paint**
  (log line, `paint_blocked` event; not counted as a failure, so `--stop-after-failures`
  ignores it). The report lists these under "Facing to verify" with their thumb paths, and
  they are not "Ready for paint". A reviewer looks at `thumb.png`, then pins `yaw` (qa and
  the gate run again) or sets `rig_facing_check` false: that override is the only bypass
  (`overrides.py` has no separate verified field). `--no-facing-gate` turns the gate and
  this hold off.
  The gate re-runs only after a qa the pass runs, or before paint, so a record from an
  older gate stays until then: **`batch.py facing [--ids ...] [--stale]`** re-runs it
  (`rig.facing_check`, CPU Blender, one monster at a time, only while MemAvailable is at
  least `--min-free-gb` 8; it waits otherwise and never asks ComfyUI to `/free`) for
  monsters the pass will not touch (ref, mesh, qa done, nothing stale, not rigged, the
  gate applies). `--stale` picks the gates recorded by older code (`rig/facing.json`
  without head-end `cues`, or older than `tools/blender_rig.py` or `model.glb`); where
  `rig/facing.json` is current but `_batch.json`'s record is older or differs, it
  re-derives the verdict from the file without Blender. It writes `rig/facing.json` and
  merges the monster's `stages.facing` (and its facing errors) into `_batch.json` (read,
  update, temp file in the same dir, rename). A running `run` rewrites `_batch.json` from
  its own copy at its next save and drops such entries: the report reads around that
  (below), and `facing --stale` after the pass re-syncs them in seconds.
- **Timeouts**: 3x the slowest run seen of the stage (`--timeout-scale`), at least
  120 s (`--timeout-floor`): ref 180 s with 4 candidates, mesh 312, paint 513, rig 465
  (SkinTokens) or 120. On a timeout (or a hang) the process group is killed (Blender and
  SkinTokens children too), then ComfyUI's queue is cleared (`POST /queue {"clear": true}`,
  `/interrupt`) and the unit restarted before the next stage: `/interrupt` alone stops the
  running job only, and a killed mesh had queued its decimation retry, which kept ComfyUI
  busy for the next 1-3 refs until they timed out too (tier-1 pass 2026-11-05: 9 refs
  lost that way). The stage's done-marker written in the killed attempt (`ref.png`,
  `model_raw.glb`, `model.glb`, `base_color_painted.png`, `lods.json`, `model_rigged.glb`)
  is removed so the stage reads pending and the next `run` redoes it; the log line says
  `outputs: ... stage reads pending` (or `STILL READS DONE`: look by hand). The manifest
  gets an `errors` entry (`batch: <stage> timed out after ...`), `_batch.json` the events
  `stage_timeout`, `comfy_clear`, `comfy_restart`.
- **Failures** do not stop the batch: forge records them in `manifest.errors`, the batch
  in `_batch.json`, and moves on to the next monster. `--stop-after-failures N` stops after
  N failed stages. A broken environment does stop it: 3 monsters in a row whose stage fails
  within 10 s (`--max-fast-failures`; the smoke run's /tmp quota made ComfyUI answer HTTP
  500 to every mesh in 0.1 s) end the run with a `stopped` event instead of failing the
  whole tier.
- Progress: `out/<source>/_batch.json` (or `--batch-dir`), rewritten after every stage:
  per monster the stage results (status, seconds, error), done and pending stages, rig
  quality (score, problems, joints, tool, facing, missing sockets), missing clips and the
  manifest errors; sessions with wall time; events (restarts, hangs, memory waits). Each
  monster's forge output goes to `out/<source>/_batch_logs/<id>.log`.
- `report` writes `_batch.md`: totals and wall time, done / pending / skipped / failed per
  stage, failures by stage with the error text (facing refusals while still refused), rig
  problems by plan, every monster the batch touched (done, pending, ref / mesh / qa
  seconds, facing, the unarmed pick and scores, stale stages), and the monsters
  needing review sorted by rig score (structural problems, SkinTokens scores below -2,
  facing warnings, missing sockets or clips, unarmed warnings). It reads every manifest in
  the registry, pilots included. The totals count facing refusals, `unverified` facings,
  pins to re-choose and monsters ready for paint; sections **Pins to re-choose** (stale
  pins, re-rolls pending with a pin, unconfirmed old ones) and **Ready for paint** (ref,
  mesh, qa done; nothing stale; no pin to re-choose; facing `ok` or the gate not applicable:
  `rig_facing_check` false, an advisory plan, a kept `rig/chosen.glb`; not on hold; paint
  pending; review verdicts are not read), with the not-ready ones and why. Facing is
  taken from `_batch.json`, or re-derived from `rig/facing.json` when that file is newer
  (marked "from rig/facing.json"); a manifest gate error or `manifest.rig` placeholder
  that a later check superseded is not listed.

## What to watch

### Smoke run (2026-11-04, tier 1, `ref,mesh,qa`, no paint)

Six tier-1 records: bandit-captain (biped, `unarmed_reference`, n 4), ghoul (biped, no
weapons), hyena (quadruped, wolf template), black-dragon-wyrmling (winged_quadruped),
phase-spider (insectoid, large), gibbering-mouther (amorphous).

| id | ref s | mesh s | qa s | facing gate | verdict |
|---|---:|---:|---:|---|---|
| bandit-captain | 59.6 (cold, 4 + unarmed check) | 288 timeout, then 75.5 | 2.7 | ok 0.5 | good: 4 of 4 empty-handed (scores -0.76, -2.22, 1.28, -1.11), auto pick 0 right; faces front, grounded, 1.8 m |
| ghoul | 28.1 | 65.1 | 2.6 | ok 0.0 | **ref off-model**: all 3 a bare skeleton (and 3 more with `prompt_extra` / `negative_extra`); mesh stale, needs a prompt replacement |
| hyena | 22.1 | 103.6 | 2.9 | ok 2.5 | good: on-model, faces +Y, 4 feet down, 1.3 m long |
| black-dragon-wyrmling | 22.1 | 70.0 | 2.7 | refused 50.0 (conf 0.74), ok 0.0 after `yaw` -155.6 | shape fine, colour off (teal and orange, not black); facing fixed by the pin |
| phase-spider | 22.1 (x2) | 81.8, 71.8 | 2.8-3.1 | refused 84.5 on the re-roll; `yaw` -173.4 | **ref top-down** in 3 of 3 (mesh pitched up, 3.7 m tall); re-roll with "side view at eye level" gave ref_1 (pinned), mesh 2.1 x 3.0 x 2.0 m, head forward |
| gibbering-mouther | 22.1 (x2) | 89.6, 81.9 | 2.8-3.0 | advisory plan | **ref off-model** in 3 of 3 (a red lizard on four legs); re-roll with "shapeless heap, no legs" gave ref_2 (pinned), a dome with a face, grounded |

- Timings: ref 22.1 s warm with 3 candidates (28 s right after a mesh, 59.6 s cold with 4
  and the unarmed check); mesh 65-104 s, mean 80 (the 70 s estimate is 14% low, kept); qa
  2.6-3.2 s; facing gate 2.7-3.7 s; ComfyUI restart 6-10 s. A `ref,mesh,qa` monster is
  ~2 min: tier 1 is ~4.4 h before paint (`plan --tier 1 --stages ref,mesh,qa`).
- Memory: ComfyUI sits at its 16 GB `MemoryHigh` (cgroup `memory.current`) through every
  mesh, even from a fresh process (`memory.events` high +12k per mesh); system available
  3.2-4.6 GB at the mesh peak, VRAM 10.6-12.0 GB, swap full the whole time. After a ref
  ComfyUI holds ~14-15 GB; `/free` takes it to 4.6 GB, which was not enough: the first
  mesh's texture-VAE decode stalled at 16.2 GB in reclaim (`high` 370k events) and hit
  the 288 s timeout. Restarting the unit before mesh fixed it (7 meshes, none over 104 s).
  The memory gate waited 70 s on the first mesh before the `/free` change; none after.
- References are the real risk, not the stages: 3 of 6 "proven" records came out
  off-model on every candidate, and only the candidate sheet shows it (no gate does).
  Look at every monster's strip and thumbnails before its paint pass; fixes are override
  `prompt_extra` / `negative_extra` + `forge.py ref <id> --force` + a `ref_pick`, after
  which the batch re-meshes on its own (stale mesh).
- Facing: 2 refusals in 6 (the dragon's wings, as predicted; the re-rolled spider), both
  fixed by a pinned yaw, one only after flipping the sign by eye (above).
- A full /tmp user quota (12.5 GB, another job's scratch) made ComfyUI fail every request
  with EDQUOT: keep `/tmp` below its quota during a run.

- Nothing else may use ComfyUI during a run: the batch restarts the unit before every
  mesh and paint, clears its queue and restarts it after every timeout, and clears and
  restarts it when the queue is still busy 60 s before a ComfyUI stage.
- Timeouts: `grep -E "timeout|killed|queue busy" <run log>`. One timeout followed by
  more on the next monsters' refs was the leftover-queue bug (fixed: look for
  `clearing the ComfyUI queue + restart` right after each `killed`). A timed-out stage
  stays pending; `plan` lists it and the next `run` redoes it (a partial ref shows
  `stale ref`). Mesh timeouts themselves (hobgoblin-warrior, earth-elemental, ogre,
  stone-golem, black-bear on the tier-1 pass, all at 312 s) are a separate question: read
  the monster's `_batch_logs/<id>.log` before raising `--timeout-scale`.
- The first hours: `tail -f out/srd-5.2.1/_batch_logs/<id>.log`, the viewer (the dashboard
  counts per plan, the candidate strip, the rig HUD), and `batch.py report` every few
  dozen monsters.
- `_batch.json` events: repeated memory waits or hangs mean the ComfyUI memory ceiling
  (TODO "State": raise `MemoryHigh` or keep the restarts).
- Facing refusals (winged bodies, and any mesh QA turned sideways): pin `yaw` from the
  suggested value in `data/overrides/srd-5.2.1/<id>.json` and run the monster again (the
  batch re-runs qa with `--force` and the gate). Then look at `thumb.png`. Since
  2026-11-05 a body the head-end cues call backwards is refused with `suggested_yaw` = QA's
  yaw + ~180 (boar, giant-badger, rhinoceros); when they cannot tell (`head_known` false)
  the gate reads it `unverified`, paint waits, and the report's "Facing to verify" lists it:
  look at `thumb.png` (the front tile shows the face, not the rump), use `hint_yaw` or QA's
  yaw + 180 if it is backwards, or set `rig_facing_check` false if it faces front. A wrong
  mirror axis can also refuse a mesh that faces front (black-bear, giant-hyena, gorgon): the
  thumb decides, never pin the suggested yaw unseen (adult-brass-dragon's -36.7 is such a
  pin: sideways). Front-view bipeds are no longer refused for a robe or tail
  (their sign is the front view's, by construction).
- Unarmed warnings ("every candidate scores armed"): look and pin `ref_pick`, or re-roll.
- Rig problems by plan: winged bipeds without wings and quadrupeds without a spine or
  tail are the pilots' known failure modes; `forge.py rig <id> --rig-reroll` then
  `anim <id> --force`.
- Disk: the pilots take 110-200 MB each in `out/` (paint/ intermediates ~64 MB, rig rolls
  ~26 MB, textures ~25 MB, three ~14 MB GLBs), so ~55 GB for the batch; `/home` has 1.2 TB free.

## Recommended order

1. Tier 1 (155): bipeds first (86, the largest group, proven by the goblin, 57 of them
   with the unarmed check), then quadrupeds (27), winged quadrupeds (19 dragons), birds
   (7, as the giant-eagle), amorphous (5), arachnids (3), tentacled (2), merfolk (2),
   specter and wraith (as the ghost), the shrieker and the reef shark. Stop and look after
   the first 20 (`report`).
2. Tier 2 (113): the same plans at tiny / huge / gargantuan sizes, horses, cats, feathered
   winged quadrupeds, crabs and beetles, bat-winged avians, 4 swarm units (bats, ravens,
   insects, piranhas) and the 20 winged bipeds (expect rig re-rolls or hand wing chains).
3. Tier 3 (33): the variants one plan at a time, reviewing each in the viewer; most will
   need a rig or clip fix afterwards (`--stages rig,anim --force`).
4. Tier 4 (14) as each blocking gap is fixed: `run --tier 4 --plans serpentine` once the
   constrictor-snake pilot is through anim (the snake swarm with it: `--ids
   swarm-of-venomous-snakes`), then the centauroids.
5. After the batch: the clip builders (`cast`, `swim`, `burrow`, `breath`, `fly`), the size
   thresholds and the winged_biped wings, each followed by `forge.py run <ids> --stages
   anim --force` (or `rig,anim`) on the records it affects (`batch.py audit` lists them).
