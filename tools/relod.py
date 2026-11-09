"""Rebuild a monster's rigged LODs (model_rigged_lod<k>.glb) from its current model_rigged.glb, without
re-rigging (tools/blender_lod_weights.py; docs/lods.md "Rigged LODs").

    python3 -I tools/relod.py check [ids...]          # which rigged LODs disagree with model_rigged.glb
    python3 -I tools/relod.py fix <ids...> [--dry-run]  # rebuild them (backs up the old files first)

check, per rigged LOD: (a) the skin's joints are model_rigged.glb's, in the same order (a glTF skin binds
by index, and engines put every LOD on LOD0's skin); (b) the weights: under a test pose (every bone
turned 25 deg about a fixed random axis) each LOD vertex is skinned with its own weights and with the
weights of the nearest LOD0 vertex whose normal agrees, at its own rest position; p95 of the difference
over p95 of LOD0's displacement must stay under 0.1 (+5 mm), the rule of godot-dnd check_monsters.gd.

fix: model_rigged.glb (armature + LOD0, the rest pose the clips were made on) stays; each LOD is the
static model_lod<k>.glb with LOD0's weights transferred normal-aware, or, when the rig's rest pose was
grounded (manifest rig.grounded: blender_rig.ground_feet turned chains, so the static LODs no longer
match LOD0), LOD0 decimated to the LOD's triangle count. Old files go to
~/.cache/monster-forge-scratch/relod/<id>/ first. Nothing else is touched (lods.json, model_lod*.glb,
clips, manifest).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ual_library as U  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out" / "srd-5.2.1"
SCRATCH = Path.home() / ".cache" / "monster-forge-scratch" / "relod"
BLENDER = os.environ.get("BLENDER", "blender")


# --- a skinned mesh in numpy ------------------------------------------------------------------------
def _qmat(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _axis_angle(a, ang):
    a = np.asarray(a, float) / np.linalg.norm(a)
    x, y, z = a
    c, s, t = math.cos(ang), math.sin(ang), 1 - math.cos(ang)
    return np.array([[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
                     [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
                     [t * x * z - s * y, t * y * z + s * x, t * z * z + c]])


class Skinned:
    def __init__(self, path: Path):
        j, b = U.read_glb(path)
        skin = j["skins"][0]
        self.joints = [j["nodes"][i]["name"] for i in skin["joints"]]
        par = {c: i for i, n in enumerate(j["nodes"]) for c in n.get("children", [])}
        nk = {ni: k for k, ni in enumerate(skin["joints"])}
        self.parent = [nk.get(par.get(ni, -1), -1) for ni in skin["joints"]]
        root = skin["joints"][[k for k, p in enumerate(self.parent) if p < 0][0]]
        top, i = np.eye(4), par.get(root)
        while i is not None:   # the nodes above the root bone (the Armature node)
            top = U.trs_matrix(j["nodes"][i]) @ top
            i = par.get(i)
        self.top = top
        self.rest = []
        for ni in skin["joints"]:
            n = j["nodes"][ni]
            m = np.eye(4)
            m[:3, :3] = _qmat(n.get("rotation", [0, 0, 0, 1])) * np.array(n.get("scale", [1, 1, 1]))[None]
            m[:3, 3] = n.get("translation", [0, 0, 0])
            self.rest.append(m)
        order, seen = [], set()

        def visit(k):
            if k in seen:
                return
            if self.parent[k] >= 0:
                visit(self.parent[k])
            seen.add(k)
            order.append(k)
        for k in range(len(self.joints)):
            visit(k)
        self.order = order
        mi = next(n for n in j["nodes"] if "mesh" in n and "skin" in n)
        P, J, W, N = [], [], [], []
        for pr in j["meshes"][mi["mesh"]]["primitives"]:
            a = pr["attributes"]
            P.append(U.accessor(j, b, a["POSITION"]))
            J.append(U.accessor(j, b, a["JOINTS_0"]).astype(int))
            W.append(U.accessor(j, b, a["WEIGHTS_0"]))
            N.append(U.accessor(j, b, a["NORMAL"]))
        self.pos, self.jt, self.w, self.nrm = (np.concatenate(x) for x in (P, J, W, N))
        self.w = self.w / np.maximum(self.w.sum(1, keepdims=True), 1e-9)
        self.ibm = U.accessor(j, b, skin["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1)

    def globals(self, turn: dict | None = None) -> np.ndarray:
        G = np.zeros((len(self.joints), 4, 4))
        for k in self.order:
            L = self.rest[k].copy()
            if turn and self.joints[k] in turn:
                L[:3, :3] = L[:3, :3] @ turn[self.joints[k]]
            G[k] = (self.top if self.parent[k] < 0 else G[self.parent[k]]) @ L
        return G

    def skin(self, G, pos=None, jt=None, w=None) -> np.ndarray:
        pos = self.pos if pos is None else pos
        jt = self.jt if jt is None else jt
        w = self.w if w is None else w
        JM = G @ self.ibm
        v = np.concatenate([pos, np.ones((len(pos), 1))], 1)
        out = np.zeros((len(pos), 3))
        for c in range(jt.shape[1]):
            out += w[:, c:c + 1] * np.einsum("vij,vj->vi", JM[jt[:, c]], v)[:, :3]
        return out

    def rest_normals(self, G) -> np.ndarray:
        JM = G @ self.ibm
        M = np.zeros((len(self.pos), 3, 3))
        for c in range(self.jt.shape[1]):
            M += self.w[:, c, None, None] * JM[self.jt[:, c], :3, :3]
        n = np.einsum("vij,vj->vi", M, self.nrm)
        return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)


def _nearest_agreeing(p0, n0, q, nq, k=12, cos=0.3) -> np.ndarray:
    n2 = (p0 ** 2).sum(1)
    out = np.empty(len(q), int)
    for a in range(0, len(q), 256):
        c = q[a:a + 256]
        d2 = (c ** 2).sum(1)[:, None] + n2[None] - 2 * c @ p0.T
        kk = min(k, d2.shape[1] - 1)
        idx = np.argpartition(d2, kk, axis=1)[:, :kk]
        dd = np.take_along_axis(d2, idx, 1)
        ok = np.einsum("vkj,vj->vk", n0[idx], nq[a:a + 256]) > cos
        out[a:a + 256] = idx[np.arange(len(c)), np.where(ok, dd, dd + 1e6).argmin(1)]
    return out


def node_order(path: Path) -> list[str]:
    """Joint names in glTF node order (what engines build the skeleton's bone order from)."""
    j, _ = U.read_glb(path)
    return [j["nodes"][i]["name"] for i in sorted(j["skins"][0]["joints"])]


def check(mid: str) -> dict:
    d = OUT / mid
    s0 = Skinned(d / "model_rigged.glb")
    rng = np.random.default_rng(0)
    turn = {n: _axis_angle(rng.normal(size=3), math.radians(25) * (1 if k % 2 else -1)) for k, n in enumerate(s0.joints)}
    R0, P0 = s0.globals(), s0.globals(turn)
    r0, p0, n0 = s0.skin(R0), s0.skin(P0), s0.rest_normals(R0)
    mag = float(np.percentile(np.linalg.norm(p0 - r0, axis=1), 95))
    res = {"id": mid, "lod0_disp_p95": round(mag, 4), "lods": {}, "ok": True}
    for f in sorted(d.glob("model_rigged_lod*.glb")):
        k = f.stem[-1]
        sk = Skinned(f)
        if sk.joints != s0.joints or node_order(f) != node_order(d / "model_rigged.glb"):
            res["lods"][k] = {"ok": False, "problem": f"skin joints or their node order differ from model_rigged.glb "
                                                      f"({len(sk.joints)} vs {len(s0.joints)} joints)"}
            res["ok"] = False
            continue
        Rk, Pk = sk.globals(), sk.globals(turn)
        rk, pk = sk.skin(Rk), sk.skin(Pk)
        nn = _nearest_agreeing(r0, n0, rk, sk.rest_normals(Rk))
        alt = s0.skin(P0, pos=sk.pos, jt=s0.jt[nn], w=s0.w[nn])
        err = np.linalg.norm(pk - alt, axis=1)
        p95 = float(np.percentile(err, 95))
        ok = p95 < 0.1 * mag + 0.005
        res["lods"][k] = {"ok": bool(ok), "weights_p95": round(p95, 4), "ratio": round(p95 / max(mag, 1e-9), 3)}
        res["ok"] &= bool(ok)
    return res


# --- fix --------------------------------------------------------------------------------------------
_CT = {5121: np.uint8, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}


def _accessor_view(j: dict, binc: bytearray, i: int):
    """(numpy view writable into binc, shape) of a tightly packed accessor."""
    a = j["accessors"][i]
    bv = j["bufferViews"][a["bufferView"]]
    n = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}[a["type"]]
    dt = np.dtype(_CT[a["componentType"]])
    if bv.get("byteStride", n * dt.itemsize) != n * dt.itemsize:
        raise ValueError("interleaved accessor")
    off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    return np.frombuffer(binc, dt, a["count"] * n, off).reshape(a["count"], n)


