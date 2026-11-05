# Body plans

Every monster gets exactly one `body_plan`. It describes the **skeleton
layout** an asset needs: which rig template, which locomotion clips, and which
pose the reference image uses. It does not describe the creature's type, size,
or abilities. When a creature has several forms (lycanthropes, mimic,
doppelganger), we classify the form the asset depicts and say which in
`body_plan_note`.

Classification runs in two stages (`tools/build_registry.py`):

1. **Rules**: swarm `type_line`, ooze type, true dragons (type dragon + fly
   speed), name tokens (snake/worm/naga -> serpentine, spider/scorpion/crab ->
   insectoid, shark/whale -> aquatic, octopus/kraken/roper -> tentacled,
   eagle/owl/bat -> avian), humanoid/giant -> biped, beasts by speed profile.
   Fiends, celestials, fey, elementals and constructs fall through to a 0.5
   confidence guess.
2. **Hand curation**: `data/body_plan_overrides.json` (192 SRD entries) sets
   the plan, confidence, and optionally the rig template and a note.
   `data/body_plan_reviewed.json` lists the remaining 138 slugs whose rule
   result was checked by hand and confirmed. Their confidence becomes 1.0.

All 330 SRD 5.2.1 monsters are either overridden or reviewed. Confidence is
below 1.0 only where the anatomy is genuinely ambiguous or depictions disagree.

### Per-source curation layout

`body_plan_overrides.json` and `visual_profiles.json` are keyed by slug. Their
flat top-level slugs are SRD 5.2.1 (the original layout, unchanged); every other
source goes under a `sources` key:

```json
{
 "aboleth": {"body_plan": "tentacled", "confidence": 0.85},
 "...": {},
 "sources": {
  "mm-2024": {
   "beholder": {"body_plan": "floating", "confidence": 1.0, "rig_template": "custom.eye_tyrant"}
  }
 }
}
```

`build_registry.load_curation(name, source)` returns one source's table (a
`sources["srd-5.2.1"]` block, if one is ever added, merges into the flat keys;
keys starting with `_` are comments). `body_plan_reviewed.json` and
`equipment.json["monsters"]` were already keyed by source id. Each builder
validates only its own source: `build_registry.py` (SRD) and
`import_names.py` (MM-2024) fail on unknown slugs, bad plans, overlap between
overrides and the reviewed list, and props without a spec. An MM-2024 visual
entry may hold only `scale_m`; its prompt description then stays the template.
Per-monster pipeline overrides were per source from the start
(`data/overrides/<source>/<id>.json`).

## The enum

