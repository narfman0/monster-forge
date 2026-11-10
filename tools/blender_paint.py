"""Multiview paint stage, Blender side (headless). See docs/paint-stage.md.

Run by tools/paint.py:
    blender -b --factory-startup -P tools/blender_paint.py -- '<job json>'

job["mode"]:

- "views": render the control images for every view of model.glb (LOD0): depth
  (near = white, background black), camera-space normal, the voxel albedo seen from the
  camera (unlit, on white: img2img init / fallback), and the coverage mask. Writes
  <paint_dir>/<view>_{depth,normal,color,mask}.png and cameras.json (orthographic
  intrinsics and extrinsics, plus the framing).
- "bake": project the generated <view>_gen.png images back onto the mesh and blend them
  into a new albedo in LOD0's UV layout; texels no view sees keep the voxel albedo.
  Writes the painted texture, a coverage map, and model.glb with the new base colour
  (every other map, the geometry and the UVs are untouched).
- "compare": voxel vs painted renders (head close-up with the QA head camera, and a
  4-angle body strip) for review sheets.

Why a numpy rasterizer and not Blender's own projection/bake tools: one function
(`raster`) produces both the camera images (z-buffer, depth/normal/colour) and the UV-space
texel positions, with the same orthographic projection used to render and to project
back, so there is no sub-pixel disagreement between "what the camera saw" and "where a
texel lands in the view", and the depth test reads the exact z-buffer the control image
came from. It needs no Cycles bake setup, no compositor (whose API changed in Blender 5),
no UV-project modifiers or per-view material stacks, and runs in a few seconds on CPU.
Blender is used for glTF I/O (keeps the material, other maps and export settings exactly
as QA wrote them) and for the EEVEE comparison renders.
"""
import json
import math
import os
import sys
import time

import bpy
import numpy as np

job = json.loads(sys.argv[sys.argv.index("--") + 1])
t_start = time.time()


# ---------------------------------------------------------------- I/O helpers
def load_glb(path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=path)
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if len(meshes) != 1:
        raise SystemExit(f"expected one mesh in {path}, got {len(meshes)}")
    return meshes[0]


def base_color_node(obj):
    mat = obj.data.materials[0]
    for n in mat.node_tree.nodes:
        if n.type == "TEX_IMAGE" and any(l.to_socket.name == "Base Color" for l in n.outputs[0].links):
            return n
    raise SystemExit("no base colour image node")


def read_png(path):
    """-> float32 (H, W, C) top-down rows, raw stored values (no colour conversion)."""
    img = bpy.data.images.load(path)
    w, h = img.size
    a = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(a)
    bpy.data.images.remove(img)
    return a.reshape(h, w, 4)[::-1].copy()


def write_png(arr, path):
    """float (H, W) / (H, W, 3) / (H, W, 4) top-down in 0..1 -> 8-bit PNG."""
    arr = np.asarray(arr, np.float32)
    if arr.ndim == 2:
        arr = np.repeat(arr[:, :, None], 3, 2)
    if arr.shape[2] == 3:
        arr = np.concatenate([arr, np.ones(arr.shape[:2] + (1,), np.float32)], 2)
    h, w = arr.shape[:2]
    img = bpy.data.images.new(os.path.basename(path), width=w, height=h, alpha=True)
    img.pixels.foreach_set(np.clip(arr[::-1], 0, 1).ravel())
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    bpy.data.images.remove(img)


def mesh_arrays(obj):
    """World-space vertex positions, loop triangles (vertex ids and loop ids), per-loop UVs
    and per-loop (custom/smooth) normals."""
    me = obj.data
    mw = np.array(obj.matrix_world)
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    me.calc_loop_triangles()
    lt = me.loop_triangles
    tv = np.empty(len(lt) * 3, np.int64)
    lt.foreach_get("vertices", tv)
    tl = np.empty(len(lt) * 3, np.int64)
    lt.foreach_get("loops", tl)
    uv = np.empty(len(me.loops) * 2)
    me.uv_layers.active.data.foreach_get("uv", uv)
    cn = np.empty(len(me.loops) * 3)
    me.corner_normals.foreach_get("vector", cn)
    cn = cn.reshape(-1, 3) @ np.linalg.inv(mw[:3, :3]).T
    cn /= np.linalg.norm(cn, axis=1, keepdims=True) + 1e-12
    return co, tv.reshape(-1, 3), tl.reshape(-1, 3), uv.reshape(-1, 2), cn


