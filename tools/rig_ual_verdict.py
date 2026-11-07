"""Verdict for a direct UAL rig (docs/anim-reuse.md, "Direct UAL rig batch"): should this biped's game
rig be the UAL skeleton (rig_source "ual"), and which UAL clips stay procedural?

    python3 -I tools/rig_ual_verdict.py archmage kobold-warrior     # write rig_ual/verdict.json
    python3 -I tools/rig_ual_verdict.py --all                       # every rig_ual/<variant>/metrics.json
    python3 -I tools/rig_ual_verdict.py --all --compare             # vs the verdict.json on disk, writes nothing
    python3 -I tools/rig_ual_verdict.py --all --compare --no-calls  # the rules alone vs the files

Reads out/srd-5.2.1/<id>/rig_ual/<variant>/metrics.json and model_rigged.glb (tools/rig_ual.py), the
SkinTokens rig's bonemap.json + model_rigged.glb, the registry record and data/rig_ual_calls.json, and
writes out/srd-5.2.1/<id>/rig_ual/verdict.json:
  status            use_ual | needs_skintokens | needs_review
  reasons           why (rules and sight calls, in words)
  procedural_clips  use_ual only: UAL clips not to promote (the procedural clip stays; run: no clip)
  edge_stretch_p999 / min_z / floor_lift_max / ual_clips  per clip, copied from metrics.json
  sockets, height, variant, judged, rules (what the rules alone said: status, procedural_clips, signals)

Rules (the batch's sheet calls, made mechanical; calibrated on the 71 verdicts of 2026-11-06):
- needs_skintokens: an appendage UAL has no bone for, so heat hangs it on a thigh, the spine or an arm
  and it sweeps with them: body plan winged_biped; the prompt names wings, tentacles, extra arms or
  chains; the prompt names a tail and the mesh has one behind the hips (>= TAIL_BEHIND of the vertices
  more than 0.15 h behind the pelvis); the SkinTokens rig has an unmapped chain off the trunk reaching
  >= CHAIN_REACH x height (or the rig stage's wing_/tail_ chains); or the mesh alone shows a long tail
  (>= TAIL_BEHIND_STRONG of the vertices 0.2 h behind).
- needs_review: no metrics (the rig failed), a socket of the record missing, the floor gate failing on
  idle/walk, or edge stretch already >= IDLE_TEAR on the idle (the weights tear standing still).
- use_ual otherwise; procedural_clips: run and attack_melee when the prompt names a long garment (robe,
  vestments, cloak, cape, coat, dress, gown, tabard, wrappings, garments: it splits onto the thighs),
  plus death above GARMENT_DEATH for those, plus any clip over retarget_ual's ceiling (p99.9 > 7 loops,
  > 10 one-shots). Override anim_ual_exclude is not applied here: rig_ual.stage honours it in ual.json.
Sight calls (data/rig_ual_calls.json, {id: {status?, procedural_clips?, reason, judged}}) replace the
rules' status / clip list where a sheet showed otherwise (tails heat put on a thigh that the mesh
statistics miss, capes taking an arm, rags joining the feet). Plain Python + numpy, no Blender.
"""
import argparse
import json
import re
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import retarget_ual  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "srd-5.2.1"
OUT = ROOT / "out" / SOURCE
CALLS = ROOT / "data" / "rig_ual_calls.json"
VARIANT = "skintokens_heat"
CLIP_ORDER = ["idle", "walk", "run", "attack_melee", "hit", "death", "cast"]

APPENDAGE_WORDS = re.compile(r"\bwings?\b|\btentacles?\b|\b(extra|four|six) arms\b|\bchains\b(?! ?(shirt|mail))", re.IGNORECASE)
TAIL_WORDS = re.compile(r"\btails?\b", re.IGNORECASE)
GARMENT_WORDS = re.compile(r"\brob(e|es|ed)\b|\bvestments\b|\bcloak\b|\bcape\b|\bcoat\b|\bdress\b|\bgown\b|"
                           r"\btabard\b|\bwrappings\b|\bgarments\b", re.IGNORECASE)
