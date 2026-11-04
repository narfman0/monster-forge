// monster-forge viewer: progress sidebar, three.js model view, animation panel.
// Vanilla ES modules; three.js r179 vendored under vendor/three (see importmap).
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const STAGES = ['ref', 'mesh', 'qa', 'paint', 'lod', 'rig', 'anim'];
const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...kids) => {
  const e = Object.assign(document.createElement(tag), props);
  for (const k of kids) if (k != null) e.append(k);
  return e;
};
const fmt = (n, d = 2) => (n == null || Number.isNaN(n) ? '–' : Number(n).toFixed(d));
const kfmt = (n) => (n == null ? '?' : n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : Number(n).toLocaleString());
// localStorage under "mf.<key>", JSON-encoded; private mode / quota errors are ignored.
const LS = {
  get(k, d) { try { const v = localStorage.getItem('mf.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('mf.' + k, JSON.stringify(v)); } catch { /* ignore */ } },
};
// Auto LOD: LOD0/LOD1/LOD2 while the bounding sphere covers more than these fractions of the
// viewport height, LOD3 below the last one. RAW is never picked automatically.
const DEFAULT_LOD_RATIOS = [0.6, 0.3, 0.12];
const DEFAULT_LOD_HYST = 0.1;
const validRatios = (r) => Array.isArray(r) && r.length === 3 && r.every((x) => typeof x === 'number' && x > 0 && x <= 4)
  && r[0] > r[1] && r[1] > r[2];
const savedMode = LS.get('lodMode', 'auto');

// ------------------------------------------------------------------ state
const state = {
  index: null,
  filtered: [],
  selId: null,
  loadToken: 0,
  // 'auto' | 'raw' | 0..3: auto LOD by screen coverage, or a pinned level (sticks across
  // monsters and reloads)
  lodMode: savedMode === 'auto' || savedMode === 'raw' || [0, 1, 2, 3].includes(savedMode) ? savedMode : 'auto',
  lodRatios: validRatios(LS.get('lodRatios', null)) ? LS.get('lodRatios', null) : [...DEFAULT_LOD_RATIOS],
  lodHyst: Number.isFinite(LS.get('lodHyst', null)) ? LS.get('lodHyst', null) : DEFAULT_LOD_HYST,
  albedo: LS.get('albedo', 'painted') === 'voxel' ? 'voxel' : 'painted',
  jobs: {},            // id -> 'running' | 'queued' (from /api/jobs)
  jobList: [],
  ov: null,            // GET /api/override/<id> payload for the selected monster
};

// ------------------------------------------------------------------ three.js scene
const viewport = $('viewport');
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.0;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
viewport.append(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x1a1d23);
scene.fog = new THREE.Fog(0x1a1d23, 30, 120);

const camera = new THREE.PerspectiveCamera(40, 1, 0.02, 500);
camera.position.set(2, 1.5, -3);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.target.set(0, 0.9, 0);

const hemi = new THREE.HemisphereLight(0xdfe8ff, 0x3a3328, 1.1);
scene.add(hemi);
const sun = new THREE.DirectionalLight(0xffffff, 2.2);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
sun.shadow.bias = -0.0005;
sun.shadow.normalBias = 0.02;
sun.shadow.radius = 4;
scene.add(sun, sun.target);
const fill = new THREE.DirectionalLight(0xb8c8ff, 0.5);
scene.add(fill);

// Procedural studio environment (no HDR file): soft gradient dome + two light panels,
// prefiltered with PMREM so metal/roughness maps read sensibly.
function makeEnvironment() {
  const envScene = new THREE.Scene();
  const dome = new THREE.Mesh(
    new THREE.SphereGeometry(10, 32, 16),
    new THREE.ShaderMaterial({
      side: THREE.BackSide,
      uniforms: { top: { value: new THREE.Color(0x9fb4d6) }, bottom: { value: new THREE.Color(0x2a2620) } },
      vertexShader: 'varying vec3 vP; void main(){ vP = normalize(position); gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }',
      fragmentShader: 'uniform vec3 top; uniform vec3 bottom; varying vec3 vP; void main(){ float t = smoothstep(-0.4, 0.8, vP.y); gl_FragColor = vec4(mix(bottom, top, t), 1.0); }',
    }));
  envScene.add(dome);
  const panel = (x, y, z, s, i) => {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(s, s), new THREE.MeshBasicMaterial({ color: new THREE.Color(i, i, i) }));
    m.position.set(x, y, z);
    m.lookAt(0, 0, 0);
    envScene.add(m);
  };
  panel(5, 7, -4, 5, 4);
  panel(-6, 3, 5, 4, 1.5);
  const pmrem = new THREE.PMREMGenerator(renderer);
  const tex = pmrem.fromScene(envScene, 0.04).texture;
  pmrem.dispose();
  return tex;
}
const envTex = makeEnvironment();
scene.environment = envTex;
scene.environmentIntensity = 0.6;

// Ground: shadow catcher + 1 m grid, rebuilt per model so big monsters fit.
const ground = new THREE.Mesh(new THREE.PlaneGeometry(400, 400),
  new THREE.MeshStandardMaterial({ color: 0x2b3038, roughness: 1, metalness: 0, envMapIntensity: 0.25 }));
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
scene.add(ground);
let grid = null;
function setGrid(extent) {
  if (grid) { scene.remove(grid); grid.geometry.dispose(); grid.material.dispose(); }
  const size = Math.max(6, Math.ceil(extent * 3 / 2) * 2);
  grid = new THREE.GridHelper(size, size, 0x7a8394, 0x444b57);
  grid.material.transparent = true;
  grid.material.opacity = 0.8;
  grid.position.y = 0.001;
  scene.add(grid);
}
setGrid(4);

// 1.8 m humanoid silhouette for scale (feet at y=0).
function makeScaleRef() {
  const g = new THREE.Group();
  const mat = new THREE.MeshStandardMaterial({ color: 0x6aa8ff, transparent: true, opacity: 0.35, roughness: 1, depthWrite: false });
  const add = (geo, x, y, z, rz = 0) => { const m = new THREE.Mesh(geo, mat); m.position.set(x, y, z); m.rotation.z = rz; g.add(m); };
  add(new THREE.SphereGeometry(0.11, 16, 12), 0, 1.69, 0);                       // head (top at 1.80)
  add(new THREE.CapsuleGeometry(0.17, 0.42, 4, 12), 0, 1.21, 0);                 // torso
  add(new THREE.CapsuleGeometry(0.065, 0.78, 4, 8), -0.1, 0.45, 0);              // legs
  add(new THREE.CapsuleGeometry(0.065, 0.78, 4, 8), 0.1, 0.45, 0);
  add(new THREE.CapsuleGeometry(0.05, 0.56, 4, 8), -0.27, 1.12, 0, -0.18);       // arms
  add(new THREE.CapsuleGeometry(0.05, 0.56, 4, 8), 0.27, 1.12, 0, 0.18);
  // 1 m tick: a short bar at 1.0 m height beside the figure
  const tick = new THREE.Mesh(new THREE.BoxGeometry(0.25, 0.012, 0.012), new THREE.MeshBasicMaterial({ color: 0xffd36a }));
  tick.position.set(-0.48, 1.0, 0);
  const pole = new THREE.Mesh(new THREE.BoxGeometry(0.012, 1.0, 0.012), new THREE.MeshBasicMaterial({ color: 0xffd36a }));
  pole.position.set(-0.48, 0.5, 0);
  g.add(tick, pole);
  return g;
}
const scaleRef = makeScaleRef();
scene.add(scaleRef);

// Current model state
const view = {
  root: null, mixer: null, clips: [], action: null, clipIdx: -1,
  skelHelper: null, playing: true, cycle: false, cycleT: 0, box: null,
  scrubbing: false, lod: 0, lodMesh: null, lodGeoms: {}, entry: null,
  // RAW tier (model_hi[_tex].glb): loaded on demand into its own group
  raw: null, rawPromise: null, showRaw: false,
  coverage: null, coveragePx: 0, autoLevel: null, autoT: 0,
  overlays: [], albedoTex: {},
  socketBones: [],     // [{name, bone}] socket bones of the loaded skeleton (not bone_<n>)
  markers: [], props: [],
};
const clock = new THREE.Clock();
const loader = new GLTFLoader();

function resize() {
  const w = viewport.clientWidth, h = viewport.clientHeight;
  renderer.setSize(w, h, false);
  renderer.domElement.style.width = w + 'px';
  renderer.domElement.style.height = h + 'px';
  camera.aspect = w / Math.max(h, 1);
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(viewport);
resize();

function animate() {
  const dt = clock.getDelta();
  if (view.mixer && view.playing && !view.scrubbing) view.mixer.update(dt);
  updateSocketMarkers();
  if (view.cycle && view.clips.length > 1 && view.playing) {
    view.cycleT += dt;
    if (view.cycleT >= Number($('cycle-secs').value || 4)) { view.cycleT = 0; nextClip(); }
  }
  if ($('t-rotate').checked && view.root) view.root.rotation.y += dt * 0.5;
  if (view.raw && view.root) view.raw.group.rotation.y = view.root.rotation.y;
  updateTimeUI();
  controls.update();
  view.autoT += dt;
  if (view.autoT >= 0.1) { view.autoT = 0; autoLodTick(); }  // throttled: 10 Hz
  renderer.render(scene, camera);
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);

function disposeModel() {
  if (view.mixer) { view.mixer.stopAllAction(); view.mixer.uncacheRoot(view.root); }
  if (view.skelHelper) { scene.remove(view.skelHelper); view.skelHelper.dispose?.(); }
  for (const [k, g] of Object.entries(view.lodGeoms)) if (k !== '0') g.dispose();
  clearWireOverlay();
  clearSocketMarkers();
  clearProps();
  if (view.raw) { scene.remove(view.raw.group); disposeTree(view.raw.group); }
  for (const t of Object.values(view.albedoTex)) t.dispose();
  if (view.root) {
    scene.remove(view.root);
    view.root.traverse((o) => {
      if (!o.isMesh) return;
      o.geometry.dispose();
      for (const m of [].concat(o.material)) {
        for (const t of Object.values(m.userData.maps || {})) t?.dispose?.();
        m.userData.origMap?.dispose?.();
        m.dispose();
      }
    });
  }
  Object.assign(view, { root: null, mixer: null, clips: [], action: null, clipIdx: -1, skelHelper: null, box: null, cycleT: 0,
    lod: 0, lodMesh: null, lodGeoms: {}, raw: null, rawPromise: null, showRaw: false, autoLevel: null, overlays: [],
    albedoTex: {}, socketBones: [], markers: [], props: [] });
}

function disposeTree(root) {
  root.traverse((o) => {
    if (!o.isMesh || o.userData.overlay) return;
    o.geometry.dispose();
    for (const m of [].concat(o.material)) {
      for (const k of MAP_KEYS) m[k]?.dispose?.();
      m.dispose();
    }
  });
}

const MAP_KEYS = ['map', 'normalMap', 'roughnessMap', 'metalnessMap', 'aoMap', 'emissiveMap'];
function applyMaterialToggles() {
  if (!view.root) return;
  const wire = $('t-wire').checked, tex = $('t-tex').checked;
  const visit = (o) => {
    if (!o.isMesh || o.userData.overlay) return;
    for (const m of [].concat(o.material)) {
      m.wireframe = wire;
      for (const k of MAP_KEYS) {
        if (!(k in m.userData.maps)) continue;
        const want = tex ? m.userData.maps[k] : null;
        if (m[k] !== want) { m[k] = want; m.needsUpdate = true; }
      }
      const c = tex ? m.userData.color : 0xb0b4bb;
      m.color.set(c);
    }
  };
  view.root.traverse(visit);
  view.raw?.group.traverse(visit);
}

function frameCamera() {
  if (!view.box) return;
  // Include the 1.8 m figure unless the model is tiny (it would shrink to a speck).
  const box = view.box.clone();
  const m = view.box.getSize(new THREE.Vector3());
  if (scaleRef.visible && Math.max(m.x, m.y, m.z) >= 0.5) box.union(new THREE.Box3().setFromObject(scaleRef));
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.length() / 2, 0.3);
  const dist = radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.05;
  // Models face -Z: look at the front three-quarter.
  const dir = new THREE.Vector3(0.75, 0.45, -1).normalize();
  controls.target.copy(center);
  camera.position.copy(center).addScaledVector(dir, dist);
  camera.near = Math.max(dist / 200, 0.01);
  camera.far = dist * 40;
  camera.updateProjectionMatrix();
  controls.update();
}

function placeLights(box) {
  const size = box.getSize(new THREE.Vector3());
  const r = Math.max(size.x, size.y, size.z, 1.8);
  const c = box.getCenter(new THREE.Vector3());
  sun.position.set(c.x + r * 1.2, r * 2.2, c.z - r * 1.4);
  sun.target.position.copy(c);
  const s = sun.shadow.camera;
  s.left = s.bottom = -r * 1.5; s.right = s.top = r * 1.5;
  s.near = 0.1; s.far = r * 6;
  s.updateProjectionMatrix();
  fill.position.set(c.x - r * 2, r, c.z + r * 2);
  scene.fog.near = r * 8; scene.fog.far = r * 30;
}

function triCount(root) {
  let n = 0;
  root.traverse((o) => {
    if (!o.isMesh || o.userData.overlay) return;
    const g = o.geometry;
    n += (g.index ? g.index.count : g.attributes.position.count) / 3;
  });
  return Math.round(n);
}

async function loadGLB(url) {
  return await loader.loadAsync(url);
}

// External clip merge: anims/<clip>.glb. Clips are used as-is when their track targets
// (node/bone names) exist in the loaded model; tracks for unknown nodes are dropped.
function adaptClip(clip, root, fallbackName) {
  const names = new Set();
  root.traverse((o) => names.add(o.name));
  const kept = clip.tracks.filter((t) => names.has(THREE.PropertyBinding.parseTrackName(t.name).nodeName));
  const ratio = clip.tracks.length ? kept.length / clip.tracks.length : 0;
  const out = new THREE.AnimationClip(fallbackName || clip.name, clip.duration, kept);
  return { clip: out, ratio };
}

async function loadModel(entry) {
  const token = ++state.loadToken;
  disposeModel();
  renderClips(entry);
  $('empty').classList.toggle('hidden', !!entry.files.model);
  $('empty-detail').textContent = entry.files.model ? '' :
    (entry.stages.ref ? 'reference image exists; run forge.py mesh' : 'run: python3 -I tools/forge.py run ' + entry.id);
  scaleRef.position.set(-1.2, 0, 0);
  if (!entry.files.model) { setHud(entry, null); renderLodBar(entry); applyAlbedo(entry); return; }
  $('loading').textContent = 'loading…';
  $('loading').classList.remove('hidden');
  let gltf;
  try {
    gltf = await loadGLB(entry.dir + entry.files.model);
  } catch (err) {
    if (token !== state.loadToken) return;
    $('loading').classList.add('hidden');
    $('empty').classList.remove('hidden');
    $('empty-detail').textContent = 'failed to load ' + entry.files.model + ': ' + err.message;
    return;
  }
  if (token !== state.loadToken) return;
  const root = gltf.scene;
  let skinned = 0, bones = 0;
  root.traverse((o) => {
    if (o.isBone) bones++;
    if (!o.isMesh) return;
    if (o.isSkinnedMesh) skinned++;
    o.castShadow = true;
    o.receiveShadow = true;
    for (const m of [].concat(o.material)) {
      m.userData.maps = {};
      for (const k of MAP_KEYS) if (m[k]) m.userData.maps[k] = m[k];
      m.userData.color = m.color.getHex();
    }
  });
  scene.add(root);
  view.root = root;
  view.mixer = new THREE.AnimationMixer(root);
  view.mixer.timeScale = Number($('speed').value);

  const clips = gltf.animations.map((c) => ({ clip: c, source: 'embedded', ok: true }));
  // External clips
  for (const name of entry.external_clips || []) {
    try {
      const ext = await loadGLB(`${entry.dir}anims/${encodeURIComponent(name)}.glb`);
      if (token !== state.loadToken) return;
      ext.animations.forEach((c, i) => {
        const label = ext.animations.length === 1 ? name : `${name}:${c.name || i}`;
        const { clip, ratio } = adaptClip(c, root, label);
        clips.push({ clip, source: 'anims/', ok: clip.tracks.length > 0, ratio });
      });
    } catch (err) {
      clips.push({ clip: new THREE.AnimationClip(name, 0, []), source: 'anims/', ok: false, error: err.message });
    }
  }
  view.clips = clips;

  root.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(root, true);
  view.box = box;
  if (skinned) {
    // Added after root, so scene.updateMatrixWorld has posed the bones before the helper
    // reads them (it draws parent-to-child lines from the bones' world matrices).
    view.skelHelper = new THREE.SkeletonHelper(root);
    view.skelHelper.setColors(new THREE.Color(0xffb347), new THREE.Color(0x7dffb0));
    view.skelHelper.visible = $('t-skel').checked;
    scene.add(view.skelHelper);
  }
  view.socketBones = findSocketBones(root);
  const size = box.getSize(new THREE.Vector3());
  setGrid(Math.max(size.x, size.z, 2));
  placeLights(box);
  scaleRef.position.set(box.min.x - 0.6, 0, (box.min.z + box.max.z) / 2); // far side from the camera
  applyMaterialToggles();
  frameCamera();
  $('loading').classList.add('hidden');
  view.info = { tris: triCount(root), size, box, skinned, bones, file: entry.files.model };
  view.entry = entry;
  const meshes = [];
  root.traverse((o) => { if (o.isMesh) meshes.push(o); });
  // LOD swap needs one mesh (LOD GLBs are geometry + LOD0's material). Rigged LODs share
  // LOD0's joint order, so a skinned mesh swaps the same way under its skeleton.
  if (meshes.length === 1) {
    view.lodMesh = meshes[0];
    view.lodGeoms = { 0: meshes[0].geometry };
  }
  view.info.lod0 = { tris: view.info.tris, file: entry.files.model };
  renderLodBar(entry);
  setHud(entry, view.info);
  updateWireOverlay();
  updateSocketMarkers(true);
  updateProps();
  applyAlbedo(entry, token);
  preloadLods(entry, token).then(() => { if (token === state.loadToken) applyMode(); });
  applyMode();
  renderClips(entry);
  const first = defaultClip();
  if (first >= 0) playClip(first);
}

// ------------------------------------------------------------------ LODs
// model_lod<k>.glb share LOD0's UVs and material; only the geometry is swapped in, so the
// loaded LOD0 material (with its textures) is reused, as an engine would. All LODs of the
// selected monster are preloaded (they are small) so auto switching is instant. RAW is the
// undecimated high-poly (model_hi.glb, clay; model_hi_tex.glb when a textured one exists),
// loaded only when picked.
function lodKeys(entry = view.entry) {
  return view.lodMesh ? Object.keys(entry?.lods || {}).map(Number).sort((a, b) => a - b) : [];
}

async function preloadLods(entry, token) {
  await Promise.all(lodKeys(entry).filter((k) => k !== 0).map(async (k) => {
    if (view.lodGeoms[k]) return;
    try {
      const g = await loadGLB(entry.dir + entry.lods[k].file);
      let geo = null;
      g.scene.traverse((o) => { if (o.isMesh && !geo) geo = o.geometry; });
      if (token !== state.loadToken) { disposeTree(g.scene); return; }
      if (geo) view.lodGeoms[k] = geo;
    } catch (err) {
      if (token === state.loadToken) $('hud-stats').textContent += `\nLOD${k}: ${err.message}`;
    }
  }));
  if (token === state.loadToken) renderLodBar(entry);
}

// Show one LOD (geometry swap under LOD0's material); hides RAW.
async function setLod(k, token = state.loadToken) {
  const entry = view.entry;
  if (!view.root) return;
  if (k !== 0 && (!view.lodMesh || !entry?.lods?.[k])) k = 0;
  if (view.lodMesh && !view.lodGeoms[k]) {
    try {
      const g = await loadGLB(entry.dir + entry.lods[k].file);
      if (token !== state.loadToken) { disposeTree(g.scene); return; }
      let geo = null;
      g.scene.traverse((o) => { if (o.isMesh && !geo) geo = o.geometry; });
      if (!geo) return;
      view.lodGeoms[k] = geo;
    } catch (err) {
      $('hud-stats').textContent += `\nLOD${k}: ${err.message}`;
      return;
    }
  }
  const changed = view.showRaw || view.lod !== k;
  if (view.lodMesh) view.lodMesh.geometry = view.lodGeoms[k];
  view.lod = k;
  view.showRaw = false;
  view.root.visible = true;
  if (view.raw) view.raw.group.visible = false;
  syncRigHelpers();
  view.info.tris = triCount(view.root);
  view.info.file = k ? entry.lods[k].file : view.info.lod0.file;
  if (changed) { setHud(entry, view.info); updateWireOverlay(); }
  renderLodBar(entry);
}

// QA transform (tools/blender_qa.py: yaw about up, stance-level rotation, uniform scale;
// the caller then puts the origin at the feet) re-applied to the raw-frame high-poly so it
// lines up with LOD0. Blender (x, y, z) is glTF (x, z, -y). null = already in the final frame.
function rawAlignMatrix(box, qa) {
  const lod = view.box, ls = lod.getSize(new THREE.Vector3()), bs = box.getSize(new THREE.Vector3());
  const near = (a, b, tol) => Math.abs(a - b) <= tol;
  // Already in the final frame (e.g. a textured high-poly exported after QA)?
  if (near(bs.y, ls.y, 0.03 * ls.y) && near(box.min.y, lod.min.y, 0.03 * ls.y) && near(bs.x, ls.x, 0.06 * ls.x)) return null;
  const M = new THREE.Matrix4();
  if (qa?.scale_factor) {
    M.makeRotationY(THREE.MathUtils.degToRad(qa.yaw_deg || 0));
    const n = qa.level?.enabled && qa.level.normal_before;
    if (n) {
      const q = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(n[0], n[2], -n[1]).normalize(), new THREE.Vector3(0, 1, 0));
      M.premultiply(new THREE.Matrix4().makeRotationFromQuaternion(q));
    }
    const s = qa.scale_factor;
    M.premultiply(new THREE.Matrix4().makeScale(s, s, s));
  } else {
    const s = ls.y / Math.max(bs.y, 1e-6);  // no QA record: fit the height
    M.makeScale(s, s, s);
  }
  return M;
}

function ensureRaw(entry, token) {
  if (view.raw) return Promise.resolve(view.raw);
  if (view.rawPromise) return view.rawPromise;
  const file = entry.files.hi;
  view.rawPromise = (async () => {
    $('loading').textContent = `loading RAW ${file} (${fmt((entry.files.hi_bytes || 0) / 1048576, 1)} MB)…`;
    $('loading').classList.remove('hidden');
    renderLodBar(entry);
    let gltf, man = {};
    try {
      [gltf, man] = await Promise.all([loadGLB(entry.dir + file),
        fetch(entry.dir + 'manifest.json', { cache: 'no-store' }).then((r) => (r.ok ? r.json() : {})).catch(() => ({}))]);
    } catch (err) {
      if (token === state.loadToken) {
        $('loading').classList.add('hidden');
        $('hud-stats').textContent += `\nRAW: ${err.message}`;
        view.rawPromise = null;
        renderLodBar(entry);
      }
      return null;
    }
    if (token !== state.loadToken) { disposeTree(gltf.scene); return null; }
    gltf.scene.updateMatrixWorld(true);
    const meshes = [];
    gltf.scene.traverse((o) => { if (o.isMesh) meshes.push(o); });
    const textured = entry.files.hi_textured;
    const clay = new THREE.MeshStandardMaterial({ color: 0xbab3a8, roughness: 0.62, metalness: 0, side: THREE.DoubleSide });
    const box = new THREE.Box3();
    for (const m of meshes) {
      m.geometry.applyMatrix4(m.matrixWorld);  // flatten node transforms
      if (!m.geometry.attributes.normal) m.geometry.computeVertexNormals();
      m.geometry.computeBoundingBox();
      box.union(m.geometry.boundingBox);
    }
    const M = rawAlignMatrix(box, man.qa);
    if (M) {
      // rotate + scale the vertices, then centre x/z and drop to y=0 on the exact bounds
      // (as QA does; a transformed bounding box would overestimate after rotation)
      const b2 = new THREE.Box3();
      for (const m of meshes) { m.geometry.applyMatrix4(M); m.geometry.computeBoundingBox(); b2.union(m.geometry.boundingBox); }
      const c = b2.getCenter(new THREE.Vector3());
      const T = new THREE.Matrix4().makeTranslation(-c.x, -b2.min.y, -c.z);
      for (const m of meshes) { m.geometry.applyMatrix4(T); m.geometry.computeBoundingBox(); m.geometry.computeBoundingSphere(); }
    }
    const group = new THREE.Group();
    let faces = 0;
    for (const m of meshes) {
      const mat = textured ? m.material : clay;
      if (!textured) { for (const old of [].concat(m.material)) old.dispose(); }
      const mesh = new THREE.Mesh(m.geometry, mat);
      mesh.castShadow = mesh.receiveShadow = true;
      for (const mm of [].concat(mat)) {
        mm.userData.maps = {};
        for (const k of MAP_KEYS) if (mm[k]) mm.userData.maps[k] = mm[k];
        mm.userData.color = mm.color.getHex();
      }
      faces += (m.geometry.index ? m.geometry.index.count : m.geometry.attributes.position.count) / 3;
      group.add(mesh);
    }
    group.visible = false;
    group.rotation.y = view.root?.rotation.y ?? 0;
    scene.add(group);
    view.raw = { group, faces: Math.round(faces), file, textured, aligned: M ? (man.qa?.scale_factor ? 'qa' : 'fit') : 'as-is' };
    $('loading').classList.add('hidden');
    applyMaterialToggles();
    return view.raw;
  })();
  return view.rawPromise;
}

async function showRaw(token = state.loadToken) {
  const entry = view.entry;
  if (!entry?.files.hi || !view.root) return;
  const raw = await ensureRaw(entry, token);
  if (!raw || token !== state.loadToken || state.lodMode !== 'raw') { renderLodBar(entry); return; }
  view.showRaw = true;
  raw.group.visible = true;
  view.root.visible = false;
  syncRigHelpers();
  view.info.tris = raw.faces;
  view.info.file = raw.file + (raw.aligned === 'as-is' ? '' : ` (aligned: ${raw.aligned})`);
  setHud(entry, view.info);
  updateWireOverlay();
  renderLodBar(entry);
}

// Apply the current mode to the loaded monster.
function applyMode() {
  if (!view.root) return;
  const m = state.lodMode;
  if (m === 'raw' && view.entry?.files.hi) { showRaw(); return; }
  if (m === 'auto' || m === 'raw') {
    view.autoLevel = null;
    autoLodTick();
    if (view.showRaw || (m === 'raw')) setLod(view.autoLevel ?? 0);
    return;
  }
  const keys = lodKeys();
  setLod(keys.includes(m) ? m : (keys.filter((k) => k <= m).pop() ?? 0));
}

function setMode(m) {
  state.lodMode = m;
  LS.set('lodMode', m);
  applyMode();
  renderLodBar(view.entry);
}

function cycleLod() {
  const keys = lodKeys();
  if (keys.length < 2) return;
  const cur = typeof state.lodMode === 'number' ? state.lodMode : view.lod;
  setMode(keys[(keys.indexOf(cur) + 1) % keys.length]);
}

// Per-monster lod_screen_ratios (overrides file) win over the viewer settings.
function monsterRatios() {
  const o = state.ov && state.ov.id === state.selId ? state.ov.fields?.lod_screen_ratios?.override : null;
  return validRatios(o) ? o : null;
}
const activeRatios = () => monsterRatios() || state.lodRatios;

// Screen coverage: the LOD0 bounding sphere's projected diameter as a fraction of the
// viewport height, tan(asin(r / d)) / tan(fov / 2) (exact for a sphere on the view axis).
const _sph = new THREE.Sphere();
const _yAxis = new THREE.Vector3(0, 1, 0);
function screenCoverage() {
  if (!view.box) return null;
  view.box.getBoundingSphere(_sph);
  if (view.root) _sph.center.applyAxisAngle(_yAxis, view.root.rotation.y);  // turntable spins about the origin
  const d = camera.position.distanceTo(_sph.center);
  const half = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
  if (d <= _sph.radius * 1.0001) return 10;
  return Math.min(10, Math.tan(Math.asin(_sph.radius / d)) / half);
}

// Level for coverage c with hysteresis around the switch points: move finer only once c
// is h above the next point up, coarser once it is h below the current one.
function pickAutoLevel(c, cur) {
  const t = activeRatios(), h = state.lodHyst;
  if (cur == null) { const i = t.findIndex((x) => c > x); return i < 0 ? 3 : i; }
  let L = cur;
  while (L > 0 && c > t[L - 1] * (1 + h)) L--;
  while (L < 3 && c < t[L] * (1 - h)) L++;
  return L;
}

function autoLodTick() {
  if (!view.root) { renderLodReadout(); return; }
  const c = screenCoverage();
  view.coverage = c;
  view.coveragePx = c == null ? 0 : c * viewport.clientHeight;
  if (c != null) view.autoLevel = pickAutoLevel(c, view.autoLevel);
  if (state.lodMode === 'auto' && view.autoLevel != null && view.lodMesh) {
    const keys = lodKeys();
    const want = keys.filter((k) => k <= view.autoLevel).pop() ?? 0;
    if ((want !== view.lod || view.showRaw) && view.lodGeoms[want]) setLod(want);
  }
  renderLodReadout();
}

let lastReadout = '';
function renderLodReadout() {
  const r = $('lod-readout');
  let txt = '', title = '';
  if (view.root) {
    const cur = view.showRaw ? 'RAW' : `LOD${view.lod}`;
    const cov = view.coverage == null ? '–' : view.coverage >= 10 ? 'inside' : `${Math.round(view.coverage * 100)}%`;
    const auto = view.autoLevel == null ? '' : `LOD${view.autoLevel}`;
    const src = monsterRatios() ? ' ✎' : '';
    txt = state.lodMode === 'auto' ? `${cur} · cov ${cov}${src}` : `${cur} (manual) · cov ${cov} → auto ${auto}${src}`;
    title = `bounding sphere covers ${cov} of the viewport height (${Math.round(view.coveragePx)} px)\n` +
      `switch points ${activeRatios().map((x) => Math.round(x * 100) + '%').join(' / ')}` +
      `${monsterRatios() ? ' (this monster: lod_screen_ratios)' : ''}, hysteresis ±${Math.round(state.lodHyst * 100)}%`;
  }
  if (txt !== lastReadout) { r.textContent = txt; r.title = title; lastReadout = txt; }
}

function renderLodBar(entry) {
  const bar = $('lod-bar');
  bar.replaceChildren();
  if (!entry || !view.root) { renderLodReadout(); return; }
  const lods = entry.lods || {};
  const keys = lodKeys(entry);
  const mode = state.lodMode;
  const btn = (label, small, title, on, active, onclick) => {
    const b = el('button', { title }, label, small != null ? el('small', { textContent: small }) : null);
    b.classList.toggle('on', on);
    b.classList.toggle('active', active && !on);
    b.onclick = onclick;
    bar.append(b);
    return b;
  };
  if (keys.length >= 2) {
    btn('auto', null, 'pick the LOD by screen coverage (a); ⚙ sets the switch points', mode === 'auto', false, () => setMode('auto'));
  }
  if (entry.files.hi) {
    const loading = view.rawPromise && !view.raw;
    btn('RAW', loading ? 'loading…' : kfmt(entry.hi_faces),
      `${entry.files.hi}: undecimated high-poly, ${Number(entry.hi_faces || 0).toLocaleString()} tris, ` +
      `${fmt((entry.files.hi_bytes || 0) / 1048576, 1)} MB${entry.files.hi_textured ? '' : ', no texture (clay)'}; loaded on demand`,
      mode === 'raw', view.showRaw, () => setMode('raw'));
  }
  if (keys.length >= 2) {
    for (const k of keys) {
      const ready = !!view.lodGeoms[k];
      const b = btn(`LOD${k}`, Number(lods[k].tris).toLocaleString(), `${lods[k].file} (ratio ${lods[k].ratio})${ready ? '' : ', loading'}`,
        mode === k, !view.showRaw && view.lod === k, () => setMode(k));
      if (!ready) b.classList.add('pending');
    }
  }
  if (keys.length >= 2) {
    const g = el('button', { title: 'auto LOD switch points', className: 'gear', textContent: '⚙' });
    g.onclick = (e) => { e.stopPropagation(); $('lod-settings').classList.toggle('hidden'); renderLodSettings(); };
    bar.append(g);
  }
  renderLodReadout();
}

// Settings popover: global switch points (localStorage) + hysteresis; a monster's
// lod_screen_ratios override is shown and takes precedence.
function renderLodSettings() {
  const box = $('lod-settings');
  if (box.classList.contains('hidden')) return;
  box.replaceChildren();
  box.append(el('div', { className: 'hd', textContent: 'auto LOD switch points' }),
    el('div', { className: 'muted', textContent: '% of viewport height covered by the bounding sphere' }));
  const inputs = [];
  const save = () => {
    const r = inputs.slice(0, 3).map((i) => Number(i.value) / 100);
    const h = Number(inputs[3].value) / 100;
    const ok = validRatios(r) && h >= 0 && h < 0.5;
    msg.textContent = ok ? '' : 'need decreasing values in (0, 400] and hysteresis 0-49';
    if (!ok) return;
    state.lodRatios = r; state.lodHyst = h;
    LS.set('lodRatios', r); LS.set('lodHyst', h);
    view.autoLevel = null;
  };
  const row = (label, val) => {
    const i = el('input', { type: 'number', min: 0, max: 400, step: 1, value: String(Math.round(val * 1000) / 10) });
    i.oninput = save;
    inputs.push(i);
    box.append(el('label', {}, el('span', { textContent: label }), i, '%'));
  };
  state.lodRatios.forEach((x, k) => row(`LOD${k} above`, x));
  box.append(el('div', { className: 'muted', textContent: `LOD3 below LOD2's point; RAW only by hand` }));
  row('hysteresis ±', state.lodHyst);
  const msg = el('div', { className: 'err' });
  const reset = el('button', { textContent: 'reset' });
  reset.onclick = () => {
    state.lodRatios = [...DEFAULT_LOD_RATIOS]; state.lodHyst = DEFAULT_LOD_HYST;
    LS.set('lodRatios', state.lodRatios); LS.set('lodHyst', state.lodHyst);
    view.autoLevel = null;
    renderLodSettings();
  };
  box.append(msg, el('div', { className: 'row' }, reset));
  const mr = monsterRatios();
  if (mr) {
    box.append(el('div', { className: 'note', textContent:
      `${state.selId}: lod_screen_ratios ${mr.join(', ')} (overrides file) takes precedence` }));
  }
}
document.addEventListener('click', (e) => {
  const box = $('lod-settings');
  if (!box.classList.contains('hidden') && !e.composedPath().includes(box)) box.classList.add('hidden');
});

// Wireframe on shaded: a line overlay sharing the visible mesh's geometry. three.js builds
// the wireframe index lazily on first draw, so nothing is built until this is switched on.
// A skinned mesh gets a SkinnedMesh overlay bound to the same skeleton and bind matrix, so
// the lines deform with the clip (a plain Mesh would draw the bind pose).
const overlayMat = new THREE.MeshBasicMaterial({ color: 0x0d1018, wireframe: true, transparent: true, opacity: 0.35, depthWrite: false });
function clearWireOverlay() {
  for (const o of view.overlays) o.parent?.remove(o);
  view.overlays = [];
}
function updateWireOverlay() {
  clearWireOverlay();
  const on = $('t-wireo').checked;
  const roots = [view.root, view.raw?.group].filter(Boolean);
  for (const r of roots) {
    r.traverse((o) => {
      if (!o.isMesh || o.userData.overlay) return;
      for (const m of [].concat(o.material)) { m.polygonOffset = on; m.polygonOffsetFactor = 1; m.polygonOffsetUnits = 1; }
    });
  }
  if (!on) return;
  const target = view.showRaw ? view.raw?.group : view.root;
  const meshes = [];
  target?.traverse((o) => { if (o.isMesh && !o.userData.overlay) meshes.push(o); });
  for (const m of meshes) {
    let w;
    if (m.isSkinnedMesh) {
      // Child of the skinned mesh in 'attached' mode: its own matrixWorld cancels out in the
      // skinning shader, so only the shared skeleton and bind matrix decide the pose.
      w = new THREE.SkinnedMesh(m.geometry, overlayMat);
      w.bindMode = m.bindMode;
      w.bind(m.skeleton, m.bindMatrix);
      w.frustumCulled = false;  // the bind-pose bounds do not follow the clip
    } else {
      w = new THREE.Mesh(m.geometry, overlayMat);
    }
    w.userData.overlay = true;
    w.renderOrder = 1;
    w.raycast = () => {};
    m.add(w);
    view.overlays.push(w);
  }
}

// Albedo toggle (paint stage): with textures/base_color_painted.png and _voxel.png both
// present, swap the base colour map on LOD0's material (shared by LOD1-3).
const texLoader = new THREE.TextureLoader();
async function applyAlbedo(entry, token = state.loadToken) {
  const f = entry.files;
  const both = !!(f.base_color_painted && f.base_color_voxel);
  const bar = $('albedo-bar');
  bar.classList.toggle('hidden', !both);
  if (!both || !view.root) return;
  bar.replaceChildren(el('span', { className: 'muted', textContent: 'albedo' }));
  for (const w of ['painted', 'voxel']) {
    const b = el('button', { textContent: w, title: f['base_color_' + w] });
    b.classList.toggle('on', state.albedo === w);
    b.onclick = () => { state.albedo = w; LS.set('albedo', w); applyAlbedo(entry); };
    bar.append(b);
  }
  const which = state.albedo;
  let tex = view.albedoTex[which];
  if (!tex) {
    try { tex = await texLoader.loadAsync(entry.dir + f['base_color_' + which]); } catch (err) {
      $('hud-stats').textContent += `\nalbedo ${which}: ${err.message || err}`;
      return;
    }
    if (token !== state.loadToken) { tex.dispose(); return; }
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.flipY = false;  // glTF UV convention
    view.albedoTex[which] = tex;
  }
  if (state.albedo !== which) return;
  view.root.traverse((o) => {
    if (!o.isMesh || o.userData.overlay) return;
    for (const m of [].concat(o.material)) {
      if (!('origMap' in m.userData)) m.userData.origMap = m.userData.maps.map || null;
      const orig = m.userData.origMap;
      if (!orig) continue;
      tex.wrapS = orig.wrapS; tex.wrapT = orig.wrapT; tex.channel = orig.channel;
      tex.anisotropy = orig.anisotropy; tex.minFilter = orig.minFilter; tex.magFilter = orig.magFilter;
      m.userData.maps.map = tex;
    }
  });
  applyMaterialToggles();
}

function setHud(entry, info) {
  $('hud-title').textContent = entry.name;
  const lines = [`${entry.body_plan} · ${entry.size} · CR ${entry.cr} · scale_m ${entry.scale_m}`];
  if (info) {
    const s = info.size;
    lines.push(`${info.file} · ${info.tris.toLocaleString()} tris${view.showRaw && !view.raw?.textured ? ' (clay: no texture)' : ''}`);
    lines.push(`bounds W ${fmt(s.x)} × H ${fmt(s.y)} × L ${fmt(s.z)} m   (grid 1 m)`);
    let rig = info.skinned ? `skeleton: ${info.bones} bones` : 'no skeleton';
    if (info.skinned && !entry.stages.rig) rig += '  (manifest has no rig stage)';
    if (!info.skinned && entry.stages.rig) rig += '  (manifest says rigged!)';
    lines.push(rig);
    const sk = socketSummary(entry, info.skinned);
    if (sk) lines.push(sk);
  }
  $('hud-stats').textContent = lines.join('\n');
}

// ------------------------------------------------------------------ animation panel
function playClip(i) {
  const c = view.clips[i];
  if (!c || !c.ok || !view.mixer) return;
  const prev = view.action;
  const action = view.mixer.clipAction(c.clip);
  action.reset();
  setLoop(action);
  action.enabled = true;
  action.paused = false;
  if (prev && prev !== action) { action.crossFadeFrom(prev, 0.2, false); }
  action.play();
  view.action = action;
  view.clipIdx = i;
  view.cycleT = 0;
  view.playing = true;
  syncPlayButton();
  highlightClip();
}
// The clip a monster opens on: walking if it has one, else the first usable clip.
const DEFAULT_CLIPS = ['walk', 'move', 'fly', 'idle'];
function defaultClip() {
  const base = (c) => c.clip.name.split(':').pop().toLowerCase();
  for (const name of DEFAULT_CLIPS) {
    const i = view.clips.findIndex((c) => c.ok && base(c) === name);
    if (i >= 0) return i;
  }
  return view.clips.findIndex((c) => c.ok);
}
function restPose() {
  if (view.mixer) view.mixer.stopAllAction();
  view.action = null;
  view.clipIdx = -1;
  view.cycle = false;
  view.playing = false;
  syncPlayButton();
  highlightClip();
}
function setLoop(action) {
  if ($('t-loop').checked) { action.setLoop(THREE.LoopRepeat, Infinity); action.clampWhenFinished = false; }
  else { action.setLoop(THREE.LoopOnce, 1); action.clampWhenFinished = true; }
}
function nextClip(step = 1) {
  const n = view.clips.length;
  if (!n) return;
  for (let k = 1; k <= n; k++) {
    const i = (((view.clipIdx + step * k) % n) + n) % n;
    if (view.clips[i].ok) { playClip(i); return; }
  }
}
function togglePlay() {
  if (!view.action) { nextClip(); return; }
  if (view.playing && !view.action.isRunning()) { view.action.reset().play(); return; }
  view.playing = !view.playing;
  syncPlayButton();
}
function syncPlayButton() {
  $('b-play').textContent = view.playing ? 'pause' : 'play';
  $('b-cycle').classList.toggle('on', view.cycle);
}
function updateTimeUI() {
  const a = view.action;
  if (!a) { $('time-val').textContent = '0.00 / 0.00'; return; }
  const d = a.getClip().duration || 0;
  const t = d ? (a.time % d + d) % d : 0;
  if (!view.scrubbing) $('scrub').value = d ? String(t / d) : '0';
  $('time-val').textContent = `${fmt(t)} / ${fmt(d)}`;
}
function highlightClip() {
  [...$('clips').children].forEach((li, i) => li.classList.toggle('sel', i === view.clipIdx));
  [...$('anim-bar').children].forEach((b) => b.classList.toggle('on', Number(b.dataset.clip) === view.clipIdx));
}

// Toolbar selector: the animation_set in order (missing clips greyed), then any other clip.
function renderAnimBar(entry) {
  const bar = $('anim-bar');
  bar.replaceChildren();
  const expected = entry.expected_clips || [];
  if (!view.clips.length && !expected.length) return;
  const btn = (label, title, i, extra) => {
    const b = el('button', { title, textContent: label, className: extra ? 'extra' : '' });
    b.dataset.clip = i;
    b.disabled = i === null;
    if (i !== null) b.onclick = () => (i < 0 ? restPose() : playClip(i));
    bar.append(b);
  };
  const usable = view.clips.some((c) => c.ok);
  if (usable) btn('rest', 'stop and show the bind pose (0)', -1);
  const info = (i) => `${fmt(view.clips[i].clip.duration)}s, ${view.clips[i].source}`;
  for (const name of expected) {
    const i = view.clips.findIndex((c) => c.ok && c.clip.name === name);
    btn(name, i >= 0 ? info(i) : `${name}: no clip yet`, i >= 0 ? i : null);
  }
  view.clips.forEach((c, i) => {
    if (!c.ok || expected.includes(c.clip.name)) return;
    btn(c.clip.name, `${info(i)}; not in animation_set`, i, true);
  });
}

function renderClips(entry) {
  const ul = $('clips');
  ul.replaceChildren();
  view.clips.forEach((c, i) => {
    const li = el('li', { title: c.error || (c.ratio != null && c.ratio < 1 ? `${Math.round(c.ratio * 100)}% of tracks matched` : '') },
      el('span', { textContent: c.clip.name + (c.ok ? '' : ' (unusable)') }),
      el('span', { className: 'src', textContent: `${c.source} ${fmt(c.clip.duration)}s` }));
    if (!c.ok) li.style.opacity = 0.45;
    li.onclick = () => playClip(i);
    ul.append(li);
  });
  renderAnimBar(entry);
  highlightClip();
  const usable = view.clips.filter((c) => c.ok).length;
  $('anim-status').textContent = !entry.files.model ? 'no model' :
    usable ? `${usable} clip${usable > 1 ? 's' : ''} loaded` :
    (entry.stages.rig ? 'rigged; no clips yet' : 'not rigged; no clips yet');
  $('anim-controls').classList.toggle('disabled', usable === 0);

  // Expected set vs found (from index + what actually loaded)
  const loaded = new Set(view.clips.filter((c) => c.ok).map((c) => c.clip.name));
  const found = new Set([...(entry.found_clips || []), ...loaded]);
  const box = $('expected');
  box.replaceChildren();
  for (const name of entry.expected_clips || []) {
    const has = found.has(name);
    const chip = el('span', { className: 'chip ' + (has ? 'found' : 'missing'), textContent: name,
      title: has ? 'click to play' : 'missing' });
    if (has) chip.onclick = () => { const i = view.clips.findIndex((c) => c.clip.name === name && c.ok); if (i >= 0) playClip(i); };
    box.append(chip);
  }
  for (const name of found) {
    if ((entry.expected_clips || []).includes(name)) continue;
    box.append(el('span', { className: 'chip extra', textContent: name, title: 'not in animation_set' }));
  }
}

// ------------------------------------------------------------------ sockets and props
// Socket bones (docs/equipment.md) are the bones the rig stage renamed or added; SkinTokens
// bones are all bone_<n>, so any other bone name is a socket. The manifest's rig.sockets
// (index rig_sockets) names the same set; the HUD flags any disagreement.
const SKIN_BONE = /^bone_\d+$/;
function findSocketBones(root) {
  const out = [];
  root.traverse((o) => { if (o.isBone && !SKIN_BONE.test(o.name)) out.push({ name: o.name, bone: o }); });
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

function socketSummary(entry, skinned) {
  const inGlb = view.socketBones.map((s) => s.name);
  const want = entry.sockets || [];
  if (!inGlb.length && !want.length) return '';
  if (!skinned) return `sockets wanted: ${want.join(' ')}   (not rigged)`;
  const missing = want.filter((s) => !inGlb.includes(s));
  let line = `sockets: ${inGlb.join(' ') || 'none'}`;
  if (missing.length) line += `   missing: ${missing.join(' ')}`;
  const man = Object.keys(entry.rig_sockets || {});
  const off = man.filter((s) => !inGlb.includes(s));
  if (off.length) line += `   (manifest only: ${off.join(' ')})`;
  return line;
}

// Model height scales the marker and label sizes, so they read the same on a goblin and a dragon.
const modelHeight = () => (view.box ? Math.max(view.box.max.y - view.box.min.y, 0.3) : 1.8);

function labelSprite(text, height) {
  const c = document.createElement('canvas');
  const ctx = c.getContext('2d');
  const font = '600 40px system-ui, sans-serif';
  ctx.font = font;
  c.width = Math.ceil(ctx.measureText(text).width) + 24;
  c.height = 56;
  ctx.font = font;
  ctx.fillStyle = 'rgba(12,14,18,0.78)';
  ctx.fillRect(0, 0, c.width, c.height);
  ctx.fillStyle = '#ffd36a';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, 12, c.height / 2 + 2);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, depthWrite: false, toneMapped: false }));
  sp.scale.set(height * c.width / c.height, height, 1);
  sp.center.set(0, 0);  // bottom-left corner at the anchor
  return sp;
}

