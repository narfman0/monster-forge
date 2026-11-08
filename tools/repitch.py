"""Re-pitch an existing monster to what QA does now, without re-painting or re-rolling the rig.

QA's upright pitch (blender_qa.py, docs/rig-anim.md "Lean check") changes model.glb by one
similarity transform (rotation about the side axis, the rescale to scale_m, re-centring on the
feet). `qa --force` would also drop the paint and the SkinTokens skeleton (a GPU re-paint and a
re-roll with new bone names). This tool instead:

1. runs the real blender_qa.py on model_raw.glb into ~/.cache/monster-forge-scratch/repitch/<id>/qa/;
2. builds the similarity M (glTF frame) QA's change amounts to: the pitch change about the side
   axis, the scale_factor ratio, QA's re-centring; checks that M(old model.glb) lands on the new
   QA output (every sampled vertex within 1e-4 x the height of one) and refuses otherwise;
3. backs up what it rewrites to ~/.cache/monster-forge-scratch/repitch/<id>/backup/ and applies M
   in place, as glTF data (no Blender round trip): model.glb (painted texture kept), model_lod*.glb,
   rig/chosen.glb, rig/skintokens_rigged.glb and rig/roll_*.glb (mesh, joints and inverse bind
   matrices: the skeleton and weights stay the same, only placed in the new frame);
4. copies QA's thumbs and qa.json in, updates manifest.qa, and drops paint/cameras.json (a forced
   re-paint must render the control views again).

Then the game files and clips are stale: run `forge.py rig <id> --force` and `forge.py anim <id>
--force` (+ `forge.py promote <id>` for rig_source ual). Both CPU only here: the kept skeleton is
not rolled again.

    python3 -I tools/repitch.py ogre troll            # dry run: QA in scratch, print M and the pitch
    python3 -I tools/repitch.py ogre troll --apply    # write into out/srd-5.2.1/<id>/
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge  # noqa: E402

SCRATCH = Path.home() / ".cache" / "monster-forge-scratch" / "repitch"
GLBS = ("model.glb", "model_lod1.glb", "model_lod2.glb", "model_lod3.glb", "rig/chosen.glb",
        "rig/skintokens_rigged.glb", "rig/roll_0.glb", "rig/roll_1.glb", "rig/roll_2.glb", "rig/roll_3.glb",
        "rig/roll_4.glb", "rig/roll_5.glb", "rig/roll_6.glb", "rig/roll_7.glb")
COPY = ("thumb.png", "thumb_head.png", "thumb_ground.png", "qa.json")


# ---- glb io
def read_glb(p: Path):
    b = p.read_bytes()
    assert b[:4] == b"glTF", p
    n = struct.unpack("<I", b[12:16])[0]
    j = json.loads(b[20:20 + n])
    off = 20 + n
    bin_ = b""
    if off < len(b):
        m = struct.unpack("<I", b[off:off + 4])[0]
        bin_ = bytearray(b[off + 8:off + 8 + m])
    return j, bytearray(bin_)


def write_glb(p: Path, j: dict, bin_: bytearray):
    js = json.dumps(j, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    bin_ = bytes(bin_) + b"\0" * (-len(bin_) % 4)
    out = b"glTF" + struct.pack("<II", 2, 12 + 8 + len(js) + 8 + len(bin_))
    out += struct.pack("<I", len(js)) + b"JSON" + js + struct.pack("<I", len(bin_)) + b"BIN\0" + bin_
    tmp = p.with_suffix(".glb.tmp")
    tmp.write_bytes(out)
    tmp.replace(p)


NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _acc_index(j, i):
    a = j["accessors"][i]
    if a["componentType"] != 5126 or "sparse" in a:
        raise ValueError(f"accessor {i}: only dense float accessors are handled")
    bv = j["bufferViews"][a["bufferView"]]
    nc = NCOMP[a["type"]]
    stride = bv.get("byteStride") or 4 * nc
    base = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    return a, base, stride, nc


def acc_get(j, bin_, i) -> np.ndarray:
    a, base, stride, nc = _acc_index(j, i)
    out = np.empty((a["count"], nc), np.float32)
    for k in range(a["count"]):
        out[k] = np.frombuffer(bin_, np.float32, nc, base + k * stride)
    return out.astype(np.float64)


def acc_set(j, bin_, i, v: np.ndarray):
    a, base, stride, nc = _acc_index(j, i)
    v = np.asarray(v, np.float32).reshape(a["count"], nc)
    for k in range(a["count"]):
        bin_[base + k * stride: base + k * stride + 4 * nc] = v[k].tobytes()
    if "min" in a:
        a["min"], a["max"] = [float(x) for x in v.min(0)], [float(x) for x in v.max(0)]


# ---- matrices (glTF: column-major 4x4)
def quat_to_m(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def m_to_quat(R):
    t = np.trace(R)
    if t > 0:
        s = math.sqrt(t + 1) * 2
        q = [(R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s, s / 4]
    else:
        i = int(np.argmax(np.diag(R)))
        j_, k = (i + 1) % 3, (i + 2) % 3
        s = math.sqrt(1 + R[i, i] - R[j_, j_] - R[k, k]) * 2
        q = [0.0] * 4
        q[i] = s / 4
        q[j_] = (R[j_, i] + R[i, j_]) / s
        q[k] = (R[k, i] + R[i, k]) / s
        q[3] = (R[k, j_] - R[j_, k]) / s
    q = np.array(q)
    return q / np.linalg.norm(q)


def node_m(n) -> np.ndarray:
    if "matrix" in n:
        return np.array(n["matrix"], float).reshape(4, 4).T
    M = np.eye(4)
    M[:3, :3] = quat_to_m(n.get("rotation", [0, 0, 0, 1])) * np.array(n.get("scale", [1, 1, 1]))
    M[:3, 3] = n.get("translation", [0, 0, 0])
    return M


def set_node_m(n, M):
    n.pop("matrix", None)
    sc = np.linalg.norm(M[:3, :3], axis=0)
    n["translation"] = [float(x) for x in M[:3, 3]]
    n["rotation"] = [float(x) for x in m_to_quat(M[:3, :3] / sc)]
    if np.allclose(sc, 1, atol=1e-6):
        n.pop("scale", None)
    else:
        n["scale"] = [float(x) for x in sc]


def transform_glb(p: Path, M: np.ndarray) -> dict:
    """Apply the similarity M (glTF world frame) to every mesh and skeleton of a glb in place."""
    j, bin_ = read_glb(p)
    if j.get("animations"):
        raise ValueError(f"{p}: has animations (not handled)")
    nodes = j["nodes"]
    parent = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}

    def world(i):
        W = np.eye(4)
        while i is not None:
            W = node_m(nodes[i]) @ W
            i = parent.get(i)
        return W

    s = float(np.cbrt(np.linalg.det(M[:3, :3])))
    R = M[:3, :3] / s
    D = np.diag([1 / s, 1 / s, 1 / s, 1.0])
    Minv = np.linalg.inv(M)
    joints = {jt for sk in j.get("skins", []) for jt in sk["joints"]}
    done_pos, done_nrm = set(), set()
    for i, n in enumerate(nodes):
        if "mesh" not in n:
            continue
        skinned = "skin" in n
        W = np.eye(4) if skinned else world(i)  # a skinned mesh's node transform is ignored
        if not np.allclose(W, np.eye(4), atol=1e-6):
            raise ValueError(f"{p}: mesh node {n.get('name')} has a transform (not handled)")
        for pr in j["meshes"][n["mesh"]]["primitives"]:
            at = pr["attributes"]
            if "targets" in pr:
                raise ValueError(f"{p}: morph targets (not handled)")
            if at["POSITION"] not in done_pos:
                v = acc_get(j, bin_, at["POSITION"])
                acc_set(j, bin_, at["POSITION"], v @ M[:3, :3].T + M[:3, 3])
                done_pos.add(at["POSITION"])
            if "NORMAL" in at and at["NORMAL"] not in done_nrm:
                v = acc_get(j, bin_, at["NORMAL"]) @ R.T
                acc_set(j, bin_, at["NORMAL"], v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12))
                done_nrm.add(at["NORMAL"])
            if "TANGENT" in at:
                raise ValueError(f"{p}: tangents (not handled)")
    for jt in joints:
        par = parent.get(jt)
        if par in joints:  # child joint: J'_c = J'_p (D^-1 L D): translation x s
            L = node_m(nodes[jt])
            L[:3, 3] *= s
            set_node_m(nodes[jt], L)
        else:  # root joint under non-joint ancestors A: L' = A^-1 M A L D
            A = world(par) if par is not None else np.eye(4)
            set_node_m(nodes[jt], np.linalg.inv(A) @ M @ A @ node_m(nodes[jt]) @ D)
    for sk in j.get("skins", []):
        if "inverseBindMatrices" in sk:
            ib = acc_get(j, bin_, sk["inverseBindMatrices"])
            new = [(np.diag([s, s, s, 1.0]) @ m.reshape(4, 4).T @ Minv).T.reshape(16) for m in ib]
            acc_set(j, bin_, sk["inverseBindMatrices"], np.array(new))
    write_glb(p, j, bin_)
    return {"file": p.name, "positions": len(done_pos), "joints": len(joints)}


def positions(p: Path) -> np.ndarray:
    j, bin_ = read_glb(p)
    return np.concatenate([acc_get(j, bin_, pr["attributes"]["POSITION"])
                           for m in j["meshes"] for pr in m["primitives"]])


def similarity(fwd: float, delta_deg: float, sc: float, v: np.ndarray) -> np.ndarray:
    """QA's change as one glTF-frame matrix: the pitch about X (blender_qa.py: Blender X = glTF X,
    the same angle), the scale ratio, then QA's re-centring (bbox centre over the origin in the
    ground plane, lowest point at 0) of the old vertices `v` so moved."""
    a = math.radians(-fwd * delta_deg)
    M = np.eye(4)
    M[:3, :3] = sc * np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
    w = v @ M[:3, :3].T
    mn, mx = w.min(0), w.max(0)
    M[:3, 3] = (-(mn[0] + mx[0]) / 2, -mn[1], -(mn[2] + mx[2]) / 2)  # glTF: Y up, Z = -Blender Y
    return M


def nearest_gap(a: np.ndarray, b: np.ndarray, n: int = 3000) -> float:
    """Largest distance from a sample of b to its nearest point of a (painted exports split a few
    more seam vertices, so the two lists do not correspond one to one)."""
    rng = np.random.default_rng(0)
    q = b[rng.choice(len(b), size=min(n, len(b)), replace=False)]
    best = np.full(len(q), np.inf)
    for i in range(0, len(a), 2000):
        d = ((q[:, None, :] - a[None, i:i + 2000, :]) ** 2).sum(-1).min(1)
        best = np.minimum(best, d)
    return float(np.sqrt(best.max()))


def qa_job(mon, d: Path, o: Path, pitch=None) -> dict:
    man = forge.load_manifest(d)
    ov = forge.ov_for(mon)
    yaw = ov.get("yaw", man.get("qa", {}).get("yaw_override"))
    return {"src": str(d / "model_raw.glb"), "dst": str(o / "model.glb"), "thumb": str(o / "thumb.png"),
            "thumb_head": str(o / "thumb_head.png"), "body_plan": mon["body_plan"], "report": str(o / "qa.json"),
            "scale_m": mon["scale_m"], "scale_axis": forge.scale_axis(mon),
            "front_view": mon["body_plan"] in forge.FRONT_VIEW_PLANS, "yaw_override": yaw,
            "forward": ov.get("forward") or "-Z", "label": f"{mon['name']} ({mon['body_plan']})",
            "thumb_ground": str(o / "thumb_ground.png"), "id": mon["id"], "source": mon["source"],
            "level": None, "level_tilt": None, "level_pitch": pitch}


def one(mid: str, reg: dict, apply: bool, pitch=None) -> dict:
    mon = forge.resolve_swarm_unit(reg, reg[mid])
    d = forge.out_dir("srd-5.2.1", mid, "")
    w = SCRATCH / mid
    o = w / "qa"
    shutil.rmtree(o, ignore_errors=True)
    o.mkdir(parents=True)
    job = qa_job(mon, d, o, pitch)
    t0 = time.time()
    p = subprocess.run([forge.BLENDER, "-b", "--factory-startup", "-P", str(forge.ROOT / "tools" / "blender_qa.py"),
                        "--", json.dumps(job)], capture_output=True, text=True, timeout=600)
    if p.returncode or not (o / "qa.json").exists():
        return {"id": mid, "error": "blender QA failed: " + (p.stdout[-600:] + p.stderr[-600:])}
    rep = json.loads((o / "qa.json").read_text())
    rep["seconds"] = round(time.time() - t0, 1)
    pr = (rep.get("level") or {}).get("pitch") or {}
    old = (forge.load_manifest(d).get("qa") or {}).get("level", {}).get("pitch") or {}
    out = {"id": mid, "pitch": pr, "old_pitch_deg": old.get("applied_deg", 0.0)}
    old_qa = forge.load_manifest(d).get("qa") or {}
    fwd = -1.0 if job["forward"] == "+Z" else 1.0
    delta = float(pr.get("applied_deg", 0.0)) - float(out["old_pitch_deg"])
    sc = float(rep["scale_factor"]) / float(old_qa.get("scale_factor") or rep["scale_factor"])
    M = similarity(fwd, delta, sc, positions(d / "model.glb"))
    a, b = positions(d / "model.glb"), positions(o / "model.glb")
    h = float(np.ptp(b[:, 1]))  # glTF +Y up
    gap = nearest_gap(a @ M[:3, :3].T + M[:3, 3], b) / h
    out.update(M=[[round(float(x), 6) for x in r] for r in M], delta_deg=round(delta, 3), scale=round(sc, 6),
               max_gap_rel=round(gap, 7))
    if gap > 1e-4:
        return {**out, "error": f"M(model.glb) misses the new QA output by {gap:.2e} x height (not the same QA "
                                "frame: a changed yaw/level/override?)"}
    if abs(delta) < 0.01 and abs(sc - 1) < 1e-5:
        out["skipped"] = "no change"
        return out
    if not apply:
        return out
    bk = w / "backup"
    shutil.rmtree(bk, ignore_errors=True)
    bk.mkdir(parents=True)
    for f in GLBS + COPY + ("manifest.json", "paint/cameras.json"):
        if (d / f).exists():
            (bk / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(d / f, bk / f)
    out["files"] = [transform_glb(d / f, M) for f in GLBS if (d / f).exists()]
    for f in COPY:
        shutil.copy2(o / f, d / f)
    (d / "paint" / "cameras.json").unlink(missing_ok=True)
    man = forge.load_manifest(d)
    if job["yaw_override"] is not None:
        rep["yaw_override"] = job["yaw_override"]
    rep["repitch"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "delta_deg": out["delta_deg"],
                      "scale": out["scale"], "tool": "tools/repitch.py"}
    man["qa"] = rep
    man["tri_count"] = rep["tris"]
    forge.save_manifest(d, man)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="+")
    ap.add_argument("--apply", action="store_true", help="write into out/ (default: dry run in scratch)")
    ap.add_argument("--jobs", type=int, default=4, help="Blender QA jobs at once (max 8)")
    a = ap.parse_args()
    reg = forge.load_registry("srd-5.2.1")
    with ThreadPoolExecutor(max(1, min(8, a.jobs))) as ex:
        res = list(ex.map(lambda m: one(m, reg, a.apply), a.ids))
    for r in res:
        print(json.dumps({k: v for k, v in r.items() if k != "M"}))
    (SCRATCH / f"last_{'apply' if a.apply else 'dry'}.json").write_text(json.dumps(res, indent=1))
    return 1 if any("error" in r for r in res) else 0


if __name__ == "__main__":
    sys.exit(main())
