// @vitest-environment happy-dom
// T5.5: aircraft store, interpolation, icon rotation, labels, card formats, verdict, filter rule, selection
// and the Track Target AIRCRAFT tab list logic.
import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { effectiveVisibility, DEFAULT_FILTERS } from '../../app/utils/filters'
import type { AdsbInfo, AircraftRow } from '../../app/utils/adsb'
import {
  aircraftCardValues, aircraftFilterState, aircraftLabel, aircraftTitle, aircraftTrackVerdict, AIRCRAFT_NOT_IN_VIEW,
  AIRCRAFT_TRACK_PENDING, azDiff, feedDownChip, formatAgeS, formatAltFt, formatKt, formatLat, formatLon, formatRangeKm,
  formatTrack, formatVRate, heightSourceFlag, iconRotationRad, ingestFrame, MAX_AHEAD_S, normAz, predictAzEl,
  showSimulatedBadge,
} from '../../app/utils/aircraft'
import {
  aircraftTabRows, DEFAULT_AIRCRAFT_SORT, exactFirstAircraft, filterAircraft, nextAircraftSort, sortAircraft,
} from '../../app/utils/aircraft-target'
import { trackPanel } from '../../app/utils/track-panel'
import { useAircraftStore } from '../../app/stores/aircraft'
import { useMountStore } from '../../app/stores/mount'
import { useSelectionStore } from '../../app/stores/selection'

const GREEN: AdsbInfo = { state: 'connected', green: true, simulated: false, detail: 'Connected to the ADS-B platform' }
const SIM: AdsbInfo = { state: 'simulated', green: true, simulated: true, detail: 'Simulated aircraft (adsb_interpreter feed 8500)' }
const DOWN: AdsbInfo = { state: 'unreachable', green: false, simulated: false, detail: 'ADS-B platform unreachable' }

function row(o: Partial<AircraftRow> = {}): AircraftRow {
  return {
    icao: 'ABC123', callsign: 'THA101', lat: 13.69, lon: 100.75, az: 90, el: 30, range_m: 23_456, track_deg: 90,
    alt_ft: 35_000, gs_kt: 451, vrate_fpm: 1500, pos_age_s: 1.2, stale: false, height_source: 'baro+diff', predictor: 'velocity', ...o,
  }
}

describe('interpolation (dead reckoning from the last two distinct positions)', () => {
  it('a first sighting has no rate: it stays at the reported position', () => {
    const m = ingestFrame(new Map(), [row()], 10_000)
    const tr = m.get('ABC123')!
    expect(tr.rate).toBeNull()
    expect(predictAzEl(tr, 10_800)).toEqual({ az: 90, el: 30 })
  })

  it('the rate comes from two frames and moves the position between them', () => {
    const a = ingestFrame(new Map(), [row({ az: 90, el: 30 })], 10_000)
    const b = ingestFrame(a, [row({ az: 91, el: 29 })], 11_000)
    const tr = b.get('ABC123')!
    expect(tr.rate).toEqual({ daz: 1, del: -1 })
    const p = predictAzEl(tr, 11_500)
    expect(p.az).toBeCloseTo(91.5, 6)
    expect(p.el).toBeCloseTo(28.5, 6)
  })

  it('never extrapolates more than 2 s past the frame', () => {
    const a = ingestFrame(new Map(), [row({ az: 90 })], 0)
    const tr = ingestFrame(a, [row({ az: 91 })], 1000).get('ABC123')!
    expect(MAX_AHEAD_S).toBe(2)
    expect(predictAzEl(tr, 1000 + 10_000).az).toBeCloseTo(93, 6) // 91 + 1 deg/s x 2 s
    expect(predictAzEl(tr, 500).az).toBeCloseTo(91, 6) // a clock before the frame never goes backwards
  })

  it('azimuth wraps through north in both directions', () => {
    expect(azDiff(359, 1)).toBe(2)
    expect(azDiff(1, 359)).toBe(-2)
    expect(normAz(-1)).toBe(359)
    const a = ingestFrame(new Map(), [row({ az: 359.5 })], 0)
    const tr = ingestFrame(a, [row({ az: 0.5 })], 1000).get('ABC123')!
    expect(tr.rate!.daz).toBeCloseTo(1, 9)
    expect(predictAzEl(tr, 2000).az).toBeCloseTo(1.5, 6)
  })

  it('a repeated position (no new fix) keeps the anchor and the rate: no freeze, no jump', () => {
    const a = ingestFrame(new Map(), [row({ az: 90 })], 0)
    const b = ingestFrame(a, [row({ az: 91 })], 1000)
    const c = ingestFrame(b, [row({ az: 91, pos_age_s: 2.2 })], 2000)
    const tr = c.get('ABC123')!
    expect(tr.t).toBe(1000)
    expect(tr.rate).toEqual({ daz: 1, del: 0 })
    expect(tr.row.pos_age_s).toBe(2.2)
    expect(predictAzEl(tr, 2000).az).toBeCloseTo(92, 6)
  })

  it('a gap longer than 5 s gives no rate; gone aircraft drop out; bad rows are skipped', () => {
    const a = ingestFrame(new Map(), [row({ az: 90 })], 0)
    expect(ingestFrame(a, [row({ az: 95 })], 9000).get('ABC123')!.rate).toBeNull()
    expect(ingestFrame(a, [], 1000).size).toBe(0)
    expect(ingestFrame(new Map(), [row({ az: Number.NaN })], 0).size).toBe(0)
  })
})