// Gizmo in the bone's frame, in prop-space terms: green +Y (along the blade, toward the
// tip), blue -Z (the edge / the way an arrow points), short red +X.
function makeSocketMarker(name, size) {
  const g = new THREE.Group();
  const arrow = (x, y, z, color, len) => new THREE.ArrowHelper(new THREE.Vector3(x, y, z), new THREE.Vector3(), len, color, len * 0.25, len * 0.12);
  g.add(arrow(0, 1, 0, 0x5ee07a, size), arrow(0, 0, -1, 0x4fa8ff, size * 0.7), arrow(1, 0, 0, 0xff5a5a, size * 0.35));
  const label = labelSprite(name, size * 0.32);
  label.position.set(0, size * 0.15, 0);
  label.userData.label = true;
  g.add(label);
  g.traverse((o) => {
    o.renderOrder = 20;
    if (o.material) { o.material.depthTest = false; o.material.depthWrite = false; o.material.transparent = true; }
  });
  return g;
}

function clearSocketMarkers() {
  for (const m of view.markers) {
    scene.remove(m.obj);
    m.obj.traverse((o) => { o.material?.map?.dispose(); o.material?.dispose(); });
  }
  view.markers = [];
}

// Markers live in the scene (not under the bones, which may carry scale) and copy each
// socket bone's world position and rotation every frame, after the mixer has posed it.
const _mp = new THREE.Vector3(), _mq = new THREE.Quaternion(), _ms = new THREE.Vector3();
function updateSocketMarkers(rebuild = false) {
  const on = $('t-sockets').checked && !!view.root;
  if (rebuild || !on) {
    clearSocketMarkers();
    if (!on) return;
    const size = THREE.MathUtils.clamp(modelHeight() * 0.12, 0.06, 0.6);
    for (const s of view.socketBones) {
      const obj = makeSocketMarker(s.name, size);
      scene.add(obj);
      view.markers.push({ obj, bone: s.bone });
    }
  }
  if (!view.markers.length) return;
  view.root.updateMatrixWorld(true);
  for (const m of view.markers) {
    m.bone.matrixWorld.decompose(_mp, _mq, _ms);
    m.obj.position.copy(_mp);
    m.obj.quaternion.copy(_mq);
    m.obj.visible = !view.showRaw;
  }
}

