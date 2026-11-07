# Rig and anim stages (2026-11-04)

`forge.py rig` gives LOD0 a skeleton and skin weights and writes the game files.
`forge.py anim` writes clips on that skeleton. Both run after `lod`. The tool research is in
`research-rig-animate.md`, and the per-pilot results are in `TODO.md` ("Rig findings").

```sh
python3 -I tools/forge.py rig wolf                 # skip if the rigged files exist
python3 -I tools/forge.py rig wolf --force         # rebuild the game files from rig/chosen.glb
python3 -I tools/forge.py rig wolf --rig-reroll    # new SkinTokens rolls (--rig-rolls N, default 4)
python3 -I tools/forge.py anim wolf --force        # rebuild every clip in the animation_set
```

## Rig stage

Code: `tools/rig.py` (driver, score weights), `tools/blender_rig.py` (facing check,
roll measurements and pose test, export; headless Blender), `tools/rig_topology.py` (finds
legs, arms, head, tail, wings, spine and the centre line from bone positions).

Part finders that took some tuning (`rig_topology.py`):

- `find_arms`: per side the shortest chain off the upper body that hangs below its root and
  is not a leg. Chains that branch off the head or run through it (hair, ears, a jaw, the
  neck) are not arms, nor is a hanging chain whose tip reaches the ground band (a tail, a
  skirt). A side with no hanging chain takes a raised one (the merfolk's sword arm) if it
  reaches less than `WING_REACH` (0.4) x the height sideways, the reach `find_wings` needs
  for a wing; a side with a hanging arm never looks at raised chains, so the harpy's arms and
  wings are what they were. On the merfolk the left "arm" had been a hair chain off the
  head, which also put `back` on the head bone.
- `find_head` (upright): the highest bone on the centre line, a short one giving way to its
  parent. On the merfolk it takes `bone_5`, which owns the skull and face (3458 vertices,
  z 1.39-1.80 of 1.80); its child above owns only back hair.
- `find_tail(..., ground=True)` (the clips): when no horizontal tail of 4+ bones exists, a
  chain of 2+ bones that leaves the hips backwards and slopes down into the ground band
  (the giant eagle's 2-bone tail fan).
- `find_snout` (horizontal): the bone reaching farthest forward above a third of the
  height, but not a wing: bones more than `SNOUT_SIDE` (0.15) x the height off the centre
  line on a chain that reaches `WING_REACH` x the height sideways are skipped. The giant
  eagle's left wing tip (`bone_11`, 0.6 m ahead of the beak) had been its head, so the
  lunge led with a wing; it is `bone_5` (the beak) now. No other pilot's head changes. For
  avian the clips' neck also starts above the bone the wings hang from (`bone_3`), so a
  head turn no longer swings the wings.

**SkinTokens rolls.** SkinTokens (`tools/skintokens/`, its own uv venv) predicts a
skeleton and skin weights from `model.glb`. It samples without a seed, so every run gives a
different skeleton, and some runs crash (`assert res.skin_pred is not None`). The stage
runs N rolls (default 4, 240 s timeout each) into `rig/roll_<i>.glb`, after waiting for an
idle ComfyUI queue and asking ComfyUI to unload its models: SkinTokens needs about 14 GB of
VRAM. A roll takes 20-30 s. Failed rolls are logged and skipped; the stage fails only when
every roll fails.

**QA auto-yaw** (`blender_qa.py` `auto_yaw`, for plans not drawn front view, with no `yaw`
override; `facing_method` in `qa.json` names the rule). The mesh stays in the reference
camera's frame, which is what the head end relies on:

- **Body axis.** Quadruped and other wingless plans: the principal axis of the surface.
  Winged quadruped and avian (the full principal axis is the wings'): the mirror plane when
  it is decisive (contrast >= 0.55, or >= 0.45 within 20 deg of the camera axis for a head-on
  reference, `mirror_*`); else the median of three principal axes that leave the wings out
  (`body_median_*`): the thick surface (inward ray to the opposite surface > 3% of the mesh
  diagonal; a membrane is thinner), the surface thicker than 2% below 60% of the height, and
  everything below 45% of the height. A low-contrast mirror plane is not used: on dragons it
  reads 30-80 deg off.
- **Head end** (quadruped, winged quadruped, avian, aquatic): the end nearer the camera
  (`*_camera_end`), since a reference shows the face. Within 8 deg of a pure profile the
  camera says nothing and `high_centre_end` decides. `toes` is not ported: no mesh on disk
  has needed the branch but white-dragon-wyrmling. Serpentine and the rest keep the
  upper-mass end and "ambiguous ends: assume it faces the camera".
- **Insectoid** (`insectoid_yaw`, 2026-11-06): the mirror plane at contrast >= 0.42, else
  the principal axis; the camera end, or within 15 deg of a profile the end the legs crowd
  toward. 14 of 16 within 15 deg of the pins (5 before); see `docs/batch.md`.

Validation (2026-11-05, the code above on every model_raw.glb of those plans, 113 meshes;
truth = pins, the review, and top views read by eye to about +-10 deg, so the thresholds
were tuned on the set they are scored on): winged within 15 deg 44 of 48 (27 before), the
rest 61 of 65 (41 before), with no mesh made worse past 15 deg. Misses: griffon 61 (an eagle
on two legs), red-dragon-wyrmling 47 (keeps its pin), pseudodragon 18, weasel 33 (sitting),
giant-toad 17. The old upper-mass end had turned 15 meshes backwards; on all 15 the camera
end lands within 8 deg of the truth, and on the 13 pinned backwards ones within 0.1 of the pin.

This rule shares the mirror plane with the check below on the meshes where it is decisive,
so there the check's axis confirms little by itself; its head-end cues stay independent of
QA, which reads no surface cue outside a pure profile.

**Facing check.** Before rolling, the stage measures which way `model.glb` points
(`blender_rig.py` mode `facing`, about 3 s), because a mesh that does not face +Y in
Blender (glTF -Z) gets a scrambled skeleton:

- **Body axis:** the vertical plane the surface is most mirror-symmetric about (scan over
  180 degrees, refined to 0.5). Paired wings, legs and horns vote for the true axis instead
  of pulling it sideways, which is what fooled QA's footprint PCA on the dragon.
- **Head end:** named by cues, each a sign along the axis with a confidence 0..1
  (`blender_rig.HEAD_CUES`, `rig/facing.json` `cues`):
  - `high_centre_end`: more surface in the top 40% of the height near the centre plane at
    one end (head and neck; wings are outside the centre band). Quadrupeds, winged
    quadrupeds, birds, fish. Weak or wrong for heads-down bodies that carry the rump
    higher (boar, rhino, badger, polar bear).
  - `toes` (quadrupeds, winged quadrupeds, insectoids): per foot (a connected patch of the
    lowest 4% of the surface, not longer than 0.3 x the body, so a tail or slab on the
    ground is skipped), how far the sole sits ahead of the leg above it (8-20% of the
    height). Paws and hooves point forward and hocks bend back; the median over the feet,
    scaled so 0.02 x height is sure, times the share of feet that agree. Not for birds
    (hallux, tail fan) or fins.
  - `head_overhang` (quadrupeds): surface above 30% of the height past the feet at each end,
    each face weighted by how far past. The head, neck and trunk stick out past the forefeet,
    the rump sits over the hind feet, a tail has little surface. Reads elephant 0.86, mammoth
    0.97, polar bear 0.88, boar 0.91 where `high_centre_end` is wrong or weak. Wrong on bodies
    with a big back end: frog, giant-frog, bulette, rust-monster, rat (these go `unverified`).
  - `abdomen_end` (insectoids): a spider's or beetle's high bulk is its abdomen, so the head
    is the other end.
  - `toes_ahead_of_ankles` (upright plans): the soles reach farther forward than the ankles.

  The head end is **known** when two cues agree with no cue >= 0.2 against them, or one
  cue reaches 0.8 with nothing >= 0.5 against it (not for aquatic: a reef shark's tall
  tail fin reads as a head). Otherwise the result is **`unverified`** (no cue, cues too
  weak, cues disagreeing, or one aquatic cue): the angle is the axis alone (sign-blind),
  the stage does not refuse for it, and the batch will not paint the monster until a
  reviewer has looked (docs/batch.md); when one cue named an end, `hint_yaw` is the yaw it
  implies. The stage itself still rolls: the hold is the batch's.
- **Front-view references** (QA `facing_method` `front_view`: an upright or floating plan
  whose reference is drawn front view and that QA turned by 180 with no `yaw` override):
  the face is on +Y by construction, so the sign is not measured: the angle is the mirror
  axis alone, as for a ghost. The toe rule is still recorded; when it says "backwards" the
  report has `cue_disagrees`. Robe hems, cloaks, chains, tails on the ground and splayed
  toes fooled it on 13 tier-1 bipeds that all faced front (archmage, lich, cultist,
  chain-devil, ...: refused at ~180).
- **Confidence:** how much better the best mirror plane is than a typical one,
  `clip((contrast - 0.1) / 0.3)`. A blob, or a curled or twisted pose, scores low. Below
  0.5 the axis means little, so the result is `unverified` however small the angle
  (black-dragon-wyrmling 20.5 deg at 0.09, salamander 76 at 0.28), except for a front-view
  reference, which keeps its prior, and advisory plans.
- **Disputed axis:** a refusal needs confidence >= 0.75 (`rig.FACING_CONF_DISPUTED`, not
  0.5) when a second opinion disputes the mirror axis: a front-view reference (only mesh
  asymmetry can put its axis off: half-dragon's curled tail, 64 deg at 0.5) or a legged
  horizontal body whose ground footprint (elongation >= 1.5) runs 45+ deg off the mirror
  axis (young-bronze-dragon's asymmetric wings, 84 deg at 0.61). Below it: `unverified`.
- Recorded, not a cue: the bearing of QA's `head_region` from the body centre. QA frames
  the +Y end of a horizontal body, so it always points ahead and cannot catch a backwards one.

`manifest.rig.facing` = `{method, angle_deg, confidence, ok, head_known, bearing_deg,
suggested_yaw, unverified?, hint_yaw?, cues, ...}`; the full report is `rig/facing.json`.
When `angle_deg` > 30 with confidence >= 0.5 (0.75 when disputed) the stage logs a warning
and refuses to roll (it raises, `manifest.rig` gets `error` plus `facing`): pin `yaw` (the
log prints `suggested_yaw` = QA's yaw plus the measured bearing; for a backwards body that
is QA's yaw + 180) and re-run `qa`. The `rig_facing_check` override set to `false` rolls
anyway. `--force` without rolling records the check and never
refuses. Plans in `rig.FACING_ADVISORY` never refuse and are never `unverified`.

Tier-1 validation (2026-11-05, 59 horizontal and centaur meshes on disk plus every
upright one, judged against thumb.png; the new head-end code run outside Blender on each
model.glb with the mirror axis from its rig/facing.json, so the angles are the recorded ones): the backwards boar, giant-badger, rhinoceros and
polar-bear (QA's auto yaw, before the pins) are now refused at 177.5-179 with
`suggested_yaw` within 2.5 deg of the yaw the review pinned by eye (boar 128.5 vs 126.5);
all 13 robe / chain / tail false refusals pass; half-dragon and young-bronze-dragon are
`unverified`; the real misfacings (crocodile 66, giant-weasel 30.5, red-dragon-wyrmling
77.5, young-silver-dragon 60.5, salamander 43.5, giant-wolf-spider 62, giant-hyena 90) stay
refused. Still missed: rust-monster (backwards; its insect legs fool `toes` and the
high cue, both say forward). `unverified`: black-bear (cues disagree; the axis is wrong
too), giant-owl, hunter-shark, reef-shark (backwards: the tail fin).

| mesh | angle off +Y | confidence | result |
|---|---|---|---|
| goblin-warrior, harpy, giant-spider, wolf | 0-0.5 | 1.0 | ok |
| owlbear | 4.0 (head end from `toes` since 2026-11-05) | 0.69 | ok |
| young-red-dragon (yaw pinned) | 6.5 | 0.75 | ok |
| young-red-dragon turned 65 degrees (QA's auto-yaw error) | 58.5 | 0.76 | refused |
| owlbear turned -50, wolf 35 / 90, goblin 45, harpy -35, spider 40 | the turn (+ the mesh's own offset) to within 1 | as unturned | refused |
| young-red-dragon_exp_curled (old mesh, 3 scrambled rolls) | 15.0 | 0.38 | warns: asymmetric pose |

The curled dragon does point roughly forward (head at +Y in a top view); its pose is
twisted with the wings spread fore and aft, so its rolls failed for another reason. The
check flags it only as low confidence; the roll score below is what catches its rolls.

**Scoring (v2).** Each roll is measured in Blender (`blender_rig.py` mode `score`, about
1 s) and scored in `rig.score()`: `score = -sum(weighted terms)`, 0 is perfect. The
weights are `rig.WEIGHTS`; one missing structural part costs 0.5, so any one of them
outweighs the small terms, which then rank complete rolls.

| term | weight | measure |
|---|---|---|
| `symmetry` | 5 | joints vs their mirror image across the centre line, / height (v1) |
| `dead_bones` | 0.02 | per bone with no vertex, or shorter than 2% of the height (v1) |
| `legs` | 0.5 | per leg missing or extra vs the plan (`FEET_BY_PLAN`; insectoid 6-10, legs + pedipalps) |
| `leg_pairs` | 0.3 | per foot without a twin on the other side of the mesh's centre line |
| `feet_mirror` | 1 | feet vs their mirror images, / height |
| `spine` | 0.5 | per torso bone short of 3: hip (hind legs' branch) to shoulder (front legs' or arms' branch), `rig_topology.find_spine` |
| `head` | 0.5 | no head (`find_head` upright, `find_snout` horizontal) |
| `tail` | 0.125 | per tail bone short of 4 (`find_tail`, quadruped and winged quadruped) |
| `wings` | 0.5 | per wing side missing (`find_wings`, winged plans) |
| `arms` | 0.5 | per arm side missing (upright plans with hands) |
| `joints` | 0.02 | per joint outside the plan's band (`rig.JOINTS`) |
| `outside` | 2 | share of joints more than 5% of the height outside the surface (joints only: glTF has no bone tails, the importer guesses them) |
| `bleed` | 1 | weight on vertices more than 0.3 x height from their bone, plus side bones' weight on the far side of the centre line |
| `torn` | 10 | pose test: mean share of moving edges stretched past 2x |
| `stretch` | 0.25 | pose test: log2 of the worst edge stretch past 8x |
| `stray` | 2 | pose test: mean share of vertices 0.3 x height from the bent part that moved more than 2% |
| `follow` | 0.5 | pose test, per bend: 1 - median(skin motion / rigid motion) of the part's own skin |

**Pose test.** Every part found is bent by 30 degrees at its root bone, one at a time:
legs, arms and the neck swing about X, the tail about Z, wings flap about Y. The skinned
mesh is compared with rest (edges, far vertices, the part's own skin). It catches a head
bone that moves no skin (owlbear roll 3), legs whose weights drag the belly, torn wing
roots, and an arm skinned to the wing behind it (harpy).

A roll that leaves any vertex unweighted scores -10. Old reports without the quality
terms fall back to the v1 score (`rig.score_v1`, also stored as `score_v1`). The best roll
is copied to `rig/chosen.glb`; when it still has structural `problems` (any of legs,
leg_pairs, spine, head, tail, wings, arms) the stage logs a warning. Every
`rig/roll_<i>.json` holds the measurements, the per-bend pose results, `parts` (bone names
per part), `score`, `score_v1`, `terms` and `problems`; `manifest.rig.rolls` holds them
without the per-bone lists. After finalize the kept `chosen.glb` is measured too
(`rig/chosen.json`, `manifest.rig.quality`), also when it was not rolled now, so a
hand-fixed rig shows its terms. `/api/index.json` carries both as `rig_quality`
(`tools/viewer.py` `rig_quality()`: `facing`, `score`, `score_v1`, `terms`, `problems`,
`joints`, `picked`, and per roll `file`, `score`, `score_v1`, `joints`, `problems`).

**`rig/chosen.glb` is the source of truth.** Once it exists, `rig --force` (and any
`run --force`) only rebuilds the game files from it, for example after a new paint or new
LODs. Only `--rig-reroll` rolls again and replaces it. A skeleton fixed by hand survives
re-runs by being saved as `rig/chosen.glb` (the harpy's was, until the stage learned to
insert wing chains itself). `qa --force` and
`mesh --force` change the geometry and delete `rig/` and `anims/`; `paint` and `lod` delete
only the game files.

**Game files** (mode `finalize`):

- `model_rigged.glb`: the chosen rig with the current albedo (`base_color_painted.png`
  when it exists), sockets added. The viewer loads this file in preference to `model.glb`.
- `model_rigged_lod<k>.glb`: each `model_lod<k>.glb` with weights transferred from LOD0
  (nearest surface point, at most 4 influences, normalised) under the same armature. The
  joint order matches LOD0, so an engine swaps LOD geometry under one skeleton. Like the
  static LODs they carry no images.
- `rig.json` and `manifest.rig`: joints, legs, symmetry, empty bones, `sockets`
  (`{socket: original bone}`), `sockets_missing`, `picked` and the roll scores, seconds.

`model.glb` stays the static source for `paint` and `lod`.

**Sockets.** SkinTokens names every bone `bone_<n>`, so the stage renames or adds the
socket bones the registry's `sockets` field asks for (`docs/equipment.md`):

- `hand_r`, `hand_l` (records with `hands: true`): the hand bone found at the end of each
  arm is renamed. When the arm ends at the forearm, a small non-deforming hand bone is
  added at the forearm's tail.
- `head`: the head bone is renamed.
- `back`: a new non-deforming bone on the spine bone the arms hang from, behind the
  shoulders.
- `hip_l`, `hip_r`: new non-deforming bones on the pelvis at hip height.

Sockets the stage could not place are logged and listed in `manifest.rig.sockets_missing`.

**Standing on the feet** (`blender_rig.ground_feet`, finalize, legged plans except
insectoids). QA grounds the mesh on its lowest vertex. When that is not a foot, so the
lowest vertex the legs own (max weight) is more than `FEET_GAP` (3 cm) up, and the skin
below the feet belongs to chains that leave the body at a branch (not the root or the
trunk), the game files are lowered onto the feet and each such chain is turned up at its
root, about the horizontal axis across it, until its skin clears the ground by `CLEAR`
(0.02) x the height. The pose is baked into LOD0 and the rigged LODs (weighted from LOD0 in
the old rest pose first) and applied as the new rest pose, so no repaint is needed;
`rig/chosen.glb` and its quality score keep the QA pose. Recorded in `rig.json` and
`manifest.rig.grounded` (`shift_m`, `turned_deg` per chain, `feet_low_m`, `min_z_m`).
Giant eagle: lowered 0.355 m, tail fan `bone_28` turned 33.9 degrees and the left feather
lobe `bone_30` 3.7; feet at z = 0, every LOD's lowest vertex within 1.4 cm of it, height
4.16 -> 3.81 m. The other pilots' feet already stand on the ground (the harpy's 4 cm gap
is hip feathers skinned to the root, which cannot be turned, so it is left as it is).

**`rig_reweight` override** (`tools/rig_reweight.py`, run by `blender_rig.py` finalize before
the sockets): a list of chains, each `{bones: [root..tip], reach, bias, blend, strip, fit,
hand_room, far}`. Vertices within `reach` of the chain and nearer to it (x `bias`) than to
any other bone go to their two nearest chain bones (a `blend` band keeps some old weight);
`strip` removes the chain's weights elsewhere; `far` drops weights of bones farther than
that from the vertex in and around the region (bleed across the body); `fit` first moves
the chain's joints to the centroid of the limb's cross-section at each end, and
`hand_room` stops the tip bone that far short of the limb's end so the 4%-height hand bone
`add_sockets` adds there spans the fist. Only the game files change: `rig/chosen.glb` and
its quality score keep SkinTokens' weights, and every `rig --force` re-applies it. The
harpy uses `[{bones: [bone_18, bone_19], reach 0.15, fit, hand_room 0.068}]`: SkinTokens
skinned its right arm to the wing chains (the left wing's bone_12/bone_13 included) and put
bone_18/bone_19's weights on the right thigh, and the arm bones ran along the outside of the
arm. After: `hand_r`/`grip_r` 1.3/0.2 cm from the skin (5.5/8.0 before), 3.3/3.0 cm from the
fist centre (10/12 before); bone_18/bone_19 own 197/108 vertices (0/0); edges stretched
past 4x over the sword cut 175 -> 77, over `fly` 211 -> 223 (right side 129 -> 139, the arm
now stays at the side while the wing beats; it used to fly up into the wing). The left chain
(`bone_8`, `bone_9`, the mirror) got the same entry with `far` 0.5 (2026-11-04, Godot round 5:
`grip_l` 11.2 cm off the skin): `grip_l`/`hand_l` 11.5/8.2 -> 2.7/2.4 cm from the skin,
bone_8/bone_9 own 299/120 vertices (148/106); edges past 4x over `fly` 223 -> 217 (left
84 -> 78), over the sword cut 77 -> 85 (left 7 -> 15). With `far` 0.3, as on the right, the
left wing-arm seam tore more in `fly` (246, left 107).

**Wing chains from the mesh** (`tools/blender_wing_weights.py`, 2026-11-04). On a winged
plan (winged_biped, winged_quadruped, avian but `custom.theropod`: `rig.adds_wings`) whose
skeleton has fewer than 2 wing chains (`find_wings`, legs found with the plan's foot count as
the clips find them), finalize finds the missing wings on the rest mesh and inserts them before
the `rig_reweight` entries and the sockets; the chosen-rig score runs the same step, so
`rig/chosen.json` measures the game skeleton (`rig/chosen.glb` itself is unchanged, like the
reweight). The rule:

- Body axis: the skeleton's centre line (`centre_x`). Shoulder height: the first ancestor of
  the head bone that branches (`shoulder_height`).
- Lobe: the vertices more than `MARGIN` (0.15) x the height from the centre line on one side,
  minus the body band (closer than `BAND` 0.3 x the height and lower than 0.05 x the height
  below the shoulders: arms, hips, a tail fan's root), cut into pieces by proximity (voxels of
  0.02 x the height, 26-neighbours, so loose feather sheets join). Per side the piece that
  reaches farthest is a wing when it reaches `WING_REACH` (0.4) x the height, holds 2% of the
  vertices and touches the inner rim (within 0.05 x the height of the margin). Loose pieces
  within 0.08 x the height of it join it for the weights.
- Chain: root = centroid of the inner rim above the shoulders (where the lobe meets the back);
  tip = geodesic far end (over the voxel graph, from the root) of the lobe's part past 0.75 of
  its reach, so the chain runs out to the span and down a hanging wing; 3 joints between at the
  centroids of the geodesic level sets near the root-tip path. 4 bones, `wing_<side>_00..03`,
  on the trunk bone nearest the root.
- Weights: a `rig_reweight` entry per chain ({bones, reach 0.3, bias 1.0, blend 0.1, far 0.15,
  all x the height but bias}) plus the lobe's vertex indices: those join the chain with bias
  0.5 and no reach limit (feathers past the tip), and the side's outer vertices lose weights of
  bones farther than `far` (the harpy's ground-level feather tips were 0.99 on the forearm and
  shot across the body in the sword cut). The body keeps its weights.
- A SkinTokens chain that falls short of `find_wings`' reach but owns half of a lobe's vertices
  (one-sided, not a leg, arm or central head) is renamed `wing_<side>_00..` instead of getting a
  second chain beside it (eagle roll_1: 0.39 x the height, both sides adopted, score -5.27 ->
  -4.27).
- `rig_topology`: `wing_*` chains are wings by name (`find_wings` takes them whatever they
  reach) and never feet, arms or the snout; a chain that branches off an arm bone may start
  farther than 0.2 x the height from the centre line. The finders give the same parts as before
  on every kept skeleton (14 chosen rigs in `out/`, the eagle's backup, and the harpy, eagle,
  dragon and goblin rolls).
- Seam (`seam_blend`, 2026-11-04, sides with both an inserted chain and an arm, so winged
  bipeds): SkinTokens skins by proximity, and the wing membrane behind an arm is near the
  forearm in space but far from it over the surface. On harpy roll_1 the torn feathers lay
  0.23 m behind the forearm in a straight line but 0.93 m from it around the shoulder, and
  they held 0.5-0.95 forearm weight, mixed with `wing_r_01`/`02` (the lobe's body band, below
  the shoulders within 0.3 x the height, leaves them out of the lobe). So arm vs wing
  ownership follows the welded surface (glTF's UV-seam duplicates welded at 1e-5 m): `g_arm`
  = geodesic distance from the arm core (skin within `core` 0.04 x the height of the arm's
  joint path shoulder -> elbow -> wrist -> hand tip, not of its bones: roll_1's upper arm
  bone runs out along the wing's leading edge), `g_wing` from the wing core (vertices the
  wing chain owns, farther than `wing_core` 0.3 x the height from the arm core). The arm's
  share of a vertex's arm + wing weight is 1 within `hold` (0.03 x the height) of the arm core,
  else `g_wing / (g_arm - hold + g_wing)`, a smooth falloff across the boundary, and it only
  moves one way: arm -> wing where the arm holds more (a vertex with wing weight, or an
  arm-only vertex nearer the wing over the surface), wing -> arm where the wing holds more (a
  vertex with arm weight, or the arm skin). Pure wing vertices never gain arm weight, other
  bones keep their share, the arm skin keeps all of its arm weight (median 1.0) and lobe
  vertices stay on the wing. Harpy roll_1: right 824 vertices arm -> wing, 307 wing -> arm;
  left 213 / 265. The chain's parent was not the cause: the inserted chains already hang from
  the chest (`bone_3`), which the cut only turns with the torso. The root stays where the lobe
  meets the body above the shoulders (roll_1: on top of the shoulder, y 0.06 m, not the back).
- Body seam (`body_seam`, 2026-11-04, after the arm-wing seam, same sides): the same surface
  rule between the wing and the body and between the arm and the body. The euclidean wing
  region and SkinTokens' proximity had mixed them up on harpy roll_1: the long feathers that
  hang from the hips beside the thighs (0.5-0.9 m from a leg over the surface, 1.0-1.4 m from
  the wing) held pure `wing_*_03` weight where they came within the chain's reach and
  thigh/hip weight next to it (77 of fly's 96 torn edges: `wing_*_03` against `bone_0`/`14`/
  `18`/`20`), a wing feather on the floor held 1.0 ankle (`bone_20`, 2.2 m from the leg over
  the surface), the back on the centre line held 0.5-0.8 of the right upper arm (`bone_11`)
  and the waist 0.33 of the forearm hanging beside it (`bone_12`): the sword cut's raised arm
  pulled those into the armpit sheet. Body core = skin within `trunk` (0.04 x the height) of
  the centre line, head to tail, plus skin within `core` of each leg's joint path; `g_body`
  the geodesic distance from it. Wing: `g_wing` from the lobe vertices the chain owns farther
  than `wing_core` from the body core; the body's share of wing + body weight is 1 within
  `hold`, else falls from 1 to 0 across `band` (0.1 x the height) around `g_body = g_wing`;
  wing weight past it goes to the vertex's body bones (its pre-wing weights if it has none
  left), body weight past it goes to the wing on vertices with wing weight or the side's
  outer vertices nearer the wing. Arm: the arm's share of arm + body weight falls the same way
  around `g_arm - hold = g_body` (arm core as the seam's); only arm -> body. Arm vs wing stays
  `seam_blend`'s. Harpy roll_1: right 913 vertices wing -> body, 32 body -> wing, 1242 arm ->
  body; left 346 / 55 / 403.
- Recorded in `rig.json` (`wings_added`: lobes, chains, weights, `seam`, `body_seam`) and
  `manifest.rig.wings_added` (bones, parent, adopted, lobe numbers, owned vertices, `seam` per
  side: arm bones, core sizes, vertices moved each way, the three distances in metres;
  `body_seam`: body core size, `trunk`/`band` in metres, per side the wing core and the
  vertices moved wing -> body, body -> wing, arm -> body); the stage logs the chains, or a
  warning when a side has no lobe.

Lobes (reach / vertices / root and tip height, x the height): harpy (every roll) right 0.50 /
2810 / 0.80 -> 0.29, left 0.52 / 2969 / 0.83 -> 0.37; dragon 0.63 / 2807 and 0.67 / 1604 (both
wings present: nothing added); eagle 0.45 / 2226 and 0.43 / 1937 (present: nothing added; its
tail fan, 616 vertices below 0.23, is not taken); goblin (not winged, never run) would show a
0.43-reach left piece of 772 vertices, so the plan gate matters. Harpy roll_1 (best SkinTokens
roll, -1.54, no wings) with the inserted chains, against the hand-built rig (edges past 4x over
the clip, every 3rd frame, right/left; sockets: distance outside the skin):

| | hand-built (until 2026-11-04) | roll_1 + inserted wings | + seam | + body seam (`out/` now) |
|---|---|---|---|---|
| wings found (`find_wings`, clips) | yes | yes | yes | yes |
| `fly` | 217 (139/78) | 126 (79/47) | 96 (58/38) | 4 (2/2) |
| `attack_melee` (sword cut) | 85 (70/15) | 172 (132/40) | 48 (33/15) | 14 (14/0) |
| `idle` / `walk` | 0 / 5 | 3 / 13 | 1 / 5 | 0 / 0 |
| `hit` / `death` | | | 28 / 40 | 0 / 4 |
| `hand_r`/`grip_r`/`hand_l`/`grip_l` | 0/0/0.3/0 cm | 0/0.5/0.8/0 cm | 0/0.5/0.8/0 cm | 0/0.5/0.8/0 cm |
| score (chosen.json) | -3.47 (wing tips counted as legs; -1.47 without) | -1.09, no problems | -0.84, no problems | -0.42, no problems |

Edges past 2x with the body seam: fly 183 -> 73, sword cut 232 -> 125, idle 25 -> 0, walk
57 -> 11.

Before the seam the sword cut tore at the right arm-wing seam (forearm `bone_12` vs
`wing_r_00`/`01`, 64 of 172 edges, plus 61 at the upper arm once the forearm was fixed):
roll_1 hangs the membrane behind the arm on the arm, and this roll's cut raises the hand above
the head, so the feathers went up with it (13 arm re-weights in scratch did not help; the
harpy's `rig_reweight` arm entries, mapped onto roll_1, made it worse, 274-310). With the seam
the harpy's `rig/chosen.glb` is roll_1 (2026-11-04, `rig --force` + `anim --force`; clip frames
unchanged: idle 75, walk 33, fly 36, attack_melee 30, hit 15, death 54) and its override has
no `rig_reweight` (the hand-built rig's entries named roll_1's right leg and left forearm).
The body seam (2026-11-04, `rig --force` + `anim --force`, clip frames unchanged) fixed what
`fly` still tore after the seam (the hip feathers beside the legs, above) and the sword cut's
armpit sheet. Left in the cut: 14 edges on top of the right shoulder, where the wing root
sits (`wing_r_00` head at y 0.06 m, beside the shoulder joint) and the raised upper arm
(`bone_11`) pulls the skin between them; rooting the chain 6 cm farther back (the rim's half
behind its median depth) gave 11 and looked the same, so it was not kept.

**Tail chains from the mesh** (`tools/blender_tail_chain.py`, 2026-11-05). On a plan in
`rig.TAIL_ADDED` (quadruped, winged_quadruped, centauroid: the legged plans whose clips sway a
tail, avians left out) whose skeleton has a `find_tail` of fewer than 4 bones, finalize (and the
chosen-rig score, after the wings) finds the tail on the rest mesh and inserts a chain, or adopts
and extends a shorter one. Recorded as `tail_added` in `rig.json` and `manifest.rig` (bones,
parent, adopted, the profile's numbers, weight counts); a gated plan with no tail found gets
`tail_added` with `found: false` and a `reason` and a warning in the log. It is on by default for
those plans (`rig.adds_tail`; override `rig_tail_chain: false` turns it off for one monster) since
the root and seam rules below (2026-11-05, later) stopped the owlbear's tearing; before them it was
opt-in. The rule:

- Body axis: the skeleton's centre line (`centre_x`); hips: the mean first joint of the hind
  legs (legs behind the feet's mean).
- Rear profile (side view): surface samples (vertices plus points on every triangle 0.01 x the
  height apart: the centaur's tail sheet is a few big triangles) behind the hips and within
  `SIDE` 0.2 x the height of the centre line, cut into horizontal bands of `BAND` 0.04 x the
  height from 0.2 x the height above the hips down to the floor. Per band the rear cluster is
  the samples within `REAR` 0.1 x the height of the band's rearmost point, split at x gaps wider
  than `GAP` 0.06 x the height; the piece kept is the one with the rearmost point at the root,
  then the one nearest the band above (the centaur's tail hangs beside a hind leg near the floor).
- Root: where the lobe leaves the rump: the first band from the top whose rear cluster is one
  piece, lies `REAR_MIN` 0.1 x the height behind the hips, is narrower than `ROOT_W` 0.6 x the
  body's width at the hips, and is at or below hip height (no lower than 0.2 x the height below
  it). Rooted 0.2 x the height above the hips (the old rule), the owlbear's lobe started on the
  upper back (0.41 m above the hips) and took the haunches with it. The tail runs down while
  the cluster stays behind the hips by that much and moves less than `JUMP` 0.08 x the height
  (forward, or sideways) from the band above; legs, under the hips, never are. A tail may reach
  the floor (the centaur's does). At least 3 bands, and thin: the clusters' median width at most
  0.75 x the body's width at the hips.
- Chain: the band centroids root to tip (the last band's sample farthest from the root), resampled
  to equal arc lengths, one bone per 0.12 x the height of tail, 4 to 6 bones, `tail_00..`, on the
  hip bone (`find_spine`'s hip, the hind legs' common ancestor). The lobe for the weights is the
  tail bands' rear clusters.
- Adoption: a SkinTokens chain (off a branch, not a leg, the torso or the neck) with a joint within
  0.08 x the height of the tail line whose bones own half the lobe's vertices (dominant weight) is
  the tail; only its bones whose head is at or below the root band's top are renamed `tail_00..`
  (the ones above stay on the back, the chain's parent) and extended from the last joint along the
  line to the tip with inserted bones, at least 4 in all (owlbear: of `bone_11..13`, `bone_11`
  starts 0.07 x the height above the root and stays; `bone_12..13` + 2 inserted). The wolf's
  rump stub `bone_27` (owns the whole tail, but its joint is at the hip, 0.18 x the height off the
  line) is not taken.
- Weights: the tail share t rises from 0 at the root to 1 over `BASE` 0.06 x the height of arc
  length along the tail line (the base blends into the rump), across the lobe's border from 0 at
  `EDGE` 0.05 x the height outside to 1 at `EDGE` inside over the welded surface (a hard border
  tore where the centaur's tail sheet is fused to the back of its left hind leg), and times the
  leg seam s; t goes to the two nearest chain bones by inverse distance, 1 - t keeps the vertex's
  old weights.
- Leg seam (`leg_seam`, the arm-wing seam's idea from `blender_wing_weights`): g_leg is the
  geodesic distance over the welded surface from the leg core (the skin within `SEAM_CORE` 0.04 x
  the height of any leg's joint path, hip -> knee -> hock -> toe, or the skin nearest each segment
  when the limb is bulkier), g_tail from the tail core (the lobe's skin within `SEAM_CORE` of the
  tail line). s = 0 within `SEAM_HOLD` 0.03 x the height of the leg core, else a smooth step from
  0 to 1 as g_leg - hold - g_tail goes from -`SEAM_BAND` to +`SEAM_BAND` (0.04 x the height). The
  legs' share of a lobe vertex is scaled by 1 - s x the border blend and what they drop goes to
  the vertex's other old bones (the hip bone when none are left): on the tail's side of the
  surface the legs let go, beside a haunch or where a tail is fused to a hock they keep it. The
  lobe is not cut above the hocks: the seam says where the leg's skin is (owlbear: 3 of 638 lobe
  vertices on the legs' side; centaur: 29 of 136, its tail sheet on the left hind leg). The old
  rule stripped the legs from every lobe vertex with t > 1/2 (owlbear: 55.6 of weight off 859
  vertices), so the border with each hind leg tore whenever the legs moved.
- Adopted weights: an adopted chain's own SkinTokens weights outside the tail would move with
  the tail now (the owlbear's `bone_13` skinned feathers beside the left hind shin, next to
  shin-skinned ones: 7 edges past 4x in the walk, 11 in the death). Every vertex with adopted
  weight is rewritten: that share takes the old (non-tail) weights of the surface-nearest vertex
  the chain does not rule (adopted share <= 1/2), then the tail share is laid over as above.
- `rig_topology`: `tail_*` chains are the tail by name (`find_tail` returns `tail_00`'s chain
  whatever its shape) and never feet or arms (`find_legs`, `find_arms`), so a tail hanging into
  the ground band is not a fifth leg.

The detector alone is not selective: on the goblin (biped) it reads a "tail" off the back of the
thighs, on the harpy off its hip feathers, on the eagle the tail fan (root 0.47, tip on the floor).
The plan gate is what keeps them out: bipeds, winged bipeds and avians never run it (the eagle's
fan stays `find_tail(ground=True)`'s and `ground_feet`'s), and the dragon has 9 SkinTokens tail
bones (the detector would adopt `bone_60..66`, share 0.65, but never runs). `rig --force` in
scratch copies of the goblin, dragon, harpy and eagle: `rig.json`, `rig/chosen.json`, sockets and
scores identical.

Rear profiles (x the height; bands, root and tip height, root and tip behind the hips, median
width vs the body's, lobe vertices; the first three with the root rule, the old root before the
arrow where it moved; hips at 0.56 (centaur), 0.69 (wolf), 0.66 (owlbear)):

| | bands | root -> tip z | behind root / tip | width / body | lobe | chain |
|---|---|---|---|---|---|---|
| centaur-trooper | 18 -> 13 | (0.66) 0.47 -> 0.00 | 0.15 / 0.09 | 0.12 / 0.32 | 349 -> 136 | 6 -> 4 inserted on `bone_15` |
| wolf | 17 -> 12 | (0.87) 0.67 -> 0.23 | 0.28 / 0.24 | 0.20 / 0.38 | 726 -> 330 | 6 -> 4 inserted on `bone_17` |
| owlbear | 19 -> 14 | (0.83) 0.63 -> 0.13 | 0.42 / 0.55 | 0.31 / 0.79 | 859 -> 638 | `bone_12..13` adopted + 2 on `bone_11` (was `bone_11..13` + 2) |
| young-red-dragon | 7 | 0.33 -> 0.12 | 0.11 / 0.34 | 0.13 / 0.44 | 376 | not run (9 bones) |
| giant-eagle | 13 | 0.47 -> 0.00 | 0.10 / 0.27 | 0.32 / 0.67 | 3277 | not run (avian) |
| harpy | 16 | 0.65 -> 0.05 | 0.22 / 0.32 | 0.36 / 0.97 | 985 | not run (winged biped) |

Results of the first version (old root, legs stripped from the whole lobe), with the clips of
the time (scratch copies, `rig --force` + `anim --force`, clip frames unchanged; edges past 4x
over the clip every 3rd frame, before -> after); the current numbers are in "Hanging tails in the
clips" below:

| | walk | death | idle | score (chosen.json) |
|---|---|---|---|---|
| centaur-trooper | 203 -> 272 | 129 -> 219 | 21 -> 30 | -3.19 -> -3.79 (`follow` on the left hind "leg", whose bones lie in the tail) |
| wolf | 0 -> 0 | 3 -> 292 | 0 -> 0 | -2.06 -> -1.37, `tail` problem gone |
| owlbear | 1 -> 22 | 1 -> 484 | 0 -> 0 | -3.11 -> -3.26, `tail` problem gone |

The tail is skinned and found by the clips on all three (`anim.parts.tail` = the chain), and the
centaur's no longer kicks with the hind hoof. Those numbers are with the clips of the time, which
drove every tail as a level one; `blender_clips.py` now handles hanging tails (below).

**Hanging tails in the clips** (`blender_clips.py`, 2026-11-05). Per clip build, a tail (not a
ground fan) *hangs* when its root drops more to its tip than it reaches out horizontally
(`HANG`: root head z - last bone's tail z > their horizontal distance). Wolf (drop 0.69 m, reach
0.09), owlbear (1.61 / 0.79) and centaur (1.59 / 0.20) hang; the dragon's tail and the eagle's fan
do not, so their clips are byte-for-byte the old ones. For a hanging tail:

- Sway (walk, idle, swim; `tail_sway`): a pendulum swing side to side, about `TAIL_SWAY_AXIS` =
  (root -> tip) x `TAIL_AXIS`, the line across the tail in its plane (about Y for a straight-down
  tail). About Z, the level tail's axis, it was a twist along the chain. The idle's bound
  (`tail_idle_amps`) measures the lever in 3D (a hanging tail's horizontal lever is ~0).
- Floor lift: about `TAIL_AXIS` = (root -> tip) x -Y, the normal of the plane through the tail
  line and the body's long axis (about -X; + lifts the end backwards). (root -> tip,
  horizontal) x Z swung the centaur's tail, whose tip hangs 0.18 m to the side and 0.08 m behind,
  30 degrees sideways. `TAIL_REACH` is the chain's arc length (the horizontal reach, 0.24 m of a
  1.6 m tail, lifted it 40 degrees), and the step is `hang_lift`: the end sits `TAIL_REACH` x
  cos(slant + lift) below the root, so the turn that raises it dz is solved exactly (the level
  tail's asin step left the centaur's tail 4 cm under). The end is let down to `TAIL_DIP`
  (`DIP_MAX` x 0.8 since clips7, below; it was `DIP_MAX`) into the ground, not lifted clear: the centaur's lower tail is fused to its left hind leg and every
  degree pulls the sheet off it (walk edges past 4x with the end at -0.03 m: 114; -0.025 m: 237;
  clear of the ground: 250; no lift at all: 110 with the end 10 cm under). Centaur turns: idle 9,
  walk 15, attack 15, hit 11 degrees; lowest vertex -0.030 m (was -0.006 with the old rig).
- Attack, hit, fly, cast curls (`tail_curl`): about `TAIL_AXIS` (+ lifts it backwards); about X
  a hanging tail swung forward into the hind legs.
- Death: no counter-turn (turned back against the roll it swung down through the floor, wolf
  death 292 edges past 4x); since clips7 it goes limp toward the ground as the body rolls
  (`death_drape`, "Round 10 clip fixes" below).
- The walk report has `tail_hangs: true`.

Results in `out/` (`rig --force` + `anim --force`, the tail chain on by default; edges past 4x
over the clip every 3rd frame; no tail = the old rigs, first = the first tail rules with these
clips (old root, legs stripped from the whole lobe), now = the root and seam rules; backups in
`~/.cache/monster-forge-scratch/tails3/backup`):

| | idle | walk | attack_melee | hit | death | chain in `out/` |
|---|---|---|---|---|---|---|
| wolf | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 3 / 3 / 3 | 4 inserted on `bone_17` (was 6) |
| centaur-trooper | 21 / 21 / 21 | 203 / 114 / 100 | 525 / 525 / 530 | 37 / 45 / 43 | 129 / 56 / 38 | 4 inserted on `bone_15` (was 6) |
| owlbear | 0 / 0 / 0 | 1 / 33 / 2 | 1 / 16 / 2 | 1 / 2 / 2 | 1 / 45 / 5 | `bone_12..13` adopted + 2 on `bone_11` |

Clip and frame counts are unchanged on all three; the tail sways in the walk and idle on all
three (owlbear: 4 degrees per bone in the walk, up to 5 at the tip in the idle). The owlbear's
first-rules tearing was the rig's weights, not the clips (with every hanging-tail turn at 0 its
walk still tore 22 edges and its death 45): the adopted chain ran from the upper back (0.41 m
above the hips) down the rear fan and the legs' share was stripped from all 859 lobe vertices,
so the fan's border with each hind leg tore whenever the legs moved. Each rule's share (scratch):
the seam and the adopted-weights rule with the old root still tore walk 11 / death 27; the new
root with the seam but SkinTokens' `bone_13` weights left beside the left hind shin 7 / 11; all
three 2 / 5. What the owlbear's clips still tear: one edge on the right hind shin (`bone_19`,
as with the old rig) and one on the left (`bone_14`), and in the death 5 edges where the fan's
tip meets the left hind foot (`tail_03` / `bone_16`). The wolf's and centaur's tails are shorter
now (rooted at hip height, the top of the wolf's tail rides the rump); the centaur's leg sheet
(`bone_0`, `bone_22`/`bone_33`) is what still tears in its walk, attack and death.

A successful `rig` or `anim` run drops that stage's entries from `manifest.errors` (which
forge.py appends on every failure), so a fixed monster no longer reads as broken.
The viewer's `sockets` toggle draws each socket bone with its axes, and `prop` hangs a
placeholder sword on `hand_r` and a bow on `hand_l` (`viewer/README.md`).

### Known limits

- Roll scores on the rolls kept on disk (v1 -> v2, picked first): goblin roll_1 -0.05 ->
  -0.38 (rolls 2-3 lose 0.8 for a loincloth chain taken for a third leg); dragon roll_1
  -0.49 -> -0.71 over roll_2 -0.78 (v1 would have taken roll_2), roll_4 -4.0 (one wing,
  1/3 legs, torn); owlbear roll_1 -3.1 over the kept roll_3 -4.0 (no roll has a spine;
  roll_3's head bone moves no skin); harpy roll_1 -1.5 (no SkinTokens roll has wings);
  the curled dragon's scrambled rolls -3.1 to -7.1. The retired hand-built harpy rig
  scored -3.5 (its wing tips hung near the ground and counted as legs); roll_1 with
  inserted wings and the seam blend scores -0.84 with no problems.
- `find_legs` rejects a foot that fans into several toes from an ankle above 9% of the
  height (owlbear roll_2, which is also the only owlbear roll with a back chain to the
  hind legs), so such a roll loses 1.0 for "missing" legs, and the anim stage would fail
  on it too.
- No weights heatmap thumb yet; the overlay sheets used to check the score (mesh + bones
  coloured by part, front/side/top) were made by a scratch script.
- **Facing matters.** A mesh that does not face -Z gets a scrambled skeleton. The young red
  dragon went from scrambled to clean once its `yaw` was pinned. The facing check above
  catches a turned body; it cannot tell a twisted pose from a turned one (low confidence,
  warning only).
- No wing bones on the harpy in 4 of 4 rolls. The stage inserts wing chains found on the
  mesh and blends the arm-wing seam (above); that rig replaced the hand-built one on
  2026-11-04; with the body seam (above) fly tears 4 stretched edges (hand-built: 217) and
  the sword cut 14 (85). What remains is a few edges on top of the right shoulder between
  the raised upper arm and the wing root.
- The owlbear has no spine (its legs hang off a back root). SkinTokens gave the wolf no
  tail chain and the owlbear a 3-bone one; the rig stage now inserts or extends a tail chain
  from the mesh by default for quadruped, winged_quadruped and centauroid ("Tail chains from
  the mesh"; `rig_tail_chain: false` turns it off), and the wolf, owlbear and centaur in
  `out/` all carry 4-bone `tail_00..03` chains. Checked on those three pilots only.
- Added hand bones point straight up in the bind pose, not along the forearm, so a prop
  attached with no offset stands upright from the wrist (the harpy's `hand_r` and `hand_l`
  in the viewer). Renamed hand bones keep SkinTokens' orientation. The prop-space
  convention in `docs/equipment.md` assumes +Y along the closed fist; a grip pose and a
  hand-aligned socket are still to do.
- Loose mesh parts weight fine; manifold cleanup is not needed before rigging.

## Anim stage

Code: `tools/anim.py` (driver), `tools/blender_clips.py` (the gait, headless Blender).

The stage builds every clip in the monster's `animation_set` that has a procedural
builder, from the skeleton alone (`rig_topology`: legs, arms, snout, neck/spine, tail,
wings, fangs). It needs `model_rigged.glb`. The shared framework: foot IK on the foot bone
(planted cycles, feet held level), chest-parented arm IK for the sword cut and bow draw,
FK layers about armature axes, and a per-frame measurement of the skinned mesh that lowers
or raises the body so every vertex stays within 3 cm of the ground and standing feet reach
their targets (a stride or lunge is rebuilt smaller when they cannot: `stride_scale`,
`body_scale` in the report). Clips: `idle`, `walk`, `attack_melee` (biped sword cut over the
right shoulder, quadruped lunge and bite, spider front-leg strike), `attack_ranged` (bow
draw), `hit`, `death` (bipeds topple backward, quadrupeds roll onto the right side, spider
legs fold; the last frame lies on the ground and holds), `fly` (hover with wing flap, or a
levitation without wings), `breath`, `cast`, `swim`, `burrow` (below). Loops start and end
on the same pose; all clips start at t=0 and stay in place.

Output: `anims/<clip>.glb` (armature only, one clip per file, 30 fps), `anims/<clip>.json`
and `anims/clips.json` (reports), `manifest.anim` (`clips`, `missing`, per-clip report,
`parts`, seconds). All six pilots are `anim_complete`; the stage takes 5-11 s per monster.
`viewer/README.md` ("Clips the anim stage writes", "Animations in the page") has the file
convention the viewer and the index follow.

A tail that lies or rests on the ground is turned up at its root in the floor clips
(`tail_raise_max_deg` in the report) instead of the body being raised off it. A tail fan
found by `find_tail(ground=True)` droops below its root, so a turn lifts it unevenly and
overshoots: for such a fan the settle passes also ease the turn back down while it floats
more than `FLOOR_TOL`, the last pass only lifts. Giant eagle walk (a scratch job; its record
has no `walk`): lowest vertex -0.085 -> +0.001 m, body raise 0.092 -> 0, stride 0.5 -> 0.8,
fan turned up to 25.7 degrees; its idle, lunge and hit no longer lift the body either (the
lunge runs at full size, was 0.8). Since the rig stage stands the eagle on its feet (above)
its fan rests 8 cm above the ground and the floor clips barely turn it (0-6 degrees); idle,
lunge, hit and death stay within 1.8 cm of the ground, fly hovers at 0.32 m as before.

**Upright tails** (2026-11-07, the tarrasque). Upright plans have no tail test (it matches an
arm) and `rig.TAIL_ADDED` leaves them out, so the tarrasque (biped, a 15 m kaiju) stood with a
rigid tail: SkinTokens' 7-bone chain `bone_41..47` follows it (rump, back and out to the right,
up the right side, the last bone's skin welded to the right shoulder and upper arm by 43 edges),
but nothing drove it, and the backward-falling death lay 7 m up on it and on the floor slabs
(below). The rear-profile detector cannot read a tail that curls sideways, so:

- Rig: override `rig_tail_chain: {"adopt": "bone_41"}` (any plan; `blender_tail_chain.adopt_named`)
  renames the longest chain from that bone `tail_00..` and keeps its weights (the vertex groups
  are renamed with the bones); reported as `tail_added` (`adopted.named`). It replaces the
  mesh detector for that monster. `ground_feet`'s turn of the chain is kept (`tail_00` 36 deg).
- Clips: an upright rig with a `tail_00` chain (`UPRIGHT_TAIL`) gets the heavy-tail layers;
  every other upright rig keeps `tail = []` and builds byte-for-byte as before. When the tail's
  last two bones share `PIN_EDGES` (10) or more mesh edges with the core skin (`tail_pin`), the
  last `len // 2 + 1` bones are held by an IK pin riding on the nearest torso bone (`bone_1`),
  and only the free bones (`tail_00..02`) are turned, sized by the far end's travel x body
  length (`HEAVY_TIP`, at most `HEAVY_MOST` 8 deg a bone, a `HEAVY_LAG` wave down the chain):
  idle a slow drift and a lift with the breath; walk a counterweight swing per stride plus a dip
  on each footfall; attack a wind-up against the torso's turn and a late lash across; hit a flick
  up and aside. Death: the free bones turn flat with the topple (`collapse_turns`: a grid over
  root X / Z and a per-bone X, on the skin, for the lying heights nearest the ground line) and
  the lying body rests on its core (`core_mask`: root, spine, neck, head, legs, arms), so what
  still reaches below the ground goes into it instead of raising the body.
- Tarrasque, scratch copy (`rig --force` + `anim --force`): tail joints travel 0.45 / 1.6 / 1.2 /
  0.95 m (idle / walk / attack / hit; 0 before); edges past 4x touching the tail's skin 0 / 0 /
  29 / 0 / 3 (idle / walk / attack / hit / death; before 0 / 0 / 31 / 0 / 1; the attack's are the
  right arm pulling the welded tail end, as before). Death: body raise 6.96 -> 4.28 m (the fall's
  own pivot), lying on the back with the chest on the ground (was 2.7 m up on the slabs). Turning
  the loose chain `bone_48..51` too tore it off the tail's base (69 edges past 4x), so it is not.
  The audit now flags death `sink` / `death_floor` (the slabs, 2.7 m under; tail base 1.3 m) where
  it flagged `death_top` (floating). Archmage, ape, wolf, boar, owlbear, adult red dragon: clips
  byte-identical; wolf and archmage `rig --force` outputs identical.
- Still broken (mesh): two floor slabs (shadow plates meshed with the tail, skinned to `bone_51`)
  hang tilted behind the feet at rest since `ground_feet` turned `bone_48` 46 deg, and the
  -X stub `bone_48..50` and the slabs never move with the tail; the right forearm is welded to
  the thigh and tail end, so the overhead cut tears there (pre-existing). A re-mesh without the
  slabs is the fix.

**Idle height** (`anim.idle_lift`, override `idle_lift`). The game moves a monster with
`walk`, else `swim`, else `fly` (godot-dnd `Monster.move_clip()`). A monster whose
`animation_set` has no `walk` only swims or flies, so its idle is held where that clip holds
the body (`match_move`): a `lift` move clip (legless swim, the ghost's fly) gives the idle
the same constant body lift, a `hover` one (a bird's fly) the same lowest-vertex height,
and a legged flier idles in the air on a slow, small beat of its fly clip (0.6 of the flap,
2.5 s) instead of standing on nothing. Monsters that walk keep a floor idle (harpy, dragon:
they land). The override takes metres of lift, `"match_move"`, or `false` for the floor.
Body lift idle / move, before -> after: giant octopus 0.062 / 0.341 -> 0.341 / 0.341 (lowest
vertex 0.00 -> 0.28, swim 0.21); merfolk 0 / 0.18 -> 0.18 / 0.18 (swim pitches the body, so
its lowest vertex is 0.43); giant eagle lowest vertex 0.00 / 0.32 -> 0.32 / 0.32; ghost and
hunter shark already matched (0.18, 0.74).

`match_move` takes the move clip's body height on its *first frame*: its lift plus its own
bob at t=0. The octopus swim starts at the bottom of its bob (0.04 x height = 0.136 m below
the lift), so the idle is held at 0.205 m, not 0.341: first-frame lowest vertex idle 0.212,
swim 0.215 (was 0.348 / 0.215). It matches the body, not the lowest vertex, because a swim
that pitches the body (merfolk: lowest vertex 0.66 on its first frame) would otherwise
float the upright idle 0.25 m higher. Merfolk, ghost and shark keep 0.18 / 0.18 / 0.74.

**Lifted one-shots** (`blender_clips.lifted_oneshot`). Every other clip of a monster with a
lifted idle (attack_melee, attack_ranged, hit, ...; not walk, swim, fly) is held where the
idle has the body on its first frame (`body_z_first` of the idle, measured after it is
built; the idle is built first), ground mode `lift`, legs limp, and a legged flier keeps its
fly clip's leg tuck. Death starts there and eases down (`Clip.start_lift`, over the clip up
to `rest_from` - 0.15; over the first 0.3 when the death's own lift already starts higher,
the ghost's) before the lying pose is set on the ground. First / last frame lowest vertex
(m): octopus attack and hit 0.205 / 0.205 (were 0 / 0), death 0.205 / 0; merfolk
attack_melee, attack_ranged, hit 0.18 / 0.18, death 0.18 / 0; ghost death starts at its idle
body 0.18 (was 0.27, its own sag); shark unchanged (0.74, already all `lift`). Giant eagle,
built in scratch on a copy of its rig: idle 0.357, attack and hit 0.362 / 0.362, death 0.362
/ 0, body 0.467 in all four. Every report has `min_z_first` and `body_z_first`, lifted
one-shots `lifted_m`, a lifted idle `idle_lift_m`.

**Attack style per weapon** (`anim.wielded`, job `weapon_r`). The anim stage passes the
prop the record holds in `hand_r`, picked as the viewer does (the first of `wields` whose
`data/registry/props.json` `grip_socket` is `hand_r`), and `attack_melee` follows its
category: `polearm` and `thrown` (spear, trident, pike, glaive, javelin) thrust
(`attack_thrust`), everything else and an empty hand cuts (`attack_biped`, as before). The
bow draw stays `attack_ranged`. The thrust is FK-aimed (`aim_arm`): from the relaxed or rest
arm the hand draws back beside the waist with the elbow back, then drives forward until the
arm is straight in front of the shoulder, holds, and recovers; the shaft (grip +Y) is kept on
the line the fist travels, and the torso's wind-up and lean are taken out of the arm's
directions so the shaft stays on line. Merfolk (spear): grip (0.28, 0.02, 1.51) ->
(0.10, 0.59, 1.58) m, within ~10 degrees of the shaft; the spear tip is 1.74 m forward at the
thrust against the body's front at 0.67, and ahead of the body in every frame. Same 30
frames. Goblin (scimitar): cut and bow draw unchanged. The merfolk's `attack_ranged` is an
FK overhand throw (`attack_throw`, report `style: throw`, round 6 below).

**Arm relax** (`blender_clips.RELAXED`). The rest pose is the reference's pose, and an arm
drawn raised (shoulder -> wrist rising more than `RAISED_DEG` 15 degrees; the merfolk's right
arm: 39) carries its prop over the head in every clip that leaves it alone. Such an arm gets
a base layer in idle, walk, swim, hit and the thrust's ends: upper arm `RELAX_DEG` (30)
from the body, forearm forward, grip +Y forward and up (a spear held upright); death fades it
out over its first half, so the fall ends as before. Merfolk idle grip z 1.87 -> 1.48 m.
Arms that hang (goblin, harpy, ghost) or reach out sideways (the merfolk's left arm, 6
degrees) are left alone. The cut's arm IK fades in and out (`hand_ik`), so it starts and
ends on the relaxed arm too (round 7 below); so does the merfolk's throw (round 6).

**Lift clips that would touch the ground.** `lift` keeps the body at one height, but a pose
can still push a part through the floor (the ghost's hit tilts its robe train to -1.3 cm):
those frames, and only those, are raised by the dip (ghost hit: lowest vertex -0.013 -> 0,
lift 0.18 -> at most 0.193 m).

**Clip-to-clip jumps (2026-11-04, Godot round 6, clips3).** The game switches clips, so each
clip's first frame should be where the idle's first frame is. "Jump" below is the largest
vertex move from the idle's first frame to the clip's first frame (Blender, skinned mesh).

- Serpentine. `idle_serpent` flexes the body as a *standing* wave (`body_wave` speed 0, the
  shape sin(2 pi s) scaled by sin(2 pi t)) and its head layers are 0 at t=0, so its first
  frame is the rest pose, where hit, attack and death start (jump 0.27 -> 0). `slither`
  starts its travelling wave at the phase whose first frame keeps the chain nearest its rest
  (`wave_phase`, a flat FK of the chain about Z over 48 phases; report `wave_phase`,
  `start_dev_m`). `swim_serpent` was 18 degrees per joint over one wave (the tail swung 2 m on
  the 4 m snake): now 16 degrees over 1.5 waves, growing toward the tail (0.5 at the head,
  1.0 at the tail end). The serpentine swim has no lift: it is `contact` like the walk (the
  game places the water line). Constrictor snake idle -> walk 0.717 -> 0.379 m, idle -> swim
  1.245 -> 0.320 m. `strike_serpent` drives the front third (`STRIKE_S` 2/3; was the front
  quarter): its base pitches up 75 degrees and its front half back down 40 (a cobra S, the
  head looking ahead), a sideways S coils each side of the mid-body root (16 degrees, each
  side's turns adding up to 0, so the tail keeps its heading) and the body draws back 0.04 x
  length; then the front third strikes forward and down (-8 / -6 degrees, the head tip at
  0.34 m against 0.48 at rest) as the body slides 0.08 x length forward, holds 0.5-0.62 and
  recovers. `contact`. Snake: head tip wind-up -> bite 0.81 -> 1.39 m (0.35 x length),
  forward 0.54 -> 1.16, rise at the wind-up 0.30 -> 0.63 m; the rear 60% of the body stays
  within 0.04-0.06 m of the ground (its rest arch) in every frame; body correction 0.015 m.
- Merfolk `attack_ranged` (`attack_throw`): FK-aimed like the thrust, from the relaxed arm:
  cocked above and behind the shoulder with the elbow out (0.35), released forward at head
  height (0.5), followed through down across the body (0.62), back on the relaxed arm; the
  torso turns away and leans back, then into the throw; the free arm points ahead while
  cocked. Same 30 frames. Idle -> attack_ranged 0.614 -> 0.059 m (= attack_melee and hit:
  the idle's own small arm and tail waves).
- Legged flier's idle (`fly(amp < 1)`): the beat's size swells back to full at the loop start
  (A(t) = amp + (1 - amp)(1 + cos 2 pi t) / 2, also on the bob), so the idle's first frame is
  the fly clip's; lifted one-shots of a legged flier hold the fly's first-frame wing pose
  (`wing_beat(at=0)`, faded out over the first half of death) next to its leg tuck. The
  avian lunge rears the neck and skull `AVIAN_REAR` (0.35) as far (the eagle's head went
  behind the near wing at 25% and tore the neck). Giant eagle, scratch copy of its rig:
  idle -> fly 0.616 -> 0.009 m, idle -> attack, hit, death 0.938 -> 0.137; edges stretched
  past 4x at 25% of the lunge 219 -> 3 (past 2x 1033 -> 67); idle body 0.413 -> 0.421 m,
  one-shots first lowest vertex 0.348 -> 0.356 (idle 0.352 -> 0.359). Fly unchanged (amp 1),
  so harpy and dragon fly are unchanged.
- Ghost (`idle_float`): every idle layer is shifted to 0 at t=0 (`from_rest`; the hem sway
  `hem_sway(zero=True)`, also in `attack_float`), so the idle starts on the pose the one-shots
  start on; before, its hem sway had lifted the robe train 0.074 m on the first frame (lowest
  vertex 0.254 vs 0.180). Idle -> hit, death 0.104 -> 0, -> attack 0.044 -> 0; idle first
  lowest vertex 0.254 -> 0.180, its cycle minimum 0.079 -> 0.015 (the sway is no longer
  centred on rest). Idle -> fly 0.702 -> 0.801 m (pose: the fly leans 14 degrees).
- Falls that let go of the IK (`death`, ground `rest`): a leg whose IK is half on (0 < k < 1)
  rolls with the body and its own vertices (`leg_masks`) dip, which raising the body barely
  lifts; the foot target is raised by that leg's dip each settle pass (`foot_raise_max`).
  Centaur death -0.033 -> -0.010 m (the -0.010 is the buckle before the roll, as before);
  the same rule changes wolf (-0.016 -> -0.006, feet up to 0.056 m), young red dragon (-0.010
  -> -0.006, 0.095), owlbear (0.024) and goblin (0.014) deaths; spider (legs not level) and the
  legless plans unchanged.

**Clip-to-clip jumps, round 7 (2026-11-05, clips4).** Every clip report now has
`from_idle_m`: its first frame's largest vertex move from the idle's first frame (the idle is
built first). Legged plans (floor idle):

- The idle starts on the rest pose: every layer is 0 at t=0 (look-about, nod, skull turn
  in phase; the upper arm's wave is -sin; the elbow bends 0-7 degrees from straight instead
  of 4 +- 3; the tail sways as one in-phase 5 degree wave instead of a travelling one, which
  had the dragon's ground-lying tail 0.78 m off rest on the first frame). The body is
  `IDLE_SINK` (0.03) x drop down, as before.
- Floor one-shots (attack, hit, death, breath, cast; not burrow) are moved as a whole so their
  first frame has the body where the idle's has it (`at_idle_height`). Started at their own
  height (hit 0, the lunge 0.02 x drop) the standing legs' IK found other knees: centaur
  idle -> hit 0.165 m, harpy 0.060. A `rest` fall's standing part tolerates `FLOOR_TOL` below
  the ground like the floor clips (it raised the harpy's first frame off the idle's).
- The sword cut (`attack_biped`) fades its arm IK in over the wind-up and out over the
  recovery (`Clip.hand_ik`; keys 0, 1 at 0.25-0.75, 0), on the relax layer (and from the
  relaxed wrist) where the idle has one: at full influence on the rest wrist the IK chose
  its own elbow. Harpy idle -> attack 0.137 -> 0 (`grip_r` was 0.133 m off), goblin cut
  0.522 -> 0, bow draw 0.027 -> 0.
- The walk starts at the frame of its cycle nearest the idle's first frame (`start_phase`:
  built once at phase 0, ground passes included, then rebuilt at that phase; every layer is
  shifted, so the loop stays whole; report `start_phase`, `from_idle_phase0_m`). Idle ->
  walk (old idle -> old walk, new): harpy 0.175 -> 0.122, goblin 0.099 -> 0.072, wolf
  0.246 -> 0.197, owlbear 0.267 -> 0.219, spider 0.414 -> 0.093, dragon 0.423 -> 0.435
  (0.496 at phase 0 against the new idle: four legs, a 0.83 m stride, some foot is always
  far), centaur 0.408 -> 0.239. Stride scales and frame counts unchanged.
- Ground contacts (`GROUND`, top-level `ground_contacts`, per clip `contact_min_z`):
  vertices lying on the floor at rest (z <= `FLOOR_TOL`) that no leg owns and no tail turn
  lifts. A floor clip lowers the body for standing feet only as far as these stay above
  -`CONTACT_DIP` (`DIP_MAX` / 3); below that the body is raised for them and the feet fall
  a little short. Only the harpy has any (2: hip-feather tips skinned to the pelvis): walk
  min_z -0.0297 -> -0.010 at stride 0.8 (unchanged), at the price of the standing feet 2.2
  cm short at the stride ends (`planted_foot_gap` 0.004 -> 0.022, under `REACH_MAX` x
  height 0.026). Goblin, wolf, dragon, owlbear, spider, centaur: 0 contacts. The eagle has 6
  but every clip of it is lifted.
- One-shot first frames from the idle after this round: 0 on every legged pilot's attack,
  hit, breath and death, except the dragon's death 0.042 m (its idle turns the tail lying on
  the ground up 1.9 degrees, the `rest` fall does not). Legless and lifted pilots are
  unchanged (now reported: octopus one-shots 0.225, shark 0.14, merfolk 0.059, pudding 0.041,
  fungus 0.051, eagle 0.072, snake and ghost 0).
- Giant eagle lunge on the re-scaled rig (21:59, scratch copy, clips identical under this
  round's code): edges stretched past 4x against the bind pose are 17 in every frame of
  the lunge, the strike included: the held fly first-frame wing pose (the fly clip itself
  has 4-17), the lunge adds none. Head (`bone_5` tip) forward travel wind-up -> strike 0.397
  m = 0.26 x body length (1.54 m). The same count on the old rig and clip: 304 at the strike
  (88 was against its first frame). No change needed.

**Clip-to-clip jumps, round 8 (2026-11-05, clips5).** `from_idle_m` compares frame 0 with
idle frame 0, but a game fires one-shots (and starts the walk) mid-idle. Every clip report
(clips.json and manifest `anim.<clip>`) now also has `from_idle_max_m`: the largest vertex
distance between the clip's first frame and any frame of the idle cycle (every
`IDLE_STEP` = 3rd frame); on the idle itself it is how far the idle wanders from its own
first frame. It is the instant-switch worst case: anything the idle wanders adds to it.

- Dragon idle tail (`tail_idle_amps`): round 7's in-phase 5 degree sway on each of the 9
  curled tail bones added up along the chain, the tip (`bone_66`) swinging 1.63 m off rest
  mid-idle (Godot: walk started after 0.5 s of idle popped 1.5 m). The idle's tail sway is
  still in phase about Z (0 on the first frame, so the idle still starts on the rest pose)
  but grows from the root to the last bone (i / n of the amplitude), the last bone at most
  `TAIL_IDLE_DEG` (5), and all of it is scaled down until the far end of the chain moves at
  most `TAIL_IDLE_TIP` (0.05) x `body_len` (bound: each bone's angle in radians x the
  largest horizontal distance from its head to a joint further out). Dragon (body_len
  5.90 m, bound 0.29 m): tip travel over the idle 1.625 -> 0.243 m; the idle's
  `from_idle_max_m` 1.657 -> 0.305 m (the rest is now the head's look-about, at the top of the
  neck); attack, breath, hit 1.657 -> 0.305, death 1.669 -> 0.315, walk 1.698 -> 0.618 (its
  own first frame is 0.435 from the idle's). `tail_raise_max_deg` 1.92 -> 1.72 (less of the
  ground-lying tail swings down). No other pilot's idle has a `tail` chain (the wolf's and
  owlbear's are not found as tails; the merfolk's fish tail and the snake's body have their
  own builders): every other clip report is identical.
- `from_idle_max_m` per pilot, worst clip (fly and swim are pose changes, not idle drift):
  wolf walk 0.198 (one-shots 0.092), goblin walk 0.084 (0.050), owlbear walk 0.219 (0.066),
  harpy fly 1.310 (walk 0.126, one-shots 0.096), spider walk 0.097 (0.024), dragon fly 3.589
  (walk 0.618, one-shots 0.305-0.315), snake walk 0.572 (one-shots 0.275), ghost fly 1.018
  (0.226), pudding walk 0.205 (0.060), fungus walk 0.185 (0.073), octopus swim 0.720 (0.356),
  shark swim 0.620 (0.280), merfolk swim 1.091 (0.076), swarm of rats 0.084 (all clips),
  giant eagle fly 1.701 (one-shots 1.683: its idle is a slow wing beat, 1.694 from its own
  first frame).

**Round 10 clip fixes (2026-11-05, clips7; Godot round 10, wolf and centaur tail chains).**
`anim --force` on all 16 pilots: the same clips and frame counts; numbers changed only on the
tailed pilots, the centaur and the armed walks (and the merfolk swim). Edges past 4x below are
against the bind pose, every 3rd frame, largest mesh (the gate of the tail work).

- Hanging tails in death (`death_drape`, `drape_solve`, `drape_settle`; report `tail_drape`,
  and on every death with a tail `tail_z_last` [lowest, highest tail vertex on the last frame]
  and `body_top_last`). Each tail bone has one layer: the floor clips' root lift
  (`tail_up`) blended into a per-frame drape by `drape_w` (0 until t 0.3, 1 when the roll
  lands at 0.75). The drape is solved from the posed parent bone on every frame: a bone with
  room below it points down and back (`DRAPE_BACK` 0.6 back per unit down), one that would
  pass the ground line points at it, the rest lie along the ground (back along the body and
  the way the tail pointed); each bone turns at most `DRAPE_BEND` (12 degrees) off the line it
  would hold riding with its parent, the root `DRAPE_ROOT` (0) off the rump. The ground line
  is settled per frame so the lying tail's lowest vertex touches the floor, raised where a
  falling one dips past `TAIL_DIP`; the lying body is set on the ground without the tail
  (`notail_low`). The bend limit is what the rigs allow: more tears the tail's base (death
  edges past 4x, root / joint limit: wolf 0/12-0/15: 3 (as before), 0/20: 5, 10/30: 42,
  20/15: 34 with the tail on the floor; owlbear 0/12: 5 (as before), 0/15: 6; centaur 0/0-
  0/15: 39, 0/25: 42, 20/15: 49, 40/15: 60): the wolf's rump stub `bone_27` shares the tail's
  base, the owlbear's fan tip meets the left hind foot, the centaur's sheet is fused to its
  left hind leg. So the tail drops part of the way. Lying tail, lowest vertex (no drape ->
  now; body top): wolf 0.18 (Godot round 10) -> 0.13 m (0.57), owlbear 0.46 -> 0.39 (1.86), centaur 0.87 ->
  0.65 (1.58). Level tails keep the counter-turn: the dragon's lies at 0.45-2.12 m against its
  body top 3.22, not above the lying body, so it is unchanged.
- Same tail pose at every clip's start. A draped death starts on the idle's first-frame root
  lift (`FIRST["tail_up0"]`, the centaur's 12.3 degrees) and eases it out over the buckle
  (`DRAPE_EASE` 0.3; held through the buckle, or raised further for the dip, it tore the sheet
  off the hind leg: 41 and 156 edges); while the legs stand, the fall's ground pass lowers
  the body for a foot that falls short and lets the tail dip `TAIL_DIP`, as the floor clips do
  (it raised the body 0.016 m off the idle's). Centaur death `from_idle_m` 0.193 -> 0
  (`from_idle_max_m` 0.319 -> 0.128). One-shot `from_idle_m` on all 16: 0 on every legged
  pilot's attack, hit, breath and death (goblin, wolf, owlbear, spider, harpy, centaur,
  dragon but its death 0.042 as before; snake, ghost and swarm 0 too); lifted and legless
  ones unchanged (eagle 0.072, pudding 0.041, fungus 0.051, octopus 0.225, shark 0.14,
  merfolk 0.059). The centaur death's one extra edge past 4x (39, was 38) is that start
  pose: the idle's own first frame (same count with no drape at all).
- `TAIL_DIP` = `DIP_MAX` x 0.8: the floor lift aims at 0.8 of it when the end trips it (the
  passes converge from below). Centaur lowest vertex idle -0.0316 -> -0.0239, walk -0.0306 ->
  -0.0239, attack, hit -0.0313 -> -0.0239/-0.024; tail turn idle 16.0 -> 17.4, walk 24.1 ->
  25.0 degrees; idle edges past 4x 21 -> 24 (the higher lift pulls the sheet off the leg).
  The centaur's walk ran at half stride only because its tail end converged just past
  `-DIP_MAX`, which tripped the walk retry; at full stride it tears 209 edges (was 100 at
  0.5). The walk retry now also shortens the stride while the clip tears more than
  `TEAR_MAX` (20) edges past `TEAR` (4) x rest (`torn_edges`, counted in the build on every
  `IDLE_STEP`-th frame; report `torn_edges` when the last stride still does): centaur 1.0 ->
  0.5 (85 there), every other walk keeps its stride. Centaur walk edges 100 -> 83.
- Arm swing from the loadout (`swing_share`, `arm_swing`; job `weapon_l` next to `weapon_r`,
  `anim.wielded` with props.json `grip`). A polearm, thrown weapon, bow or crossbow, or a
  two-hander (`grip` 2) is held steady: the shoulder swings `STEADY_SWING` (0.2) of the walk's
  16 degrees, the elbow keeps its mean bend, and the hand (else the forearm) turns back
  against the shoulder's swing through the arm's other layers (the relax layer), so the grip
  keeps its direction. Other weapons swing `LIGHT_SWING` (0.5); empty hands the full swing.
  Same in `swim_legged`, `swim_naga` and `crawl_naga` (on the oscillating part; the offsets
  stay). Report `arm_swing` {side: share}, `grip_cycle_deg` (most the grip's +Y turns over the
  clip from its first frame) and `grip_from_idle_deg` (from the idle's first frame). Grip +Y
  over the cycle, before -> after: centaur pike 18.9 -> 0.0 degrees (from the idle 15.6 ->
  0.0: the pike no longer sweeps to the floor and over the head), goblin scimitar 23.8 ->
  11.9, goblin bow 23.8 -> 0.0, merfolk spear in the swim 30.2 -> 2.9 (its empty left hand
  30.2 as before). Unarmed walks (harpy, wolf, ...) unchanged.
- Hanging tail clear of the hind legs (`tail_clearance`; walk report `tail_leg_min_m`,
  `tail_leg_rest_m`, `tail_sway_min_k`). Distance: the tail line below its root bone against
  the hind legs' joint paths below the hip (`tail_leg_dist`; the root and hip bones meet at the
  rump). Per frame, where it is under `TAIL_CLEAR` (0.9) x the rest distance, the sway's
  share (`sway_k`) is cut by (d / clearance)^2, down to `TAIL_CLEAR_MIN` (0.2), smoothed over
  the loop (moving minimum, then mean, circular); three passes. A tail within `TAIL_FUSED`
  (0.02 x height) of a hind leg at rest sways with it (centaur, 0.008 m: its walk tore 88
  edges with the clearance, 83 without). Wolf walk min distance 0.0003 -> 0.036 m (rest
  0.060; the tail end passed through the hind hock at 75%), owlbear 0.233 (rest 0.442);
  edges unchanged (0, 2).

Gate (edges past 4x, walk / death; limits wolf 0/3, owlbear 2/5, centaur 100/38): wolf 0/3,
owlbear 2/5, centaur 83/39 (the one over is the start pose above).

**cast, swim, burrow, breath and fly for every plan that asks (2026-11-04, clips2).** The
SRD animation sets ask for `cast` 48 times, `swim` on legged plans and floating 39, `burrow`
18, `breath` off the winged quadrupeds 11 and `fly` without wing chains 9; the legged
framework (`plan_clips`), floating and serpentine now build them:

- `cast` (1.2 s, once; `CAST_T`: gather to 0.3, hold with a small pulse to 0.58, release at
  0.7, hold to 0.8, recover). With arms (`cast_arms`, FK-aimed like the thrust): the
  `hand_r` arm rises forward and up over the head (grip +Y up), then throws forward at
  shoulder height (grip +Y forward); the other hand comes up before the chest and pushes
  forward with it; the torso leans back 5 degrees and forward 7, the head tips up. Without
  arms (`cast_head`: dragons, beasts, the owl): neck rears 28 degrees with a 3 degree pulse,
  snaps forward 8, the body pitches nose-up 5 degrees about the hind hips (upright and
  floating bodies rise 0.03 x drop instead), wings lift, tail curls. It starts and ends on the
  rest pose (the relaxed arm where the idle has one), on the floor; a lifted monster's is
  held at its idle height like the attacks. It has the walk/lunge `DIP_MAX` retry
  (`body_scale`): the owlbear's front paws, stretched by the pitch, dip 2.8 cm at 0.8 (4.5 cm
  at full pitch with a body lift on top, which four legs therefore do not get). Floating uses the same builder on
  `hover_clip`, serpentine its own (`cast_serpent`: the front quarter rears 40 degrees).
- `swim` (1.8 s, loop; `swim_legged`): ground `lift`, body lifted `SWIM_LEGS` (0.5) x the leg
  drop with IK off, pitched nose-up 6 degrees about the hind hips (upright bodies lean 35
  degrees forward about the middle), a 0.012 x height double bob. Legs paddle in diagonal
  pairs (front-right with hind-left): upper leg +-24 degrees, the paw curling on the forward
  swing; upright legs flutter-kick; sprawled legs sweep fore and aft about Z. Arms
  dog-paddle over the relax layer, wings fold back (`fold_wings`: 40 degrees about Z, 15 up),
  the tail sways as a rudder, the neck holds the head up. Because it is `lift`, a monster
  whose set has no walk idles at the swim's body height (`match_move`), and its one-shots
  are held there: a wolf with `[idle, swim, cast, hit, death]` (scratch) holds idle 0.396 /
  swim 0.394 / cast and hit 0.396 m body lift. With a walk the swim simply hovers and the
  game blends. Floating: `swim_float` (lean 25 degrees, arms breaststroke, hem kick).
- `burrow` (1.5 s, once; ends sunk, hold the last frame): ground `none`, so it is the one
  clip that goes below the ground on purpose; `min_z` is the real lowest vertex and
  `below_ground: "allowed"` is in its report. Crouch 0.15 x drop, the front legs (a
  spider's front pair; a biped's arms) dig three alternating strokes (up and forward, then
  0.2 x drop down into the ground and back), the head goes down, the body pitches nose-down
  to 14 degrees, then from 0.62 the body and every foot sink `BURROW_SINK` (0.5) x the body
  height. The body height leaves the wings out (`body_h`): the dragon's raised wings make
  its height 6.2 m against a 4 m body (sink 3.1 -> 1.98 m). It is never lifted (a swim-only
  burrower would start from the floor; no SRD record is one). Serpentine:
  `burrow_serpent` (the head pitches down 40 degrees, the body slides forward and down).
- `breath` on any skeleton with a head chain (was: non-upright and winged). Four legs keep
  the dragon's clip exactly; upright bodies (half-dragon, iron golem, mephits) lean the
  torso back 16 degrees and thrust it forward 10, the neck doing half the dragon's share,
  arms swinging back and forward with it. Serpentine: `breath_serpent` (front quarter).
- `fly` without wing chains (`levitate`, 2 s loop): ground `hover` at `HOVER`, IK off, a
  slow bob (half the winged bob), a 6 degree forward lean upright (3 nose-up on four legs),
  legs hanging and trailing a little, arms slightly out. Wing chains found: the wing-beat
  `fly` as before. The 9 records: cloud-giant, efreeti, oni, storm-giant (biped),
  nightmare (quadruped), couatl and flying-snake (serpentine: `fly_serpent`, the slither
  held `HOVER` up), giant-wasp (insectoid: the wing beat if `find_wings` finds its wings,
  else the levitation), animated-flying-sword (plant_object: its own hover set since 2026-11-07,
  "Animated objects" below).

Pilots tested in scratch jobs (`anims` in the scratchpad, the real record untouched): goblin
(cast arms, swim, burrow, upright breath, levitate), wolf and owlbear (head cast, swim,
burrow, breath, levitate), giant-spider (swim, burrow, levitate), young-red-dragon and harpy
(cast, swim, burrow, breath; their wing-beat fly unchanged), ghost (cast with arms, swim),
constrictor-snake on a copy of its current rig (cast, breath, burrow, fly). No pilot's
registry `animation_set` contains any of the new clips (the four with `swim` already had
it), so the real re-run changed no clip list.

**Size-relative thresholds.** The metre constants were the pilots': a 0.1-0.6 m tiny body got
a 0.32 m hover, a 15 m roc the same. Each is now multiplied by `SCALE`: with `SIZE` = max(mesh
height, mesh length) and `SIZE_BAND` = (0.95, 6.5) m (the 15 pilots: goblin 1.0 to dragon
6.3), `SCALE` = SIZE / 0.95 below the band, SIZE / 6.5 above it, 1 inside, so every pilot
keeps its numbers exactly:

| constant | was | now |
|---|---|---|
| `FLOOR_TOL` | 0.008 m | 0.008 x SCALE (0.0084 x SIZE below the band, 0.00123 x SIZE above) |
| `FOOT_TOL` | 0.004 m | 0.004 x SCALE (0.0042 x SIZE / 0.00062 x SIZE) |
| `DIP_MAX` | 0.03 m | 0.03 x SCALE (0.0316 x SIZE / 0.0046 x SIZE) |
| `HOVER` | 0.32 m | 0.32 x SCALE (0.337 x SIZE / 0.049 x SIZE) |
| fly bob | 0.06 / 0.1 m (height under / over 3 m) | `FLY_BOB` x SCALE |

`REACH_MAX` was already x height; `FLOAT`, `SWIM_LIFT` and the other clip sizes were already
fractions of height, drop or body length. `clips.json` reports `size` and `size_scale`. A
0.3x copy of the goblin (scratch, SIZE 0.3, SCALE 0.316): fly hovers at 0.101 m, `DIP_MAX`
0.0095 m, swim lift 0.047 m, burrow sinks 0.15 m.

Known limits: the owlbear walk and lunge run at half size. The kept skeleton
(`rig/chosen.glb` = roll_1 since score v2) skins the tail fan to the tail chain (`bone_13`);
the fan on a hind shin was reported on roll_3 (`rig/chosen_roll3_backup.glb`), kept before score v2. At full stride the front
paws' non-sole vertices dip 5-14 cm and the standing feet fall 0.093 m short of their
targets (limit 0.035), so a re-weight of the fan would not help. The harpy's walk runs at 0.8 stride; its
two hip-feather tips on the floor (ground contacts, round 7) keep the standing feet up to
2.2 cm short at the stride ends; its wings hide most of the sword cut. The spider's death rests on curled
legs rather than its belly.

## Clip audit (2026-11-06)

`python3 -I tools/clip_audit.py --all --jobs 8` (Blender on the machine that runs it; ai: 115 rigs in
2.7 min) plays every procedural clip in `anims/` on `model_rigged.glb` (`tools/blender_clip_audit.py`,
skinned mesh on every frame) and writes `out/srd-5.2.1/_audit/<id>.json` (metrics), `flags.json` and
`clips.md` (per-plan table, flag counts, monsters worst first). Read-only: nothing under `<id>/` is
written; promoted UAL clips and `rig.tool: ual` rigs are skipped. `--report` redoes the flags from
the metrics (thresholds in `clip_audit.T`), `--sheets 12` renders the 12 worst non-bipeds
(`_audit/sheets/<id>.png`: 5 side + 2 front frames per clip, plus 4 consecutive frames around each
flagged pop; left limbs blue, right orange, tail green, wings purple, head yellow).
Metrics per clip: lowest vertex (sink / float, x height), edge stretch p99.9 and where the worst edges
are (part), per-vertex pops (a one-frame jump over 2x both neighbours' moves, or an out-and-back
flicker), loop closure, from_idle, walk stride and lift per foot / leg length, stance-foot backward
speed (the ground speed the walk implies) and its spread over the feet, socket drift against the 24
nearest vertices, wing-tip and tail-tip travel, death last frame (floor, top, still), idle motion,
limb share inside a torso ellipsoid; per rig the tail's vertices near the floor (`tail_low_share`).

Result (115 rigs, 32 non-biped): flagged non-bipeds are almost all quadrupeds with a tail chain on
floor vertices. Seen on the sheets: giant-toad's right hind leg is the tail *and* a wing chain (hit
flips it up, attack/hit sink 18-32% h, walk barely steps); boar's tail chain is its right hind leg;
giant-weasel's hind leg hangs under the tail root; badger, hippo, centaur, black-bear and giant-hyena
tails are fused to the rump/hind thigh and drag through the floor or tear. `tail_low_share` separates
them (0.07-0.44 vs 0-0.04 on the clean hanging tails). Polar bear: weights bleed between the legs
(webs in walk/swim, stretch 5.7 idle, 15 swim). Weasel walk: the body drops one frame after the loop
seam (per-frame ground correction, unsmoothed); boar/badger idle and attack pops sit on the tail end.
Implied walk ground speed is 0.38x the SRD walk speed (median of 104 walks; 0.09-0.83).
False alarms found on the sheets and fixed in the thresholds: idle float/wander on fliers that idle in
the air (lifted idle now follows `anim.idle_lift`), serpentine wander/from_idle (displacements now
against max(height, length)), wing-membrane stretch 4.5 (+1.5 where wings tear most), legs tucked in a
lying death (limb_in_torso skipped on death), hyena sinks of 2.5% h (sink 0.02 -> 0.03, the stage's
DIP_MAX), eased 3-frame hit snaps (pop needs 2x both neighbours), tail-end pops of 0.02-0.03 h not
visible (pop 0.03), black-pudding settling (death_hold 0.005). Not trusted: stride/gait on sprawled
legs (insectoid skipped), gait spread on owlbear/black-bear, limb_in_torso on bipeds' cuts, socket
drift, every biped flag (not looked at; bipeds go to UAL).

### Tail, topology and smoothing fixes (2026-11-06)

The audit's top fixes for the non-biped plans. Bipeds are untouched (archmage and ape: rig and
every clip identical, checked against a same-machine run of the old code).

- **find_tail** (`rig_topology`, clips and roll scoring): a chain whose first bone has a leg
  under it is the pelvis; the tail starts below that branch (giant weasel: `bone_24` carried the
  tail and the right hind leg, now `bone_34..40`). A chain with a joint `WING_REACH` x the height
  out to the side above the ground band is a sprawled limb (giant toad: its right hind leg was the
  tail and the right wing). A chain rooted in the ground band is a foot or toe piece.
- **Tail insertion** (`blender_tail_chain.detect_tail`): the found tail must be isolated: every
  band centroid at least `LEG_NEAR` 0.08 x h from every leg's joint path (clean tails: 0.13 or
  more; boar 0.054, hippo 0.069, black bear 0.061, giant hyena 0.063, centaur 0.038) and at most
  `LOW_MAX` 0.06 of the lobe within 0.1 x h of the floor (clean 0-0.04; badger 0.23). Otherwise
  no chain (`tail_added.found` false, the reason in the log), as `rig_tail_chain: false` would.
  Override `rig_tail_chain: "fused"` keeps the chain anyway (set on centaur-trooper: its tail
  sheet is fused to a hind leg and the seam weights handle it; without the chain the sheet kicks
  with the hoof and the death tears, edge stretch p99.9 4.2 -> 9.8).
- **Topology gate** (`blender_clips.topology_gate`, before any clip): no chain used twice (a wing
  that is a leg, a tail that is a wing or a leg), no leg under the tail's root, no hanging tail
  with over 0.06 of its vertices within 0.1 x h of the floor (the audit's `tail_low_share`;
  skipped with `rig_tail_chain: "fused"`). What fails is left out of the clips and listed in
  `manifest.anim.warnings` (and the log). Wings are only searched on winged plans (avian,
  winged_*) or when the record has `fly` (the toad's leg and the kobold's tail were "wings"),
  and the ground tail fan is avian-only (on the badger it was a piece of a hind foot).
- **Smoothed corrections** (`blender_clips.smooth_series`): after the floor passes settle, a
  body-height or tail-lift series with a one-frame jump over `POP_TOL` 0.01 x h is closed (3-frame
  max, then 3-frame mean; loops wrap): a lone frame becomes a 5-frame ramp and no frame ends lower
  than its own correction asked. Series without such a jump are untouched. Not on upright plans.
  The weasel's walk raised the body 0.08 / 0.03 / 0.12 m on consecutive frames.

Audit (score = sum of flag severities; before = `out/` as audited, which a same-machine run of the
old code reproduces exactly):

| monster | score | flags gone | still |
|---|---|---|---|
| giant-toad | 16.8 -> 1.9 | attack/hit/idle sink, hit pop (no tail, no wing) | walk stride: the 4 legs found are the front feet and toes, the hind legs ride the body (needs a re-rig) |
| boar | 10.3 -> 0 | all 6 (sinks, idle tail pop): no tail, the right hind leg walks as a leg | |
| giant-weasel | 9.1 -> 6.5 | walk pop (the seam drop); walk sink 0.138 -> 0.077, tail no longer swings the hind leg | walk/attack sink (a hind leg pokes through: rig sides/legs), death stretch 3.2 -> 6.7 (the rump no longer turns with the tail) |
| giant-badger | 9.6 -> 0 | all 7 | |
| hippopotamus | 6.1 -> 0 | all 5 | |
| black-bear | 6.3 -> 4.1 | death sink, swim stretch | walk/attack pops on a front leg, walk gait (as before) |
| giant-hyena | 1.2 -> 0 | walk stretch 5.4 -> 1.5 | |
| centaur-trooper | 9.8 -> 9.8 | (tail kept by override) | as before (the arm stick, the leg sheet) |

Controls (wolf, mastiff, hyena, rhinoceros, hell-hound, dire-wolf, owlbear, blink-dog, brown-bear,
giant-boar, polar-bear, young-red-dragon, giant-eagle, swarm-of-bats, harpy, giant-spider): rigs
identical; clips identical except where smoothing ran (mastiff, hyena, blink-dog walk; dire-wolf
attack/hit/walk; red dragon attack/hit; spider attack/hit/walk; centaur hit): pops 0.015-0.038 ->
0.000-0.011 x h, sinks equal or smaller, no new flags (spider attack/hit feet up to 0.01 x h off
the floor). Kobold-warrior's idle/hit change (its tail is no longer flapped as a left wing).
copper-dragon-wyrmling loses its inserted tail (root 0.033 x h from a hind leg; the coiled tail is
skinned to the right hind leg either way, the old chain only took its tip): score 1.06 -> 0.

Re-apply without the GPU: `forge.py rig <id> --force` (no `--rig-reroll`) rebuilds the game files
and the tail chain from the kept `rig/chosen.glb` in Blender only (finalize); `anim <id> --force`
then rebuilds the clips.

### Missing legs (2026-11-06)

The anim stage failed (`found N legs, need 4`) on 11 horizontal bodies, and the batch retried them:
six dragon wyrmlings (blue, brass, bronze, gold, green, red), bulette, crocodile, giant frog, giant
lizard, rust monster. The legs were mostly in the rig; `find_legs` missed them.

- **Rescue pass** (`rig_topology.find_legs(..., rescue=True)`, `rescue_legs`): only when the strict
  rules find fewer than `want` legs, so a rig that has its legs gets the same answer (controls below).
  A foot that fans into toes from an ankle above `TOES` is the foot (`_toe_fans`; the lizard, the
  crocodile, the rust monster and most wyrmlings: each toe was a one-bone "leg"); not legs: a tail on
  the floor (backwards on the centre line, the crocodile's; or a non-fan foot lying out farther than
  it drops or spanning more than `FOOT_SPAN` on the floor, the red and green wyrmlings' curled tails),
  a wing finger resting on the floor (root `LEG_ROOT_SIDE` out and above half the height), 2-bone
  chains (the frog's folded hind legs flip under IK), two chains on one foot (`SAME_FOOT`, the green
  wyrmling's right hind); at most half per side first; then a raised twin: a chain off the same bone
  as a leg taken, on the other side, level with it (`TWIN_Y`), dropping 0.1 x h below that bone (the
  rearing red wyrmling's left fore paw, the brass wyrmling's lifted left fore leg). The anim stage
  uses it on non-upright plans (`blender_clips`); roll scoring, the tail and wing insertion still use
  the strict rules.
- **Partial legs** (`blender_clips.PARTIAL`): a horizontal body still short of legs walks on the ones
  found (the gait's odd-count phases; the missing leg rides the body), `manifest.anim.partial_legs`
  and a line in `manifest.anim.warnings`; with none it gets the standing set. The stage no longer
  fails on missing legs.
- **`rig_fix_legs: "mirror"`** (experimental, `tools/blender_leg_mirror.py`, rig stage finalize and
  chosen score, `forge.py rig <id> --force`, CPU): for a leg with no twin, the twin's chain is the
  leg's chain and toes mirrored about the mesh's centre plane and moved onto the unowned foot piece on
  the floor nearest the mirror image (`legm_<bone>`), weighted by `rig_reweight` (fit). Set on no
  monster: none of the 11 has a free foot for it (the crocodile's mesh has no right hind leg, the
  bulette's left side one foot, the bronze wyrmling no fore-leg chain on either side). Synthetic test
  (the giant lizard's right hind chain cut out of a copy): the leg comes back and walks, but its thigh
  tears (walk stretch 12.2 against 1.9 on the real rig).

Per monster (all failed before; after: legs, audit score, flags):

| monster | legs | score | flags / notes |
|---|---|---|---|
| giant-lizard | 4 (toe fans) | 0 | |
| rust-monster | 4 (toe fan at 0.09 x h) | 0 | |
| crocodile | 3 (tail was the "leg") | 0 | partial: the mesh has no right hind leg |
| red-dragon-wyrmling | 4 (raised fore paws; curled tail dropped) | 0 | rearing pose, walks with the paws up |
| green-dragon-wyrmling | 4 (duplicate hind chain, tail dropped) | 0 | tail gated (on the floor) |
| blue-dragon-wyrmling | 4 | 2.2 | death/breath stretch (wing on the right hind leg's weights) |
| brass-dragon-wyrmling | 4 (lifted fore leg) | 1.3 | fly stretch |
| gold-dragon-wyrmling | 4 | 11.3 | wings tear in fly/swim/death (stretch 17-21), tail tip on the right hind leg: rig weights |
| bronze-dragon-wyrmling | 2 (tail was a "leg") | 0 | partial: no fore-leg chains in the roll (needs a re-roll) |
| bulette | 3 | 0 | partial: the roll's right side has 3 chains, the left 1 (needs a re-roll) |
| giant-frog | 2 | 1.0 | partial: hind legs are 2-bone chains, ride the body; swim stretch 4.5 |

Controls on ai, old and new code on copies (rig `--force` and anim `--force`): wolf, mastiff, hyena,
boar, young-red-dragon, black- and copper-dragon-wyrmling, archmage, ape: `model_rigged*.glb` and
every `anims/*.glb` byte-identical; the JSON reports differ only in `build_seconds` and timings.

### Insectoids, nagas, fused legs, wing chains (2026-11-06)

The audit's next worst non-bipeds. Clip code changes touch only the insectoid and merfolk_naga plans;
the rest are opt-in overrides applied by `forge.py rig <id> --force` (CPU, no re-roll).

- **Sprawled legs** (`rig_topology.find_legs(..., sprawl_drop=True)`, anim stage only; roll scoring
  unchanged): a sprawled chain whose end is higher than its root is not a leg. Ranked by length, the
  giant scorpion's tail and a claw were 2 of its 8 "legs" (their IK targets in the air pulled the body
  0.3-0.4 x h into the floor in the attack and hit), and the phase spider's two legs held up in the air
  were legs too (attack sink 0.39, walk 0.22). The giant spider's raised front legs end below their hips
  and stay (its legs and clips are byte-identical).
- **Sting and claws** (`find_sting`: 4+ bones rooted behind the hips near the centre line, rising to
  0.5 x h and ending within 0.3 x h of it; `find_claws`: 3+ bone non-leg chains reaching 0.1 x h past the
  front feet): `attack_scorpion` keeps the legs planted, raises and opens the claws and snaps them shut
  as the sting arcs forward over the back (`STING_STRIKE` -40 degrees over the chain); idle sway, walk
  sway, hit flinch, death slump. None on the three spiders. Report and `manifest.anim.parts`: `sting`,
  `claws` (only when found).
- **Long sprawled legs**: the IK chain of a tip-only leg grows past 3 bones until it spans `IK_SPAN`
  0.5 of the leg (the scorpion's 7-8 bone legs; their last 3 bones are 0.3 of it, and the body was
  lowered 0.35 m for them in the lunge), and the death fold per joint is scaled by 3 / (bones - 1)
  (spiders' 3-4 bone legs unchanged).
- **Naga idle** (`idle_naga`): every layer is 0 on the first frame (`from_zero`), so the one-shots start
  where it does (from_idle 0.16 x h -> 0 on the salamander, 0.033 / 0.021 -> 0 on the merfolk and
  merrow); `tail_kick` gives a tail of n > `KICK_BONES` 7 bones (7 / n)^1.5 of the turn per bone (the
  salamander's coiled 15-bone tail swung its far coil 1.5 m in the idle; merfolk and merrow unchanged).
- **`rig_leg_seams: true`** (`rig_reweight.smooth_seams`, finalize after `rig_reweight`): the polar
  bear's legs are fused in the mesh (149 edges bridge the fore legs from the paws up, 63 the hind), so
  no weight split removes the web: an exclusive split (each vertex to one leg) made it worse (swim 32.6).
  Instead the weights within 4 edge hops of a leg-leg seam (legs as the clips find them) are relaxed 20
  times toward their neighbours over the welded surface.
- **`rig_add_wings: false`**: no inserted wing chains; the roll's own are used. The gold dragon
  wyrmling's wing fingers rest on the floor, the rig stage's strict leg rule took them for legs, so
  finalize inserted `wing_*` chains beside SkinTokens' wings, which kept most of the membrane (fly/swim/
  death stretch 17-21). The clips' rescue legs leave the fingers alone and `find_wings` takes bone_31 /
  bone_43.
- Salamander: `rig_reweight` on its left "arm" (bone_14..16, a finger chain `find_arms` takes; bone_16
  owned 729 vertices of the tail's far coil, which the throw tore).

Audit on ai copies (score, old code -> new; the old code reproduces the audit's `out/` numbers):

| monster | score | fixed | still |
|---|---|---|---|
| giant-scorpion | 18.6 -> 8.5 | tail and claw no longer legs: attack/hit sink 0.30/0.42 -> 0.05/0.04, hit stretch 16.4 -> 6.8, attack 13.3 -> 5.6, death 14.6 -> 8.4 | rig weights: bone_26 (front right leg) and bone_33/bone_80 share a second right leg the roll has no chain for (walk stretch 7.1); needs a re-roll |
| phase-spider | 13.4 -> 1.6 | attack/walk/idle sinks | death stretch 9.7 (10.4) |
| giant-wolf-spider | 3.2 -> 3.2 | (two hind chains whose bones zigzag over the body ride it now) | attack sink 0.056, death stretch 7.8 |
| salamander | 12.2 -> 0 | from_idle, idle wander, throw stretch (3.1 with the clip changes alone) | the "head" chain is its right arm, the attack moves fingers |
| polar-bear | 11.3 -> 2.9 | swim 15.1 -> 5.6, walk 9.1 -> 4.2, attack 9.0 -> 4.6, death 8.5 -> 3.6 | fused legs (thin webs in the swim); death from_idle 0.20, as before (not looked into; its first frame stands 0.057 m higher than the idle's) |
| gold-dragon-wyrmling | 11.3 -> 1.2 | wings flap with their membrane | swim stretch 5.5 |

Controls (ai, old vs new code): wolf (rig `--force` too), mastiff, boar, hyena, archmage, ape,
giant-spider, giant-lizard, young-red-dragon, giant-eagle, black- and blue-dragon-wyrmling, owlbear,
centaur-trooper, brown-bear, harpy: every `anims/*.glb` byte-identical, reports equal but timings.
merfolk-skirmisher and merrow: only the idle changes (no new flags; merrow idle stretch 1.45 -> 1.90).

### Animated objects: rug and flying sword (2026-11-07)

Both are `plant_object` and were built by the batch with the plant clips: the rug (`fallback:vertical`,
5 bones) stood up as a 3 m wall swaying like a fungus stalk (its top-view reference meshes as an
upright panel, thin in Y), the sword (5 bones) bent like rubber, stood on its tip on the floor, and
had no `fly`. The anim job now carries the record's `rig_template` (`job["template"]`); two
template-keyed sets in `blender_clips.py` take over, every other plant_object builds as before:

- **Cloth** (`custom.cloth_grid`, `CLOTH`): every clip lays the panel flat (`CLOTH_Q` about its
  centre: top end forward, the `CLOTH_FACE` +Y side, the pattern, up) and shapes the chain as a
  profile `pitch(s, t)` (degrees each bone rises toward the front, s = 0 back .. 1 front) plus a
  sideways bend `yaw(s, t)`: the back bone's angle is the body tilt, every other bone turns by the
  difference to the one behind it. Ground `contact`. Rest of every one-shot = the idle's first frame
  (`cloth_rest`: flat, front end curled up `CLOTH_CURL` 30 degrees, the ripple at its start).
  `idle` a ripple running back to front, the curl breathing; `walk` a slither-glide (a 13 degree
  wave running front to back, a sideways wave, the front nodding); `attack_melee` rears the front
  half 70 degrees, lunges 0.2 x its length and folds the front over and down (a hood), holds,
  lies back; `hit` front end snaps up and aside, a shudder; `death` rears and shudders, slaps flat
  and lies with two shallow wrinkles and a kink. A wave needs bones: override
  `rig_fallback {"bones": 12}` (new, any fallback plan) and `rig --force` rebuilds rig/fallback.glb
  with that count on the CPU when `rig/fallback_build.json` was built with another (`rig.fallback_stale`);
  without the override nothing changes.
- **Rigid flying prop** (`custom.rigid_prop` and `fly` in the set, `BLADE`): nothing bends; each
  clip is the root's rigid transform about the mesh centre, from the blade's own axes (PCA long
  axis toward the tip = the end away from the widest slice, the guard; the thinnest axis is the
  flat's normal), so the tip points forward (+Y) whatever the rest pose (this mesh rests tip down
  and backward). The centre is placed from the lowest vertex of the rigid rest vertices: ground
  `none`, no ground pass, and the record's `match_move` lift is not applied (`LIFTED ... and not
  BLADE`). `idle` hovers with the lowest vertex `BLADE_HOVER` 0.5 x blade length up, tip 10 degrees
  down, a two-bob loop, a 10 degree yaw sway; `fly` (and `walk`) glides tip first at 22 degrees,
  banking and bobbing; `attack_melee` draws back and thrusts 0.55 x its length along its own line
  with a twist; `hit` knocked back with the tip up, wobbles; `death` shudders, drops tip first,
  the tip strikes the floor, the hilt slaps down as it rolls onto its flat, two small bounces, lies
  still with its lowest vertex on the ground (z extent 0.088 m: on its flat).

Scratch copies on ai (`rig --force` rug, `anim --force` both), before -> after: rug lowest vertex 0
in every clip (before too, standing up), one-shots `from_idle_m` 0 (was 0.098), stretch p99.9
idle/walk/attack/death 1.03/1.09/1.04/2.26 -> 1.22/1.27/1.26/1.35; sword clips 4 -> 5 (`fly` built),
stretch 1.05-1.56 -> 1.00 (rigid), idle lowest vertex 0 -> 0.80 m, death last frame 0, one-shots
`from_idle_m` 0. Clip audit: no flags before or after (the audit has no test for a wall-standing rug
or a bending blade). Archmage, ape, wolf, boar, awakened-shrub, violet-fungus: every `anims/*.glb`
byte-identical; archmage, wolf, awakened-shrub `rig --force` outputs identical.

Still: the sword mesh is 1.58 m tip to pommel (`scale_m` 1.2 was taken on the bounding box of a
diagonal blade), and both rest poses (an upright rug, a backward diagonal sword) show in anything
that draws the model without a clip. The rug folds only along its length (one chain): a sideways
wrap would need a grid (`cloth_grid` proper: 3 parallel chains) and new weights.

### Winged quadrupeds: fused feet (2026-11-07)

The audit of 2026-11-07 flagged 22 of 45 winged quadrupeds (fly stretch 10, death 9, swim 8). The
torn edges of each flagged clip's worst frame, sorted by the parts their vertices' weights mix:
on 13 of them most sit within 0.1 x h of the floor and mix two legs (front and hind foot on one side,
or the two fore paws): the dragons sit with their feet close together and SkinTokens blends the
soles' weights, so fly (front legs +20, hind -25 degrees, IK off), swim (diagonal paddle) and death
shear the bridge. No wing finger was taken for a leg on those (the rig stage's inserted wings sit on
sides the roll has no wing chain: `rig_add_wings: false` would leave those wings unanimated). The
fix is the polar bear's `rig_leg_seams: true` (weights relaxed over the leg-leg seams), set on 14
monsters where it lowered the stretch with no new flag. Tried and dropped (audit worse or flat):
a strict-leg rule taking a chain lying out 1.25 x its drop for a tail or wing finger on winged plans
(adult gold 1.05 -> 1.46, adult silver 2.63 -> 3.79: the rescued legs tore more), and leg seams on
young gold (1.25 -> 1.38), adult gold, adult silver, adult red, hippogriff, adult bronze, young
silver, pegasus, pseudodragon, ancient silver (flat).

Audit on ai copies (`rig --force` + `anim --force` from the kept `rig/chosen.glb`; before = the same
run of the old tree, which reproduces the audit's 135 scores exactly):

| monster | score | stretch p99.9 before -> after |
|---|---|---|
| adult-white-dragon | 7.89 -> 0 | breath 2.5 -> 1.7, hit 2.4 -> 1.4; the death pop and the breath/hit floats go |
| ancient-green-dragon | 5.93 -> 2.87 | swim 8.9 -> 5.8, fly 6.1 -> 4.5, death 6.9 -> 5.0, attack 5.7 -> 3.9 |
| ancient-bronze-dragon | 5.18 -> 2.54 | swim 9.9 -> 5.5, fly 7.5 -> 3.7, death 5.5 -> 3.3 |
| ancient-white-dragon | 3.09 -> 2.63 | swim 7.6 -> 6.1, death 8.3 -> 7.7 |
| ancient-gold-dragon | 2.93 -> 2.22 | swim 7.0 -> 5.2, fly 6.2 -> 4.8 |
| ancient-black-dragon | 2.63 -> 1.22 | fly 5.5 -> 4.2, swim 6.3 -> 5.5 |
| silver-dragon-wyrmling | 2.29 -> 0 | fly 5.4 -> 2.6, death 6.6 -> 3.6 |
| ancient-brass-dragon | 2.26 -> 0 | fly 4.0 -> 2.7 |
| blue-dragon-wyrmling | 2.23 -> 0 | breath 6.4 -> 3.7, death 7.0 -> 5.5, attack 5.6 -> 3.9 |
| sphinx-of-valor | 1.46 -> 1.26 | fly 6.6 -> 5.7 |
| brass-dragon-wyrmling | 1.27 -> 1.07 | fly 5.7 -> 4.8 |
| gold-dragon-wyrmling | 1.22 -> 0 | swim 5.5 -> 4.4 |
| young-blue-dragon, young-brass-dragon | 0 -> 0 | fly 4.0 -> 3.8 / 2.7 |

Plan: 22 -> 17 flagged (fly stretch 10 -> 6). Every other monster of the 135 copied (all quadrupeds,
winged quadrupeds, insectoids and avians, archmage, ape, harpy, centaur): `model_rigged*.glb` and
every `anims/*.glb` byte-identical (no code changed). Still, and why:

- Swim still tears on the fused feet (5.2-6.1): the paddle moves front and hind of a side in
  antiphase; relaxed weights spread the bridge but cannot follow two feet half a body apart.
- Rolls to redo (GPU `--rig-reroll`): ancient-silver (15.2: rearing, the left wing hangs off the neck
  chain and the head bones own part of it, so every head move tears it; 3 legs), adult-red (2 legs;
  the right wing's finger bones own the right fore foot's sole), young-silver and pseudodragon (2
  legs), hippogriff, adult-bronze, brass wyrmling (the inserted wing owns only the lower half of
  its wing: the upper half stays and the two halves part in fly and death).
- 31 of 45 winged quadrupeds fly with one wing (20) or none (11): find_wings and the rig stage's
  lobe search miss a raised wing (it reaches less than WING_REACH sideways). The audit has no flag
  for an unanimated wing.
- remorhaz (58.8): its roll has ~18 one-bone legs and three 4-bone legs, all on the left; the
  longest "leg" is the tail curled over the back (bone_40.., 0.46 of the vertices), and bone_26..29's
  vertices lie on the floor 0.22 x h below the bone (the idle/walk sinks). Dropping the tail from the
  legs (tried) left 60.1. Needs a re-roll, likely a re-mesh first (the tail whip meshed as a wire loop).
- rat (13.7): its "front legs" are two snout chains owning 0-1 vertices, the real fore legs ride the
  body; the tail is the roll's own chain lying on the floor (no inserted chain, so neither the tail
  gate nor `rig_tail_chain: false` applies). The sinks are 6-16 mm on a 0.15 m rat (the audit's
  0.03 x h is 4.6 mm; the stage's size-scaled DIP_MAX is 7.9 mm). Needs a re-roll. No other
  quadruped has a flagged tail at the floor.

## Body plans (2026-11-04)

One pilot per plan the six pilots did not cover took the whole chain (`forge.py run`, paint
included). `rig.RIG_STRATEGY` says which plans skip SkinTokens for a generated skeleton
(`blender_rig.py` mode `fallback`, `manifest.rig.tool` = `fallback:<kind>`, the build report in
`rig/fallback_build.json`, its measurements in `rig/fallback.json`); `--rig-reroll` on such a
plan still rolls SkinTokens first and logs the scores for the record. `anim.LEGLESS` plans get the
"legless" builders in `blender_clips.py` instead of the foot-IK framework; avian and
centauroid stay on the legged framework (centauroid adds the arms via `anim.ARMED`).

| plan | pilot | rig | clips | result |
|---|---|---|---|---|
| serpentine | constrictor-snake | `fallback:spine_chain` (16 bones). SkinTokens: 7-13 joint star, no chain (4 of 4) | `walk`/`swim` slither (curvature wave head -> tail, mid-body root holds place and heading), `idle`, `attack_melee` strike (front third rears in an S, coils, strikes 0.35 x length; round 6 above), `hit`, `death` (front slumps). Ground `contact` | works. (2026-11-04, pose template) The reference is now a side-view snake stretched along the ground (`serpentine_side` depth ControlNet, docs/references.md): mesh 4.0 x 0.40 x 0.56 m, no leveling needed, facing 1.0 degree off; spine_chain 16 bones head to tail, 0 loose vertices (no fused coils); SkinTokens rolls still 10-12 joint stars. Slither sheet: curvature wave in the top view, lowest vertex 0 in every clip. The side-view drawing's S comes out as a gentle vertical wave, so the mid-body arches a few cm off the ground at rest |
| floating | ghost | SkinTokens roll 3 (34 joints): a biped under the robe, legs to the hem, both arms, `hand_r/l` | `idle`, `fly` (lean + hem trailing), `attack_melee` (both arms reach, FK-aimed), `hit`, `death` (falls forward onto the ground; the robe train, flat on the floor behind the feet at rest, folds back at the hem's first bone below 0.15 x the height so it lies flat instead of standing up as a 0.9 m sheet: last-frame top 1.28 -> 0.78 m, body raise 0.30 -> 0.18). All but death `lift`: a constant 0.1 x height above the ground in every clip | works |
| avian | giant-eagle | SkinTokens roll 3 (32 joints): both wing chains, 2 legs, no spine (`problems` spine, leg_pairs) | legged framework: `idle`, `fly` (both wings flap), `attack_melee` (lunge), `hit` (no body tilt for avian), `death` (pitches forward onto its breast, wings fold back). The 2-bone tail fan resting on the ground is turned up instead of the body being raised (Anim stage) | works; `walk` (not in this record's set) builds clean in scratch. The head is the beak (`find_snout` skips wings) and the rig stage stands it on its feet (Rig stage). (2026-11-04) Yaw pinned -150.5 (the facing check's suggested yaw; was 29.5 degrees off at QA's auto 180): facing 0.0, wings symmetric in the front view, legs one per side (was 2 right, 0 left); rig roll_0 of 4 (score -1.70 after the mesh-found wing chains, problem spine only; was roll_3 -3.32 with leg_pairs and spine), grounded (lowered 0.49 m, tail fan turned up 30.8 degrees). (2026-11-05) Avian scales by its largest dimension now (`forge.MAX_SCALE_PLANS`, theropods keep nose-to-tail): wingspan 3.00 m (= scale_m), height 2.97, depth 1.54 (was 5.84 / 5.78 nose-to-tail, 1.39x too big). Re-rolled rig: roll_1 of 4 (56 joints, score -1.38, problem spine only; both wing chains from SkinTokens, 7 bones each, one leg per side, head `bone_5`, facing 0.0); no grounding shift needed. Clips: idle and fly 0.32 m hover as before, attack 0.31, hit 0.37, death ends on the ground |
| amorphous | black-pudding | `fallback:vertical` (4 bones, one straight line through the upper body's bbox centre). SkinTokens: 21-24 joints, 0.83-0.88 bleed | squash and stretch = the root's scale (`vscale`: on the bone axis closest to world up), lean = chain about X. `idle` pulse, `walk` ooze, `attack_melee` engulf (rear up, surge and flatten forward), `hit`, `death` (flattens into a puddle 1.5x wide). Ground `contact` | works |
| plant_object | violet-fungus | `fallback:vertical` (5 bones). SkinTokens roll 3 was clean but rooted at the cap with the stalk hanging down | stalk sway `idle`, `walk` shuffle (rock), `attack_melee`/`attack_special` (coil back, whip forward), `hit`, `death` (topples onto its right side: the root bone, which owns the base clump, stays planted and the stalk breaks over at the next two bones (0.7 / 0.3 of the angle) by the angle that brings the cap onto the ground, bisected in `fit_angle`: 83.6 degrees; the base used to roll over with it and stand on its side). Limbs (tendrils, branches) found off the stalk swing too when the skeleton has them | works (this mesh has no tendrils) |
| tentacled | giant-octopus | `fallback:tentacle_hub`: root + body + 8 tentacles x 5 bones (tips = local maxima of geodesic distance from the mantle). SkinTokens rolls 2-3 also gave a usable hub (58-67 joints), but unnamed | `idle` (phase-shifted curls), `swim`/`fly` (all pulse together, `lift`), `walk` (crawl wave), `attack_melee` (two front tentacles rear and lash), `hit`, `death` (splay flat, body flattens) | works |
| aquatic | hunter-shark | SkinTokens roll 2 (32 joints): spine head to tail, pectoral fins, tail lobes | `swim`/`idle`/`walk` carangiform wave (grows toward the tail), `attack_melee` (dart + head snap), `hit`, `death` (rolls belly-up, sinks to the ground). All but death `lift` 0.25 x height | works |
| centauroid | centaur-trooper | (2026-11-04) a real centaur mesh from the `centaur_34` depth template (docs/references.md; yaw pinned 100.5, facing 0.0 after). SkinTokens roll_1 of 4 (34 joints, score -3.19): 4 legs, spine, head, the right arm only (`hand_l` not placed: an arrow shaft the reference drew behind the head meshed as a 1 m stick out of the left side and takes that arm's place); problems leg_pairs, arms | `idle`, `walk` (four-beat, lowest vertex -0.006..0.001), `attack_melee` (sword cut on the right arm), `hit`, `death`; `attack_ranged` missing (needs both arms). The tail hangs to the ground and swings with the hind legs in the walk sheet; the stick shreds in the sword cut | mesh and rig work; ship needs a clean reference (no shaft) for both arms. Proxy rider before: 3 grounded feet, refused. (2026-11-05) A no-quiver re-reference (docs/references.md rounds 4-5) meshed clean (no stick, the tail apart from the legs) but its arms hang against the body: 8 of 8 SkinTokens rolls found no arm and 5 legs (scores -4.8..-5.4), so the round-2 mesh and rig above are kept. No roll in either mesh (12 rolls) had a tail chain: a tail-chain insertion like the wing chains is rig-stage work. Props: `rider_height_m` 1.8 (override; docs/equipment.md) |
| merfolk_naga | merfolk-skirmisher | SkinTokens (52 joints): spine, tail chain, arms with fingers. Both arms are out sideways, the right one raised: `find_arms` takes them since the raised-arm fallback (sockets `hand_l` bone_16, `hand_r` bone_28, `back` on the chest bone_3, `head` bone_5; score -1.40 -> -0.80, no problems) | `idle`, `swim` (body pitched 55 degrees, tail kicks about X, `lift`), `walk` (tail slithers sideways), `attack_melee`/`attack_ranged` (biped sword cut on the right arm, also for the throw), `hit`, `death` (topples onto its right side; the left arm, on top, drops against the body instead of pointing at the sky, the right one swings up past the head) | works |
| swarm | swarm-of-rats | `fallback:root` (1 bone, all weights 1) | root only: `idle` bob, `walk`/`fly`/`swim` scurry (lift for fly/swim), `attack_melee` surge, `hit`, `death` (scatter and sink: 1.7x wide, 0.3 of the height, a slight slew; a squash to 0.15 flattened the rats) | works; the pilot mesh is a 1.4 m column of rats (the reference draws a wall of them). The batch should model one unit per swarm and leave the swarm to the game (particles or instances) |

Fallback skeletons (`blender_rig.py`):

- `spine_chain`: the two surface points farthest apart along the welded edge graph are the tips;
  joints are the centroids of bands of geodesic distance; the higher (else thicker) end is the
  head. Bones `spine_00` (tail) .. `head`, the root mid-body. Weights linear along the body.
  Touching coils fuse the surface, so the geodesic band runs across into the next coil: a vertex
  farther than 2.5 x the tube radius from its band's bone takes the position along the chain of
  the bone nearest in space (939 of the snake's vertices). Without that, rearing the head lifted
  whole coils into the air.
- `vertical`: a straight chain up from the base through the upper body's bbox centre; weights
  linear in height. Slice centroids wander with a puddle or drips and bend the chain.
- `tentacle_hub`: see above; vertices beyond 0.35 x the farthest geodesic distance go to the
  tentacle whose path is nearest along the surface, blended into the body at the base.
- `root`: one bone.

The pose score was written for SkinTokens rolls; on fallbacks `bleed` and `outside` count by
design (a vertical chain owns a whole puddle), so the fallback scores (-0.15 to -3.1) are not
comparable with roll scores. Symmetry is not scored for the plans with no mirror plane
(`rig.FACING_ADVISORY`), the head not for `rig.HEADLESS`, wings are scored for avian except
`custom.theropod`, arms and head for floating (`rig.UPRIGHT_BODY`).

Facing: the check is advisory (recorded, never refuses) for serpentine, amorphous,
plant_object, swarm and tentacled (`rig.FACING_ADVISORY`), and `blender_rig.NO_HEAD_CUE`
(those plus floating) reports only the axis: the ghost's robe train trails behind and read as
toes pointing backwards (175 degrees "off").

QA: `forge.MAX_SCALE_PLANS` (floating, amorphous, plant_object, swarm, tentacled) scale
`scale_m` to the largest dimension (`blender_qa.py` scale_axis `max`); a ghost scaled by its
depth came out about 3x too tall.

Ground modes (`Clip.ground`): `floor` (legged), `hover` (fly: lowest vertex `HOVER` above the
ground, per clip), `rest` (death), `contact` (legless: lowest vertex on the ground every frame),
`lift` (floating, aquatic, swim: the same constant height in every clip, so clips do not pop
against each other; a frame whose pose dips below the ground is raised by the dip), `none`
(no correction: a rooted fall, where `Clip.fit` bisects the clip's fall angle until the last
frame's lowest vertex not owned by the planted root bone touches the ground; and `burrow`,
which sinks into the ground on purpose). Bone scale layers:
`Clip.scale`, keyed with the rotations.
