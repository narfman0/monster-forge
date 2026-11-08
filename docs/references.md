# Reference images (ref stage)

`forge.py ref <id> [--n 4]` draws `--n` reference candidates (`ref_<i>.png`, SDXL with the
house style, `data/style.json`) and copies one to `ref.png`, which feeds the mesh stage.
Which one: `--pick`, else the `ref_pick` override, else the unarmed check below, else 0.
`manifest.ref.picked_by` says which rule chose (`pinned`, `unarmed_check`, `default`).
A changed prompt (override `prompt_extra` / `negative_extra` / `ref_template`, a `prompt_tweaks.json` entry) needs `ref --force`; `batch.py` re-rolls it by itself (stamped `manifest.ref.prompt_fp`, or `--reroll-changed-prompts` for older references; docs/batch.md "Stale prompt").

## Unarmed check (`unarmed_reference` monsters)

Held weapons and shields are runtime props (`docs/equipment.md`), so the 79 records with
`unarmed_reference` get an empty-hands prompt suffix and a weapon negative. That alone is not
enough: 2 of 4 goblin-warrior candidates still drew a bow and a dagger in the fist. After
the candidates are drawn, `tools/unarmed_check.py` scores each one for "holding something in
a hand" vs "empty hand":

1. DWPose (the `comfyui_controlnet_aux` checkpoints already in ComfyUI, run on the whole
   frame: its YOLOX person detector misses goblins) finds the hands; each is cropped square
   at 1.8x its keypoint box. Fallbacks: the wrist, the A-pose ControlNet image's wrists
   (`--pose-image`, biped and winged_biped), the two sides of the body.
2. SigLIP `google/siglip-so400m-patch14-384` (Apache-2.0, 3.5 GB, downloaded once to the
   Hugging Face cache) scores each crop: hand score = max logit over "a hand holding
   <weapon>" (18 weapons and held objects: pot, jug, rope or cord, sack and stave added 2026-11-06, untested against the goblin) - max logit over four empty-hand prompts. A candidate's score is
   its highest hand; above **2.5** it counts as armed.
3. The pick is the lowest-scoring (emptiest) candidate at or under 2.5 (2026-11-06; it was
   the first one under it, which took sprite's dragonfly at 2.02 and frost-giant's caped
   ref_0 at 0.49 over ref_3 at -3.04; ties go to the lower index; `ref_pick` / `--pick`
   still pin). Records accepted before the change keep their pick: nothing is re-picked until
   it is re-rolled. If every candidate is armed it takes the lowest and logs a warning (`ref.unarmed.warning`): re-roll with another `--seed`, or
   look and pin `ref_pick`.

It runs in ComfyUI's venv (`PIL_PYTHON`) next to the loaded SDXL: ~2 GB of VRAM, ~8 s for 4
candidates including model loading. `--n 1` gets checked too, but with one candidate there
is nothing to switch to, so batch runs of unarmed monsters should use `--n 4`.

Manifest: `ref.unarmed_scores` (one per candidate) and `ref.unarmed` (`model`,
`threshold`, `scores`, `armed`, `best`, per candidate the `hands` with box, score and the
best-matching weapon word, `hand_source`, and `whole`, a whole-image score that is recorded
but does not decide: a quiver or a sheathed sword pushes it up, every goblin candidate
scores 3.7-9.2 on it).