// Placeholder props, built in prop space (origin at the grip, +Y toward the tip, -Z the edge
// or arrow direction) for a 1.8 m wielder, scaled by scale_m / 1.8 like the runtime props,
// and added as children of the socket bone so they follow it exactly as a Godot
// BoneAttachment3D would. The bone's own world scale is divided out.
const propMat = {
  steel: new THREE.MeshStandardMaterial({ color: 0xc9ced6, metalness: 0.85, roughness: 0.3 }),
  edge: new THREE.MeshBasicMaterial({ color: 0xff5a5a }),
  grip: new THREE.MeshStandardMaterial({ color: 0x4a2f1c, roughness: 0.8 }),
  wood: new THREE.MeshStandardMaterial({ color: 0x8a5a2b, roughness: 0.7 }),
  string: new THREE.LineBasicMaterial({ color: 0xeeeeee }),
};
function makeSword() {
  const g = new THREE.Group();
  const add = (geo, mat, y) => { const m = new THREE.Mesh(geo, mat); m.position.y = y; m.castShadow = true; g.add(m); return m; };
  add(new THREE.BoxGeometry(0.012, 0.72, 0.05), propMat.steel, 0.13 + 0.36);   // blade 0.13-0.85 m, flat in YZ
  add(new THREE.BoxGeometry(0.016, 0.72, 0.006), propMat.edge, 0.49).position.z = -0.027;  // -Z cutting edge
  add(new THREE.BoxGeometry(0.03, 0.03, 0.2), propMat.steel, 0.115);           // crossguard
  add(new THREE.CylinderGeometry(0.016, 0.016, 0.2, 8), propMat.grip, 0.0);     // grip, origin at its middle
  add(new THREE.SphereGeometry(0.028, 8, 6), propMat.steel, -0.11);             // pommel
  return g;
}
function makeBow() {
  // Shortbow, ~1.1 m: the stave bows toward -Z (the arrow direction) at the grip, the tips
  // sweep back toward the archer (+Z), the string joins the tips.
  const R = 0.8, a = 0.72, pts = [];
  for (let i = 0; i <= 16; i++) { const t = -a + (2 * a * i) / 16; pts.push(new THREE.Vector3(0, R * Math.sin(t), R * (1 - Math.cos(t)))); }
  const g = new THREE.Group();
  const stave = new THREE.Mesh(new THREE.TubeGeometry(new THREE.CatmullRomCurve3(pts), 32, 0.014, 6), propMat.wood);
  stave.castShadow = true;
  const tip = pts[pts.length - 1];
  const string = new THREE.Line(new THREE.BufferGeometry().setFromPoints([pts[0], tip]), propMat.string);
  g.add(stave, string);
  return g;
}
// Each prop goes on the grip socket (grip_r/grip_l: centre of the fist, already in prop
// space, docs/equipment.md) when the rig has one, else on the hand bone (wrist frame).
const PROPS = [{ sockets: ['grip_r', 'hand_r'], make: makeSword, param: 'prop_r' },
               { sockets: ['grip_l', 'hand_l'], make: makeBow, param: 'prop_l' }];

