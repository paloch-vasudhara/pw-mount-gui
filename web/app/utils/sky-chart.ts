// Sky chart maths (T2.1), pure and allocation-free in the per-star loop.
//
// Projection: azimuthal equidistant, N up, E LEFT, W right. Ported from ssa-toolkit
// web/app/utils/observer-hemisphere.ts `projXY` (live mirror convention, "observer looking up")
// @84f2ded, parameterised by centre/radius instead of the fixed 640 px canvas.
//
// Stars: J2000 RA/Dec -> IAU 1976 precession to the date -> local hour angle -> alt/az.
// No nutation, aberration or refraction: < 0.02 deg vs skyfield apparent above 10 deg elevation.

import { LIM_MAG_STOPS, SKY_STOPS, STAR_ALPHA_STOPS } from '../lib/sky/theme'

const DEG = Math.PI / 180
const ARCSEC = DEG / 3600

/** Point at chart radius `r` px and azimuth `az` deg (N up, E left). */
export function projPolar(az: number, r: number, cx: number, cy: number) {
  const a = az * DEG
  return { x: cx - r * Math.sin(a), y: cy - r * Math.cos(a) }
}

/** Azimuthal-equidistant projection: r = R (1 - el/90). */
export function projXY(az: number, el: number, cx: number, cy: number, R: number) {
  return projPolar(az, R * (1 - el / 90), cx, cy)
}

export function jdFromMs(ms: number): number {
  return ms / 86400000 + 2440587.5
}

/** Greenwich mean sidereal time in degrees (IAU 1982; same polynomial as gateway/snapshot.py). */
export function gmstDeg(jd: number): number {
  const t = (jd - 2451545.0) / 36525.0
  const g
    = 280.46061837
      + 360.98564736629 * (jd - 2451545.0)
      + 0.000387933 * t * t
      - (t * t * t) / 38710000.0
  return ((g % 360) + 360) % 360
}

export function lmstDeg(jd: number, lonDegEast: number): number {
  return (((gmstDeg(jd) + lonDegEast) % 360) + 360) % 360
}

/** Rows of the J2000 -> mean-of-date rotation (IAU 1976), as a 9-element row-major array. */
export function precessionMatrix(jd: number): Float64Array {
  const T = (jd - 2451545.0) / 36525.0
  const zeta = (2306.2181 * T + 0.30188 * T * T + 0.017998 * T * T * T) * ARCSEC
  const z = (2306.2181 * T + 1.09468 * T * T + 0.018203 * T * T * T) * ARCSEC
  const th = (2004.3109 * T - 0.42665 * T * T - 0.041833 * T * T * T) * ARCSEC
  const cz = Math.cos(zeta), sz = Math.sin(zeta)
  const cZ = Math.cos(z), sZ = Math.sin(z)
  const ct = Math.cos(th), st = Math.sin(th)
  return Float64Array.of(
    cz * ct * cZ - sz * sZ, -sz * ct * cZ - cz * sZ, -st * cZ,
    cz * ct * sZ + sz * cZ, -sz * ct * sZ + cz * cZ, -st * sZ,
    cz * st, -sz * st, ct,
  )
}

/** Unit vectors (x,y,z interleaved) of J2000 RA/Dec degrees; computed once per catalogue. */
export function unitVectors(raDeg: Float64Array, decDeg: Float64Array): Float64Array {
  const n = raDeg.length
  const out = new Float64Array(n * 3)
  for (let i = 0; i < n; i++) {
    const a = raDeg[i]! * DEG
    const d = decDeg[i]! * DEG
    const cd = Math.cos(d)
    out[3 * i] = cd * Math.cos(a)
    out[3 * i + 1] = cd * Math.sin(a)
    out[3 * i + 2] = Math.sin(d)
  }
  return out
}

/**
 * Alt/az (deg) of `n` stars for the site and instant. `vec` from `unitVectors`.
 * Writes into `outAlt` / `outAz` (az: N=0, E=90). No allocation per star.
 */
