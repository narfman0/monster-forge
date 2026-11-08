"""Paired (synced) animations for UAL bipeds (docs/anim-reuse.md, "Paired animations").

    python3 -I tools/paired.py author grapple            # Blender (headless): bake the recipe, write the clips + sidecar
    python3 -I tools/paired.py check grapple tough bandit ogre:bandit   # numpy runtime model: contact distances
    python3 -I tools/paired.py sheet grapple tough:bandit ogre:bandit   # Blender contact sheets (side + front)

A paired animation is two (or more) clips that play together on two actors: an initiator and a receiver,
placed relative to each other (alignment), with contacts (a bone of one actor held on a bone of the other)
that IK keeps closed at runtime when the actors' proportions differ from the authored ones. Nothing in the
format knows what the clip means: grapple, shove, carry and a healing touch are all "actors, alignment,
contacts".

Output (out/<source>/_shared/paired/, next to _shared/ual/):
- <clip>.glb: one armature-only GLB per actor clip in the shared UAL library format (tools/ual_library.py
  write_clip: UAL1's skeleton at rest, rotation channels for the 64 non-root bones, the pelvis translation
  u in UAL units). A UAL biped plays it like a shared UAL clip: rotations as is, pelvis = hip_scale * u +
  pelvis_offset (manifest anim.ual_shared), every other bone at its own rest.
- library.json: the same index as _shared/ual/library.json ({"clips": {name: {file, frames, seconds,
  loop}}, ...}) so the same loaders read it, plus "paired": {entry: sidecar file}.
- <entry>.json: the sidecar (format below, FORMAT_DOC).

Authoring: RECIPES below is the source of every clip: per variant, the actors' placement and scale, a base
UAL clip (Idle_Loop), keyed per-bone rotation offsets (actor axes), waves (struggle sway), IK chains whose
targets are bones of the other actor (Blender IK constraints, solved every frame against the other actor's
evaluated pose), and planted feet (leg IK). tools/blender_paired.py evaluates it on two copies of UAL1 in
Blender and dumps every bone's armature-space matrix per frame; this script converts those to UAL1's own
glTF bone frames (constant per-bone correction from the rest pose) and writes the clips.

Runtime model (check, sheet; the same as godot-dnd scripts/paired_anim.gd): each actor's skeleton posed
from the clip (rotations, pelvis s*u + c, other bones at rest), the receiver placed at the alignment
(offset scaled by the actors' hip_scales), then per contact a two-bone IK on the chain named by the contact
moves the "from" point (e.g. the palm) onto the "to" point (e.g. the top of the other actor's shoulder),
keeping the bend plane of the animated limb and the end bone's animated local rotation.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ual_library as U  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "srd-5.2.1"
OUT = ROOT / "out" / SOURCE
LIB_REL = "_shared/paired"
SCRATCH = Path.home() / ".cache" / "monster-forge-scratch" / "paired"
BLENDER = os.environ.get("BLENDER", "blender")
FORMAT = "monster-forge paired v1"
FPS = 30
R180 = np.diag([-1.0, 1.0, -1.0, 1.0])  # the monsters' Armature node: UAL faces +Z, our creatures face -Z

FORMAT_DOC = (
    "Sidecar of a paired animation (docs/anim-reuse.md 'Paired animations'). Roles are names; nothing here "
    "knows what the clip means. Lengths are UAL metres (hip_scale 1): multiply by the owning actor's "
    "hip_scale. Spaces: 'model' = the monster node (metres, +Y up, faces -Z, right +X); bone offsets are in "
    "the glTF bone frame of the named bone (same on every UAL biped). Each variant: ratio = [min, max] of "
    "receiver hip_scale / initiator hip_scale it was authored for; clips {role: clip in library.json}; "
    "alignment: the receiver's root in the initiator's model space at frame 0 (offset, yaw_deg about +Y), "
    "offset scaled by sum(scale_weights[role] * hip_scale[role] / authored_scale[role]); warp_frames: frames "
    "to slide the receiver from where it stands onto the alignment (0 = snap); contacts: 'from' point of one "
    "role held on the 'to' point of the other ({role, bone, frame_bone (orientation; default bone), offset}), "
    "full weight over frames [a, b], IK weight ramping in over blend[0] frames before a and out over blend[1] "
    "after b; ik: {role, chain [root, middle, end]}: the chain that moves (its end bone keeps its animated "
    "local rotation); tolerance: metres x mean hip_scale; sync_frame: the frame the contact lands (start "
    "both clips together; the receiver's reaction is keyed here); exit {role: {state, frame}}: the clip each "
    "actor returns to after the pair (cross-fade from that frame). alignment.fit: the runtime scales the "
    "offset so every contact at full weight on fit.frame needs reach (|wrist target - chain root| / chain "
    "length) = the contact's authored 'reach', within range x the scale_weights rule, and never closer than "
    "the two torso fronts (fit.depth) + min_gap x mean hip_scale. lean {bones, max_deg, reach[frame]}: "
    "before the IK, bend these bones (equal shares, forward toward the targets) by the smallest angle that "
    "brings the chain back to the authored reach of that frame.")

# --------------------------------------------------------------------------------------------------
# Recipes: the source of every paired clip (tools/blender_paired.py evaluates them).
#
# Actor axes (Blender armature space of UAL1: the actor faces -Y, +Z up, its left is +X):
#   rotation keys [pitch, yaw, roll] degrees: pitch about +X (+ = bend forward), yaw about +Z (+ = turn to
#   its left), roll about +Y (+ = lean to its left). Applied at the bone in its parent's rest frame on top
#   of the base clip (B' = Rest^-1 R Rest @ B).
#   "pelvis@loc" keys [x, y, z] metres in actor axes (+y = backwards, -z = down).
# Keys interpolate Catmull-Rom; values before the first / after the last key hold.
# waves: {bone: [{"axis": "pitch|yaw|roll", "amp": deg, "period": frames, "phase": 0..1, "env": keys}]}.
# ik: {"name", "actor", "chain": [root, mid, end], "from": {offset in the end bone's frame},
#      "to": {"actor", "bone", "frame_bone", "offset"}, "influence": keys, "pole": [x, y, z] actor axes}.
# Contacts (the sidecar) are taken from the ik list: full contact where influence == 1.
# --------------------------------------------------------------------------------------------------


def _grapple(variant: str) -> dict:
    big = variant == "big"
    rs = 0.55 if big else 1.0          # receiver scale it is authored at (initiator 1)
    d = 0.34 if big else 0.43          # receiver root ahead of the initiator's (UAL m, initiator scale 1)
    n, sync = 150, 18
    rel = [[0, 0], [4, 0], [sync - 1, 1], [112, 1], [126, 0]]
    rel_r = [[0, 0], [22, 0], [32, 1], [104, 1], [116, 0]]
    stoop = 2.2 if big else 1.0
    I = {
        "spine_01": [[0, [0, 0, 0]], [6, [0, 0, 0]], [16, [8 * stoop, 0, 0]], [22, [10 * stoop, 0, 0]],
                     [60, [9 * stoop, 0, 0]], [112, [9 * stoop, 0, 0]], [128, [2, 0, 0]], [150, [0, 0, 0]]],
        "spine_02": [[0, [0, 0, 0]], [6, [0, 0, 0]], [16, [5 * stoop, 0, 0]], [22, [7 * stoop, 0, 0]],
                     [112, [6 * stoop, 0, 0]], [128, [1, 0, 0]], [150, [0, 0, 0]]],
        "spine_03": [[0, [0, 0, 0]], [6, [0, 0, 0]], [16, [3 * stoop, 0, 0]], [112, [3 * stoop, 0, 0]],
                     [128, [0, 0, 0]], [150, [0, 0, 0]]],
        "Head": [[0, [0, 0, 0]], [6, [0, 0, 0]], [16, [-6 * stoop, 0, 0]], [112, [-6 * stoop, 0, 0]],
                 [128, [0, 0, 0]], [150, [0, 0, 0]]],
        "pelvis@loc": [[0, [0, 0, 0]], [6, [0, 0, 0]], [18, [0, 0.04 * stoop, -0.03 - 0.1 * big]],
                       [112, [0, 0.04 * stoop, -0.03 - 0.1 * big]], [130, [0, 0.01, 0]], [150, [0, 0, 0]]],
    }
    for s in "lr":   # arms forward (FK), so the IK blend in and out swings instead of sliding
        I[f"upperarm_{s}"] = [[0, [0, 0, 0]], [4, [0, 0, 0]], [14, [-50 + 25 * big, 0, 0]],
                              [112, [-50 + 25 * big, 0, 0]], [132, [0, 0, 0]]]
        I[f"lowerarm_{s}"] = [[0, [0, 0, 0]], [4, [0, 0, 0]], [14, [-35, 0, 0]], [112, [-35, 0, 0]], [132, [0, 0, 0]]]
    R = {
        # the grab lands at the sync frame: the receiver jolts back, then struggles, staggers when released
        "spine_01": [[0, [0, 0, 0]], [sync, [0, 0, 0]], [22, [-8, 0, 0]], [30, [-3, 0, 0]], [112, [-3, 0, 0]],
                     [120, [-7, 0, 0]], [138, [0, 0, 0]]],
        "spine_02": [[0, [0, 0, 0]], [sync, [0, 0, 0]], [22, [-6, 0, 0]], [30, [-2, 0, 0]], [112, [-2, 0, 0]],
                     [120, [-5, 0, 0]], [138, [0, 0, 0]]],
        "Head": [[0, [0, 0, 0]], [sync, [0, 0, 0]], [21, [-14, 0, 0]], [30, [-6 - 10 * big, 0, 0]],
                 [112, [-6 - 10 * big, 0, 0]], [120, [-8, 0, 0]], [140, [0, 0, 0]]],
        "pelvis@loc": [[0, [0, 0, 0]], [sync, [0, 0, 0]], [22, [0, 0.03, 0]], [30, [0, 0.01, -0.015]],
                       [112, [0, 0.01, -0.015]], [122, [0, 0.04, 0]], [142, [0, 0, 0]]],
    }
    for s in "lr":
        up = -75 if big else -50
        R[f"upperarm_{s}"] = [[0, [0, 0, 0]], [22, [0, 0, 0]], [30, [up, 0, 0]], [106, [up, 0, 0]], [124, [0, 0, 0]]]
        R[f"lowerarm_{s}"] = [[0, [0, 0, 0]], [22, [0, 0, 0]], [30, [-55, 0, 0]], [106, [-55, 0, 0]], [124, [0, 0, 0]]]
    env = [[26, 0], [36, 1], [100, 1], [112, 0]]
    waves = {
        "initiator": {"spine_02": [{"axis": "yaw", "amp": 4, "period": 26, "phase": 0.55, "env": env}],
                      "spine_01": [{"axis": "pitch", "amp": 2, "period": 13, "phase": 0.0, "env": env}]},
        "receiver": {"pelvis": [{"axis": "yaw", "amp": 5, "period": 26, "phase": 0.0, "env": env}],
                     "spine_01": [{"axis": "yaw", "amp": 6, "period": 26, "phase": 0.05, "env": env}],
                     "spine_02": [{"axis": "yaw", "amp": 5, "period": 26, "phase": 0.1, "env": env},
                                  {"axis": "roll", "amp": 4, "period": 13, "phase": 0.0, "env": env}],
                     "Head": [{"axis": "yaw", "amp": 14, "period": 26, "phase": 0.3, "env": env}]},
    }
    ik = []
    for s, o in (("l", "r"), ("r", "l")):
        # the initiator's left hand lands on the receiver's right shoulder (they face each other)
        ik.append({"name": f"initiator_hand_{s}_on_receiver_shoulder_{o}", "actor": "initiator",
                   "chain": [f"upperarm_{s}", f"lowerarm_{s}", f"hand_{s}"],
                   "from": {"bone": f"hand_{s}", "offset": "palm"},
                   "to": {"actor": "receiver", "bone": f"upperarm_{o}", "frame_bone": f"clavicle_{o}",
                          "offset": "shoulder_top"},
                   "influence": rel, "pole": [1.0 if s == "l" else -1.0, 0.3, -1.0], "tolerance": 0.03,
                   "lean": {"bones": ["spine_01", "spine_02", "spine_03"], "max_deg": 40}})
    for s, o in (("r", "l"), ("l", "r")):
        # the receiver's hands grab the initiator's forearms (the arm on the same side of the gap)
        ik.append({"name": f"receiver_hand_{s}_on_initiator_forearm_{o}", "actor": "receiver",
                   "chain": [f"upperarm_{s}", f"lowerarm_{s}", f"hand_{s}"],
                   "from": {"bone": f"hand_{s}", "offset": "palm"},
                   "to": {"actor": "initiator", "bone": f"lowerarm_{o}", "offset": "forearm"},
                   "influence": rel_r, "pole": [1.0 if s == "l" else -1.0, 0.5, -1.0], "tolerance": 0.05})
    return {
        "entry": "grapple", "variant": "grapple_big" if big else "grapple",
        "ratio": [0.42, 0.75] if big else [0.75, 1.34],
        "frames": n, "sync_frame": sync,
        "actors": {"initiator": {"scale": 1.0, "base": "Idle_Loop", "keys": I, "plant_feet": True},
                   "receiver": {"scale": rs, "base": "Idle_Loop", "keys": R, "plant_feet": True, "phase": 37}},
        "alignment": {"offset": d, "yaw_deg": 180.0, "scale_weights": {"initiator": 0.5, "receiver": 0.5},
                      "warp_frames": 4},
        "waves": waves, "ik": ik,
        "exit": {"initiator": {"state": "idle", "frame": n}, "receiver": {"state": "idle", "frame": n}},
    }


RECIPES = {"grapple": lambda: [_grapple("same"), _grapple("big")]}


# --------------------------------------------------------------------------------------------------
# Skeletons and FK (glTF conventions; matrices are 4x4 numpy, column vectors)
# --------------------------------------------------------------------------------------------------
def qmat(q) -> np.ndarray:
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def matq(m: np.ndarray) -> np.ndarray:
    """3x3 rotation -> quaternion (x, y, z, w), w >= 0."""
    m = np.asarray(m, float)
    t = np.trace(m)
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        q = [(m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, 0.25 * s]
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        q = [0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s]
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        q = [(m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s]
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        q = [(m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s, (m[1, 0] - m[0, 1]) / s]
    q = np.array(q)
    q /= np.linalg.norm(q)
    return -q if q[3] < 0 else q


def axis_angle(axis, ang: float) -> np.ndarray:
    a = np.asarray(axis, float)
    a = a / (np.linalg.norm(a) or 1.0)
    x, y, z = a
    c, s, t = math.cos(ang), math.sin(ang), 1 - math.cos(ang)
    return np.array([[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
                     [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
                     [t * x * z - s * y, t * y * z + s * x, t * z * z + c]])


def rot_between(a, b) -> np.ndarray:
    a = np.asarray(a, float) / (np.linalg.norm(a) or 1.0)
    b = np.asarray(b, float) / (np.linalg.norm(b) or 1.0)
    v = np.cross(a, b)
    c = float(np.clip(a @ b, -1, 1))
    if np.linalg.norm(v) < 1e-9:
        if c > 0:
            return np.eye(3)
        p = np.cross(a, [1, 0, 0]) if abs(a[0]) < 0.9 else np.cross(a, [0, 1, 0])
        return axis_angle(p, math.pi)
    return axis_angle(v, math.atan2(np.linalg.norm(v), c))


class Skel:
    """A glTF skeleton: bones in skin order, parents, rest local TRS; `top` = the matrix above the root
    bone (the monsters' Armature node: 180 deg about Y; UAL1: identity, so we use R180 too and every
    skeleton lives in the monsters' model space)."""

    def __init__(self, j: dict, top: np.ndarray, binc: bytes | None = None, mesh: bool = False):
        skin = j["skins"][0]
        self.j, self.binc = j, binc
        self.joints = list(skin["joints"])
        self.names = [j["nodes"][i]["name"] for i in self.joints]
        self.index = {n: k for k, n in enumerate(self.names)}
        par = {}
        for i, n in enumerate(j["nodes"]):
            for c in n.get("children", []):
                par[c] = i
        node_to_k = {ni: k for k, ni in enumerate(self.joints)}
        self.parent = [node_to_k.get(par.get(ni, -1), -1) for ni in self.joints]
        self.rest_t = np.array([j["nodes"][ni].get("translation", [0, 0, 0]) for ni in self.joints], float)
        self.rest_q = np.array([j["nodes"][ni].get("rotation", [0, 0, 0, 1]) for ni in self.joints], float)
        self.rest_s = np.array([j["nodes"][ni].get("scale", [1, 1, 1]) for ni in self.joints], float)
        self.top = top
        # topological order (parents first)
        order, seen = [], set()

        def visit(k):
            if k in seen:
                return
            if self.parent[k] >= 0:
                visit(self.parent[k])
            seen.add(k)
            order.append(k)
        for k in range(len(self.names)):
            visit(k)
        self.order = order
        self.mesh = self._mesh() if mesh else None

    def local(self, rot: dict | None = None, pelvis_t=None) -> np.ndarray:
        """(n, 4, 4) local matrices: rest, with rot {bone: quat} and the pelvis translation replaced."""
        L = np.zeros((len(self.names), 4, 4))
        for k in range(len(self.names)):
            q = self.rest_q[k]
            if rot is not None and self.names[k] in rot:
                q = rot[self.names[k]]
            L[k, :3, :3] = qmat(q) * self.rest_s[k][None, :]
            L[k, :3, 3] = self.rest_t[k]
            L[k, 3, 3] = 1
        if pelvis_t is not None:
            L[self.index["pelvis"], :3, 3] = pelvis_t
        return L

    def fk(self, L: np.ndarray, world: np.ndarray | None = None) -> np.ndarray:
        G = np.zeros_like(L)
        base = self.top if world is None else world @ self.top
        for k in self.order:
            p = self.parent[k]
            G[k] = (base if p < 0 else G[p]) @ L[k]
        return G

    def _mesh(self) -> dict:
        """LOD0 skinned mesh: positions, joints (skin indices), weights, triangles, inverse binds."""
        j, b = self.j, self.binc
        mi = next(i for i, n in enumerate(j["nodes"]) if "mesh" in n and "skin" in n)
        prims = j["meshes"][j["nodes"][mi]["mesh"]]["primitives"]
        P, J, W, T = [], [], [], []
        off = 0
        for pr in prims:
            a = pr["attributes"]
            P.append(U.accessor(j, b, a["POSITION"]))
            J.append(U.accessor(j, b, a["JOINTS_0"]).astype(int))
            W.append(U.accessor(j, b, a["WEIGHTS_0"]))
            T.append(U.accessor(j, b, pr["indices"]).astype(int).reshape(-1, 3) + off)
            off += len(P[-1])
        ibm = U.accessor(j, b, j["skins"][0]["inverseBindMatrices"]).reshape(-1, 4, 4).transpose(0, 2, 1)
        return {"pos": np.concatenate(P), "joints": np.concatenate(J), "weights": np.concatenate(W),
                "tris": np.concatenate(T), "ibm": ibm}

    def skin(self, G: np.ndarray) -> np.ndarray:
        m = self.mesh
        JM = G @ m["ibm"]                       # (n, 4, 4)
        v = np.concatenate([m["pos"], np.ones((len(m["pos"]), 1))], 1)
        out = np.zeros((len(v), 3))
        w = m["weights"] / np.maximum(m["weights"].sum(1, keepdims=True), 1e-9)
        for c in range(m["joints"].shape[1]):
            M = JM[m["joints"][:, c]]           # (v, 4, 4)
            out += w[:, c:c + 1] * np.einsum("vij,vj->vi", M, v)[:, :3]
        return out


_UAL: Skel | None = None


def ual_skel() -> Skel:
    global _UAL
    if _UAL is None:
        pk = U.packs()[0]
        _UAL = Skel(pk["j"], R180)
    return _UAL


TORSO_BONES = ("spine_01", "spine_02", "spine_03")


def torso_front(j: dict, b: bytes, flip: bool) -> float:
    """How far the torso reaches forward of the root at rest (metres, along the creature's facing):
    the furthest vertex whose largest skin weight is on a TORSO_BONES bone (belly, chest). Mesh space;
    flip: the mesh faces +Z (UAL1) rather than -Z (ours)."""
    mi = next(n for n in j["nodes"] if "mesh" in n and "skin" in n)
    joints = [j["nodes"][k]["name"] for k in j["skins"][mi["skin"]]["joints"]]
    torso = {k for k, n in enumerate(joints) if n in TORSO_BONES}
    best = 0.0
    for pr in j["meshes"][mi["mesh"]]["primitives"]:
        a = pr["attributes"]
        P = U.accessor(j, b, a["POSITION"])
        J = U.accessor(j, b, a["JOINTS_0"]).astype(int)
        W = U.accessor(j, b, a["WEIGHTS_0"])
        dom = J[np.arange(len(J)), W.argmax(1)]
        sel = np.isin(dom, list(torso))
        if sel.any():
            z = P[sel, 2]
            best = max(best, float(z.max() if flip else -z.min()))
    return best


class Actor:
    """A monster (or UAL1 itself, mid "ual") as the runtime poses it from library-format clips."""

    def __init__(self, mid: str, mesh: bool = False):
        self.id = mid
        if mid.split("@")[0] == "ual":   # UAL1 itself ("ual@0.55": at hip_scale 0.55): the authoring check
            pk = U.packs()[0]
            self.skel = Skel(pk["j"], R180, pk["bin"], mesh=mesh) if mesh else ual_skel()
            self.s = float(mid.split("@")[1]) if "@" in mid else 1.0
            self.c = np.zeros(3)
            self.scale_m = 1.8 * self.s
            self.depth = torso_front(pk["j"], pk["bin"], True) * self.s
            if self.s != 1.0:   # scale the whole skeleton (rest offsets), like Blender's object scale
                sk = Skel(pk["j"], R180 @ np.diag([self.s, self.s, self.s, 1.0]), pk["bin"], mesh=mesh)
                self.skel = sk
                self.c = np.zeros(3)
                self._unit = True
            return
        d = OUT / mid
        man = json.loads((d / "manifest.json").read_text())
        sh = (man.get("anim") or {}).get("ual_shared") or {}
        if (man.get("rig") or {}).get("tool") != "ual" or not sh.get("hip_scale"):
            raise SystemExit(f"{mid}: not a UAL biped with anim.ual_shared (hip_scale, pelvis_offset)")
        j, b = U.read_glb(d / "model_rigged.glb")
        arm = next(n for n in j["nodes"] if n.get("name") == "Armature")
        self.skel = Skel(j, U.trs_matrix(arm), b, mesh=mesh)
        self.s = float(sh["hip_scale"])
        self.c = np.array(sh["pelvis_offset"], float)
        self.scale_m = float(man.get("scale_m") or 1.8)
        self.depth = torso_front(j, b, False)

    def local(self, clip: dict, f: int) -> np.ndarray:
        rot = {bn: clip["rot"][bn][f] for bn in clip["rot"] if bn != "root" and bn in self.skel.index}
        if getattr(self, "_unit", False):   # scaled via the top matrix: pelvis in UAL units
            return self.skel.local(rot, clip["pelvis"][f])
        return self.skel.local(rot, self.s * clip["pelvis"][f] + self.c)

    def point(self, G: np.ndarray, spec: dict, offsets: dict) -> np.ndarray:
        """A contact point: the bone's origin + the frame bone's orientation x offset x hip_scale."""
        k = self.skel.index[spec["bone"]]
        fb = self.skel.index[spec.get("frame_bone", spec["bone"])]
        off = np.asarray(offsets[spec["offset"]] if isinstance(spec["offset"], str) else spec["offset"], float)
        rot = G[fb, :3, :3] / np.linalg.norm(G[fb, :3, :3], axis=0, keepdims=True)
        return G[k, :3, 3] + rot @ off * self.s


def two_bone_ik(sk: Skel, L: np.ndarray, world: np.ndarray, chain: list[str], tip_local: np.ndarray,
                target: np.ndarray, weight: float = 1.0, iters: int = 3) -> np.ndarray:
    """Analytic two-bone IK (godot-dnd paired_anim.gd does the same): rotate the chain's root and middle
    bones so the end bone's point tip_local (end-bone frame, metres) reaches target (world), keeping the
    animated bend plane and the end bone's local rotation. weight blends the rotations (slerp from the
    animated pose). Returns new local matrices."""
    a_k, b_k, c_k = (sk.index[n] for n in chain)
    L = L.copy()
    for _ in range(iters):
        G = sk.fk(L, world)
        a, b, c = G[a_k, :3, 3], G[b_k, :3, 3], G[c_k, :3, 3]
        tip = (G[c_k] @ np.append(tip_local, 1))[:3]
        t = target - (tip - c)                       # where the end bone's origin must go
        lab, lcb = np.linalg.norm(b - a), np.linalg.norm(c - b)
        lat = float(np.clip(np.linalg.norm(t - a), 1e-4, (lab + lcb) * 0.999))
        ac_ab0 = math.acos(np.clip((c - a) @ (b - a) / (np.linalg.norm(c - a) * lab + 1e-12), -1, 1))
        ba_bc0 = math.acos(np.clip((a - b) @ (c - b) / (lab * lcb + 1e-12), -1, 1))
        ac_ab1 = math.acos(np.clip((lcb * lcb - lab * lab - lat * lat) / (-2 * lab * lat), -1, 1))
        ba_bc1 = math.acos(np.clip((lat * lat - lab * lab - lcb * lcb) / (-2 * lab * lcb), -1, 1))
        axis = np.cross(c - a, b - a)
        if np.linalg.norm(axis) < 1e-6:
            axis = np.cross(t - a, [0, 1, 0])
        r0 = axis_angle(axis, ac_ab1 - ac_ab0)
        r1 = axis_angle(axis, ba_bc1 - ba_bc0)
        c1 = a + r0 @ (b - a) + r0 @ r1 @ (c - b)   # end after r0 at a and r1 at b
        r2 = rot_between(c1 - a, t - a)
        Ra, Rb = r2 @ r0, r2 @ r0 @ r1               # new global rotation = R x old (about the joint)
        if weight < 1.0:
            Ra, Rb = _slerp_m(np.eye(3), Ra, weight), _slerp_m(np.eye(3), Rb, weight)
        pa = G[sk.parent[a_k]] if sk.parent[a_k] >= 0 else world @ sk.top
        newA = Ra @ G[a_k, :3, :3]
        L[a_k, :3, :3] = np.linalg.inv(pa[:3, :3]) @ newA
        newB = Rb @ G[b_k, :3, :3]
        L[b_k, :3, :3] = np.linalg.inv(newA) @ newB
    return L


def _slerp_m(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    r = a.T @ b
    q = matq(r)
    ang = 2 * math.acos(min(1.0, abs(q[3])))
    if ang < 1e-9:
        return a
    ax = q[:3] / (np.linalg.norm(q[:3]) or 1)
    return a @ axis_angle(ax, ang * t)


# --------------------------------------------------------------------------------------------------
# Contact offsets from UAL1's rest (glTF bone frames)
# --------------------------------------------------------------------------------------------------
def _offsets() -> dict:
    """{"palm_l", "palm_r", "shoulder_top_l", ..., "forearm_l", ...}: offset vectors in the named bone's
    frame (UAL m). Palm: 60% from the wrist to middle_01, 2 cm to the palm side (UAL's T-pose palms face
    down). Shoulder top: 7 cm above the shoulder joint, 2.5 cm in, in the clavicle's frame. Forearm: 65%
    along the forearm towards the wrist, 3.5 cm up (the back of the forearm)."""
    sk = ual_skel()
    G = sk.fk(sk.local())
    ix = sk.index
    pos = lambda b: G[ix[b], :3, 3]   # noqa: E731
    out = {}
    up = (sk.top[:3, :3] @ np.array([0, 1.0, 0]))
    for s in "lr":
        R = lambda b: G[ix[b], :3, :3]  # noqa: E731
        p = pos(f"hand_{s}") + 0.6 * (pos(f"middle_01_{s}") - pos(f"hand_{s}")) - 0.02 * up
        out[f"palm_{s}"] = R(f"hand_{s}").T @ (p - pos(f"hand_{s}"))
        inward = pos("neck_01") - pos(f"upperarm_{s}")
        inward[1] = 0
        inward /= np.linalg.norm(inward)
        p = pos(f"upperarm_{s}") + 0.07 * up + 0.025 * inward
        out[f"shoulder_top_{s}"] = R(f"clavicle_{s}").T @ (p - pos(f"upperarm_{s}"))
        p = pos(f"lowerarm_{s}") + 0.65 * (pos(f"hand_{s}") - pos(f"lowerarm_{s}")) + 0.035 * up
        out[f"forearm_{s}"] = R(f"lowerarm_{s}").T @ (p - pos(f"lowerarm_{s}"))
    return {k: [round(float(x), 5) for x in v] for k, v in out.items()}


def _resolve(spec: dict, offs: dict) -> list:
    o = spec["offset"]
    if isinstance(o, str):
        return offs[f"{o}_{spec['bone'][-1]}"]
    return o


# --------------------------------------------------------------------------------------------------
# author: Blender bake -> library clips + sidecar
# --------------------------------------------------------------------------------------------------
def ual_rest_globals_gltf() -> dict:
    """UAL1 bone globals at rest in UAL1's own glTF space (no R180): what the clip frames are."""
    sk = ual_skel()
    G = sk.fk(sk.local(), np.linalg.inv(R180))   # world @ top = identity
    return {n: G[k].tolist() for k, n in enumerate(sk.names)}


def run_blender(script: str, job: dict, log: Path) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    jf = log.with_suffix(".job.json")
    jf.write_text(json.dumps(job))
    cmd = [BLENDER, "-b", "--factory-startup", "--python-exit-code", "1", "-P", str(ROOT / "tools" / script),
           "--", str(jf)]
    t = time.time()
    with open(log, "w") as fh:
        r = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise SystemExit(f"blender {script} failed ({r.returncode}), log {log}")
    print(f"  blender {script} {time.time() - t:.1f}s (log {log})")


def to_clip(arr: np.ndarray, conv: dict) -> dict:
    """Blender armature-space bone matrices (frames, bones, 4, 4) -> library clip {rot, pelvis, times}.
    G_gltf = Cv^-1 . M . C_b with C_b = (Cv^-1 . M_rest)^-1 . G_rest_gltf (per bone, constant)."""
    sk = ual_skel()
    names = conv["bones"]
    cv_inv = np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1.0]])  # Blender Z-up -> glTF Y-up
    rest_b = np.array(conv["rest"])           # Blender bone.matrix_local per bone
    rest_g = ual_rest_globals_gltf()
    Cb = [np.linalg.inv(cv_inv @ rest_b[k]) @ np.array(rest_g[n]) for k, n in enumerate(names)]
    n_f = arr.shape[0]
    rot = {n: np.zeros((n_f, 4)) for n in sk.names}
    pel = np.zeros((n_f, 3))
    bk = {n: k for k, n in enumerate(names)}
    for f in range(n_f):
        G = {n: cv_inv @ arr[f, bk[n]] @ Cb[bk[n]] for n in names}
        for k, n in enumerate(sk.names):
            p = sk.parent[k]
            Lm = G[n] if p < 0 else np.linalg.inv(G[sk.names[p]]) @ G[n]
            rot[n][f] = matq(Lm[:3, :3] / np.linalg.norm(Lm[:3, :3], axis=0, keepdims=True))
            if n == "pelvis":
                pel[f] = Lm[:3, 3]
    for n in rot:   # sign continuity
        q = rot[n]
        for i in range(1, len(q)):
            if np.dot(q[i], q[i - 1]) < 0:
                q[i] = -q[i]
    return {"times": np.arange(n_f) / FPS, "frames": n_f - 1, "seconds": round((n_f - 1) / FPS, 3),
            "rot": rot, "pelvis": pel}