// Real props instead of the placeholders: ?prop_r=<id>&prop_l=<id> (?prop=<id> = prop_r)
// loads out/props/<id>/model.glb (tools/props.py, already in prop space). One load per
// id, cloned per attachment; a missing file falls back to the placeholder.
const propQuery = new URLSearchParams(location.search);
const realPropId = (param) => propQuery.get(param) || (param === 'prop_r' ? propQuery.get('prop') : null);
const realProps = new Map();  // id -> Promise<THREE.Object3D | null>
function loadRealProp(id) {
  if (!realProps.has(id)) {
    const dir = `/out/props/${encodeURIComponent(id)}/`;
    realProps.set(id, Promise.all([
      loader.loadAsync(dir + 'model.glb'),
      fetch(dir + 'manifest.json', { cache: 'no-store' }).then((r) => r.json()).catch(() => ({})),
    ]).then(([g, man]) => {
      // manifest prop.grip_offset: rotation (deg, XYZ) to apply on a grip_* socket
      // (bows, crossbows, pistols and shields turn 90 deg so -Z points forward)
      g.scene.userData.gripRot = man.prop?.grip_offset?.rotation_deg_xyz || [0, 0, 0];
      return g.scene;
    }).catch((e) => { console.warn('prop', id, e); return null; }));
  }
  return realProps.get(id);
}

