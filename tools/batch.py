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
    python3 -I tools/batch.py facing --stale              # re-run gates recorded by older gate code

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
# Before a ComfyUI stage the queue must drain within this many seconds, else clear + restart.
# Tier-1 pass 2026-11-05: a killed mesh stage left forge's second mesh job queued (/interrupt
# stops the running job only), and the next 1-3 refs sat in forge.wait_idle until they timed out.
QUEUE_BUSY_S = 60.0
# What each stage's "done" reads from (viewer.stage_status, rig_done): a file a killed stage wrote
# in the attempt is removed so the stage reads pending (the manifest record may be missing).
DONE_MARKERS = {"ref": ["ref.png"], "mesh": ["model_raw.glb"], "qa": ["model.glb"],
                "paint": ["textures/base_color_painted.png"], "lod": ["lods.json"], "rig": ["model_rigged.glb"]}

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
    ("facing_wings", "winged bodies: QA's auto-yaw takes the body axis from the mirror plane or the thick, low "
                     "surface, 90% within 15 deg on disk; the rest are refused past 30 deg or held unverified",
     lambda r: r["body_plan"] in ("winged_quadruped", "winged_biped", "avian") and r["id"] not in PILOTS, False,
     "pin `yaw`, then qa, paint, lod, rig, anim", "the batch's facing gate stops these after qa, before paint; "
     "unverified ones wait for a reviewer"),
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


def status(rec: dict, source: str, args=None) -> dict:
    """{done: {stage: bool}, pending: [...] in RUN_ORDER (with downstream invalidation),
    skipped: {stage: why}, man, st} for one record. `args` carries --reroll-changed-prompts."""
    d = mon_dir(source, rec["id"])
    man = fg.load_manifest(d) if d.is_dir() else {}
    st = viewer.stage_status(d, man)
    done = dict(st["stages"])
    done["rig"] = rig_done(d, man, st)
    stale = {}
    ref_prompt = False  # the ref is stale for a changed prompt (the stale-pin rule follows it)
    why = ref_partial(d)
    if why:  # the viewer counts candidates alone as a reference; the mesh needs the pick
        done["ref"], stale["ref"] = False, why
    elif done["ref"]:
        why = prompt_stale(rec, d, man, args)
        if why:  # forge skips an existing ref.png: --force (and mesh follows: ref.png newer)
            done["ref"], stale["ref"], ref_prompt = False, why, True
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
            "skipped": skipped, "stale": stale, "ref_prompt": ref_prompt, "man": man, "st": st, "dir": d}


def ref_partial(d: Path) -> str | None:
    """Why the reference is incomplete, or None (complete, or nothing drawn yet). Done means
    the candidates (ref_*.png) AND the pick (ref.png, written by forge after the candidates)
    exist, the pick no older than the candidates. A ref killed by a timeout after drawing
    leaves candidates without ref.png (tier-1 pass: warrior-infantry, warrior-veteran,
    efreeti); a `ref --force` killed mid-way leaves the old ref.png with no or newer
    candidates. forge has no "pick from the kept candidates" path: its skip needs both files,
    anything else deletes ref_*.png and draws again (same seed, so the same candidates), so
    the batch re-runs ref, with --force (stale) because forge would skip an old ref.png
    next to candidates."""
    if not d.is_dir():
        return None
    refs = list(d.glob("ref_*.png"))
    pick = d / "ref.png"
    if refs and not pick.exists():
        return f"{len(refs)} candidates but no ref.png (the ref stage stopped before the pick)"
    if pick.exists() and not refs:
        return "ref.png without candidates (a ref re-roll stopped after deleting them)"
    if refs and pick.stat().st_mtime + 1 < max(p.stat().st_mtime for p in refs):
        return "ref.png is older than the candidates (a ref re-roll stopped before the pick)"
    return None


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
    sp = stale_pin(rec, d, man)
    if sp:  # the pin was chosen from the strip before a prompt re-roll: the stage's own pick counts
        if made is not None and int(made) != sp["auto"]:
            return f"mesh made from ref_{made}, the re-rolled strip's own pick is ref_{sp['auto']} (stale ref_pick {pin})"
        return None
    if pin is not None and made is not None and int(pin) != int(made):
        return f"override ref_pick {pin} != the candidate the mesh was made from ({made})"
    return None


# ---------------------------------------------------------------- stale ref_pick pins
# forge honours an override `ref_pick` in both the ref stage (ref.png = ref_<pin>) and the
# mesh stage, also right after a re-roll that drew a new strip: the pin then names a
# candidate nobody looked at (pass 3: dryad, medusa, sea-hag meshed the wrong one). After a
# ref it re-rolled for a changed prompt the batch stamps manifest.ref.prompt_reroll; a pin
# in an override file no newer than the new candidates is then stale: the batch meshes the
# ref stage's own pick (the unarmed check's best, else 0) with `forge.py mesh --pick`, and
# `plan` / `report` list the monster under "Pins to re-choose". A pin written after the
# re-roll (override newer than the candidates) is honoured as before. NB any later write of
# the override file (a note, a yaw) makes its pin count as chosen after the re-roll.
def auto_pick(ref: dict, n: int) -> int:
    """The ref stage's own pick without a pin (forge.stage_ref): the unarmed check's first
    empty-handed candidate, else 0."""
    chk = ref.get("unarmed") if isinstance(ref, dict) else None
    p = chk["best"] if isinstance(chk, dict) and isinstance(chk.get("best"), int) else 0
    return max(0, min(p, n - 1)) if n else 0


def _ov_pin(rec: dict) -> tuple:
    """(ref_pick or None, override file mtime or None, override dict)."""
    try:
        ov = overrides.load(rec["source"], rec["id"])
    except Exception:
        return None, None, {}
    p = overrides.path_for(rec["source"], rec["id"])
    return ov.get("ref_pick"), (p.stat().st_mtime if p.exists() else None), ov


