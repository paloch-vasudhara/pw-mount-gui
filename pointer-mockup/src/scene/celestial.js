// Sun (with keep-out cap) and Moon (phase-shaded from the sun direction).

import * as THREE from 'three';
import { altAzToDir } from '../coords.js';
import { COLORS, DOME_RADIUS, SUN_KEEPOUT_DEG, ALT_AXIS_HEIGHT } from '../config.js';
import { DOME_CENTER, domePoint } from './hemisphere.js';

// Clip anything below the horizon plane (y >= alt-axis height is kept).
export const ABOVE_HORIZON = new THREE.Plane(new THREE.Vector3(0, 1, 0), -ALT_AXIS_HEIGHT);

function glowTexture(rgb) {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const ctx = c.getContext('2d');
  const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
  g.addColorStop(0, `rgba(${rgb},0.9)`);
  g.addColorStop(0.25, `rgba(${rgb},0.35)`);
  g.addColorStop(1, `rgba(${rgb},0)`);
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 128, 128);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

const glowSprite = (rgb, size) => {
  const s = new THREE.Sprite(new THREE.SpriteMaterial({
    map: glowTexture(rgb), blending: THREE.AdditiveBlending, depthWrite: false, transparent: true,
  }));
  s.scale.setScalar(size);
  return s;
};

function createSun() {
  const group = new THREE.Group();
  group.add(new THREE.Mesh(new THREE.SphereGeometry(3.4, 32, 16), new THREE.MeshBasicMaterial({ color: COLORS.sun })));
  group.add(glowSprite('255,205,90', 26));

  // Keep-out cap: spherical cap around +Y, re-oriented to the sun direction.
  const capAngle = SUN_KEEPOUT_DEG * Math.PI / 180;
  const capGeo = new THREE.SphereGeometry(DOME_RADIUS * 0.995, 64, 6, 0, Math.PI * 2, 0, capAngle);
  const cap = new THREE.Mesh(capGeo, new THREE.MeshBasicMaterial({
    color: COLORS.danger, transparent: true, opacity: 0.16, side: THREE.DoubleSide, depthWrite: false,
    clippingPlanes: [ABOVE_HORIZON],
  }));
  const ringPts = [];
  for (let i = 0; i <= 128; i++) {
    const t = (i / 128) * Math.PI * 2;
    ringPts.push(new THREE.Vector3(Math.sin(capAngle) * Math.cos(t), Math.cos(capAngle), Math.sin(capAngle) * Math.sin(t))
      .multiplyScalar(DOME_RADIUS * 0.994));
  }
  const ring = new THREE.Line(new THREE.BufferGeometry().setFromPoints(ringPts), new THREE.LineBasicMaterial({
    color: COLORS.danger, transparent: true, opacity: 0.85, clippingPlanes: [ABOVE_HORIZON],
  }));
  const zone = new THREE.Group();
  zone.add(cap, ring);
  zone.position.copy(DOME_CENTER);

  return { body: group, zone };
}

function createMoon() {
  const mat = new THREE.ShaderMaterial({
    uniforms: {
      sunDir: { value: new THREE.Vector3(0, 1, 0) },
      lit: { value: new THREE.Color(COLORS.moonLit) },
      dark: { value: new THREE.Color(COLORS.moonDark) },
    },
    vertexShader: /* glsl */`
      varying vec3 vN;
      void main() {
        vN = normalize(mat3(modelMatrix) * normal);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }`,
    fragmentShader: /* glsl */`
      uniform vec3 sunDir; uniform vec3 lit; uniform vec3 dark;
      varying vec3 vN;
      void main() {
        float k = smoothstep(-0.06, 0.06, dot(normalize(vN), sunDir));
        gl_FragColor = vec4(mix(dark, lit, k), 1.0);
        #include <colorspace_fragment>
      }`,
  });
  const group = new THREE.Group();
  group.add(new THREE.Mesh(new THREE.SphereGeometry(3.6, 48, 24), mat));
  group.add(glowSprite('200,215,240', 16));
  return { body: group, material: mat };
}

export function createCelestial() {
  const object = new THREE.Group();
  const sun = createSun();
  const moon = createMoon();
  object.add(sun.body, sun.zone, moon.body);

  const up = new THREE.Vector3(0, 1, 0);
  const dir = new THREE.Vector3();
  const toObserver = new THREE.Vector3(), toCamera = new THREE.Vector3();
  const q = new THREE.Quaternion();

  return {
    object,
    /** Update from store data; cameraPos keeps the moon's phase as seen by the observer. */
    update({ sun: s, moon: m }, cameraPos) {
      sun.body.visible = s.alt > 0;
      domePoint(s.alt, s.az, 0.975, sun.body.position);
      dir.set(...altAzToDir(s.alt, s.az));
      sun.zone.quaternion.setFromUnitVectors(up, dir);
      sun.zone.visible = s.alt > -SUN_KEEPOUT_DEG;   // cap may reach above horizon before sunrise

      moon.body.visible = m.alt > 0;
      domePoint(m.alt, m.az, 0.975, moon.body.position);
      // Lit as seen from the dome centre: rotate the sun direction by the turn that maps
      // the moon->observer ray onto the moon->camera ray, so any orbit angle shows the true phase.
      toObserver.subVectors(DOME_CENTER, moon.body.position).normalize();
      toCamera.subVectors(cameraPos, moon.body.position).normalize();
      q.setFromUnitVectors(toObserver, toCamera);
      moon.material.uniforms.sunDir.value.copy(dir).applyQuaternion(q);
    },
  };
}
