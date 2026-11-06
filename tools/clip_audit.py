"""Read-only quality audit of the procedural clips (anims/) on every rigged monster.

    python3 -I tools/clip_audit.py --all --jobs 8          # metrics (Blender, this machine) + report
    python3 -I tools/clip_audit.py wolf owlbear --force    # just these, re-measured
    python3 -I tools/clip_audit.py --report                # flags + clips.md from the metrics, no Blender
    python3 -I tools/clip_audit.py --sheets 12             # review sheets: the 12 worst non-bipeds
    python3 -I tools/clip_audit.py --sheets wolf,owlbear   # ... or these

--root is the directory holding <id>/ (default out/srd-5.2.1; a copy works: only model_rigged.glb,
manifest.json and anims/ are read), --out where the results go (default <root>/_audit: <id>.json
metrics, flags.json, clips.md, sheets/<id>.png). Nothing under <id>/ is written.

Per clip, tools/blender_clip_audit.py plays the clip's action on model_rigged.glb and evaluates the
skinned mesh on every frame; this driver turns the numbers into flags (FLAGS below), so thresholds can
be tuned with --report alone. Clips promoted from UAL (manifest anim.source) and UAL-skeleton rigs
(rig.tool "ual") are skipped: the audit is for the procedural clips on SkinTokens/fallback rigs.
docs/rig-anim.md "Clip audit".
"""
import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "srd-5.2.1"
BLENDER = os.environ.get("BLENDER", "/usr/bin/blender")
SCRIPT = ROOT / "tools" / "blender_clip_audit.py"
REGISTRY = ROOT / "data" / "registry" / f"{SOURCE}.json"
UPRIGHT = {"biped", "winged_biped"}
LIFTED_PLANS = {"floating", "aquatic"}  # every clip but death held off the ground (ground "lift")
MOVES = {"fly", "swim"}  # pose changes away from the idle, lifted by design
ONESHOT_FLOOR = {"attack_melee", "attack_ranged", "attack_special", "hit", "cast", "breath"}

# flag: (threshold, what it measures). Severity = value / threshold, capped at 5; the sort key is the
# sum over a monster's flags. Calibrated on the sheets of the worst non-bipeds (docs/rig-anim.md).
T = {
    "sink": 0.03,          # lowest vertex below the ground, x height (not burrow; the anim stage allows DIP_MAX,
                           # 0.03 m on a 1-6.5 m body: 0.02 flagged hyena feet nobody can see)
    "float": 0.03,         # floor clip: the lowest vertex above the ground on some frame, x height
    "feet_up": 0.03,       # walk: every foot's lowest vertex off the ground at once, x height
    "pop": 0.03,           # a vertex's one-frame jump or out-and-back flicker beyond its neighbour frames' moves, x height
    "loop": 0.005,         # loop: last frame vs first, x height
    "from_idle": 0.12,     # one-shot first frame vs the idle's first, x height
    "from_idle_walk": 0.30,  # walk / move clips: (the game crossfades)
    "stretch_loop": 4.5,   # edge stretch p99.9 (x rest length), loops (+1.5 where wings tear most: membranes)
    "stretch_once": 6.0,   # ... one-shots
    "stride": 0.12,        # walk: a foot's fore-aft travel / leg length, under this = the foot stays put
    "gait_cv": 0.5,        # walk: spread of the stance feet's backward speeds (std / mean): feet slide
    "socket": 0.03,        # socket drifts off the mesh around it, x height
    "wing": 0.15,          # fly: wing tip travel / wing length, under this = wings not beating
    "tail": 0.02,          # walk/idle: tail tip travel / tail length, under this = tail frozen
    "death_floor": 0.03,   # death last frame: lowest vertex off the ground, x height
    "death_top": 0.65,     # death last frame: top of an upright body, x standing height
    "death_hold": 0.005,   # death: still moving over its last 3 frames, x size (0.003 caught the pudding settling)
    "idle_still": 0.003,   # idle: largest vertex travel, x height, under this = frozen
    "idle_wander": 0.15,   # idle: over this = more than an idle
    "attack_weak": 0.04,   # attack: largest vertex travel x height under this = no visible strike
    "limb_in_torso": 0.25,  # share of a limb's distal vertices inside the torso ellipsoid beyond rest
}


def registry() -> dict:
    return {m["id"]: m for m in json.loads(REGISTRY.read_text())["monsters"]}