let propsGen = 0;  // bumped on every clear: pending real-prop loads of an older call are dropped
function clearProps() {
  propsGen++;
  for (const p of view.props) {
    p.parent?.remove(p);
    if (!p.userData.realProp) p.traverse((o) => o.geometry?.dispose());  // clones share the cached geometry
  }
  view.props = [];
}

async function updateProps() {
  clearProps();
  const gen = propsGen;
  if (!$('t-prop').checked || !view.root) return;
  const s = (Number(view.entry?.scale_m) || 1.8) / 1.8;
  for (const { sockets, make, param } of PROPS) {
    const bone = sockets.map((n) => view.socketBones.find((b) => b.name === n)?.bone).find(Boolean);
    const onGrip = bone?.name.startsWith('grip');
    if (!bone) continue;
    const id = realPropId(param);
    const real = id ? await loadRealProp(id) : null;
    if (gen !== propsGen || !view.root) return;
    const p = real ? real.clone(true) : make();
    p.userData.realProp = !!real;
    if (real && onGrip) p.rotation.set(...real.userData.gripRot.map(THREE.MathUtils.degToRad));
    view.root.updateMatrixWorld(true);
    const ws = bone.getWorldScale(new THREE.Vector3());
    p.scale.set(s / ws.x, s / ws.y, s / ws.z);
    p.traverse((o) => { o.userData.overlay = true; o.castShadow = true; });  // not model geometry: skip in toggles, tri counts, albedo
    bone.add(p);
    view.props.push(p);
  }
}

