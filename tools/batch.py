#!/usr/bin/env python3
"""monster-forge batch runner: the whole SRD through tools/forge.py, stage by stage per
monster, resumable, with memory and ComfyUI guards. See docs/batch.md.

    python3 -I tools/batch.py audit                       # coverage per body plan, gaps (docs/batch.md)
    python3 -I tools/batch.py plan                        # ordered work list + time estimate
    python3 -I tools/batch.py plan --tier 1 --plans biped,quadruped
    python3 -I tools/batch.py run --tier 1 --dry-run      # what it would do
    python3 -I tools/batch.py run --tier 1 --stop-after-failures 10
    python3 -I tools/batch.py run --ids bandit,guard --stages ref,mesh,qa
    python3 -I tools/batch.py report                      # out/<source>/_batch.md + a short summary

Uses forge.py as a library (registry, manifests, paths, ComfyUI HTTP, RUN_ORDER) and
viewer.py's stage_status() / rig_quality(), so "done" means what the viewer shows. Each
stage runs as its own `forge.py <stage> <id>` subprocess (in its own process group), so a
stage can be timed out and killed with its Blender / SkinTokens children; forge records
a failed stage in manifest.errors itself, the batch adds timeouts and the facing gate.

Tiers (classify()): 0 pilot (never run here unless --include-pilots), 1 proven (a
pilot's body plan and rig template, pilot sizes), 2 same code path as a pilot (or a
size the pilots did not cover, a swarm made as its unit, or the hand-fixed winged_biped),
3 untested plan variant,
4 hold (needs a reference-stage fix first; only run with --tier 4 or --ids).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge as fg  # noqa: E402  (registry, manifests, ComfyUI HTTP, RUN_ORDER)
import overrides  # noqa: E402
import paint  # noqa: E402
import rig  # noqa: E402
import anim  # noqa: E402
import viewer  # noqa: E402  (stage_status, rig_quality: the viewer's "done" rules)

ROOT = fg.ROOT
FORGE = ROOT / "tools" / "forge.py"
STAGES = list(fg.RUN_ORDER)  # ref mesh qa paint lod rig anim

# The 15 body-plan pilots, done through anim (docs/TODO.md B, H). The batch skips them.
PILOTS = ("goblin-warrior", "wolf", "owlbear", "giant-spider", "harpy", "young-red-dragon",
          "constrictor-snake", "ghost", "giant-eagle", "black-pudding", "violet-fungus",
          "giant-octopus", "hunter-shark", "merfolk-skirmisher", "swarm-of-rats")

# ---------------------------------------------------------------- timings
# Seconds per stage from the pilots (docs/TODO.md, manifests): ref 22-28 s with 3
# candidates, ~40 s with 4 + the unarmed check (bandit 38, goblin 50); mesh 50-96; qa 3;
# paint 150-171 (+ a ComfyUI restart before it); lod 1; rig 120-155 for 4 SkinTokens rolls,
# 7 for a generated skeleton or a rebuild from rig/chosen.glb; anim 3-6.
EST = {"ref": 24, "ref_unarmed": 40, "mesh": 70, "qa": 3, "facing": 6, "restart": 10, "paint": 160,
       "lod": 1.5, "rig": 140, "rig_fallback": 7, "anim": 5}
# Timeout per stage = TIMEOUT_SCALE x the pilots' slowest run, at least TIMEOUT_FLOOR.
# Smoke run (2026-11-04, 6 tier-1 records, ref,mesh,qa): ref 22.1 s warm (28 s after a mesh),
# 59.6 s cold with the unarmed check; mesh 65-104 s (mean 80) from a freshly restarted ComfyUI;
# qa 2.6-3.2; facing gate 2.7-3.7; ComfyUI restart 6-10 s.
MAX_SEEN = {"ref": 30, "ref_unarmed": 60, "mesh": 104, "qa": 3, "paint": 171, "lod": 1.4, "rig": 155,
            "rig_fallback": 8, "anim": 10}
TIMEOUT_SCALE = 3.0
TIMEOUT_FLOOR = 120.0
COMFY_STAGES = {"ref", "mesh", "paint", "rig"}  # stages that talk to ComfyUI (rig: /free, wait_idle)
MIN_FREE_GB = 8.0
# ComfyUI restarts before these stages. Paint: it pushes ComfyUI to its 16 GB MemoryHigh and
# stalled twice. Mesh (smoke run 2026-11-04): Trellis2 peaks at the 16 GB ceiling even from a
# fresh process (75 s); after a ref, /free still left 4.6 GB resident and the texture-VAE
# decode stalled in MemoryHigh reclaim past the 288 s timeout.
RESTART_BEFORE = ("mesh", "paint")


def restart_stages(args) -> set:
    if getattr(args, "no_restart", False):
        return set()
    return {x for x in (getattr(args, "restart_before", None) or ",".join(RESTART_BEFORE)).split(",") if x}
FAST_FAIL_S = 10.0  # a stage failing this fast is the environment (ComfyUI 500s, disk quota), not the monster
MAX_FAST_FAILURES = 3  # stop the run after this many monsters in a row fail that way
HANG_S = 60.0  # /queue silent this long = hung ComfyUI
WATCH_POLL = 10.0  # seconds between the watchdog's /queue polls

# ---------------------------------------------------------------- coverage model
# (body_plan, rig_template) pairs that run the same code path as a pilot. The code keys on
# the plan (rig.RIG_STRATEGY, anim.LEGLESS / ARMED, forge.MAX_SCALE_PLANS); the rig
# template is only a label except custom.theropod (rig score: no wings).
SAME_PATH = {
    ("biped", "custom.ghost"): "shadow: SkinTokens biped like the goblin, robed like the ghost",
    ("quadruped", "rigify.horse"): "SkinTokens + the legged framework, as wolf and owlbear",
    ("quadruped", "rigify.cat"): "SkinTokens + the legged framework, as wolf and owlbear",
    ("winged_quadruped", "custom.quadruped_winged"): "as the dragon; feathered wings must come out of SkinTokens "
                                                     "(the dragon's did, the harpy's did not)",
    ("winged_quadruped", "custom.horse_winged"): "pegasus: as the dragon, feathered wings",
    ("avian", "custom.bat_wing"): "legged avian framework; membrane wings not yet seen by find_wings",
    ("insectoid", "custom.hexapod"): "sprawled legged framework; 6 legs is inside LEG_RANGE",
    ("insectoid", "custom.decapod"): "crabs, chuul: 8 legs + claws (claws may count as legs, LEG_RANGE 6-10)",
    ("insectoid", "custom.multileg_reptile"): "basilisk: 8 legs on the sprawled framework",
}
UNTESTED = {
    ("biped", "custom.kaiju_tail"): "tarrasque: upright with a heavy ground tail (upright plans have no tail "
                                    "test), gargantuan",
    ("quadruped", "custom.hydra"): "5 neck chains; head and neck finders expect one",
    ("quadruped", "custom.turtle"): "dragon turtle: shell, gargantuan, swims",
    ("avian", "custom.wyvern"): "bat wings as forelimbs plus a stinger tail",
    ("avian", "custom.theropod"): "allosaurus, T. rex: wingless avian (rig score skips wings)",
    ("insectoid", "custom.myriapod"): "centipede, remorhaz: more legs than LEG_RANGE (6-10)",
    ("insectoid", "custom.hexapod_winged"): "giant wasp: fly-only, insect wings vs find_wings",
    ("serpentine", "custom.spine_chain_winged"): "couatl: the generated spine chain ignores the wings; "
                                                 "no serpentine fly clip",
    ("serpentine", "custom.serpent_multileg"): "behir: 12 legs on a spine chain; the generated chain ignores them",
    ("floating", "custom.elemental_column"): "air/fire/water elementals, djinni, invisible stalker: a column "
                                             "with arms (TODO H: untested)",
    ("floating", "custom.membrane"): "cloaker: a cloak-shaped membrane",
    ("floating", "custom.wisp"): "will-o'-wisp: a ball of light",
    ("aquatic", "custom.flipper"): "plesiosaurus, archelon: 4 flipper chains (TODO H: untested)",
    ("aquatic", "custom.spine_chain"): "seahorses: an upright curled body",
    ("plant_object", "custom.tree_roots"): "awakened tree and shrub: branches and root legs on a vertical chain",
    ("plant_object", "custom.cloth_grid"): "rug of smothering: a flat cloth, the vertical chain is wrong",
    ("plant_object", "custom.rigid_prop"): "flying sword: a rigid prop that flies; no plant_object fly clip",
    ("plant_object", "custom.limb_chain"): "troll limb: a crawling arm",
    ("amorphous", "custom.blob_torso"): "lemure: blob with a head and arms; the vertical chain ignores the arms",
    ("centauroid", "custom.drider"): "spider lower body + a torso",
    ("radial", "custom.radial_tripod"): "xorn: no clip builder for the plan (anim stage skips it)",
}
UNTESTED_IDS = {
    "glabrezu": "4 arms (two pincers); find_arms takes one pair",
    "marilith": "6 arms: sockets hand_r1..3 / hand_l1..3, which the rig stage does not place",
    "otyugh": "tentacled on three legs (TODO H: untested)",
    "roper": "tentacled column (TODO H: untested)",
    "aboleth": "fish body with 4 tentacles: tentacle_hub vs a fish spine",
    "swarm-of-crawling-claws": "unit is a crawling hand on custom.limb_chain, untested like the troll limb",
}
# The pilots that needed hand work the batch cannot repeat: same template, but tier 2.
HAND_FIXED = {("winged_biped", "custom.human_winged"): "harpy pilot: SkinTokens gave no wing bones in 4 of 4 rolls; "
              "hand-added wing chains + blender_wing_weights.py + rig_reweight"}
EXTREME_SIZES = {"tiny", "huge", "gargantuan"}  # the pilots are small to large
# Fixed in cade6ef by data/prompt_tweaks.json: "_plans.avian" (front view, both wings spread;
# not custom.theropod) and the per-id specter / wraith ghost-robe tweaks. Kept for the audit.
GHOST_PROMPT = {"specter", "wraith"}
_REG_CACHE: dict = {}


def unit_rec(rec: dict) -> dict:
    """The record forge's stages see: a swarm made as its unit (forge.resolve_swarm_unit:
    SWARM_UNITS / override swarm_unit, the unit's plan, template, size, scale_m), anything
    else (and swarm-of-rats, which has no unit) as is."""
    if rec.get("body_plan") != "swarm" and not rec.get("swarm_unit"):
        return rec
    src = rec.get("source") or "srd-5.2.1"
    if src not in _REG_CACHE:
        _REG_CACHE[src] = fg.load_registry(src)
    try:
        return fg.resolve_swarm_unit(_REG_CACHE[src], rec)
    except Exception:  # a bad unit fails the ref stage loudly in forge; here: the record as is
        return rec


def hold_reason(rec: dict) -> str | None:
    """Why a record should wait for a reference-stage fix (tier 4), or None. Fixing these
    after the batch means re-running the whole chain from ref. Swarms are judged by their
    unit (unit_rec): swarm-of-venomous-snakes is a serpentine unit."""
    u = unit_rec(rec)
    plan = u["body_plan"]
    unit = (u.get("swarm_unit") or {}).get("unit")
    pre = f"swarm unit {unit}: " if unit else ""
    if plan == "centauroid":
        return "centauroid: the centaur-trooper pilot is not through anim (new centaur_34 reference, mesh re-made)"
    if plan == "serpentine":
        return (pre + "serpentine: the serpentine_side depth template is new; the constrictor-snake pilot is being "
                "re-made with it (paint, rig, anim pending)")
    return None


def classify(rec: dict, pilot_keys: dict) -> dict:
    """{tier, status, note} for one record (see the module doc). A swarm is classified as
    its unit (unit_rec): plan, template and size of the unit, tier 2 at best (the swarm
    itself is a new record even when the unit's path is a pilot's)."""
    if rec["id"] in PILOTS:
        return {"tier": 0, "status": "pilot", "note": ""}
    hold = hold_reason(rec)
    u = unit_rec(rec)
    su = u.get("swarm_unit")
    key = (u["body_plan"], u["rig_template"])
    if su and rec["id"] not in UNTESTED_IDS and key not in UNTESTED:
        plan_pilot = next((p for (pl, _t), p in pilot_keys.items() if pl == key[0]), None)
        if key in pilot_keys or key in SAME_PATH or plan_pilot:
            note = (f"made as one {su.get('unit') or 'unit'} ({key[0]}, {key[1]}, {u['size']}): same path as "
                    f"{pilot_keys.get(key) or plan_pilot}")
            return {"tier": 4 if hold else 2, "status": "hold" if hold else "same path", "note": hold or note}
    rec = u
    if rec["id"] in UNTESTED_IDS:
        tier, status, note = 3, "untested", UNTESTED_IDS[rec["id"]]
    elif key in UNTESTED:
        tier, status, note = 3, "untested", UNTESTED[key]
    elif key in HAND_FIXED:
        tier, status, note = 2, "proven (hand-fixed)", HAND_FIXED[key]
    elif key in pilot_keys:
        tier, status, note = 1, "proven", f"as {pilot_keys[key]}"
    elif key in SAME_PATH:
        tier, status, note = 2, "same path", SAME_PATH[key]
    else:
        tier, status, note = 3, "untested", "no pilot or known path for this plan and template"
    if rec["size"] in EXTREME_SIZES and tier < 2:
        tier, note = 2, note + f"; {rec['size']} (pilots are small-large)"
    if hold:
        tier, status, note = 4, "hold", hold
    return {"tier": tier, "status": status, "note": note}


# Clips the anim stage can build per plan, a mirror of blender_clips.py plan_clips() and
# legless_clips() (keep in sync; clips2, docs/rig-anim.md). Legged plans: walk, idle, hit,
# death, attack_melee, fly (the wing beat with wing chains, else `levitate`), breath (any
# head chain), cast, swim, burrow; attack_ranged with both arms. The legless builders are
# fixed sets; serpentine and floating have the clips2 extras, the others do not.
LEGLESS_CLIPS = {
    "serpentine": {"idle", "walk", "swim", "attack_melee", "hit", "death", "cast", "breath", "burrow", "fly"},
    "amorphous": {"idle", "walk", "attack_melee", "hit", "death"},
    "plant_object": {"idle", "walk", "attack_melee", "attack_special", "hit", "death"},
    "tentacled": {"idle", "swim", "fly", "walk", "attack_melee", "hit", "death"},
    "aquatic": {"idle", "swim", "walk", "attack_melee", "attack_special", "hit", "death"},
    "merfolk_naga": {"idle", "swim", "walk", "hit", "death"},
    "swarm": {"idle", "walk", "fly", "swim", "attack_melee", "hit", "death"},
    "floating": {"idle", "fly", "walk", "attack_melee", "hit", "death", "swim", "cast"},
}


def buildable_clips(rec: dict) -> set:
    """What the anim stage would build for this record if its skeleton has the plan's parts."""
    plan = rec["body_plan"]
    if plan in LEGLESS_CLIPS:
        s = set(LEGLESS_CLIPS[plan])
        if plan == "merfolk_naga" and rec.get("hands"):
            s |= {"attack_melee", "attack_ranged"}
        return s
    if plan not in overrides.FEET_BY_PLAN:
        return set()  # radial: anim.stage_anim skips the plan
    s = {"walk", "idle", "hit", "death", "attack_melee", "fly", "breath", "cast", "swim", "burrow"}
    if plan in anim.UPRIGHT or (plan in anim.ARMED and rec.get("hands")):
        s.add("attack_ranged")
    return s


