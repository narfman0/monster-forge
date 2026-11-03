# House art style for reference images (2026-11-03)

Decision: **`manga-ink-v1`**, prompt-only on juggernautXL_v9 (no LoRA), plus an OpenPose
A-pose ControlNet (controlnet-union-sdxl-promax, strength 0.6, end 0.6) for `biped` and
`winged_biped`. Spec: `data/style.json`; `tools/forge.py ref` applies it by default
(`--style none` gives the old unstyled prompts, `--style <file>` another style).

Sheets (git-ignored, under `out/srd-5.2.1/_style/`): `final/contact_final.png` (the 6 picks),
`sheet_<variant>.png` (every variant, 6 pilot monsters x 3 seeds, 1024², 30 steps, CFG 5.5),
and `out/srd-5.2.1/_contact_style.png` (meshes built from the picks).

## How a styled prompt is built

1. Subject: the registry `reference_prompt` (or `data/prompt_tweaks.json`), with
   `"Low-poly game asset,"` and the old `"plain gray background, ... flat even lighting."` tail
   removed (`strip_from_subject`). The low-poly phrase bakes facets into the albedo.
2. Plus `, ` and `positive_suffix`: *full color manga ink illustration, Kim Jung Gi style dense
   ink linework, dynamic brush pen strokes, bold black outlines, vibrant saturated flat colors,
   flat color fills, evenly lit, flat lighting, no shadows, isolated on plain white background*.
3. Negative: the base negative from `workflows/ref_image.api.json`, the style `negative`
   (photo/3D-render/chiaroscuro/shadow terms, weighted `monochrome` and `nude` terms, artist
   seals, ink splatter, smoke and fire), then the per-monster `negative_append`.

## Variants tried (all juggernautXL_v9, same seeds)

| variant | result |
|---|---|
| a_inkcel: "manga illustration, ink linework, cross-hatching, cel-colored" | clean, flat and consistent, but reads as Western comic colouring rather than brushy ink |
| b_kjg: "in the style of Kim Jung Gi" | the artist name adds pen texture; washes out colour (white dresses, pale owlbear), stray signatures |
| c_brushpen: "dynamic brush pen drawing, comic inking, character design sheet" | drew a literal brush pen in one wolf frame; signatures and torn-paper borders |
| d_seinen: "Kim Jung Gi and Katsuya Terada, seinen manga" | densest ink; kanji seals, pale near-white harpies |
| e/f_mangaink (CFG 5.5 / 7) | best balance; CFG 7 is no better. **Harpy came out topless** |
| g + weighted nude negative, j + weighted monochrome negative and no fire | fixed colour drift (wolves stay coloured) and mostly fixed the harpy |
| l_final = j + pose ControlNet + subject tweaks | chosen |

Every variant drew Yoda goblins, yellow owl-only owlbears, teal-bodied dragons with red
wings, and spiders seen from the front. Those are subject problems, fixed in
`data/prompt_tweaks.json` (goblin: green skin, toothy grin, `yoda` negative; owlbear: weighted
dark brown fur; harpy: rewritten clothed, monstrous prompt; dragon: weighted red scales).
The spider front view is kept, since it meshes well.

## LoRA verdict: not worth it

The Kim Jung Gi, "Hand Drawn Brush Pen" (setz) and "jpen_bold" LoRAs on Civitai need a logged-in
download, so they were not tested. Two LoRAs with anonymous downloads were tested (sha256 checked against the Civitai API, in
`~/workspace/ComfyUI/models/loras/`):

| LoRA | license | strengths | verdict |
|---|---|---|---|
| `lineartSDXL.safetensors` "Line Art + Flat Colors SDXL" (civitai 134955, v148673, sha256 b7477191...99ab) | images may be sold, no credit needed, derivatives allowed | 0.6, 1.0 | flatter, less-Yoda goblins and a more bear-like owlbear, but **nude harpies at both strengths** and duller colour. Rejected |
| `tokiame_xl_v1.safetensors` "Tokiame Inking Style XL" (civitai 306850, v344436, sha256 ca7234db...73e9) | images may be sold, no credit needed, derivatives allowed | 0.5, 0.8 | more brushy, manga-like inking, but it drains the colour (monochrome harpies, white wolves) and adds seals. Rejected |

Prompting alone gives a consistent ink + bold-colour look, and both LoRAs cost either colour or SFW safety.

## Pose control verdict: use it for upright plans

`workflows/pose/apose_openpose*.png` (drawn by `tools/pose_skeleton.py`, standard COCO-18
colours; a shorter, stockier skeleton for tiny/small) feed controlnet-union in openpose mode.
Strength 0.4 already spreads the arms, and 0.6 gives a clean symmetric A-pose with arms clear of the body
on all goblins and harpies, with no loss of style. Without it, juggernaut ignores "A-pose"
(the goblin draws its bow). Side effects: cream backgrounds and coloured ground shadows,
which the background and shadow negatives remove. Quadrupeds, spiders and dragons get no ControlNet.

## Known issues / what to watch in batch runs

- Some frames still show faint ground shadows or a pale vignette (BiRefNet removes them).
- The style says "white background", but goblin frames come out with a light-grey vignette. Harmless.
- Hybrid monsters still need per-monster tweaks; budget a human pass over reference contact sheets.
- Ink lines survive into the baked albedo as dark creases, which suits the look but may
  fight the normal map in close-ups.
