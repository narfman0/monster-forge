"""Side-plane lean of an upright body (numpy only; imported by blender_qa.py and blender_lean.py).

The mesh generator reconstructs a model in its reference camera's frame. A front-view biped
whose reference was drawn from slightly below (or above) comes out pitched back (or forward)
by a few to 20 degrees, and QA's stance leveling cannot see it: two feet side by side define
no pitch (`two_lowest_pitch` leaves it alone). docs/rig-anim.md "Lean check".

`core_lean(cen, area, fwd)`: the body column's lean in degrees from vertical, + forward / -
back. The column is the surface within CORE_HALF x height of the body's mid-plane (lateral
median), which leaves out arms held at the sides and spread wings; per 5% height slice from
LO to HI its area-weighted forward position, then a weighted least-squares line through the
slice centroids (forward position against height). Robes, tails on the ground and folded
wings pull the low or the back slices; the line's slope is the median over three fits (the
whole span, its lower and upper 60%), so one bad part of the column does not tip it.
"""
import math

import numpy as np

CORE_HALF = 0.09   # lateral half-width of the body column, x height
LO, HI = 0.12, 0.95
STEP = 0.05


def slices(cen, area, fwd=1.0):
    """(z_mid, forward, weight) per height slice of the body column (fractions of the height)."""
    x, y, z = cen[:, 0], cen[:, 1] * fwd, cen[:, 2]
    z0, H = float(z.min()), float(np.ptp(z)) or 1e-9
    xm = float(np.median(x[z > z0 + 0.3 * H])) if np.any(z > z0 + 0.3 * H) else float(np.median(x))
    core = np.abs(x - xm) < CORE_HALF * H
    out = []
    for lo in np.arange(LO, HI - 1e-9, STEP):
        m = core & (z >= z0 + lo * H) & (z < z0 + (lo + STEP) * H)
        w = float(area[m].sum())
        if w > 0:
            out.append((lo + STEP / 2, float(np.average(y[m], weights=area[m])) / H, w))
    return np.array(out) if out else np.zeros((0, 3))


def _fit(s):
    if len(s) < 3:
        return None
    zz, yy, w = s[:, 0], s[:, 1], np.sqrt(s[:, 2])
    A = np.c_[zz, np.ones(len(zz))] * w[:, None]
    k = np.linalg.lstsq(A, yy * w, rcond=None)[0][0]
    return math.degrees(math.atan(k))


def head_over_feet(cen, area, fwd=1.0):
    """Degrees from vertical of the line from the support (surface in the lowest 3% of the
    height, mean forward position) to the head (the column's top HEAD of the height)."""
    x, y, z = cen[:, 0], cen[:, 1] * fwd, cen[:, 2]
    z0, H = float(z.min()), float(np.ptp(z)) or 1e-9
    xm = float(np.median(x[z > z0 + 0.3 * H])) if np.any(z > z0 + 0.3 * H) else float(np.median(x))
    foot = z < z0 + 0.03 * H
    head = (np.abs(x - xm) < CORE_HALF * H) & (z > z0 + (1 - HEAD) * H)
    if not foot.any() or not head.any() or area[head].sum() <= 0:
        return None
    hy, hz = np.average(y[head], weights=area[head]), np.average(z[head], weights=area[head])
    return math.degrees(math.atan2(hy - float(y[foot].mean()), hz - z0))


HEAD = 0.12


def core_lean(cen, area, fwd=1.0):
    """{"lean": degrees (+ forward), "fits": [whole, lower, upper, head], "spread", "slices"}:
    the median of the column's three line fits and the head-over-feet line; lean None when
    the column has under 5 slices."""
    s = slices(cen, area, fwd)
    if len(s) < 5:
        return {"lean": None, "fits": [], "spread": None, "slices": int(len(s))}
    n = len(s)
    k = max(3, int(round(0.6 * n)))
    fits = [f for f in (_fit(s), _fit(s[:k]), _fit(s[n - k:]), head_over_feet(cen, area, fwd)) if f is not None]
    return {"lean": round(float(np.median(fits)), 2), "fits": [round(f, 2) for f in fits],
            "spread": round(float(np.ptp(fits)), 2), "slices": n}