| body_plan | Definition | Default rig template | Variants used |
|---|---|---|---|
| `biped` | Upright, two legs, two (or more) arms. Humanoids, giants, devils, golems, undead, hybrid lycanthropes, treant, apes. | `rigify.basic_human` (Rigify "Basic Human"; use "Human" with face rig for hero NPCs) | `custom.kaiju_tail` (tarrasque: human rig + heavy tail chain), `custom.ghost` (shadow) |
| `quadruped` | Four walking legs, horizontal spine. Mammals, lizards, frogs, crocodiles, hydra, owlbear, dragon turtle. | `rigify.basic_quadruped` | `rigify.horse` (equines, bovines, deer, goats, camel), `rigify.wolf` (canines, hyenas), `rigify.cat` (felines), `custom.hydra` (+5 neck chains), `custom.turtle` |
| `avian` | Bird-like: two hind legs, horizontal spine, forelimbs are wings or vestigial. Birds, bats, pterosaurs, wyvern, theropod dinosaurs. | `rigify.bird` | `custom.bat_wing` (membrane wing with finger bones), `custom.wyvern` (bat wing + stinger tail), `custom.theropod` (bird legs, tiny arms, heavy tail, no wings) |
| `winged_biped` | Upright biped with a separate pair of wings on the back. Angels, winged devils/demons, gargoyle, imp, mephits, sprite. The harpy and vrock are classed here because they are drawn with arms *and* wings. | `custom.human_winged` (Basic Human + 2 wing chains from the upper spine) | |
| `winged_quadruped` | Four legs **plus** a separate pair of wings: six limbs. True dragons, pseudodragon, griffon, hippogriff, pegasus, chimera, manticore, sphinxes. | `custom.dragon` (Rigify wolf/cat base + long neck and tail chains + 2 wing chains with finger bones) | `custom.quadruped_winged` (cat/lion base + feathered wings), `custom.horse_winged` (pegasus) |
| `serpentine` | Limbless or near-limbless long body. Snakes, nagas, purple worm, couatl, grick, behir (12 small legs). | `custom.spine_chain` (20-40 bone spline-IK chain, head, jaw) | `custom.spine_chain_winged` (couatl), `custom.serpent_multileg` (behir: chain + 6 short leg pairs) |
| `aquatic` | Swims, no walking legs: fish, sharks, whales, seahorses, flipper-limbed marine reptiles. | `rigify.shark` | `custom.flipper` (plesiosaurus, archelon: torso + 4 flipper chains + neck), `custom.spine_chain` (seahorses) |
| `insectoid` | Six or more walking legs: insects, arachnids, crustaceans, myriapods, and the eight-legged basilisk. | `custom.hexapod` | `custom.arachnid` (8 legs, spiders and scorpions; scorpions add tail chain), `custom.decapod` (crabs, chuul: 8 legs + 2 claws), `custom.myriapod` (centipede, remorhaz: repeated segment+leg-pair module), `custom.hexapod_winged`, `custom.multileg_reptile` (basilisk) |
| `amorphous` | No fixed skeleton. Oozes, gibbering mouther, mimic (chest form + pseudopods), lemure. | `custom.blob` (lattice / a few soft bones + shape keys) | `custom.blob_torso` (lemure: blob + humanoid head/arms) |
| `floating` | Hovers with no locomotor limbs. Ghosts, specters, wraiths, will-o'-wisp, air/fire/water elementals, djinni, invisible stalker, cloaker. | `custom.ghost` (human upper body + tapering tail chain, no legs) | `custom.elemental_column` (vortex column + arms), `custom.wisp` (single root + orbiting glow bones; MM flameskull, demilich, gas spore), `custom.membrane` (cloaker: bone grid), `custom.eye_tyrant` (MM beholder family: sphere root + jaw + one short chain per eyestalk) |
| `swarm` | A swarm stat block. The asset is **one** tiny unit model plus instancing/boids at runtime. | `custom.swarm` (unit uses its own plan's rig; the swarm is a particle/instance system) | |
| `plant_object` | Stationary or nearly stationary plants, animated objects, body parts. Shrieker, violet fungus, awakened shrub and tree, flying sword, rug of smothering, troll limb. | `custom.rigid_prop` (one root bone) | `custom.stalk` (fungi: stalk chain + tendrils), `custom.tree_roots` (trunk + branch chains + root legs), `custom.cloth_grid` (rug), `custom.limb_chain` (troll limb) |
| `centauroid` | A humanoid torso where the head would be, mounted on a non-humanoid multi-legged lower body. Centaur, drider (spider lower body), lamia (lion lower body). | `custom.centaur` (Rigify horse + Basic Human spine/arms/head from the withers) | `custom.drider` (arachnid + human torso) |
| `merfolk_naga` | A humanoid torso on a legless serpent or fish tail. Merfolk, merrow, marilith (six arms), salamander. Full-snake nagas with only a human face are `serpentine`. | `custom.naga` (Basic Human upper body + spline-IK tail chain) | |
| `tentacled` | Tentacles are the main limbs for movement or attack. Octopuses, kraken, roper, otyugh, aboleth, darkmantle. | `custom.tentacle_hub` (body root + N tentacle chains with spline IK) | |
| `radial` | Radially symmetric body with limbs arranged around a central axis. Xorn (3 legs, 3 arms, top mouth). Kept separate because no mirrored rig works. | `custom.radial_tripod` | `custom.radial_pentapod` (MM modron pentadrone: 5 legs + 5 arms) |

### Edge-case decisions

- **Dragons vs wyvern.** True dragons have four legs and two wings, so they are
  `winged_quadruped`. The wyvern's wings *are* its forelimbs (bat or pterosaur
  layout), so it is `avian` with the `custom.wyvern` rig.