def _clip_from(cn: dict) -> dict:
    """A to_clip() result as load_entry() returns clips (float32 round trip like the GLB)."""
    return {"rot": {b: q.astype(np.float32).astype(float) for b, q in cn["rot"].items()},
            "pelvis": cn["pelvis"].astype(np.float32).astype(float)}


def author(entry: str, keep_blend: bool = False) -> dict:
    recipes = RECIPES[entry]()
    offs = _offsets()
    work = SCRATCH / "author"
    work.mkdir(parents=True, exist_ok=True)
    lib_dir = OUT / LIB_REL
    lib_dir.mkdir(parents=True, exist_ok=True)
    skel, bones = U.ual_skeleton()
    side = {"entry": entry, "format": FORMAT, "doc": FORMAT_DOC, "skeleton": "ual", "fps": FPS,
            "roles": ["initiator", "receiver"], "requires": "manifest anim.ual_shared (hip_scale, pelvis_offset)",
            "source": "tools/paired.py RECIPES (tools/blender_paired.py bakes them)", "variants": []}
    clips_out = {}
    for rc in recipes:
        ik = []
        for c in rc["ik"]:
            c = json.loads(json.dumps(c))
            c["from"]["offset"] = _resolve(c["from"], offs)
            c["to"]["offset"] = _resolve(c["to"], offs)
            ik.append(c)
        job = {"mode": "author", "ual": U.packs()[0]["path"], "recipe": {**rc, "ik": ik},
               "rest_gltf": ual_rest_globals_gltf(), "out": str(work / f"{rc['variant']}.npz"),
               "blend": str(work / f"{rc['variant']}.blend") if keep_blend else ""}
        run_blender("blender_paired.py", job, work / f"{rc['variant']}.log")
        z = np.load(job["out"], allow_pickle=False)
        conv = {"bones": [str(x) for x in z["bones"]], "rest": z["rest"]}
        rep = json.loads(str(z["report"]))
        cn_all = {}
        for role in ("initiator", "receiver"):
            cn = to_clip(z[role], conv)
            cn_all[f"{rc['variant']}_{role}"] = cn
            name = f"{rc['variant']}_{role}"
            U.write_clip(lib_dir / f"{name}.glb", name, cn, skel, bones)
            clips_out[name] = {"file": f"{name}.glb", "frames": cn["frames"], "seconds": cn["seconds"], "loop": False,
                               "paired": entry, "role": role}
        al = rc["alignment"]
        # alignment in the initiator's model space (faces -Z): the receiver d ahead, turned to face it
        variant = {
            "name": rc["variant"], "ratio": rc["ratio"], "ratio_of": "receiver hip_scale / initiator hip_scale",
            "authored_scale": {r: a["scale"] for r, a in rc["actors"].items()},
            "clips": {r: f"{rc['variant']}_{r}" for r in rc["actors"]},
            "frames": rc["frames"], "seconds": round(rc["frames"] / FPS, 3), "sync_frame": rc["sync_frame"],
            "alignment": {"role": "receiver", "relative_to": "initiator", "offset": [0.0, 0.0, -al["offset"]],
                          "yaw_deg": al["yaw_deg"], "scale_weights": al["scale_weights"],
                          "warp_frames": al["warp_frames"]},
            "contacts": [], "exit": rc["exit"], "bake": rep}
        for c in ik:
            infl = c["influence"]
            full = [k[0] for k in infl if k[1] >= 1.0]
            a, b = min(full), max(full)
            zero_before = max([k[0] for k in infl if k[1] <= 0 and k[0] < a] or [a])
            zero_after = min([k[0] for k in infl if k[1] <= 0 and k[0] > b] or [b])
            variant["contacts"].append({
                "name": c["name"],
                "from": {"role": c["actor"], "bone": c["from"]["bone"], "offset": c["from"]["offset"]},
                "to": {"role": c["to"]["actor"], "bone": c["to"]["bone"],
                       **({"frame_bone": c["to"]["frame_bone"]} if c["to"].get("frame_bone") else {}),
                       "offset": c["to"]["offset"]},
                "frames": [a, b], "blend": [a - zero_before, zero_after - b],
                "ik": {"role": c["actor"], "chain": c["chain"]}, "tolerance": c["tolerance"],
                **({"lean": c["lean"]} if c.get("lean") else {})})
        # alignment.fit: the authored reach of each contact at full weight on the sync frame (UAL actors at
        # the authored scales), so the runtime can place differently proportioned actors the same way
        ual_act = {r: Actor(f"ual@{a['scale']}") for r, a in rc["actors"].items()}
        variant["alignment"]["fit"] = {
            "frame": rc["sync_frame"], "range": [0.6, 1.4],
            "min_gap": al.get("min_gap", 0.0),
            "depth": "torso front: furthest rest vertex ahead of the root whose largest skin weight is on "
                     "spine_01/02/03 (LOD0), metres"}
        k_auth = sum(w * 1.0 for w in al["scale_weights"].values())
        for c, r in reach_ratios(variant, ual_act, {n: _clip_from(cn_all[n]) for n in cn_all}, k_auth,
                                 rc["sync_frame"]).items():
            next(x for x in variant["contacts"] if x["name"] == c)["reach"] = round(r, 4)
        clips_u = {n: _clip_from(cn_all[n]) for n in cn_all}
        for x in variant["contacts"]:
            if x.get("lean"):   # lean threshold: the most of its reach the chain used over full contact
                # the authored reach per frame (0 where the contact has no weight): lean only past it
                x["lean"]["reach"] = [round(reach_ratios(variant, ual_act, clips_u, k_auth, f, any_weight=True)
                                            .get(x["name"], 0.0), 3) for f in range(variant["frames"] + 1)]
        side["variants"].append(variant)
    (lib_dir / f"{entry}.json").write_text(json.dumps(side, indent=1) + "\n")
    lj = lib_dir / "library.json"
    lib = json.loads(lj.read_text()) if lj.exists() else {}
    lib.update({"version": 1, "fps": FPS, "skeleton": "Quaternius Universal Animation Library 1 (CC0), 65 bones",
                "root_bone": U.ROOT_BONE, "pelvis_bone": U.PELVIS_BONE, "bones": bones,
                "tracks": "rotation: every bone but root; translation: pelvis only (s * u + c per monster)",
                "doc": "docs/anim-reuse.md (Paired animations); same clip format as _shared/ual"})
        # keep other entries' clips
    clips = {k: v for k, v in (lib.get("clips") or {}).items() if v.get("paired") != entry}
    clips.update(clips_out)
    lib["clips"] = dict(sorted(clips.items()))
    lib.setdefault("paired", {})[entry] = f"{entry}.json"
    lj.write_text(json.dumps(lib, indent=1) + "\n")
    print(f"{entry}: {len(clips_out)} clips, sidecar {lib_dir / (entry + '.json')}")
    return side


