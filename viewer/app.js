// monster-forge viewer: progress sidebar, three.js model view, animation panel.
// Vanilla ES modules; three.js r179 vendored under vendor/three (see importmap).
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const STAGES = ['ref', 'mesh', 'qa', 'lod', 'rig', 'anim'];
const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...kids) => {
  const e = Object.assign(document.createElement(tag), props);
  for (const k of kids) if (k != null) e.append(k);
  return e;
};
const fmt = (n, d = 2) => (n == null || Number.isNaN(n) ? '–' : Number(n).toFixed(d));

// ------------------------------------------------------------------ state
const state = {
  index: null,
  filtered: [],
  selId: null,
  loadToken: 0,
  lodPref: 0,          // LOD the user picked; re-applied when the next monster has it
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
  if (view.cycle && view.clips.length > 1 && view.playing) {
    view.cycleT += dt;
    if (view.cycleT >= Number($('cycle-secs').value || 4)) { view.cycleT = 0; nextClip(); }
  }
  if ($('t-rotate').checked && view.root) view.root.rotation.y += dt * 0.5;
  updateTimeUI();
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);

function disposeModel() {
  if (view.mixer) { view.mixer.stopAllAction(); view.mixer.uncacheRoot(view.root); }
  if (view.skelHelper) { scene.remove(view.skelHelper); view.skelHelper.dispose?.(); }
  for (const [k, g] of Object.entries(view.lodGeoms)) if (k !== '0') g.dispose();
  if (view.root) {
    scene.remove(view.root);
    view.root.traverse((o) => {
      if (!o.isMesh) return;
      o.geometry.dispose();
      for (const m of [].concat(o.material)) {
        for (const t of Object.values(m.userData.maps || {})) t?.dispose?.();
        m.dispose();
      }
    });
  }
  Object.assign(view, { root: null, mixer: null, clips: [], action: null, clipIdx: -1, skelHelper: null, box: null, cycleT: 0,
    lod: 0, lodMesh: null, lodGeoms: {} });
}

const MAP_KEYS = ['map', 'normalMap', 'roughnessMap', 'metalnessMap', 'aoMap', 'emissiveMap'];
function applyMaterialToggles() {
  if (!view.root) return;
  const wire = $('t-wire').checked, tex = $('t-tex').checked;
  view.root.traverse((o) => {
    if (!o.isMesh) return;
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
  });
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
    if (!o.isMesh) return;
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
  if (!entry.files.model) { setHud(entry, null); return; }
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
    view.skelHelper = new THREE.SkeletonHelper(root);
    view.skelHelper.visible = $('t-skel').checked;
    scene.add(view.skelHelper);
  }
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
  // LOD swap needs one plain mesh (LOD GLBs are geometry + LOD0's material)
  if (meshes.length === 1 && !meshes[0].isSkinnedMesh) {
    view.lodMesh = meshes[0];
    view.lodGeoms = { 0: meshes[0].geometry };
  }
  renderLodBar(entry);
  setHud(entry, view.info);
  if (state.lodPref && entry.lods?.[state.lodPref]) setLod(state.lodPref, token);
  renderClips(entry);
  const firstOk = view.clips.findIndex((c) => c.ok);
  if (firstOk >= 0) playClip(firstOk);
}

// ------------------------------------------------------------------ LODs
// model_lod<k>.glb share LOD0's UVs and material; only the geometry is swapped in, so the
// loaded LOD0 material (with its textures) is reused, as an engine would.
function renderLodBar(entry) {
  const bar = $('lod-bar');
  bar.replaceChildren();
  const lods = entry.lods || {};
  const keys = Object.keys(lods).sort();
  if (!view.lodMesh || keys.length < 2) return;
  for (const k of keys) {
    const b = el('button', { title: `${lods[k].file} (ratio ${lods[k].ratio})` },
      `LOD${k}`, el('small', { textContent: Number(lods[k].tris).toLocaleString() }));
    b.classList.toggle('on', Number(k) === view.lod);
    b.onclick = () => { state.lodPref = Number(k); setLod(Number(k)); };
    bar.append(b);
  }
}

