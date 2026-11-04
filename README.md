# monster-forge

Low-poly, game-ready 3D assets (mesh, PBR texture, rig, animation clips) for every
D&D monster, generated locally with ComfyUI and Blender and consumed by Godot 4.

Scope tiers (see `docs/corpus-plan.md`):

1. SRD 5.2.1 (CC-BY-4.0): 330 monsters, registry in `data/registry/srd-5.2.1.json`.
2. Monster Manual 2024: the 173 stat blocks not in the SRD, unreviewed stubs in
   `data/registry/mm-2024.json` (`tools/import_names.py`; WotC IP, personal use only,
   generated locally and not committed).
3. Other official books (2024-2025 first, then legacy 2014-era books).

Non-SRD monsters are WotC IP: personal use only; a public pack ships SRD assets or
renamed analogues.

## Layout

- `data/registry/` one JSON record per monster: body plan, rig template, animation set,
  reference prompt, scale, hands, wielded props and sockets (`data/registry/README.md` has the schema).
  `props.json` lists the held weapons and shields, which are separate runtime props (`docs/equipment.md`).
- `data/equipment.json` hand-reviewed hands and weapons per monster, plus prop specs.
- `data/body_plan_overrides.json`, `data/body_plan_reviewed.json` hand-curated taxonomy.
- `docs/plan.md` pipeline design and milestones. `docs/research-*.md` tool research.
- `tools/` scripts. `workflows/` ComfyUI API workflows. `viewer/` local web viewer.
- `out/<source>/<id>/` generated assets (git-ignored; never commit art).

## Environment

RTX 5070 Ti 16 GB, ComfyUI at `../ComfyUI` (Python 3.12, torch 2.14 cu130, port 8188),
Blender 5.2.2 LTS. Rigging and motion tools each get their own `uv` venv under `tools/`.

## Running the pipeline

```sh
python3 -I tools/forge.py run <id>...          # ref -> mesh -> qa -> paint -> lod -> rig -> anim, resumable (--force to redo)
python3 -I tools/forge.py run <id> --stages qa,lod --force   # redo some stages
python3 -I tools/forge.py rig <id> --rig-reroll    # new SkinTokens rolls; plain --force keeps rig/chosen.glb
python3 -I tools/forge.py contact <id>... --name batch1
```

Needs the ComfyUI server on 8188 with the models listed in `docs/pilot-report.md`.
Per-monster settings (tri budget, upsample, yaw, leveling, prompt extras, LOD ratios, ...)
go in `data/overrides/<source>/<id>.json`; QA levels the stance so all feet touch the
ground, `lod` writes `model_lod1-3.glb` from `model.glb` (`docs/lods.md`), `rig` writes
`model_rigged.glb` and `model_rigged_lod1-3.glb` (needs ~14 GB VRAM and the SkinTokens venv
in `tools/skintokens/`), and `anim` writes the procedural clips of the registry's `animation_set` (idle, walk, attack,
hit, death; fly and breath where expected) to `anims/<clip>.glb`.

Held weapons and shields are separate props (`docs/equipment.md`): `python3 -I tools/props.py
run --all` turns `data/registry/props.json` into `out/props/<id>/model.glb` (origin at the
grip, +Y toward the tip, -Z the edge or arrow direction, scaled to `length_m`) plus LODs, a
4-view axes sheet and a manifest; `tools/props.py contact` makes `out/props/_contact.png`.
Per-prop overrides go in `data/overrides/props/<id>.json`.

## Viewer

```sh
python3 -I tools/viewer.py            # http://127.0.0.1:8787/ (--port, --source, --no-open)
```

A local web page (stdlib server, vanilla JS, vendored three.js) for checking progress and
assets: stage counts per body plan, a filterable monster list, a three.js view of
`model.glb` with a 1.8 m scale figure, wireframe/texture toggles, and animation playback
for embedded clips and `anims/<clip>.glb` files with a clip selector, an LOD switcher, a per-monster overrides
form and a re-run job queue (`forge.py run --stages ...` in the background). On rigged models it can show the
skeleton, socket markers and placeholder sword and bow props following the clip. The stage-status rules that `forge.py` has
to follow (`rig`, `anim` in `manifest.json`) are in `viewer/README.md`.

## Publishing (public viewer)

`python3 -I tools/release.py <id>...` marks a monster released (`"released": true` in its
manifest; `--unset` takes it back, `--list` shows them). `python3 -I tools/publish.py --dest
../monster-forge-viewer --clean` then copies the released monsters' game files (rigged LOD
chain, `anims/*.glb`, preview image, trimmed manifest), the props they wield, the viewer and
a baked `index.json` into a directory that is a complete static site for GitHub Pages, with
this CC-BY-4.0 attribution on the page and in its README. The same `viewer/app.js` runs there
in static mode (no overrides, re-runs or RAW tier); see "Static mode" in `viewer/README.md`.

## License and attribution

Monster data in `data/registry/srd-5.2.1.json` and the per-monster overrides are derived
from the SRD 5.2.1 and are redistributed under CC-BY-4.0 with this attribution, which any
distribution of this repo or of assets generated from it must carry:

> This work includes material from the System Reference Document 5.2.1 ("SRD 5.2.1") by
> Wizards of the Coast LLC, available at https://www.dndbeyond.com/srd. The SRD 5.2.1 is
> licensed under the Creative Commons Attribution 4.0 International License, available at
> https://creativecommons.org/licenses/by/4.0/legalcode.

"Dungeons & Dragons" and the D&D logos are trademarks of Wizards of the Coast and are not
covered by that license; this project is not affiliated with or endorsed by Wizards of the
Coast. Nothing outside the SRD is included (see `docs/corpus-plan.md`).