def stale_pin(rec: dict, d: Path, man: dict) -> dict | None:
    """{pin, auto, reason} when the override's ref_pick predates the prompt re-roll that drew
    the current candidates (manifest.ref.prompt_reroll, stamped by the batch), else None."""
    ref = man.get("ref") if isinstance(man.get("ref"), dict) else {}
    rr = ref.get("prompt_reroll")
    if not rr:
        return None
    pin, t_ov, _ = _ov_pin(rec)
    refs = list(d.glob("ref_*.png")) if d.is_dir() else []
    if pin is None or not refs:
        return None
    if t_ov is not None and t_ov > max(p.stat().st_mtime for p in refs) + 1:
        return None  # pinned after the re-roll: a choice from this strip
    auto = auto_pick(ref, len(refs))
    return {"pin": int(pin), "auto": auto,
            "reason": f"ref_pick {pin} predates the prompt re-roll of {rr.get('at')} ({rr.get('reason')}); "
                      f"the mesh uses the strip's own pick ref_{auto}"}


def pin_predates_strip(rec: dict, d: Path, man: dict) -> str | None:
    """A reference re-rolled before the stale-pin rule (no prompt_reroll stamp) whose pin, set
    together with a prompt field, is older than its candidates: forge meshed the pin blind.
    Listed only (the mesh is not redone): re-choose and pin again."""
    ref = man.get("ref") if isinstance(man.get("ref"), dict) else {}
    if not ref or ref.get("prompt_reroll") or ref.get("picked_by") != "pinned":
        return None
    pin, t_ov, ov = _ov_pin(rec)
    refs = list(d.glob("ref_*.png")) if d.is_dir() else []
    if pin is None or t_ov is None or not refs or not any(k in ov for k in PROMPT_FIELDS):
        return None
    if t_ov < min(p.stat().st_mtime for p in refs) - 1:
        return (f"ref_pick {pin} (override with a prompt field) is older than the candidates: the re-roll "
                "kept the pin blind (before the stale-pin rule); look at the strip and pin again")
    return None


def pin_notes(rec: dict, s: dict) -> str | None:
    """Why the monster's ref_pick must be chosen again, for plan and report, or None."""
    sp = stale_pin(rec, s["dir"], s["man"])
    if sp:
        return f"stale pin: {sp['reason']}"
    if s.get("ref_prompt") and _ov_pin(rec)[0] is not None:
        return (f"the ref re-rolls for a changed prompt with ref_pick {_ov_pin(rec)[0]} pinned: the mesh will use "
                "the new strip's own pick; pin again after a look")
    why = pin_predates_strip(rec, s["dir"], s["man"])
    return f"unconfirmed: {why}" if why else None


# ---------------------------------------------------------------- prompt fingerprints
# A changed prompt (override prompt_extra / negative_extra / ref_template / swarm_unit / style,
# a per-id or per-plan entry in data/prompt_tweaks.json) re-rolls the reference. forge records
# the final strings in manifest.ref (prompt, negative, controlnet.template, n), so the batch
# hashes those after each ref stage it runs (manifest.ref.prompt_fp) and status() compares
# that stamp with the prompt forge would build now. A manifest with no stamp (every reference
# made before this rule) is never stale by it: --reroll-changed-prompts covers those.
PROMPT_FIELDS = ("prompt_extra", "negative_extra", "ref_template", "swarm_unit", "style")


def prompt_fingerprint(prompt: str, negative: str, template: str | None, n: int | None) -> str:
    import hashlib
    blob = json.dumps([" ".join(str(prompt).split()), " ".join(str(negative).split()), template, n])
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def recorded_fingerprint(ref: dict) -> str | None:
    """Fingerprint of what manifest.ref says the reference was drawn with (None: no record)."""
    if not isinstance(ref, dict) or not ref.get("prompt"):
        return None
    return prompt_fingerprint(ref["prompt"], ref.get("negative", ""), (ref.get("controlnet") or {}).get("template"),
                              ref.get("n"))


def current_ref_prompt(rec: dict) -> tuple[str, str, str | None]:
    """(positive, negative, template name) as forge.stage_ref would build them now, without
    ComfyUI: the same steps in the same order; apply_template (an upload) is not called."""
    mon = unit_rec(rec)
    ov = fg.ov_for(mon)
    wf = fg.load_workflow("ref_image.api.json")
    prompt, tweak_neg, _ = fg.ref_prompt(mon)
    if ov.get("prompt_extra"):
        prompt = prompt.rstrip(" ,.") + ", " + ov["prompt_extra"]
    style = fg.load_style(fg.resolve_style(ov.get("style")))
    tpl = fg.ref_template(mon, style, ov)
    if style:
        keep = fg.apply_template
        fg.apply_template = lambda wf_, tpl_: None  # apply_style uploads the pose image through it
        try:
            prompt = fg.apply_style(wf, style, prompt, mon, ov)
        finally:
            fg.apply_template = keep
    if mon.get("unarmed_reference") and style.get("unarmed_suffix"):
        prompt = prompt.rstrip(" ,.") + ", " + style["unarmed_suffix"]
        if style.get("unarmed_negative"):
            wf["neg"]["inputs"]["text"] += ", " + style["unarmed_negative"]
    if tweak_neg:
        wf["neg"]["inputs"]["text"] += ", " + tweak_neg
    if ov.get("negative_extra"):
        wf["neg"]["inputs"]["text"] += ", " + ov["negative_extra"]
    return prompt, wf["neg"]["inputs"]["text"], (tpl or {}).get("name")


def stamp_prompt_fp(d: Path) -> None:
    """After a ref stage the batch ran: manifest.ref.prompt_fp from what forge recorded."""
    man = fg.load_manifest(d)
    ref = man.get("ref")
    fp = recorded_fingerprint(ref)
    if fp:
        ref["prompt_fp"] = fp
        fg.save_manifest(d, man)


