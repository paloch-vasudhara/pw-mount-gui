import { describe, expect, it } from 'vitest'
import fx from '../fixtures/sky-altaz.json'
import {
  altAzFromVectors, gmstDeg, jdFromMs, limitingMagnitude, mixHex, projXY, quantiseSunAlt, skyBrightness, skyColor,
  smallCircle, starAlphaFactor, starDot, sunElevationApprox, unitVectors,
} from '../../app/utils/sky-chart'

// Fixture generated once by tools/gen_sky_fixture.py (skyfield, topocentric apparent, no refraction).
interface Star { name: string, ra_deg: number, dec_deg: number, alt: number, az: number }

describe('projXY', () => {
  const cx = 300, cy = 300, R = 200
  it('zenith is the centre, horizon is R', () => {
    const z = projXY(123, 90, cx, cy, R)
    expect(z.x).toBeCloseTo(cx); expect(z.y).toBeCloseTo(cy)
    const h = projXY(40, 0, cx, cy, R)
    expect(Math.hypot(h.x - cx, h.y - cy)).toBeCloseTo(R)
  })
  it('N top, S bottom, E LEFT, W right', () => {
    const n = projXY(0, 0, cx, cy, R)
    expect(n.x).toBeCloseTo(cx); expect(n.y).toBeCloseTo(cy - R)
    const s = projXY(180, 0, cx, cy, R)
    expect(s.y).toBeCloseTo(cy + R)
    const e = projXY(90, 0, cx, cy, R)
    expect(e.x).toBeCloseTo(cx - R); expect(e.y).toBeCloseTo(cy)
    const w = projXY(270, 0, cx, cy, R)
    expect(w.x).toBeCloseTo(cx + R)
  })
  it('r is linear in elevation', () => {
    const p = projXY(0, 45, cx, cy, R)
    expect(cy - p.y).toBeCloseTo(R / 2)
  })
})

describe('star alt/az vs skyfield fixture', () => {
  for (const c of fx.cases) {
    it(`${c.utc}`, () => {
      const ra = Float64Array.from(c.stars.map((s: Star) => s.ra_deg))
      const dec = Float64Array.from(c.stars.map((s: Star) => s.dec_deg))
      const n = ra.length
      const alt = new Float64Array(n), az = new Float64Array(n)
      altAzFromVectors(unitVectors(ra, dec), n, jdFromMs(c.epoch_ms), fx.site.lat, fx.site.lon, alt, az)
      c.stars.forEach((s: Star, i: number) => {
        expect(Math.abs(alt[i]! - s.alt), `${s.name} alt`).toBeLessThan(0.05)
        let da = Math.abs(az[i]! - s.az)
        if (da > 180) da = 360 - da
        // az error scales with 1/cos(alt) near the pole: compare on-sky
        expect(da * Math.cos((s.alt * Math.PI) / 180), `${s.name} az`).toBeLessThan(0.05)
      })
    })
  }
  it('GMST at J2000.0 is 280.46 deg', () => {
    expect(gmstDeg(2451545.0)).toBeCloseTo(280.46061837, 6)
  })
})

describe('helpers', () => {
  it('small circle points stay at the angular radius', () => {
    const pts = smallCircle(200, 30, 15)
    expect(pts).toHaveLength(72)
    const d = (az1: number, a1: number, az2: number, a2: number) => {
      const r = Math.PI / 180
      return Math.acos(
        Math.sin(a1 * r) * Math.sin(a2 * r) + Math.cos(a1 * r) * Math.cos(a2 * r) * Math.cos((az1 - az2) * r),
      ) / r
    }
    for (const p of pts) expect(d(p.az, p.alt, 200, 30)).toBeCloseTo(15, 6)
  })
  it('brighter stars are bigger and more opaque', () => {
    expect(starDot(0).radius).toBeGreaterThan(starDot(6).radius)
    expect(starDot(0).alpha).toBeGreaterThan(starDot(6).alpha)
  })
})

describe('sky colour (T2.1b)', () => {
  it('night is the dark navy, day end is blue, sunset is orange', () => {
    expect(skyColor(-12)).toEqual([10, 14, 23])
    expect(skyColor(-40)).toEqual([10, 14, 23])
    expect(skyColor(20)).toEqual([170, 205, 245])
    expect(skyColor(60)).toEqual([170, 205, 245])
    expect(skyColor(5)).toEqual([205, 225, 250])
    expect(skyColor(0)).toEqual([220, 140, 100])
    expect(skyColor(-6)).toEqual([40, 30, 70])
  })
  it('interpolates between stops and never leaves 0..255', () => {
    expect(skyColor(-9)).toEqual([25, 22, 47]) // halfway -6 / -12
    for (let a = -90; a <= 90; a += 0.5) for (const c of skyColor(a)) expect(c >= 0 && c <= 255).toBe(true)
  })
  it('brightness: 0 at night, 1 at +5, monotone through twilight', () => {
    expect(skyBrightness(-20)).toBe(0)
    expect(skyBrightness(5)).toBeCloseTo(1, 6)
    expect(skyBrightness(-3)).toBeGreaterThan(skyBrightness(-8))
    expect(skyBrightness(30)).toBeGreaterThan(0.8)
  })
  it('limiting magnitude: 6.5 at -18, 4 at -6, 1 at 0, none at +5', () => {
    expect(limitingMagnitude(-30)).toBe(6.5)
    expect(limitingMagnitude(-18)).toBe(6.5)
    expect(limitingMagnitude(-6)).toBeCloseTo(4, 6)
    expect(limitingMagnitude(0)).toBeCloseTo(1, 6)
    expect(limitingMagnitude(5)).toBeLessThan(-1) // nothing is visible
    expect(limitingMagnitude(30)).toBeLessThan(-1)
    expect(limitingMagnitude(-12)).toBeGreaterThan(limitingMagnitude(-6))
  })
  it('star alpha factor: 1 at night, falls with brightness, 0 from +5', () => {
    expect(starAlphaFactor(-30)).toBe(1)
    expect(starAlphaFactor(-12)).toBe(1)
    expect(starAlphaFactor(0)).toBeCloseTo(0.3, 6)
    expect(starAlphaFactor(5)).toBe(0)
    expect(starAlphaFactor(40)).toBe(0)
    expect(starAlphaFactor(-6)).toBeGreaterThan(starAlphaFactor(-2))
  })
  it('quantises to 0.5 deg', () => {
    expect(quantiseSunAlt(10.26)).toBe(10.5)
    expect(quantiseSunAlt(-3.74)).toBe(-3.5)
    expect(quantiseSunAlt(-3.76)).toBe(-4)
  })
  it('mixHex blends', () => {
    expect(mixHex('#000000', '#ffffff', 0.5)).toBe('rgb(128,128,128)')
    expect(mixHex('#102030', '#102030', 0.7)).toBe('rgb(16,32,48)')
  })
  it('sunElevationApprox agrees with the gateway Sun to a degree or two', () => {
    // 2026-10-21 02:48 UTC at Bangkok-ish: Sun high in the sky (gateway reported ~49 deg).
    const a = sunElevationApprox(new Date(Date.UTC(2026, 9, 21, 2, 48)), 13.7862, 101.4639)
    expect(a).toBeGreaterThan(46)
    expect(a).toBeLessThan(52)
    // midnight local: well below the horizon
    expect(sunElevationApprox(new Date(Date.UTC(2026, 9, 20, 17, 0)), 13.7862, 101.4639)).toBeLessThan(-30)
  })
})