- **Theropods** (allosaurus, T. rex) are `avian` with no wings. Birds are
  theropods, and the hind-leg/tail/spine layout is the same. Using a human
  biped rig would be wrong.
- **Harpy** is `winged_biped`: 5e art shows arms and wings. If an art pass
  merges them, switch it to `avian`.
- **Drider** and **lamia** are `centauroid`, with the lower body named by
  their rig variant. Lamia is 0.55: depictions disagree between a lion and a
  serpent lower half. A serpent tail would make it `merfolk_naga`.
- **Behir** and **grick** are `serpentine` (0.7). The extra legs or tentacles
  are attachments on a spine chain.
- **Basilisk** is `insectoid` (0.6) by leg count (eight). It needs a
  multi-leg rig even though the surface is reptilian.
- **Elementals.** Earth is a `biped`. Air, fire and water are legless
  columns: `floating` with `custom.elemental_column`, even when they only
  have a walk speed. The djinni has a whirlwind lower body (`floating`). The
  efreeti has legs (`biped`, 0.8).
- **Shadow** walks and has no fly speed, so it is a `biped` (0.75). It can
  share the ghost rig.
- **Lycanthropes** are modelled in hybrid form (`biped`). Beast forms reuse
  the matching beast asset.
- **Owlbear, displacer-beast-likes, hydra** are `quadruped`. Extra heads,
  tentacles or necks are rig add-ons, not a different plan.
- **Ape / giant ape** are `biped` (knuckle-walker on a human rig with long
  arms). **Baboon** is `quadruped`. Both are 0.7-0.8.
- **Tarrasque** is `biped` (0.7): an upright kaiju with a heavy tail.
- **Troll limb** is `plant_object` (0.5): a crawling severed arm, not really
  a body plan.
- **Stirge** is `avian` (0.6): it has bat wings and clinging legs. It could
  also be a winged insectoid.

## SRD 5.2.1 counts

| body_plan | count |
|---|---:|
| biped | 99 |
| quadruped | 61 |
| winged_quadruped | 49 |
| winged_biped | 21 |
| avian | 19 |
| insectoid | 15 |
| serpentine | 11 |
| floating | 10 |
| aquatic | 9 |
| amorphous | 7 |
| swarm | 7 |
| plant_object | 7 |
| tentacled | 7 |
| merfolk_naga | 4 |
| centauroid | 3 |
| radial | 1 |
| **total** | **330** |

40 of the 49 `winged_quadruped` entries are the ten true-dragon colours times
four ages. They share one `custom.dragon` rig and differ by mesh and scale.

Rig template usage: `rigify.basic_human` 97, `custom.dragon` 41,
`rigify.basic_quadruped` 28, `custom.human_winged` 21, `rigify.horse` 15,
`rigify.bird` 12, `rigify.wolf` 11, `custom.spine_chain` 11, and 36 others
with 1-7 uses each.

`build_registry.py` prints these counts every time it runs. Update this table
when they change.

## MM-2024 counts (personal use only)

All 173 Monster Manual 2024 records not in the SRD (`data/registry/mm-2024.json`,
generated locally and git-ignored) are curated under `sources["mm-2024"]`: 92
overrides and 77 reviewed rule results. Four stay rule guesses at 0.5 because
their shape is not known well enough: `empyrean-iota`, `graveyard-revenant`,
`haunting-revenant` (a haunted place), `violet-fungus-necrohulk`.

| body_plan | before review | after |
|---|---:|---:|
| biped | 128 | 101 |
| winged_biped | 24 | 13 |
| floating | 8 | 12 |
| quadruped | 0 | 6 |
| winged_quadruped | 4 | 6 |
| serpentine | 2 | 6 |
| plant_object | 0 | 6 |
| swarm | 4 | 4 |
| merfolk_naga | 0 | 4 |
| amorphous | 2 | 3 |
| avian | 0 | 3 |
| insectoid | 0 | 3 |
| tentacled | 0 | 3 |
| radial | 0 | 2 |
| centauroid | 0 | 1 |
| aquatic | 1 | 0 |
| **total** | **173** | **173** |

