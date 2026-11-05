# Reference images (ref stage)

`forge.py ref <id> [--n 4]` draws `--n` reference candidates (`ref_<i>.png`, SDXL with the
house style, `data/style.json`) and copies one to `ref.png`, which feeds the mesh stage.
Which one: `--pick`, else the `ref_pick` override, else the unarmed check below, else 0.
`manifest.ref.picked_by` says which rule chose (`pinned`, `unarmed_check`, `default`).

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
   <weapon>" (13 weapons) - max logit over four empty-hand prompts. A candidate's score is
   its highest hand; above **2.5** it counts as armed.
3. The pick is the first candidate at or under 2.5 (candidates are otherwise exchangeable,
   and the lowest score is not the most on-model one). If every candidate is armed it takes
   the lowest and logs a warning (`ref.unarmed.warning`): re-roll with another `--seed`, or
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
| biped, winged_biped | `apose` (the style's `controlnet`) | `workflows/pose/apose_openpose*.png` | openpose | 0.6 / 0.6 |
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

The kept config reproduces the picked candidates bit for bit (`ref --force` gave the same
four files). The template wins over the style: the depth map's grey body pulls the
colouring toward photo and paper looks; a style-preserving recipe for centaurs is open.

## Unarmed check subject (name with a weapon word)

`forge.py` passes the display name ("a kuo-toa whip") to `tools/unarmed_check.py` for the
whole-image prompts. A name with one of the check's weapon words (`WEAPONS`: dagger, sword,
bow, ..., whip, as a whole word) is replaced by the first clause of the monster's
`data/visual_profiles.json` description (`--desc`, e.g. "a kuo-toa priest") when that has no
weapon word, else by "a creature"; the output keeps it as `name`, the given one as
`name_given`. The hand-crop prompts, which decide, never used the name, so scores of
monsters without weapon words are unchanged (goblin-warrior re-run on its four candidates with `--desc`: subject "a goblin warrior", scores 3.349 / 3.335 / -1.537 / -0.824, identical to the manifest's).