def match_node_order(path: Path, ref: Path) -> bool:
    """Put path's joint nodes in ref's node order: the node slots the joints hold are refilled in
    the order ref has them, and every children list follows that order. Engines build the skeleton's
    bone order from the glTF node layout, not only from skin.joints (godot-dnd check_monsters.gd:
    "LOD1: bone 2 is hind_l_upper, LOD0 has spine_01" after a re-imported armature exported its
    bones in another order). Returns True when it changed the file."""
    j, b = U.read_glb(path)
    jr, _ = U.read_glb(ref)
    rank = {jr["nodes"][i]["name"]: k for k, i in enumerate(sorted(jr["skins"][0]["joints"]))}
    jnodes = sorted(j["skins"][0]["joints"])
    want = sorted(jnodes, key=lambda i: rank[j["nodes"][i]["name"]])
    perm = {old: new for new, old in zip(jnodes, want)}     # old node index -> new index
    if all(o == n for o, n in perm.items()):
        same_children = all(n.get("children", []) == sorted(n.get("children", []), key=lambda c: rank.get(j["nodes"][c]["name"], -1))
                            for n in j["nodes"])
        if same_children:
            return False
    m = lambda i: perm.get(i, i)  # noqa: E731
    nodes = list(j["nodes"])
    for old, new in perm.items():
        nodes[new] = j["nodes"][old]
    for n in nodes:
        if "children" in n:
            n["children"] = sorted((m(c) for c in n["children"]),
                                   key=lambda c: (rank.get(nodes[c]["name"], -1), c))
    j["nodes"] = nodes
    for sc in j.get("scenes", []):
        sc["nodes"] = [m(i) for i in sc["nodes"]]
    for sk in j.get("skins", []):
        sk["joints"] = [m(i) for i in sk["joints"]]
        if "skeleton" in sk:
            sk["skeleton"] = m(sk["skeleton"])
    for an in j.get("animations", []):
        for c in an["channels"]:
            c["target"]["node"] = m(c["target"]["node"])
    U.write_glb(path, j, b)
    return True


