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