def targets(root: Path, ids: list[str] | None) -> list[dict]:
    reg = registry()
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if d.name not in reg or (ids and d.name not in ids):
            continue
        if not (d / "model_rigged.glb").exists() or not (d / "manifest.json").exists():
            continue
        man = json.loads((d / "manifest.json").read_text())
        anim = man.get("anim") or {}
        rig = man.get("rig") or {}
        if not anim.get("clips") or rig.get("tool") == "ual":
            continue
        src = anim.get("source") or {}
        clips = [c for c in anim["clips"] if src.get(c) != "ual" and (d / "anims" / f"{c}.glb").exists()]
        out.append({"id": d.name, "dir": d, "plan": man.get("body_plan"), "parts": anim.get("parts") or {},
                    "clips": clips, "skipped_ual": [c for c in anim["clips"] if src.get(c) == "ual"],
                    "rig_tool": rig.get("tool"), "rec": reg[d.name]})
    return out


def measure(t: dict, out: Path, tiles: Path | None = None, force: bool = False) -> str:
    dst = out / (f"{t['id']}.json" if tiles is None else f"_tiles_{t['id']}.json")
    if dst.exists() and not force and tiles is None:
        return f"{t['id']}: exists"
    job = {"id": t["id"], "src": str(t["dir"] / "model_rigged.glb"), "anims": str(t["dir"] / "anims"),
           "clips": t["clips"], "parts": t["parts"], "out": str(dst),
           "tiles": str(tiles) if tiles else None, "tile_px": 180, "tile_frames": 5}
    t0 = time.time()
    p = subprocess.run([BLENDER, "-b", "--factory-startup", "--python-exit-code", "1", "-P", str(SCRIPT), "--",
                        json.dumps(job)], capture_output=True, text=True, timeout=3600)
    if p.returncode != 0 or not dst.exists():
        (out / f"{t['id']}.err").write_text(p.stdout[-6000:] + p.stderr[-6000:])
        return f"{t['id']}: FAILED rc={p.returncode} (see {t['id']}.err)"
    return f"{t['id']}: {time.time() - t0:.0f}s"