TAIL_BEHIND = 0.01         # prompt says tail: this share of the vertices > 0.15 h behind the pelvis
TAIL_BEHIND_STRONG = 0.08  # no word needed: this share > 0.2 h behind the pelvis
CHAIN_REACH = 0.4          # an unmapped SkinTokens chain off the trunk reaching this x height
HIP_CHAIN_REACH = 0.15     # ... or off the pelvis reaching this, with TAIL_BEHIND_HIP of the vertices
TAIL_BEHIND_HIP = 0.015    #     > 0.2 h behind the pelvis (a short tail; troll's rag strips have none)
HEADS_WORDS = re.compile(r"\b(two|three|multi)[- ]headed\b|\btwo heads\b", re.IGNORECASE)
IDLE_TEAR = 6.0            # edge stretch p99.9 on the idle
GARMENT_DEATH = 7.0        # garments: death p99.9 above this stays procedural
TRUNK = {"pelvis", "spine_01", "spine_02", "spine_03", "neck_01", "Head"}

_CT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _glb(path: Path):
    b = path.read_bytes()
    n = struct.unpack_from("<I", b, 12)[0]
    g = json.loads(b[20:20 + n])
    bn = struct.unpack_from("<I", b, 20 + n)[0]
    blob = b[28 + n:28 + n + bn]

    def acc(i):
        a = g["accessors"][i]
        v = g["bufferViews"][a["bufferView"]]
        dt, k = np.dtype(_CT[a["componentType"]]), _NC[a["type"]]
        o, st = v.get("byteOffset", 0) + a.get("byteOffset", 0), v.get("byteStride") or dt.itemsize * k
        raw = np.frombuffer(blob, np.uint8, count=st * (a["count"] - 1) + dt.itemsize * k, offset=o)
        return np.lib.stride_tricks.as_strided(raw, (a["count"], dt.itemsize * k), (st, 1)).copy().view(dt).reshape(a["count"], k)
    return g, acc