def missing_clips_expected(rec: dict) -> list:
    can = buildable_clips(rec)
    return [c for c in rec.get("animation_set") or [] if c not in can]


# Concrete gaps: (key, title, predicate, blocking, re-run, note). blocking = fix before the
# record runs (the fix is in ref or mesh, so after means the whole chain again).
def _clip_gap(clip):
    return lambda r: clip in missing_clips_expected(r)


GAPS = [
    ("clip_ranged", "`attack_ranged` without two arms (spit, tail spikes, rays)", _clip_gap("attack_ranged"), False,
     "anim --force", "floating and serpentine casters, manticore"),
    ("clip_fly", "`fly` on a plan with no fly builder (amorphous, plant_object, aquatic, merfolk_naga)", _clip_gap("fly"),
     False, "anim --force", "the flying sword"),
    ("clip_swim", "`swim` on a plan with no swim builder (amorphous, plant_object)", _clip_gap("swim"), False,
     "anim --force", "amorphous builders: idle, walk, attack_melee, hit, death"),
    ("clip_other", "radial: no clip builder for the plan (anim stage skips it)",
     lambda r: any(c not in {"attack_ranged", "fly", "swim"} for c in missing_clips_expected(r)),
     False, "anim --force", ""),
    ("winged_biped_wings", "winged_biped wing chains: SkinTokens found none on the harpy (hand-built)",
     lambda r: r["body_plan"] == "winged_biped" and r["id"] not in PILOTS, False,
     "rig --rig-reroll (+ hand chains / wing weights), anim --force",
     "expect fly missing or torn wings; TODO C has the stage work"),
    ("avian_prompt", "avian reference prompt: front view, wings spread to the sides (fixed)",
     lambda r: r["body_plan"] == "avian" and r["rig_template"] != "custom.theropod" and r["id"] not in PILOTS,
     False, "-", "fixed in cade6ef: prompt_tweaks.json `_plans.avian` (front view, both wings spread; "
     "theropods excepted)"),
    ("serpentine_template", "serpentine: pilot re-made with the serpentine_side depth template",
     lambda r: r["body_plan"] == "serpentine" and r["id"] not in PILOTS, True, "everything (ref)",
     "owned by the pilot stream; release once constrictor-snake is through anim"),
    ("swarm_unit", "swarms: one unit per swarm (prompt, unit scale_m) (fixed)",
     lambda r: bool(r.get("swarm_unit")) and r["id"] not in PILOTS, False, "-",
     "fixed in cade6ef: forge.SWARM_UNITS makes each swarm as one unit (plan, template, size, scale_m); "
     "swarm-of-rats keeps its column-of-rats pilot"),
    ("centauroid", "centauroid: no centaur pilot through anim yet",
     lambda r: r["body_plan"] == "centauroid", True, "everything (ref)", "centaur-trooper in progress"),
    ("ghost_prompt", "specter, wraith: the ghost's legless-robe prompt tweak (fixed)",
     lambda r: r["id"] in GHOST_PROMPT, False, "-",
     "fixed in cade6ef: per-id specter / wraith tweaks in data/prompt_tweaks.json"),
    ("variant", "untested plan variant (see the plan table)",
     lambda r: r["id"] not in PILOTS and ((r["body_plan"], r["rig_template"]) in UNTESTED or r["id"] in UNTESTED_IDS),
     False, "rig --rig-reroll, anim --force (mesh only if the shape is wrong)", ""),
    ("multi_arm", "multi-armed wielders: hand_r1..n sockets / more than one arm pair",
     lambda r: (r.get("arms") or 2) != 2, False, "rig --force, anim --force", "glabrezu, marilith"),
    ("size_tiny", "tiny: no pilot below 0.95 m (anim thresholds are size-relative since clips2, untested there)",
     lambda r: r["size"] == "tiny" and r["id"] not in PILOTS, False, "anim --force", ""),
    ("size_big", "huge / gargantuan: 20-25k tri LOD0 never through SkinTokens; paint views are 1024 px whatever "
                 "the size (anim thresholds are size-relative since clips2)",
     lambda r: r["size"] in ("huge", "gargantuan") and r["id"] not in PILOTS, False,
     "rig (if SkinTokens fails: tri_budget override, then mesh..), anim --force", ""),
    ("facing_wings", "winged bodies: QA's auto-facing follows the wings (the dragon pilot was 65 deg off); the rig "
                     "stage refuses past 30 deg",
     lambda r: r["body_plan"] in ("winged_quadruped", "winged_biped", "avian") and r["id"] not in PILOTS, False,
     "pin `yaw`, then qa, paint, lod, rig, anim", "the batch's facing gate stops these after qa, before paint"),
    ("unarmed", "unarmed_reference: --n 4 + the unarmed check (thin margin, 2.5 threshold)",
     lambda r: r.get("unarmed_reference") and r["id"] not in PILOTS, False, "ref (re-pick) when a weapon slips through",
     "handled by the batch (--n 4); review the candidate strip"),
    ("props_offmodel", "wields an off-model prop (fork, heavy crossbow, mace)",
     lambda r: bool(set(r.get("wields") or []) & {"fork", "heavy-crossbow", "mace"}), False,
     "props.py only (no monster re-run)", ""),
    ("floating_wielder", "a floating wielder: the floating attack is a two-arm reach, not a weapon cut",
     lambda r: r["body_plan"] == "floating" and bool(r.get("wields")), False, "anim --force", "djinni"),
    ("wields_no_hands", "wields something but the plan or record has no hands",
     lambda r: bool(r.get("wields")) and not r.get("hands"), True, "registry", ""),
]


