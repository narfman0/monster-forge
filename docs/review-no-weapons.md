WEAPON ids: barbed-devil, centaur-trooper, chain-devil, shield-guardian, succubus

# No-weapons audit of the SRD references (2026-11-06)

Rule (user, 2026-11-06): no monster has a weapon baked into its reference or mesh (quivers, arrows, swords, bows, staves, shields, clubs, daggers, held objects used as weapons). The game attaches weapons at the hands / wrists, so hands must be free and visible. Pouches, belts, bags, capes and armour are fine; an empty sheath / scabbard is borderline ("minor").

Method: every `out/srd-5.2.1/<id>/ref.png` (the picked reference; the `ref_N` alternates were not audited) in labelled montages of 20 at 280 px, with full-size zooms of every figure that had something at the hands, hips or back (~30 zooms). Montages and notes: `~/.cache/monster-forge-scratch/weaponaudit/` (not tracked). Stage from the files on disk: mesh = `model.glb`, painted = `paint/bake.json`, rigged = `model_rigged.glb`, released = `"released": true` in the manifest.

## Counts (330 records)

| verdict | count |
|---|---|
| ok | 295 |
| minor | 7 |
| WEAPON | 5 |
| handled (moved aside earlier, no ref.png) | 10 |
| no ref.png, not audited | 13 |

None of the 5 released monsters (giant-spider, harpy, owlbear, wolf, young-red-dragon) is flagged.

## WEAPON