describe('icon rotation (chart: N up, E to the LEFT)', () => {
  const vec = (deg: number) => {
    // the screen vector of the icon nose after the canvas rotation (clockwise, y down), start (0, -1)
    const r = iconRotationRad(deg)
    return { x: Math.sin(r), y: -Math.cos(r) }
  }
  it('north points up, east points left, south down, west right', () => {
    expect(vec(0).x).toBeCloseTo(0, 9)
    expect(vec(0).y).toBeCloseTo(-1, 9)
    const e = vec(90)
    expect(e.x).toBeCloseTo(-1, 9)
    expect(e.y).toBeCloseTo(0, 9)
    const s = vec(180)
    expect(s.x).toBeCloseTo(0, 9)
    expect(s.y).toBeCloseTo(1, 9)
    expect(vec(270).x).toBeCloseTo(1, 9)
  })
  it('matches the chart projection x = cx - r sin(az), y = cy - r cos(az) for a ground displacement', () => {
    // moving along compass bearing t at the chart centre: dx = -sin t, dy = -cos t
    for (const t of [0, 33, 90, 145, 180, 250, 315]) {
      const v = vec(t)
      expect(v.x).toBeCloseTo(-Math.sin((t * Math.PI) / 180), 9)
      expect(v.y).toBeCloseTo(-Math.cos((t * Math.PI) / 180), 9)
    }
  })
  it('no track leaves the icon upright', () => {
    expect(iconRotationRad(null)).toBe(0)
    expect(iconRotationRad(undefined)).toBe(0)
  })
})