# ---------------------------------------------------------------- status
def mon_dir(source: str, mid: str) -> Path:
    return fg.OUT_ROOT / source / mid  # not fg.out_dir(): that creates the directory


def paint_enabled(mon: dict) -> bool:
    try:
        return bool(paint.settings(mon, fg.ov_for(mon)).get("enabled"))
    except Exception:  # a bad override fails the stage loudly in forge; here: assume on
        return True


def rig_done(d: Path, man: dict, st: dict) -> bool:
    """The viewer's rule (manifest rig ok) plus forge's skip rule (the rigged game files
    exist): paint and lod delete model_rigged*.glb but leave manifest.rig."""
    if not st["stages"]["rig"] or not (d / "model_rigged.glb").exists():
        return False
    lods = viewer.load_json(d / "lods.json").get("lods") or {}
    return all((d / f"model_rigged_lod{k}.glb").exists() for k, v in lods.items() if k != "0" and v.get("file"))


def anim_supported(rec: dict) -> bool:
    return rec["body_plan"] in overrides.FEET_BY_PLAN or rec["body_plan"] in anim.LEGLESS


def status(rec: dict, source: str) -> dict:
    """{done: {stage: bool}, pending: [...] in RUN_ORDER (with downstream invalidation),
    skipped: {stage: why}, man, st} for one record."""
    d = mon_dir(source, rec["id"])
    man = fg.load_manifest(d) if d.is_dir() else {}
    st = viewer.stage_status(d, man)
    done = dict(st["stages"])
    done["rig"] = rig_done(d, man, st)
    stale = {}
    if done["mesh"]:
        why = mesh_stale(rec, d, man)
        if why:  # forge's mesh clears model.glb and the thumbs, so qa follows by itself
            done["mesh"], stale["mesh"] = False, why
    if done["qa"] and "mesh" not in stale:
        why = qa_stale(rec, man)
        if why:
            done["qa"], stale["qa"] = False, why
    skipped = {}
    u = unit_rec(rec)  # a swarm's stages run on its unit
    if not paint_enabled(u):
        skipped["paint"] = "disabled by override paint.enabled"
    if not anim_supported(u):
        skipped["anim"] = f"no clip builder for plan {u['body_plan']}"
    raw = [s for s in STAGES if not done[s] and s not in skipped]
    # what running a stage deletes downstream (forge: mesh/qa clear paint, lods, rig;
    # paint clears lods and the rigged files; lod clears the rigged files)
    pend = set(raw)
    for s in raw:
        if s in ("ref", "mesh", "qa"):
            pend |= {x for x in STAGES[STAGES.index(s) + 1:] if x not in skipped}
        elif s == "paint":
            pend |= {"lod", "rig"}
        elif s == "lod":
            pend.add("rig")
    return {"done": done, "pending": [s for s in STAGES if s in pend], "raw_pending": raw,
            "skipped": skipped, "stale": stale, "man": man, "st": st, "dir": d}


def mesh_stale(rec: dict, d: Path, man: dict) -> str | None:
    """model_raw.glb made from another reference than the one in use now: a re-rolled ref
    (`forge.py ref --force`, ref.png newer than the mesh) or a `ref_pick` pinned after the
    mesh ran. forge skips mesh while model_raw.glb exists, so the batch runs it with --force."""
    raw, ref = d / "model_raw.glb", d / "ref.png"
    if not raw.exists():
        return None
    if ref.exists() and ref.stat().st_mtime > raw.stat().st_mtime + 1:
        return "ref.png is newer than model_raw.glb (re-rolled reference)"
    try:
        pin = overrides.load(rec["source"], rec["id"]).get("ref_pick")
    except Exception:
        pin = None
    made = (man.get("mesh") or {}).get("ref_picked") if isinstance(man.get("mesh"), dict) else None
    if pin is not None and made is not None and int(pin) != int(made):
        return f"override ref_pick {pin} != the candidate the mesh was made from ({made})"
    return None


