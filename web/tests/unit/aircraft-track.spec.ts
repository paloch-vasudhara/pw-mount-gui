// @vitest-environment happy-dom
// T5.6b aircraft cueing, web side: the TRACK AIRCRAFT verdict branches (local + the polled gateway check),
// the A1b card's phase / ended lines and the button, and the Track Target AIRCRAFT tab's TRACK SELECTED TARGET.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import TrackCard from '../../app/components/track/TrackCard.vue'
import TrackTargetDialog from '../../app/components/track/TrackTargetDialog.vue'
import type { AdsbInfo, AircraftRow } from '../../app/utils/adsb'
import { ADSB_STATUS_UNAVAILABLE, AIRCRAFT_NOT_IN_VIEW, aircraftTrackVerdict } from '../../app/utils/aircraft'
import { airTrackCardState, isAirTrackActive, parseAircraftVerdict } from '../../app/utils/aircraft-track'
import { jogGateReason } from '../../app/utils/jog-modal'
import { useAircraftStore } from '../../app/stores/aircraft'
import { useMountStore } from '../../app/stores/mount'
import { useSelectionStore } from '../../app/stores/selection'

const api = vi.hoisted(() => ({ post: vi.fn(async (_path: string, _body?: unknown): Promise<unknown> => ({ ok: true, reason: null, notes: [] })) }))
vi.mock('~/composables/useGatewayApi', () => ({
  useGatewayApi: () => api,
}))

const GREEN: AdsbInfo = { state: 'connected', green: true, simulated: false, detail: 'Connected to the ADS-B platform' }
const DOWN: AdsbInfo = { state: 'unreachable', green: false, simulated: false, detail: 'ADS-B platform unreachable' }

function row(o: Partial<AircraftRow> = {}): AircraftRow {
  return {
    icao: 'ABC123', callsign: 'THA101', lat: 13.69, lon: 100.75, az: 90, el: 30, range_m: 23_456, track_deg: 90,
    alt_ft: 35_000, gs_kt: 451, vrate_fpm: 1500, pos_age_s: 1.2, stale: false, height_source: 'gnss', predictor: 'velocity', ...o,
  }
}

describe('aircraftTrackVerdict', () => {
  it('local branches: no ADS-B status, not green, not in view (never needs a request)', () => {
    expect(aircraftTrackVerdict(row(), null)).toEqual({ enabled: false, reason: ADSB_STATUS_UNAVAILABLE })
    expect(aircraftTrackVerdict(row(), DOWN)).toEqual({ enabled: false, reason: 'ADS-B platform unreachable' })
    expect(aircraftTrackVerdict(null, GREEN)).toEqual({ enabled: false, reason: AIRCRAFT_NOT_IN_VIEW })
    // a polled answer never overrides a local refusal
    const ok = parseAircraftVerdict({ ok: true, reason: null, notes: [] })
    expect(aircraftTrackVerdict(row(), DOWN, ok).enabled).toBe(false)
    expect(aircraftTrackVerdict(null, GREEN, ok).reason).toBe(AIRCRAFT_NOT_IN_VIEW)
  })

  it('last branch: the polled gateway check (refusal reason, ok, ok with a note, unreachable)', () => {
    expect(aircraftTrackVerdict(row(), GREEN, null)).toEqual({ enabled: false, reason: null })
    const refused = parseAircraftVerdict({ ok: false, reason: 'Below Lim EL', code: 'below_limit' })
    expect(aircraftTrackVerdict(row(), GREEN, refused)).toEqual({ enabled: false, reason: 'Below Lim EL' })
    const ok = parseAircraftVerdict({ ok: true, reason: null, notes: [] })
    expect(aircraftTrackVerdict(row(), GREEN, ok)).toEqual({ enabled: true, reason: null })
    const note = parseAircraftVerdict({ ok: true, reason: null, notes: ['Near-zenith overflight: tracking lags near the peak'] })
    expect(aircraftTrackVerdict(row(), GREEN, note)).toEqual({
      enabled: true, reason: 'Near-zenith overflight: tracking lags near the peak',
    })
    const bad = parseAircraftVerdict('garbage')
    expect(aircraftTrackVerdict(row(), GREEN, bad)).toEqual({ enabled: false, reason: 'Gateway not responding' })
  })
})