def flags_for(t: dict, m: dict) -> list[dict]:
    """[{clip, flag, value, sev}] from one monster's metrics."""
    plan, out = t["plan"], []
    rec = t["rec"]
    # anim.idle_lift: a monster that only swims or flies idles (and holds its one-shots) at that
    # clip's height, unless the override idle_lift says otherwise
    want = rec.get("animation_set") or []
    move = next((c for c in ("walk", "swim", "fly") if c in want), None)
    lift = None
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import overrides
        lift = overrides.load(rec["source"], rec["id"]).get("idle_lift")
    except Exception:
        pass
    lifted_idle = bool(lift) if lift is not None else move in MOVES
    # displacements are against the larger of height and length: a 4 m snake 0.5 m tall
    ks = m["height"] / max(m.get("size") or m["height"], 1e-6)

    def add(clip, flag, value, thr, invert=False):
        sev = (thr / max(value, 1e-6)) if invert else (value / thr)
        out.append({"clip": clip, "flag": flag, "value": round(value, 4), "sev": round(min(5.0, sev), 2)})

    for c, r in m["clips"].items():
        if "error" in r:
            out.append({"clip": c, "flag": "error", "value": 0, "sev": 5.0, "note": r["error"]})
            continue
        # the anim stage's own report says whether the clip is held off the ground
        try:
            prep = json.loads((t["dir"] / "anims" / f"{c}.json").read_text())
        except (OSError, ValueError):
            prep = {}
        lifted = c in MOVES or "lifted_m" in prep or "idle_lift_m" in prep or (plan in LIFTED_PLANS and c != "death") \
            or (lifted_idle and c not in ("death", "burrow"))
        if c != "burrow" and r["min_z_frac"] < -T["sink"]:
            add(c, "sink", -r["min_z_frac"], T["sink"])
        if not lifted and c not in ("death", "burrow") and r["low_max_frac"] > T["float"]:
            add(c, "float", r["low_max_frac"], T["float"])
        if c == "walk" and r.get("feet_all_up_max_frac", 0) > T["feet_up"]:
            add(c, "feet_up", r["feet_all_up_max_frac"], T["feet_up"])
        if r.get("pop_frac", 0) * ks > T["pop"]:
            add(c, "pop", r["pop_frac"] * ks, T["pop"])
            out[-1]["where"] = r.get("pop_where")
        if r["loop"] and r.get("loop_delta_frac", 0) > T["loop"]:
            add(c, "loop", r["loop_delta_frac"], T["loop"])
        if "from_idle_frac" in r and c not in MOVES:  # fly / swim are pose changes (docs/rig-anim.md round 8)
            thr = T["from_idle_walk"] if (r["loop"] or c in MOVES) else T["from_idle"]
            if r["from_idle_frac"] * ks > thr:
                add(c, "from_idle", r["from_idle_frac"] * ks, thr)
        thr = T["stretch_loop"] if r["loop"] else T["stretch_once"]
        where = r.get("stretch_where") or {}
        if where and max(where, key=where.get).startswith("wing"):
            thr += 1.5
        if r["edge_stretch_p999"] > thr:
            add(c, "stretch", r["edge_stretch_p999"], thr)
        if c == "walk" and r.get("feet") and plan != "insectoid":  # sprawled tips sweep about Z, not fore-aft
            strides = [f["stride_frac"] for f in r["feet"]]
            if min(strides) < T["stride"]:
                add(c, "stride", min(strides), T["stride"], invert=True)
            sp = [f["stance_speed"] for f in r["feet"]]
            mean = statistics.mean(sp)
            cv = (statistics.pstdev(sp) / mean) if mean > 1e-3 else 9.0
            if cv > T["gait_cv"]:
                add(c, "gait", min(cv, 9.0), T["gait_cv"])
        for s, v in (r.get("socket_drift_frac") or {}).items():
            if v * ks > T["socket"]:
                add(c, f"socket_{s}", v * ks, T["socket"])
        if c == "fly" and r.get("wing_tip_travel_frac"):
            w = min(r["wing_tip_travel_frac"].values())
            if w < T["wing"]:
                add(c, "wing", w, T["wing"], invert=True)
        if c in ("walk",) and "tail_tip_travel_frac" in r and r["tail_tip_travel_frac"] < T["tail"]:
            add(c, "tail", r["tail_tip_travel_frac"], T["tail"], invert=True)
        if c == "death":
            if abs(r["low_last_frac"]) > T["death_floor"]:
                add(c, "death_floor", abs(r["low_last_frac"]), T["death_floor"])
            if plan in UPRIGHT and r["top_last_frac"] > T["death_top"]:
                add(c, "death_top", r["top_last_frac"], T["death_top"])
            if r["hold_step_frac"] * ks > T["death_hold"]:
                add(c, "death_hold", r["hold_step_frac"] * ks, T["death_hold"])
        if c == "idle":
            if r["motion_frac"] * ks < T["idle_still"]:
                add(c, "idle_still", r["motion_frac"] * ks, T["idle_still"], invert=True)
            elif r["motion_frac"] * ks > T["idle_wander"] and not lifted:
                add(c, "idle_wander", r["motion_frac"] * ks, T["idle_wander"])
        if c.startswith("attack") and r["motion_frac"] * ks < T["attack_weak"]:
            add(c, "attack_weak", r["motion_frac"] * ks, T["attack_weak"], invert=True)
        for k, v in (r.get("limb_in_torso") or {}).items() if c != "death" else ():  # a lying body tucks its legs
            if v > T["limb_in_torso"]:
                add(c, f"limb_in_torso:{k}", v, T["limb_in_torso"])
    return out


def implied_speed(r: dict) -> float | None:
    sp = [f["stance_speed"] for f in r.get("feet") or [] if f["stance_speed"] > 0]
    return round(statistics.median(sp), 3) if sp else None


def report(root: Path, out: Path, ts: list[dict]) -> list[dict]:
    rows = []
    for t in ts:
        p = out / f"{t['id']}.json"
        if not p.exists():
            continue
        m = json.loads(p.read_text())
        fl = flags_for(t, m)
        w = m["clips"].get("walk") or {}
        spd = implied_speed(w) if "error" not in w else None
        rec_walk = (t["rec"].get("speeds") or {}).get("walk")
        rows.append({"id": t["id"], "plan": t["plan"], "rig": t["rig_tool"], "height": m["height"],
                     "score": round(sum(f["sev"] for f in fl), 2), "flags": fl,
                     "stretch": {c: r.get("edge_stretch_p999") for c, r in m["clips"].items()},
                     "walk_speed_mps": spd, "srd_walk_mps": round(rec_walk * 0.3048 / 6, 2) if rec_walk else None,
                     "skipped_ual": t["skipped_ual"],
                     # rig diagnostics: tail vertices near the floor at rest (> 0.05 on a hanging tail: it
                     # took a hind leg or the rump), a tail chain also found as a wing/leg, legs under its root
                     "tail_low_share": m.get("tail_low_share"), "tail_shared": m.get("tail_shared") or [],
                     "legs_under_tail": m.get("legs_under_tail") or []})
    rows.sort(key=lambda r: -r["score"])
    (out / "flags.json").write_text(json.dumps({"thresholds": T, "monsters": rows}, indent=1))
    write_md(out, rows)
    return rows