# --------------------------------------------------------------------------------------------------
# runtime model: pose a pair, IK the contacts, measure
# --------------------------------------------------------------------------------------------------
def load_entry(entry: str) -> tuple[dict, dict]:
    lib_dir = OUT / LIB_REL
    side = json.loads((lib_dir / f"{entry}.json").read_text())
    clips = {}
    for v in side["variants"]:
        for role, name in v["clips"].items():
            j, b = U.read_glb(lib_dir / f"{name}.glb")
            ch = U.channels(j, b, j["animations"][0])
            t = np.arange(v["frames"] + 1) / side["fps"]
            rot = {bn: U.sample(c, t, True) for (bn, path), c in ch.items() if path == "rotation"}
            pel = U.sample(ch[("pelvis", "translation")], t, False)
            clips[name] = {"rot": rot, "pelvis": pel}
    return side, clips


def pick_variant(side: dict, s_i: float, s_r: float) -> dict | None:
    r = s_r / s_i
    best, bd = None, 1e9
    for v in side["variants"]:
        lo, hi = v["ratio"]
        d = 0.0 if lo <= r <= hi else min(abs(r - lo), abs(r - hi))
        if d < bd:
            best, bd = v, d
    return best if bd <= 0.05 else None


def contact_weight(c: dict, f: float) -> float:
    a, b = c["frames"]
    bi, bo = c["blend"]
    if f < a - bi or f > b + bo:
        return 0.0
    if f < a:
        x = (f - (a - bi)) / max(bi, 1e-9)
    elif f > b:
        x = 1 - (f - b) / max(bo, 1e-9)
    else:
        return 1.0
    return x * x * (3 - 2 * x)