describe('airTrackCardState', () => {
  const run = (o: object) => ({ icao: 'ABC123', callsign: 'THA101', phase: 'tracking', t_r: 100, notes: [], ended: null, ...o })

  it('phase lines of the tracked aircraft; the button is disabled while it is tracked', () => {
    expect(airTrackCardState(run({ phase: 'slewing' }), 'ABC123')).toEqual({ isTracked: true, line: 'Slewing to rendezvous' })
    expect(airTrackCardState(run({ phase: 'waiting' }), 'ABC123')).toEqual({ isTracked: true, line: 'Waiting for the aircraft' })
    expect(airTrackCardState(run({ phase: 'tracking' }), 'ABC123')).toEqual({ isTracked: true, line: 'Tracking' })
    expect(airTrackCardState(run({ notes: ['Near-zenith overflight: tracking lags near the peak'] }), 'ABC123').line)
      .toBe('Tracking · Near-zenith overflight: tracking lags near the peak')
  })

  it('another aircraft, no track, no selection: nothing to show', () => {
    expect(airTrackCardState(run({}), 'ZZZ999')).toEqual({ isTracked: false, line: null })
    expect(airTrackCardState(null, 'ABC123')).toEqual({ isTracked: false, line: null })
    expect(airTrackCardState(run({}), null)).toEqual({ isTracked: false, line: null })
    expect(isAirTrackActive(run({}))).toBe(true)
    expect(isAirTrackActive(run({ phase: null }))).toBe(false)
    expect(isAirTrackActive(null)).toBe(false)
  })

  it('the ended reason (gateway: 30 s) for the aircraft that ended', () => {
    const ended = { icao: null, callsign: null, phase: null, t_r: null, notes: [], ended: { icao: 'ABC123', reason: 'Aircraft below Lim EL', at: 5 } }
    expect(airTrackCardState(ended, 'ABC123')).toEqual({ isTracked: false, line: 'Aircraft below Lim EL' })
    expect(airTrackCardState(ended, 'ZZZ999')).toEqual({ isTracked: false, line: null })
  })

  it('the jog pad is gated while an aircraft track runs', () => {
    expect(jogGateReason(null, { airtrack: { phase: 'tracking' } })).toBe('Object tracking; press STOP first')
    expect(jogGateReason(null, { airtrack: null, track: null })).toBeNull()
  })
})

// ---------------------------------------------------------------- components

let w: VueWrapper | null = null
const q = (sel: string) => document.body.querySelector(sel) as HTMLElement | null
const text = (id: string) => q(`[data-testid="${id}"]`)?.textContent?.replace(/\s+/g, ' ').trim()

function setup(adsb: AdsbInfo | null, rows: AircraftRow[], airtrack: unknown = null) {
  const m = useMountStore()
  m.setConnection('open')
  if (adsb) m.ingestStatus({ adsb, airtrack })
  useAircraftStore().ingest({ adsb: adsb ?? GREEN, aircraft: rows })
  useSelectionStore().selectAircraft('ABC123')
}

const checkCalls = () => api.post.mock.calls.filter(c => c[0] === '/api/mount/check')

