import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

import { altAzToDir, angularSep, moonIllumination, shortestDelta, wrap360 } from './coords.js';
import { DISPLAY_TAU, SLEW_PRESETS, SUN_KEEPOUT_DEG } from './config.js';
import { store } from './store.js';
import { createMockSource } from './sources/mockSource.js';
import { createHemisphere } from './scene/hemisphere.js';
import { createMount } from './scene/mount.js';
import { createCelestial } from './scene/celestial.js';
import { createPointer } from './scene/pointer.js';
import { createPanel } from './ui/panel.js';

// --- Renderer / scene ----------------------------------------------------------------------
const viewport = document.getElementById('viewport');
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.localClippingEnabled = true;
viewport.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x070a12);

const camera = new THREE.PerspectiveCamera(38, 1, 1, 3000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.12;
controls.minDistance = 45;
controls.maxDistance = 520;
controls.maxPolarAngle = Math.PI * 0.495;

scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x2a2a22, 1.5));
const key = new THREE.DirectionalLight(0xffffff, 2.2);
key.position.set(90, 170, 130);
scene.add(key);
const fill = new THREE.DirectionalLight(0xbfd0ff, 0.7);
fill.position.set(-120, 60, -90);
scene.add(fill);

const hemi = createHemisphere();
const mount = createMount();
const celestial = createCelestial();
const pointer = createPointer();
scene.add(hemi.object, mount.object, celestial.object, pointer.object);

// --- Camera presets ------------------------------------------------------------------------
const VIEWS = {
  overview: { target: [0, 52, 0], dist: 390, alt: 27, az: 160 },
  mount: { target: [0, 24, 0], dist: 120, alt: 16, az: 215 },
};
let tween = null;
function goView(name, instant = false) {
  const v = VIEWS[name];
  const target = new THREE.Vector3(...v.target);
  const [x, y, z] = altAzToDir(v.alt, v.az);
  const pos = new THREE.Vector3(x, y, z).multiplyScalar(v.dist).add(target);
  if (instant) {
    camera.position.copy(pos);
    controls.target.copy(target);
    controls.update();
  } else {
    tween = { t: 0, p0: camera.position.clone(), t0: controls.target.clone(), p1: pos, t1: target };
  }
  requestFrame();
}

// --- Data source + panel -------------------------------------------------------------------
const source = createMockSource();
const panel = createPanel(document.getElementById('controls'), source);

function derive(data) {
  const m = data.mount;
  const sunDist = angularSep(m.alt, m.az, data.sun.alt, data.sun.az);
  return {
    sunDist,
    inZone: sunDist < SUN_KEEPOUT_DEG,
    targetInZone: angularSep(m.targetAlt, m.targetAz, data.sun.alt, data.sun.az) < SUN_KEEPOUT_DEG,
    distToTarget: angularSep(m.alt, m.az, m.targetAlt, m.targetAz),
    moonIllum: moonIllumination(angularSep(data.sun.alt, data.sun.az, data.moon.alt, data.moon.az)),
  };
}

store.subscribe((data) => {
  panel.update(data, derive(data));
  requestFrame();
});

// --- Display state (smoothed toward reported position) -------------------------------------
const disp = { alt: store.data.mount.alt, az: store.data.mount.az };

function updateView(dt) {
  const data = store.data, m = data.mount;
  const k = 1 - Math.exp(-dt / DISPLAY_TAU);
  const dAz = shortestDelta(disp.az, m.az), dAlt = m.alt - disp.alt;
  disp.az = wrap360(disp.az + dAz * k);
  disp.alt += dAlt * k;
  const settling = Math.abs(dAz) > 1e-3 || Math.abs(dAlt) > 1e-3;
  if (!settling) { disp.az = m.az; disp.alt = m.alt; }

  mount.setPose(disp.alt, disp.az);
  const tgt = { alt: m.targetAlt, az: m.targetAz };
  const sunDisp = angularSep(disp.alt, disp.az, data.sun.alt, data.sun.az);
  pointer.update(mount.apertureWorld(), disp, tgt, {
    showTarget: angularSep(disp.alt, disp.az, tgt.alt, tgt.az) > 0.05,
    danger: sunDisp < SUN_KEEPOUT_DEG,
    targetDanger: angularSep(tgt.alt, tgt.az, data.sun.alt, data.sun.az) < SUN_KEEPOUT_DEG,
  });
  return settling;
}

// --- On-demand render loop -----------------------------------------------------------------
const frameCounter = document.getElementById('frames');
let raf = 0, last = 0, frames = 0;

function requestFrame() {
  if (!raf) raf = requestAnimationFrame(frame);
}

function frame(t) {
  raf = 0;
  const dt = last ? Math.min((t - last) / 1000, 0.1) : 1 / 60;
  last = t;

  const simActive = source.update(dt);
  const viewActive = updateView(dt);

  let tweenActive = false;
  if (tween) {
    tween.t = Math.min(1, tween.t + dt / 0.8);
    const e = tween.t < 0.5 ? 4 * tween.t ** 3 : 1 - (-2 * tween.t + 2) ** 3 / 2;
    camera.position.lerpVectors(tween.p0, tween.p1, e);
    controls.target.lerpVectors(tween.t0, tween.t1, e);
    tweenActive = tween.t < 1;
    if (!tweenActive) tween = null;
  }
  const ctlActive = controls.update();
  celestial.update(store.data, camera.position);   // after the camera moves: depth cue + moon limb

  renderer.render(scene, camera);
  frameCounter.textContent = String(++frames);

  if (simActive || viewActive || tweenActive || ctlActive) requestFrame();
  else last = 0;
}

controls.addEventListener('change', requestFrame);
controls.addEventListener('start', () => { tween = null; });

new ResizeObserver(() => {
  const { clientWidth: w, clientHeight: h } = viewport;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  requestFrame();
}).observe(viewport);

// --- Footer controls -----------------------------------------------------------------------
document.querySelectorAll('[data-view]').forEach((b) => b.addEventListener('click', () => goView(b.dataset.view)));
document.querySelectorAll('[data-slew]').forEach((b) => b.addEventListener('click', () => {
  source.setSlewRate(SLEW_PRESETS[b.dataset.slew]);
  document.querySelectorAll('[data-slew]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
}));

goView('overview', true);
panel.update(store.data, derive(store.data));
requestFrame();

// Debug handle for console checks / automated screenshots.
window.__mock = { source, store, goView, scene, camera, mount, pointer, disp };
