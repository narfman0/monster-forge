"""Quadruped mocap clips: CC0 Mesh2Motion fox / horse / dragon clips retargeted onto the standard quadruped
skeleton (docs/rig-anim.md "Quadruped mocap clips").

    python3 -I tools/anim_quad.py sample [--packs DIR]          # source .blend -> data/quadmocap/<rig>/<clip>.json
    python3 -I tools/anim_quad.py run wolf rat ... [--jobs 4]    # = forge.py anim --force for canonical quadrupeds
    python3 -I tools/anim_quad.py plan wolf                      # which source rig and clips a monster gets

`sample` needs the Mesh2Motion asset repo's .blend files (rigs/<rig>/animation-<rig>-<clip>.blend and
rigs/rig-<rig>.blend; licenses/mesh2motion-*); everything after it reads only data/quadmocap/. The anim stage
(tools/anim.py) passes `mocap` to blender_clips.py for every canonical quadruped / winged quadruped (canon.json
status ok or partial, override `anim_mocap: false` opts out): the clips with a source are built from the motion
files, the others (cast, burrow, ...) by the procedural builders as before, all through the same IK, ground and
export passes. manifest.anim.mocap records the source per clip and the version (MOCAP_VERSION), and
manifest.anim.speeds the ground speed of each locomotion clip (m/s at 1x playback) for root motion.
"""
import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

DATA = ROOT / "data" / "quadmocap"
SAMPLER = ROOT / "tools" / "blender_quad_retarget.py"
PACKS = Path.home() / ".cache" / "monster-forge-scratch" / "quadmocap" / "packs" / "m2ma"
BLENDER = "blender"
MOCAP_VERSION = 1
QUAD_PLANS = {"quadruped", "winged_quadruped"}

# our clip -> (source clip file stem, loops). Sampled once per rig into data/quadmocap/<rig>/<clip>.json.
SOURCE_CLIPS = {
    "fox": {"idle": ("idle", True), "walk": ("walk", True), "trot": ("trot", True), "run": ("run", True),
            "attack_melee": ("bite", False), "death": ("death", False), "swim": ("swim", True)},
    "horse": {"idle": ("idle", True), "walk": ("walk", True), "trot": ("trot", True), "run": ("run", True),
              "attack_melee": ("headbut", False), "kick": ("kick", False), "death": ("death", False)},
    "dragon": {"idle": ("idle", True), "walk": ("walk", True), "attack_melee": ("attack", False),
               "hit": ("hurt", False), "death": ("death", False), "breath": ("fire-breath", False),
               "fly": ("fly-flap", True)},
}


# Which source rig drives a monster's body: hoofed and horned grazers walk like the horse, reptiles and dragons
# (low, long-tailed, sprawling a little) like the dragon, everything else (carnivores, rodents, bears, cats,
# apes) like the fox. By id; winged quadrupeds that are not dragons take the body of their beast.
HORSE = {"camel", "deer", "draft-horse", "elephant", "elk", "giant-elk", "giant-goat", "goat", "gorgon", "mammoth",
         "mule", "nightmare", "pegasus", "pony", "rhinoceros", "riding-horse", "triceratops", "unicorn",
         "warhorse", "warhorse-skeleton", "boar", "giant-boar", "hippopotamus", "hippogriff"}
DRAGON_IDS = {"crocodile", "giant-crocodile", "lizard", "giant-lizard", "hydra", "dragon-turtle", "ankylosaurus",
              "pseudodragon"}
# Clips a rig lacks borrowed from another (every source maps onto the same canonical slots): the dragon's hurt
# flinch and fire breath (the fox paddle on a sprawled or hoofed body flailed: those swim procedurally).
BORROW = {"hit": "dragon", "breath": "dragon"}
BORROW_AMP = {"hit": 0.5}  # the dragon's hurt is a big rear-and-crouch: on a fox or horse body half of it reads as a flinch
# Per clip: ground mode and leg IK in blender_clips (mocap()).
CLIP_MODE = {"death": {"ground": "rest", "ik": "contact", "roll_max": 95.0}, "fly": {"ground": "hover", "ik": "off", "wings": True},
             "swim": {"ground": "lift", "ik": "off"}}
EXTRA = ("run",)
# Hip height (m) of the animal each source's timing reads as (the .blend units are arbitrary: the fox is 1 unit
# at the hip): a dog-sized fox, a horse, a horse-sized dragon. blender_clips slows a clip by sqrt(our hip
# height / this) (stride frequency ~ 1 / sqrt(leg length), clamped 0.5-2.5).
NOMINAL_HIP = {"fox": 0.45, "horse": 1.4, "dragon": 1.2}  # offered beyond the animation_set (no SRD set lists it), like the UAL bipeds' run


def rig_for(mon: dict) -> str:
    mid = mon["id"]
    if mid in HORSE:
        return "horse"
    if mid in DRAGON_IDS or "dragon" in mid:
        return "dragon"
    return "fox"


def canonical(d: Path) -> bool:
    try:
        st = json.loads((d / "canon.json").read_text()).get("status")
    except (OSError, ValueError):
        return False
    return st in ("ok", "partial")