function syncRigHelpers() {
  if (view.skelHelper) view.skelHelper.visible = $('t-skel').checked && !view.showRaw;
  updateSocketMarkers();
}

// ------------------------------------------------------------------ images + details
async function renderSide(entry) {
  const imgs = $('images');
  imgs.replaceChildren();
  $('details').replaceChildren();
  $('manifest-link').textContent = '';
  let man = {};
  if (entry.files.manifest) {
    try { man = await (await fetch(entry.dir + 'manifest.json', { cache: 'no-store' })).json(); } catch { man = {}; }
  }
  if (state.selId !== entry.id) return;
  const picked = man.mesh?.ref_picked ?? man.ref?.picked;
  const fig = (src, cap, cls = '', wide = false) => {
    const img = el('img', { src: entry.dir + src, loading: 'lazy', className: cls, alt: cap });
    img.onclick = () => { $('lightbox').querySelector('img').src = img.src; $('lightbox').classList.remove('hidden'); };
    imgs.append(el('figure', { className: wide ? 'wide' : '' }, img, el('figcaption', { textContent: cap })));
  };
  for (const r of entry.files.refs) {
    const i = Number(r.match(/ref_(\d+)/)?.[1]);
    fig(r, r.replace('.png', '') + (i === picked ? ' ✓' : ''), i === picked ? 'picked' : '');
  }
  if (!entry.files.refs.length && entry.files.ref) fig(entry.files.ref, 'ref');
  if (entry.files.crop) fig(entry.files.crop, 'crop');
  if (entry.files.thumb) fig(entry.files.thumb, 'thumb (QA strip)', '', true);
  if (entry.files.thumb_head) fig(entry.files.thumb_head, 'head close-up (shaded / wireframe)', '', true);
  if (entry.files.thumb_ground) fig(entry.files.thumb_ground, 'ground contact (side / front, level camera; red = feet used)', '', true);
  if (!imgs.children.length) imgs.append(el('div', { className: 'muted', textContent: 'no images yet', style: 'grid-column: 1 / -1' }));

  const rows = [
    ['id', entry.id], ['type', entry.type + (entry.tags?.length ? ` (${entry.tags.join(', ')})` : '')],
    ['size', entry.size], ['CR', entry.cr], ['body plan', `${entry.body_plan} (${fmt(entry.body_plan_confidence)})`],
    ['rig template', entry.rig_template], ['scale_m', entry.scale_m],
    ['speeds', Object.entries(entry.speeds || {}).map(([k, v]) => `${k} ${v}`).join(', ')],
    ['stages', STAGES.filter((s) => entry.stages[s]).join(' · ') || 'none'],
  ];
  if (man.tri_count != null) rows.push(['tris', man.tri_count]);
  if (man.mesh?.settings?.target_tris) rows.push(['tri target', man.mesh.settings.target_tris]);
  if (man.qa?.dims_m) rows.push(['dims m', man.qa.dims_m.map((v) => fmt(v)).join(' × ')]);
  if (man.qa) rows.push(['non-manifold', `${man.qa.non_manifold_edges} edges, ${man.qa.loose_parts} part(s)`]);
  if (man.qa?.facing_method) rows.push(['facing', `${man.qa.facing_method}, yaw ${fmt(man.qa.yaw_deg, 1)}°`]);
  if (man.qa?.level) {
    const L = man.qa.level;
    rows.push(['stance', `${L.method}, tilt ${fmt(L.tilt_deg, 1)}° (cap ${L.tilt_cap_deg}°), feet gaps m ${(L.foot_gaps_m || []).map((g) => fmt(g, 3)).join(' ')}`]);
  }
  if (entry.lods && Object.keys(entry.lods).length) rows.push(['LODs', Object.entries(entry.lods).map(([k, v]) => `${k}: ${v.tris}`).join(' · ')]);
  if (man.timings) rows.push(['timings s', Object.entries(man.timings).map(([k, v]) => `${k} ${v}`).join(', ')]);
  if (man.ref) rows.push(['ref seed', `${man.ref.seed} (${man.ref.model}, ${man.ref.n} cand.)`]);
  if (man.mesh) rows.push(['mesh seed', `${man.mesh.seed} (${man.mesh.model})`]);
  if (man.rig) rows.push(['rig', JSON.stringify(man.rig)]);
  if (man.anim) rows.push(['anim', JSON.stringify(man.anim)]);
  if (entry.sockets?.length || (entry.rig_sockets && Object.keys(entry.rig_sockets).length)) {
    const placed = entry.rig_sockets || {};
    rows.push(['sockets', Object.keys(placed).length ? Object.entries(placed).map(([k, v]) => `${k} (${v})`).join(', ')
      : (entry.stages.rig ? 'none placed' : 'not rigged')]);
    rows.push(['sockets wanted', (entry.sockets || []).join(', ') || '–']);
    if (entry.sockets_missing?.length) rows.push(['sockets missing', entry.sockets_missing.join(', ')]);
  }
  if (man.errors?.length) rows.push(['errors', man.errors.map((e) => `${e.stage}: ${e.error}`).join('\n')]);
  if (man.ref?.prompt) rows.push(['prompt', man.ref.prompt, 'long']);
  for (const [k, v, cls] of rows) {
    $('details').append(el('dt', { textContent: k }), el('dd', { textContent: String(v ?? '–'), className: cls || '' }));
  }
  if (entry.files.manifest) {
    $('manifest-link').href = entry.dir + 'manifest.json';
    $('manifest-link').textContent = 'manifest.json ↗';
  }
}

