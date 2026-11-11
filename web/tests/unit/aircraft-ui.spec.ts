// @vitest-environment happy-dom
// T5.5 components: Filters > Aircrafts tree, the A1b Aircraft card and the Track Target AIRCRAFT tab.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import TrackCard from '../../app/components/track/TrackCard.vue'
import TrackTargetDialog from '../../app/components/track/TrackTargetDialog.vue'
import VisibilityFilters from '../../app/components/filters/VisibilityFilters.vue'
import type { AdsbInfo, AircraftRow } from '../../app/utils/adsb'
import { useAircraftStore } from '../../app/stores/aircraft'
import { useMountStore } from '../../app/stores/mount'
import { useSelectionStore } from '../../app/stores/selection'

const api = vi.hoisted(() => ({ post: vi.fn(async (_path: string, _body?: unknown): Promise<unknown> => ({ ok: false, reason: 'Aircraft not in view' })) }))
vi.mock('~/composables/useGatewayApi', () => ({
  useGatewayApi: () => api,
}))

const GREEN: AdsbInfo = { state: 'connected', green: true, simulated: false, detail: 'Connected to the ADS-B platform' }
const DOWN: AdsbInfo = { state: 'unreachable', green: false, simulated: false, detail: 'ADS-B platform unreachable' }

function row(o: Partial<AircraftRow> = {}): AircraftRow {
  return {
    icao: 'ABC123', callsign: 'THA101', lat: 13.69, lon: 100.75, az: 90, el: 30, range_m: 23_456, track_deg: 90,
    alt_ft: 35_000, gs_kt: 451, vrate_fpm: 1500, pos_age_s: 1.2, stale: false, height_source: 'baro', predictor: 'velocity', ...o,
  }
}

let w: VueWrapper | null = null
const q = (sel: string) => document.body.querySelector(sel) as HTMLElement | null
const qa = (sel: string) => [...document.body.querySelectorAll(sel)] as HTMLElement[]