def stamp_prompt_reroll(rec: dict, d: Path, why: str) -> dict | None:
    """After a ref the batch re-rolled for a changed prompt: manifest.ref.prompt_reroll, and
    when the override's ref_pick is now stale (stale_pin) point ref.png and manifest.ref's
    pick at the strip's own pick (forge's ref copied ref_<pin>; the mesh gets --pick).
    Returns the stale_pin record or None."""
    import shutil
    man = fg.load_manifest(d)
    ref = man.get("ref")
    if not isinstance(ref, dict):
        return None
    ref["prompt_reroll"] = {"reason": why, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    sp = stale_pin(rec, d, man)
    if sp:
        src = d / f"ref_{sp['auto']}.png"
        if src.exists():
            shutil.copyfile(src, d / "ref.png")
        chk = ref.get("unarmed")
        how = "unarmed_check" if isinstance(chk, dict) and isinstance(chk.get("best"), int) else "default"
        ref.update({"picked": sp["auto"], "picked_by": f"{how} (batch: stale ref_pick {sp['pin']} ignored)",
                    "stale_pin": {"pin": sp["pin"], "reason": sp["reason"]}})
        fg.log(f"batch: {rec['id']}: {sp['reason']}")
    fg.save_manifest(d, man)
    return sp


def prompt_stale(rec: dict, d: Path, man: dict, args=None) -> str | None:
    """Why the reference must be re-rolled for a changed prompt, or None. Stamped manifests:
    the prompt forge would build now differs from the stamp. With --reroll-changed-prompts,
    also an override file with a prompt field (PROMPT_FIELDS) newer than ref.png, or a
    data/prompt_tweaks.json entry for the id (or its plan) newer than ref.png."""
    ref = man.get("ref") if isinstance(man.get("ref"), dict) else {}
    pick = d / "ref.png"
    if not ref or not pick.exists():
        return None
    stamp = ref.get("prompt_fp")
    if stamp:
        try:
            pos, neg, tpl = current_ref_prompt(rec)
        except Exception:  # a bad override fails the ref stage loudly in forge
            return None
        n = ref.get("n")
        if prompt_fingerprint(pos, neg, tpl, n) != stamp:
            return f"prompt changed since the reference (fingerprint {stamp})"
    if getattr(args, "reroll_changed_prompts", False) and not stamp:
        # unstamped (older) references: a prompt source edited after ref.png AND a prompt that
        # differs from the one recorded (an override edited only for yaw or notes, or a
        # tweak that was already in use, does not re-roll)
        t_ref = pick.stat().st_mtime
        try:
            ov = overrides.load(rec["source"], rec["id"])
        except Exception:
            ov = {}
        ovp = overrides.path_for(rec["source"], rec["id"])
        fields = [k for k in PROMPT_FIELDS if k in ov]
        newer = []
        if fields and ovp.exists() and ovp.stat().st_mtime > t_ref + 1:
            newer.append(f"override {', '.join(fields)}")
        tw = ROOT / "data" / "prompt_tweaks.json"
        if tw.exists() and tw.stat().st_mtime > t_ref + 1:
            ent = fg.load_prompt_tweaks()
            if rec["id"] in ent or unit_rec(rec)["body_plan"] in (ent.get("_plans") or {}):
                newer.append("data/prompt_tweaks.json")
        if newer:
            try:
                pos, neg, _ = current_ref_prompt(rec)
            except Exception:
                return None
            if " ".join(pos.split()) != " ".join(str(ref.get("prompt", "")).split()) or \
                    " ".join(neg.split()) != " ".join(str(ref.get("negative", "")).split()):
                return f"{' and '.join(newer)} newer than ref.png and the prompt differs (--reroll-changed-prompts)"
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


def ref_n(rec: dict, args, force: bool = False) -> int:
    n = max(args.n, 4) if rec.get("unarmed_reference") else args.n
    if force:  # a re-roll keeps the monster's own candidate count (review re-rolls asked for 4)
        ref = fg.load_manifest(mon_dir(args.source, rec["id"])).get("ref")
        if isinstance(ref, dict) and isinstance(ref.get("n"), int):
            n = max(n, ref["n"])
    return n


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
    pins = []  # (id, why): ref_pick pins to choose again (stale_pin, pin_notes)
    for rec, cls in items:
        if cls["tier"] != cur:
            cur = cls["tier"]
            print(f"\n== {names[cur]}")
            print(f"{'#':>4}  {'id':<30} {'plan':<17} {'size':<10} {'n':>2}  {'pending':<38} {'est':>7}  note")
        s = status(rec, args.source, args)
        pend = [x for x in s["pending"] if want is None or x in want]
        pn = pin_notes(rec, s) if cls["tier"] else None
        if pn:
            pins.append((rec["id"], pn))
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
              f"{ref_n(rec, args, 'ref' in s['stale']) if 'ref' in pend else '':>2}  {','.join(pend) or 'done':<38} {hms(t):>7}  "
              f"{cls['status']}: {cls['note']}{sk}{mark}")
        if pend:
            counts["pending"] += 1
            per_tier[cls["tier"]] += t
            if runnable(cls, rec, args):
                total += t
                n_run += 1
        else:
            counts["done"] += 1
    if pins:
        print(f"\n== Pins to re-choose ({len(pins)})")
        for mid, why in pins:
            print(f"  {mid}: {why}")
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
        sp = stale_pin(rec, s["dir"], s["man"])
        if sp:  # cleared again once the review pins from the new strip
            m["stale_pin"] = {"pin": sp["pin"], "reason": sp["reason"]}
        else:
            m.pop("stale_pin", None)

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


def comfy_queue_len(timeout: float = 10) -> int | None:
    """Running + pending jobs on ComfyUI's /queue, None when it does not answer."""
    try:
        q = json.loads(fg.http("GET", "/queue", timeout=timeout))
        return len(q.get("queue_running") or []) + len(q.get("queue_pending") or [])
    except Exception:
        return None


