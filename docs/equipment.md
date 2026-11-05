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
| `hand_r`, `hand_l` | the deform hand bone, renamed at export (Rigify `DEF-hand.R` becomes `hand_r`; SkinTokens: the bone after the forearm, or a short bone added at the wrist when the arm ends there). Its head is at the wrist. | finger/hand VFX and IK targets. Weapons go on `grip_r`. Bows and shields default to `hand_l`. |
| `grip_r`, `grip_l` | non-deforming child of the hand bone at the centre of the fist, **in prop space**: +Y forward at rest (along the blade or shaft), +Z up, so a prop's edge (-Z) hangs down | held props: a `BoneAttachment3D` on `grip_r` needs no offset for blades, hafts and bows (the stave runs through the fist); crossbows, pistols and shields carry a `grip_offset` in their manifest, unverified by any pilot. `hand_r`'s own frame runs wrist-to-fingers, which is not the grip direction |
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
  Centauroids and merfolk/naga are measured over the whole body (the centaur-trooper's
  scale_m 2.4 gave a 4.7 m pike), so their override can set `rider_height_m`, the height of
  the humanoid part as a standing wielder (centaur-trooper 1.8): scale props by
  `rider_height_m / 1.8` when it is set. The viewer and godot-dnd do not read it yet.
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
(7 each), then mace, scimitar, shield and spear (6 each). Prop specs that only
non-SRD monsters use live with that source's curation in the private non-SRD repo
(`docs/body-plans.md`, "Per-source curation layout").

### Generating props

`tools/props.py` runs `ref -> mesh -> qa -> lod` for the entries of `props.json`, using
`tools/forge.py` as a library (ComfyUI plumbing, house style, workflows, resource
sampling, the mesh stage and its Blender decimation). There is no paint stage.

```sh
python3 -I tools/props.py run longsword shield longbow     # all stages, resumable
python3 -I tools/props.py run --all                        # stage by stage over all 32
python3 -I tools/props.py run scimitar --stages qa,lod --force
python3 -I tools/props.py check longsword                  # re-check model.glb's frame
python3 -I tools/props.py contact                          # out/props/_contact.png
```