def qa_stale(rec: dict, man: dict) -> str | None:
    """A `yaw` pinned in the override after qa ran (the facing gate's fix) makes model.glb
    stale; forge would skip qa because its files exist, so the batch runs it with --force."""
    try:
        yaw = overrides.load(rec["source"], rec["id"]).get("yaw")
    except Exception:
        return None
    if yaw is None:
        return None
    q = man.get("qa") if isinstance(man.get("qa"), dict) else {}
    have = q.get("yaw_override")
    if have is None or abs(float(have) - float(yaw)) > 0.05:
        return f"override yaw {yaw} != the yaw qa ran with ({have})"
    return None


def ref_n(rec: dict, args) -> int:
    return max(args.n, 4) if rec.get("unarmed_reference") else args.n


def rig_kind(rec: dict, d: Path) -> str:
    """A swarm costs what its unit's plan costs (SkinTokens rolls or a generated skeleton)."""
    if (d / "rig" / "chosen.glb").exists():
        return "rig_fallback"  # rebuild from the kept skeleton: ~5-7 s
    return "rig_fallback" if unit_rec(rec)["body_plan"] in rig.RIG_STRATEGY else "rig"


def est_key(stage: str, rec: dict, d: Path) -> str:
    if stage == "ref":
        return "ref_unarmed" if rec.get("unarmed_reference") else "ref"
    if stage == "rig":
        return rig_kind(rec, d)
    return stage


def needs_facing_gate(rec: dict, d: Path, pending: list, args) -> bool:
    if args.no_facing_gate or "rig" not in pending or unit_rec(rec)["body_plan"] in rig.FACING_ADVISORY:
        return False
    if (d / "rig" / "chosen.glb").exists():
        return False  # the rig stage will not roll: no facing refusal
    try:
        return overrides.load(rec["source"], rec["id"]).get("rig_facing_check") is not False
    except Exception:
        return True


def estimate(rec: dict, s: dict, args, stages=None) -> float:
    pend = [x for x in s["pending"] if stages is None or x in stages]
    t = sum(EST[est_key(x, rec, s["dir"])] for x in pend)
    t += EST["restart"] * len(restart_stages(args) & set(pend))
    if needs_facing_gate(rec, s["dir"], pend, args):
        t += EST["facing"]
    return t


def timeout_for(stage: str, rec: dict, d: Path, args) -> float:
    return max(args.timeout_floor, args.timeout_scale * MAX_SEEN[est_key(stage, rec, d)])