def alignment_world(v: dict, s: dict, k: float | None = None) -> np.ndarray:
    """The receiver's world (= initiator model space) transform. k: the offset scale; default the
    scale_weights rule (sum w * hip_scale / authored_scale)."""
    al = v["alignment"]
    if k is None:
        k = sum(w * s[r] / v["authored_scale"][r] for r, w in al["scale_weights"].items())
    m = np.eye(4)
    m[:3, :3] = axis_angle([0, 1, 0], math.radians(al["yaw_deg"]))
    m[:3, 3] = np.array(al["offset"]) * k
    return m


def tip_scale(a: "Actor") -> float:
    return 1.0 if getattr(a, "_unit", False) else a.s


def reach_ratios(v: dict, act: dict, clips: dict, k: float, f: int, any_weight: bool = False) -> dict:
    """{contact: |wrist target - chain root| / (upper + lower bone length)} at frame f with the receiver at
    offset scale k, for the contacts at full weight on f (any_weight: with any weight) (no IK)."""
    s = {r: a.s for r, a in act.items()}
    world = {"initiator": np.eye(4), "receiver": alignment_world(v, s, k)}
    G = {r: act[r].skel.fk(act[r].local(clips[v["clips"][r]], f), world[r]) for r in act}
    out = {}
    for c in v["contacts"]:
        if contact_weight(c, f) < 1.0 and not (any_weight and contact_weight(c, f) > 0):
            continue
        role = c["ik"]["role"]
        sk = act[role].skel
        a_k, b_k, c_k = (sk.index[n] for n in c["ik"]["chain"])
        g = G[role]
        tip = (g[c_k] @ np.append(np.asarray(c["from"]["offset"]) * tip_scale(act[role]), 1))[:3]
        tgt = act[c["to"]["role"]].point(G[c["to"]["role"]], c["to"], {}) - (tip - g[c_k, :3, 3])
        ln = np.linalg.norm(g[b_k, :3, 3] - g[a_k, :3, 3]) + np.linalg.norm(g[c_k, :3, 3] - g[b_k, :3, 3])
        out[c["name"]] = float(np.linalg.norm(tgt - g[a_k, :3, 3]) / ln)
    return out


