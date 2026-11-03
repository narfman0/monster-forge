# monster-forge

Low-poly, game-ready 3D assets (mesh, PBR texture, rig, animation clips) for every
D&D monster, generated locally with ComfyUI and Blender and consumed by Godot 4.

Scope tiers (see `docs/corpus-plan.md`):

1. SRD 5.2.1 (CC-BY-4.0): 330 monsters, registry in `data/registry/srd-5.2.1.json`.
2. Monster Manual 2024: the ~170-200 stat blocks not in the SRD.
3. Other official books (2024-2025 first, then legacy 2014-era books).

Non-SRD monsters are WotC IP: personal use only; a public pack ships SRD assets or
renamed analogues.

## Layout

- `data/registry/` one JSON record per monster: body plan, rig template, animation set,
  reference prompt, scale (`data/registry/README.md` has the schema).
- `data/body_plan_overrides.json`, `data/body_plan_reviewed.json` hand-curated taxonomy.
- `docs/plan.md` pipeline design and milestones. `docs/research-*.md` tool research.
- `tools/` scripts. `workflows/` ComfyUI API workflows.
- `out/<source>/<id>/` generated assets (git-ignored; never commit art).

## Environment

RTX 5070 Ti 16 GB, ComfyUI at `../ComfyUI` (Python 3.12, torch 2.14 cu130, port 8188),
Blender 5.2.2 LTS. Rigging and motion tools each get their own `uv` venv under `tools/`.
