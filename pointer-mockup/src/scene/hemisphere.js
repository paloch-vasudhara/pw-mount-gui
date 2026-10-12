// Observer hemisphere centred on the mount's altitude axis so pointing has no parallax.
// The dome sphere continues below the horizon until it meets the solid ground disc.

import * as THREE from 'three';
import { altAzToDir, DEG } from '../coords.js';
import { ALT_AXIS_HEIGHT, COLORS, DOME_RADIUS } from '../config.js';

export const DOME_CENTER = new THREE.Vector3(0, ALT_AXIS_HEIGHT, 0);

/** World position on (or scaled inside) the dome for an alt/az pair. */
export function domePoint(altDeg, azDeg, scale = 1, out = new THREE.Vector3()) {
  const [x, y, z] = altAzToDir(altDeg, azDeg);
  return out.set(x, y, z).multiplyScalar(DOME_RADIUS * scale).add(DOME_CENTER);
}

function makeLabel(text, color) {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const ctx = c.getContext('2d');
  ctx.font = '700 84px "Segoe UI", system-ui, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.lineWidth = 10;
  ctx.strokeStyle = 'rgba(5,10,20,0.85)';
  ctx.strokeText(text, 64, 68);
  ctx.fillStyle = color;
  ctx.fillText(text, 64, 68);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthWrite: false, transparent: true }));
  s.scale.setScalar(11);
  return s;
}

function gridGeometry() {
  const pts = [];
  const push = (a, b) => pts.push(...a.toArray(), ...b.toArray());
  const p = (alt, az) => domePoint(alt, az, 0.999);
  // Altitude rings every 15° (horizon drawn separately).
  for (let alt = 15; alt < 90; alt += 15) {
    for (let az = 0; az < 360; az += 2) push(p(alt, az), p(alt, az + 2));
  }
  // Azimuth meridians every 30°, horizon to zenith.
  for (let az = 0; az < 360; az += 30) {
    for (let alt = 0; alt < 90; alt += 2) push(p(alt, az), p(alt + 2, az));
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
  return g;
}

export function createHemisphere() {
  const group = new THREE.Group();
  const R = DOME_RADIUS, H = ALT_AXIS_HEIGHT;
  const groundAngle = Math.asin(H / R);                // depth of dome below horizon
  const groundRadius = Math.sqrt(R * R - H * H);

  // Sky shell (inside visible only, so the near side never hides the mount).
  const sky = new THREE.Mesh(
    new THREE.SphereGeometry(R, 64, 24, 0, Math.PI * 2, 0, Math.PI / 2),
    new THREE.MeshBasicMaterial({ color: COLORS.sky, side: THREE.BackSide, transparent: true, opacity: 0.55, depthWrite: false }),
  );
  sky.position.copy(DOME_CENTER);
  sky.renderOrder = -2;
  group.add(sky);

  // Below-horizon band between horizon and ground.
  const band = new THREE.Mesh(
    new THREE.SphereGeometry(R, 64, 4, 0, Math.PI * 2, Math.PI / 2, groundAngle),
    new THREE.MeshBasicMaterial({ color: COLORS.belowHorizon, side: THREE.BackSide, transparent: true, opacity: 0.85, depthWrite: false }),
  );
  band.position.copy(DOME_CENTER);
  band.renderOrder = -2;
  group.add(band);

  // Solid ground disc.
  const groundGeo = new THREE.CircleGeometry(groundRadius, 96);
  groundGeo.rotateX(-Math.PI / 2);
  group.add(new THREE.Mesh(groundGeo, new THREE.MeshStandardMaterial({ color: COLORS.ground, roughness: 0.95 })));

  // Faint compass ticks on the ground every 30° for orientation near the mount.
  {
    const pts = [];
    for (let az = 0; az < 360; az += 30) {
      const [x, , z] = altAzToDir(0, az);
      const r0 = az % 90 === 0 ? 14 : 16;
      pts.push(x * r0, 0.05, z * r0, x * 20, 0.05, z * 20);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
    group.add(new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: COLORS.grid, transparent: true, opacity: 0.35 })));
  }

  // Grid lines (single draw call).
  group.add(new THREE.LineSegments(
    gridGeometry(),
    new THREE.LineBasicMaterial({ color: COLORS.grid, transparent: true, opacity: 0.22, depthWrite: false }),
  ));

  // Horizon ring.
  {
    const pts = [];
    for (let az = 0; az <= 360; az += 1) pts.push(domePoint(0, az, 0.999));
    group.add(new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: COLORS.horizon, transparent: true, opacity: 0.8 }),
    ));
  }

  // Cardinal labels just above the horizon.
  for (const [t, az, col] of [['N', 0, '#ff8a6b'], ['E', 90, '#e8eefc'], ['S', 180, '#e8eefc'], ['W', 270, '#e8eefc']]) {
    const s = makeLabel(t, col);
    domePoint(4.5, az, 0.97, s.position);
    group.add(s);
  }

  return { object: group, groundAngleDeg: groundAngle / DEG };
}
