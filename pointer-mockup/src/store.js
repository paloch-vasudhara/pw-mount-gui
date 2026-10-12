// Shared state the 3D view reads. Data sources (mock controls now, PWI4 later)
// write ONLY through store.set(); the view never knows where values came from.
//
// Shape mirrors what PWI4 status + a local ephemeris will provide:
//   mount: alt/az actually reported, target alt/az, is_slewing
//   sun / moon: topocentric alt/az

const listeners = new Set();

export const store = {
  data: {
    mount: { alt: 45, az: 135, targetAlt: 45, targetAz: 135, isSlewing: false, connected: true },
    sun: { alt: 0, az: 0 },
    moon: { alt: 0, az: 0 },
  },

  /** Shallow-merge per section: store.set({ mount: { alt: 10 } }). */
  set(patch) {
    for (const key of Object.keys(patch)) {
      this.data[key] = { ...this.data[key], ...patch[key] };
    }
    for (const fn of listeners) fn(this.data);
  },

  subscribe(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
};