def fit_offset(v: dict, act: dict, clips: dict) -> float:
    """alignment.fit: the offset scale k at which no contact at full weight on the fit frame needs more of
    its chain's reach than it had when authored (max over contacts of reach / authored reach = 1), within
    [lo, hi] x the scale_weights rule. Positions are linear in k, so a bisection is exact enough."""
    s = {r: a.s for r, a in act.items()}
    al = v["alignment"]
    k0 = sum(w * s[r] / v["authored_scale"][r] for r, w in al["scale_weights"].items())
    fit = al.get("fit")
    if not fit:
        return k0
    want = {c["name"]: c["reach"] for c in v["contacts"] if c.get("reach")}

    def g(k):
        r = reach_ratios(v, act, clips, k, fit["frame"])
        return max(r[n] / want[n] for n in r if n in want) - 1.0
    lo, hi = fit["range"][0] * k0, fit["range"][1] * k0
    if g(lo) >= 0:
        k = lo
    elif g(hi) <= 0:
        k = hi
    else:
        for _ in range(30):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if g(mid) < 0 else (lo, mid)
        k = (lo + hi) / 2
    # clearance floor: roots at least the two torso fronts (+ min_gap x mean hip_scale) apart
    if "min_gap" in fit:
        d = float(np.linalg.norm(al["offset"]))
        ms = (s["initiator"] + s["receiver"]) / 2
        k = max(k, (act["initiator"].depth + act["receiver"].depth + fit["min_gap"] * ms) / d)
    return k