def match_joint_order(path: Path, names: list[str]) -> bool:
    """Reorder path's skin joints to `names` (model_rigged.glb's order): skin.joints, the inverse bind
    matrices and every JOINTS_0 index. A re-imported armature can export its bones in another order,
    and engines bind each LOD to LOD0's skin by index. Returns True when it changed the file."""
    j, b = U.read_glb(path)
    skin = j["skins"][0]
    cur = [j["nodes"][i]["name"] for i in skin["joints"]]
    if cur == names:
        return False
    if sorted(cur) != sorted(names):
        raise SystemExit(f"{path}: joints {len(cur)} are not model_rigged.glb's {len(names)}")
    binc = bytearray(b)
    node_of = {j["nodes"][i]["name"]: i for i in skin["joints"]}
    old_index = {n: k for k, n in enumerate(cur)}
    remap = np.array([names.index(n) for n in cur])        # old joint index -> new
    ibm = _accessor_view(j, binc, skin["inverseBindMatrices"])
    ibm[:] = ibm[[old_index[n] for n in names]].copy()
    skin["joints"] = [node_of[n] for n in names]
    for m in j["meshes"]:
        for pr in m["primitives"]:
            if "JOINTS_0" in pr["attributes"]:
                jt = _accessor_view(j, binc, pr["attributes"]["JOINTS_0"])
                jt[:] = remap[jt.astype(np.int64)].astype(jt.dtype)
    U.write_glb(path, j, bytes(binc))
    return True


