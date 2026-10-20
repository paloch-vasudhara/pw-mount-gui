// T1.4: Detailed Mount Status modal formatters + display builder + 3D inputs.
import { describe, expect, it } from 'vitest'
import {
  buildDetailsDisplay, formatAzimuth3, formatDec, formatElevation3, formatHourAngle, formatLst, formatMjd,
  formatRa, formatRms, formatSunDistance3, SUP_H, SUP_M, SUP_S,
} from '../../app/utils/mount-details-format'
import { EM_DASH } from '../../app/utils/mount-format'
import { buildHemisphereInputs } from '../../app/lib/hemisphere3d/inputs'
import { angularSep, haDecToAltAz, shortestDelta } from '../../app/lib/hemisphere3d/coords'

const MINUS = '−'

describe('modal formatters', () => {
  it('MJD: 5 decimals, single-space thousands', () => {
    expect(formatMjd(61333.19885)).toBe('61 333.19885')
    expect(formatMjd(61308)).toBe('61 308.00000')
    expect(formatMjd(999.5)).toBe('999.50000')
    expect(formatMjd(1234567.123456)).toBe('1 234 567.12346')
    expect(formatMjd(null)).toBe(EM_DASH)
  })

  it('LST: HH:MM:SS with wrap and carry', () => {
    expect(formatLst(19 + 34 / 60 + 25 / 3600)).toBe('19:34:25')
    expect(formatLst(0)).toBe('00:00:00')
    expect(formatLst(23.99999999)).toBe('00:00:00')
    expect(formatLst(24 + 1.5)).toBe('01:30:00')
    expect(formatLst(-1)).toBe('23:00:00')
    expect(formatLst(undefined)).toBe(EM_DASH)
  })

  it('azimuth / elevation: 3 decimals, degree sign, minus only when negative', () => {
    expect(formatAzimuth3(320.5)).toBe('320.500°')
    expect(formatAzimuth3(359.9999)).toBe('0.000°')
    expect(formatAzimuth3(null)).toBe(EM_DASH)
    expect(formatElevation3(39.302)).toBe('39.302°')
    expect(formatElevation3(-3.1)).toBe(`${MINUS}3.100°`)
    expect(formatElevation3(-0.0001)).toBe('0.000°')
    expect(formatElevation3(Number.NaN)).toBe(EM_DASH)
  })

  it('RA: superscript h/m/s, wrapped, seconds carry', () => {
    expect(formatRa(16 + 25 / 60 + 43.32 / 3600)).toBe(`16${SUP_H} 25${SUP_M} 43.32${SUP_S}`)
    expect(formatRa(0)).toBe(`00${SUP_H} 00${SUP_M} 00.00${SUP_S}`)
    expect(formatRa(5.999999999)).toBe(`06${SUP_H} 00${SUP_M} 00.00${SUP_S}`)
    expect(formatRa(23.9999999999)).toBe(`00${SUP_H} 00${SUP_M} 00.00${SUP_S}`)
    expect(formatRa(25)).toBe(`01${SUP_H} 00${SUP_M} 00.00${SUP_S}`)
    expect(formatRa(null)).toBe(EM_DASH)
    expect(SUP_H).toBe('ʰ')
    expect(SUP_M).toBe('ᵐ')
    expect(SUP_S).toBe('ˢ')
  })

  it('Dec: signed DMS with prime / double-prime', () => {
    expect(formatDec(50 + 8 / 60 + 39.3 / 3600)).toBe('+50° 08′ 39.30″')
    expect(formatDec(-(5 + 59 / 60 + 59.999 / 3600))).toBe(`${MINUS}06° 00′ 00.00″`)
    expect(formatDec(0)).toBe('+00° 00′ 00.00″')
    expect(formatDec(-0.00000001)).toBe('+00° 00′ 00.00″')
    expect(formatDec(90)).toBe('+90° 00′ 00.00″')
    expect(formatDec(undefined)).toBe(EM_DASH)
  })

  it('Hour Angle: h/m/s superscripts, minus only when negative', () => {
    expect(formatHourAngle(3 + 12 / 60 + 20.11 / 3600)).toBe(`03${SUP_H} 12${SUP_M} 20.11${SUP_S}`)
    expect(formatHourAngle(-(3 + 12 / 60 + 20.11 / 3600))).toBe(`${MINUS}03${SUP_H} 12${SUP_M} 20.11${SUP_S}`)
    expect(formatHourAngle(0)).toBe(`00${SUP_H} 00${SUP_M} 00.00${SUP_S}`)
    expect(formatHourAngle(null)).toBe(EM_DASH)
  })

  it('RMS and sun distance', () => {
    expect(formatRms(0.56)).toBe('0.560″')
    expect(formatRms(0.4712)).toBe('0.471″')
    expect(formatRms(null)).toBe(EM_DASH)
    expect(formatSunDistance3(82.27)).toBe('82.270°')
    expect(formatSunDistance3(null)).toBe(EM_DASH)
  })
})

const SNAP = {
  state: 'TRACKING',
  state_reason: null,
  time: { mjd: 61333.19885, lmst_hours: 19.573611 },
  mount: { connected: true, az: 320.5, alt: 39.302, ra_j2000_h: 16.4287, dec_j2000_deg: 50.14425 },
  axes: [{ enabled: true, rms_arcsec: 0.471 }, { enabled: false, rms_arcsec: 0.56 }],
  hour_angle_h: 3.205586,
  sun_distance_deg: 82.27,
  limits: { sun_keepout_deg: 15 },
}