Output: `out/props/<id>/` with `ref_<i>.png` + `ref.png`, `model_hi.glb`, `model_raw.glb`
(generator frame), `model.glb` (prop frame, LOD0, textures embedded), `model_lod1-3.glb`
+ `lods.json` (no images, they reuse LOD0's material, as for the monsters), `textures/`
(512 px base colour, normal, roughness, metallic, AO), `thumb.png` (4 views with the axes
drawn: green +Y, blue -Z, red +X, magenta grip, orange off-hand), `qa.json` and
`manifest.json` (`ref`, `mesh`, `qa`, `lod` like the monsters, with `resources` = peak
RAM/swap/VRAM per stage, plus `prop`: grip method, every axis decision with its signal and
confidence, `orientation_confidence` = the lowest, `offhand_grip`, `length_check`,
`glb_check` and `grip_offset`). Per-prop overrides: `data/overrides/props/<id>.json`, the
monster fields of `tools/overrides.py` (`ref_pick`, `prompt_extra`, `tri_budget`, ...)
plus `view`, `remesh_resolution`, `flip_tip`, `flip_edge`, `grip_frac`, `roll_deg`,
`pitch_deg` (about +X, positive lifts the -Z end), `tip_rule`, `edge_rule` and
`procedural` (registered by `props.py`). `manifest.prop.source` is `generated` or
`procedural`.

- **ref**: `single object, centered, ` + the registry prompt + the house style. The view
  phrase is replaced per category: elongated props "lying horizontal, pointing right"
  (tested on the longsword: SDXL draws a diagonal blade either way, the horizontal prompt
  reconstructed straighter), shields front on, bows as a bare stave (below). The negative
  adds hands, people, racks, pairs, scabbards, and drops the base negative's `props`.
  `--n 3` candidates are scored on the silhouette (largest blob's share of the foreground,
  -0.15 per extra blob, -0.3 if cut by the frame, -0.25 x |ln(aspect / expected)|) and the
  best is used; scores are in `manifest.ref.scores`.
- **mesh**: `forge.stage_mesh_guarded` unchanged: Pixal3D 512 (`upsample` 0), remesh 256,
  importance-weighted Blender decimation to `tri_budget`, bake at 512. The 1024 cascade
  plus remesh 384 gave no visible gain on the longsword and pushed ComfyUI to its 16 GB
  ceiling. Plain collapse (`--vg-factor 0`) and the weighted one gave the same blade.
  Bows get 1.6 x the budget because QA cuts the string ribbons out afterwards.
  With the GPU decimator (`--decimator comfy`, or the weighted path's fallback when the
  target is under 1.6 x the topology floor) `forge.py` re-runs DecimateMesh up to 5 times,
  bisecting between a target that came out under and one that came out over, and keeps the
  run closest to the budget from below (`manifest.mesh.decimate_kept`), not the last one.
  The first greatclub (a sawn log) gave 189 / 2126 / 182 faces and silently kept a fourth,
  unlogged run of 2765 tris; re-run on the same reference it now ends at 641 of 700.
- **procedural** (override `"procedural": "whip"`): no ref and no GPU; the mesh stage runs
  `tools/blender_prop_procedural.py`, which builds the prop in prop space and writes
  `model_raw.glb` + `procedural.json`; QA takes it as built (`prebuilt`: no PCA or grip
  search, only the scale to `length_m`, export, the axes sheet and the GLB check), then
  the LODs run as usual. The whip: one 8-sided tube, a 0.3 m handle (pommel knob, spiral
  leather wrap, ferrule knot; grip 0.11 m above the butt) and a braided lash tapering from
  13 mm to a 2 mm popper that droops progressively toward -Z (20 deg at the tip, 0.3 m of
  sag), 992 tris, a generated 512 px base colour texture (wrap and braid). `length_m` is the
  extent along +Y; the arc is 2.53 m. The generated whip folded in a U, so its length axis
  was half the whip.
- **qa** (`tools/blender_prop_qa.py`): PCA of area-uniform surface samples gives the long
  axis (e1), the wide axis (e2) and the thin axis (e3). Category rules then decide:

  | category | +Y (tip) | -Z | grip (origin) |
  |---|---|---|---|
  | blades | `thin_end`: the flat end third (thinner along e3) is the blade; the guard (widest bin) votes for the hilt | e2, sign arbitrary for straight blades; scimitar: convex side of the curve | middle of the narrow handle (pommel to guard); two-handed: half a fist above the pommel |
  | axes, glaive, sickle | `head_end`: larger e2 x e3 section | `extension`: the head reaches farther on the blade side of the haft line | 0.06 + 0.04 x L above the butt |
  | warhammer, light hammer | `head_end` | `blunt`: the side whose outer third has more surface (face, not spike) | as axes |
  | maces, clubs, staves, wands, polearms | `head_end` | e2, arbitrary | hafted: as axes; polearms 0.4 L (two-handed 0.28 L, pike 0.22 L), staff 0.35 L |
  | whip | `thick_handle` (opposite) | arbitrary | as axes |
  | bows | the generator's up | `stave_forward`: the grip is in front of the tips | mid-length, centre of the stave |
  | crossbows, pistols | e1 taken level in the generator frame is -Z (prod end: wider span across; pistol: the end without the hanging grip); +Y = generator up | | stock 0.3 L from the butt; pistol and hand crossbow: the grip under the rear |
  | shields | e3 = face normal, toward the reference camera; +Y = generator up | | back surface at the centre |

  Blades and hafted weapons are then tilted about X so +Y follows the handle's own centre
  line, scaled so the length axis (+Y; -Z for crossbows and pistols; the larger face side
  for shields) is `length_m`, and moved so the grip is the origin. Loose islands under 0.5%
  of the area are dropped. Bows keep only the stave: the reference is drawn without a
  string (SDXL's strings cross and float; Pixal3D turns them into ribbons), ribbons fused
  to the stave more than 6 cm behind it are cut, and a straight 4-sided string (3 mm
  radius, 8 tris, material `bowstring`) joins the limb tips, 1 cm inside each. The string
  is its own mesh node `string`, a sibling of the stave node `prop`, in model.glb and in
  every LOD (`blender_lods.py` job key `keep`), so a game can hide it or replace it with
  a drawn string; `manifest.prop.nodes` / `prop.string` record it. The stave alone is
  what the tri budget counts (shortbow 397 of 800, longbow 692 of 800). Two-handed props get an `offhand_grip` empty in model.glb.
  `props.py` then re-reads model.glb with a stdlib GLB reader and checks, in glTF axes:
  the length along the axis (2%), the origin inside the bounds, and a -Z test per category
  (blade: the upper 40% is wider along Z than X; axes: the head reaches farther to -Z;
  bows: the tips are behind the grip in Z; crossbows: the prod end is at -Z; shields: the
  body is in front of the strap).

  Where it fails: a reconstruction whose blade is as thick as its grip (the guard then
  decides, at confidence 0.5); double-bitted axes and symmetric hammers (edge side is
  arbitrary, confidence near 0); a crossbow whose prod is longer than its stock (e1 is the
  prod); a whip drawn coiled; weapons with a big pommel or no visible handle narrowing
  (the grip falls back to 12% of the length); `head_end` on a polearm whose point is
  slimmer than an ornate butt or collar (first fork, glaive) and on a club with a
  sword-like guard (greatclub); a reference drawn tilted (the pistol's barrel climbed
  ~25 deg: `pitch_deg`). Fix those with the overrides above. The per-prop decisions and
  the axes-sheet verdicts are in each `data/overrides/props/<id>.json` `notes` and in
  `docs/TODO.md` section F.
- **lod**: `tools/blender_lods.py`, ratios 0.5 / 0.3 / 0.18 of LOD0, floor 100 tris (LOD3
  ~100-270 tris).

**Attaching in Godot.** The rig's `grip_r` / `grip_l` bones are the fist centre in prop
space, so melee props and bows attach there with no offset (a bow's stave runs through
the fist; the `attack_ranged` clip raises it). Crossbows, pistols and shields turn +90
degrees about X (prop +Y up, prop -Z forward); `manifest.prop.grip_offset` has the
rotation per category, and the viewer applies it (`?prop_r=longsword&prop_l=longbow`,
`viewer/README.md`). Checked on the goblin-warrior (2026-11-04, after the string became
its own node): the longsword's handle sits in the right fist, blade forward; the longbow's
stave runs through the left fist, string behind it. Two-handed props also need the off hand IK on `offhand_grip`; a strapped
(not centre-grip) shield would sit ~0.12 m up the forearm.