# ---------------------------------------------------------------- rasterizer
def raster(xy, z, tris, W, H, zbuffer=True, chunk_px=6_000_000):
    """Rasterize triangles given per-corner pixel coords xy (m, 3, 2) (pixel (i, j) covers
    [i, i+1) x [j, j+1); its centre is sampled) and depths z (m, 3). Returns flat pixel ids,
    triangle ids and barycentrics (k, 3) of the covered pixels: the nearest triangle per
    pixel when zbuffer, else any covering triangle (UV layouts do not overlap)."""
    xy = np.asarray(xy, np.float64)
    lo = np.ceil(xy.min(1) - 0.5).astype(np.int64)
    hi = np.floor(xy.max(1) - 0.5).astype(np.int64)
    lo = np.maximum(lo, 0)
    hi[:, 0] = np.minimum(hi[:, 0], W - 1)
    hi[:, 1] = np.minimum(hi[:, 1], H - 1)
    span = hi - lo + 1
    a, b, c = xy[:, 0], xy[:, 1], xy[:, 2]
    den = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    ok = (span.min(1) > 0) & (np.abs(den) > 1e-12)
    size = span.max(1)
    out_pix, out_tri, out_bary, out_z = [], [], [], []
    S = 1
    while True:
        sel = np.nonzero(ok & (size <= S) & (size > S // 2))[0]
        if len(sel):
            per = max(1, chunk_px // (S * S))
            ar = np.arange(S)
            for k0 in range(0, len(sel), per):
                t = sel[k0:k0 + per]
                gx = lo[t, 0][:, None, None] + ar[None, None, :]
                gy = lo[t, 1][:, None, None] + ar[None, :, None]
                px, py = gx + 0.5, gy + 0.5
                A, B, C = a[t], b[t], c[t]
                d = den[t][:, None, None]
                w0 = ((B[:, 0, None, None] - px) * (C[:, 1, None, None] - py)
                      - (B[:, 1, None, None] - py) * (C[:, 0, None, None] - px)) / d
                w1 = ((C[:, 0, None, None] - px) * (A[:, 1, None, None] - py)
                      - (C[:, 1, None, None] - py) * (A[:, 0, None, None] - px)) / d
                w2 = 1.0 - w0 - w1
                eps = -1e-7
                inside = ((w0 >= eps) & (w1 >= eps) & (w2 >= eps)
                          & (gx <= hi[t, 0][:, None, None]) & (gy <= hi[t, 1][:, None, None]))
                ti, yi, xi = np.nonzero(inside)
                if not len(ti):
                    continue
                W0, W1, W2 = w0[ti, yi, xi], w1[ti, yi, xi], w2[ti, yi, xi]
                tt = t[ti]
                out_pix.append(gy[ti, yi, 0] * W + gx[ti, 0, xi])
                out_tri.append(tt)
                out_bary.append(np.stack([W0, W1, W2], 1).astype(np.float32))
                out_z.append((W0 * z[tt, 0] + W1 * z[tt, 1] + W2 * z[tt, 2]).astype(np.float32))
        if S >= max(W, H) * 2 or not (ok & (size > S)).any():
            break
        S *= 2
    if not out_pix:
        return np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros((0, 3), np.float32)
    pix, tri, bary, zz = (np.concatenate(v) for v in (out_pix, out_tri, out_bary, out_z))
    if zbuffer:
        order = np.lexsort((zz, pix))
    else:
        order = np.argsort(pix, kind="stable")
    pix, tri, bary = pix[order], tri[order], bary[order]
    first = np.ones(len(pix), bool)
    first[1:] = pix[1:] != pix[:-1]
    return pix[first], tri[first], bary[first]


# ---------------------------------------------------------------- cameras
def camera_basis(direction):
    """direction: unit vector from the target toward the camera. Returns right, up, view
    (view = the camera's looking direction). World up is +Z (Blender); a camera looking
    straight down takes +Y (the model's forward) as image up."""
    dvec = np.asarray(direction, float)
    dvec = dvec / np.linalg.norm(dvec)
    v = -dvec
    wup = np.array([0.0, 0.0, 1.0]) if abs(v[2]) < 0.95 else np.array([0.0, 1.0, 0.0])
    r = np.cross(v, wup)
    r /= np.linalg.norm(r)
    u = np.cross(r, v)
    return r, u, v


def project(p, cam):
    """World points -> (x, y_down) pixel coords and depth along the view direction."""
    c, r, u, v = (np.asarray(cam[k]) for k in ("center", "right", "up", "view"))
    q = p - c
    s, W, H = cam["ortho_scale"], cam["width"], cam["height"]
    x = (q @ r / s + 0.5) * W
    y = (0.5 - q @ u / s) * H
    return x, y, q @ v


def view_dirs(job_views, fwd):
    """view spec {name, yaw, elev} (degrees; yaw about +Z from the model's front, positive
    = toward the model's left) -> unit direction from the target to the camera."""
    out = {}
    for vs in job_views:
        yaw, el = math.radians(vs["yaw"]), math.radians(vs["elev"])
        f = np.array(fwd, float)
        side = np.array([-f[1], f[0], 0.0])  # +90 deg about Z: the model's left for fwd +Y
        hdir = math.cos(yaw) * f + math.sin(yaw) * side
        out[vs["name"]] = math.cos(el) * hdir + np.array([0, 0, math.sin(el)])
    return out


# ---------------------------------------------------------------- image ops
def erode_dist(mask, R):
    """Approximate distance (px, capped at R) from each True pixel to the nearest False one
    (4-neighbour erosion steps)."""
    dist = np.zeros(mask.shape, np.float32)
    cur = mask.copy()
    for i in range(R):
        dist += cur
        nxt = cur.copy()
        nxt[1:, :] &= cur[:-1, :]
        nxt[:-1, :] &= cur[1:, :]
        nxt[:, 1:] &= cur[:, :-1]
        nxt[:, :-1] &= cur[:, 1:]
        cur = nxt
        if not cur.any():
            break
    return dist


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def bilinear(img, x, y):
    """Sample img (H, W, C) at continuous pixel coords (x right, y down; centres at +0.5)."""
    H, W = img.shape[:2]
    x = np.clip(x - 0.5, 0, W - 1.001)
    y = np.clip(y - 0.5, 0, H - 1.001)
    x0, y0 = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
    fx, fy = (x - x0)[:, None], (y - y0)[:, None]
    return ((img[y0, x0] * (1 - fx) + img[y0, x0 + 1] * fx) * (1 - fy)
            + (img[y0 + 1, x0] * (1 - fx) + img[y0 + 1, x0 + 1] * fx) * fy)


def downsample(img, k):
    H, W = img.shape[:2]
    return img.reshape(H // k, k, W // k, k, -1).mean((1, 3))


# ---------------------------------------------------------------- views
def render_view(co, tv, tl, uv, cn, tex, cam, ss=2):
    """z-buffer render of one camera: depth (float, inf = background), normal (camera space),
    voxel colour (unlit, supersampled ss x ss) and coverage."""
    W, H = cam["width"], cam["height"]
    big = dict(cam, width=W * ss, height=H * ss)
    x, y, z = project(co, big)
    xy = np.stack([x, y], 1)[tv]
    pix, tri, bary = raster(xy, z[tv], tv, W * ss, H * ss)
    T = tex.shape[0]
    # voxel colour (bilinear texture lookup of the interpolated UV), supersampled
    luv = (uv[tl[tri]] * bary[:, :, None]).sum(1)
    col = np.ones((H * ss * W * ss, 3), np.float32)
    col[pix] = bilinear(tex[:, :, :3], luv[:, 0] * tex.shape[1], (1 - luv[:, 1]) * T)
    cov = np.zeros(H * ss * W * ss, np.float32)
    cov[pix] = 1
    color = downsample(col.reshape(H * ss, W * ss, 3), ss)
    mask_ss = cov.reshape(H * ss, W * ss)
    # depth and normal at 1x: take the supersample at the pixel centre region (top-left
    # sample of each ss block is fine for control images; use the block minimum depth)
    dep = np.full(H * ss * W * ss, np.inf, np.float32)
    dep[pix] = (z[tv[tri]] * bary).sum(1)
    dep = dep.reshape(H, ss, W, ss).min((1, 3))
    nrm = np.zeros((H * ss * W * ss, 3), np.float32)
    n = (cn[tl[tri]] * bary[:, :, None]).sum(1)
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    r, u, v = (np.asarray(cam[k]) for k in ("right", "up", "view"))
    nrm[pix] = np.stack([n @ r, n @ u, -(n @ v)], 1)
    nrm = downsample(nrm.reshape(H * ss, W * ss, 3), ss)
    nrm /= np.linalg.norm(nrm, axis=2, keepdims=True) + 1e-12
    mask = downsample(mask_ss[:, :, None], ss)[:, :, 0]
    return dep, nrm, color, mask


def frame_camera(co, direction, name, size, margin=1.08, box=None):
    r, u, v = camera_basis(direction)
    if box is not None:
        center, side = np.asarray(box["center"], float), float(box["side"])
        scale = side
    else:
        lo, hi = co.min(0), co.max(0)
        mid = (lo + hi) / 2
        q = co - mid
        px, py = q @ r, q @ u
        center = mid + r * (px.max() + px.min()) / 2 + u * (py.max() + py.min()) / 2
        scale = max(np.ptp(px), np.ptp(py)) * margin
    ext = float(np.linalg.norm(co.max(0) - co.min(0)))
    cam_center = center - v * ext * 2  # only the depth origin; orthographic
    return {"name": name, "type": "ORTHO", "center": cam_center.tolist(), "target": center.tolist(),
            "right": r.tolist(), "up": u.tolist(), "view": v.tolist(), "direction": list(map(float, direction)),
            "ortho_scale": float(scale), "width": size, "height": size,
            "pixel_size_m": float(scale) / size}


def mode_views():
    obj = load_glb(job["src"])
    co, tv, tl, uv, cn = mesh_arrays(obj)
    tex = read_png(job["voxel_tex"])
    pdir = job["paint_dir"]
    os.makedirs(pdir, exist_ok=True)
    fwd = job.get("fwd", [0, 1, 0])
    dirs = view_dirs(job["views"], fwd)
    size = int(job.get("size", 1024))
    cams = []
    ext = float(np.linalg.norm(co.max(0) - co.min(0)))
    for vs in job["views"]:
        name = vs["name"]
        cam = frame_camera(co, dirs[name], name, size, box=job.get("head_box") if vs.get("head") else None)
        cam.update({"yaw": vs["yaw"], "elev": vs["elev"], "head": bool(vs.get("head")), "hint": vs.get("hint", "")})
        dep, nrm, color, mask = render_view(co, tv, tl, uv, cn, tex, cam)
        fin = np.isfinite(dep)
        if fin.any():
            dmin, dmax = float(dep[fin].min()), float(dep[fin].max())
        else:
            dmin, dmax = 0.0, 1.0
        dn = np.zeros_like(dep)
        dn[fin] = 1.0 - 0.85 * (dep[fin] - dmin) / max(dmax - dmin, 1e-9)  # near 1.0, far 0.15, bg 0
        write_png(dn, os.path.join(pdir, f"{name}_depth.png"))
        write_png(np.where(fin[:, :, None], nrm * 0.5 + 0.5, 0.0), os.path.join(pdir, f"{name}_normal.png"))
        a = mask[:, :, None]
        write_png(color * a + (1 - a), os.path.join(pdir, f"{name}_color.png"))
        write_png(mask, os.path.join(pdir, f"{name}_mask.png"))
        np.save(os.path.join(pdir, f"{name}_zbuf.npy"), dep.astype(np.float32))
        cam.update({"depth_range": [dmin, dmax], "coverage": float(mask.mean())})
        cams.append(cam)
    rep = {"views": cams, "model_extent_m": ext, "texture": list(tex.shape[:2]),
           "seconds": round(time.time() - t_start, 1)}
    with open(os.path.join(pdir, "cameras.json"), "w") as f:
        json.dump(rep, f, indent=1)
    print("PAINT_VIEWS_OK", json.dumps({"seconds": rep["seconds"], "views": len(cams)}))


# ---------------------------------------------------------------- bake
def uv_texels(co, tv, tl, uv, cn, T):
    """Rasterize the UV layout at T x T (Blender row order: row 0 = v 0). Returns flat texel
    ids, world positions and normals of the covered texels."""
    xy = uv[tl] * T
    pix, tri, bary = raster(xy, np.zeros((len(tv), 3)), tv, T, T, zbuffer=False)
    pos = (co[tv[tri]] * bary[:, :, None]).sum(1)
    n = (cn[tl[tri]] * bary[:, :, None]).sum(1)
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    return pix, pos, n


def color_match(gen, ref, mask, amount):
    """Shift gen's per-channel mean/std inside the mask toward ref's (amount 0..1)."""
    if amount <= 0:
        return gen
    m = mask > 0.5
    if m.sum() < 100:
        return gen
    g, r = gen[m], ref[m]
    mg, sg, mr, sr = g.mean(0), g.std(0) + 1e-4, r.mean(0), r.std(0) + 1e-4
    scale = np.clip(sr / sg, 0.5, 2.0)
    out = (gen - mg) * scale + mr
    return np.clip(gen + amount * (out - gen), 0, 1)


def mode_bake():
    params = job.get("params", {})
    cos_min = math.cos(math.radians(float(params.get("max_angle", 70))))
    edge_px = int(params.get("edge_px", 12))
    full_w = float(params.get("full_weight", 0.35))
    cm = float(params.get("color_match", 0.5))
    obj = load_glb(job["src"])
    co, tv, tl, uv, cn = mesh_arrays(obj)
    vox = read_png(job["voxel_tex"])[:, :, :3]
    T = vox.shape[0]
    Tout = int(job.get("texture_size") or T)
    if Tout != T:
        img = bpy.data.images.load(job["voxel_tex"])
        img.scale(Tout, Tout)
        a = np.empty(Tout * Tout * 4, np.float32)
        img.pixels.foreach_get(a)
        bpy.data.images.remove(img)
        vox = a.reshape(Tout, Tout, 4)[::-1, :, :3].copy()
        T = Tout
    vox_bu = vox[::-1].reshape(-1, 3)  # Blender row order (row 0 = v 0), like uv_texels
    pix, pos, nrm = uv_texels(co, tv, tl, uv, cn, T)
    cams = json.load(open(os.path.join(job["paint_dir"], "cameras.json")))
    ext = cams["model_extent_m"]
    acc = np.zeros((len(pix), 3), np.float64)
    wsum = np.zeros(len(pix), np.float64)
    best = np.zeros(len(pix), np.float32)
    per_view = {}
    max_scale = max(c["ortho_scale"] for c in cams["views"])
    for cam in cams["views"]:
        name = cam["name"]
        gpath = os.path.join(job["paint_dir"], f"{name}_gen.png")
        if not os.path.exists(gpath) or name in set(job.get("skip_views") or []):
            continue
        gen = read_png(gpath)[:, :, :3]
        W, H = cam["width"], cam["height"]
        if gen.shape[:2] != (H, W):
            raise SystemExit(f"{gpath}: size {gen.shape[:2]} != {(H, W)}")
        zbuf = np.load(os.path.join(job["paint_dir"], f"{name}_zbuf.npy"))
        vcol = read_png(os.path.join(job["paint_dir"], f"{name}_color.png"))[:, :, :3]
        fin = np.isfinite(zbuf)
        gen = color_match(gen, vcol, fin.astype(np.float32), cm)
        # trust map: inside the silhouette, away from silhouette and depth edges, where the
        # generated image is most likely misaligned with the mesh
        zf = np.where(fin, zbuf, np.inf)
        jump = 0.02 * ext
        edge = np.zeros_like(fin)
        for dy, dx in ((0, 1), (1, 0)):
            a_, b_ = zf[: H - dy, : W - dx], zf[dy:, dx:]
            e = ~(np.abs(a_ - b_) < jump)  # inf - x is inf, so silhouettes count too
            edge[: H - dy, : W - dx] |= e
            edge[dy:, dx:] |= e
        trust = smoothstep(0, edge_px, erode_dist(fin & ~edge, edge_px + 1))
        x, y, d = project(pos, cam)
        xi, yi = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
        inb = (xi >= 0) & (yi >= 0) & (xi < W) & (yi < H)
        xi, yi = np.clip(xi, 0, W - 1), np.clip(yi, 0, H - 1)
        zs = zbuf[yi, xi]
        tol = float(params.get("depth_tol", 0.006)) * ext + 2.0 * cam["pixel_size_m"]
        vis = inb & np.isfinite(zs) & (d <= zs + tol)
        cosang = -(nrm @ np.asarray(cam["view"]))
        w = np.where(vis, cosang * smoothstep(cos_min, cos_min + 0.25, cosang), 0.0)
        w *= trust[yi, xi]
        wm = os.path.join(job["paint_dir"], f"{name}_weight.png")  # optional 0..1 mask: limit a view to the region where its image is trustworthy
        if os.path.exists(wm):
            w *= read_png(wm)[:, :, 0][yi, xi]
        # close-up views carry more pixels per texel: weight by resolution
        w *= (max_scale / cam["ortho_scale"]) ** float(params.get("res_power", 1.0))
        k = w > 1e-4
        if k.any():
            acc[k] += w[k, None] * bilinear(gen, x[k], y[k])
            wsum[k] += w[k]
            best[k] = np.maximum(best[k], w[k])
        per_view[name] = {"texels": int(k.sum()), "weight": round(float(w[k].sum()), 1)}
    painted = acc / np.maximum(wsum, 1e-9)[:, None]
    alpha = smoothstep(0.0, full_w, wsum)[:, None]
    out = vox_bu.copy()
    out[pix] = alpha * painted + (1 - alpha) * vox_bu[pix]
    # bleed the painted colour 3 texels into the gutter around each island so mip-maps and
    # bilinear lookups at chart borders do not pick up the voxel colour
    cov = np.zeros(T * T, bool)
    cov[pix] = True
    img = out.reshape(T, T, 3)
    covm = cov.reshape(T, T)
    for _ in range(int(params.get("dilate", 3))):
        s = np.zeros((T, T, 3))
        c = np.zeros((T, T))
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            sh = np.roll(np.roll(img, dy, 0), dx, 1)
            shm = np.roll(np.roll(covm, dy, 0), dx, 1)
            s += sh * shm[:, :, None]
            c += shm
        grow = (~covm) & (c > 0)
        img[grow] = s[grow] / c[grow][:, None]
        covm = covm | grow
    out_td = img[::-1]
    write_png(out_td, job["out_tex"])
    covimg = np.zeros(T * T, np.float32)
    covimg[pix] = alpha[:, 0]
    write_png(covimg.reshape(T, T)[::-1], job["coverage"])
    rep = {"texels": int(len(pix)), "texture_size": T,
           "painted_fraction": round(float((alpha[:, 0] > 0.5).mean()), 4),
           "any_view_fraction": round(float((wsum > 1e-4).mean()), 4),
           "per_view": per_view, "params": params}
    # swap the base colour image and re-export LOD0 with QA's glTF settings
    node = base_color_node(obj)
    old = node.image
    new = bpy.data.images.load(job["out_tex"])
    new.name = "base_color_painted"
    new.colorspace_settings.name = old.colorspace_settings.name
    node.image = new
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    tris_before = sum(len(p.vertices) - 2 for p in obj.data.polygons)
    bpy.ops.export_scene.gltf(filepath=job["dst"], export_format="GLB", use_selection=True,
                              export_yup=True, export_apply=True)
    rep["tris"] = tris_before
    rep["seconds"] = round(time.time() - t_start, 1)
    with open(job["report"], "w") as f:
        json.dump(rep, f, indent=1)
    print("PAINT_BAKE_OK", json.dumps({k: rep[k] for k in ("painted_fraction", "seconds")}))


# ---------------------------------------------------------------- compare renders
def setup_scene():
    scene = bpy.context.scene
    world = bpy.data.worlds.new("bg")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.8, 0.8, 0.8, 1.0)
    bg.inputs[1].default_value = 1.0
    sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
    sun.data.energy = 3.0
    sun.rotation_euler = (math.radians(45), 0, math.radians(30))
    scene.collection.objects.link(sun)
    scene.view_settings.view_transform = "Standard"
    scene.render.engine = "BLENDER_EEVEE"
    scene.eevee.taa_render_samples = 32
    scene.render.image_settings.file_format = "PNG"
    cam_data = bpy.data.cameras.new("cam")
    cam_data.type = "ORTHO"
    cam = bpy.data.objects.new("cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    return scene, cam


def render(scene, path, res):
    scene.render.resolution_x, scene.render.resolution_y = res
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    a = read_png(path)
    os.remove(path)
    return a


def mode_compare():
    from mathutils import Vector
    obj = load_glb(job["src"])
    node = base_color_node(obj)
    imgs = {k: bpy.data.images.load(job[k]) for k in ("voxel_tex", "painted_tex")}
    for im in imgs.values():
        im.colorspace_settings.name = node.image.colorspace_settings.name
    scene, cam = setup_scene()
    co, *_ = mesh_arrays(obj)
    mn, mx = co.min(0), co.max(0)
    center = (mn + mx) / 2
    radius = float(np.linalg.norm(mx - mn)) / 2
    dist = radius * 3
    cam.data.clip_end = radius * 10
    fwd = np.array(job.get("fwd", [0, 1, 0]), float)
    tmp = job["out_head"] + ".tmp.png"
    head = job["head"]
    rows_h, rows_b = [], []
    for key in ("voxel_tex", "painted_tex"):
        node.image = imgs[key]
        # head close-up, same camera as QA's thumb_head.png
        hv = np.asarray(head["view"], float)
        d = Vector((hv[0], hv[1], 0.12)).normalized()
        cam.data.ortho_scale = float(head["side"])
        cam.location = Vector(head["center"]) + d * dist
        cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
        rows_h.append(render(scene, tmp, (768, 768)))
        tiles = []
        cam.data.ortho_scale = max(mx[2] - mn[2], math.hypot(mx[0] - mn[0], mx[1] - mn[1])) * 1.08
        for ang in (0, 90, 180, 270):
            c_, s_ = math.cos(math.radians(ang)), math.sin(math.radians(ang))
            f = np.array([c_ * fwd[0] - s_ * fwd[1], s_ * fwd[0] + c_ * fwd[1], 0.0])
            d = Vector((f[0], f[1], 0.25)).normalized()
            cam.location = Vector(center.tolist()) + d * dist
            cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
            tiles.append(render(scene, tmp, (512, 512)))
        rows_b.append(np.concatenate(tiles, 1))
    write_png(np.concatenate(rows_h, 1), job["out_head"])
    write_png(np.concatenate(rows_b, 0), job["out_body"])
    print("PAINT_COMPARE_OK")


{"views": mode_views, "bake": mode_bake, "compare": mode_compare}[job["mode"]]()