def lean(sk: "Skel", L: np.ndarray, world: np.ndarray, spec: dict, items: list, weight: float) -> tuple:
    """Contact lean (godot-dnd paired_anim.gd does the same): when the targets are further than the
    chains reached when authored, bend spec["bones"] (forward, toward the targets, equal shares, about
    a horizontal axis through the first bone) by the smallest angle <= max_deg that brings every chain
    back to its authored reach. items: [(chain root bone, chain length, wrist target, authored reach)].
    Returns (new L, degrees)."""
    G = sk.fk(L, world)
    piv = G[sk.index[spec["bones"][0]], :3, 3]
    tmean = np.mean([t for _, _, t, _ in items], 0)
    dirh = tmean - piv
    dirh[1] = 0
    if np.linalg.norm(dirh) < 1e-6:
        return L, 0.0
    axis = np.cross([0, 1.0, 0], dirh / np.linalg.norm(dirh))
    roots = [G[sk.index[r], :3, 3] for r, _, _, _ in items]

    def worst(th):
        R = axis_angle(axis, th)
        return max(np.linalg.norm(t - (piv + R @ (a - piv))) / ln / reach
                   for a, (r, ln, t, reach) in zip(roots, items))
    th = 0.0
    mx = math.radians(spec.get("max_deg", 40))
    while worst(th) > 1.0 and th < mx:
        th = min(mx, th + math.radians(1))
    th *= weight
    if th <= 0:
        return L, 0.0
    L = L.copy()
    n = len(spec["bones"])
    for b in spec["bones"]:
        G = sk.fk(L, world)
        k = sk.index[b]
        pg = G[sk.parent[k]]
        L[k, :3, :3] = np.linalg.inv(pg[:3, :3]) @ axis_angle(axis, th / n) @ G[k, :3, :3]
    return L, math.degrees(th)


