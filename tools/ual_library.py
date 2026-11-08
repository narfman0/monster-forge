"""Shared UAL clip library (docs/anim-reuse.md, "Shared UAL library"): the UAL clips of every monster
rigged straight onto the UAL skeleton (rig_source "ual") stored once, not once per monster.

    python3 -I tools/ual_library.py check ape skeleton          # report only: which clips match the library
    python3 -I tools/ual_library.py build                       # out/<source>/_shared/ual/<clip>.glb + library.json
    python3 -I tools/ual_library.py share ape skeleton | --all  # build if needed, drop the shared clips from anims/
    python3 -I tools/ual_library.py unshare ape | --all         # put anims/<clip>.glb back (from anims_ual/)
    python3 -I tools/ual_library.py stats --all                 # files / bytes saved

Run after `forge.py promote <id>` (also `forge.py share <id>`); promote copies the clips back into anims/,
so share again after it. Pure Python + numpy (reads and writes the GLB JSON/BIN itself), no Blender.

Why it works (checked by `check` on every clip it shares): blender_rig_ual.py makes each monster's
skeleton a copy of UAL's (same names and hierarchy) and bakes a UAL clip so that its exported LOCAL
rotations equal UAL's own (a constant per-bone rest conversion C_b). So a clip differs between monsters
only in
- translation channels: every bone's is its constant rest offset (the monster's proportions), except
  the pelvis: pelvis(t) = s * u(t) + c, u = UAL's pelvis track, s = hip_scale (leg length / UAL's),
  c = a per-monster constant (root-local; our pelvis-over-hips offset vs UAL's, scaled);
- per-monster floor lift (deaths, some walks: the body raised out of the floor, vertical, on top of c);
- extra chains / sockets (bones UAL has no clip data for).
A library clip is UAL's clip resampled exactly as sample_clip() does (30 fps; Blender imports UAL at the
scene's 30 fps and the bake reads it at that rate, so clips keep UAL's own length; before library
version 2 the bake read 24 fps and clips ran 1.25x long) on UAL's own skeleton (UAL1 rest pose), with rotation channels for the 64 non-root bones and the
pelvis translation, nothing else. A player rebuilds a monster's clip as: rotations as is, pelvis
translation s * u + c, every other bone at its own rest. A clip is shared only when that rebuild matches
the monster's baked clip (rotations <= ROT_TOL_DEG, pelvis <= max(PELVIS_TOL_M, PELVIS_TOL_FRAC x
height), every other channel constant at the bone's rest); the rest stay per-monster in anims/
(procedural clips, anim_ual_exclude, lifted clips, clips keying extra chains).

manifest.anim.ual_shared (the contract for the viewer and godot-dnd):
    {"library": "_shared/ual" (relative to out/<source>/), "hip_scale": s, "pelvis_offset": [x, y, z]
     (glTF, in the pelvis's parent = root bone's frame), "root_bone": "root", "pelvis_bone": "pelvis",
     "clips": {our clip: library clip name}, "own": {our clip: why it stays in anims/}}
`anim.clips` still lists every clip the monster has. library.json: {"clips": {name: {"file", "ual",
"frames", "seconds", "loop"}}, "bones", "fps", ...}.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import retarget_ual  # noqa: E402  (packs(); no Blender needed)

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "srd-5.2.1"
OUT = ROOT / "out" / SOURCE
LIB_REL = "_shared/ual"
VERSION = 2
FPS = 30          # blender_rig_ual.FPS: output frames per second
IMPORT_FPS = 30   # the scene fps Blender's glTF importer converts UAL's seconds to frames with
UAL_FPS = IMPORT_FPS  # blender_rig_ual.UAL_FPS: what sample_clip() takes those frames to be (was 24 before v2)
LOOP_CLIPS = {"idle", "walk", "run"}
ROT_TOL_DEG = 0.1
POS_TOL_M = 2e-4          # non-pelvis translation channels vs the bone's rest
PELVIS_TOL_M = 0.001      # pelvis vs s * u + c + lift (the rebuild is exact to ~1e-5 m)
PELVIS_TOL_FRAC = 0.0025  # lift_ok=False only: floor lifts above this x height keep the clip per-monster
LIFT_MIN_M = 0.0005       # smaller lifts are dropped (no curve)
ROOT_BONE, PELVIS_BONE = "root", "pelvis"

# --- GLB IO ------------------------------------------------------------------------------------
_CT = {5126: np.float32, 5123: np.uint16, 5125: np.uint32, 5121: np.uint8, 5122: np.int16, 5120: np.int8}
_NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def read_glb(p: Path) -> tuple[dict, bytes]:
    b = Path(p).read_bytes()
    if b[:4] != b"glTF":
        raise ValueError(f"{p}: not a GLB")
    n = struct.unpack("<I", b[12:16])[0]
    j = json.loads(b[20:20 + n])
    o = 20 + n
    binc = b[o + 8:o + 8 + struct.unpack("<I", b[o:o + 4])[0]] if len(b) > o + 8 else b""
    return j, binc


def accessor(j: dict, binc: bytes, i: int) -> np.ndarray:
    a = j["accessors"][i]
    bv = j["bufferViews"][a["bufferView"]]
    k, dt = _NC[a["type"]], _CT[a["componentType"]]
    off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = bv.get("byteStride")
    item = k * np.dtype(dt).itemsize
    if stride and stride != item:
        raw = np.frombuffer(binc, np.uint8, (a["count"] - 1) * stride + item, off)
        arr = np.stack([np.frombuffer(raw[r * stride:r * stride + item].tobytes(), dt) for r in range(a["count"])])
    else:
        arr = np.frombuffer(binc, dt, a["count"] * k, off).reshape(a["count"], k)
    arr = arr.astype(np.float64)
    return arr[:, 0] if k == 1 else arr


def write_glb(p: Path, j: dict, binc: bytes) -> None:
    js = json.dumps(j, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    binc += b"\0" * (-len(binc) % 4)
    body = struct.pack("<I4s", len(js), b"JSON") + js + struct.pack("<I4s", len(binc), b"BIN\0") + binc
    tmp = Path(str(p) + ".tmp")
    tmp.write_bytes(struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body)
    tmp.replace(p)


def channels(j: dict, binc: bytes, anim: dict) -> dict:
    """{(node name, path): (times, values, interpolation)} of one glTF animation."""
    out = {}
    for c in anim["channels"]:
        s = anim["samplers"][c["sampler"]]
        out[(j["nodes"][c["target"]["node"]]["name"], c["target"]["path"])] = (
            accessor(j, binc, s["input"]), accessor(j, binc, s["output"]), s.get("interpolation", "LINEAR"))
    return out


def sample(ch, t: np.ndarray, quat: bool) -> np.ndarray:
    """A channel at times t (LINEAR: component lerp, quaternions sign-aligned then normalised, as
    Blender's fcurves evaluate them; STEP: hold; clamped at the ends)."""
    ti, v, interp = ch
    v = np.array(v, float).reshape(len(ti), -1)
    if quat:
        for k in range(1, len(v)):
            if np.dot(v[k], v[k - 1]) < 0:
                v[k] = -v[k]
    if len(ti) == 1:
        out = np.repeat(v[:1], len(t), 0)
    elif interp == "STEP":
        out = v[np.clip(np.searchsorted(ti, t + 1e-6, side="right") - 1, 0, len(ti) - 1)]
    else:
        out = np.stack([np.interp(t, ti, v[:, e]) for e in range(v.shape[1])], 1)
    if quat:
        out = out / np.linalg.norm(out, axis=1)[:, None]
    return out


def qmul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ax, ay, az, aw = np.moveaxis(a, -1, 0)
    bx, by, bz, bw = np.moveaxis(b, -1, 0)
    return np.stack([aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz], -1)


def qrot(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    qv = np.concatenate([v, np.zeros(v.shape[:-1] + (1,))], -1)
    qc = q * np.array([-1, -1, -1, 1.0])
    return qmul(qmul(q, qv), qc)[..., :3]


def qangle_deg(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    d = np.clip(np.abs((a * b).sum(-1)) / (np.linalg.norm(a, axis=-1) * np.linalg.norm(b, axis=-1)), 0, 1)
    return np.degrees(2 * np.arccos(d))


def trs_matrix(n: dict) -> np.ndarray:
    x, y, z, w = n.get("rotation", [0, 0, 0, 1])
    r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    m = np.eye(4)
    m[:3, :3] = r * np.array(n.get("scale", [1, 1, 1]))[None, :]
    m[:3, 3] = n.get("translation", [0, 0, 0])
    return m


# --- the UAL packs -------------------------------------------------------------------------------
_PACKS: list | None = None


def packs() -> list[dict]:
    """[{path, j, bin, anims: {name: anim}}] in retarget_ual.packs() order (UAL1 first: first wins,
    as in blender_rig_ual)."""
    global _PACKS
    if _PACKS is None:
        _PACKS = []
        for p in retarget_ual.packs():
            j, b = read_glb(Path(p))
            _PACKS.append({"path": p, "j": j, "bin": b, "anims": {a["name"]: a for a in j.get("animations", [])}})
    return _PACKS


def ual_skeleton() -> tuple[dict, list[str]]:
    """UAL1's skeleton: ({bone: node dict}, bones in skin joint order)."""
    pk = packs()[0]
    j = pk["j"]
    skin = j["skins"][0]
    names = [j["nodes"][i]["name"] for i in skin["joints"]]
    return {j["nodes"][i]["name"]: j["nodes"][i] for i in skin["joints"]}, names


def ual_action(name: str) -> tuple[dict, dict]:
    for pk in packs():
        if name in pk["anims"]:
            return pk, pk["anims"][name]
    raise KeyError(f"UAL clip {name!r} in no pack")


def canonical(key: str) -> dict:
    """The library clip for a UAL clip ("A+B" chained), resampled the way blender_rig_ual.sample_clip
    does: {times, frames, seconds, rot: {bone: (n,4)}, pelvis: (n,3)} on UAL's skeleton."""
    _, bones = ual_skeleton()
    segs, total = [], 0.0
    for part in key.split("+"):
        pk, an = ual_action(part)
        ch = channels(pk["j"], pk["bin"], an)
        t0, t1 = min(c[0].min() for c in ch.values()), max(c[0].max() for c in ch.values())
        # Blender keeps keyframe frames (time x scene fps) as float32: 1.5333333 s -> frame 46.0, not
        # 45.999999, and round(57.5) vs round(57.4999) decides the frame count
        f0, f1 = (float(np.float32(np.float32(x) * IMPORT_FPS)) for x in (t0, t1))
        segs.append((ch, t0, f1 - f0, total))
        total += (f1 - f0) / UAL_FPS   # seconds as the bake reads them
    n = max(1, round(total * FPS))
    tt = total * np.arange(n + 1) / n
    rot = {b: np.zeros((n + 1, 4)) for b in bones}
    pel = np.zeros((n + 1, 3))
    ks = [max(k for k, sg in enumerate(segs) if sg[3] <= x + 1e-9) for x in tt]
    for k, (ch, t0, nframes, start) in enumerate(segs):
        idx = np.array([i for i, kk in enumerate(ks) if kk == k], int)
        if not len(idx):
            continue
        uf = np.minimum(nframes, (tt[idx] - start) * UAL_FPS)  # frames after the action's first
        tg = t0 + uf / IMPORT_FPS
        for b in bones:
            c = ch.get((b, "rotation"))
            rot[b][idx] = sample(c, tg, True) if c else np.array(ual_skeleton()[0][b].get("rotation", [0, 0, 0, 1]))
        c = ch.get((PELVIS_BONE, "translation"))
        pel[idx] = sample(c, tg, False) if c else np.array(ual_skeleton()[0][PELVIS_BONE].get("translation", [0, 0, 0]))
    # one sign convention per bone (continuity), like the bake
    for b in bones:
        q = rot[b]
        for i in range(1, len(q)):
            if np.dot(q[i], q[i - 1]) < 0:
                q[i] = -q[i]
    return {"key": key, "times": np.arange(n + 1) / FPS, "frames": n, "seconds": round(total, 3), "rot": rot,
            "pelvis": pel}


_CANON: dict = {}


def canon(key: str) -> dict:
    if key not in _CANON:
        _CANON[key] = canonical(key)
    return _CANON[key]


def clip_name(key: str) -> str:
    """Library clip / file name for a UAL clip key ("A+B+C" -> "A-B-C")."""
    return key.replace("+", "-")


# --- per monster ------------------------------------------------------------------------------
def manifest(d: Path) -> dict:
    p = d / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_manifest(d: Path, man: dict) -> None:
    tmp = d / "manifest.json.tmp"
    tmp.write_text(json.dumps(man, indent=2) + "\n")
    tmp.replace(d / "manifest.json")


def is_ual(man: dict) -> bool:
    return (man.get("rig") or {}).get("tool") == "ual"


def ual_ids() -> list[str]:
    return sorted(p.name for p in OUT.iterdir() if p.is_dir() and not p.name.startswith("_")
                  and is_ual(manifest(p)))


def clip_file(d: Path, clip: str) -> Path | None:
    """The monster's baked clip: anims/<clip>.glb, else (shared already) the identical anims_ual/ copy."""
    for p in (d / "anims" / f"{clip}.glb", d / "anims_ual" / f"{clip}.glb"):
        if p.exists():
            return p
    return None


def candidates(d: Path, man: dict) -> dict[str, str]:
    """{our clip: UAL key} for the clips promote made UAL (manifest anim.source == "ual")."""
    an = man.get("anim") or {}
    out = {}
    for clip, src in (an.get("source") or {}).items():
        if src != "ual":
            continue
        key = (an.get(clip) or {}).get("ual_clip")
        js = d / "anims_ual" / f"{clip}.json"
        if not key and js.exists():
            key = json.loads(js.read_text()).get("ual_clip")
        if key:
            out[clip] = key
    return out


def height(d: Path, man: dict) -> float:
    for p in (d / "anims_ual" / "metrics.json",):
        if p.exists():
            h = json.loads(p.read_text()).get("height")
            if h:
                return float(h)
    return float(man.get("scale_m") or 1.0)


def check(mid: str, lift_ok: bool = True) -> dict:
    """Compare every UAL clip of one monster with the library rebuild. Returns {hip_scale,
    pelvis_offset, clips: {clip: {key, shared, why, rot_err_deg, pelvis_err_m, ...}}, own}."""
    d = OUT / mid
    man = manifest(d)
    if not is_ual(man):
        return {"id": mid, "error": "not a UAL rig (manifest rig.tool != ual)"}
    _, ual_bones = ual_skeleton()
    ubones = set(ual_bones)
    h = height(d, man)
    cand = candidates(d, man)
    data = {}
    for clip, key in cand.items():
        f = clip_file(d, clip)
        if f is None:
            continue
        j, b = read_glb(f)
        if len(j.get("animations", [])) != 1:
            data[clip] = {"key": key, "error": f"{len(j.get('animations', []))} animations in {f.name}"}
            continue
        data[clip] = {"key": key, "j": j, "ch": channels(j, b, j["animations"][0]), "file": f}
    ok = {c: x for c, x in data.items() if "ch" in x and (PELVIS_BONE, "translation") in x["ch"]}
    if not ok:
        return {"id": mid, "error": "no UAL clip files", "clips": {c: {"key": x["key"], "shared": False,
                                                                       "why": [x.get("error", "missing")]} for c, x in data.items()}}
    nodes = {n["name"]: n for n in next(iter(ok.values()))["j"]["nodes"]}
    # up in the pelvis's parent (root) frame: glTF +Y under the Armature node, which has no lift
    rq = np.array(nodes[ROOT_BONE].get("rotation", [0, 0, 0, 1]))
    up = qrot(rq * np.array([-1, -1, -1, 1.0]), np.array([0, 1.0, 0]))
    # s, c: least squares over every frame of every clip on the axes across up (no floor lift there);
    # c along up = the lowest residual (lift >= 0 and is 0 on most frames)
    U, P = [], []
    for c, x in ok.items():
        cn = canon(x["key"])
        tk = max((v[0] for v in x["ch"].values()), key=len)
        if abs(float(tk[-1]) - cn["times"][-1]) > 0.5 / FPS:
            continue  # not the library's timing: not used for the fit
        p = sample(x["ch"][(PELVIS_BONE, "translation")], cn["times"], False)
        U.append(cn["pelvis"])
        P.append(p)
    if not U:
        return {"id": mid, "error": "no clip with the library's frame count"}
    U, P = np.concatenate(U), np.concatenate(P)
    e1 = np.cross(up, [1.0, 0, 0]) if abs(up[0]) < 0.9 else np.cross(up, [0, 1.0, 0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(up, e1)
    A, y = [], []
    for k, e in enumerate((e1, e2)):
        row = np.zeros((len(U), 3))
        row[:, 0] = U @ e
        row[:, 1 + k] = 1
        A.append(row)
        y.append(P @ e)
    sol, *_ = np.linalg.lstsq(np.concatenate(A), np.concatenate(y), rcond=None)
    s = float(sol[0])
    cz = float(np.min(P @ up - s * (U @ up)))
    cvec = sol[1] * e1 + sol[2] * e2 + cz * up
    res = {}
    for clip in cand:
        x = data.get(clip)
        if x is None:
            res[clip] = {"key": cand[clip], "shared": False, "why": ["no clip file"]}
            continue
        if "ch" not in x:
            res[clip] = {"key": x["key"], "shared": False, "why": [x["error"]]}
            continue
        cn = canon(x["key"])
        why, r = [], {"key": x["key"]}
        t = cn["times"]
        ch = x["ch"]
        tk = max((v[0] for v in ch.values()), key=len)  # constant channels may span a frame more
        n_ours, last = len(tk) - 1, float(tk[-1])
        if abs(last - t[-1]) > 0.5 / FPS:
            why.append(f"length {last:.3f}s vs library {t[-1]:.3f}s")
            res[clip] = {**r, "shared": False, "why": why, "frames": n_ours}
            continue
        rot_err, worst = 0.0, ""
        pos_err, extra = 0.0, []
        for (bn, path), c in ch.items():
            node = nodes.get(bn, {})
            if path == "rotation" and bn in ubones and bn != ROOT_BONE:
                e = float(qangle_deg(sample(c, t, True), cn["rot"][bn]).max())
                if e > rot_err:
                    rot_err, worst = e, bn
            elif path == "translation" and bn == PELVIS_BONE:
                continue
            else:  # must stay at the node's rest
                v = sample(c, t, path == "rotation")
                if path == "rotation":
                    e = float(qangle_deg(v, np.array(node.get("rotation", [0, 0, 0, 1]))[None]).max())
                    bad = e > ROT_TOL_DEG
                else:
                    restv = np.array(node.get(path, [1, 1, 1] if path == "scale" else [0, 0, 0]))
                    e = float(np.abs(v - restv[None]).max())
                    bad = e > (1e-3 if path == "scale" else POS_TOL_M)
                    pos_err = max(pos_err, e) if path == "translation" else pos_err
                if bad:
                    extra.append(f"{bn}.{path}")
        p = sample(ch[(PELVIS_BONE, "translation")], t, False)
        dv = p - (s * cn["pelvis"] + cvec)
        lift = np.round(dv @ up, 4)   # the bake's floor lift along up (0 on most clips)
        perr = float(np.linalg.norm(dv - lift[:, None] * up[None], axis=1).max())
        r.update({"rot_err_deg": round(rot_err, 4), "rot_worst": worst, "pelvis_err_m": round(perr, 5),
                  "lift_m": round(float(lift.max()), 4), "rest_err_m": round(pos_err, 6)})
        if float(np.abs(lift).max()) >= LIFT_MIN_M:
            r["lift"] = [float(v) for v in lift]
        if rot_err > ROT_TOL_DEG:
            why.append(f"rotations differ ({worst} {rot_err:.2f} deg)")
        if extra:
            why.append(f"keys bones off their rest: {', '.join(sorted(extra)[:6])}{' ...' if len(extra) > 6 else ''}")
        if perr > PELVIS_TOL_M:
            why.append(f"pelvis off the rebuild by {perr:.4f} m")
        if float(lift.min()) < -PELVIS_TOL_M:
            why.append(f"pelvis below the rebuild by {-float(lift.min()):.4f} m")
        if not lift_ok and float(lift.max()) > max(PELVIS_TOL_M, PELVIS_TOL_FRAC * h):
            why.append(f"floor lift {float(lift.max()):.3f} m")
        res[clip] = {**r, "shared": not why, "why": why}
    return {"id": mid, "height": h, "hip_scale": round(s, 6), "pelvis_offset": [round(float(v), 6) for v in cvec],
            "lift_axis": [round(float(v), 6) for v in up], "clips": res}


# --- library ------------------------------------------------------------------------------------
def lib_dir() -> Path:
    return OUT / LIB_REL


def build(keys: list[str], loops: set[str]) -> dict:
    """Write one GLB per UAL key into _shared/ual/ and library.json (kept: entries of other keys)."""
    out = lib_dir()
    out.mkdir(parents=True, exist_ok=True)
    lj = out / "library.json"
    lib = json.loads(lj.read_text()) if lj.exists() else {}
    skel, bones = ual_skeleton()
    lib.update({"version": VERSION, "fps": FPS, "skeleton": "Quaternius Universal Animation Library 1 (CC0), 65 bones",
                "root_bone": ROOT_BONE, "pelvis_bone": PELVIS_BONE, "bones": bones,
                "tracks": "rotation: every bone but root; translation: pelvis only (s * u + c per monster)",
                "doc": "docs/anim-reuse.md (Shared UAL library)"})
    clips = lib.setdefault("clips", {})
    for key in keys:
        cn = canon(key)
        name = clip_name(key)
        write_clip(out / f"{name}.glb", name, cn, skel, bones)
        clips[name] = {"file": f"{name}.glb", "ual": key, "frames": cn["frames"], "seconds": cn["seconds"],
                       "loop": key in loops or clips.get(name, {}).get("loop", False),
                       "sha256": hashlib.sha256((out / f"{name}.glb").read_bytes()).hexdigest()[:16]}
    lib["clips"] = dict(sorted(clips.items()))
    tmp = lj.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(lib, indent=1) + "\n")
    tmp.replace(lj)
    return lib


def write_clip(path: Path, name: str, cn: dict, skel: dict, bones: list[str]) -> None:
    """Armature-only GLB: UAL's skeleton at its rest (a skin, no mesh) and one animation."""
    pk = packs()[0]
    pj = pk["j"]
    by_name = {n["name"]: i for i, n in enumerate(pj["nodes"])}
    # the skeleton's top node (the Armature) and the bones under it, in a stable order
    parent = {}
    for i, n in enumerate(pj["nodes"]):
        for ch in n.get("children", []):
            parent[ch] = i
    top = by_name[bones[0]]
    while top in parent and pj["nodes"][parent[top]]["name"] in skel:
        top = parent[top]
    arm = parent.get(top)
    order = [b for b in bones]
    idx = {b: i + 1 for i, b in enumerate(order)}  # node 0 = Armature
    nodes = [{"name": pj["nodes"][arm]["name"] if arm is not None else "Armature", "children": [idx[pj["nodes"][top]["name"]]]}]
    if arm is not None:
        for k in ("rotation", "translation", "scale"):
            if k in pj["nodes"][arm]:
                nodes[0][k] = pj["nodes"][arm][k]
    for b in order:
        src = skel[b]
        n = {"name": b}
        for k in ("rotation", "translation", "scale"):
            if k in src:
                n[k] = src[k]
        kids = [idx[pj["nodes"][c]["name"]] for c in src.get("children", []) if pj["nodes"][c]["name"] in idx]
        if kids:
            n["children"] = kids
        nodes.append(n)
    # inverse bind matrices from the rest pose
    world = {}

    def wm(i):
        if i in world:
            return world[i]
        par = next((k for k, n in enumerate(nodes) if i in n.get("children", [])), None)
        m = trs_matrix(nodes[i]) if par is None else wm(par) @ trs_matrix(nodes[i])
        world[i] = m
        return m
    ibm = np.stack([np.linalg.inv(wm(idx[b])) for b in order]).astype(np.float32)
    buf = bytearray()
    views, accs = [], []

    def add(arr: np.ndarray, typ: str, minmax: bool = False) -> int:
        a = np.ascontiguousarray(arr, np.float32)
        while len(buf) % 4:
            buf.append(0)
        views.append({"buffer": 0, "byteOffset": len(buf), "byteLength": a.nbytes})
        buf.extend(a.tobytes())
        acc = {"bufferView": len(views) - 1, "componentType": 5126, "count": int(a.shape[0]), "type": typ}
        if minmax:
            acc["min"] = [float(a.min())]
            acc["max"] = [float(a.max())]
        accs.append(acc)
        return len(accs) - 1

    ibm_acc = add(np.transpose(ibm, (0, 2, 1)).reshape(len(order), 16), "MAT4")  # column-major
    t_all = add(cn["times"], "SCALAR", True)
    t_one = add(cn["times"][:1], "SCALAR", True)
    samplers, chans = [], []
    for b in order:
        if b == ROOT_BONE:
            continue
        q = cn["rot"][b]
        const = np.abs(q - q[:1]).max() < 1e-7
        samplers.append({"input": t_one if const else t_all, "output": add(q[:1] if const else q, "VEC4"),
                         "interpolation": "LINEAR"})
        chans.append({"sampler": len(samplers) - 1, "target": {"node": idx[b], "path": "rotation"}})
    samplers.append({"input": t_all, "output": add(cn["pelvis"], "VEC3"), "interpolation": "LINEAR"})
    chans.append({"sampler": len(samplers) - 1, "target": {"node": idx[PELVIS_BONE], "path": "translation"}})
    j = {"asset": {"version": "2.0", "generator": "monster-forge tools/ual_library.py",
                   "copyright": "Animation: Quaternius Universal Animation Library (CC0 1.0)"},
         "scene": 0, "scenes": [{"name": "Scene", "nodes": [0]}], "nodes": nodes,
         "skins": [{"name": "Armature", "joints": [idx[b] for b in order], "inverseBindMatrices": ibm_acc}],
         "animations": [{"name": name, "channels": chans, "samplers": samplers}],
         "accessors": accs, "bufferViews": views, "buffers": [{"byteLength": len(buf)}]}
    write_glb(path, j, bytes(buf))


# --- share / unshare ------------------------------------------------------------------------------
def share(mid: str, rep: dict | None = None, dry: bool = False) -> dict:
    """Drop every clip check() passes from anims/ (the identical file stays in anims_ual/) and write
    manifest.anim.ual_shared. The library must hold the clips (build first)."""
    d = OUT / mid
    rep = rep or check(mid)
    if rep.get("error"):
        return rep
    lib = json.loads((lib_dir() / "library.json").read_text())
    man = manifest(d)
    an = man.setdefault("anim", {})
    shared, own = {}, {}
    for clip, r in rep["clips"].items():
        name = clip_name(r["key"])
        if r["shared"] and name in lib["clips"]:
            shared[clip] = name
        else:
            own[clip] = "; ".join(r["why"]) or "not in the library"
    for p in sorted((d / "anims").glob("*.glb")):
        if p.stem not in shared and p.stem not in own:
            own[p.stem] = "procedural" if (an.get("source") or {}).get(p.stem) != "ual" else "per-monster"
    if dry:
        return {**rep, "would_share": shared, "own": own}
    for clip in shared:
        g = d / "anims" / f"{clip}.glb"
        keep = d / "anims_ual" / f"{clip}.glb"
        if g.exists():
            if not keep.exists() or keep.read_bytes() != g.read_bytes():
                keep.parent.mkdir(exist_ok=True)
                shutil.copy2(g, keep)  # unshare() restores from here
            g.unlink()
    if shared:
        an["ual_shared"] = {"library": LIB_REL, "version": VERSION, "hip_scale": rep["hip_scale"],
                            "pelvis_offset": rep["pelvis_offset"], "root_bone": ROOT_BONE, "pelvis_bone": PELVIS_BONE,
                            "lift_axis": rep["lift_axis"], "fps": FPS,
                            "clips": dict(sorted(shared.items())), "own": dict(sorted(own.items())),
                            "lift": {c: rep["clips"][c]["lift"] for c in sorted(shared) if "lift" in rep["clips"][c]},
                            "check": {c: {k: r[k] for k in ("rot_err_deg", "pelvis_err_m", "lift_m") if k in r}
                                      for c, r in rep["clips"].items()}}
    else:
        an.pop("ual_shared", None)
    save_manifest(d, man)
    return {**rep, "shared": shared, "own": own}


def unshare(mid: str) -> list[str]:
    d = OUT / mid
    man = manifest(d)
    an = man.get("anim") or {}
    sh = an.get("ual_shared") or {}
    back = []
    for clip in sh.get("clips", {}):
        src, dst = d / "anims_ual" / f"{clip}.glb", d / "anims" / f"{clip}.glb"
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            back.append(clip)
    an.pop("ual_shared", None)
    save_manifest(d, man)
    return back


def stats(ids: list[str]) -> dict:
    lib = lib_dir()
    lib_files = list(lib.glob("*.glb")) if lib.exists() else []
    n_shared = b_shared = n_own = b_own = 0
    for mid in ids:
        d = OUT / mid
        sh = ((manifest(d).get("anim") or {}).get("ual_shared") or {}).get("clips", {})
        for clip in sh:
            f = d / "anims_ual" / f"{clip}.glb"
            n_shared += 1
            b_shared += f.stat().st_size if f.exists() else 0
        for f in (d / "anims").glob("*.glb"):
            n_own += 1
            b_own += f.stat().st_size
    return {"monsters": len(ids), "clips_now_shared": n_shared, "bytes_before_shared": b_shared,
            "library_files": len(lib_files), "library_bytes": sum(f.stat().st_size for f in lib_files),
            "own_files_left": n_own, "own_bytes_left": b_own}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["check", "build", "share", "unshare", "stats"])
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--all", action="store_true", help="every monster whose manifest rig.tool is ual")
    ap.add_argument("--json", type=Path, help="check/share: write the full report here")
    ap.add_argument("--dry-run", action="store_true", help="share: report only")
    ap.add_argument("--no-lift", action="store_true", help="keep clips with a floor lift per-monster (no lift curves)")
    a = ap.parse_args()
    ids = ual_ids() if a.all else a.ids
    if a.cmd == "unshare":
        for mid in ids:
            print(f"{mid}: restored {unshare(mid)}")
        return 0
    if a.cmd == "stats":
        print(json.dumps(stats(ids), indent=1))
        return 0
    reps = {mid: check(mid, lift_ok=not a.no_lift) for mid in ids}
    if a.cmd in ("build", "share"):
        keys = sorted({r["key"] for rep in reps.values() for r in (rep.get("clips") or {}).values() if r.get("shared")}
                      | ({k for k in ("Idle_Loop",)} if a.cmd == "build" and not ids else set()))
        loops = {r["key"] for rep in reps.values() for c, r in (rep.get("clips") or {}).items() if c in LOOP_CLIPS}
        lib = build(keys, loops)
        print(f"library {lib_dir()}: {len(lib['clips'])} clips ({', '.join(lib['clips'])})")
    fails = 0
    for mid, rep in reps.items():
        if rep.get("error"):
            print(f"{mid}: {rep['error']}")
            fails += 1
            continue
        if a.cmd == "share":
            rep.update(share(mid, rep, dry=a.dry_run))
        sh = [c for c, r in rep["clips"].items() if r["shared"]]
        own = {c: r["why"] for c, r in rep["clips"].items() if not r["shared"]}
        print(f"{mid}: s={rep['hip_scale']:.4f} c={rep['pelvis_offset']} shared {sh}"
              + (f"; own {own}" if own else "")
              + f"; max rot {max([r.get('rot_err_deg', 0) for r in rep['clips'].values()] or [0]):.3f} deg")
    if a.json:
        a.json.write_text(json.dumps(reps, indent=1, default=str))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