function setAdsb(a: AdsbInfo | null) {
  const m = useMountStore()
  m.setConnection('open')
  if (a) m.ingestStatus({ adsb: a })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.stubGlobal('useRuntimeConfig', () => ({ app: { baseURL: '/' } }))
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => [] })))
})
afterEach(() => {
  w?.unmount(); w = null
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('Filters > Aircrafts', () => {
  it('green: enabled, top level alone (no Airborne child); not green: disabled with the reason as the title', async () => {
    setAdsb(GREEN)
    w = mount(VisibilityFilters, { attachTo: document.body })
    const cb = () => q('[data-testid="flt-aircrafts"]') as HTMLInputElement
    expect(cb().disabled).toBe(false)
    expect(q('[data-testid="flt-airborne"]')).toBeNull()
    expect(q('[data-testid="flt-aircrafts-row"]')!.getAttribute('title')).toBeNull()
    setAdsb(DOWN)
    await nextTick()
    expect(cb().disabled).toBe(true)
    expect(q('[data-testid="flt-aircrafts-row"]')!.getAttribute('title')).toBe('ADS-B platform unreachable')
  })

  it('no status yet: disabled', () => {
    w = mount(VisibilityFilters, { attachTo: document.body })
    expect((q('[data-testid="flt-aircrafts"]') as HTMLInputElement).disabled).toBe(true)
  })
})

describe('A1b Aircraft card', () => {
  it('shows the aircraft values, the BARO flag and the TRACK AIRCRAFT button with the gateway reason', async () => {
    setAdsb(GREEN)
    useAircraftStore().ingest({ adsb: GREEN, aircraft: [row()] })
    useSelectionStore().selectAircraft('ABC123')
    w = mount(TrackCard, { attachTo: document.body })
    await flushPromises()
    expect(q('[data-testid="aircraft-card"]')).not.toBeNull()
    expect(q('[data-testid="track-card"]')).toBeNull()
    expect(q('[data-testid="aircraft-name"]')!.textContent).toContain('THA101 (ABC123)')
    const t = (id: string) => q(`[data-testid="${id}"]`)!.textContent!.replace(/\s+/g, ' ').trim()
    expect(t('air-lat')).toBe('13.69000° N')
    expect(t('air-lon')).toBe('100.75000° E')
    expect(t('air-alt')).toBe('35 000 ftBARO')
    expect(t('air-range')).toBe('23.456 km')
    expect(t('air-az')).toBe('90.00°')
    expect(t('air-el')).toBe('+30.00°')
    expect(t('air-gs')).toBe('451 kt')
    expect(t('air-track')).toBe('90°')
    expect(t('air-vrate')).toBe('+1 500 fpm')
    expect(t('air-age')).toBe('1.2 s')
    const btn = q('[data-testid="track-aircraft"]') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(btn.textContent).toBe('TRACK AIRCRAFT')
    expect(t('aircraft-reason')).toBe('Aircraft not in view') // the gateway's refusal (the mocked check)
  })

  it('refreshes with the next 1 Hz frame; an aircraft that left shows dashes and "not in view"', async () => {
    setAdsb(GREEN)
    const air = useAircraftStore()
    air.ingest({ adsb: GREEN, aircraft: [row()] }, 0)
    useSelectionStore().selectAircraft('ABC123')
    w = mount(TrackCard, { attachTo: document.body })
    await nextTick()
    air.ingest({ adsb: GREEN, aircraft: [row({ alt_ft: 36_000, height_source: 'gnss' })] }, 1000)
    await nextTick()
    expect(q('[data-testid="air-alt"]')!.textContent!.trim()).toBe('36 000 ft')
    expect(q('[data-testid="air-flag"]')).toBeNull()
    air.ingest({ adsb: GREEN, aircraft: [] }, 2000)
    await nextTick()
    expect(q('[data-testid="air-lat"]')!.textContent!.trim()).toBe('—')
    expect(q('[data-testid="aircraft-name"]')!.textContent!.trim()).toBe('ABC123')
    expect(q('[data-testid="aircraft-reason"]')!.textContent).toBe('Aircraft not in view')
  })

  it('the close button deselects', async () => {
    setAdsb(GREEN)
    useAircraftStore().ingest({ adsb: GREEN, aircraft: [row()] })
    const sel = useSelectionStore()
    sel.selectAircraft('ABC123')
    w = mount(TrackCard, { attachTo: document.body })
    await nextTick()
    q('[data-testid="track-close"]')!.click()
    await nextTick()
    expect(sel.selectedIcao).toBeNull()
    expect(q('[data-testid="track-empty"]')).not.toBeNull()
  })
})

describe('Track Target > AIRCRAFT tab', () => {
  async function openDialog() {
    w = mount(TrackTargetDialog, { props: { open: false }, attachTo: document.body })
    await w.setProps({ open: true })
    await flushPromises()
    await nextTick()
  }

  it('disabled with the reason when ADS-B is not green; enabled when green', async () => {
    setAdsb(DOWN)
    await openDialog()
    const tab = () => q('[data-testid="tt-tab-aircraft"]') as HTMLButtonElement
    expect(tab().disabled).toBe(true)
    expect(tab().getAttribute('title')).toBe('ADS-B platform unreachable')
    setAdsb(GREEN)
    await nextTick()
    expect(tab().disabled).toBe(false)
  })

  it('lists aircraft in range (nearest first), searches, selects into the Sky Viewer, TRACK shows the gateway reason', async () => {
    setAdsb(GREEN)
    useAircraftStore().ingest({
      adsb: GREEN,
      aircraft: [
        row({ icao: 'A00001', callsign: 'THA101', range_m: 30_000 }),
        row({ icao: 'A00002', callsign: 'AIQ22', range_m: 12_000 }),
        row({ icao: 'A00003', callsign: null, range_m: 150_000 }),
      ],
    })
    await openDialog()
    ;(q('[data-testid="tt-tab-aircraft"]') as HTMLButtonElement).click()
    await nextTick()
    const icaos = () => qa('[data-testid="tt-air-row"]').map(r => r.getAttribute('data-icao'))
    expect(icaos()).toEqual(['A00002', 'A00001']) // default 100 km, sorted by range
    const search = q('[data-testid="tt-air-search"]') as HTMLInputElement
    search.value = 'tha'
    search.dispatchEvent(new Event('input'))
    await nextTick()
    expect(icaos()).toEqual(['A00001'])
    search.value = ''
    search.dispatchEvent(new Event('input'))
    await nextTick()
    // column sort: a second click on RANGE flips to farthest first
    ;(q('[data-testid="tt-air-sort-range"]') as HTMLButtonElement).click()
    await nextTick()
    expect(icaos()).toEqual(['A00001', 'A00002'])
    // selecting a row selects it in the Sky Viewer (shared selection)
    ;(qa('[data-testid="tt-air-row"]')[0] as HTMLElement).click()
    await nextTick()
    expect(useSelectionStore().selectedIcao).toBe('A00001')
    const go = q('[data-testid="tt-air-track"]') as HTMLButtonElement
    await flushPromises()
    expect(go.disabled).toBe(true) // the mocked gateway check refuses
    expect(q('[data-testid="tt-air-status"]')!.textContent!.trim()).toBe('Aircraft not in view')
    // the satellite tab's controls are gone while the aircraft tab is up
    expect(q('[data-testid="tt-search"]')).toBeNull()
  })

  it('goes back to the satellite tab when ADS-B drops', async () => {
    setAdsb(GREEN)
    await openDialog()
    ;(q('[data-testid="tt-tab-aircraft"]') as HTMLButtonElement).click()
    await nextTick()
    expect(q('[data-testid="tt-air-table"]')).not.toBeNull()
    setAdsb(DOWN)
    await nextTick()
    expect(q('[data-testid="tt-air-table"]')).toBeNull()
    expect(q('[data-testid="tt-search"]')).not.toBeNull()
  })

  it('pick mode (the satellite picker) has no aircraft tab', async () => {
    setAdsb(GREEN)
    w = mount(TrackTargetDialog, { props: { open: false, mode: 'pick' }, attachTo: document.body })
    await w.setProps({ open: true })
    await flushPromises()
    expect((q('[data-testid="tt-tab-aircraft"]') as HTMLButtonElement).disabled).toBe(true)
  })
})
