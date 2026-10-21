// T2.3: filter semantics, nearest picking, label grid, card formatting.
import { describe, expect, it } from 'vitest'
import {
  DEFAULT_FILTERS, effectiveVisibility, parseFilters, serializeFilters, type FilterState,
} from '../../app/utils/filters'
import {
  BoxGrid, ensureScreenCapacity, indexOfNorad, makePickResult, makeScreenLayer, pickNearest,
} from '../../app/utils/sky-pick'
import {
  formatAz, formatEl, formatRange, formatTleAge, illuminationOf,
} from '../../app/utils/track-format'
import { resolveFlag } from '../../app/utils/country-flag'

describe('filters', () => {
  it('defaults: everything on except the disabled Aircrafts', () => {
    expect(effectiveVisibility({ ...DEFAULT_FILTERS })).toEqual({
      showLeo: true, showMeo: true, showGeo: true, showStars: true, showBodies: true, showLabels: true,
    })
    expect(DEFAULT_FILTERS.aircrafts).toBe(false)
  })

  it('parent off hides all children but keeps the children checks', () => {
    const f: FilterState = { ...DEFAULT_FILTERS, meo: false, satellites: false }
    expect(effectiveVisibility(f)).toMatchObject({ showLeo: false, showMeo: false, showGeo: false })
    expect(f.leo && f.geo && !f.meo).toBe(true) // own checks untouched
    const back = effectiveVisibility({ ...f, satellites: true })
    expect(back).toMatchObject({ showLeo: true, showMeo: false, showGeo: true })
  })

  it('celestial parent controls stars and bodies', () => {
    const f: FilterState = { ...DEFAULT_FILTERS, celestial: false, planets: false }
    expect(effectiveVisibility(f)).toMatchObject({ showStars: false, showBodies: false })
    expect(effectiveVisibility({ ...f, celestial: true })).toMatchObject({ showStars: true, showBodies: false })
  })

  it('Labels is top level: independent of the parents, default on, persisted', () => {
    expect(DEFAULT_FILTERS.labels).toBe(true)
    const off: FilterState = { ...DEFAULT_FILTERS, labels: false }
    expect(effectiveVisibility(off)).toMatchObject({ showLabels: false, showLeo: true, showStars: true })
    expect(effectiveVisibility({ ...off, satellites: false, celestial: false }).showLabels).toBe(false)
    expect(effectiveVisibility({ ...DEFAULT_FILTERS, satellites: false }).showLabels).toBe(true)
    expect(parseFilters(serializeFilters(off)).labels).toBe(false)
    expect(parseFilters('{"leo":false}').labels).toBe(true) // older stored value: default on
  })

  it('round-trips and tolerates garbage', () => {
    const f: FilterState = { ...DEFAULT_FILTERS, geo: false, stars: false }
    expect(parseFilters(serializeFilters(f))).toEqual(f)
    expect(parseFilters('not json')).toEqual(DEFAULT_FILTERS)
    expect(parseFilters(null)).toEqual(DEFAULT_FILTERS)
    expect(parseFilters('{"leo":"yes","aircrafts":true,"meo":false}')).toEqual({ ...DEFAULT_FILTERS, meo: false })
  })
})

function layerWith(pts: Array<[number, number, number]>) {
  const l = makeScreenLayer()
  ensureScreenCapacity(l, pts.length)
  pts.forEach(([x, y, n], i) => {
    l.x[i] = x
    l.y[i] = y
    l.norad[i] = n
  })
  l.n = pts.length
  return l
}

describe('pickNearest', () => {
  const geo = layerWith([[100, 100, 1], [106, 100, 2], [300, 300, 3]])
  const leo = layerWith([[103, 100.5, 9]])
  const out = makePickResult()

  it('picks the nearest, never the topmost, inside a clump', () => {
    expect(pickNearest([geo, leo], 101, 100, 10, out)).toBe(true)
    expect(out.norad).toBe(1)
    expect(pickNearest([geo, leo], 105.2, 100, 10, out)).toBe(true)
    expect(out.norad).toBe(2)
    // leo is drawn last (topmost) but is not the nearest here
    expect(pickNearest([geo, leo], 100.2, 100, 10, out)).toBe(true)
    expect(out.norad).toBe(1)
    expect(pickNearest([geo, leo], 103.2, 100.4, 10, out)).toBe(true)
    expect(out.norad).toBe(9)
  })

  it('respects the radius and hidden (empty) layers', () => {
    expect(pickNearest([geo], 200, 200, 10, out)).toBe(false)
    expect(out.layer).toBe(-1)
    const hidden = layerWith([[100, 100, 5]])
    hidden.n = 0
    expect(pickNearest([hidden], 100, 100, 10, out)).toBe(false)
    expect(indexOfNorad(geo, 3)).toBe(2)
    expect(indexOfNorad(geo, 99)).toBe(-1)
  })
})

describe('BoxGrid', () => {
  it('detects overlaps (including across cells) and clears on reset', () => {
    const g = new BoxGrid(32)
    g.reset(400, 400)
    g.add(10, 10, 80, 22)
    expect(g.overlaps(70, 12, 120, 20)).toBe(true)
    expect(g.overlaps(80, 10, 120, 22)).toBe(false) // touching edges do not overlap
    expect(g.overlaps(10, 30, 80, 42)).toBe(false)
    g.add(300, 300, 310, 310)
    expect(g.overlaps(305, 305, 400, 320)).toBe(true)
    g.reset(400, 400)
    expect(g.overlaps(10, 10, 80, 22)).toBe(false)
  })

  it('handles many boxes', () => {
    const g = new BoxGrid(32)
    g.reset(800, 800)
    for (let i = 0; i < 5000; i++) g.add((i * 7) % 790, (i * 13) % 790, ((i * 7) % 790) + 5, ((i * 13) % 790) + 5)
    expect(g.overlaps(0, 0, 800, 800)).toBe(true)
  })
})

describe('track card formatting', () => {
  it('az / signed el / range', () => {
    expect(formatAz(241.164)).toBe('241.16°')
    expect(formatEl(59.844)).toBe('+59.84°')
    expect(formatEl(-0.551)).toBe('−0.55°')
    expect(formatEl(0)).toBe('+0.00°')
    expect(formatRange(36540.4)).toBe('36 540 km')
    expect(formatRange(1310)).toBe('1 310 km')
    expect(formatRange(980.6)).toBe('981 km')
    expect(formatRange(null)).toBe('—')
    expect(formatTleAge(3.24)).toBe('3.2 d')
  })

  it('illumination: below horizon wins', () => {
    expect(illuminationOf(10, true)).toBe('illuminated')
    expect(illuminationOf(10, false)).toBe('umbra')
    expect(illuminationOf(-0.5, true)).toBe('below')
    expect(illuminationOf(10, null)).toBe('unknown')
  })

  it('flags: plain, combo, unknown', () => {
    expect(resolveFlag('THAI')).toEqual({ kind: 'single', iso: 'th' })
    expect(resolveFlag('US')).toEqual({ kind: 'single', iso: 'us' })
    expect(resolveFlag('CHBZ')).toEqual({ kind: 'combo', leftIso: 'cn', rightIso: 'br' })
    expect(resolveFlag('NATO')).toBeNull()
    expect(resolveFlag(null)).toBeNull()
  })
})