def plan(mon: dict, d: Path) -> dict:
    """{clip: spec} for blender_clips.py's job `mocap` ({} = all procedural): canonical quadrupeds only,
    override `anim_mocap` false (off) or {clip: false} (that clip procedural)."""
    import overrides
    if mon.get("body_plan") not in QUAD_PLANS or not canonical(d):
        return {}
    ov = overrides.load(mon["source"], mon["id"]).get("anim_mocap", True)
    if ov is False:
        return {}
    off = {k for k, v in ov.items() if v is False} if isinstance(ov, dict) else set()
    off |= set(gate_fallback(d))  # clips the audit gate sent back to procedural (run_one)
    rig = rig_for(mon)
    want = list(mon.get("animation_set") or []) + [c for c in EXTRA if c in SOURCE_CLIPS[rig]]
    if mon["body_plan"] == "winged_quadruped":  # the procedural death lies flat on the belly, wings clear of
        off.add("death")                         # the floor; the sources roll onto a side (dragon: 127 degrees)
    out = {}
    for clip in want:
        src = rig if clip in SOURCE_CLIPS[rig] else BORROW.get(clip)
        if not src or clip in off or not (DATA / src / f"{clip}.json").exists():
            continue
        out[clip] = {"file": str(DATA / src / f"{clip}.json"), "rig": src, "nominal": NOMINAL_HIP[rig],
                     **CLIP_MODE.get(clip, {}), **({"amp": BORROW_AMP.get(clip, 1.0)} if src != rig else {})}
    return out


def stamp(man: dict, mocap: dict, rep: dict) -> None:
    """manifest.anim.mocap (version, per clip its source rig/clip, which clips stayed procedural) and
    manifest.anim.speeds (m/s at 1x for each locomotion clip with planted feet: walk, run)."""
    a = man.setdefault("anim", {})
    if not mocap:
        a.pop("mocap", None)
        a.pop("speeds", None)
        return
    built = {c: r for c, r in rep["clips"].items() if (r.get("mocap") or {}).get("rig")}
    a["mocap"] = {"version": MOCAP_VERSION, "clips": {c: f"{r['mocap']['rig']}/{r['mocap']['clip']}"
                                                      for c, r in built.items()},
                  "procedural": [c for c in rep["clips"] if c not in built]}
    a["speeds"] = {c: r["speed_mps"] for c, r in built.items() if r.get("speed_mps")}


def stale(mon: dict, d: Path, man: dict) -> str | None:
    """The anim stage is out of date for mocap: a canonical quadruped whose clips predate MOCAP_VERSION."""
    if not plan(mon, d):
        return None
    v = ((man.get("anim") or {}).get("mocap") or {}).get("version")
    return None if v == MOCAP_VERSION else f"mocap clips v{v} (current v{MOCAP_VERSION})"


def sample(packs: Path, rigs: list[str]) -> None:
    for rig in rigs:
        out = DATA / rig
        out.mkdir(parents=True, exist_ok=True)
        ref = out / "_ref.json"
        order = sorted(SOURCE_CLIPS[rig].items(), key=lambda kv: kv[0] != "idle")  # idle first: the reference
        for clip, (stem, loop) in order:
            job = {"rig": rig, "clip": clip, "loop": loop, "out": str(out / f"{clip}.json"),
                   "blend": str(packs / "rigs" / rig / f"animation-{rig}-{stem}.blend"),
                   "rig_dir": str(packs / "rigs")}
            if clip == "idle":
                job["dump_ref"] = str(ref)
            else:
                job["ref"] = str(ref)
            p = subprocess.run([BLENDER, "-b", "--factory-startup", "--python-exit-code", "1", "-P", str(SAMPLER),
                                "--", json.dumps(job)], capture_output=True, text=True, timeout=600)
            if p.returncode or "_OK" not in p.stdout:
                sys.stderr.write(p.stdout[-3000:] + p.stderr[-3000:])
                raise SystemExit(f"{rig} {clip}: sampling failed")
            print(f"{rig} {clip}: {p.stdout.split('_OK', 1)[1].strip()}")
        ref.unlink(missing_ok=True)


OUT = ROOT / "out" / "srd-5.2.1"
GATE_FILE = "mocap_gate.json"  # <id>/: clips the audit gate sent back to procedural ({clip: [flags]})


def gate_fallback(d: Path) -> dict:
    try:
        return json.loads((d / GATE_FILE).read_text()).get("fallback") or {}
    except (OSError, ValueError):
        return {}


def audit_gate(mid: str) -> dict:
    """Measure one monster's clips (tools/clip_audit.py, Blender): {clip: {flag: severity}} of the hard gate."""
    import clip_audit
    t = clip_audit.targets(OUT, [mid])
    if not t:
        return {}
    out = OUT / "_audit"
    out.mkdir(exist_ok=True)
    msg = clip_audit.measure(t[0], out, force=True)
    if "FAILED" in msg:
        raise RuntimeError(msg)
    m = json.loads((out / f"{mid}.json").read_text())
    res = {}
    for f in clip_audit.flags_for(t[0], m):
        if f.get("gate"):
            res.setdefault(f["clip"], {})[f["flag"]] = f["sev"]
    return res


