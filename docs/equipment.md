# Equipment, sockets and props (2026-11-03)

Rule (from the user, also in `plan.md`): intelligent creatures that wield weapons are
modelled with **empty hands** and proper wrists and palms. Weapons, shields and other held
items (wands, harpoons) are separate props, attached at runtime and never baked into the
mesh. Worn gear stays baked: armour, clothing, belts, scabbards, quivers, helmets, holy
symbols on chains. Natural weapons are part of the body: claws, bites, horns, pincers, the
iron golem's forearm blade, the chain devil's chains.

## Registry fields

`tools/build_registry.py` adds these fields to every record. They come from
`data/equipment.json` (hand-reviewed against each stat block) and the body plan.

| field | meaning |
|---|---|
| `hands` | Has graspable hands that the rig exposes as hand bones. Defaults to true for `biped`, `winged_biped`, `centauroid` and `merfolk_naga`, and false for every other plan. `data/equipment.json` overrides it either way. |
| `wields` | Prop ids the creature holds, from `gear` and attack names. Duplicates mean several copies (marilith: six `longsword`). |
| `shield` | `"shield" in wields` |
| `sockets` | Bone names the rig must provide (see below) |
| `unarmed_reference` | `hands` and (`wields` is non-empty, or `armed_look`). The reference image is drawn with empty, open hands. |
| `arms` | Only present when it is not 2 (glabrezu 4, marilith 6) |
| `wields_aka` | Stat-block names for a prop (`{"greatsword": "Flame Sword"}`) |
| `equipment_note` | Why an edge case was decided the way it was |

`armed_look` (only in `data/equipment.json`) marks creatures that have hands and no weapon
in the 5.2.1 stat block but are usually drawn armed: animated armor, drider, lich, medusa,
oni, sahuagin warrior, satyr (panpipes), minotaur skeleton and ogre zombie. The image model
adds a sword or staff unless it is told not to, so they also get the unarmed reference.

### How `wields` is decided

1. `derive_wields()` scans gear entries and attack-action names for weapon words. Specific
   names come first (`light crossbow`, `greatsword`). Generic words (`sword`, `blade`, `bow`,
   `hammer`, `cudgel`, `staff`) are used only when no specific name matched.
2. `data/equipment.json` is authoritative. For 5.2.1, every slug whose plan has hands or
   whose stat block names a weapon must be listed, or the build fails. The build prints
   every slug where the curated list differs from the derived one. For 5.2.1 these are all
   deliberate:
   - Giants use two-handed weapons. Fire and storm giants get a greatsword, and the hill
     giant's Tree Club and the stone giant's Stone Club are a greatclub.
   - Planetar and solar get a greatsword. The solar also gets a longbow for its Slaying Bow.
   - Djinni and efreeti get a scimitar. Their 2d6 blades are a scimitar at Large size.
   - Sprite: Needle Sword becomes a rapier, Enchanting Bow becomes a shortbow.
3. Thrown debris is a spawned FX object, not a carried prop: the hill giant's Trash Lob, the
   stone giant's Boulder, the ape's Rock. Spell-like attacks hold nothing: Hurl Flame,
   Arcane Burst, Thorn Burst.
4. Stat-block facts win over classic art: the 5.2.1 knight has a greatsword and heavy
   crossbow and no shield, `warrior-infantry` has no shield, and the erinyes has no bow.

## Socket convention (Godot)

Each socket is a bone in the exported skeleton. Godot attaches a prop with a
`BoneAttachment3D` whose `bone_name` is the socket name, and adds the prop scene as its child.

| socket | bone | used for |
|---|---|---|
| `hand_r`, `hand_l` | the deform hand bone, renamed at export (Rigify `DEF-hand.R` becomes `hand_r`). Its head is at the wrist. | held props. Weapons default to `hand_r`. Bows and shields default to `hand_l`. |
| `hand_r1..n`, `hand_l1..n` | as above, top pair first | multi-armed wielders (marilith: 1-3) |
| `back` | non-deforming bone parented to the upper spine, between the shoulder blades | stowed bows, two-handed weapons, polearms, shields |
| `hip_l`, `hip_r` | non-deforming bones parented to the pelvis at the belt | stowed one-handed weapons (swords on the left hip, for a right-hand draw) |
| `head` | the head bone | helmets, crowns and hats when they are swapped, plus head VFX |