def simulate(side: dict, clips: dict, ids: dict, frames=None, ik: bool = True, mesh: bool = False) -> dict:
    """Pose both actors over the clip as the runtime does. ids {role: monster id}. Returns
    {variant, per-frame {role: G}, contacts: {name: [(frame, weight, dist_before, dist_after)]}}."""
    act = {r: Actor(m, mesh=mesh) for r, m in ids.items()}
    s = {r: a.s for r, a in act.items()}
    v = pick_variant(side, s["initiator"], s["receiver"])
    if v is None:
        return {"error": f"no variant for ratio {s['receiver'] / s['initiator']:.2f}"}
    k = fit_offset(v, act, clips)
    world = {"initiator": np.eye(4), "receiver": alignment_world(v, s, k)}
    offs: dict = {}
    out = {"variant": v["name"], "ratio": round(s["receiver"] / s["initiator"], 3), "frames": {}, "contacts": {}, "k": k,
           "world": world, "actors": act, "mean_s": (s["initiator"] + s["receiver"]) / 2}
    frames = range(v["frames"] + 1) if frames is None else frames
    for f in frames:
        L = {r: act[r].local(clips[v["clips"][r]], f) for r in act}
        G = {r: act[r].skel.fk(L[r], world[r]) for r in act}
        for role in act:
            lc = [c for c in v["contacts"] if ik and c.get("lean") and c["ik"]["role"] == role
                  and contact_weight(c, f) > 0]
            if not lc:
                continue
            sk = act[role].skel
            spec = dict(lc[0]["lean"])
            total = 0.0
            for _ in range(3):   # the pivot estimate is first order: re-measure and lean on
                items = []
                for c in lc:
                    a_k, b_k, c_k = (sk.index[n] for n in c["ik"]["chain"])
                    g = G[role]
                    tip = (g[c_k] @ np.append(np.asarray(c["from"]["offset"]) * tip_scale(act[role]), 1))[:3]
                    t = act[c["to"]["role"]].point(G[c["to"]["role"]], c["to"], offs) - (tip - g[c_k, :3, 3])
                    ln = np.linalg.norm(g[b_k, :3, 3] - g[a_k, :3, 3]) + np.linalg.norm(g[c_k, :3, 3] - g[b_k, :3, 3])
                    items.append((c["ik"]["chain"][0], ln, t, max(c["lean"]["reach"][f], 0.05)))
                spec["max_deg"] = lc[0]["lean"].get("max_deg", 40) - total
                L[role], deg = lean(sk, L[role], world[role], spec, items, max(contact_weight(c, f) for c in lc))
                G[role] = sk.fk(L[role], world[role])
                total += deg
                if deg < 0.5:
                    break
            out.setdefault("lean", {})[f] = round(total, 1)
        for c in v["contacts"]:
            w = contact_weight(c, f)
            fr, to = c["from"], c["to"]
            a_from, a_to = act[fr["role"]], act[to["role"]]
            before = float(np.linalg.norm(a_from.point(G[fr["role"]], fr, offs) - a_to.point(G[to["role"]], to, offs)))
            if ik and w > 0:
                role = c["ik"]["role"]
                tgt = a_to.point(G[to["role"]], to, offs)
                L[role] = two_bone_ik(act[role].skel, L[role], world[role], c["ik"]["chain"],
                                      np.asarray(fr["offset"]) * tip_scale(act[role]), tgt, w)
                G[role] = act[role].skel.fk(L[role], world[role])
            after = float(np.linalg.norm(a_from.point(G[fr["role"]], fr, offs) - a_to.point(G[to["role"]], to, offs)))
            out["contacts"].setdefault(c["name"], []).append((f, round(w, 3), before, after))
        out["frames"][f] = G
    out["v"] = v
    return out