async function setLod(k, token = state.loadToken) {
  const entry = view.entry;
  if (!view.lodMesh || !entry?.lods?.[k]) return;
  let geo = view.lodGeoms[k];
  if (!geo) {
    try {
      const g = await loadGLB(entry.dir + entry.lods[k].file);
      if (token !== state.loadToken) return;
      g.scene.traverse((o) => { if (o.isMesh && !geo) geo = o.geometry; });
    } catch (err) {
      $('hud-stats').textContent += `\nLOD${k}: ${err.message}`;
      return;
    }
    if (!geo) return;
    view.lodGeoms[k] = geo;
  }
  view.lodMesh.geometry = geo;
  view.lod = k;
  view.info.tris = triCount(view.root);
  view.info.file = k ? entry.lods[k].file : entry.files.model;
  setHud(entry, view.info);
  renderLodBar(entry);
}

function cycleLod() {
  const keys = Object.keys(view.entry?.lods || {}).map(Number).sort();
  if (keys.length < 2 || !view.lodMesh) return;
  const next = keys[(keys.indexOf(view.lod) + 1) % keys.length];
  state.lodPref = next;
  setLod(next);
}

function setHud(entry, info) {
  $('hud-title').textContent = entry.name;
  const lines = [`${entry.body_plan} · ${entry.size} · CR ${entry.cr} · scale_m ${entry.scale_m}`];
  if (info) {
    const s = info.size;
    lines.push(`${info.file} · ${info.tris.toLocaleString()} tris`);
    lines.push(`bounds W ${fmt(s.x)} × H ${fmt(s.y)} × L ${fmt(s.z)} m   (grid 1 m)`);
    let rig = info.skinned ? `skeleton: ${info.bones} bones` : 'no skeleton';
    if (info.skinned && !entry.stages.rig) rig += '  (manifest has no rig stage)';
    if (!info.skinned && entry.stages.rig) rig += '  (manifest says rigged!)';
    lines.push(rig);
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
const STAGE_COLORS = { ref: 'var(--ref)', mesh: 'var(--mesh)', qa: 'var(--qa)', lod: 'var(--lod)', rig: 'var(--rig)', anim: 'var(--anim)' };

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
$('t-tex').onchange = applyMaterialToggles;
$('t-env').onchange = () => { scene.environment = $('t-env').checked ? envTex : null; };
$('t-skel').onchange = () => { if (view.skelHelper) view.skelHelper.visible = $('t-skel').checked; };
$('t-ref').onchange = () => { scaleRef.visible = $('t-ref').checked; frameCamera(); };
$('t-rotate').onchange = () => { if (!$('t-rotate').checked && view.root) view.root.rotation.y = 0; };
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
  const typing = e.target.matches('input, select, textarea');
  if (e.key === 'Escape') { $('lightbox').classList.add('hidden'); if (typing) e.target.blur(); return; }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key;
  if (k === 'j' || k === 'ArrowDown') move(1);
  else if (k === 'k' || k === 'ArrowUp') move(-1);
  else if (k === ' ') togglePlay();
  else if (k === 'n') nextClip(1);
  else if (k === 'N') nextClip(-1);
  else if (k === 'c') $('b-cycle').click();
  else if (k === 'w') { $('t-wire').checked = !$('t-wire').checked; applyMaterialToggles(); }
  else if (k === 't') { $('t-tex').checked = !$('t-tex').checked; applyMaterialToggles(); }
  else if (k === 'f') frameCamera();
  else if (k === 'l') cycleLod();
  else if (k === '/') { $('search').focus(); }
  else return;
  e.preventDefault();
});
window.addEventListener('hashchange', () => { const id = location.hash.slice(1); if (id && id !== state.selId) select(id); });
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
  lod_ratios: { type: 'list' }, notes: { type: 'text', area: true },
};
const RUN_ORDER = ['ref', 'mesh', 'qa', 'lod'];
const show = (v) => (v == null || v === '' ? '–' : Array.isArray(v) ? v.join(', ') : String(v));

function toInput(k, v) {
  if (v == null) return '';
  return FIELD_UI[k].type === 'list' ? v.join(', ') : String(v);
}
function fromInput(k, raw) {
  const ui = FIELD_UI[k];
  raw = raw.trim();
  if (raw === '') return null;
  if (ui.type === 'int') return Number.isInteger(Number(raw)) ? Number(raw) : NaN;
  if (ui.type === 'float') return Number(raw);
  if (ui.type === 'list') return raw.split(/[\s,]+/).filter(Boolean).map(Number);
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
      box.append(el('label', {}, el('input', { type: 'checkbox', id: `run-${st}`, checked: st === 'qa' || st === 'lod' }), st));
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