def clear_comfy_queue() -> None:
    """Drop the pending jobs (POST /queue {"clear": true}) and stop the running one
    (POST /interrupt). /interrupt alone leaves a killed stage's queued jobs behind."""
    for path, body in (("/queue", json.dumps({"clear": True}).encode()), ("/interrupt", b"")):
        try:
            fg.http("POST", path, body, headers={"Content-Type": "application/json"}, timeout=10)
        except Exception as e:
            fg.log(f"batch: POST {path} failed ({str(e)[:120]})")


def recover_comfy(prog: Progress, why: str) -> bool:
    """After a stage was killed (timeout, hang) or a queue that will not drain: clear the
    queue, interrupt, restart the unit and wait for /system_stats, so the next stage starts
    on an empty ComfyUI."""
    fg.log(f"batch: clearing the ComfyUI queue + restart ({why})")
    clear_comfy_queue()
    prog.event("comfy_clear", why=why)
    return restart_comfy(prog, why)


def ensure_comfy(prog: Progress, label: str = "a stage") -> None:
    """Before a ComfyUI stage: /queue must answer within HANG_S, else restart; then the queue
    must be empty within QUEUE_BUSY_S (forge's stages wait_idle() for it forever), else
    clear it and restart."""
    t0 = time.time()
    while not comfy_ok():
        if time.time() - t0 >= HANG_S:
            restart_comfy(prog, f"/queue silent for {HANG_S:.0f}s before {label}")
            break
        time.sleep(5)
    t0 = time.time()
    while True:
        n = comfy_queue_len()
        if n == 0 or n is None:  # None: just restarted and still loading; the stage's watchdog takes it
            return
        if time.time() - t0 >= QUEUE_BUSY_S:
            fg.log(f"batch: ComfyUI queue busy ({n} jobs) for {QUEUE_BUSY_S:.0f}s before {label}")
            recover_comfy(prog, f"queue busy {QUEUE_BUSY_S:.0f}s before {label}")
            return
        time.sleep(5)


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
        cmd += ["--n", str(ref_n(rec, args, force))]
    if stage == "mesh":  # forge's mesh honours the override pin: a stale one is overruled here
        d = mon_dir(args.source, rec["id"])
        sp = stale_pin(rec, d, fg.load_manifest(d)) if d.is_dir() else None
        if sp:
            cmd += ["--pick", str(sp["auto"])]
    if force:
        cmd.append("--force")  # a stale stage (status()["stale"]): forge would skip it otherwise
    return cmd


def scrub_partial(stage: str, rec: dict, d: Path, t0: float, source: str) -> dict:
    """After a killed stage: remove the done-markers (DONE_MARKERS) it wrote in this attempt
    (mtime >= t0), since the stage never finished (manifest record, thumbs, the pick may be
    missing), then check that status() reads the stage as pending, so a resumed run redoes it.
    A ref's candidates stay: ref_partial() makes the ref pending without ref.png. anim has no
    single marker (clips are files + manifest); a killed anim is redone by `anim --force`."""
    removed = []
    for name in DONE_MARKERS.get(stage, []):
        p = d / name
        try:
            if p.exists() and p.stat().st_mtime >= t0 - 1:
                p.unlink()
                removed.append(name)
        except OSError as e:
            fg.log(f"batch: {rec['id']}: could not remove partial {name}: {e}")
    s = status(rec, source)
    pending = stage in s["pending"] or stage in s["skipped"]
    note = (f"removed {', '.join(removed)}; " if removed else "") + \
        ("stage reads pending" if pending else "STAGE STILL READS DONE (check the files by hand)")
    return {"removed": removed, "pending": pending, "note": note}


def run_stage(stage: str, rec: dict, d: Path, args, prog: Progress, logf, force: bool = False) -> dict:
    """One forge.py stage in its own process group with a timeout and the /queue watchdog.
    A hung ComfyUI kills the stage, restarts the unit and retries the stage once."""
    timeout = timeout_for(stage, rec, d, args)
    for attempt in (1, 2):
        if stage in COMFY_STAGES:
            ensure_comfy(prog, f"{rec['id']} {stage}")
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
        if outcome != "exit":
            # Killed: whatever forge queued (mesh queues a second job for the decimation retry)
            # must not outlive it, and what it half wrote must not read as done.
            how = f"/queue silent {HANG_S:.0f}s during" if outcome == "hung" else "timeout in"
            recover_comfy(prog, f"{how} {rec['id']} {stage}")
            check = scrub_partial(stage, rec, d, t0, args.source)
            fg.log(f"batch: {rec['id']}: {stage} killed ({outcome}); outputs: {check['note']}")
        if outcome == "hung":
            prog.event("comfy_hung", id=rec["id"], stage=stage, seconds=dt, attempt=attempt)
            if attempt == 1:
                fg.log(f"batch: {rec['id']}: {stage} retry after the ComfyUI restart")
                continue
            text = f"batch: ComfyUI stopped answering /queue during {stage} (twice); stage killed"
            add_manifest_error(d, stage, text)
            return {"status": "failed", "seconds": dt, "error": text, "outputs": check}
        if outcome == "timeout":
            text = f"batch: {stage} timed out after {timeout:.0f}s (killed)"
            add_manifest_error(d, stage, text)
            prog.event("stage_timeout", id=rec["id"], stage=stage, seconds=dt, outputs=check["note"])
            return {"status": "timeout", "seconds": dt, "error": text, "outputs": check}
        if proc.returncode != 0:
            return {"status": "failed", "seconds": dt,
                    "error": last_manifest_error(d, stage) or f"forge.py {stage} exited {proc.returncode}"}
        return {"status": "ok", "seconds": dt}
    return {"status": "failed", "seconds": 0, "error": "unreachable"}


