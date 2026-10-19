// Mock data source: UI controls -> simulated mount + HA/Dec ephemeris -> store.
// Replace with a pwi4Source that writes the same store fields from the backend feed.

import { haDecToAltAz, shortestDelta, wrap360 } from '../coords.js';
import { SITE, SLEW_ACCEL, SLEW_PRESETS } from '../config.js';
import { store } from '../store.js';

const EPS_POS = 0.002;   // deg — "arrived" threshold
const EPS_VEL = 0.05;    // deg/s

export function createMockSource() {
  const inputs = {
    scope: { alt: 45, az: 135 },
    sun: { ha: -2.5, dec: -7.3 },
    moon: { ha: 3.0, dec: 12.0 },
  };
  const axis = {
    az: { pos: inputs.scope.az, vel: 0 },
    alt: { pos: inputs.scope.alt, vel: 0 },
  };
  let vmax = SLEW_PRESETS.normal;

  function publishBodies() {
    store.set({
      sun: haDecToAltAz(inputs.sun.ha, inputs.sun.dec, SITE.latDeg),
      moon: haDecToAltAz(inputs.moon.ha, inputs.moon.dec, SITE.latDeg),
    });
  }

  // Trapezoidal-profile step for one axis; returns true while moving.
  function stepAxis(ax, delta, dt) {
    if (Math.abs(delta) < EPS_POS && Math.abs(ax.vel) < EPS_VEL) {
      ax.vel = 0;
      return false;
    }
    const brakeSpeed = Math.sqrt(2 * SLEW_ACCEL * Math.abs(delta));
    const desired = Math.sign(delta) * Math.min(vmax, brakeSpeed);
    const dv = desired - ax.vel;
    const maxDv = SLEW_ACCEL * dt;
    ax.vel += Math.abs(dv) > maxDv ? Math.sign(dv) * maxDv : dv;
    let move = ax.vel * dt;
    if (Math.sign(move) === Math.sign(delta) && Math.abs(move) > Math.abs(delta)) move = delta;
    ax.pos += move;
    return true;
  }

  publishBodies();

  return {
    inputs,

    setScopeTarget({ alt = inputs.scope.alt, az = inputs.scope.az }) {
      inputs.scope = { alt, az: wrap360(az) };
      store.set({ mount: { targetAlt: inputs.scope.alt, targetAz: inputs.scope.az, isSlewing: true } });
    },

    setBody(name, { ha = inputs[name].ha, dec = inputs[name].dec }) {
      inputs[name] = { ha, dec };
      publishBodies();
    },

    setSlewRate(degPerSec) { vmax = degPerSec; },

    /** Advance the simulation; returns true while the mount is still moving. */
    update(dt) {
      const movingAz = stepAxis(axis.az, shortestDelta(axis.az.pos, inputs.scope.az), dt);
      const movingAlt = stepAxis(axis.alt, inputs.scope.alt - axis.alt.pos, dt);
      axis.az.pos = wrap360(axis.az.pos);
      const moving = movingAz || movingAlt;
      const m = store.data.mount;
      if (moving || m.isSlewing || m.alt !== axis.alt.pos || m.az !== axis.az.pos) {
        store.set({ mount: { alt: axis.alt.pos, az: axis.az.pos, isSlewing: moving } });
      }
      return moving;
    },
  };
}