def fmt_flags(fl: list[dict], n: int = 6) -> str:
    fl = sorted(fl, key=lambda f: -f["sev"])
    s = ", ".join(f"{f['clip']}:{f['flag']} {f['value']:g}" + (f" ({f['where']})" if f.get("where") else "")
                  for f in fl[:n])
    return s + (f" (+{len(fl) - n})" if len(fl) > n else "")


def write_md(out: Path, rows: list[dict]) -> None:
    plans = {}
    for r in rows:
        plans.setdefault(r["plan"], []).append(r)
    L = ["# Procedural clip audit", "",
         f"{len(rows)} rigged monsters with procedural clips, {time.strftime('%Y-%m-%d %H:%M')}. "
         "Generated by `tools/clip_audit.py` (thresholds in its `T`; per-monster metrics `<id>.json`, flags "
         "`flags.json`). Severity = value / threshold (capped at 5), score = sum per monster. "
         "Stretch = edge stretch p99.9 (x rest length), the median over the plan's monsters per clip. "
         "walk m/s = the stance feet's backward speed (the ground speed the clip implies), SRD = the record's "
         "walk speed. tail@floor = share of the tail's vertices within 10% of the height of the floor at rest.", "",
         "## Per body plan", "",
         "| plan | n | idle | walk | attack | hit | death | move (fly/swim) | flagged | top flags |",
         "|---|---|---|---|---|---|---|---|---|---|"]

    def med(rs, clips):
        v = [r["stretch"][c] for r in rs for c in clips if r["stretch"].get(c)]
        return f"{statistics.median(v):.2f}" if v else "-"

    for plan, rs in sorted(plans.items(), key=lambda kv: -len(kv[1])):
        cnt = {}
        for r in rs:
            for f in {(f["clip"].split("_")[0] if f["clip"].startswith("attack") else f["clip"]) + ":" +
                      f["flag"].split(":")[0] for f in r["flags"]}:
                cnt[f] = cnt.get(f, 0) + 1
        top = ", ".join(f"{k} {v}" for k, v in sorted(cnt.items(), key=lambda kv: -kv[1])[:5])
        L.append(f"| {plan} | {len(rs)} | {med(rs, ['idle'])} | {med(rs, ['walk'])} | "
                 f"{med(rs, ['attack_melee', 'attack_ranged'])} | {med(rs, ['hit'])} | {med(rs, ['death'])} | "
                 f"{med(rs, ['fly', 'swim'])} | {sum(1 for r in rs if r['flags'])} | {top} |")
    L += ["", "## Flag counts (monsters affected, non-biped / biped)", "",
          "| flag | non-biped | biped | median severity |", "|---|---|---|---|"]
    fc = {}
    for r in rows:
        for k in {f["clip"] + ":" + f["flag"].split(":")[0] for f in r["flags"]}:
            e = fc.setdefault(k, [0, 0, []])
            e[0 if r["plan"] not in UPRIGHT else 1] += 1
        for f in r["flags"]:
            fc[f["clip"] + ":" + f["flag"].split(":")[0]][2].append(f["sev"])
    for k, (a, b, s) in sorted(fc.items(), key=lambda kv: -(kv[1][0] * 1000 + kv[1][1])):
        L.append(f"| {k} | {a} | {b} | {statistics.median(s):.2f} |")
    L += ["", "## Monsters, worst first", "",
          "| # | monster | plan | score | stretch idle/walk/attack/death | walk m/s (SRD) | tail@floor | flags |",
          "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        s = r["stretch"]
        st = "/".join(f"{s[c]:.1f}" if s.get(c) else "-" for c in ("idle", "walk", "attack_melee", "death"))
        sp = f"{r['walk_speed_mps']} ({r['srd_walk_mps']})" if r["walk_speed_mps"] is not None else "-"
        tl = "-" if r["tail_low_share"] is None else f"{r['tail_low_share']:.2f}" + \
            (" shared" if r["tail_shared"] else "") + (" legs-under" if r["legs_under_tail"] else "")
        L.append(f"| {i} | {r['id']} | {r['plan']} | {r['score']} | {st} | {sp} | {tl} | {fmt_flags(r['flags'])} |")
    (out / "clips.md").write_text("\n".join(L) + "\n")


def sheet(t: dict, out: Path, row: dict | None) -> Path:
    from PIL import Image, ImageDraw
    tiles = out / "tiles" / t["id"]
    shutil.rmtree(tiles, ignore_errors=True)
    measure(t, out, tiles=tiles, force=True)
    clips = [c for c in t["clips"] if (tiles / f"{c}_side_0.png").exists()]
    m = json.loads((out / f"_tiles_{t['id']}.json").read_text())
    flagged = {f["clip"] for f in (row or {}).get("flags", []) if f["flag"] == "pop"}
    pops = {c: m["clips"][c]["pop_frame"] for c in clips if c in flagged and (tiles / f"{c}_pop_0.png").exists()}
    tw = 180
    cols = 5 + 2
    lab = 16
    img = Image.new("RGB", (cols * tw + 120, (len(clips) + len(pops)) * (tw + lab) + 30), "white")
    d = ImageDraw.Draw(img)
    d.text((6, 6), f"{t['id']} ({t['plan']}) score {row['score'] if row else '?'}", fill="black")
    y = 30 - (tw + lab)
    for c in clips:
        y += tw + lab
        fl = [f for f in (row or {}).get("flags", []) if f["clip"] == c]
        d.text((6, y), c + ": " + ", ".join(f"{f['flag']} {f['value']:g}" for f in fl)[:150],
               fill="red" if fl else "black")
        ims = [tiles / f"{c}_side_{k}.png" for k in range(5)] + sorted(tiles.glob(f"{c}_front_*.png"))[:2]
        for k, p in enumerate(ims):
            if p.exists():
                img.paste(Image.open(p).convert("RGB"), (120 + k * tw, y + lab))
        if c in pops:  # 4 consecutive frames around the pop
            y += tw + lab
            d.text((6, y), f"{c}: pop frames {pops[c] - 1}..{pops[c] + 2} ({m['clips'][c].get('pop_where')})", fill="red")
            for k in range(4):
                p = tiles / f"{c}_pop_{k}.png"
                if p.exists():
                    img.paste(Image.open(p).convert("RGB"), (120 + k * tw, y + lab))
    dst = out / "sheets" / f"{t['id']}.png"
    dst.parent.mkdir(exist_ok=True)
    img.save(dst)
    return dst


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--root", default=str(ROOT / "out" / SOURCE))
    ap.add_argument("--out")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--report", action="store_true", help="flags and clips.md only, no Blender")
    ap.add_argument("--sheets", help="N (worst N non-bipeds) or comma-separated ids")
    a = ap.parse_args()
    root = Path(a.root)
    out = Path(a.out) if a.out else root / "_audit"
    out.mkdir(parents=True, exist_ok=True)
    ts = targets(root, a.ids or None)
    if not ts:
        raise SystemExit("no rigged monsters with procedural clips found")
    if a.sheets:
        rows = {r["id"]: r for r in report(root, out, ts)}
        if a.sheets.isdigit():
            pick = [r for r in rows.values() if r["plan"] not in UPRIGHT]
            pick = [r["id"] for r in sorted(pick, key=lambda r: -r["score"])[:int(a.sheets)]]
        else:
            pick = a.sheets.split(",")
        by = {t["id"]: t for t in ts}
        with ThreadPoolExecutor(a.jobs) as ex:
            for p in ex.map(lambda i: sheet(by[i], out, rows.get(i)), [i for i in pick if i in by]):
                print(p, flush=True)
        return
    if not a.report:
        with ThreadPoolExecutor(a.jobs) as ex:
            for msg in ex.map(lambda t: measure(t, out, force=a.force), ts):
                print(msg, flush=True)
    rows = report(root, out, targets(root, None))
    print(f"{len(rows)} monsters -> {out / 'clips.md'}")


if __name__ == "__main__":
    sys.exit(main())