def tail_behind(glb: Path) -> dict:
    """Share of the skinned mesh's vertices more than 0.15 / 0.2 x height behind the pelvis joint (our
    meshes face -Z, so behind = +Z), counting only vertices whose strongest bone is a leg, pelvis or
    spine bone: a tail or a train that heat hangs on the hips/thighs."""
    g, acc = _glb(glb)
    sk = g["skins"][0]
    names = [g["nodes"][j]["name"] for j in sk["joints"]]
    ibm = acc(sk["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1)
    pelvis = np.linalg.inv(ibm[names.index("pelvis")])[:3, 3]
    node = next(n for n in g["nodes"] if "mesh" in n and "skin" in n)
    P, J, W = [], [], []
    for pr in g["meshes"][node["mesh"]]["primitives"]:
        a = pr["attributes"]
        P.append(acc(a["POSITION"]).astype(float))
        J.append(acc(a["JOINTS_0"]))
        W.append(acc(a["WEIGHTS_0"]).astype(float))
    P, J, W = np.concatenate(P), np.concatenate(J), np.concatenate(W)
    h = float(P[:, 1].max() - P[:, 1].min())
    dom = J[np.arange(len(J)), W.argmax(1)]
    low = np.array([any(k in n for k in ("pelvis", "spine", "thigh", "calf", "foot", "ball")) for n in names])[dom]
    z = (P[:, 2] - pelvis[2]) / h
    return {"behind_015": round(float((low & (z > 0.15)).mean()), 4), "behind_02": round(float((low & (z > 0.2)).mean()), 4)}


def skin_chains(mid: str) -> list:
    """Unmapped SkinTokens chains hanging off a trunk bone: [(root, ual parent, joints, reach x height)]."""
    import bonemap
    d = OUT / mid
    if not (d / "bonemap.json").exists() or not (d / "model_rigged.glb").exists():
        return []
    bm = json.loads((d / "bonemap.json").read_text())
    sk = bonemap.load_skeleton(d / "model_rigged.glb")
    j, h = sk["joints"], sk["height"] or 1.0
    unm = set(bm.get("unmapped") or [])
    out = []
    for r in sorted(unm):
        par = j.get(r, {}).get("parent")
        if r not in j or bm["map"].get(par) not in TRUNK:
            continue
        base, stack, reach, cnt = j[par]["pos"], [r], 0.0, 0
        while stack:
            x = stack.pop()
            cnt += 1
            reach = max(reach, float(np.linalg.norm(np.subtract(j[x]["pos"], base))))
            stack += j[x]["children"]
        out.append((r, bm["map"][par], cnt, round(reach / h, 2)))
    return out


def rules(mid: str, mon: dict, m: dict | None, variant: str) -> dict:
    """What the rules alone say: {status, reasons, procedural_clips, signals}."""
    prompt = (mon.get("reference_prompt") or "").split(". Low-poly")[0]
    sig, sk_why, rev_why = {}, [], []
    if mon.get("body_plan") == "winged_biped":
        sk_why.append("winged biped: the wings get no UAL bone")
    w = APPENDAGE_WORDS.search(prompt)
    if w:
        sk_why.append(f"prompt names '{w.group(0)}': no UAL bone for it")
    chains = skin_chains(mid)
    sig["skintokens_chains"] = chains
    long_chains = [c for c in chains if c[3] >= CHAIN_REACH or c[0].startswith(("wing_", "tail_"))]
    hip_chains = [c for c in chains if c[1] == "pelvis" and c[3] >= HIP_CHAIN_REACH]
    if long_chains:
        sk_why.append("SkinTokens rig has a chain off the trunk UAL lacks: "
                      + ", ".join(f"{c[0]} on {c[1]} ({c[2]} joints, {c[3]} h)" for c in long_chains))
    glb = OUT / mid / "rig_ual" / variant / "model_rigged.glb"
    if glb.exists():
        tb = tail_behind(glb)
        sig.update(tb)
        if TAIL_WORDS.search(prompt) and tb["behind_015"] >= TAIL_BEHIND:
            sk_why.append(f"tail (prompt) behind the hips ({tb['behind_015']:.1%} of the vertices > 0.15 h back) rides the hips/thighs")
        elif tb["behind_02"] >= TAIL_BEHIND_STRONG:
            sk_why.append(f"{tb['behind_02']:.0%} of the vertices > 0.2 h behind the pelvis (a tail) ride the hips/thighs")
        elif hip_chains and not long_chains and tb["behind_02"] >= TAIL_BEHIND_HIP:
            sk_why.append(f"SkinTokens chain off the pelvis ({hip_chains[0][0]}, {hip_chains[0][3]} h) with "
                          f"{tb['behind_02']:.1%} of the vertices > 0.2 h behind it: a tail on the hips/thighs")
    proc, why = [], []
    hw = HEADS_WORDS.search(prompt)
    if hw:
        rev_why.append(f"prompt says '{hw.group(0)}': UAL has one head bone, the other head rides a shoulder")
    if m is None:
        rev_why.append(f"no rig_ual/{variant}/metrics.json: the direct rig failed (see its log)")
    else:
        clips = m["clips"]
        want = [s for s in mon.get("sockets") or [] if s not in (m["rig"].get("sockets") or {})]
        if want:
            rev_why.append(f"sockets missing on the UAL rig: {', '.join(want)}")
        idle = clips.get("idle", {}).get("edge_stretch_p999", 0.0)
        sig["idle_stretch"] = idle
        if idle >= IDLE_TEAR:
            rev_why.append(f"edge stretch {idle:.1f} already on the idle: the weights tear standing still")
        for c in ("idle", "walk"):
            r = clips.get(c)
            if r and r["min_z"] < -retarget_ual.FLOOR_FRAC * m["height"]:
                rev_why.append(f"{c} sinks {r['min_z']:+.3f} m under the floor")
        g = GARMENT_WORDS.search(prompt)
        if g:
            sig["garment"] = g.group(0)
            gp = [c for c in ("run", "attack_melee") if c in clips]
            if clips.get("death", {}).get("edge_stretch_p999", 0.0) > GARMENT_DEATH:
                gp.append("death")
            if gp:
                proc += gp
                why.append(f"{g.group(0)} (prompt) splits onto the thighs: {', '.join(gp)} stay procedural")
        over = [c for c, r in clips.items() if c not in proc and r["edge_stretch_p999"]
                > retarget_ual.STRETCH_CEIL["loop" if c in retarget_ual.LOOPS else "once"]]
        if over:
            proc += over
            why.append("stretch over the gate ceiling (7 loops / 10 one-shots): " + ", ".join(over))
    if sk_why:
        return {"status": "needs_skintokens", "reasons": sk_why + rev_why, "procedural_clips": [], "signals": sig}
    if rev_why:
        return {"status": "needs_review", "reasons": rev_why, "procedural_clips": [], "signals": sig}
    proc = [c for c in CLIP_ORDER if c in proc]
    return {"status": "use_ual", "reasons": why or ["no appendage, garment or stretch flag"],
            "procedural_clips": proc, "signals": sig}


def verdict(mid: str, mon: dict, variant: str = VARIANT, calls: dict | None = None) -> dict:
    mp = OUT / mid / "rig_ual" / variant / "metrics.json"
    m = json.loads(mp.read_text()) if mp.exists() else None
    r = rules(mid, mon, m, variant)
    v = {"status": r["status"], "reasons": list(r["reasons"]), "procedural_clips": list(r["procedural_clips"]),
         "variant": variant}
    call = (calls or {}).get(mid)
    if call:
        if "status" in call:
            v["status"] = call["status"]
        if "procedural_clips" in call:
            v["procedural_clips"] = list(call["procedural_clips"])
        if v["status"] != "use_ual":
            v["procedural_clips"] = []
        if call.get("status", r["status"]) != r["status"] or "procedural_clips" in call:
            v["reasons"] = [f"sight: {call['reason']}"] + [f"rules ({r['status']}): {x}" for x in r["reasons"]]
        else:
            v["reasons"].append(f"sight: {call['reason']}")
    if m:
        cl = m["clips"]
        v.update({"edge_stretch_p999": {c: x["edge_stretch_p999"] for c, x in cl.items()},
                  "min_z": {c: x["min_z"] for c, x in cl.items()},
                  "floor_lift_max": {c: x["floor_lift_max"] for c, x in cl.items()},
                  "ual_clips": {c: x["ual_clip"] for c, x in cl.items()},
                  "sockets": sorted(m["rig"].get("sockets") or []), "height": m["height"]})
    v["judged"] = (f"{call['judged']}; rules {RULES_DATE}" if call else
                   f"rules only ({RULES_DATE}): no sight call in data/rig_ual_calls.json") + "; not loaded in the viewer or Godot"
    v["rules"] = {"status": r["status"], "procedural_clips": r["procedural_clips"], "signals": r["signals"]}
    return v


RULES_DATE = "2026-11-07"


def candidates(variant: str) -> list[str]:
    reg = retarget_ual.registry()
    ids = {p.parts[-4] for p in OUT.glob(f"*/rig_ual/{variant}/metrics.json")}
    ids |= {p.parts[-3] for p in OUT.glob("*/rig_ual/verdict.json")}
    return sorted(i for i in ids if i in reg)


def same(a: dict, b: dict) -> tuple[bool, bool]:
    st = a.get("status") == b.get("status")
    return st, st and set(a.get("procedural_clips") or []) == set(b.get("procedural_clips") or [])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--all", action="store_true", help="every monster with rig_ual/<variant>/metrics.json or a verdict")
    ap.add_argument("--variant", default=VARIANT)
    ap.add_argument("--compare", action="store_true", help="compare with the verdict.json on disk; write nothing")
    ap.add_argument("--no-calls", action="store_true", help="the rules alone (ignore data/rig_ual_calls.json)")
    a = ap.parse_args()
    ids = candidates(a.variant) if a.all else a.ids
    if not ids:
        ap.error("give monster ids or --all")
    reg = retarget_ual.registry()
    calls = {} if a.no_calls else json.loads(CALLS.read_text()) if CALLS.exists() else {}
    counts, n_st, n_set, n_cmp = {}, 0, 0, 0
    for mid in ids:
        v = verdict(mid, reg[mid], a.variant, calls)
        counts.setdefault(v["status"], []).append(mid)
        f = OUT / mid / "rig_ual" / "verdict.json"
        if a.compare:
            if not f.exists():
                print(f"{mid:22s} {v['status']:16s} (no verdict on disk)")
                continue
            old = json.loads(f.read_text())
            st, full = same(v, old)
            n_cmp += 1
            n_st += st
            n_set += full
            if not full:
                print(f"{mid:22s} disk {old['status']:16s} {','.join(old.get('procedural_clips') or []):30s} | "
                      f"now {v['status']:16s} {','.join(v['procedural_clips'])}")
            continue
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(v, indent=1) + "\n")
        print(f"{mid:22s} {v['status']:16s} {','.join(v['procedural_clips']):30s} {v['reasons'][0][:90]}")
    print("\n" + ", ".join(f"{k} {len(x)}" for k, x in sorted(counts.items())))
    if a.compare and n_cmp:
        print(f"vs disk ({n_cmp}): status {n_st}/{n_cmp} = {n_st / n_cmp:.0%}, status + procedural_clips "
              f"{n_set}/{n_cmp} = {n_set / n_cmp:.0%}")


if __name__ == "__main__":
    main()
