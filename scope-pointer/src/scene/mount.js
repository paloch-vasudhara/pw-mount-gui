// Simplified PlaneWave L-350 (single-arm alt-az) + Celestron RASA 11, in inches.
// Dimensions follow the L350 plan drawing: alt axis at 33.5", overall 39.7",
// footprint 19.95" x 15.45". Tripod omitted (model starts at the base flange).
//
// Hierarchy:  root (fixed base) -> azGroup (rotation.y = -az) -> altGroup (rotation.x = alt)
// In azGroup/altGroup local space the tube points along -Z at alt 0. The arm sits on -X,
// the D-shaped base housing and the OTA on +X so the payload rides over the azimuth axis.

import * as THREE from 'three';
import { ALT_AXIS_HEIGHT } from '../config.js';

const M = {
  white: new THREE.MeshStandardMaterial({ color: 0xf2f2ee, roughness: 0.55, metalness: 0.05 }),
  panel: new THREE.MeshStandardMaterial({ color: 0xe1e1dc, roughness: 0.6 }),
  black: new THREE.MeshStandardMaterial({ color: 0x17181a, roughness: 0.45, metalness: 0.2 }),
  steel: new THREE.MeshStandardMaterial({ color: 0xb8bcc2, roughness: 0.35, metalness: 0.7 }),
  orange: new THREE.MeshStandardMaterial({ color: 0xd8692a, roughness: 0.45, metalness: 0.3 }),
  red: new THREE.MeshStandardMaterial({ color: 0xc8202a, roughness: 0.4, metalness: 0.35 }),
  glass: new THREE.MeshStandardMaterial({ color: 0x0c1626, roughness: 0.08, metalness: 0.9 }),
};

const mesh = (geo, mat, { x = 0, y = 0, z = 0 } = {}) => {
  const m = new THREE.Mesh(geo, mat);
  m.position.set(x, y, z);
  m.castShadow = m.receiveShadow = false;
  return m;
};

/** Cylinder whose axis runs along X. */
const cylX = (r, len, mat, pos) => {
  const g = new THREE.CylinderGeometry(r, r, len, 48);
  g.rotateZ(Math.PI / 2);
  return mesh(g, mat, pos);
};

/** Cylinder whose axis runs along Z. */
const cylZ = (r, len, mat, pos, rTop = r) => {
  const g = new THREE.CylinderGeometry(rTop, r, len, 48);
  g.rotateX(Math.PI / 2);
  return mesh(g, mat, pos);
};

function buildFixedBase() {
  const g = new THREE.Group();
  g.add(mesh(new THREE.CylinderGeometry(8.2, 8.2, 0.7, 48), M.white, { y: 0.35 }));     // flange
  g.add(mesh(new THREE.CylinderGeometry(7.4, 7.6, 3.6, 48), M.white, { y: 2.5 }));      // az bearing ring
  g.add(mesh(new THREE.CylinderGeometry(7.45, 7.45, 0.25, 48), M.black, { y: 4.35 }));  // seam
  const knob = mesh(new THREE.CylinderGeometry(0.6, 0.6, 1.4, 12), M.black, { y: 2.4, z: 8.1 });
  knob.rotation.x = Math.PI / 2;
  g.add(knob);                                                                           // az lock knob
  return g;
}