// ------------------------------------------------------------------ sidebar
const STAGE_COLORS = { ref: 'var(--ref)', mesh: 'var(--mesh)', qa: 'var(--qa)', paint: 'var(--paint)', lod: 'var(--lod)', rig: 'var(--rig)', anim: 'var(--anim)' };

function renderProgress(idx) {
  $('source').textContent = idx.source;
  const total = idx.summary.total;
  const bars = $('stage-bars');
  bars.replaceChildren();
  for (const s of STAGES) {
    const n = idx.summary.stages[s];
    const fillEl = el('div', { className: 'fill' });
    fillEl.style.width = `${(100 * n) / Math.max(total, 1)}%`;
    fillEl.style.background = STAGE_COLORS[s];
    const row = el('div', { className: 'sbar', title: `filter: has ${s}` },
      el('span', { textContent: s, style: `color: ${STAGE_COLORS[s]}` }), el('div', { className: 'track' }, fillEl),
      el('span', { className: 'n', textContent: `${n}/${total}` }));
    row.style.cursor = 'pointer';
    row.onclick = () => { $('f-stage').value = `has:${s}`; applyFilters(); };
    bars.append(row);
  }
  const tbl = $('plan-table');
  tbl.replaceChildren();
  const plans = Object.entries(idx.summary.by_body_plan).sort((a, b) => b[1].total - a[1].total);
  for (const [plan, c] of plans) {
    const mini = el('div', { className: 'mini' });
    for (const s of STAGES) {
      const i = el('i');
      i.style.width = `${(100 * c[s]) / c.total}%`;
      i.style.background = STAGE_COLORS[s];
      mini.append(el('span', { title: `${s} ${c[s]}/${c.total}` }, i));
    }
    const tr = el('tr', {}, el('td', { textContent: plan }), el('td', {}, mini),
      el('td', { className: 'num', textContent: `${c.qa}/${c.total}`, title: 'qa done / total' }));
    tr.onclick = () => { $('f-plan').value = plan; applyFilters(); };
    tbl.append(tr);
  }
}

function fillFilters(idx) {
  const plans = $('f-plan'), sizes = $('f-size'), stage = $('f-stage');
  const keepPlan = plans.value, keepSize = sizes.value, keepStage = stage.value;
  plans.length = 1; sizes.length = 1; stage.length = 1;
  const counts = {};
  for (const m of idx.monsters) counts[m.body_plan] = (counts[m.body_plan] || 0) + 1;
  for (const p of idx.body_plans) if (counts[p]) plans.append(el('option', { value: p, textContent: `${p} (${counts[p]})` }));
  for (const s of ['tiny', 'small', 'medium', 'large', 'huge', 'gargantuan']) sizes.append(el('option', { value: s, textContent: s }));
  for (const s of STAGES) stage.append(el('option', { value: `has:${s}`, textContent: `has ${s}` }));
  for (const s of STAGES) stage.append(el('option', { value: `no:${s}`, textContent: `missing ${s}` }));
  stage.append(el('option', { value: 'anim-incomplete', textContent: 'anim set incomplete' }));
  stage.append(el('option', { value: 'errors', textContent: 'has errors' }));
  plans.value = keepPlan; sizes.value = keepSize; stage.value = keepStage;
}

function matches(m) {
  const plan = $('f-plan').value, size = $('f-size').value, st = $('f-stage').value;
  const q = $('search').value.trim().toLowerCase();
  if (plan && m.body_plan !== plan) return false;
  if (size && m.size !== size) return false;
  if (st) {
    const [kind, s] = st.split(':');
    if (kind === 'has' && !m.stages[s]) return false;
    if (kind === 'no' && m.stages[s]) return false;
    if (st === 'anim-incomplete' && m.anim_complete) return false;
    if (st === 'errors' && !m.errors) return false;
  }
  if (q) {
    const hay = `${m.name} ${m.id} ${m.type} ${(m.tags || []).join(' ')} ${m.body_plan} cr${m.cr}`.toLowerCase();
    if (!q.split(/\s+/).every((w) => hay.includes(w))) return false;
  }
  return true;
}

function applyFilters() {
  const idx = state.index;
  state.filtered = idx.monsters.filter(matches);
  $('count').textContent = `${state.filtered.length} / ${idx.monsters.length}`;
  const ul = $('list');
  ul.replaceChildren();
  const frag = document.createDocumentFragment();
  for (const m of state.filtered) {
    const th = m.files.thumb ? el('img', { className: 'th', loading: 'lazy', src: m.dir + m.files.thumb, alt: '' }) : el('div', { className: 'th' });
    const dots = el('div', { className: 'dots' });
    for (const s of STAGES) dots.append(el('span', { className: `dot ${s}${m.stages[s] ? ' on' : ''}`, title: s }));
    const nm = el('div', { className: 'nm', textContent: m.name });
    if (m.has_override) nm.append(el('span', { className: 'ov', textContent: '✎', title: 'has overrides' }));
    const js = state.jobs[m.id] || m.job;
    if (js) nm.append(el('span', { className: `job ${js}`, title: `re-run ${js}` }));
    const li = el('li', { className: m.id === state.selId ? 'sel' : '' }, th,
      el('div', {}, nm,
        el('div', { className: 'sub', textContent: `${m.body_plan} · ${m.size} · CR ${m.cr}` })), dots);
    li.dataset.id = m.id;
    li.onclick = () => select(m.id);
    frag.append(li);
  }
  ul.append(frag);
}

function select(id) {
  const entry = state.index.monsters.find((m) => m.id === id);
  if (!entry) return;
  state.selId = id;
  if (location.hash.slice(1) !== id) history.replaceState(null, '', '#' + id);
  for (const li of $('list').children) {
    const on = li.dataset.id === id;
    li.classList.toggle('sel', on);
    if (on) li.scrollIntoView({ block: 'nearest' });
  }
  loadModel(entry);
  renderSide(entry);
  loadOverrides(entry.id);
  renderJob();
}

function move(step) {
  const list = state.filtered;
  if (!list.length) return;
  const i = list.findIndex((m) => m.id === state.selId);
  const j = i < 0 ? 0 : Math.min(list.length - 1, Math.max(0, i + step));
  select(list[j].id);
}

async function refreshIndex() {
  const idx = await (await fetch('/api/index.json', { cache: 'no-store' })).json();
  state.index = idx;
  renderProgress(idx);
  fillFilters(idx);
  applyFilters();
  return idx;
}

// ------------------------------------------------------------------ wiring
for (const id of ['f-plan', 'f-size', 'f-stage']) $(id).onchange = applyFilters;
$('search').oninput = applyFilters;
$('t-wire').onchange = applyMaterialToggles;
$('t-wireo').onchange = updateWireOverlay;
$('t-tex').onchange = applyMaterialToggles;
$('t-env').onchange = () => { scene.environment = $('t-env').checked ? envTex : null; };
$('t-skel').onchange = syncRigHelpers;
$('t-sockets').onchange = () => updateSocketMarkers(true);
$('t-prop').onchange = updateProps;
$('t-ref').onchange = () => { scaleRef.visible = $('t-ref').checked; frameCamera(); };
$('t-rotate').onchange = () => {
  if (!$('t-rotate').checked && view.root) view.root.rotation.y = 0;
  if (view.raw) view.raw.group.rotation.y = view.root?.rotation.y ?? 0;
};
$('b-frame').onclick = frameCamera;
$('b-play').onclick = togglePlay;
$('b-next').onclick = () => nextClip();
$('b-cycle').onclick = () => { view.cycle = !view.cycle; view.cycleT = 0; syncPlayButton(); };
$('t-loop').onchange = () => { if (view.action) { setLoop(view.action); view.action.reset().play(); } };
$('speed').oninput = () => {
  const v = Number($('speed').value);
  $('speed-val').textContent = `${v.toFixed(2)}×`;
  if (view.mixer) view.mixer.timeScale = v;
};
$('scrub').addEventListener('pointerdown', () => { view.scrubbing = true; });
$('scrub').addEventListener('pointerup', () => { view.scrubbing = false; });
$('scrub').oninput = () => {
  const a = view.action;
  if (!a) return;
  a.paused = false;
  a.time = Number($('scrub').value) * a.getClip().duration;
  view.mixer.update(0);
};
$('lightbox').onclick = () => $('lightbox').classList.add('hidden');

document.addEventListener('keydown', (e) => {
  // A focused toolbar checkbox or slider must not swallow the shortcuts; text fields do.
  const typing = e.target.matches('input:not([type=checkbox]):not([type=range]), select, textarea');
  if (e.key === 'Escape') { $('lightbox').classList.add('hidden'); $('lod-settings').classList.add('hidden'); if (typing) e.target.blur(); return; }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key;
  if (k === 'j' || k === 'ArrowDown') move(1);
  else if (k === 'k' || k === 'ArrowUp') move(-1);
  else if (k === ' ') togglePlay();
  else if (k === 'n') nextClip(1);
  else if (k === 'N') nextClip(-1);
  else if (k >= '0' && k <= '9') { const b = $('anim-bar').querySelectorAll('button:not(:disabled)')[Number(k)]; if (b) b.click(); }
  else if (k === 'c') $('b-cycle').click();
  else if (k === 'w') { $('t-wire').checked = !$('t-wire').checked; applyMaterialToggles(); }
  else if (k === 'W') { $('t-wireo').checked = !$('t-wireo').checked; updateWireOverlay(); }
  else if (k === 'a') setMode('auto');
  else if (k === 's') { $('t-sockets').checked = !$('t-sockets').checked; updateSocketMarkers(true); }
  else if (k === 'p') { $('t-prop').checked = !$('t-prop').checked; updateProps(); }
  else if (k === 't') { $('t-tex').checked = !$('t-tex').checked; applyMaterialToggles(); }
  else if (k === 'f') frameCamera();
  else if (k === 'l') cycleLod();
  else if (k === '/') { $('search').focus(); }
  else return;
  e.preventDefault();
});
window.addEventListener('hashchange', () => { const id = decodeURIComponent(location.hash.slice(1)); if (state.index && id && id !== state.selId) select(id); });
// Pick up new forge output without a reload when the tab regains focus.
window.addEventListener('focus', async () => {
  if (!state.index) return;
  const before = JSON.stringify(state.index.monsters.find((m) => m.id === state.selId));
  await refreshIndex();
  const after = state.index.monsters.find((m) => m.id === state.selId);
  if (after && JSON.stringify(after) !== before) select(after.id);
});