export function altAzFromVectors(
  vec: Float64Array,
  n: number,
  jd: number,
  latDeg: number,
  lonDeg: number,
  outAlt: Float64Array,
  outAz: Float64Array,
): void {
  const P = precessionMatrix(jd)
  const L = lmstDeg(jd, lonDeg) * DEG
  const cL = Math.cos(L), sL = Math.sin(L)
  const phi = latDeg * DEG
  const sp = Math.sin(phi), cp = Math.cos(phi)
  const [p0, p1, p2, p3, p4, p5, p6, p7, p8] = P as unknown as number[]
  for (let i = 0; i < n; i++) {
    const x0 = vec[3 * i]!, y0 = vec[3 * i + 1]!, z0 = vec[3 * i + 2]!
    const X = p0! * x0 + p1! * y0 + p2! * z0
    const Y = p3! * x0 + p4! * y0 + p5! * z0
    const Z = p6! * x0 + p7! * y0 + p8! * z0
    const cdcH = X * cL + Y * sL // cos(dec) cos(HA)
    const cdsH = X * sL - Y * cL // cos(dec) sin(HA)
    let s = sp * Z + cp * cdcH
    s = s > 1 ? 1 : s < -1 ? -1 : s
    outAlt[i] = Math.asin(s) / DEG
    const az = Math.atan2(-cdsH, Z * cp - sp * cdcH) / DEG
    outAz[i] = az < 0 ? az + 360 : az
  }
}

/** Great-circle small circle of angular radius `rho` deg around (az0, alt0): n points. */
export function smallCircle(az0: number, alt0: number, rho: number, n = 72) {
  const a0 = alt0 * DEG
  const r = rho * DEG
  const pts: { az: number, alt: number }[] = []
  for (let k = 0; k < n; k++) {
    const b = (k / n) * 2 * Math.PI
    const sa = Math.sin(a0) * Math.cos(r) + Math.cos(a0) * Math.sin(r) * Math.cos(b)
    const alt = Math.asin(Math.max(-1, Math.min(1, sa)))
    const dAz = Math.atan2(Math.sin(b) * Math.sin(r) * Math.cos(a0), Math.cos(r) - Math.sin(a0) * Math.sin(alt))
    pts.push({ az: (((az0 * DEG + dAz) / DEG) % 360 + 360) % 360, alt: alt / DEG })
  }
  return pts
}

/** Star dot radius (css px at R=340) and alpha from magnitude (ssa-toolkit drawHemisphere sizing). */
export function starDot(vmag: number) {
  const br = 1 - vmag / 5.5
  return {
    radius: Math.max(0.8, 1 + 2 * br),
    alpha: Math.max(0.15, Math.min(0.9, 0.2 + 0.7 * br)),
  }
}

/** The slice of the gateway snapshot the sky viewer reads. */
export interface SkySnapshotLike {
  site?: { lat?: number | null, lon?: number | null, timezone?: string | null } | null
  mount?: { connected?: boolean | null, az?: number | null, alt?: number | null } | null
  sun?: { alt?: number | null, az?: number | null } | null
  moon?: { alt?: number | null, az?: number | null, illumination?: number | null } | null
  limits?: { lim_el_deg?: number | null, sun_keepout_deg?: number | null } | null
}

/**
 * Low-precision Sun elevation [deg] for a site and instant. Ported verbatim from ssa-toolkit
 * observer-hemisphere.ts `sunElevationApprox` (observer-view-hemisphere.js:99-111) @84f2ded.
 */