beforeEach(() => {
  setActivePinia(createPinia())
  api.post.mockReset()
  api.post.mockImplementation(async () => ({ ok: true, reason: null, notes: [] }))
  vi.stubGlobal('useRuntimeConfig', () => ({ app: { baseURL: '/' } }))
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => [] })))
})
afterEach(() => {
  w?.unmount(); w = null
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('A1b card: the polled check', () => {
  it('polls /api/mount/check track_aircraft with the ICAO at 1 Hz and enables the button when allowed', async () => {
    vi.useFakeTimers()
    setup(GREEN, [row()])
    w = mount(TrackCard, { attachTo: document.body })
    await flushPromises()
    expect(checkCalls()).toHaveLength(1)
    expect(checkCalls()[0]![1]).toEqual({ cmd: 'track_aircraft', args: { icao: 'ABC123' } })
    const btn = q('[data-testid="track-aircraft"]') as HTMLButtonElement
    expect(btn.disabled).toBe(false)
    expect(btn.getAttribute('data-enabled')).toBe('true')
    await vi.advanceTimersByTimeAsync(3000)
    expect(checkCalls().length).toBeGreaterThanOrEqual(3)
  })

  it('no request while ADS-B is not green or the aircraft is not in view (the local reason shows)', async () => {
    vi.useFakeTimers()
    setup(DOWN, [row()])
    w = mount(TrackCard, { attachTo: document.body })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2500)
    expect(checkCalls()).toHaveLength(0)
    expect(text('aircraft-reason')).toBe('ADS-B platform unreachable')
    w.unmount()
    document.body.innerHTML = ''
    setActivePinia(createPinia())
    setup(GREEN, [])
    w = mount(TrackCard, { attachTo: document.body })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2500)
    expect(checkCalls()).toHaveLength(0)
    expect(text('aircraft-reason')).toBe(AIRCRAFT_NOT_IN_VIEW)
  })

  it('a refusal from the gateway check shows as the reason and keeps the button off; an unreachable gateway too', async () => {
    api.post.mockImplementation(async () => ({ ok: false, reason: 'Cannot reach the aircraft in time', code: 'no_rendezvous' }))
    setup(GREEN, [row()])
    w = mount(TrackCard, { attachTo: document.body })
    await flushPromises()
    expect((q('[data-testid="track-aircraft"]') as HTMLButtonElement).disabled).toBe(true)
    expect(text('aircraft-reason')).toBe('Cannot reach the aircraft in time')
  })

  it('an allowed track with a note shows the note on the reason line', async () => {
    api.post.mockImplementation(async () => ({ ok: true, reason: null, notes: ['Near-zenith overflight: tracking lags near the peak'] }))
    setup(GREEN, [row()])
    w = mount(TrackCard, { attachTo: document.body })
    await flushPromises()
    expect((q('[data-testid="track-aircraft"]') as HTMLButtonElement).disabled).toBe(false)
    expect(text('aircraft-reason')).toBe('Near-zenith overflight: tracking lags near the peak')
  })
})