describe('labels and card formats', () => {
  it('label: callsign, else ICAO; title adds the ICAO', () => {
    expect(aircraftLabel({ callsign: 'THA101', icao: 'ABC123' })).toBe('THA101')
    expect(aircraftLabel({ callsign: null, icao: 'ABC123' })).toBe('ABC123')
    expect(aircraftLabel({ callsign: '  ', icao: 'ABC123' })).toBe('ABC123')
    expect(aircraftTitle({ callsign: 'THA101', icao: 'ABC123' })).toBe('THA101 (ABC123)')
    expect(aircraftTitle({ callsign: null, icao: 'ABC123' })).toBe('ABC123')
  })

  it('formats of A1b', () => {
    expect(formatLat(13.6951)).toBe('13.69510° N')
    expect(formatLat(-33.8688)).toBe('33.86880° S')
    expect(formatLon(100.7482)).toBe('100.74820° E')
    expect(formatLon(-0.1278)).toBe('0.12780° W')
    expect(formatAltFt(35_000.4)).toBe('35 000 ft')
    expect(formatAltFt(-300)).toBe('-300 ft')
    expect(formatRangeKm(23_456)).toBe('23.456 km')
    expect(formatRangeKm(123_456.7)).toBe('123.457 km')
    expect(formatKt(450.6)).toBe('451 kt')
    expect(formatTrack(89.6)).toBe('90°')
    expect(formatTrack(359.7)).toBe('0°')
    expect(formatVRate(1500)).toBe('+1 500 fpm')
    expect(formatVRate(-640)).toBe('−640 fpm')
    expect(formatVRate(0)).toBe('0 fpm')
    expect(formatAgeS(1.24)).toBe('1.2 s')
  })

  it('missing values are em dashes (and a row-less card is all dashes with the ICAO title)', () => {
    const v = aircraftCardValues(row({
      callsign: null, lat: null, lon: undefined, alt_ft: null, gs_kt: null, track_deg: null, vrate_fpm: null, height_source: null,
    }), 'ABC123')
    expect(v.title).toBe('ABC123')
    expect([v.lat, v.lon, v.alt, v.gs, v.track, v.vrate]).toEqual(['—', '—', '—', '—', '—', '—'])
    expect(v.flag).toBeNull()
    expect(v.range).toBe('23.456 km')
    expect(v.az).toBe('90.00°')
    expect(v.el).toBe('+30.00°')
    const none = aircraftCardValues(null, 'ABC123')
    expect(none.title).toBe('ABC123')
    expect(Object.values({ ...none, title: '', flag: '' }).filter(x => x !== '')).toEqual(Array(10).fill('—'))
  })

  it('a full row', () => {
    const v = aircraftCardValues(row(), 'ABC123')
    expect(v).toMatchObject({
      title: 'THA101 (ABC123)', lat: '13.69000° N', lon: '100.75000° E', alt: '35 000 ft', range: '23.456 km',
      az: '90.00°', el: '+30.00°', gs: '451 kt', track: '90°', vrate: '+1 500 fpm', age: '1.2 s', flag: null,
    })
  })

  it('only barometric-only heights are flagged', () => {
    expect(heightSourceFlag('baro')?.label).toBe('BARO')
    expect(heightSourceFlag('baro+diff')).toBeNull()
    expect(heightSourceFlag('gnss')).toBeNull()
    expect(aircraftCardValues(row({ height_source: 'baro' }), 'ABC123').flag?.label).toBe('BARO')
  })
})

describe('TRACK AIRCRAFT verdict, filter enable rule, chips', () => {
  it('tracking is disabled with the T5.6b reason on a green feed', () => {
    expect(aircraftTrackVerdict(row(), GREEN)).toEqual({ enabled: false, reason: AIRCRAFT_TRACK_PENDING })
    expect(AIRCRAFT_TRACK_PENDING).toBe('Aircraft tracking: coming in T5.6b')
    expect(aircraftTrackVerdict(null, GREEN).reason).toBe(AIRCRAFT_NOT_IN_VIEW)
    expect(aircraftTrackVerdict(row(), DOWN).reason).toBe('ADS-B platform unreachable')
    expect(aircraftTrackVerdict(row(), null).enabled).toBe(false)
  })

  it('Filters > Aircrafts is enabled only when green; otherwise the reason', () => {
    expect(aircraftFilterState(GREEN)).toEqual({ enabled: true, reason: null })
    expect(aircraftFilterState(SIM)).toEqual({ enabled: true, reason: null })
    expect(aircraftFilterState(DOWN)).toEqual({ enabled: false, reason: 'ADS-B platform unreachable' })
    expect(aircraftFilterState({ ...DOWN, state: 'module_missing', detail: 'ADS-B module not installed' }).reason).toBe('ADS-B module not installed')
    expect(aircraftFilterState(null).enabled).toBe(false)
  })

  it('the Airborne child needs its parent: visibility', () => {
    expect(effectiveVisibility({ ...DEFAULT_FILTERS }).showAircraft).toBe(true)
    expect(effectiveVisibility({ ...DEFAULT_FILTERS, aircrafts: false }).showAircraft).toBe(false)
    expect(effectiveVisibility({ ...DEFAULT_FILTERS, airborne: false }).showAircraft).toBe(false)
  })

  it('feed-down chip: ADS-B in use but not green; the simulated badge only on a green simulated feed', () => {
    expect(feedDownChip(GREEN).show).toBe(false)
    expect(feedDownChip(DOWN)).toEqual({ show: true, title: 'ADS-B platform unreachable' })
    expect(feedDownChip({ ...DOWN, state: 'disabled' }).show).toBe(false)
    expect(feedDownChip({ ...DOWN, state: 'not_configured' }).show).toBe(false)
    expect(feedDownChip({ ...DOWN, state: 'module_missing' }).show).toBe(false)
    expect(feedDownChip({ ...DOWN, state: 'connecting', simulated: true }).show).toBe(true)
    expect(feedDownChip(null).show).toBe(false)
    expect(showSimulatedBadge(SIM)).toBe(true)
    expect(showSimulatedBadge(GREEN)).toBe(false)
    expect(showSimulatedBadge({ ...SIM, green: false })).toBe(false)
  })
})

