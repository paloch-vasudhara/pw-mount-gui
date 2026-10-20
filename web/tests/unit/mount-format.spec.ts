// T1.3: Mount Status display rules + telemetry store staleness.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import {
  buildMountDisplay, EM_DASH, formatAzimuth, formatElevation, formatSunDistance, isLinkStale,
  STALE_AFTER_MS, statusTone, sunDistanceTone,
} from '../../app/utils/mount-format'
import { useMountStore } from '../../app/stores/mount'

const MINUS = '−'

describe('red-warning rules (A4-i)', () => {
  it('status: green TRACKING, red FAULT, neutral otherwise', () => {
    expect(statusTone('TRACKING')).toBe('positive')
    expect(statusTone('FAULT')).toBe('danger')
    for (const s of ['STOPPED', 'SLEWING', 'HOMING', 'BUSY', 'DISCONNECTED', null, undefined]) {
      expect(statusTone(s)).toBe('normal')
    }
  })

  it('panel display: FAULT status is red, sun inside the keep-out is red', () => {
    const snap = { state: 'FAULT', state_reason: 'axis 0 disabled', mount: { az: 1, alt: 2 }, sun_distance_deg: 9.5, limits: { sun_keepout_deg: 15 } }
    const d = buildMountDisplay(snap, false)
    expect(d.status).toEqual({ text: 'FAULT', tone: 'danger' })
    expect(d.sunDistance.tone).toBe('danger')
    expect(buildMountDisplay({ ...snap, sun_distance_deg: 15.01 }, false).sunDistance.tone).toBe('positive')
  })

  it('stale link is never red: dimmed dashes and a neutral DISCONNECTED', () => {
    const d = buildMountDisplay({ state: 'FAULT', sun_distance_deg: 3 }, true)
    expect(d.status.tone).toBe('normal')
    expect(d.sunDistance.tone).toBe('dim')
  })
})

describe('formatters', () => {
  it('azimuth: 2 decimals, no sign, null is a dash', () => {
    expect(formatAzimuth(204.634)).toBe('204.63')
    expect(formatAzimuth(0)).toBe('0.00')
    expect(formatAzimuth(359.999)).toBe('0.00')
    expect(formatAzimuth(null)).toBe(EM_DASH)
    expect(formatAzimuth(undefined)).toBe(EM_DASH)
    expect(formatAzimuth(Number.NaN)).toBe(EM_DASH)
  })

  it('elevation: sign, space, 2 decimals, U+2212 minus', () => {
    expect(formatElevation(58.32)).toBe('+ 58.32')
    expect(formatElevation(-3.1)).toBe(`${MINUS} 3.10`)
    expect(formatElevation(0)).toBe('+ 0.00')
    expect(formatElevation(-0.004)).toBe('+ 0.00')
    expect(formatElevation(null)).toBe(EM_DASH)
  })

  it('sun distance: degree sign', () => {
    expect(formatSunDistance(136.17)).toBe('136.17°')
    expect(formatSunDistance(null)).toBe(EM_DASH)
  })

  it('sun tone: green strictly above the keep-out, red at or inside it (A4-i)', () => {
    expect(sunDistanceTone(15.0001)).toBe('positive')
    expect(sunDistanceTone(15)).toBe('danger')
    expect(sunDistanceTone(3)).toBe('danger')
    expect(sunDistanceTone(0)).toBe('danger')
    expect(sunDistanceTone(null)).toBe('dim')
    expect(sunDistanceTone(20, 25)).toBe('danger')
  })
})

describe('buildMountDisplay', () => {
  const snap = {
    state: 'TRACKING',
    state_reason: null,
    mount: { az: 204.63, alt: 58.32 },
    sun_distance_deg: 136.17,
    limits: { sun_keepout_deg: 15 },
  }

  it('tracking is green, sun far is green', () => {
    const d = buildMountDisplay(snap, false)
    expect(d.azimuth).toEqual({ text: '204.63', tone: 'normal' })
    expect(d.elevation.text).toBe('+ 58.32')
    expect(d.status).toEqual({ text: 'TRACKING', tone: 'positive' })
    expect(d.sunDistance).toEqual({ text: '136.17°', tone: 'positive' })
  })

  it('non-tracking status is neutral; sun at exactly 15 is red', () => {
    const d = buildMountDisplay({ ...snap, state: 'STOPPED', sun_distance_deg: 15 }, false)
    expect(d.status.tone).toBe('normal')
    expect(d.sunDistance.tone).toBe('danger')
  })

  it('null values show dimmed dashes', () => {
    const d = buildMountDisplay({ ...snap, mount: { az: null, alt: null }, sun_distance_deg: null }, false)
    expect(d.azimuth).toEqual({ text: EM_DASH, tone: 'dim' })
    expect(d.elevation).toEqual({ text: EM_DASH, tone: 'dim' })
    expect(d.sunDistance).toEqual({ text: EM_DASH, tone: 'dim' })
  })

  it('stale link: dashes, DISCONNECTED, gateway reason', () => {
    const d = buildMountDisplay(snap, true)
    expect(d.azimuth).toEqual({ text: EM_DASH, tone: 'dim' })
    expect(d.status.text).toBe('DISCONNECTED')
    expect(d.status.tone).toBe('normal')
    expect(d.statusReason).toBe('gateway not reachable')
  })

  it('carries the gateway state reason through', () => {
    const d = buildMountDisplay({ ...snap, state: 'DISCONNECTED', state_reason: 'PWI4 not running / not reachable' }, false)
    expect(d.statusReason).toBe('PWI4 not running / not reachable')
  })

  it('no snapshot yet shows dashes', () => {
    const d = buildMountDisplay(null, false)
    expect(d.status.text).toBe(EM_DASH)
    expect(d.azimuth.text).toBe(EM_DASH)
  })
})

describe('isLinkStale', () => {
  it('stale unless open with a fresh message', () => {
    expect(isLinkStale('closed', 1000, 1000)).toBe(true)
    expect(isLinkStale('connecting', 1000, 1000)).toBe(true)
    expect(isLinkStale('open', null, 1000)).toBe(true)
    expect(isLinkStale('open', 1000, 1000 + STALE_AFTER_MS)).toBe(false)
    expect(isLinkStale('open', 1000, 1000 + STALE_AFTER_MS + 1)).toBe(true)
  })
})

describe('mount store staleness', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-20T00:00:00Z'))
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('goes stale 1.5 s after the last status and recovers on the next one', () => {
    const s = useMountStore()
    s.startClock()
    s.setConnection('open')
    expect(s.linkStale).toBe(true) // open but nothing received yet
    s.ingestStatus({ state: 'TRACKING', mount: { az: 1, alt: 2 }, sun_distance_deg: 90 })
    expect(s.linkStale).toBe(false)
    expect(s.display.status.text).toBe('TRACKING')
    vi.advanceTimersByTime(1400)
    expect(s.linkStale).toBe(false)
    vi.advanceTimersByTime(350)
    expect(s.linkStale).toBe(true)
    expect(s.display.status.text).toBe('DISCONNECTED')
    s.ingestStatus({ state: 'STOPPED', mount: { az: 1, alt: 2 }, sun_distance_deg: 90 })
    expect(s.linkStale).toBe(false)
    expect(s.display.status.text).toBe('STOPPED')
    s.stopClock()
  })

  it('socket closing is stale immediately', () => {
    const s = useMountStore()
    s.setConnection('open')
    s.ingestStatus({ state: 'TRACKING' })
    expect(s.linkStale).toBe(false)
    s.setConnection('closed')
    expect(s.linkStale).toBe(true)
  })
})