| id | kind | stage | frozen | recommended fix |
|---|---|---|---|---|
| barbed-devil | dagger-like blade in each hand | painted + rigged | `_frozen.ids` | desc edited (below: "bare empty clawed hands with long fingers"); add the style `unarmed_negative` (it gets none today: `unarmed_reference` false). Unfreeze to re-roll. |
| centaur-trooper | arrow / spear shaft over the right shoulder, a second shaft strapped at the horse flank (the quiver's arrows) | rigged, not painted (pilot) | `_frozen.ids` | The desc was already cleaned in 3d1433d, but two stale copies keep the quiver: the registry prompt (`data/registry/srd-5.2.1.json` still ends ", arrow quiver on the back") and the per-id `prompt` in `data/prompt_tweaks.json` ("bearded warrior with a breastplate and an arrow quiver on the back"). Drop the quiver from that per-id prompt and add `(quiver, arrows, arrow, shaft:1.4)` to its `negative_append` (round 4 in `tried_2026_11_05` already showed it works: 4 of 4 clean). Expensive: pilot with a depth template and a hand-picked rig. |
| chain-devil | chains gripped in both hands, swinging out from the fists (iconic, but they are the stat block's weapon) | painted + rigged | `_frozen.ids` | desc edited: "hooked chains trailing from its body" (drew chains held and swung) -> "chains bound tight around its torso and limbs, bare empty clawed hands"; add `unarmed_negative` plus `(chain in hand, holding chains:1.3)`. Unfreeze to re-roll. |
| shield-guardian | round shield on the left arm | painted + rigged | not frozen | desc edited: "with a shield arm" (the cause) -> "heavy armored forearms, glowing amulet socket in the chest, bare empty iron hands". Add `shield` to its negative (the name itself says shield; consider `(shield, buckler:1.4)` per id). Not frozen: the next batch run would re-roll it as soon as its prompt fingerprint changes. |
| succubus | long red sickle-like blade hanging from each hand (could be read as claws; reads as held blades) | mesh only | `pass5_ids` | desc edited: + "bare empty hands with slender fingers"; add `unarmed_negative`. Cheap: mesh only. Unfreeze (pass5) to re-roll. |

## minor

| id | kind | stage | frozen | recommended fix |
|---|---|---|---|---|
| animated-flying-sword | the monster IS a longsword (inherent, exempt) | mesh | `pass5_ids` | none: the sword is the body. Make sure the game does not also attach a weapon to it. |
| earth-elemental | a glowing orb / crystal clutched in each fist (not a weapon, but the hands are closed and not free); boulders in the background | painted + rigged | `_frozen.ids` | if re-rolled: "boulder fists" -> "open rocky hands with thick stone fingers"; negative `glowing orb, held crystal`. Low priority. |
| goblin-minion | a small hooked tool / knife handle at the belt, right hip (tiny; unsure) | painted + rigged | `_frozen.ids` | desc already clean; registry prompt is stale (", knife sheaths on the belt"): rebuild the registry. No re-roll needed for this alone. |
| imp | the barbed tail tip is an axe / pick-shaped hook right next to the left hand, reads as a held pickaxe | mesh | `pass5_ids` | "barbed scorpion-like tail" -> "thin tail ending in a small stinger"; negative `pickaxe, axe`. Re-roll is cheap if wanted. |
| knight | two thin diagonal rods and a red plume rod behind the helm: could read as crossed blades or spear tips slung on the back (unsure, may be helmet crest spikes) | painted + rigged | `_frozen.ids` | none unless the mesh shows them; if re-rolled: "plain helm with no crest". |
| spy | a black holster-like case at the left hip (may be a pouch) | painted + rigged | `_frozen.ids` | desc already clean; registry prompt is stale (", scabbard and holster at the hip"): rebuild the registry. |
| troll-limb | the hand is clamped on a big log / stump (terrain, but held; would bake in as a club-like mass) | mesh | not frozen | already on the tier-3 re-roll list (docs/review-tier3.md: drawn as a whole troll); add `log, stump, tree trunk, holding` to its negative. |

## handled (moved aside for a re-roll with quiver negatives, no ref.png now)

gnoll-warrior, goblin-boss, goblin-warrior, hobgoblin-captain, hobgoblin-warrior, scout, skeleton, weretiger, werewolf, wight. Note: their registry prompts are still the stale ones with "arrow quiver on the back" / "scabbard at the hip" (see cause 2): rebuild the registry before re-rolling them, or the quiver negatives fight the prompt.

## not audited (no ref.png)

behir, couatl, drider, flying-snake, giant-constrictor-snake, giant-venomous-snake, grick, guardian-naga, lamia, purple-worm, spirit-naga, swarm-of-venomous-snakes, venomous-snake (lamia and drider have hands: check them when drawn).

## ok (295)

Every other id with a ref.png. Zoomed and cleared: assassin, bandit, bandit-captain, bugbear-stalker, bugbear-warrior, druid, gladiator, green-hag, guard (a key chain at the hip), harpy, kobold-warrior, lich, mimic (chest contents are trinkets), minotaur-of-baphomet, noble, oni (sash cord end, not a hilt), pirate, pirate-captain (hip pouches), rakshasa, satyr, tarrasque (red tabard, not a blade), tough, warrior-infantry, warrior-veteran, wereboar. Glanced at with care because their stat blocks are armed and found empty-handed: deva, planetar, solar, erinyes, ice-devil, horned-devil, marilith, glabrezu, hezrou, nalfeshnee, bone-devil, cloud / fire / frost / hill / stone / storm giant, ettin, ogre, troll, mummy-lord, vampire, guard-captain, tough-boss, sahuagin-warrior, merfolk-skirmisher.

## Systematic causes

1. **The unarmed gate skips hands-but-unarmed monsters.** `forge.py` appends the style's `unarmed_suffix` / `unarmed_negative` only when `unarmed_reference` is set, and `build_registry.py` sets it only for `hands and armed` (wields a prop or `armed_look`). 48 records have hands but no wields (fiends, golems, undead, hags, mephits...) and their negative has no weapon word at all. 4 of the 5 WEAPON hits are in that group: barbed-devil, chain-devil, succubus (fiends whose claws / chains turned into blades) and shield-guardian. Fix: make `unarmed_reference = hands` (every hand-bearing monster gets the empty-hands suffix and weapon negative), or at least add the negative to all of them. As a rule entry it would re-roll nothing frozen; as a style change it would change every hands prompt fingerprint.
2. **The registry was not rebuilt after 3d1433d.** `data/visual_profiles.json` dropped the weapon carriers, but `data/registry/srd-5.2.1.json` (what `forge.py` reads) still carries them for 26 records: ", arrow quiver on the back" (centaur-trooper, gnoll-warrior, hobgoblin-captain, weretiger, werewolf), "scabbard at the hip and an arrow quiver on the back" (goblin-boss, goblin-warrior, hobgoblin-warrior, scout, skeleton, wight), "scabbard and holster at the hip" (assassin, bandit-captain, pirate-captain, spy), "scabbard at the hip" (guard-captain, noble), "knife sheaths on the belt / sash" (goblin-minion, kobold-warrior, pirate, vampire-familiar), "bolt case on the belt" (bandit, tough, tough-boss, warrior-veteran, wererat). Run `python3 -I tools/build_registry.py` before any re-roll (it also picks up the 4 desc edits below). The frozen ones among them drew clean anyway (pouches); only the 10 handled ones and centaur-trooper drew the gear.
3. **Description words that name the weapon:** "a shield arm" (shield-guardian), "hooked chains trailing from its body" (chain-devil). Edited.
4. **Per-id prompts in `data/prompt_tweaks.json` bypass the desc:** centaur-trooper's full `prompt` names the quiver.

## Edits made (data/visual_profiles.json, desc text only)

- barbed-devil: "long clawed fingers" -> "bare empty clawed hands with long fingers"
- chain-devil: "hooked chains trailing from its body" -> "chains bound tight around its torso and limbs, bare empty clawed hands"
- shield-guardian: "in a vaguely knight-like shape with a shield arm and amulet socket" -> "in a vaguely knight-like shape, heavy armored forearms, glowing amulet socket in the chest, bare empty iron hands"
- succubus: + ", bare empty hands with slender fingers"

Not touched: `_frozen.ids` / `pass5_ids`, `data/prompt_tweaks.json`, the registry, any ref.png or output. To re-roll: drop barbed-devil, centaur-trooper, chain-devil from `_frozen.ids` and succubus from `pass5_ids` (shield-guardian is not frozen), rebuild the registry, fix centaur-trooper's per-id prompt.

## Out of scope, seen in passing

- sphinx-of-lore's reference is topless (nipples visible), mesh only, not frozen; it needs a re-roll on NSFW grounds.
- red-dragon-wyrmling's reference has drawing pens at the image edges (crop / ground clutter, not weapons).
- death-dog is drawn as two separate dogs (the two heads became two bodies); hill-giant is a featureless blob (both mesh only).