def forge_anim(mid: str) -> None:
    for attempt in range(2):  # once more after a crash (a Blender segfault was seen once, not reproducible)
        p = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "forge.py"), "anim", mid, "--force"],
                           capture_output=True, text=True, timeout=3600)
        if not p.returncode:
            return
    raise RuntimeError(f"{mid}: anim failed\n{p.stdout[-2000:]}{p.stderr[-2000:]}")


def write_fallback(d: Path, fb: dict) -> None:
    if fb:
        (d / GATE_FILE).write_text(json.dumps({"version": MOCAP_VERSION, "fallback": fb}, indent=1) + "\n")
    else:
        (d / GATE_FILE).unlink(missing_ok=True)


def run_one(mid: str, backup: Path | None, gate_only: bool = False) -> dict:
    """Back up <id>/ (once), rebuild its clips (forge.py anim --force: mocap where planned) and audit them. The
    automatic fix for a mocap clip that fails the hard gate (clip_audit.GATE): the procedural clip instead
    (<id>/mocap_gate.json, rebuilt and audited), kept only when its gate severity is lower; otherwise the mocap
    clip is restored. manifest.anim.gate records each failing clip, its flags and severities, and the fix."""
    import shutil
    d = OUT / mid
    if backup is not None and not (backup / mid).exists():
        backup.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", "-a", "--reflink=auto", str(d), str(backup / mid)], check=True)
    if not gate_only:
        forge_anim(mid)
    fm = audit_gate(mid)
    man = json.loads((d / "manifest.json").read_text())
    mocap = ((man.get("anim") or {}).get("mocap") or {}).get("clips") or {}
    cand = {c: f for c, f in fm.items() if c in mocap}
    prior = gate_fallback(d)
    gate = {"checked": time.strftime("%Y-%m-%d"), "clips": {}}
    final = fm
    if cand:
        write_fallback(d, {**prior, **{c: sorted(f) for c, f in cand.items()}})
        forge_anim(mid)
        fp = audit_gate(mid)
        keep = {c for c in cand if sum(fp.get(c, {}).values()) >= sum(cand[c].values())}  # procedural no better
        if keep:
            write_fallback(d, {**prior, **{c: sorted(f) for c, f in cand.items() if c not in keep}})
            forge_anim(mid)
            final = audit_gate(mid)
        else:
            final = fp
        for c, f in cand.items():
            gate["clips"][c] = {"mocap": f, "procedural": fp.get(c, {}),
                                "fix": "kept mocap (procedural no better)" if c in keep else "procedural"}
        man = json.loads((d / "manifest.json").read_text())
    for c, f in final.items():
        if c not in gate["clips"]:
            gate["clips"][c] = {"flags": f, "fix": "none (procedural clip)" if c not in mocap else "none"}
    man.setdefault("anim", {})["gate"] = gate
    import forge as fg
    fg.save_manifest(d, man)
    shutil.rmtree(OUT / "_audit" / "tiles" / mid, ignore_errors=True)
    return {"id": mid, "fallback": sorted(gate_fallback(d)), "failing": final,
            "mocap": sorted((man.get("anim", {}).get("mocap") or {}).get("clips") or {})}


def canonical_ids() -> list[str]:
    return sorted(p.parent.name for p in OUT.glob("*/canon.json") if canonical(p.parent))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["sample", "run", "gate", "plan"])
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--all", action="store_true", help="every canonical quadruped / winged quadruped")
    ap.add_argument("--packs", type=Path, default=PACKS)
    ap.add_argument("--rigs", nargs="*", default=list(SOURCE_CLIPS))
    ap.add_argument("--jobs", type=int, default=1, help="monsters at once (each runs one Blender at a time)")
    ap.add_argument("--backup", type=Path, help="copy <id>/ here once before replacing its clips")
    args = ap.parse_args()
    if args.cmd == "sample":
        sample(args.packs, args.rigs)
        return
    import forge as fg
    reg = fg.load_registry("srd-5.2.1")
    ids = canonical_ids() if args.all else args.ids
    if args.cmd == "plan":
        for mid in ids:
            p = plan(reg[mid], OUT / mid)
            print(mid, rig_for(reg[mid]), {c: f"{v['rig']}{' x' + str(v['amp']) if 'amp' in v else ''}" for c, v in p.items()},
                  "gate fallback:", sorted(gate_fallback(OUT / mid)))
        return
    with ThreadPoolExecutor(args.jobs) as ex:
        futs = {ex.submit(run_one, mid, args.backup, args.cmd == "gate"): mid for mid in ids}
        for f in futs:
            mid = futs[f]
            try:
                r = f.result()
                print(f"{mid}: mocap {r['mocap']} fallback {r['fallback']} failing {r['failing']}", flush=True)
            except Exception as e:  # keep going; the report says which
                print(f"{mid}: ERROR {e}", flush=True)


if __name__ == "__main__":
    main()