def torso_gap(sim: dict, f: int) -> float:
    """Smallest distance between the two actors' torso/head capsule axes minus their radii (UAL-ish
    radii scaled by hip_scale): < 0 means the torsos overlap. Rough: a sanity number, the sheet decides."""
    segs = [("pelvis", "spine_02", 0.13), ("spine_02", "neck_01", 0.13), ("neck_01", "Head", 0.09)]
    G = sim["frames"][f]
    best = 1e9
    acts = sim["actors"]
    pts = {}
    for r in ("initiator", "receiver"):
        sk = acts[r].skel
        pts[r] = [(G[r][sk.index[a], :3, 3], G[r][sk.index[b], :3, 3], rad * acts[r].s) for a, b, rad in segs]
    for a0, a1, ra in pts["initiator"]:
        for b0, b1, rb in pts["receiver"]:
            best = min(best, _seg_dist(a0, a1, b0, b1) - ra - rb)
    return best


def _seg_dist(p0, p1, q0, q1) -> float:
    best = 1e9
    for t in np.linspace(0, 1, 11):
        a = p0 + (p1 - p0) * t
        d = q1 - q0
        u = float(np.clip((a - q0) @ d / (d @ d + 1e-12), 0, 1))
        best = min(best, float(np.linalg.norm(a - (q0 + d * u))))
    return best


def check(entry: str, pairs: list[tuple[str, str]]) -> int:
    side, clips = load_entry(entry)
    fails = 0
    for i_id, r_id in pairs:
        sim = simulate(side, clips, {"initiator": i_id, "receiver": r_id})
        if sim.get("error"):
            print(f"{i_id} -> {r_id}: {sim['error']}")
            continue
        v = sim["v"]
        ms = sim["mean_s"]
        print(f"{i_id} -> {r_id}: variant {sim['variant']} (ratio {sim['ratio']}), alignment "
              f"{np.round(sim['world']['receiver'][:3, 3], 3).tolist()}")
        for c in v["contacts"]:
            rows = sim["contacts"][c["name"]]
            full = [r for r in rows if r[1] >= 1.0]
            sync = next(r for r in rows if r[0] == v["sync_frame"])
            mb = max(r[2] for r in full)
            ma = max(r[3] for r in full)
            tol = c["tolerance"] * ms
            ok = ma <= tol
            fails += 0 if ok else 1
            print(f"  {c['name']:48s} full contact {c['frames']}: before IK max {mb:.3f} m, after IK max {ma:.4f} m "
                  f"(sync {sync[3]:.4f}) tol {tol:.3f} {'ok' if ok else 'FAIL'}")
        gaps = [(torso_gap(sim, f), f) for f in sim["frames"]]
        g, gf = min(gaps)
        print(f"  torso gap min {g:.3f} m at frame {gf}" + (" (overlap)" if g < 0 else "")
              + f"; offset scale k {sim['k']:.3f}; lean max {max((sim.get('lean') or {0: 0}).values()):.1f} deg")
    return 1 if fails else 0


# --------------------------------------------------------------------------------------------------
# sheet: skin both actors on a few frames, render side + front in Blender
# --------------------------------------------------------------------------------------------------
def sheet(entry: str, pairs: list[tuple[str, str]], frames: list[int] | None = None) -> list[Path]:
    side, clips = load_entry(entry)
    outs = []
    for i_id, r_id in pairs:
        sim = simulate(side, clips, {"initiator": i_id, "receiver": r_id}, mesh=True)
        if sim.get("error"):
            print(f"{i_id} -> {r_id}: {sim['error']}")
            continue
        v = sim["v"]
        fr = frames or sorted({0, 10, v["sync_frame"], 30, 50, 75, 100, 118, 130, v["frames"]})
        data = {}
        for role in ("initiator", "receiver"):
            a = sim["actors"][role]
            data[f"{role}_tris"] = a.skel.mesh["tris"]
            for f in fr:
                data[f"{role}_{f}"] = a.skel.skin(sim["frames"][f][role]).astype(np.float32)
        tag = f"{entry}_{i_id}_{r_id}".replace("@", "-")
        work = SCRATCH / "sheet"
        work.mkdir(parents=True, exist_ok=True)
        npz = work / f"{tag}.npz"
        np.savez_compressed(npz, **data)
        h = max(sim["actors"]["initiator"].scale_m, sim["actors"]["receiver"].scale_m)
        labels = {}
        for f in fr:
            ds = [r[3] for c in v["contacts"] for r in sim["contacts"][c["name"]] if r[0] == f and r[1] > 0]
            labels[str(f)] = f"f{f}" + (" sync" if f == v["sync_frame"] else "") + (f"  max contact {max(ds) * 100:.1f} cm" if ds else "")
        job = {"mode": "sheet", "npz": str(npz), "frames": fr, "height": h, "labels": labels,
               "title": f"{entry} ({v['name']}): {i_id} (orange, initiator) grabs {r_id} (blue, receiver)",
               "tiles": str(work / tag), "px": 360}
        run_blender("blender_paired.py", job, work / f"{tag}.log")
        out = SCRATCH / f"sheet_{tag}.png"
        _assemble(work / tag, fr, labels, job["title"], out)
        outs.append(out)
        print(f"  sheet {out}")
    return outs


def _assemble(tiles: Path, frames: list[int], labels: dict, title: str, out: Path) -> None:
    from PIL import Image, ImageDraw
    import contact_sheet
    ims = {(v, f): Image.open(tiles / f"{v}_{f}.png").convert("RGB") for v in ("side", "front") for f in frames}
    w, h = next(iter(ims.values())).size
    lab = 26
    sheet_im = Image.new("RGB", (w * len(frames), 40 + 2 * (h + lab)), "white")
    dr = ImageDraw.Draw(sheet_im)
    fnt, fs = contact_sheet.font(22), contact_sheet.font(15)
    dr.text((10, 8), title, fill="black", font=fnt)
    for r, v in enumerate(("side", "front")):
        for c, f in enumerate(frames):
            x, y = c * w, 40 + r * (h + lab)
            dr.text((x + 6, y + 4), f"{v}  {labels[str(f)]}", fill="black", font=fs)
            sheet_im.paste(ims[(v, f)], (x, y + lab))
    sheet_im.save(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["author", "check", "sheet", "offsets"])
    ap.add_argument("entry")
    ap.add_argument("pairs", nargs="*", help="initiator:receiver (a lone id pairs with itself)")
    ap.add_argument("--keep-blend", action="store_true", help="author: also save each variant's .blend in the scratch dir")
    a = ap.parse_args()
    pairs = [tuple(p.split(":")) if ":" in p else (p, p) for p in a.pairs]
    if a.cmd == "offsets":
        print(json.dumps(_offsets(), indent=1))
        return 0
    if a.cmd == "author":
        author(a.entry, a.keep_blend)
        return 0
    if a.cmd == "check":
        return check(a.entry, pairs or [("tough", "bandit"), ("ogre", "bandit")])
    if a.cmd == "sheet":
        sheet(a.entry, pairs or [("tough", "bandit"), ("ogre", "bandit")])
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