# ---------------------------------------------------------------- selection
def parse_tiers(v: str | None) -> set | None:
    if not v:
        return None
    out = set()
    for part in v.split(","):
        if "-" in part:
            a, b = part.split("-")
            out |= set(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def work_list(reg: dict, args) -> list:
    """[(rec, cls)] in batch order: tier, then the plan's size in the registry (big proven
    groups first), then size category (medium/large/small before the extremes), then id."""
    pilot_keys = {(reg[p]["body_plan"], reg[p]["rig_template"]): p for p in PILOTS if p in reg}
    plan_n = collections.Counter(r["body_plan"] for r in reg.values())
    size_rank = {"medium": 0, "large": 1, "small": 2, "huge": 3, "tiny": 4, "gargantuan": 5}
    plans = set(args.plans.split(",")) if getattr(args, "plans", None) else None
    ids = [x for x in args.ids.split(",") if x] if getattr(args, "ids", None) else None
    tiers = parse_tiers(getattr(args, "tier", None))
    if ids:
        bad = [i for i in ids if i not in reg]
        if bad:
            raise SystemExit(f"unknown ids in {args.source}: {bad}")
    out = []
    for rec in reg.values():
        cls = classify(rec, pilot_keys)
        if ids is not None and rec["id"] not in ids:
            continue
        if plans is not None and rec["body_plan"] not in plans:
            continue
        if tiers is not None and cls["tier"] not in tiers and ids is None:
            continue
        out.append((rec, cls))
    out.sort(key=lambda rc: (rc[1]["tier"], -plan_n[rc[0]["body_plan"]], rc[0]["body_plan"],
                             size_rank.get(rc[0]["size"], 9), rc[0]["id"]))
    return out


def runnable(cls: dict, rec: dict, args) -> bool:
    """Pilots only with --include-pilots; hold (tier 4) only when asked for by tier or id."""
    if cls["tier"] == 0:
        return bool(args.include_pilots)
    if cls["tier"] == 4:
        tiers = parse_tiers(args.tier)
        return bool((tiers and 4 in tiers) or args.ids)
    return True


def hms(s: float) -> str:
    s = int(round(s))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


# ---------------------------------------------------------------- audit
def cmd_audit(args) -> int:
    reg = fg.load_registry(args.source)
    pilot_keys = {(reg[p]["body_plan"], reg[p]["rig_template"]): p for p in PILOTS if p in reg}
    print(audit_markdown(reg, pilot_keys))
    return 0


def audit_markdown(reg: dict, pilot_keys: dict) -> str:
    recs = list(reg.values())
    cls = {r["id"]: classify(r, pilot_keys) for r in recs}
    units = {r["id"]: unit_rec(r) for r in recs}  # gaps, clips and rigs: what the stages see
    plan_n = collections.Counter(r["body_plan"] for r in recs)
    lines = ["## Coverage per body plan", "",
             "| plan | records | pilot | proven | same path | untested | hold | rig | expected missing clips |",
             "|---|---:|---|---:|---:|---:|---:|---|---|"]
    for plan, n in plan_n.most_common():
        rs = [r for r in recs if r["body_plan"] == plan]
        c = collections.Counter(cls[r["id"]]["status"] for r in rs)
        pil = [r["id"] for r in rs if r["id"] in PILOTS]
        kinds = collections.Counter(
            f"fallback:{rig.RIG_STRATEGY[p]['kind']}" if p in rig.RIG_STRATEGY else "SkinTokens"
            for p in (units[r["id"]]["body_plan"] for r in rs))
        rigs = kinds.most_common(1)[0][0] if len(kinds) == 1 else ", ".join(f"{k} {v}" for k, v in kinds.most_common())
        miss = collections.Counter(c2 for r in rs for c2 in missing_clips_expected(units[r["id"]]))
        lines.append(f"| {plan} | {n} | {', '.join(pil) or '-'} | {c['proven']} | "
                     f"{c['same path'] + c['proven (hand-fixed)']} | {c['untested']} | {c['hold']} | {rigs} | "
                     + (", ".join(f"{k} {v}" for k, v in miss.most_common()) or "-") + " |")
    lines += ["", "Pilots are not counted in proven / same path / untested / hold.", "",
              "## Per rig template", "",
              "| plan | rig_template | records | status | tier | note |", "|---|---|---:|---|---:|---|"]
    by_key = collections.defaultdict(list)
    for r in recs:
        by_key[(r["body_plan"], r["rig_template"])].append(r)
    for (plan, tpl), rs in sorted(by_key.items(), key=lambda kv: (-plan_n[kv[0][0]], kv[0][0], -len(kv[1]))):
        sts = collections.Counter((cls[r["id"]]["status"], cls[r["id"]]["tier"]) for r in rs)
        for (status_, tier), k in sorted(sts.items(), key=lambda kv: kv[0][1]):
            ex = [r for r in rs if (cls[r["id"]]["status"], cls[r["id"]]["tier"]) == (status_, tier)]
            notes = sorted({cls[r["id"]]["note"] for r in ex})
            ids = ", ".join(r["id"] for r in ex) if len(ex) <= 6 else f"{len(ex)} records"
            if tier == 2 and status_ == "proven":  # bumped by size only: say which sizes
                notes = ["sizes " + ", ".join(sorted({r["size"] for r in ex})) + " (pilots are small-large)"]
            note = "; ".join(notes)[:400]
            lines.append(f"| {plan} | {tpl} | {k} | {status_} | {tier} | {ids} - {note} |" if status_ != "pilot"
                         else f"| {plan} | {tpl} | {k} | pilot | 0 | {ids} |")
    lines += ["", "## Gaps, ranked by records", "",
              "| # | gap | records | blocks the batch? | re-run after the fix | examples |",
              "|---:|---|---:|---|---|---|"]
    rows = []
    for key, title, pred, blocking, rerun, note in GAPS:
        hit = [r["id"] for r in recs if pred(units[r["id"]])]
        rows.append((len(hit), key, title, blocking, rerun, note, hit))
    rows.sort(key=lambda x: (-x[0]))
    for i, (n, key, title, blocking, rerun, note, hit) in enumerate(rows, 1):
        ex = ", ".join(hit[:6]) + (", ..." if len(hit) > 6 else "")
        lines.append(f"| {i} | {title} | {n} | {'**yes** (fix first)' if blocking and n else 'no (fixed)' if rerun == '-' else 'no (after)'} | "
                     f"{rerun} | {ex}{'; ' + note if note else ''} |")
    clip_miss = collections.Counter(c for r in recs for c in missing_clips_expected(units[r["id"]]))
    lines += ["", "Expected missing clips over all records (animation_set clips no builder makes): "
              + ", ".join(f"`{k}` {v}" for k, v in clip_miss.most_common()) + "."]
    tiers = collections.Counter(c["tier"] for c in cls.values())
    lines += ["", "Tiers: " + ", ".join(f"{t} = {tiers[t]}" for t in sorted(tiers)) + "."]
    return "\n".join(lines)


# ---------------------------------------------------------------- plan
def cmd_plan(args) -> int:
    reg = fg.load_registry(args.source)
    items = work_list(reg, args)
    names = {0: "pilots (skipped)", 1: "tier 1: proven (a pilot's plan + template)",
             2: "tier 2: same code path / hand-fixed pilot / untested size",
             3: "tier 3: untested plan variant", 4: "tier 4: hold (fix the reference stage first)"}
    total, per_tier, counts = 0.0, collections.Counter(), collections.Counter()
    n_run = 0
    want = set(args.stages.split(",")) if args.stages else None
    cur = None
    k = 0
    for rec, cls in items:
        if cls["tier"] != cur:
            cur = cls["tier"]
            print(f"\n== {names[cur]}")
            print(f"{'#':>4}  {'id':<30} {'plan':<17} {'size':<10} {'n':>2}  {'pending':<38} {'est':>7}  note")
        s = status(rec, args.source)
        pend = [x for x in s["pending"] if want is None or x in want]
        if cls["tier"] == 0:
            left = ",".join(s["pending"]) or "-"
            print(f"{'':>4}  {rec['id']:<30} {rec['body_plan']:<17} {rec['size']:<10} {'':>2}  "
                  f"{'done' if not s['pending'] else 'pilot, pending ' + left:<38} {'':>7}  skipped (pilot)")
            counts["pilot"] += 1
            continue
        t = estimate(rec, s, args, want)
        k += 1
        mark = "" if runnable(cls, rec, args) else " [not run by default]"
        sk = "".join(f"; {st} skipped: {why}" for st, why in s["skipped"].items())
        print(f"{k:>4}  {rec['id']:<30} {rec['body_plan']:<17} {rec['size']:<10} "
              f"{ref_n(rec, args) if 'ref' in pend else '':>2}  {','.join(pend) or 'done':<38} {hms(t):>7}  "
              f"{cls['status']}: {cls['note']}{sk}{mark}")
        if pend:
            counts["pending"] += 1
            per_tier[cls["tier"]] += t
            if runnable(cls, rec, args):
                total += t
                n_run += 1
        else:
            counts["done"] += 1
    print("\n== totals")
    for t in sorted(per_tier):
        print(f"  {names[t]}: {hms(per_tier[t])}")
    print(f"  pilots {counts['pilot']}, monsters with work {counts['pending']}, already done {counts['done']}")
    print(f"  estimated batch time (runs by default: {n_run} monsters): {hms(total)} "
          f"({total / 3600:.1f} h); with tier 4: {hms(sum(per_tier.values()))}")
    return 0


# ---------------------------------------------------------------- progress file
class Progress:
    """out/<source>/_batch.json (or --batch-dir): per monster stages done, seconds, errors,
    rig quality, missing clips; sessions with wall time. Rewritten after every stage."""

    def __init__(self, path: Path, write: bool = True):
        self.path, self.write = path, write
        self.data = json.loads(path.read_text()) if path.exists() else {"monsters": {}, "sessions": [], "events": []}
        self.data.setdefault("monsters", {})
        self.data.setdefault("sessions", [])
        self.data.setdefault("events", [])

    def session(self, args) -> dict:
        s = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "t0": time.time(), "argv": sys.argv[1:],
             "stages_run": 0, "failures": 0}
        self.data["sessions"].append(s)
        return s

    def event(self, kind: str, **kw) -> None:
        self.data["events"].append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **kw})
        self.data["events"] = self.data["events"][-500:]
        self.save()

    def monster(self, rec: dict, cls: dict) -> dict:
        m = self.data["monsters"].setdefault(rec["id"], {"stages": {}, "errors": [], "seconds": 0.0})
        m.update({"plan": rec["body_plan"], "tier": cls["tier"], "status": cls["status"]})
        return m

    def refresh(self, rec: dict, source: str) -> None:
        """Re-read the monster's manifest and outputs: done stages, rig quality, missing clips."""
        m = self.data["monsters"].setdefault(rec["id"], {"stages": {}, "errors": [], "seconds": 0.0})
        s = status(rec, source)
        m["done"] = [x for x in STAGES if s["done"][x]]
        m["pending"] = s["pending"]
        m["skipped"] = s["skipped"]
        rm = s["man"].get("rig") if isinstance(s["man"].get("rig"), dict) else {}
        q = viewer.rig_quality(rm)
        m["rig_quality"] = None if q is None else {
            "score": q.get("score"), "problems": q.get("problems"), "joints": q.get("joints"), "tool": rm.get("tool"),
            "facing_ok": (q.get("facing") or {}).get("ok"), "facing_deg": (q.get("facing") or {}).get("angle_deg"),
            "facing_conf": (q.get("facing") or {}).get("confidence"), "sockets_missing": rm.get("sockets_missing")}
        found = set(s["st"]["found_clips"])
        m["missing_clips"] = [c for c in rec.get("animation_set") or [] if c not in found] if found else None
        m["manifest_errors"] = [{k: e.get(k) for k in ("stage", "error", "at")} for e in s["man"].get("errors") or []]

    def save(self) -> None:
        if not self.write:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=1) + "\n")
        tmp.replace(self.path)


# ---------------------------------------------------------------- ComfyUI and memory guards
def comfy_ok(timeout: float = 10) -> bool:
    try:
        fg.http("GET", "/queue", timeout=timeout)
        return True
    except Exception:
        return False