def fix(mid: str, dry: bool = False) -> dict:
    d = OUT / mid
    man = json.loads((d / "manifest.json").read_text())
    lods = json.loads((d / "lods.json").read_text())["lods"]
    grounded = bool((man.get("rig") or {}).get("grounded"))
    job = {"rigged": str(d / "model_rigged.glb"), "lods": {}, "decimate": {}, "out": {},
           "report": str(SCRATCH / mid / "relod.json")}
    for k, v in lods.items():
        if k == "0" or not v.get("file") or not (d / f"model_rigged_lod{k}.glb").exists():
            continue
        if grounded:
            job["decimate"][k] = int(v["tris"])
        else:
            job["lods"][k] = str(d / v["file"])
        job["out"][k] = str(SCRATCH / mid / "new" / f"model_rigged_lod{k}.glb")
    if dry:
        return {"id": mid, "job": job}
    (SCRATCH / mid / "new").mkdir(parents=True, exist_ok=True)
    (SCRATCH / mid / "old").mkdir(parents=True, exist_ok=True)
    jf = SCRATCH / mid / "job.json"
    jf.write_text(json.dumps(job))
    log = SCRATCH / mid / "blender.log"
    t = time.time()
    with open(log, "w") as fh:
        r = subprocess.run([BLENDER, "-b", "--factory-startup", "--python-exit-code", "1", "-P",
                            str(ROOT / "tools" / "blender_lod_weights.py"), "--", str(jf)], stdout=fh, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise SystemExit(f"{mid}: blender failed, log {log}")
    names = Skinned(d / "model_rigged.glb").joints
    reordered = [k for k in job["out"] if match_node_order(Path(job["out"][k]), d / "model_rigged.glb")
                 | match_joint_order(Path(job["out"][k]), names)]
    for k in job["out"]:
        dst = d / f"model_rigged_lod{k}.glb"
        if not (SCRATCH / mid / "old" / dst.name).exists():
            shutil.copy2(dst, SCRATCH / mid / "old" / dst.name)
        shutil.copyfile(job["out"][k], dst)
    rep = json.loads(Path(job["report"]).read_text())
    return {"id": mid, "seconds": round(time.time() - t, 1), "grounded": grounded,
            **({"joint_order_matched": reordered} if reordered else {}), **rep}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["check", "fix"])
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", help="check: write every result here")
    a = ap.parse_args()
    if a.cmd == "check":
        ids = a.ids or sorted(p.parent.name for p in OUT.glob("*/model_rigged_lod1.glb"))
        bad, rows = [], []
        for mid in ids:
            r = check(mid)
            rows.append(r)
            if not r["ok"]:
                bad.append(mid)
                print(mid, json.dumps(r["lods"]))
        if a.json:
            Path(a.json).write_text(json.dumps(rows, indent=1))
        print(f"checked {len(ids)}, failing {len(bad)}: {' '.join(bad)}")
        return 1 if bad else 0
    if not a.ids:
        raise SystemExit("fix: name the ids")
    for mid in a.ids:
        print(json.dumps(fix(mid, a.dry_run)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