describe('A1b card: sending, refusal, phase lines', () => {
  it('click sends track_aircraft {icao}; an ok ack keeps the aircraft selected', async () => {
    setup(GREEN, [row()])
    const m = useMountStore()
    m.sendCommand = vi.fn(async () => ({ id: 1, cmd: 'track_aircraft', ok: true })) as never
    w = mount(TrackCard, { attachTo: document.body })
    await flushPromises()
    ;(q('[data-testid="track-aircraft"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(m.sendCommand).toHaveBeenCalledWith('track_aircraft', { icao: 'ABC123' })
    expect(useSelectionStore().selectedIcao).toBe('ABC123')
    expect(q('[data-testid="aircraft-card"]')).not.toBeNull()
  })

  it('a refused command shows the reason for 3 s, then the verdict line returns', async () => {
    setup(GREEN, [row()])
    const m = useMountStore()
    m.sendCommand = vi.fn(async () => ({ id: 1, cmd: 'track_aircraft', ok: false, reason: 'Another window has control' })) as never
    w = mount(TrackCard, { attachTo: document.body })
    await flushPromises()
    ;(q('[data-testid="track-aircraft"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(text('aircraft-reason')).toBe('Another window has control')
    m.now = Date.now() + 3500
    await nextTick()
    expect(q('[data-testid="aircraft-reason"]')).toBeNull() // the allowed verdict has no line of its own
  })

  it('phase lines while this aircraft is tracked, the button disabled; then the ended reason', async () => {
    setup(GREEN, [row()], { icao: 'ABC123', callsign: 'THA101', phase: 'slewing', t_r: 100, notes: [], ended: null })
    const m = useMountStore()
    w = mount(TrackCard, { attachTo: document.body })
    await flushPromises()
    const btn = () => q('[data-testid="track-aircraft"]') as HTMLButtonElement
    expect(text('aircraft-reason')).toBe('Slewing to rendezvous')
    expect(btn().disabled).toBe(true)
    expect(btn().textContent).toBe('TRACKING')
    for (const [phase, line] of [['waiting', 'Waiting for the aircraft'], ['tracking', 'Tracking']] as const) {
      m.ingestStatus({ adsb: GREEN, airtrack: { icao: 'ABC123', callsign: 'THA101', phase, t_r: 100, notes: [], ended: null } })
      await nextTick()
      expect(text('aircraft-reason')).toBe(line)
      expect(btn().disabled).toBe(true)
    }
    m.ingestStatus({
      adsb: GREEN,
      airtrack: { icao: null, callsign: null, phase: null, t_r: null, notes: [], ended: { icao: 'ABC123', reason: 'Aircraft below Lim EL', at: 1 } },
    })
    await flushPromises()
    expect(text('aircraft-reason')).toBe('Aircraft below Lim EL')
    expect(btn().textContent).toBe('TRACK AIRCRAFT')
    expect(btn().disabled).toBe(false) // the verdict allows a new track
  })

  it('another aircraft is tracked: this card shows its own verdict, not the phase', async () => {
    setup(GREEN, [row()], { icao: 'ZZZ999', callsign: 'OTHER', phase: 'tracking', t_r: 100, notes: [], ended: null })
    w = mount(TrackCard, { attachTo: document.body })
    await flushPromises()
    expect(q('[data-testid="aircraft-reason"]')).toBeNull()
    expect((q('[data-testid="track-aircraft"]') as HTMLButtonElement).disabled).toBe(false)
  })
})

describe('Track Target > AIRCRAFT tab', () => {
  async function openAircraftTab() {
    w = mount(TrackTargetDialog, { props: { open: false }, attachTo: document.body })
    await w.setProps({ open: true })
    await flushPromises()
    ;(q('[data-testid="tt-tab-aircraft"]') as HTMLButtonElement).click()
    await flushPromises()
  }

  it('TRACK SELECTED TARGET sends track_aircraft {icao} and closes the dialog on an ok ack', async () => {
    setup(GREEN, [row()])
    const m = useMountStore()
    m.sendCommand = vi.fn(async () => ({ id: 1, cmd: 'track_aircraft', ok: true })) as never
    await openAircraftTab()
    const go = q('[data-testid="tt-air-track"]') as HTMLButtonElement
    expect(checkCalls()[0]![1]).toEqual({ cmd: 'track_aircraft', args: { icao: 'ABC123' } })
    expect(go.disabled).toBe(false)
    expect(text('tt-air-status')).toBe('Ready')
    go.click()
    await flushPromises()
    expect(m.sendCommand).toHaveBeenCalledWith('track_aircraft', { icao: 'ABC123' })
    expect(useSelectionStore().selectedIcao).toBe('ABC123')
    expect(w!.emitted('update:open')?.at(-1)).toEqual([false])
  })

  it('a refusal stays on the status line and the dialog stays open', async () => {
    setup(GREEN, [row()])
    const m = useMountStore()
    m.sendCommand = vi.fn(async () => ({ id: 1, cmd: 'track_aircraft', ok: false, reason: 'Mount is slewing; press STOP first' })) as never
    await openAircraftTab()
    ;(q('[data-testid="tt-air-track"]') as HTMLButtonElement).click()
    await flushPromises()
    expect(text('tt-air-status')).toBe('Mount is slewing; press STOP first')
    expect(w!.emitted('update:open')?.at(-1)).not.toEqual([false])
  })

  it('while the selected aircraft is tracked the button is off and the status shows the phase', async () => {
    setup(GREEN, [row()], { icao: 'ABC123', callsign: 'THA101', phase: 'waiting', t_r: 100, notes: [], ended: null })
    await openAircraftTab()
    expect((q('[data-testid="tt-air-track"]') as HTMLButtonElement).disabled).toBe(true)
    expect(text('tt-air-status')).toBe('Waiting for the aircraft')
  })
})