export function sunElevationApprox(date: Date, latDeg: number, lonDeg: number): number {
  const rad = Math.PI / 180
  const n = date.getTime() / 86400000 + 2440587.5 - 2451545.0
  const L = (280.460 + 0.9856474 * n) % 360
  const g = ((357.528 + 0.9856003 * n) % 360) * rad
  const lam = (L + 1.915 * Math.sin(g) + 0.020 * Math.sin(2 * g)) * rad
  const eps = (23.439 - 0.0000004 * n) * rad
  const ra = Math.atan2(Math.cos(eps) * Math.sin(lam), Math.cos(lam))
  const dec = Math.asin(Math.sin(eps) * Math.sin(lam))
  const lst = (((280.46061837 + 360.98564736629 * n) % 360) + lonDeg) * rad
  const H = lst - ra
  const lat = latDeg * rad
  return Math.asin(Math.sin(lat) * Math.sin(dec) + Math.cos(lat) * Math.cos(dec) * Math.cos(H)) / rad
}

/** Quantise a Sun altitude to 0.5 deg so the base layer only redraws when the colour can change. */
export function quantiseSunAlt(alt: number): number {
  return Math.round(alt * 2) / 2
}

/**
 * Sky disc RGB for a Sun altitude: piecewise-linear over SKY_STOPS (ssa-toolkit skyColor,
 * observer-hemisphere.ts @84f2ded, with bluer day stops). Clamps beyond the end stops.
 */
export function skyColor(sunAltDeg: number): [number, number, number] {
  const st = SKY_STOPS
  if (sunAltDeg >= st[0]!.alt) return [...st[0]!.rgb] as [number, number, number]
  for (let i = 0; i < st.length - 1; i++) {
    const a = st[i]!, b = st[i + 1]!
    if (sunAltDeg <= a.alt && sunAltDeg >= b.alt) {
      const f = (sunAltDeg - b.alt) / (a.alt - b.alt)
      return a.rgb.map((c, j) => Math.round(b.rgb[j]! + (c - b.rgb[j]!) * f)) as [number, number, number]
    }
  }
  return [...st[st.length - 1]!.rgb] as [number, number, number]
}

function lerpStops(stops: ReadonlyArray<readonly [number, number]>, x: number): number {
  if (x >= stops[0]![0]) return stops[0]![1]
  for (let i = 0; i < stops.length - 1; i++) {
    const [xa, ya] = stops[i]!
    const [xb, yb] = stops[i + 1]!
    if (x <= xa && x >= xb) return yb + ((x - xb) / (xa - xb)) * (ya - yb)
  }
  return stops[stops.length - 1]![1]
}

/** Faintest star magnitude visible: ~6.5 at Sun <= -18, ~4 at -6, ~1 at 0, none from +5. */
export function limitingMagnitude(sunAltDeg: number): number {
  return lerpStops(LIM_MAG_STOPS, sunAltDeg)
}

/** Star brightness factor 0..1: 1 at Sun <= -12 deg, 0.3 at 0, 0 from +5. */
export function starAlphaFactor(sunAltDeg: number): number {
  return lerpStops(STAR_ALPHA_STOPS, sunAltDeg)
}

/** 0 = night disc, 1 = full daylight disc, from the luminance of the sky colour. */
export function skyBrightness(sunAltDeg: number): number {
  const lum = (c: readonly number[]) => (0.2126 * c[0]! + 0.7152 * c[1]! + 0.0722 * c[2]!) / 255
  const night = lum(SKY_STOPS[SKY_STOPS.length - 1]!.rgb)
  const day = lum(SKY_STOPS[1]!.rgb) // brightest stop
  return Math.max(0, Math.min(1, (lum(skyColor(sunAltDeg)) - night) / (day - night)))
}

/** Blend two #rrggbb colours (t = 0 -> a, 1 -> b). */
export function mixHex(a: string, b: string, t: number): string {
  const p = (h: string, i: number) => parseInt(h.slice(1 + 2 * i, 3 + 2 * i), 16)
  const c = [0, 1, 2].map(i => Math.round(p(a, i) + (p(b, i) - p(a, i)) * t))
  return `rgb(${c[0]},${c[1]},${c[2]})`
}