Settings: override `ref_unarmed_check` (default true for `unarmed_reference` records, false
turns it off). A pinned `ref_pick` (or `--pick`) always wins; the check still records the
scores. When the ref stage skips (refs exist, no `--force`) and the manifest has no scores
yet, it scores the kept candidates for the record without touching `ref.png`, and warns when
the candidate in use (pinned `ref_pick`, else the mesh's `ref_picked`) scores armed.

Validation (2026-11-04), hand scores per candidate, armed > 2.5:

| monster | ref_0 | ref_1 | ref_2 | ref_3 | truth | pick |
|---|---|---|---|---|---|---|
| goblin-warrior | 3.35 (bow) | 3.34 (dagger) | -1.54 | -0.82 | 0 bow, 1 dagger, 2-3 empty | 2 (pinned `ref_pick` 2 kept) |
| bandit | -1.48 | 1.46 | -1.80 | 0.04 | all empty | 0 |
| guard | -1.48 | -1.52 | -2.17 | -1.02 | all empty | 0 |
| knight | -0.78 | 7.87 (two swords) | 1.22 | -0.01 | 1 armed | 0 |
| gladiator | -0.38 | -0.04 | 1.90 | -0.63 | all empty | 0 |

20 of 20 right, but the margin is thin on the empty side (bandit ref_1 1.46, gladiator
ref_2 1.90 against the armed 3.3): watch the threshold as the batch adds monsters. Goblin
ref_3 has a spear shaft crossing behind its open hand and scores empty, which is right for
the mesh only if the shaft does not mesh as part of the arm. Known gaps: non-humanoid
"hands" (claws of a beast plan) are untested; the check only looks at hands, so a shield
on the arm or a weapon on the back is not seen (a held shield is usually caught by its hand).

## Pose templates (`ref_template`)

Some body plans cannot be posed by the prompt: juggernaut draws a snake as top-down coils
whatever the words say (5 prompt rounds), and a centaur as a man riding a horse (8 of 8
over three prompts). The ref stage drives controlnet-union-sdxl-promax (already used for
the A-pose) with a per-plan template, `forge.REF_TEMPLATES` / `REF_TEMPLATE_BY_PLAN`:

| plan | template | image | ControlNet type | strength / end |
|---|---|---|---|---|
| biped, winged_biped (in `_frozen.pose_v1_ids`: every one with a reference on 2026-11-08) | `apose` (the style's `controlnet`) | `workflows/pose/apose_openpose*.png` | openpose | 0.6 / 0.6 |
| biped, winged_biped (new references) | rig-friendly, by plan and prompt (below) | `workflows/pose/biped_*_openpose.png` + `_depth.png`, `apose_wide_openpose*.png` | openpose + depth | 0.8 / 0.75 + 0.3 / 0.35 |
| serpentine | `serpentine_side` | `workflows/pose/serpentine_side_depth.png` | depth | 0.75 / 0.7 |
| centauroid | `centaur_34` | `workflows/pose/centaur_34_depth.png` | depth | 0.75 / 0.7 |

The depth templates are drawn by `tools/blender_ref_templates.py` (headless Blender, no
download): a proxy body of tapered capsules rendered orthographically to 1024 x 1024 in the
MiDaS convention (near white, far dark, background black). OpenPose has no keypoints for a
snake or a horse body, so depth it is; no new ControlNet was needed.

- `serpentine_side`: a 4 m snake along X in a shallow S on the ground (0.42 m amplitude,
  0.33 m thick), head raised 0.32 m at +X, camera 20 degrees above the ground.
- `centaur_34`: horse barrel, chest and rump, four legs with every hoof on the ground, a tail,
  a human torso, head and arms (a little away from the body, open hands) rising from the
  chest where the horse's neck would be; camera 65 degrees round from the front, 10 up.

Override `ref_template` (data/overrides): `false` (no template), a template name, or an object
(`image`, `type`, `strength`, `start`, `end`, `model`, `name`) merged over the plan's.
`manifest.ref.controlnet` records `template`, `type`, `strength`, `end`. Only an openpose
template is passed to the unarmed check as its wrist fallback.

Rounds (2026-11-04, 4 candidates each, seeds the per-monster default):

| monster | round | prompt / template | drawn |
|---|---|---|---|
| constrictor-snake | 1 | "side view, long body stretched out along the ground in a shallow wave", 0.75 / 0.7 | 4 of 4 side-view stretched snakes; 3 on sea waves or sand ("wave" read as water), ref_2 on white |
| constrictor-snake | 2 | "gentle S-curve", water/waves/sand negatives | refs 0-2 clean side-view snakes on white, ref_3 a coiled ring. **Pick ref_2** |
| centaur-trooper | 1 | registry-based centaur prompt, 0.75 / 0.7 | 4 of 4 real centaurs (no rider, no horse head, 4 hooves down), but the house style lost: yellow paper backgrounds, a statue and a photo; the human torso turned away in 2; ref_1 one human foot |
| centaur-trooper | 2 | "fantasy centaur", statue/pottery/yellow negatives, override 0.6 / 0.6 | style still lost (paper, studio photos); refs 0 and 3 grew a horse head at the lower strength, ref_1 holds a bow; ref_2 a photo-like centaur facing the camera, 4 hooves down, empty hands, an arrow shaft behind the head. **Pick ref_2** |
| centaur-trooper | 3 | 0.75 / 0.7 again, "manga ink drawing ... plain white background" weighted, no quiver | uncoloured line drawings and mannequins, torsos seen from behind. Worse; not kept |
| centaur-trooper | 4 (2026-11-05) | no quiver / arrows / bow (positive and weighted negative), "both arms held out ... empty open hands", 0.7 / 0.65 | no weapon or quiver in 4 of 4 (paper and photo looks); ref_1 torso to the camera, empty hands, but both arms hang against the body: it meshed clean (no stick, tail apart from the legs) and 8 of 8 SkinTokens rolls found no arm (5 "legs"). Not kept (`prompt_tweaks.json` `centaur-trooper.tried_2026_11_05`) |
| centaur-trooper | 5 (2026-11-05) | "arms spread wide out to the sides", plain white background, 0.65 / 0.6 | arms still down, a horse head (ref_0), masked faces. Worse. The pilot stays on round 2 ref_2 |

The kept config reproduces the picked candidates bit for bit (`ref --force` gave the same
four files). The template wins over the style: the depth map's grey body pulls the
colouring toward photo and paper looks; a style-preserving recipe for centaurs is open.

### One-body multi-head and hybrid templates (2026-11-07)

Prompts alone drew two whole dogs for the death dog, three stacked animals for the chimera,
single-headed lizards for the hydra and a woman riding (or standing beside) a lion for the
sphinx, over 3-5 rounds each. A depth template with ONE body silhouette and the necks where
they belong fixed the body count on the first round, 4 of 4 every time. They are drawn by
`quadruped(body, necks, wings, tail, head_scale, wing_y)` in `tools/blender_ref_templates.py`
(one barrel / chest / rump in lion, dog or reptile proportions, four legs down, Bezier necks
from the top of the chest, each ending in a `head(kind)`: reptile, dog, lion, goat, human,
woman; raised `feathered` or `membrane` wings). A new multi-head monster is a new function
calling it and a `REF_TEMPLATES` entry. They are not tied to a plan: the override picks one.

| template | proxy | used by |
|---|---|---|
| `hydra_34` | low reptile body, short tail, five necks fanned from forward-low to up-and-back; side view 20 deg from the front | hydra |
| `twohead_dog_34` | lean dog, two necks splayed left/right from one chest (far head higher); three-quarter front | death-dog |
| `sphinx_side` | lion body, raised feathered wings, upright neck, `woman` head (no side flaps); side view | sphinx-of-lore |
| `chimera_34` | lion body, three-lobed membrane wings mid-back, goat neck up on the far side, lion head in front, dragon on a long forward neck; side view | chimera |

Override `ref_template` takes a name or `{"name": ..., "strength": ..., "end": ...}` (an
object naming a `REF_TEMPLATES` entry is merged over that entry, 2026-11-07).

Findings: at the default 0.75 / 0.7 the grey proxy pulls the picture to photo / toy /
statue looks (hydra: a green toy on a dark studio background; sphinx: gold-and-blue statues);
**0.6 / 0.5** plus a weighted "(full color manga ink illustration, isolated on a plain white
background:1.3)" and a photo / toy / statue / dark-background negative kept the body and won
the white background back. Parts of the proxy that the prompt does not name are drawn
literally: wing-bone sticks became rods, flat wing slabs became egg shapes, headdress flaps
became floppy dog ears. Per-round prompts and results are in `data/prompt_tweaks.json`
(`tried_2026_11_06`, `tried_2026_11_07_r1`, ...) and the overrides' notes.

| monster | rounds | kept settings | pick | mesh + rig |
|---|---|---|---|---|
| death-dog | 1 | `twohead_dog_34` 0.75 / 0.7 | ref_3: one black hound, two heads, four legs, tail | one body, two heads; facing 3 deg; SkinTokens best roll 3 legs (score -3.4) but the walk drives 4 feet, audit clean |
| hydra | 2 | `hydra_34` 0.6 / 0.5 + white-background weight | ref_2: one crocodile-like body, five heads on five necks (smooth rendered look) | five necks fanned in 3D, feet down; facing 2 deg; walk on 4 legs (small stride), necks unanimated (`custom.hydra`) |
| sphinx-of-lore | 3 | `sphinx_side` 0.6 / 0.5, `woman` head | ref_2: a calm woman's face, cloth bib over neck and chest, tawny lion body, raised feathered wings | facing 0.5 deg; fly clip has frozen wings (the raised-wing defect, docs/rig-anim.md "Raised wings") |
| chimera | 4 | `chimera_34` 0.75 / 0.7 (0.6 / 0.5 put whole goats on its back) | ref_3: one lion body, maned head in front, goat head, red dragon head; white coat, feathered wings, studio-photo look | three heads apart, wings, feet down; facing 2.5 deg; rig 4 legs + 2 wings, no problems (-1.40); fly flaps both wings; painted cream-white, the front head reads as a lamb |

### Rig-friendly poses (2026-11-08)

Image-to-3D fuses parts that touch in the reference, and the rigger cannot pull them apart again:
24 bipeds failed the direct UAL rig check (docs/anim-reuse.md "Extra chains") mostly for that
reason: tails hanging against or between the legs (gargoyle, barbed-devil, dretch, rakshasa,
sahuagin-warrior, wererat, werewolf, weretiger, half-dragon), wings or fringes drawn down beside the
legs (balor, horned-devil, bone-devil, steam-mephit, imp, succubus, sprite, incubus), robes
(cloud-giant, night-hag, deva, solar) and fit failures (treant, medusa, shambling-mound). The pilot
re-drew them with a pose that keeps every appendage clear.

**Templates** (`forge.REF_TEMPLATES`, `tools/blender_ref_templates.py` `biped(wings, tail, small)`,
`tools/pose_skeleton.py`):

| template | pose | images |
|---|---|---|
| `apose_wide` | arms 45 degrees from the body (house A-pose 35), each ankle 0.08 x height further out | `apose_wide_openpose{,_small}.png` (OpenPose only) |
| `biped_wings_front` | wide A-pose, two membrane wings from the upper back raised up and out, tips above the head, the lower edge above the shoulders | `biped_wings_front{,_small}_{openpose,depth}.png` |
| `biped_tail_front` | wide A-pose, a tail held out from behind the hips to the figure's left at hip height, under the hand, its end curving down well outside the foot | `biped_tail_front{,_small}_{openpose,depth}.png` |
| `biped_wings_tail_front` | both | `biped_wings_tail_front{,_small}_{openpose,depth}.png` |

The depth proxy is rendered front-on, and the same script writes `<name>_keypoints.json` (the
proxy's COCO-18 joints projected by the same camera), from which `pose_skeleton.py --keypoints`
draws the matching OpenPose image, so the two ControlNets agree. Tiny and small creatures get the
stocky `_small` proxy (bigger head, shorter legs). A template may now chain more ControlNets:
`extra: [{image, type, strength, start, end}]` (`forge.apply_template`; manifest
`ref.controlnet.extra`; the override `ref_template` accepts `extra` too, replacing the template's).
Template images may be per size (`{"default": ..., "tiny": ..., "small": ...}`).

**Default.** `forge.rig_friendly_template`: a winged biped gets wings, a biped whose registry
prompt names a tail gets the tail (both for a tailed winged biped), any other biped `apose_wide`,
except the ids in `prompt_tweaks.json` `_frozen.pose_v1_ids` (the 120 bipeds that had a reference
when the default changed, minus the ones re-rolled here), which keep the house `apose` and none of
the rules below, so no existing prompt or batch fingerprint changes (checked on all 330 records).
An id whose creature has a tail its prompt does not name (gargoyle, werewolf) sets `ref_template`
in its override. Rules (`_rules`, match key `templates` = the pose template's name, new; the pose
words follow the pose ControlNet):

| rule | templates | insert / negative |
|---|---|---|
| `pose_legs_apart` | all four | "(legs apart, feet planted wide apart, arms held out away from the body:1.2)" / legs together, feet together, crossed legs, arms at the sides |
| `pose_tail_out` | tail ones | "(tail held out to the side, away from the legs:1.2)" / tail between the legs, wrapped round a leg, touching the legs, coiled on the ground |
| `pose_wings_up` | wing ones | "(wings spread up and out above the shoulders:1.2)" / folded, drooping, hanging beside the legs, touching the arms |
| `pose_style_guard` | all four | "(full color manga ink illustration, isolated on a plain white background:1.3)" / photo, toy, action figure, figurine, insect, exoskeleton, naked mannequin |

The `hands_free`, `house_ground` and `no_carried_gear` rules skip `_frozen.ids`; a re-rolled id
leaves that list (and `pose_v1_ids`), so they apply to it again.

**Rounds** (gargoyle, imp, balor, werewolf; 4 candidates each):

| round | settings | drawn |
|---|---|---|
| 1 | OpenPose 0.8 / 0.75 + depth 0.45 / 0.5, no guard | pose right (tail out, wings up, feet apart) but photo / toy looks, insect wings (the proxy's membrane lobes), mannequin bodies; imp a red-and-black toy insect, werewolf without its clothes. (This round fed the depth map to BOTH ControlNets: the upload bug below) |
| 2 | OpenPose only 0.8 / 0.75 | wings up, feet apart, bat wings back; tails mostly not out |
| 3 | both, depth 0.3 / 0.35 vs 0.45 / 0.5, + `pose_style_guard` | the house ink look and white backgrounds back; at 0.3 / 0.35 the tails still stand clear and the likeness is closer (the werewolf keeps its jacket). Kept |

**Upload bug (fixed).** `forge.upload_file` reused its `name` argument as the form-field loop
variable, so every upload was stored as `<subfolder>/overwrite`: two ControlNets in one graph read
one image, and paint has always fed the colour render for all its inputs (docs/paint-stage.md).

**Mesh-side finding.** The sideways tail meshes free of the legs (werewolf: straight out behind),
but `blender_tail_chain.detect_tail` reads a tail off the rear profile in horizontal bands and only
finds a hanging one ("1 tail band"). `detect_tail_radial` (opt-in, `radial`, used by the UAL rig's
`rig_ual_add_tail` only): the largest surface-connected lobe at most 0.12 h above the hips, 0.09 h
or more from every leg and arm joint path and 0.11 h from the hips' vertical line, with the joints
at the centroids of radial bands from its root (werewolf: 8 bands, 0.44 h). On that path a `find_tail` chain that
runs into a hand, grip or head no longer counts as an existing tail (on upright skeletons find_tail matched an arm,
so weretiger got no tail at all). The rig stage's quadruped tails are unchanged.

**Results** (all 24, one round of 4 candidates each, picked by eye; then mesh, qa, paint, lod, the SkinTokens rig
and the UAL variants: plain heat, auto extra chains with SkinTokens / heat / whole-body SkinTokens weights, an inserted
tail. "UAL" = clips the gate keeps on UAL (retarget_ual.verdict against the same mesh's SkinTokens procedural clips;
stale `anim_ual_exclude` of the old meshes dropped), best variant; old = best of the 2026-11-08 extra-chain runs.
Sheets `~/.cache/monster-forge-scratch/posepilot/sheets/cmp_<id>.png` (old / new ref and mesh, both UAL sheets),
candidates `r_final4.png`, `r_a.png`, `r_b.png`, `r_c.png`):

| id | pick | UAL old -> new (max p99.9 new) | SkinTokens score old -> new | outcome |
|---|---|---|---|---|
| gargoyle | ref_0 | 2/6 -> 6/6 (4.6) | -2.43 (3 legs) -> -0.35 | **replaced**: no display base; tail rides the right thigh without tearing |
| balor | ref_0 | 3/6 -> 6/6 (3.8) | -1.44 -> -0.47 | **replaced**: no fringe between the legs |
| werewolf | ref_1 | needs_skintokens -> 6/6 (2.5) | -1.80 -> -0.84 | **replaced**: tail out behind, own chain (radial); keeps jacket and trousers; facing check off |
| sahuagin-warrior | ref_0 | 3/6 -> 6/6 (3.7) | -1.38 -> -1.66 | **replaced**: tail chain (auto extras, heat) |
| weretiger | ref_1 | needs_skintokens -> 6/6 (3.7) | -1.52 -> -1.07 | **replaced**: tail chain inserted (after the find_tail arm fix) |
| horned-devil | ref_3 | 1/5 -> 5/5 (4.7) | -0.72 -> -1.73 | **replaced**: wing chains + inserted tail; dark slate body, red wings |
| steam-mephit | ref_2 | 1/6 -> 6/6 (3.9) | -1.18 -> -0.13 | **replaced**: SkinTokens weights + wing chains; darker navy than the old cyan |
| succubus | ref_3 | 3/6 -> 6/6 (2.0) | -0.96 -> -0.14 | **replaced**: wing chains |
| treant | ref_0 | 1/6 (floats) -> 6/6 (4.7) | -0.61 -> -0.58 | **replaced**: fit auto; root legs apart instead of a trunk |
| imp | ref_3 | 0/6 -> 6/6 (9.2) | -1.77 (3 legs) -> -0.65 | held: the ink ground line meshed as a bar under the feet, plus a loose stick; a re-roll with another seed drew white insect bellies |
| dretch | ref_3 | 2/6 -> 6/6 (3.2) | -0.17 -> -1.44 | held: lean and muscular, the pot belly lost |
| half-dragon | ref_2 | 2/6 -> 6/6 (2.7) | -1.49 (3 legs) -> -0.60 | held: the plate armour gone, paint drifted to red |
| incubus | ref_0 | 0/7 -> 7/7 (3.3) | -1.04 -> -0.55 | held: "fine clothing" gone, a bare pale figure |
| sprite | ref_1 | 3/5 -> 2/5 | -1.91 -> -1.27 | held (rig not better); **the old sprite mesh is topless**, the new one wears a leaf bodysuit |
| barbed-devil | ref_1 | 2/6 -> 1/6 | -1.11 -> -1.18 | not better: the dense barbs tear |
| rakshasa | ref_0 | 3/7 -> 4/7 | -0.59 -> -0.83 | not better; the coat opened |
| wererat | ref_1 | 5/6 -> 4/6 (fit auto) | -0.94 -> -1.23 (1 arm in 8 rolls) | not better: the tail out under the right hand merged with the arm for SkinTokens |
| bone-devil | ref_1 | 1/6 -> 0/6 (fit broken, 29 at idle) | -1.37 -> -2.37 (spine) | not better |
| medusa | ref_1 | 4/6 -> 0/6 | -3.20 -> -0.80 | held: procedural clips much better on the new SkinTokens rig, but loose snake pieces lie at the feet; the snakes are still round the body, not hair |
| shambling-mound | ref_1 | 4/6 -> 1/6 (fit auto) | -0.37 -> -0.34 | not better |
| cloud-giant | ref_2 | 2/7 -> 7/7 (3.5) | -0.49 -> -0.10 | robe: user's call (the wide stance opened the robe over trousers) |
| night-hag | ref_3 | 4/7 -> 5/7 | -0.59 -> -1.00 | robe: user's call (robe open at the legs) |
| deva | ref_3 | 5/7 -> 7/7 (4.1) | -2.00 (4 legs) -> -0.45 | robe: user's call (drapes over a bodysuit; refs 0 and 2 near nude) |
| solar | ref_1 | 6/7 -> 7/7 (7.3) | -1.68 -> -1.40 | robe: user's call (gown open at the legs) |

Findings: wings are not the problem they looked like (the old references already raised them; what fused was
legs, fringes and tails), and a raised wing pair plus a tail held out gives SkinTokens the right leg count (gargoyle,
imp, half-dragon, deva: 3-4 legs -> 2). Tails: held out behind meshes best (werewolf); out at hand height can merge
with the arm (wererat). The pose words also change the costume: long garments open over the legs (robes, coats) and
some clothing is dropped (incubus, half-dragon armour), so likeness needs the per-id words kept strong.

## Unarmed check subject (name with a weapon word)

`forge.py` passes the display name ("a kuo-toa whip") to `tools/unarmed_check.py` for the
whole-image prompts. A name with one of the check's weapon words (`WEAPONS`: dagger, sword,
bow, ..., whip, as a whole word) is replaced by the first clause of the monster's
`data/visual_profiles.json` description (`--desc`, e.g. "a kuo-toa priest") when that has no
weapon word, else by "a creature"; the output keeps it as `name`, the given one as
`name_given`. The hand-crop prompts, which decide, never used the name, so scores of
monsters without weapon words are unchanged (goblin-warrior re-run on its four candidates with `--desc`: subject "a goblin warrior", scores 3.349 / 3.335 / -1.537 / -0.824, identical to the manifest's).

## Per-plan prompt tweaks (`_plans`)

`data/prompt_tweaks.json` has per-id entries (`prompt` | `prepend`, `negative_append`) and,
under `"_plans"`, per-body-plan entries applied first (`forge.ref_prompt`):

- `replace` {old: new} on the registry `reference_prompt` (the registry builds the pose
  words per plan, so a plan entry swaps them), then `prepend` / `append`;
- `negative_append` (plan, then id: both apply);
- `except_templates`: rig templates the plan entry skips;
- a per-id `prompt` replaces the whole subject (the plan's replace / prepend / append are then
  moot, its negative still applies).

`manifest.ref.tweaks` lists what applied (`plan:avian`, `id:ghoul`).

**avian** (all but `custom.theropod`): "neutral standing stance with wings partly spread,
side view" becomes "(front view, body facing the viewer:1.2), standing upright with both feet
on the ground, (both wings spread wide open to the left and right:1.3), symmetrical pose";
negative side view / folded wings / flying / perched. 2026-11-05, 4 candidates each:

| record | drawn | pick | mesh + qa |
|---|---|---|---|
| giant-vulture | 4 of 4 front view, both wings spread, both feet down | ref_1 (most symmetric) | wingspan 2.40 m (= scale_m, the avian max-dimension rule), height 1.96 m, facing 0.5 deg off +Y at QA's auto yaw, feet on the ground |
| raven | 4 of 4 wings spread, feet down; refs 0 and 2 three-quarter, ref 3 head turned | ref_1 (front, symmetric) | not meshed |
| swarm-of-bats (bat unit) | 4 of 4 front-view bats, wings spread, feet hanging | ref_2 | see Swarm units |

The ghost family (specter, wraith) uses per-id copies of the ghost's legless-robe tweak rather
than a plan entry (the floating plan also holds elementals, the cloaker and the wisp).

| monster | round | drawn | pick |
|---|---|---|---|
| specter | 1 | "shadowy apparition, wisps of shroud": grey fog, the colour style lost, legs or claws under the shroud 3 of 4 | - |
| specter | 2 | + legs / fog negatives, blue-violet: bare feet 4 of 4 | - |
| specter | 3 | "long tattered flowing robes that hide the feet" (the ghost pilot is a floor-length robe too) | refs 0, 2, 3 robes to the floor, ref 1 feet. **ref_3** (arms out, like the ghost pick) |
| wraith | 1-2 | hooded black robes, red eyes, 4 of 4; refs 0 and 2 shoes, refs 1 and 3 floor-length robes | **ref_3** (arms out) |
| ghoul | 1 | full prompt "undead ... rotting flesh, not a skeleton": skull heads on a statue base 4 of 4 | - |
| ghoul | 2 | no "undead"; "ashen grey skin over the whole body and face, a fleshy face", skull / statue / diorama negatives | 4 of 4 fleshy grey clawed humanoids, empty hands (unarmed check -4.2..-4.6, all empty; run by hand, the ghoul has no `unarmed_reference`), painterly on grey. **ref_2** (no horns); mesh 1.8 m, facing 0.5 deg |

## Table rules and the frozen list (`_rules`, `_frozen`, 2026-11-05)

The tier-2 review (docs/review-tier2.md "Systematic findings") found families of failures
that one table entry fixes better than an override per monster. `forge.ref_prompt` applies,
in order: the plan entry, the matching `_rules` (`forge.tweak_rule`), the per-id entry.
A rule matches on `plans`, `ids`, `id_match` (regex on the id), `types`, `prompt_match`
(regex in the registry prompt) and `unarmed_reference`, minus `except_ids`; it may `replace`,
`insert` (placed before the registry's ". Low-poly game asset" tail, so the style's
`strip_from_subject` still matches) and `negative_append`. `by_group` is the generator:
`id_match`'s group picks a sub-entry (one rule covers every dragon colour). A per-id `prompt`
makes the rules' `insert` moot; their negatives still apply. `manifest.ref.tweaks` lists
`rule:<name>`.

**`_frozen.ids`** (216 on 2026-11-05) are the records with an accepted reference: every
SRD record with `ref.png` except the 20 being re-rolled for a known problem (the 14 tier-2
re-rolls, shield-guardian, medusa, ettin, death-dog, chimera and the unreviewed
ancient-brass-dragon). A plan, rule or per-id entry with `skip_frozen` passes them by, so
the new rules leave their prompt (and the batch's fingerprint) unchanged and nothing good is
re-rolled. The older entries (`_plans.avian`, ghoul, specter, ...) have no `skip_frozen`
and apply as before. Drop an id from the list to let the rules in at its next re-roll.
**`_frozen.pass5_ids`** (71, 2026-11-06) are the pass-5 acceptances (the ready-for-paint lists
of `docs/review-tier2.md` Pass 5 and `docs/review-tier3.md` that were not in `ids`, plus the
re-pick / qa-pending records): their references were drawn WITH the older rules, so adding them
to `ids` would have dropped those rules from their prompt (49 prompts changed, 79 stale refs).
They are skipped only by entries marked `"skip_frozen": "pass5"` (the rules below written after
the review); the older `true` entries still apply, so their prompts stay byte-identical.
adult-silver-dragon, badger and weasel left `ids` (they need a re-roll). `except_ids_after_pass5`
on a rule excludes ids except pass-5 acceptances (bone-devil in `fleshy_undead`). `sizes` is a
rule match key. Generated (with the dragon colour table) by
`~/.cache/monster-forge-scratch/prompts/gen_tweaks.py`, not tracked.

| entry | what | table / live (not frozen) |
|---|---|---|
| `_plans.quadruped` | "neutral stance, side view" -> "(standing on all four legs:1.2), side view"; negative sitting / on haunches / rearing / standing upright, and rider / person / human / saddle / reins / bridle | 61 / 9 |
| `_plans.winged_quadruped` | "(standing on all four legs:1.2), wings partly spread, side view"; negative rider / saddle / reins (no person / human: sphinx heads), sitting / rearing | 49 / 14 |
| rule `house_ground` | negative "(cast shadow, floor, ground plane, ground patch, grass mound, pedestal, base, plinth, platform, rock base, puddle, reflection:1.2)": the workflow negative already had cast shadow, rocks, grass, scenery; the style drop / ground shadow and shadow under feet. A rule rather than `data/style.json`: the style is in every prompt and would re-roll all 216 | 330 / 114 |
| rule `dragon_colour` | the 40 chromatic and metallic dragon ids: "(<colour> scales all over the body:1.3), (<colour> wing membranes:1.2)" and the colours it drifts to, negated (black: green / teal / blue, orange wings; red: blue / black / teal, fire on the ground; metallics: teal / green / blue) | 40 / 11 |
| rule `fleshy_undead` | registry prompts with corpse / undead / rotting / skeletal / emaciated / stitched / withered words, not the skeletons, mummies, lich, the incorporeal ones or shambling-mound: "(intact skin and flesh, no exposed bones:1.2)", negative bare skeleton / ribcage / bones | 10 / 2 (bone-devil, crawling claw) |
| rule `no_carried_gear` | `unarmed_reference` records: negative quiver / arrows / weapons on the back / sheathed weapon | 79 / 15 |
| rule `insectoid_side` (pass5) | insectoid plan: "(side view, seen from the side, standing on its legs:1.3)"; negative top-down / overhead / seen from above / bird's eye (chuul, scorpion, crab, beetle, ankheg drew top-down) | 15 / 2 (chuul, scorpion) |
| rule `aquatic_side` (pass5) | aquatic plan: "(swimming horizontally, side view, whole body in frame:1.3)"; negative head-on / breaching / leaping out of the water / front view / jumping | 9 / 3 (giant-shark, killer-whale, plesiosaurus) |
| rule `tiny_fur` (pass5) | quadruped plan, size tiny: "(simple smooth fur, soft rounded body, no whiskers, no stray hairs:1.2)"; negative whiskers / fur strands / spiky fur / hair tufts (shard-shell meshes) | 6 / 2 (badger, weasel) |
| rule `giant_brute` (pass5) | cloud / fire / frost / hill / stone / storm giant: "(a huge muscular warrior-sized brute, bare muscular arms, tunic, not a robe, not a wizard, not old, no long beard:1.3)"; negative robe / wizard / sage / old man / long white beard / staff | 6 / 1 (hill-giant) |
| rule `single_head_dragon` (pass5) | black and red dragons: negative "two heads, second head" | 8 / 0 (adult-black-dragon and ancient-red-dragon, which grew one, are accepted re-picks) |
| per-id `prompt` (2026-11-06) | hydra (five snake-like necks from one body, "(five heads:1.5)"), chimera and sphinx-of-lore strengthened (one body, necks, "not a bust, not a portrait", lion from head to tail); sphinx-of-valor left alone (accepted re-pick 2) | 3 |
| per-id `prompt` | riding-horse, warhorse (a bare horse: no saddle, no barding), warhorse-skeleton, sphinx-of-lore, sphinx-of-valor, chimera (three heads named), ankylosaurus, triceratops, giant-crocodile, medusa (snakes on the head only), ettin and death-dog ("two heads" up front, two bodies negated); griffon, hippogriff, pegasus wait (`skip_frozen`: their re-picks are on-model) | 15 / 12 |

Registry source fix (`data/visual_profiles.json`, rebuilt): frost-giant, solar and sprite no
longer name a quiver. The other 26 wielders (centaur-trooper through its own per-id prompt) whose prompt names a scabbard, quiver, bolt case
or knife sheaths are frozen (their references were judged good with empty hands; worn gear is
baked by docs/equipment.md) and keep their prompt; edit their desc when one is re-rolled.

## Swarm units

A swarm is made as ONE unit; the game multiplies it. `forge.SWARM_UNITS` maps each swarm to
its unit, resolved once per record in `forge.main` (`resolve_swarm_unit`), so ref, mesh, qa,
rig and anim all see the unit: its `body_plan`, `rig_template`, `size` (tri budget, anim
thresholds), `scale_m` and `reference_prompt`, no hands or sockets. The swarm's
`animation_set` is kept. `unit` borrows a registry record; `plan`, `rig_template`, `size`,
`scale_m`, `prompt` set or override (`scale_m` default 0.3 m, a Tiny unit). The manifest
records `swarm_unit` {unit, plan, rig_template, size, scale_m, swarm: {body_plan, size,
scale_m}} and the unit's `body_plan` / `scale_m` / `size`. Override `swarm_unit`: false (make
the swarm as a swarm) or an object over the table entry.

| swarm | unit | plan / template | scale_m |
|---|---|---|---|
| swarm-of-bats | bat | avian / custom.bat_wing | 0.3 |
| swarm-of-ravens | raven | avian / rigify.bird | 0.4 |
| swarm-of-piranhas | piranha | aquatic / rigify.shark | 0.3 |
| swarm-of-venomous-snakes | venomous-snake | serpentine / custom.spine_chain | 1.0 |
| swarm-of-insects | (a beetle prompt) | insectoid / custom.hexapod | 0.3 |
| swarm-of-crawling-claws | (a crawling hand prompt) | plant_object / custom.limb_chain (as the troll limb, untested) | 0.3 |

swarm-of-rats is not in the table: its pilot is a column of rats made as `swarm` and is not
re-run; add `{"unit": "rat"}` (override or table) when it is regenerated (docs/TODO.md H).

2026-11-05: swarm-of-bats 4 of 4 single front-view bats (avian plan tweak), ref_2: mesh 0.30 m
wingspan, facing 0.0; rig (avian path, 4 rolls) roll_1 score -1.48, 47 joints, 2 legs, both
wing chains (problem spine only, as the eagle); anim 5 clips (idle, fly, attack_melee, hit,
death), fly flaps both wings, idle/fly min_z 0.06 m (the avian hover), death on the ground.
swarm-of-insects 4 of 4 single beetles, ref_2 (side view): mesh 0.30 m long; QA's auto yaw
(pca_upper_mass_end) faced it backwards and the facing check (head_known false) passes either
way, so `yaw` is pinned 118.6; not rigged.