Confidence: 120 at 1.0 (was 0), 14 below 0.8 (was 129 at 0.5).

MM-2024 edge cases:

- **Beholder family** (beholder, beholder zombie, death tyrant, spectator) are
  `floating` with `custom.eye_tyrant`. The gas spore is `floating` with
  `custom.wisp`; the flameskull and demilich (floating skulls) too.
- **Grell, flumph** are `tentacled` (they hover, but the tentacles are the
  limbs). The giant squid is `tentacled`, not `aquatic`.
- **Yugoloths and gith that fly by magic** (arcanaloth, ultroloth, githzerai
  psion, githyanki dracomancer), **mind flayers** and **vampires** are `biped`:
  a fly speed without wings does not make a `winged_biped` or `floating` plan.
  The nycaloth has wings (`winged_biped`).
- **Multi-armed uprights**: mezzoloth and thri-kreen are `biped` with 4 arms,
  the modron quadrone a `winged_biped` with 4; the tridrone is `radial` (3 legs,
  3 arms, holds a spear), the pentadrone `radial` with `custom.radial_pentapod`.
- **Yuan-ti**: malison type 1 (snake head) and type 2 (snakes for arms, no
  hands) are `biped`; type 3 and the abomination are `merfolk_naga`.
- **Displacer beast** is `quadruped` (`rigify.cat`), six legs and two shoulder
  tentacles as add-ons; the **intellect devourer** is a `quadruped` brain.
  **Carrion crawler** and **young remorhaz** are `insectoid` (`custom.myriapod`),
  the **chasme** `insectoid` (`custom.hexapod_winged`).
- **Blights**: tree and Gulthias blights and the twig blight are `plant_object`
  (`custom.tree_roots`); needle and vine blights are humanoid-shaped `biped`s
  without hands (as the shambling mound).
- **Genies**: the dao has legs (`biped`, 0.75, like the efreeti); the marid is
  `floating` with `custom.elemental_column` and hands (0.6, art varies).
- **Shapechangers** are modelled in one form (`body_plan_note`): jackalwere and
  animal lord in hybrid form, yochlol in its slime-pillar true form
  (`amorphous`, `custom.blob_torso`).

## Hands and sockets

The four upright plans (`biped`, `winged_biped`, `centauroid`, `merfolk_naga`)
have graspable hands by default. Every other plan has none. `data/equipment.json`
lists the exceptions:

- Without hands: earth elemental, shambling mound, treant, tarrasque.
- With hands: the floating djinni, ghost, specter and wraith.

The baboon, lemure and xorn have hand-like limbs but never grip anything, so they
get no sockets. The glabrezu (4 arms) and marilith (6 arms) are the multi-armed
cases. SRD 5.2.1 has 127 monsters with hands, 70 that wield a prop, and 79 with
an unarmed reference image. MM-2024 has 116 with hands, 54 that wield a prop
and 56 unarmed references; its exceptions are in `equipment.json["monsters"]["mm-2024"]`
(without hands: galeb duhr, hook horror, umber hulk, manes and manes vaporspawn, needle and vine
blights, yuan-ti malison type 2; with hands: banshee, poltergeist, shadow demon,
marid, modron tridrone). See `equipment.md`.

## Rig template naming

- `rigify.*` is a stock Blender Rigify metarig (Add > Armature): `basic_human`,
  `human`, `basic_quadruped`, `horse`, `wolf`, `cat`, `bird`, `shark`.
- `custom.*` is a project template still to be built under `workflows/`
  (usually a Rigify metarig with extra chains, or a plain spline-IK chain).
  Build them in descending order of SRD usage: `custom.dragon` (41),
  `custom.human_winged` (21), `custom.spine_chain` (11) first.