describe('buildDetailsDisplay', () => {
  it('formats every parameter and applies the red / green rules', () => {
    const d = buildDetailsDisplay(SNAP, false)
    expect(d.mjd.text).toBe('61 333.19885')
    expect(d.lst.text).toBe('19:34:25')
    expect(d.azimuth.text).toBe('320.500°')
    expect(d.status).toEqual({ text: 'TRACKING', tone: 'positive' })
    expect(d.axis0Status).toEqual({ text: 'Enabled', tone: 'normal', dot: 'on' })
    expect(d.axis1Status).toEqual({ text: 'Disabled', tone: 'normal', dot: 'off' })
    expect(d.axis0Rms.text).toBe('0.471″')
    expect(d.axis1Rms.text).toBe('0.560″')
    expect(d.sunDistance).toEqual({ text: '82.270°', tone: 'positive' })
    expect(d.tracking).toBe(true)

    const bad = buildDetailsDisplay({ ...SNAP, state: 'FAULT', sun_distance_deg: 15 }, false)
    expect(bad.status.tone).toBe('danger')
    expect(bad.sunDistance.tone).toBe('danger')
    expect(bad.tracking).toBe(false)
  })

  it('null fields and an unconnected mount are dimmed dashes', () => {
    const d = buildDetailsDisplay({ ...SNAP, mount: { connected: false, az: null, alt: null }, state: 'DISCONNECTED' }, false)
    expect(d.azimuth).toEqual({ text: EM_DASH, tone: 'dim' })
    expect(d.axis0Status.tone).toBe('dim')
    expect(d.axis1Rms.tone).toBe('dim')
    expect(d.mountConnected).toBe(false)
  })

  it('stale link: everything dashes, status DISCONNECTED (never red)', () => {
    const d = buildDetailsDisplay({ ...SNAP, state: 'FAULT' }, true)
    expect(d.status).toEqual({ text: 'DISCONNECTED', tone: 'normal' })
    expect(d.sunDistance.tone).toBe('dim')
    expect(d.mjd.text).toBe(EM_DASH)
    expect(buildDetailsDisplay(null, false).status.tone).toBe('dim')
  })
})

describe('3D inputs', () => {
  const snap = {
    state: 'TRACKING',
    site: { lat: 14.3479 },
    time: { lmst_hours: 5 },
    mount: { az: 100, alt: 40, target_ra_apparent_h: 3, target_dec_apparent_deg: 20 },
    sun: { alt: 10, az: 250 },
    moon: { alt: -5, az: 30 },
    limits: { lim_el_deg: 15, sun_keepout_deg: 15 },
  }

  it('maps pointing, bodies and limits; no target unless SLEWING', () => {
    const i = buildHemisphereInputs(snap, false)
    expect(i.mount).toEqual({ alt: 40, az: 100 })
    expect(i.sun).toEqual({ alt: 10, az: 250 })
    expect(i.limElDeg).toBe(15)
    expect(i.keepoutDeg).toBe(15)
    expect(i.target).toBeNull()
  })

  it('SLEWING converts the apparent RA/Dec target to alt/az via LMST and latitude', () => {
    const i = buildHemisphereInputs({ ...snap, state: 'SLEWING' }, false)
    const want = haDecToAltAz(5 - 3, 20, 14.3479)
    expect(i.target!.alt).toBeCloseTo(want.alt, 9)
    expect(i.target!.az).toBeCloseTo(want.az, 9)
    expect(buildHemisphereInputs({ ...snap, state: 'SLEWING', time: { lmst_hours: null } }, false).target).toBeNull()
  })

  it('stale link or disconnected mount hides the pointer but keeps the sky', () => {
    const stale = buildHemisphereInputs(snap, true)
    expect(stale.mount).toBeNull()
    expect(stale.sun).not.toBeNull()
    const gone = buildHemisphereInputs({ ...snap, mount: { az: null, alt: null } }, false)
    expect(gone.mount).toBeNull()
    expect(buildHemisphereInputs(null, false).mount).toBeNull()
  })

  it('falls back to the 15 deg keep-out and no ring when limits are missing', () => {
    const i = buildHemisphereInputs({ ...snap, limits: null }, false)
    expect(i.keepoutDeg).toBe(15)
    expect(i.limElDeg).toBeNull()
  })
})

describe('3D coordinate helpers (ported)', () => {
  it('zenith, horizon and great-circle separation', () => {
    expect(angularSep(90, 0, 90, 180)).toBeCloseTo(0, 6)
    expect(angularSep(0, 0, 0, 90)).toBeCloseTo(90, 6)
    expect(angularSep(45, 10, 45, 10)).toBeCloseTo(0, 6)
  })

  it('shortestDelta wraps through north', () => {
    expect(shortestDelta(350, 10)).toBeCloseTo(20, 9)
    expect(shortestDelta(10, 350)).toBeCloseTo(-20, 9)
  })

  it('HA 0 on the meridian: due south at lat 45 for dec 0 (alt 45)', () => {
    const r = haDecToAltAz(0, 0, 45)
    expect(r.alt).toBeCloseTo(45, 6)
    expect(r.az).toBeCloseTo(180, 6)
  })
})
