"""Where each source's registry, curation and per-monster overrides live.

This repo is public and holds only SRD 5.2.1 (CC-BY-4.0). Every other source (Monster
Manual 2024, later books: WotC IP, personal use only) lives in a separate, private
checkout, the "extra" repo:

  $MONSTER_FORGE_EXTRA, else ../monster-forge-nonsrd next to this repo
    registry/<source>.json
    curation/<source>/<name>.json      body_plan_overrides, body_plan_reviewed,
                                       equipment, visual_profiles
    overrides/<source>/<id>.json

SRD paths are the local data/ files. Another source's registry and overrides resolve to a
local path when one exists (a git-ignored copy or symlink), else to the extra repo; its
curation is only ever read from the extra repo. Stdlib only.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ENV = "MONSTER_FORGE_EXTRA"
LOCAL_SOURCES = {"srd-5.2.1"}


def extra_dir() -> Path:
    """The non-SRD repo: $MONSTER_FORGE_EXTRA, else ../monster-forge-nonsrd."""
    env = os.environ.get(ENV)
    return Path(env).expanduser().resolve() if env else (ROOT.parent / "monster-forge-nonsrd").resolve()


def _extra(source: str, rel: str, need_file: bool) -> Path:
    base = extra_dir()
    path = base / rel
    if path.exists() or (not need_file and base.is_dir()):
        return path
    raise FileNotFoundError(
        f"{source}: {path} not found. Non-SRD sources live in a separate checkout: set "
        f"{ENV} to it (default ../monster-forge-nonsrd next to this repo)")


def registry_path(source: str, write: bool = False) -> Path:
    """data/registry/<source>.json for the SRD or when a local (git-ignored) copy exists,
    else the extra repo's registry/<source>.json. write=True: where a builder writes the
    source's registry (the extra repo for every non-SRD source, never a local copy)."""
    local = DATA / "registry" / f"{source}.json"
    if source in LOCAL_SOURCES or (not write and local.exists()):
        return local
    return _extra(source, f"registry/{source}.json", need_file=not write)


def curation_path(source: str, name: str) -> Path:
    """data/<name>.json for the SRD, else the extra repo's curation/<source>/<name>.json
    (non-SRD curation is never read from this repo)."""
    if source in LOCAL_SOURCES:
        return DATA / f"{name}.json"
    return _extra(source, f"curation/{source}/{name}.json", need_file=True)


def overrides_dir(source: str) -> Path:
    """data/overrides/<source>/ for the SRD or when it exists locally (a git-ignored dir or
    symlink), else the extra repo's overrides/<source>/ (which may not exist yet: a missing
    override file means no overrides)."""
    local = DATA / "overrides" / source
    if source in LOCAL_SOURCES or local.exists():
        return local
    return _extra(source, f"overrides/{source}", need_file=False)