describe('aircraft store', () => {
  beforeEach(() => setActivePinia(createPinia()))

  const msg = (rows: AircraftRow[], adsb: AdsbInfo = GREEN) => ({ adsb, aircraft: rows })
  const open = () => { useMountStore().setConnection('open') }

  it('ingests rows, keeps the anchors across frames and bumps the version', () => {
    open()
    const st = useAircraftStore()
    st.ingest(msg([row({ az: 90 })]), 0)
    st.ingest(msg([row({ az: 91 })]), 1000)
    expect(st.rows).toHaveLength(1)
    expect(st.get('ABC123')?.rate).toEqual({ daz: 1, del: 0 })
    expect(st.version).toBe(2)
    expect(st.green).toBe(true) // no status yet: the message's own adsb block is used
  })

  it('uses the message adsb block until a status arrives, then the 10 Hz status wins', () => {
    open()
    const st = useAircraftStore()
    st.ingest(msg([row()], SIM), 0)
    expect(st.adsb?.simulated).toBe(true)
    expect(st.green).toBe(true)
    useMountStore().ingestStatus({ adsb: DOWN })
    expect(st.adsb?.state).toBe('unreachable')
    expect(st.green).toBe(false)
  })

  it('the empty frame clears', () => {
    open()
    const st = useAircraftStore()
    st.ingest(msg([row()]), 0)
    expect(st.rows).toHaveLength(1)
    st.ingest(msg([]), 1000)
    expect(st.rows).toHaveLength(0)
    expect(st.get('ABC123')).toBeNull()
  })

  it('clears when the status says not green, and refuses frames while not green', async () => {
    open()
    const mount = useMountStore()
    const st = useAircraftStore()
    mount.ingestStatus({ adsb: GREEN })
    st.ingest(msg([row()]), 0)
    expect(st.rows).toHaveLength(1)
    mount.ingestStatus({ adsb: DOWN })
    await Promise.resolve()
    expect(st.rows).toHaveLength(0)
    st.ingest(msg([row()]), 1000) // a late frame whose own block says green, status says down
    expect(st.rows).toHaveLength(0)
    st.ingest(msg([row()], DOWN), 2000)
    expect(st.rows).toHaveLength(0)
  })

  it('clears on disconnect and hides the adsb block', async () => {
    const mount = useMountStore()
    mount.setConnection('open')
    const st = useAircraftStore()
    st.ingest(msg([row()]), 0)
    expect(st.rows).toHaveLength(1)
    mount.setConnection('closed')
    await Promise.resolve()
    expect(st.rows).toHaveLength(0)
    expect(st.adsb).toBeNull()
  })
})

describe('selection (one object at a time) and the Track Info panel', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('selecting an aircraft deselects the satellite and vice versa', () => {
    const sel = useSelectionStore()
    sel.select(25544)
    expect(sel.selectedNorad).toBe(25544)
    sel.selectAircraft('ABC123')
    expect(sel.selectedNorad).toBeNull()
    expect(sel.selectedIcao).toBe('ABC123')
    sel.select(7)
    expect(sel.selectedIcao).toBeNull()
    expect(sel.selectedNorad).toBe(7)
    sel.selectAircraft('ABC123')
    sel.deselect()
    expect(sel.selectedIcao).toBeNull()
    expect(sel.selectedNorad).toBeNull()
  })

  it('an aircraft selected: no satellite main card; the tracked satellite becomes the strip', () => {
    const track = { norad: 1, name: 'ALPHA', phase: 'tracking', t_end: 200 }
    const v = trackPanel(null, track, 140, true)
    expect(v.main).toBeNull()
    expect(v.aircraft).toBe(true)
    expect(v.empty).toBe(false)
    expect(v.strip).toEqual({ norad: 1, name: 'ALPHA', line: 'Tracking — ends in 01:00' })
    expect(trackPanel(null, null, 0, true)).toEqual({ main: null, strip: null, empty: false, aircraft: true })
    expect(trackPanel(null, null, 0).empty).toBe(true) // unchanged without an aircraft
  })
})

