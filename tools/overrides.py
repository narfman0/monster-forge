"""Per-monster overrides: data/overrides/<source>/<id>.json (git-tracked, hand-editable).

Stdlib only; imported by tools/forge.py and tools/viewer.py. An override file holds any
subset of FIELDS; everything else comes from the defaults (size table, body plan, ...).
`effective()` returns {field: {"default", "override", "value"}} for one monster.

Example (data/overrides/srd-5.2.1/harpy.json):
    {"upsample": 1024, "notes": "face texture soft at 512 voxels"}
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OVERRIDES_DIR = ROOT / "data" / "overrides"

# LOD0 budget by size (triangles). LOD0 is the detail tier; LOD1-3 derive from it.
TRI_BUDGET_BY_SIZE = {"tiny": 5000, "small": 8000, "medium": 12000, "large": 15000,
                      "huge": 20000, "gargantuan": 25000}
BIG_TEXTURE_SIZES = {"medium", "large", "huge", "gargantuan"}
# Stance leveling (tools/blender_qa.py): feet per plan; plans not listed are not leveled.
FEET_BY_PLAN = {"biped": 2, "winged_biped": 2, "avian": 2, "centauroid": 4, "quadruped": 4,
                "winged_quadruped": 4, "insectoid": 6}
TILT_CAP_BY_PLAN = {"insectoid": 40.0}
DEFAULT_TILT_CAP = 20.0
LOD_RATIOS = [0.5, 0.25, 0.1]

_NAME = re.compile(r"^[A-Za-z0-9_.\-/]{1,120}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


def _num(lo, hi, integer=False):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "must be a number"
        if integer and int(v) != v:
            return "must be an integer"
        if not lo <= v <= hi:
            return f"must be in [{lo}, {hi}]"
        return None
    return check


def _choice(*opts):
    return lambda v: None if v in opts else f"must be one of {list(opts)}"


def _text(maxlen):
    return lambda v: None if isinstance(v, str) and len(v) <= maxlen else f"must be a string of <= {maxlen} chars"


def _bool(v):
    return None if isinstance(v, bool) else "must be true or false"


def _style(v):
    return None if isinstance(v, str) and _NAME.match(v) else "must be a style name or data path"


def _screen_ratios(v):
    if not (isinstance(v, list) and len(v) == 3):
        return "must be a list of 3 numbers (LOD0, LOD1, LOD2 switch points)"
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 < x <= 4 for x in v):
        return "ratios must be in (0, 4] (fraction of the viewport height)"
    if any(b >= a for a, b in zip(v, v[1:])):
        return "ratios must be strictly decreasing"
    return None


def _ratios(v):
    if not (isinstance(v, list) and 1 <= len(v) <= 6):
        return "must be a list of 1-6 ratios"
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 < x < 1 for x in v):
        return "ratios must be in (0, 1)"
    if any(b >= a for a, b in zip(v, v[1:])):
        return "ratios must be strictly decreasing"
    return None


# Multiview paint stage (tools/paint.py, docs/paint-stage.md): defaults and the keys of
# the `paint` override. views None = by body plan (6 + top for legged plans, + head views).
PAINT_DEFAULTS = {"enabled": True, "views": None, "denoise": 0.7, "ip_weight": 0.7, "cn_strength": 0.7,
                  "normal_strength": 0.0, "color_match": 0.5, "head_views": True, "seed": None}
PAINT_KEYS = {
    "enabled": lambda v: _bool(v),
    "views": lambda v: None if isinstance(v, list) and v and all(isinstance(x, str) and _NAME.match(x) for x in v)
    else "must be a non-empty list of view names",
    "denoise": _num(0.05, 1.0),
    "ip_weight": _num(0.0, 1.5),
    "cn_strength": _num(0.0, 2.0),
    "normal_strength": _num(0.0, 2.0),
    "color_match": _num(0.0, 1.0),
    "head_views": lambda v: _bool(v),
    "seed": _num(0, 2**31 - 1, True),
}


def _paint(v):
    if not isinstance(v, dict):
        return "must be an object"
    for k, x in v.items():
        if k not in PAINT_KEYS:
            return f"unknown key {k!r} (allowed: {sorted(PAINT_KEYS)})"
        if x is not None:
            e = PAINT_KEYS[k](x)
            if e:
                return f"{k} {e}"
    return None


# field -> (validator, stage it affects, help)
FIELDS = {
    "tri_budget": (_num(300, 200000, True), "mesh", "LOD0 triangle budget"),
    "vg_factor": (_num(0, 2), "mesh", "weighted decimation: vertex_group_factor"),
    "upsample": (_choice(0, 1024), "mesh", "1024 = cascade, sharper voxel colour (default); 0 = Pixal3D 512 only"),
    "texture_size": (_choice(512, 1024, 2048, 4096), "mesh", "bake + UV atlas size"),
    "normal_cage": (_num(0.001, 0.2), "mesh", "normal bake search band (fraction of bbox diagonal)"),
    "ref_pick": (_num(0, 15, True), "mesh", "which ref_<i>.png feeds the mesh stage"),
    "seed_mesh": (_num(0, 2**31 - 1, True), "mesh", "Pixal3D seed"),
    "prompt_extra": (_text(500), "ref", "appended to the reference prompt"),
    "negative_extra": (_text(500), "ref", "appended to the negative prompt"),
    "style": (_style, "ref", "art style (name or data/*.json path; 'none' = unstyled)"),
    "seed_ref": (_num(0, 2**31 - 1, True), "ref", "reference image seed"),
    "yaw": (_num(-360, 360), "qa", "degrees about up (overrides auto facing)"),
    "forward": (_choice("-Z", "+Z"), "qa", "glTF facing direction"),
    "level": (_bool, "qa", "stance leveling on/off"),
    "level_tilt": (_num(0, 60), "qa", "max tilt (deg) leveling may apply"),
    "lod_ratios": (_ratios, "lod", "LOD1.. triangle ratios of LOD0"),
    "rig_facing_check": (_bool, "rig", "refuse to roll a skeleton when model.glb looks misfaced (false = roll anyway)"),
    "paint": (_paint, "paint", "multiview paint: {enabled, views, denoise, ip_weight, cn_strength, ...}"),
    "lod_screen_ratios": (_screen_ratios, None,
                          "viewer auto LOD: LOD0/LOD1/LOD2 when the model covers more than these fractions "
                          "of the viewport height (else LOD3); unset = the viewer's settings"),
    "notes": (_text(2000), None, "free text"),
}


def path_for(source: str, mid: str) -> Path:
    return OVERRIDES_DIR / source / f"{mid}.json"


def load(source: str, mid: str) -> dict:
    p = path_for(source, mid)
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{p}: not a JSON object")
    return data


def validate(data: dict) -> list[str]:
    """Errors for unknown keys and bad values; None values mean 'unset' and are allowed."""
    if not isinstance(data, dict):
        return ["body must be a JSON object"]
    errs = []
    for k, v in data.items():
        if k not in FIELDS:
            errs.append(f"{k}: unknown field")
        elif v is not None:
            e = FIELDS[k][0](v)
            if e:
                errs.append(f"{k}: {e}")
    return errs


def save(source: str, mid: str, data: dict) -> dict:
    """Write the override file (None values dropped; empty -> file removed)."""
    errs = validate(data)
    if errs:
        raise ValueError("; ".join(errs))
    clean = {k: data[k] for k in FIELDS if k in data and data[k] is not None}
    p = path_for(source, mid)
    if not clean:
        p.unlink(missing_ok=True)
        return {}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, indent=2) + "\n")
    tmp.replace(p)
    return clean


def defaults(mon: dict, style_name: str | None = None) -> dict:
    """Defaults for one registry record (what runs with no override file)."""
    size, plan = mon.get("size", "medium"), mon.get("body_plan", "")
    tex = 2048 if size in BIG_TEXTURE_SIZES else 1024
    return {
        "tri_budget": TRI_BUDGET_BY_SIZE.get(size, 12000), "vg_factor": 0.5, "upsample": 1024,
        "texture_size": tex, "normal_cage": 0.015, "ref_pick": 0, "seed_mesh": None,
        "prompt_extra": "", "negative_extra": "", "style": style_name, "seed_ref": None,
        "yaw": None, "forward": "-Z", "level": plan in FEET_BY_PLAN,
        "level_tilt": TILT_CAP_BY_PLAN.get(plan, DEFAULT_TILT_CAP),
        "lod_ratios": list(LOD_RATIOS), "rig_facing_check": True, "lod_screen_ratios": None, "notes": "",
    }


def effective(mon: dict, source: str, style_name: str | None = None) -> dict:
    d = defaults(mon, style_name)
    o = load(source, mon["id"])
    return {k: {"default": d.get(k), "override": o.get(k),
                "value": o[k] if o.get(k) is not None else d.get(k),
                "stage": FIELDS[k][1], "help": FIELDS[k][2]} for k in FIELDS}


def values(mon: dict, source: str, style_name: str | None = None) -> dict:
    return {k: v["value"] for k, v in effective(mon, source, style_name).items()}
