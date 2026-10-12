// Pure astro/geometry helpers (no three.js) — unit-testable in node.
// Conventions: azimuth 0 = North, 90 = East (clockwise from above).
// Scene axes: +Y up, North = -Z, East = +X.

export const DEG = Math.PI / 180;

export const wrap360 = (d) => ((d % 360) + 360) % 360;

/** Wrap hour angle into [-12, 12). */
export const wrapHA = (h) => ((((h + 12) % 24) + 24) % 24) - 12;

/** Signed shortest angular delta from a to b, in (-180, 180]. */
export function shortestDelta(a, b) {
  let d = wrap360(b - a);
  if (d > 180) d -= 360;
  return d;
}

/** Unit direction vector [x, y, z] in scene axes for an alt/az pair (deg). */
export function altAzToDir(altDeg, azDeg) {
  const a = altDeg * DEG, z = azDeg * DEG;
  return [Math.cos(a) * Math.sin(z), Math.sin(a), -Math.cos(a) * Math.cos(z)];
}

/** Hour angle (hours, +W) / declination (deg) -> {alt, az} (deg) at latitude latDeg. */
export function haDecToAltAz(haHours, decDeg, latDeg) {
  const H = haHours * 15 * DEG, d = decDeg * DEG, p = latDeg * DEG;
  const sinAlt = Math.sin(p) * Math.sin(d) + Math.cos(p) * Math.cos(d) * Math.cos(H);
  const alt = Math.asin(Math.max(-1, Math.min(1, sinAlt)));
  const az = Math.atan2(
    -Math.cos(d) * Math.sin(H),
    Math.sin(d) * Math.cos(p) - Math.cos(d) * Math.sin(p) * Math.cos(H),
  );
  return { alt: alt / DEG, az: wrap360(az / DEG) };
}

/** Great-circle separation (deg) between two alt/az positions. */
export function angularSep(alt1, az1, alt2, az2) {
  const a = altAzToDir(alt1, az1), b = altAzToDir(alt2, az2);
  const dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  return Math.acos(Math.max(-1, Math.min(1, dot))) / DEG;
}

/** Illuminated fraction of the Moon from Sun–Moon elongation (deg). */
export const moonIllumination = (elongDeg) => (1 - Math.cos(elongDeg * DEG)) / 2;

export const fmtDeg = (v, digits = 2) => `${v.toFixed(digits)}°`;

export function fmtHA(h) {
  const sign = h < 0 ? '−' : '+';
  const t = Math.abs(h);
  let hh = Math.floor(t), mm = Math.round((t - hh) * 60);
  if (mm === 60) { hh += 1; mm = 0; }
  return `${sign}${String(hh).padStart(2, '0')}h${String(mm).padStart(2, '0')}m`;
}