// ------------------------------------------------------------------ overrides panel
// data/overrides/<source>/<id>.json through GET/POST /api/override/<id>. An empty input
// means "use the default"; the default is shown under each field.
const FIELD_UI = {
  tri_budget: { type: 'int' }, vg_factor: { type: 'float', step: 0.05 },
  upsample: { type: 'choice', opts: [0, 1024] }, texture_size: { type: 'choice', opts: [512, 1024, 2048, 4096] },
  normal_cage: { type: 'float', step: 0.001 }, ref_pick: { type: 'int' }, seed_mesh: { type: 'int' },
  prompt_extra: { type: 'text', area: true }, negative_extra: { type: 'text', area: true },
  style: { type: 'text' }, seed_ref: { type: 'int' },
  yaw: { type: 'float', step: 1 }, forward: { type: 'choice', opts: ['-Z', '+Z'] },
  level: { type: 'choice', opts: [true, false] }, level_tilt: { type: 'float', step: 1 },
  lod_ratios: { type: 'list' }, lod_screen_ratios: { type: 'list' }, paint: { type: 'json', area: true }, notes: { type: 'text', area: true },
};
const RUN_ORDER = ['ref', 'mesh', 'qa', 'paint', 'lod'];
const show = (v) => (v == null || v === '' ? '–' : Array.isArray(v) ? v.join(', ') : typeof v === 'object' ? JSON.stringify(v) : String(v));

function toInput(k, v) {
  if (v == null) return '';
  const t = FIELD_UI[k]?.type;
  return t === 'list' ? v.join(', ') : t === 'json' || typeof v === 'object' ? JSON.stringify(v) : String(v);
}
function fromInput(k, raw) {
  const ui = FIELD_UI[k] || { type: 'text' };
  raw = raw.trim();
  if (raw === '') return null;
  if (ui.type === 'int') return Number.isInteger(Number(raw)) ? Number(raw) : NaN;
  if (ui.type === 'float') return Number(raw);
  if (ui.type === 'list') return raw.split(/[\s,]+/).filter(Boolean).map(Number);
  if (ui.type === 'json') { try { return JSON.parse(raw); } catch { return undefined; } }
  if (ui.type === 'choice') return ui.opts.find((o) => String(o) === raw) ?? raw;
  return raw;
}

async function loadOverrides(id) {
  $('ov-form').replaceChildren();
  $('ov-status').textContent = '';
  let ov;
  try {
    const r = await fetch(`/api/override/${encodeURIComponent(id)}`, { cache: 'no-store' });
    ov = await r.json();
    if (!r.ok) throw new Error(ov.error || r.status);
  } catch (err) { $('ov-status').textContent = 'overrides unavailable: ' + err.message; return; }
  if (state.selId !== id) return;
  state.ov = ov;
  renderOverrides();
  renderLodSettings();
  view.autoLevel = null;
}

function renderOverrides() {
  const ov = state.ov, form = $('ov-form');
  form.replaceChildren();
  $('ov-file').textContent = ov.exists ? ov.file.replace(/^data\//, '') : '(defaults)';
  for (const [k, f] of Object.entries(ov.fields)) {
    const ui = FIELD_UI[k] || { type: 'text' };
    let input;
    if (ui.type === 'choice') {
      input = el('select', {}, el('option', { value: '', textContent: `default (${show(f.default)})` }),
        ...ui.opts.map((o) => el('option', { value: String(o), textContent: String(o) })));
    } else if (ui.area) {
      input = el('textarea', { rows: 1, placeholder: show(f.default) });
    } else {
      input = el('input', { type: ui.type === 'int' || ui.type === 'float' ? 'number' : 'text', placeholder: show(f.default) });
      if (ui.step) input.step = ui.step;
    }
    input.value = toInput(k, f.override);
    input.dataset.k = k;
    input.dataset.saved = input.value;
    input.oninput = input.onchange = () => input.classList.toggle('dirty', input.value !== input.dataset.saved);
    const reset = el('button', { type: 'button', className: 'reset' + (f.override != null ? ' show' : ''), textContent: '×',
      title: `reset to default (${show(f.default)}) and save` });
    reset.onclick = () => { input.value = ''; saveOverrides(); };
    const label = el('label', { className: 'k' + (f.override != null ? ' set' : ''), textContent: k,
      title: `${f.help}${f.stage ? ` (re-run from: ${f.stage})` : ''}` });
    form.append(label, input, reset);
    if (f.override != null && k !== 'notes') form.append(el('div'), el('div', { className: 'def', textContent: `default ${show(f.default)}` }));
  }
  const box = $('run-stages');
  if (!box.children.length) {
    for (const st of RUN_ORDER) {
      box.append(el('label', {}, el('input', { type: 'checkbox', id: `run-${st}`, checked: st === 'qa' || st === 'paint' || st === 'lod' }), st));
    }
  }
}

async function saveOverrides() {
  const id = state.selId, body = {};
  const changed = [];
  for (const input of $('ov-form').querySelectorAll('[data-k]')) {
    const k = input.dataset.k, v = fromInput(k, input.value);
    if (typeof v === 'number' && Number.isNaN(v)) { $('ov-status').textContent = `${k}: not a number`; return; }
    if (Array.isArray(v) && v.some(Number.isNaN)) { $('ov-status').textContent = `${k}: not numbers`; return; }
    if (v === undefined) { $('ov-status').textContent = `${k}: not valid JSON`; return; }
    body[k] = v;
    if (input.value !== input.dataset.saved) changed.push(k);
  }
  $('ov-status').textContent = 'saving…';
  const r = await fetch(`/api/override/${encodeURIComponent(id)}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const res = await r.json();
  if (!r.ok) { $('ov-status').textContent = (res.details || [res.error]).join('; '); return; }
  if (state.selId !== id) return;
  state.ov = res;
  renderOverrides();
  renderLodSettings();
  view.autoLevel = null;  // re-pick with the new per-monster switch points
  // Tick the stages the changed fields need: the earliest affected stage and everything after.
  const first = Math.min(...changed.map((k) => RUN_ORDER.indexOf(res.fields[k]?.stage)).filter((i) => i >= 0));
  if (Number.isFinite(first)) RUN_ORDER.forEach((st, i) => { $(`run-${st}`).checked = i >= first; });
  $('ov-status').textContent = changed.length ? `saved (${changed.join(', ')})` : 'saved';
  const m = state.index.monsters.find((x) => x.id === id);
  if (m) { m.has_override = res.exists; applyFilters(); }
}

$('ov-save').onclick = saveOverrides;
$('ov-revert').onclick = () => { if (state.ov) renderOverrides(); $('ov-status').textContent = ''; };

// ------------------------------------------------------------------ re-run jobs
$('run-go').onclick = async () => {
  const id = state.selId;
  const stages = RUN_ORDER.filter((st) => $(`run-${st}`)?.checked);
  if (!id || !stages.length) { $('run-status').textContent = 'pick at least one stage'; return; }
  const r = await fetch(`/api/run/${encodeURIComponent(id)}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ stages, force: true }) });
  const res = await r.json();
  $('run-status').textContent = r.ok ? `job #${res.id} ${res.status}` : res.error;
  pollJobs(true);
};

let pollTimer = null;
async function pollJobs(soon = false) {
  clearTimeout(pollTimer);
  let snap;
  try { snap = await (await fetch('/api/jobs?tail=60', { cache: 'no-store' })).json(); } catch { snap = null; }
  if (snap) {
    const prev = state.jobList;
    state.jobList = snap.jobs;
    const jobs = {};
    for (const j of snap.jobs) {
      if (j.status === 'running' || j.status === 'cancelling') jobs[j.mid] = 'running';
      else if (j.status === 'queued' && !jobs[j.mid]) jobs[j.mid] = 'queued';
    }
    const changedMarkers = JSON.stringify(jobs) !== JSON.stringify(state.jobs);
    state.jobs = jobs;
    if (state.index && changedMarkers) {
      // a job finished since the last poll: pick up its output
      const finished = prev.filter((p) => (p.status === 'running' || p.status === 'queued')
        && snap.jobs.find((j) => j.id === p.id && !['running', 'queued', 'cancelling'].includes(j.status)));
      if (finished.length) {
        const before = state.selId;
        await refreshIndex();
        if (finished.some((f) => f.mid === before)) { select(before); }
      } else applyFilters();
    }
    renderJob();
  }
  const active = Object.keys(state.jobs).length > 0;
  pollTimer = setTimeout(pollJobs, active || soon ? 2000 : 15000);
}

function renderJob() {
  const j = state.jobList.find((x) => x.mid === state.selId);
  const pre = $('job-log');
  if (!j) { pre.classList.add('hidden'); $('run-status').textContent = ''; return; }
  pre.classList.remove('hidden');
  const t = j.ended_at ? ` in ${Math.round(j.ended_at - j.started_at)} s` : j.started_at ? ` ${Math.round(Date.now() / 1000 - j.started_at)} s` : '';
  const queuedAhead = j.status === 'queued' ? ` (${state.jobList.filter((x) => x.status === 'queued' && x.id < j.id).length} ahead)` : '';
  $('run-status').replaceChildren(`job #${j.id} [${j.stages.join(',')}] ${j.status}${queuedAhead}${t}${j.rc != null ? ` rc ${j.rc}` : ''} `);
  if (j.status === 'queued' || j.status === 'running') {
    const c = el('button', { textContent: 'cancel', title: 'drop from the queue / stop the forge process' });
    c.onclick = async () => { await fetch(`/api/jobs/${j.id}/cancel`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); pollJobs(true); };
    $('run-status').append(c);
  }
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
  pre.textContent = j.log_tail.join('\n') || '(no output yet)';
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

refreshIndex().then((idx) => {
  const want = decodeURIComponent(location.hash.slice(1));
  const first = idx.monsters.find((m) => m.id === want) || idx.monsters.find((m) => m.stages.qa) || idx.monsters[0];
  if (first) select(first.id);
  pollJobs();
});

window.__viewer = { THREE, scene, view, state, camera, controls }; // debugging hook