def restart_comfy(prog: Progress, why: str, wait_s: float = 300) -> bool:
    fg.log(f"batch: restarting ComfyUI ({why})")
    t0 = time.time()
    r = subprocess.run(["systemctl", "--user", "restart", "comfyui"], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        fg.log(f"batch: systemctl restart failed: {r.stderr.strip()[:300]}")
    up = False
    while time.time() - t0 < wait_s:
        try:
            fg.http("GET", "/system_stats", timeout=5)
            up = True
            break
        except Exception:
            time.sleep(3)
    dt = round(time.time() - t0, 1)
    fg.log(f"batch: ComfyUI {'up' if up else 'NOT up'} after {dt}s")
    prog.event("comfy_restart", why=why, seconds=dt, up=up)
    return up


def ensure_comfy(prog: Progress) -> None:
    """Before a ComfyUI stage: /queue must answer within HANG_S, else restart."""
    t0 = time.time()
    while time.time() - t0 < HANG_S:
        if comfy_ok():
            return
        time.sleep(5)
    restart_comfy(prog, f"/queue silent for {HANG_S:.0f}s before a stage")


def memory_gate(prog: Progress, min_gb: float, label: str) -> None:
    """Wait until MemAvailable (free's 'available') >= min_gb. Short at once: ask ComfyUI to
    /free (between stages nothing is queued; what it holds is the previous stage's models:
    after a ref ComfyUI keeps SDXL + DWPose at ~14 GB RSS, which left 6.5 GB on the smoke
    run). After 15 min restart it; keeps waiting after that, logging every minute."""
    t0, freed, restarted, last = time.time(), False, False, 0.0
    while True:
        avail = paint.mem_available_gb()
        if avail >= min_gb:
            if time.time() - t0 > 1:
                prog.event("memory_wait", stage=label, seconds=round(time.time() - t0, 1), available_gb=round(avail, 1),
                           freed=freed, restarted=restarted)
            return
        el = time.time() - t0
        if el - last >= 60 or last == 0:
            fg.log(f"batch: {label}: {avail:.1f} GB available < {min_gb} GB, waiting ({el:.0f}s)")
            last = el or 1
        if not freed:
            fg.log(f"batch: {label}: asking ComfyUI to /free")
            paint.free_comfy(fg)
            freed = True
        if el > 900 and not restarted:
            restart_comfy(prog, f"memory gate: {avail:.1f} GB available for {el:.0f}s")
            restarted = True
        time.sleep(10)


class Watchdog(threading.Thread):
    """Polls /queue every WATCH_POLL s while a stage runs; sets .hung when it has not answered for HANG_S."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop, self.hung, self.last_ok = threading.Event(), False, time.time()

    def run(self):
        while not self.stop.wait(WATCH_POLL):
            if comfy_ok(timeout=10):
                self.last_ok = time.time()
            elif time.time() - self.last_ok > HANG_S:
                self.hung = True
                return


def add_manifest_error(d: Path, stage: str, text: str) -> None:
    man = fg.load_manifest(d)
    man.setdefault("errors", []).append({"stage": stage, "error": text[:2000], "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    fg.save_manifest(d, man)


def last_manifest_error(d: Path, stage: str) -> str | None:
    errs = [e for e in fg.load_manifest(d).get("errors") or [] if e.get("stage") == stage]
    return errs[-1].get("error") if errs else None


def forge_cmd(stage: str, rec: dict, args, force: bool = False) -> list:
    cmd = [sys.executable, "-I", str(FORGE), stage, rec["id"], "--source", args.source]
    if stage == "ref":
        cmd += ["--n", str(ref_n(rec, args))]
    if force:
        cmd.append("--force")  # a stale stage (status()["stale"]): forge would skip it otherwise
    return cmd


def run_stage(stage: str, rec: dict, d: Path, args, prog: Progress, logf, force: bool = False) -> dict:
    """One forge.py stage in its own process group with a timeout and the /queue watchdog.
    A hung ComfyUI kills the stage, restarts the unit and retries the stage once."""
    timeout = timeout_for(stage, rec, d, args)
    for attempt in (1, 2):
        if stage in COMFY_STAGES:
            ensure_comfy(prog)
        cmd = forge_cmd(stage, rec, args, force)
        logf.write(f"\n=== {time.strftime('%H:%M:%S')} {' '.join(cmd)} (timeout {timeout:.0f}s)\n")
        logf.flush()
        t0 = time.time()
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True, cwd=ROOT)
        wd = Watchdog()
        wd.start()
        outcome = None
        while outcome is None:
            try:
                proc.wait(timeout=2)
                outcome = "exit"
            except subprocess.TimeoutExpired:
                if time.time() - t0 > timeout:
                    outcome = "timeout"
                elif wd.hung:
                    outcome = "hung"
        wd.stop.set()
        if outcome != "exit":
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=20)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                proc.wait()
        dt = round(time.time() - t0, 1)
        if outcome == "hung":
            prog.event("comfy_hung", id=rec["id"], stage=stage, seconds=dt, attempt=attempt)
            restart_comfy(prog, f"/queue silent {HANG_S:.0f}s during {rec['id']} {stage}")
            if attempt == 1:
                fg.log(f"batch: {rec['id']}: {stage} retry after the ComfyUI restart")
                continue
            text = f"batch: ComfyUI stopped answering /queue during {stage} (twice); stage killed"
            add_manifest_error(d, stage, text)
            return {"status": "failed", "seconds": dt, "error": text}
        if outcome == "timeout":
            if stage in COMFY_STAGES:
                try:
                    fg.http("POST", "/interrupt", b"", timeout=10)
                except Exception:
                    pass
            text = f"batch: {stage} timed out after {timeout:.0f}s (killed)"
            add_manifest_error(d, stage, text)
            return {"status": "timeout", "seconds": dt, "error": text}
        if proc.returncode != 0:
            return {"status": "failed", "seconds": dt,
                    "error": last_manifest_error(d, stage) or f"forge.py {stage} exited {proc.returncode}"}
        return {"status": "ok", "seconds": dt}
    return {"status": "failed", "seconds": 0, "error": "unreachable"}


def facing_gate(rec: dict, d: Path) -> dict:
    """rig.facing_check on model.glb (Blender only) before paint and the rolls: a misfaced
    mesh would be refused by the rig stage after ~3 min of paint, and fixing it (pin yaw,
    qa --force) deletes the paint again."""
    f = rig.facing_check(fg, rec, d)
    man = fg.load_manifest(d)
    if f["ok"]:  # a pinned yaw fixed it: drop this gate's earlier refusal (the report reads manifest errors)
        errs = man.get("errors") or []
        keep = [e for e in errs if not str(e.get("error", "")).startswith("batch facing gate:")]
        rm = man.get("rig")
        placeholder = isinstance(rm, dict) and rm.get("error") == "facing check failed" and set(rm) <= {"error", "facing"}
        if len(keep) != len(errs) or placeholder:
            man["errors"] = keep
            if placeholder:
                man.pop("rig")
            fg.save_manifest(d, man)
    else:
        man.setdefault("rig", {"error": "facing check failed"})["facing"] = f
        fg.save_manifest(d, man)
        add_manifest_error(d, "rig", f"batch facing gate: model.glb faces {f['angle_deg']} deg off +Y "
                                     f"(confidence {f['confidence']}); pin `yaw` (about {f.get('suggested_yaw')}) "
                                     "and re-run qa, or set rig_facing_check false")
    return f


def run_facing_gate(rec: dict, d: Path, m: dict, prog: Progress, args) -> bool:
    """facing_gate() with the bookkeeping: m["stages"]["facing"], an errors entry when
    refused. False = refused (leave the monster before paint)."""
    t0 = time.time()
    try:
        f = facing_gate(rec, d)
    except Exception as e:  # a Blender failure here is not a verdict: let rig decide
        f = {"ok": True, "error": str(e)[:300]}
        fg.log(f"batch: {rec['id']}: facing gate failed to run ({e}); continuing")
    m["stages"]["facing"] = {"status": "ok" if f["ok"] else "refused", "angle_deg": f.get("angle_deg"),
                             "confidence": f.get("confidence"), "suggested_yaw": f.get("suggested_yaw"),
                             "seconds": round(time.time() - t0, 1), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if not f["ok"]:
        m["errors"].append({"stage": "facing", "error": f"{f['angle_deg']} deg off +Y, confidence "
                            f"{f['confidence']}, suggested yaw {f.get('suggested_yaw')}",
                            "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        fg.log(f"batch: {rec['id']}: facing gate refused ({f['angle_deg']} deg); next monster")
    prog.refresh(rec, args.source)
    prog.save()
    return bool(f["ok"])


# ---------------------------------------------------------------- run
def cmd_run(args) -> int:
    reg = fg.load_registry(args.source)
    items = [(r, c) for r, c in work_list(reg, args) if runnable(c, r, args)]
    want = [s for s in STAGES if not args.stages or s in args.stages.split(",")]
    bdir = Path(args.batch_dir) if args.batch_dir else fg.OUT_ROOT / args.source
    prog = Progress(bdir / "_batch.json", write=not args.dry_run)
    sess = prog.session(args) if not args.dry_run else {}
    logdir = bdir / "_batch_logs"
    failures, total_est = 0, 0.0
    fast_streak = 0  # monsters in a row whose stage failed within FAST_FAIL_S: a broken environment
    fg.log(f"batch: {len(items)} monsters, stages {','.join(want)}{' (dry run)' if args.dry_run else ''}")
    for i, (rec, cls) in enumerate(items, 1):
        mid = rec["id"]
        s = status(rec, args.source)
        todo = [x for x in s["pending"] if x in want]
        if not todo:
            continue
        d = s["dir"]
        if args.dry_run:
            t = estimate(rec, s, args, want)
            total_est += t
            print(f"[{i}/{len(items)}] {mid} (tier {cls['tier']}, {rec['body_plan']}, {rec['size']}): ~{hms(t)}")
            gate_done = False
            for st in todo:
                if not gate_done and st in ("paint", "lod", "rig") and needs_facing_gate(rec, d, todo, args):
                    print("    facing gate: rig.facing_check on model.glb (Blender); stop this monster if refused")
                    gate_done = True
                if st in restart_stages(args):
                    print("    systemctl --user restart comfyui; wait for /system_stats")
                if st in ("mesh", "paint"):
                    print(f"    memory gate: wait for >= {args.min_free_gb} GB available")
                print(f"    {' '.join(forge_cmd(st, rec, args, st in s['stale'])[2:])}   "
                      f"(timeout {timeout_for(st, rec, d, args):.0f}s)" + (f"  [stale: {s['stale'][st]}]" if st in s["stale"] else ""))
                if st == "qa" and not gate_done and needs_facing_gate(rec, d, s["pending"], args):
                    print("    facing gate: rig.facing_check on model.glb (Blender); stop this monster if refused")
                    gate_done = True
            for st, why in s["skipped"].items():
                if st in want:
                    print(f"    {st}: skipped ({why})")
            continue
        m = prog.monster(rec, cls)
        logdir.mkdir(parents=True, exist_ok=True)
        fg.log(f"batch [{i}/{len(items)}] {mid}: pending {','.join(todo)}")
        ran, gate_passed, fast_fail = set(), False, None
        with open(logdir / f"{mid}.log", "a") as logf:
            while True:
                s = status(rec, args.source)
                todo = [x for x in s["raw_pending"] if x in want]
                if not todo:
                    break
                st = todo[0]
                if st in ran:  # forge returned ok but the outputs still read as missing
                    text = f"batch: {st} ran and exited 0 but still reads as not done (forge skipped it?); re-run with --force"
                    m["errors"].append({"stage": st, "error": text, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                    fg.log(f"batch: {mid}: {text}")
                    failures += 1
                    break
                if not gate_passed and st in ("paint", "lod", "rig") and needs_facing_gate(rec, d, s["pending"], args):
                    gate_passed = True
                    if not run_facing_gate(rec, d, m, prog, args):
                        failures += 1
                        break
                if st in restart_stages(args):
                    restart_comfy(prog, f"before {mid} {st}")
                if st in ("mesh", "paint"):
                    memory_gate(prog, args.min_free_gb, f"{mid} {st}")
                ran.add(st)
                res = run_stage(st, rec, d, args, prog, logf, force=st in s["stale"])
                res["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                m["stages"][st] = res
                m["seconds"] = round(m.get("seconds", 0) + res["seconds"], 1)
                sess["stages_run"] += 1
                if res["status"] != "ok":
                    m["errors"].append({"stage": st, "error": res.get("error"), "at": res["at"]})
                    if res["status"] == "failed" and res["seconds"] < FAST_FAIL_S:
                        fast_fail = f"{st}: {str(res.get('error'))[:200]}"
                    failures += 1
                    sess["failures"] += 1
                fg.log(f"batch: {mid}: {st} {res['status']} in {res['seconds']}s")
                prog.refresh(rec, args.source)
                sess["wall_s"] = round(time.time() - sess["t0"], 1)
                prog.save()
                if res["status"] != "ok":
                    break  # move on to the next monster
                # A ref,mesh,qa pass stops before paint, where the gate would run: check the
                # facing right after qa so refusals (pin `yaw`) are known before the paint pass.
                if st == "qa" and not gate_passed and needs_facing_gate(rec, d, status(rec, args.source)["pending"], args):
                    gate_passed = True
                    if not run_facing_gate(rec, d, m, prog, args):
                        failures += 1
                        break
        prog.refresh(rec, args.source)
        sess["wall_s"] = round(time.time() - sess["t0"], 1)
        prog.save()
        fast_streak = fast_streak + 1 if fast_fail else (0 if ran else fast_streak)
        if args.max_fast_failures and fast_streak >= args.max_fast_failures:
            prog.event("stopped", why=f"{fast_streak} monsters in a row failed within {FAST_FAIL_S:.0f}s", last=fast_fail)
            fg.log(f"batch: stopping: {fast_streak} monsters in a row failed a stage within {FAST_FAIL_S:.0f}s "
                   f"(last {mid} {fast_fail}); the environment is broken (ComfyUI, disk, quota?), not the monsters")
            failures = max(failures, 1)
            break
        if args.stop_after_failures and failures >= args.stop_after_failures:
            fg.log(f"batch: stopping after {failures} failures (--stop-after-failures {args.stop_after_failures})")
            break
    if args.dry_run:
        print(f"\nestimated: {hms(total_est)} ({total_est / 3600:.1f} h)")
        return 0
    sess["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    sess["wall_s"] = round(time.time() - sess["t0"], 1)
    prog.save()
    fg.log(f"batch: done, {sess['stages_run']} stages, {failures} failures, wall {hms(sess['wall_s'])}")
    return 1 if failures else 0


# ---------------------------------------------------------------- report
def cmd_report(args) -> int:
    reg = fg.load_registry(args.source)
    pilot_keys = {(reg[p]["body_plan"], reg[p]["rig_template"]): p for p in PILOTS if p in reg}
    bdir = Path(args.batch_dir) if args.batch_dir else fg.OUT_ROOT / args.source
    bj = viewer.load_json(bdir / "_batch.json") or {"monsters": {}, "sessions": [], "events": []}
    rows = []
    for rec in reg.values():
        s = status(rec, args.source)
        rm = s["man"].get("rig") if isinstance(s["man"].get("rig"), dict) else {}
        q = viewer.rig_quality(rm) or {}
        found = set(s["st"]["found_clips"])
        rows.append({"rec": rec, "cls": classify(rec, pilot_keys), "s": s, "q": q, "rig_m": rm,
                     "missing": [c for c in rec.get("animation_set") or [] if c not in found] if found else None,
                     "errors": s["man"].get("errors") or [], "b": (bj.get("monsters") or {}).get(rec["id"], {}),
                     "unarmed": viewer.ref_unarmed(s["man"].get("ref") if isinstance(s["man"].get("ref"), dict)
                                                   else {})})
    n = len(rows)
    L = [f"# Batch report: {args.source} ({time.strftime('%Y-%m-%d %H:%M')})", ""]
    # totals and wall time
    sessions = bj.get("sessions") or []
    wall = sum(x.get("wall_s", 0) for x in sessions)
    stage_s = collections.defaultdict(float)
    for m in (bj.get("monsters") or {}).values():
        for st, r in (m.get("stages") or {}).items():
            stage_s[st] += r.get("seconds") or 0
    done_all = sum(1 for r in rows if not r["s"]["pending"])
    complete = sum(1 for r in rows if r["missing"] == [])
    L += ["## Totals", "",
          f"- {n} records, {done_all} with every stage done, {complete} with every animation_set clip "
          f"(viewer anim_complete).",
          f"- Batch sessions: {len(sessions)}, wall time {hms(wall)}"
          + (f", stage time {hms(sum(stage_s.values()))} (" + ", ".join(f"{k} {hms(v)}" for k, v in
                                                                         sorted(stage_s.items())) + ")"
             if stage_s else "") + ".",
          f"- ComfyUI restarts: {sum(1 for e in bj.get('events') or [] if e.get('kind') == 'comfy_restart')}, "
          f"hangs: {sum(1 for e in bj.get('events') or [] if e.get('kind') == 'comfy_hung')}, memory waits: "
          f"{sum(1 for e in bj.get('events') or [] if e.get('kind') == 'memory_wait')}.", ""]
    # counts per stage
    L += ["## Stages", "", "| stage | done | pending | skipped | failed (manifest errors) |", "|---|---:|---:|---:|---:|"]
    for st in STAGES:
        done = sum(1 for r in rows if r["s"]["done"][st] and st not in r["s"]["skipped"])
        sk = sum(1 for r in rows if st in r["s"]["skipped"])
        pend = sum(1 for r in rows if st in r["s"]["pending"])
        fail = sum(1 for r in rows if any(e.get("stage") == st for e in r["errors"]))
        L.append(f"| {st} | {done} | {pend} | {sk} | {fail} |")
    # failures by stage
    L += ["", "## Failures by stage", ""]
    by_stage = collections.defaultdict(list)
    for r in rows:
        for e in r["errors"]:
            by_stage[e.get("stage", "?")].append((r["rec"]["id"], e.get("error", ""), e.get("at", "")))
        for e in r["b"].get("errors") or []:
            refused = ((r["b"].get("stages") or {}).get("facing") or {}).get("status") == "refused"
            if (e.get("stage") == "facing" and refused) or "still reads" in str(e.get("error", "")):
                by_stage[e["stage"]].append((r["rec"]["id"], e.get("error", ""), e.get("at", "")))
    if not by_stage:
        L.append("None.")
    for st in sorted(by_stage, key=lambda x: STAGES.index(x) if x in STAGES else 99):
        L += [f"### {st} ({len(by_stage[st])})", ""]
        for mid, err, at in by_stage[st]:
            L.append(f"- `{mid}` {at}: {' '.join(str(err).split())[:400]}")
        L.append("")
    # rig problems by plan
    L += ["## Rig problems by plan", ""]
    probs = collections.defaultdict(list)
    for r in rows:
        q = r["q"]
        f = q.get("facing") or {}
        issues = list(q.get("problems") or [])
        if f and not f.get("ok", True):
            issues.append(f"facing {f.get('angle_deg')} deg")
        if r["rig_m"].get("sockets_missing"):
            issues.append("sockets missing " + ",".join(r["rig_m"]["sockets_missing"]))
        if issues:
            probs[r["rec"]["body_plan"]].append(f"`{r['rec']['id']}` ({q.get('score')}: {', '.join(issues)})")
    if not probs:
        L.append("None.")
    for plan in sorted(probs):
        L.append(f"- **{plan}** ({len(probs[plan])}): " + "; ".join(probs[plan]))
    # every monster the batch touched: stage seconds, facing, stale stages, the unarmed pick
    L += ["", "## Batch monsters", "",
          "| id | plan | tier | done | pending | ref s | mesh s | qa s | facing | unarmed pick (scores) | notes |",
          "|---|---|---:|---|---|---:|---:|---:|---|---|---|"]
    for r in rows:
        b = r["b"]
        if not b:
            continue
        stg = b.get("stages") or {}
        sec = lambda k: "" if k not in stg else f"{stg[k].get('seconds')}" + ("" if stg[k].get("status") == "ok" else
                                                                             f" {stg[k].get('status')}")
        fc = stg.get("facing") or {}
        facing = "" if not fc else f"{fc.get('status')} {fc.get('angle_deg')} deg"
        u = r["unarmed"] or {}
        ref_m = r["s"]["man"].get("ref") if isinstance(r["s"]["man"].get("ref"), dict) else {}
        unarmed = "" if not u else f"{ref_m.get('picked')} by {ref_m.get('picked_by')} ({', '.join(str(x) for x in u['scores'])})"
        notes = [f"stale {k}: {v}" for k, v in r["s"]["stale"].items()]
        if u.get("warning") or u.get("warning_pick"):
            notes.append("unarmed: " + str(u.get("warning_pick") or u.get("warning")))
        L.append(f"| `{r['rec']['id']}` | {r['rec']['body_plan']} | {r['cls']['tier']} | "
                 f"{','.join(s_ for s_ in STAGES if r['s']['done'][s_]) or '-'} | {','.join(r['s']['pending']) or '-'} | "
                 f"{sec('ref')} | {sec('mesh')} | {sec('qa')} | {facing} | {unarmed} | {'; '.join(notes) or '-'} |")
    # review list sorted by rig score
    L += ["", "## Needs review (lowest rig score first)", "",
          "| id | plan | tier | rig score | problems | missing clips | other |", "|---|---|---:|---:|---|---|---|"]
    review = []
    for r in rows:
        q, other = r["q"], []
        if not q and r["missing"] is None:
            continue  # not rigged or animated yet
        f = q.get("facing") or {}
        if f and (not f.get("ok", True) or (f.get("confidence") or 1) < rig.FACING_CONF):
            other.append(f"facing {f.get('angle_deg')} deg conf {f.get('confidence')}")
        if r["rig_m"].get("sockets_missing"):
            other.append("sockets " + ",".join(r["rig_m"]["sockets_missing"]))
        u = r["unarmed"] or {}
        if u.get("warning") or u.get("warning_pick"):
            other.append("unarmed check: " + (u.get("warning_pick") or "every candidate armed"))
        score = q.get("score")
        if r["missing"] is None and r["s"]["done"]["rig"] and "anim" not in r["s"]["skipped"]:
            other.append("rigged, no clips")
        # fallback skeletons score low by design (docs/rig-anim.md): only rolls count against the threshold
        low = score is not None and score < args.review_below and \
            not str(r["rig_m"].get("tool") or "").startswith("fallback")
        if q.get("problems") or r["missing"] or other or low:
            review.append((score if score is not None else 99, r, other))
    review.sort(key=lambda x: (x[0], x[1]["rec"]["id"]))
    for score, r, other in review:
        L.append(f"| `{r['rec']['id']}` | {r['rec']['body_plan']} | {r['cls']['tier']} | "
                 f"{'' if score == 99 else score} | {', '.join(r['q'].get('problems') or []) or '-'} | "
                 f"{', '.join(r['missing'] or []) or '-'} | {'; '.join(other) or '-'} |")
    if not review:
        L.append("| - | | | | | | |")
    out = bdir / "_batch.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n")
    stage_line = ", ".join(f"{st} {sum(1 for r in rows if r['s']['done'][st])}" for st in STAGES)
    print(f"{args.source}: {n} records; done per stage: {stage_line}")
    print(f"every stage done: {done_all}; anim complete: {complete}; failures: "
          f"{sum(len(v) for v in by_stage.values())} in {len(by_stage)} stages; rig problems: "
          f"{sum(len(v) for v in probs.values())}; review: {len(review)}; wall {hms(wall)}")
    print(f"wrote {out}")
    return 0


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--source", default="srd-5.2.1")
        p.add_argument("--plans", help="comma list of body plans")
        p.add_argument("--ids", help="comma list of monster ids (runs them even when on hold)")
        p.add_argument("--tier", help="tiers: 1, 1,2 or 1-3 (0 = pilots, 4 = hold); default all but hold for run")
        p.add_argument("--stages", help=f"comma subset of {','.join(STAGES)}")
        p.add_argument("--n", type=int, default=3, help="ref candidates (unarmed_reference monsters: at least 4)")
        p.add_argument("--include-pilots", action="store_true", help="run the pilots too")
        p.add_argument("--no-restart", action="store_true", help="no ComfyUI restart before mesh and paint")
        p.add_argument("--restart-before", default=",".join(RESTART_BEFORE),
                       help="stages to restart ComfyUI before (default %(default)s)")
        p.add_argument("--no-facing-gate", action="store_true", help="no facing check before paint / rig")
        p.add_argument("--batch-dir", help="where _batch.json / _batch.md / _batch_logs go (default out/<source>)")
        p.add_argument("--timeout-scale", type=float, default=TIMEOUT_SCALE)
        p.add_argument("--timeout-floor", type=float, default=TIMEOUT_FLOOR)
        p.add_argument("--min-free-gb", type=float, default=MIN_FREE_GB)

    common(sub.add_parser("plan", help="ordered work list and time estimate"))
    pr = sub.add_parser("run", help="run the batch (resumable)")
    common(pr)
    pr.add_argument("--dry-run", action="store_true", help="print what it would do; touch nothing")
    pr.add_argument("--stop-after-failures", type=int, default=0, help="stop after N failed stages (0 = never)")
    pr.add_argument("--max-fast-failures", type=int, default=MAX_FAST_FAILURES,
                    help=f"stop after N monsters in a row fail a stage within {FAST_FAIL_S:.0f}s (0 = never)")
    rp = sub.add_parser("report", help="write <batch-dir>/_batch.md and print a summary")
    rp.add_argument("--source", default="srd-5.2.1")
    rp.add_argument("--batch-dir")
    rp.add_argument("--review-below", type=float, default=-2.0, help="rig scores below this need review")
    au = sub.add_parser("audit", help="coverage tables and the gap list (markdown)")
    au.add_argument("--source", default="srd-5.2.1")
    args = ap.parse_args(argv)
    if getattr(args, "stages", None):
        bad = [x for x in args.stages.split(",") if x not in STAGES]
        if bad:
            ap.error(f"--stages: unknown {bad}; choose from {STAGES}")
    return {"plan": cmd_plan, "run": cmd_run, "report": cmd_report, "audit": cmd_audit}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