describe('AIRCRAFT tab list', () => {
  const R = [
    row({ icao: 'A00001', callsign: 'THA101', range_m: 30_000, alt_ft: 5000, el: 10 }),
    row({ icao: 'A00002', callsign: 'AIQ22', range_m: 12_000, alt_ft: 30_000, el: 40 }),
    row({ icao: 'A00003', callsign: null, range_m: 80_000, alt_ft: null, el: 5 }),
    row({ icao: 'THA101', callsign: 'XYZ9', range_m: 150_000, alt_ft: 20_000, el: 2 }), // an ICAO that equals another's callsign
    row({ icao: 'A00005', callsign: 'THA1010', range_m: 49_999, alt_ft: 100, el: 3 }),
  ]

  it('range presets filter by km (inclusive)', () => {
    expect(filterAircraft(R, '', 20).map(r => r.icao)).toEqual(['A00002'])
    expect(filterAircraft(R, '', 50).map(r => r.icao)).toEqual(['A00001', 'A00002', 'A00005'])
    expect(filterAircraft(R, '', 100).map(r => r.icao)).toEqual(['A00001', 'A00002', 'A00003', 'A00005'])
    expect(filterAircraft(R, '', 200)).toHaveLength(5)
    expect(filterAircraft([row({ range_m: 100_000 })], '', 100)).toHaveLength(1)
  })

  it('search matches callsign or ICAO, case-insensitive substring', () => {
    expect(filterAircraft(R, 'tha', 350).map(r => r.icao)).toEqual(['A00001', 'THA101', 'A00005'])
    expect(filterAircraft(R, 'a00003', 350).map(r => r.icao)).toEqual(['A00003'])
    expect(filterAircraft(R, 'nomatch', 350)).toEqual([])
  })

  it('default sort is range, nearest first; exact callsign / ICAO matches go on top', () => {
    expect(aircraftTabRows(R, '', 350, DEFAULT_AIRCRAFT_SORT).map(r => r.icao)).toEqual(['A00002', 'A00001', 'A00005', 'A00003', 'THA101'])
    // "tha101" matches the callsign of A00001 and the ICAO of THA101 exactly; A00005 (THA1010) only partly
    expect(aircraftTabRows(R, 'tha101', 350, DEFAULT_AIRCRAFT_SORT).map(r => r.icao)).toEqual(['A00001', 'THA101', 'A00005'])
    expect(exactFirstAircraft(R, '').map(r => r.icao)).toEqual(R.map(r => r.icao))
  })

  it('column sort: directions, new-column defaults, empty values last', () => {
    expect(nextAircraftSort(DEFAULT_AIRCRAFT_SORT, 'range')).toEqual({ key: 'range', dir: 'desc' })
    expect(nextAircraftSort(DEFAULT_AIRCRAFT_SORT, 'alt')).toEqual({ key: 'alt', dir: 'desc' })
    expect(nextAircraftSort(DEFAULT_AIRCRAFT_SORT, 'callsign')).toEqual({ key: 'callsign', dir: 'asc' })
    expect(sortAircraft(R, { key: 'alt', dir: 'desc' }).map(r => r.icao)).toEqual(['A00002', 'THA101', 'A00001', 'A00005', 'A00003'])
    expect(sortAircraft(R, { key: 'alt', dir: 'asc' }).map(r => r.icao)).toEqual(['A00005', 'A00001', 'THA101', 'A00002', 'A00003'])
    expect(sortAircraft(R, { key: 'callsign', dir: 'asc' }).map(r => r.icao)).toEqual(['A00002', 'A00001', 'A00005', 'THA101', 'A00003'])
    expect(sortAircraft(R, { key: 'el', dir: 'desc' }).map(r => r.icao)).toEqual(['A00002', 'A00001', 'A00003', 'A00005', 'THA101'])
    expect(sortAircraft(R, { key: 'range', dir: 'asc' })).not.toBe(R)
  })
})

describe('aircraft icon colours against the sky (user 11-10)', () => {
  it('a light sky gets a light icon with a dark outline, a dark sky the reverse', async () => {
    const { aircraftIconColors, AIRCRAFT_ICON_PX } = await import('../../app/utils/aircraft')
    const day = aircraftIconColors(0.9)
    const night = aircraftIconColors(0.05)
    expect(day).toEqual({ fill: '#f4f6f8', outline: '#161c24' })
    expect(night).toEqual({ fill: '#262c35', outline: '#eef1f4' })
    expect(aircraftIconColors(0.5)).toEqual(day)
    expect(AIRCRAFT_ICON_PX).toBe(20)
  })
})