Every creature with hands gets the hand sockets. Every armed creature (`unarmed_reference`)
also gets `back`, `hip_l`, `hip_r` and `head`, so loadouts can be swapped the same way on
any of them. The glabrezu's pincers are natural weapons, so only its two small hands get
sockets.

### Prop space

- The prop's origin is the grip point: the centre of the fist, where the hand closes.
- +Y runs along the blade or shaft toward the tip (the head, for maces and axes).
- -Z points forward out of the fist: the cutting edge or striking face, or the direction
  the arrow points, for bows and crossbows.
- Props are built for a Medium wielder (1.8 m), in metres (`length_m`). At runtime they are
  scaled by `wielder.scale_m / 1.8` for upright bodies (giants carry giant weapons).
- Two-handed props (`grip: 2`) attach to `grip_socket`. The off hand reaches the shaft with
  a two-bone IK target, `offhand_grip`, a marker node in the prop scene 0.3-0.6 m along +Y.
- Shields: origin at the arm strap, +Y toward the top rim, -Z out of the face.

### Rig stage requirements

For every `hands: true` record, the rig stage (SkinTokens, UniRig or the Rigify fallback;
see `research-rig-animate.md`) must:

1. Produce separate hand bones with a wrist joint, plus at least a thumb and one finger
   chain, so a fist can close around a grip. Auto-riggers that end the arm in one "hand"
   blob fail this check.
2. Rename the bones at export to the socket names above, and add the non-deforming `back`,
   `hip_l` and `hip_r` bones for armed records.
3. Author a `grip` pose (closed fist) and an `open` pose for each hand. Clips for armed
   creatures (`attack_melee`, `attack_ranged`) are authored with the default loadout attached.

The mesh needs empty, open hands for this to work. That is why `forge.py ref` adds the house
style's `unarmed_suffix` and `unarmed_negative` for `unarmed_reference` records, and why their
`visual_profiles.json` descriptions mention only worn gear. The build fails if one of these
descriptions names a held weapon.

## Props registry

`data/registry/props.json` is generated from the `wields` lists and the `props` specs in
`data/equipment.json`. It has one entry per distinct prop:

| field | meaning |
|---|---|
| `id`, `name`, `category` | `light_blade`, `blade`, `heavy_blade`, `axe`, `bludgeon`, `polearm`, `thrown`, `bow`, `crossbow`, `firearm`, `implement`, `shield`, `flexible`, ... |
| `grip` | 1 or 2 hands |
| `length_m` | overall length for a Medium wielder |
| `tri_budget` | LOD0 triangles, 500-1500 |
| `grip_socket`, `stow_socket` | default sockets |
| `wielder_sizes` | sizes of the monsters that use it |
| `monster_count`, `used_by` | `{monster_id: copies}` |
| `variants` | stat-block names, `{"Flame Sword": ["fire-giant"]}`. Later these can become separate props (tree club and stone club are visually distinct), with the base prop as the fallback. |
| `reference_prompt` | same shape as the monster prompts, so the house style's strip and suffix rules apply |
| `rig` | only for props that bend (whip: a spline-IK chain) |

SRD 5.2.1: 32 props. Longbow is the most used (10 monsters), then greatsword and longsword
(7 each), then mace, scimitar, shield and spear (6 each).

### Generating props

Props use the same `ref -> mesh -> qa -> lod` pipeline at a small budget:

- ref: house style with a vertical, flat side view and no hand or character. Thin blades may
  need `remesh_resolution` 384 so they do not melt.
- mesh: `tri_budget` 500-1500, texture 512.
- qa: a props mode. Instead of feet-on-ground leveling, put the origin at the grip and orient
  the prop along +Y and -Z as above. The grip point is found by heuristic (the narrowest
  cross-section near the bottom third for bladed weapons, the middle for bows, the arm strap
  for shields), with an override for exceptions.
- lod: same chain, with a lower floor (LOD3 is about 150 tris).

The prop jobs go under `out/props/<id>/`, and `forge.py` will need a `--registry props`
switch. None of this is built yet.