def facing_gate(rec: dict, d: Path) -> dict:
    """rig.facing_check on model.glb (Blender only) before paint and the rolls: a misfaced
    mesh would be refused by the rig stage after ~3 min of paint, and fixing it (pin yaw,
    qa --force) deletes the paint again."""
    f = rig.facing_check(fg, unit_rec(rec), d)  # a swarm's rig runs on its unit
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
    # unverified: not refused, but the head end (or a disputed axis) is not settled; ref,
    # mesh and qa go on, paint waits (paint_blocked), the report lists it under "Facing to
    # verify" (docs/batch.md)
    m["stages"]["facing"] = facing_entry(f, time.time() - t0)
    if f["ok"] and f.get("unverified"):
        fg.log(f"batch: {rec['id']}: facing UNVERIFIED ({f['unverified']}); paint waits for a look at thumb.png")
    if not f["ok"]:
        m["errors"].append({"stage": "facing", "error": f"{f['angle_deg']} deg off +Y, confidence "
                            f"{f['confidence']}, suggested yaw {f.get('suggested_yaw')}",
                            "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        fg.log(f"batch: {rec['id']}: facing gate refused ({f['angle_deg']} deg); next monster")
    prog.refresh(rec, args.source)
    prog.save()
    return bool(f["ok"])


def facing_record(prog, mid: str) -> dict | None:
    """The gate's stages.facing as _batch.json has it now, for the dry run."""
    return ((prog.data.get("monsters") or {}).get(mid) or {}).get("stages", {}).get("facing")


def paint_blocked(rec: dict, d: Path, pending: list, args, fc: dict | None) -> bool:
    """An `unverified` gate result holds paint. The only way past it is the reviewer's
    `rig_facing_check` false, set after looking at thumb.png: needs_facing_gate is then
    false, as it is for --no-facing-gate. A pinned `yaw` is checked again after its qa."""
    return bool(fc) and fc.get("status") == "unverified" and needs_facing_gate(rec, d, pending, args)


def facing_entry(f: dict, seconds: float = 0.0, **extra) -> dict:
    """_batch.json stages.facing from a rig.facing_check result."""
    return {"status": "refused" if not f["ok"] else "unverified" if f.get("unverified") else "ok",
            "angle_deg": f.get("angle_deg"), "confidence": f.get("confidence"),
            "suggested_yaw": f.get("suggested_yaw"),
            **({"unverified": f["unverified"]} if f.get("unverified") else {}),
            **({"hint_yaw": f["hint_yaw"]} if f.get("hint_yaw") is not None else {}),
            **({"error": f["error"]} if f.get("error") else {}),
            "seconds": round(seconds, 1), "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **extra}


# ---------------------------------------------------------------- facing refresh
# The gate's verdict comes from rig/facing.json (blender_rig.py "facing") plus rig.facing_check's
# rules (FACING_MAX_DEG, confidences, disputes, unverified). Records written by an older gate
# (no head-end `cues` in facing.json, or facing.json older than tools/blender_rig.py or than
# model.glb) need Blender again; a facing.json newer than _batch.json's record (a refresh the
# live pass overwrote, or the rig stage's own check) only needs the verdict re-derived.
BLENDER_RIG = ROOT / "tools" / "blender_rig.py"


def _epoch(at: str | None) -> float:
    try:
        return time.mktime(time.strptime(at, "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0.0


def facing_json_state(d: Path) -> str:
    """'missing', 'old' (needs Blender: pre-cues gate code, or older than blender_rig.py /
    model.glb) or 'current'."""
    fj = d / "rig" / "facing.json"
    if not fj.exists():
        return "missing"
    try:
        rep = json.loads(fj.read_text())
    except (OSError, ValueError):
        return "old"
    t = fj.stat().st_mtime
    model = d / "model.glb"
    if ("cues" not in rep and "unverified" not in rep) or t < BLENDER_RIG.stat().st_mtime or \
            (model.exists() and model.stat().st_mtime > t + 1):
        return "old"
    return "current"


def derive_facing(rec: dict, d: Path) -> dict | None:
    """rig.facing_check's verdict from the existing rig/facing.json, without Blender (rig.blender
    swapped for a read of the report; fg.log silenced). None when there is no readable report."""
    if not (d / "rig" / "facing.json").exists():
        return None
    keep_b, keep_log = rig.blender, fg.log
    rig.blender = lambda fg_, job: json.loads(Path(job["report"]).read_text())
    fg.log = lambda *a, **k: None
    try:
        return rig.facing_check(fg, unit_rec(rec), d)
    except Exception:
        return None
    finally:
        rig.blender, fg.log = keep_b, keep_log


def effective_facing(rec: dict, d: Path, b: dict) -> dict | None:
    """The facing verdict to report: _batch.json's record, unless rig/facing.json is newer
    (then re-derived from it, marked from_json); None when model.glb is newer than both."""
    fc = (b.get("stages") or {}).get("facing") or None
    fj = d / "rig" / "facing.json"
    model = d / "model.glb"
    t_model = model.stat().st_mtime if model.exists() else 0.0
    t_fc = _epoch(fc.get("at")) if fc else 0.0
    t_fj = fj.stat().st_mtime if fj.exists() else 0.0
    if t_fj > t_fc + 2 and t_fj + 1 >= t_model:
        f = derive_facing(rec, d)
        if f is not None:
            e = facing_entry(f, from_json=True)
            e["at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t_fj))
            return e
    if fc and t_fc + 2 >= t_model:
        return fc
    return None


def merge_batch_facing(path: Path, mid: str, entry: dict) -> dict | None:
    """Read _batch.json, replace one monster's stages.facing (and its facing errors), write a
    temp file in the same dir and rename it over: never a half-written file. A live `run`
    rewrites the whole file from its own copy at its next save, so this entry can be lost
    while a pass runs: `facing --stale` re-derives it afterwards without Blender. Returns the
    previous entry."""
    data = json.loads(path.read_text()) if path.exists() else {"monsters": {}, "sessions": [], "events": []}
    m = data.setdefault("monsters", {}).setdefault(mid, {"stages": {}, "errors": [], "seconds": 0.0})
    before = (m.setdefault("stages", {})).get("facing")
    m["stages"]["facing"] = entry
    errs = [e for e in m.get("errors") or [] if e.get("stage") != "facing"]
    if entry["status"] == "refused":
        errs.append({"stage": "facing", "error": f"{entry['angle_deg']} deg off +Y, confidence {entry['confidence']}, "
                                                 f"suggested yaw {entry.get('suggested_yaw')}", "at": entry["at"]})
    m["errors"] = errs
    data.setdefault("events", []).append({"at": entry["at"], "kind": "facing_refresh", "id": mid,
                                          "before": (before or {}).get("status"), "after": entry["status"]})
    data["events"] = data["events"][-500:]
    tmp = path.with_name(f".{path.name}.facing.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    os.replace(tmp, path)
    return before


def gate_applies(rec: dict, d: Path) -> str | None:
    """Why the facing gate does not apply to this monster (as needs_facing_gate), or None."""
    u = unit_rec(rec)
    if u["body_plan"] in rig.FACING_ADVISORY:
        return f"advisory plan {u['body_plan']}"
    if (d / "rig" / "chosen.glb").exists():
        return "rig/chosen.glb kept (no roll)"
    try:
        if overrides.load(rec["source"], rec["id"]).get("rig_facing_check") is False:
            return "rig_facing_check false"
    except Exception:
        pass
    return None


def cmd_facing(args) -> int:
    """Re-run (or re-derive) the facing gate for monsters the live pass will not touch: ref,
    mesh and qa done with no stale reason, rig not done. Blender (CPU) one monster at a time,
    only with MemAvailable >= --min-free-gb (waits otherwise; never asks ComfyUI to /free)."""
    if not args.ids and not args.stale:
        raise SystemExit("facing: give --ids and/or --stale")
    reg = fg.load_registry(args.source)
    items = [(r, c) for r, c in work_list(reg, args) if c["tier"] != 0 or args.ids]
    bdir = Path(args.batch_dir) if args.batch_dir else fg.OUT_ROOT / args.source
    bpath = bdir / "_batch.json"
    args.reroll_changed_prompts = True  # judge "will the pass touch it" as the strictest pass would
    todo, rows = [], []
    bmons = (viewer.load_json(bpath) or {}).get("monsters") or {}
    for rec, cls in items:
        mid = rec["id"]
        d = mon_dir(args.source, mid)
        s = status(rec, args.source, args)
        b = bmons.get(mid, {})
        fc = (b.get("stages") or {}).get("facing")
        why = None
        if not all(s["done"][x] for x in ("ref", "mesh", "qa")) or s["stale"]:
            why = "ref/mesh/qa not all done or stale: the pass may still run it"
        elif s["done"]["rig"]:
            why = "rigged"
        else:
            why = gate_applies(rec, d)
        state = facing_json_state(d)
        if why is None:
            if args.force or state in ("missing", "old"):
                action = "blender"
            else:
                derived = derive_facing(rec, d)
                if derived is None:
                    action = "blender"
                else:
                    stale_rec = (not fc or fc.get("status") != facing_entry(derived)["status"] or
                                 (d / "rig" / "facing.json").stat().st_mtime > _epoch(fc.get("at")) + 2)
                    action = "sync" if stale_rec else "current"
            if args.stale and action == "current":
                continue
            if args.stale and not args.ids and not fc and state == "missing":
                continue  # --stale: only gates that ran before
            todo.append((rec, d, action, fc))
        elif args.ids:
            rows.append((mid, "skip", why))
    for mid, act, why in rows:
        print(f"{mid}: {act} ({why})")
    print(f"facing: {len(todo)} monsters: " + ", ".join(f"{r['id']} ({a})" for r, _, a, _ in todo))
    if args.dry_run:
        return 0
    results = []
    for rec, d, action, fc in todo:
        mid = rec["id"]
        t0 = time.time()
        if action == "blender":
            last = 0.0
            while paint.mem_available_gb() < args.min_free_gb:
                el = time.time() - t0
                if el - last >= 60 or last == 0:
                    fg.log(f"batch facing: {mid}: {paint.mem_available_gb():.1f} GB available < "
                           f"{args.min_free_gb} GB, waiting ({el:.0f}s)")
                    last = el or 1
                time.sleep(15)
            t1 = time.time()
            try:
                f = rig.facing_check(fg, unit_rec(rec), d)
            except Exception as e:
                fg.log(f"batch facing: {mid}: Blender failed ({e}); record left as it was")
                results.append((mid, (fc or {}).get("status"), f"error: {str(e)[:120]}"))
                continue
            entry = facing_entry(f, time.time() - t1, refreshed_by="batch.py facing")
        else:
            f = derive_facing(rec, d)
            if f is None:
                results.append((mid, (fc or {}).get("status"), "error: no readable rig/facing.json"))
                continue
            entry = facing_entry(f, refreshed_by="batch.py facing (from rig/facing.json)")
        merge_batch_facing(bpath, mid, entry)
        results.append((mid, (fc or {}).get("status"), entry["status"]))
        fg.log(f"batch facing: {mid}: {(fc or {}).get('status')} -> {entry['status']} "
               f"({entry['angle_deg']} deg, confidence {entry['confidence']}"
               + (f", {entry['unverified']}" if entry.get("unverified") else "")
               + (f", suggested yaw {entry.get('suggested_yaw')}" if entry["status"] == "refused" else "") + ")")
    print("\n| id | before | after |\n|---|---|---|")
    for mid, before, after in results:
        print(f"| {mid} | {before} | {after} |")
    return 0


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
        s = status(rec, args.source, args)
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
                if st == "paint" and paint_blocked(rec, d, s["pending"], args, facing_record(prog, mid)):
                    print("    paint blocked: facing unverified; look at thumb.png, then pin `yaw` or set rig_facing_check false")
                    break
                if st in restart_stages(args):
                    print("    systemctl --user restart comfyui; wait for /system_stats")
                if st in ("mesh", "paint"):
                    print(f"    memory gate: wait for >= {args.min_free_gb} GB available")
                print(f"    {' '.join(forge_cmd(st, rec, args, st in s['stale'])[2:])}   "
                      f"(timeout {timeout_for(st, rec, d, args):.0f}s)" + (f"  [stale: {s['stale'][st]}]" if st in s["stale"] else ""))
                if st == "ref" and s.get("ref_prompt") and _ov_pin(rec)[0] is not None:
                    print(f"    stale pin: ref_pick {_ov_pin(rec)[0]} predates this re-roll; mesh --pick <the strip's own pick>")
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
                s = status(rec, args.source, args)
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
                if st == "paint" and paint_blocked(rec, d, s["pending"], args, m["stages"].get("facing")):
                    # not a failure: nothing broke, and a pass can run on past any number of these
                    fg.log(f"batch: {mid}: paint blocked, facing unverified "
                           f"({m['stages']['facing'].get('unverified')}); look at thumb.png")
                    prog.event("paint_blocked", id=mid, why=m["stages"]["facing"].get("unverified"))
                    prog.save()
                    break
                if st in restart_stages(args):
                    restart_comfy(prog, f"before {mid} {st}")
                if st in ("mesh", "paint"):
                    memory_gate(prog, args.min_free_gb, f"{mid} {st}")
                ran.add(st)
                res = run_stage(st, rec, d, args, prog, logf, force=st in s["stale"])
                res["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                m["stages"][st] = res
                if st == "ref" and res["status"] == "ok":
                    stamp_prompt_fp(d)  # the prompt this reference was drawn with (prompt_stale)
                    if s.get("ref_prompt"):  # re-rolled for a changed prompt: a pin from the old strip is stale
                        sp = stamp_prompt_reroll(rec, d, s["stale"].get("ref", "prompt changed"))
                        if sp:
                            prog.event("stale_pin", id=mid, pin=sp["pin"], auto=sp["auto"])
                m["seconds"] = round(m.get("seconds", 0) + res["seconds"], 1)
                sess["stages_run"] += 1
                if res["status"] != "ok":
                    m["errors"].append({"stage": st, "error": res.get("error"), "at": res["at"]})
                    if res.get("outputs") and not res["outputs"]["pending"]:
                        m["errors"].append({"stage": st, "error": f"batch: killed {st} still reads done: "
                                            f"{res['outputs']['note']}", "at": res["at"]})
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
                if st == "qa" and not gate_passed and needs_facing_gate(rec, d, status(rec, args.source, args)["pending"], args):
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
def ready_for_paint(rows: list) -> tuple[list, list]:
    """(rows ready for the paint pass, [(id, why not)] for the rest with qa done and paint
    pending). Ready: ref, mesh, qa done; no stale stage; facing ok or not applicable; not
    on hold (tier 4) or a pilot."""
    ready, not_ready = [], []
    for r in rows:
        s, rec = r["s"], r["rec"]
        if r["cls"]["tier"] in (0,) or "paint" not in s["pending"] or not s["done"]["qa"]:
            continue
        why = []
        if r["cls"]["tier"] == 4:
            why.append("hold")
        if not all(s["done"][x] for x in ("ref", "mesh")):
            why.append("ref/mesh pending")
        if s["stale"]:
            why.append("stale " + ",".join(s["stale"]))
        if r.get("pin"):
            why.append("ref_pick to re-choose")
        if gate_applies(rec, s["dir"]) is None:
            st = (r["fc"] or {}).get("status")
            if st != "ok":
                why.append(f"facing {st or 'not checked'}")
        (not_ready.append((rec["id"], ", ".join(why))) if why else ready.append(r))
    return ready, not_ready


def cmd_report(args) -> int:
    reg = fg.load_registry(args.source)
    pilot_keys = {(reg[p]["body_plan"], reg[p]["rig_template"]): p for p in PILOTS if p in reg}
    bdir = Path(args.batch_dir) if args.batch_dir else fg.OUT_ROOT / args.source
    bj = viewer.load_json(bdir / "_batch.json") or {"monsters": {}, "sessions": [], "events": []}
    rows = []
    for rec in reg.values():
        s = status(rec, args.source, args)
        rm = s["man"].get("rig") if isinstance(s["man"].get("rig"), dict) else {}
        q = viewer.rig_quality(rm) or {}
        found = set(s["st"]["found_clips"])
        b = (bj.get("monsters") or {}).get(rec["id"], {})
        fcv = effective_facing(rec, s["dir"], b) if b else None
        errors = s["man"].get("errors") or []
        if fcv and fcv.get("status") != "refused":  # a gate refusal a later check superseded
            errors = [e for e in errors if not str(e.get("error", "")).startswith("batch facing gate:")]
            if rm.get("error") == "facing check failed" and set(rm) <= {"error", "facing"}:
                rm, q = {}, {}
        rows.append({"rec": rec, "cls": classify(rec, pilot_keys), "s": s, "q": q, "rig_m": rm, "fc": fcv,
                     "missing": [c for c in rec.get("animation_set") or [] if c not in found] if found else None,
                     "errors": errors, "b": b, "pin": pin_notes(rec, s) if rec["id"] not in PILOTS else None,
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
    to_verify = [r for r in rows if (r["fc"] or {}).get("status") == "unverified" and
                 gate_applies(r["rec"], r["s"]["dir"]) is None]
    n_unver = len(to_verify)
    n_refused = sum(1 for r in rows if (r["fc"] or {}).get("status") == "refused")
    pins = [r for r in rows if r["pin"]]
    ready, not_ready = ready_for_paint(rows)
    L += ["## Totals", "",
          f"- {n} records, {done_all} with every stage done, {complete} with every animation_set clip "
          f"(viewer anim_complete).",
          f"- Batch sessions: {len(sessions)}, wall time {hms(wall)}"
          + (f", stage time {hms(sum(stage_s.values()))} (" + ", ".join(f"{k} {hms(v)}" for k, v in
                                                                         sorted(stage_s.items())) + ")"
             if stage_s else "") + ".",
          f"- ComfyUI restarts: {sum(1 for e in bj.get('events') or [] if e.get('kind') == 'comfy_restart')}, "
          f"hangs: {sum(1 for e in bj.get('events') or [] if e.get('kind') == 'comfy_hung')}, memory waits: "
          f"{sum(1 for e in bj.get('events') or [] if e.get('kind') == 'memory_wait')}.",
          f"- Facing gate: {n_refused} refused, {n_unver} unverified (Facing to verify); pins to re-choose: "
          f"{len(pins)}; ready for paint: {len(ready)}.", ""]
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
            refused = (r["fc"] or {}).get("status") == "refused"
            if (e.get("stage") == "facing" and refused) or "still reads" in str(e.get("error", "")):
                by_stage[e["stage"]].append((r["rec"]["id"], e.get("error", ""), e.get("at", "")))
    if not by_stage:
        L.append("None.")
    for st in sorted(by_stage, key=lambda x: STAGES.index(x) if x in STAGES else 99):
        L += [f"### {st} ({len(by_stage[st])})", ""]
        for mid, err, at in by_stage[st]:
            L.append(f"- `{mid}` {at}: {' '.join(str(err).split())[:400]}")
        L.append("")
    # facing gate results that are not refusals but need a look at thumb.png
    L += [f"## Facing to verify ({n_unver})", "",
          "The facing gate could not settle these (head end not named, mirror axis disputed or low confidence): the "
          "batch will not paint them. Look at thumb.png, then pin `yaw` (QA's yaw + 180 for a backwards body) and let "
          "qa and the gate run again, or, when it faces front, set `rig_facing_check` false.", ""]
    L += [f"- `{r['rec']['id']}` {r['fc'].get('angle_deg')} deg, confidence {r['fc'].get('confidence')}: "
          f"{r['fc'].get('unverified')}" + (f" (one cue's yaw {r['fc']['hint_yaw']})"
                                            if r['fc'].get("hint_yaw") is not None else "")
          + f"; `{(r['s']['dir'] / 'thumb.png').relative_to(ROOT)}`" for r in to_verify] or ["None."]
    L.append("")
    # ref_pick pins chosen from a strip a prompt re-roll replaced (stale_pin, pin_notes)
    L += [f"## Pins to re-choose ({len(pins)})", "",
          "An override `ref_pick` chosen before the reference was re-rolled for a changed prompt names a candidate of "
          "the old strip: the batch meshes the new strip's own pick instead (`stale pin`) until a pin is written "
          "after the re-roll. Look at the strip and pin again (or drop the pin).", ""]
    L += [f"- `{r['rec']['id']}`: {r['pin']}" for r in pins] or ["None."]
    L.append("")
    # ref, mesh, qa done, nothing stale, facing settled, not on hold, paint pending
    L += [f"## Ready for paint ({len(ready)})", "",
          "ref, mesh and qa done; no stale stage; facing gate ok (or not applicable: `rig_facing_check` false, an "
          "advisory plan, a kept rig/chosen.glb); not on hold; paint pending. Review verdicts (docs/review-*.md) are "
          "not read here.", ""]
    L += [", ".join(f"`{r['rec']['id']}`" for r in ready) or "None.", ""]
    if not_ready:
        L += ["Not ready (qa done, paint pending): " + "; ".join(f"`{mid}` {why}" for mid, why in not_ready), ""]
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
        fc = r["fc"] or {}
        facing = "" if not fc else f"{fc.get('status')} {fc.get('angle_deg')} deg" + \
            (" (from rig/facing.json)" if fc.get("from_json") else "")
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
    print(f"facing refused {n_refused}, unverified {n_unver}; pins to re-choose {len(pins)}; ready for paint {len(ready)}")
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
        p.add_argument("--reroll-changed-prompts", action="store_true",
                       help="also re-roll refs whose override prompt fields (prompt_extra, negative_extra, "
                            "ref_template, swarm_unit, style) or prompt_tweaks.json entry are newer than ref.png")
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
    fa = sub.add_parser("facing", help="re-run the facing gate (Blender, CPU) where its record is stale")
    fa.add_argument("--source", default="srd-5.2.1")
    fa.add_argument("--ids", help="comma list of monster ids")
    fa.add_argument("--tier", help="with --stale: tiers to scan (default all but pilots)")
    fa.add_argument("--plans", help="with --stale: body plans to scan")
    fa.add_argument("--stale", action="store_true",
                    help="only monsters whose recorded facing predates the gate code (no cues in rig/facing.json, "
                         "or it is older than tools/blender_rig.py / model.glb) or whose _batch.json record is older "
                         "than rig/facing.json")
    fa.add_argument("--force", action="store_true", help="run Blender even when rig/facing.json is current")
    fa.add_argument("--dry-run", action="store_true", help="list what it would do")
    fa.add_argument("--batch-dir")
    fa.add_argument("--min-free-gb", type=float, default=MIN_FREE_GB, help="wait for this much MemAvailable")
    au = sub.add_parser("audit", help="coverage tables and the gap list (markdown)")
    au.add_argument("--source", default="srd-5.2.1")
    args = ap.parse_args(argv)
    if getattr(args, "stages", None):
        bad = [x for x in args.stages.split(",") if x not in STAGES]
        if bad:
            ap.error(f"--stages: unknown {bad}; choose from {STAGES}")
    return {"plan": cmd_plan, "run": cmd_run, "report": cmd_report, "audit": cmd_audit,
            "facing": cmd_facing}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