function buildArmAndHousing() {
  const g = new THREE.Group();
  const yBase = 4.5;

  // D-shaped base housing (top view: stadium, rounded end toward +X).
  const d = new THREE.Shape();
  d.moveTo(-3, -7.2);
  d.lineTo(3.8, -7.2);
  d.absarc(3.8, 0, 7.2, -Math.PI / 2, Math.PI / 2, false);
  d.lineTo(-3, 7.2);
  d.closePath();
  const dGeo = new THREE.ExtrudeGeometry(d, {
    depth: 8.6, bevelEnabled: true, bevelSize: 0.35, bevelThickness: 0.35, bevelSegments: 2, curveSegments: 32,
  });
  dGeo.rotateX(-Math.PI / 2);
  g.add(mesh(dGeo, M.white, { y: yBase }));

  // Arm column: tapered profile in (z, y), extruded 6" along X (x = -9 .. -3).
  const p = new THREE.Shape();
  p.moveTo(-7.2, yBase);
  p.lineTo(7.2, yBase);
  p.lineTo(6.3, 22);
  p.lineTo(6.3, ALT_AXIS_HEIGHT);
  p.lineTo(-6.3, ALT_AXIS_HEIGHT);
  p.lineTo(-6.3, 22);
  p.closePath();
  const aGeo = new THREE.ExtrudeGeometry(p, {
    depth: 6, bevelEnabled: true, bevelSize: 0.3, bevelThickness: 0.3, bevelSegments: 2,
  });
  aGeo.rotateY(Math.PI / 2);          // extrusion now along +X, profile x -> -Z (symmetric)
  g.add(mesh(aGeo, M.white, { x: -9 }));

  // Recessed service panels on the outer face (x = -9).
  for (const [y, h] of [[9.5, 5.5], [16.2, 5.5], [23, 5.5]]) {
    g.add(mesh(new THREE.BoxGeometry(0.25, h, 8.4), M.panel, { x: -9.4, y }));
  }

  // Altitude drive drum + black faceplate (outer) + encoder hub.
  g.add(cylX(6.6, 6.2, M.white, { x: -6, y: ALT_AXIS_HEIGHT }));
  g.add(cylX(6.35, 0.5, M.black, { x: -9.3, y: ALT_AXIS_HEIGHT }));
  g.add(cylX(2.0, 0.4, M.steel, { x: -9.7, y: ALT_AXIS_HEIGHT }));
  g.add(cylX(0.9, 0.5, M.black, { x: -9.9, y: ALT_AXIS_HEIGHT }));

  // Side handles.
  for (const s of [-1, 1]) {
    g.add(mesh(new THREE.BoxGeometry(1.0, 5.2, 0.9), M.black, { x: -6, y: 16.5, z: s * 8.4 }));
    for (const dy of [-2.2, 2.2]) {
      g.add(mesh(new THREE.BoxGeometry(0.9, 0.8, 1.4), M.black, { x: -6, y: 16.5 + dy, z: s * 7.6 }));
    }
  }
  return g;
}

/** OTA assembly in altGroup space (origin on the alt axis). Returns { group, apertureLocal }. */
function buildOTA() {
  const g = new THREE.Group();
  g.add(cylX(5.6, 0.6, M.black, { x: -2.8 }));                                        // saddle plate on drum
  g.add(mesh(new THREE.BoxGeometry(1.2, 3.6, 9.5), M.black, { x: -2.0 }));             // saddle clamp
  g.add(mesh(new THREE.BoxGeometry(0.8, 4.0, 18), M.orange, { x: -1.0 }));             // dovetail bar

  const tubeX = 5.7, R = 6.3;
  // Tube runs z = -14 (front) .. +16 (rear).
  g.add(cylZ(R + 0.25, 3.0, M.black, { x: tubeX, z: -12.5 }));                        // front ring
  g.add(cylZ(R, 23.5, M.white, { x: tubeX, z: 0.75 }));                                // tube
  g.add(cylZ(R + 0.25, 3.5, M.black, { x: tubeX, z: 14.25 }));                         // rear cell
  g.add(cylZ(4.6, 0.6, M.black, { x: tubeX, z: 16.2 }));                               // rear cap
  g.add(cylZ(5.7, 0.2, M.glass, { x: tubeX, z: -13.6 }));                              // corrector
  g.add(cylZ(2.4, 3.2, M.red, { x: tubeX, z: -15.3 }));                                // camera
  g.add(cylZ(2.45, 0.35, M.black, { x: tubeX, z: -16.95 }));                           // camera back

  return { group: g, apertureLocal: new THREE.Vector3(tubeX, 0, -17.2) };
}

export function createMount() {
  const root = new THREE.Group();
  root.add(buildFixedBase());

  const azGroup = new THREE.Group();
  azGroup.add(buildArmAndHousing());
  root.add(azGroup);

  const altGroup = new THREE.Group();
  altGroup.position.y = ALT_AXIS_HEIGHT;
  const ota = buildOTA();
  altGroup.add(ota.group);
  azGroup.add(altGroup);

  const tmp = new THREE.Vector3();
  return {
    object: root,
    /** Pose the mount at a given altitude/azimuth (deg). */
    setPose(altDeg, azDeg) {
      azGroup.rotation.y = -azDeg * Math.PI / 180;
      altGroup.rotation.x = altDeg * Math.PI / 180;
    },
    /** World-space point at the front of the optical train (arrow origin). */
    apertureWorld(out = tmp) {
      root.updateMatrixWorld(true);
      return altGroup.localToWorld(out.copy(ota.apertureLocal));
    },
  };
}
