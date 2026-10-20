// Pure astro/geometry helpers (no three.js). Ported from scope-pointer/src/coords.js.
// Conventions: azimuth 0 = North, 90 = East (clockwise from above).
// Scene axes: +Y up, North = -Z, East = +X.

export const DEG = Math.PI / 180

export const wrap360 = (d: number): number => ((d % 360) + 360) % 360

/** Signed shortest angular delta from a to b, in (-180, 180]. */
export function shortestDelta(a: number, b: number): number {
  let d = wrap360(b - a)
  if (d > 180) d -= 360
  return d
}

export type Vec3 = [number, number, number]

/** Unit direction vector [x, y, z] in scene axes for an alt/az pair (deg). */
export function altAzToDir(altDeg: number, azDeg: number): Vec3 {
  const a = altDeg * DEG
  const z = azDeg * DEG
  return [Math.cos(a) * Math.sin(z), Math.sin(a), -Math.cos(a) * Math.cos(z)]
}

/** Hour angle (hours, +W) / declination (deg) -> {alt, az} (deg) at latitude latDeg. */
export function haDecToAltAz(haHours: number, decDeg: number, latDeg: number): { alt: number, az: number } {
  const H = haHours * 15 * DEG
  const d = decDeg * DEG
  const p = latDeg * DEG
  const sinAlt = Math.sin(p) * Math.sin(d) + Math.cos(p) * Math.cos(d) * Math.cos(H)
  const alt = Math.asin(Math.max(-1, Math.min(1, sinAlt)))
  const az = Math.atan2(
    -Math.cos(d) * Math.sin(H),
    Math.sin(d) * Math.cos(p) - Math.cos(d) * Math.sin(p) * Math.cos(H),
  )
  return { alt: alt / DEG, az: wrap360(az / DEG) }
}

/** Great-circle separation (deg) between two alt/az positions. */
export function angularSep(alt1: number, az1: number, alt2: number, az2: number): number {
  const a = altAzToDir(alt1, az1)
  const b = altAzToDir(alt2, az2)
  const dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
  return Math.acos(Math.max(-1, Math.min(1, dot))) / DEG
}
