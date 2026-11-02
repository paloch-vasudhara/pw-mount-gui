// T5.2c: the pure Astrometry model (table, mapping, specify, add / remove, readiness, detect confirm).
import { describe, expect, it } from 'vitest'
import {
  addStreak, applySpecify, astrometryErrorText, completeStreaks, emptyTable, fileAllowed, fmtCell, fromDetections, imageToPreview, needsDetectConfirm,
  overlayShapes, parseEpoch, parseExposure, previewToImage, removeStreak, rowAfterRemove, runReadiness, setCell, specAfterRemove, specFor, specifyLabel,
  toDetections, type AsTable, type Detection,
} from '../../app/utils/astrometry'

const det = (id: string, role: 'star' | 'target', xs: number, ys: number, xe: number, ye: number, brt = 3): Detection => ({
  id, role, shape: role === 'star' ? 'streak' : 'point', x: (xs + xe) / 2, y: (ys + ye) / 2, xs, ys, xe, ye, brt,
})
const streaks = (n: number): Detection[] => Array.from({ length: n }, (_, i) => det(`S${i + 1}`, 'star', 10 + i, 20, 50 + i, 60))
const full = (n: number): AsTable => fromDetections([det('T', 'target', 5, 6, 5, 6), ...streaks(n)])
const EPOCH = new Date(Date.UTC(2026, 0, 1))

describe('table model', () => {
  it('fromDetections: T first, streaks numbered by position, clean', () => {
    const t = fromDetections([...streaks(2), det('T', 'target', 5, 6, 5, 6)])
    expect(t.streaks).toHaveLength(2)
    expect(t.target).toEqual({ x: 5, y: 6 })
    expect(t.edited).toBe(false)
    expect(toDetections(t).map(d => d.id)).toEqual(['T', 'S1', 'S2'])
  })
  it('no target detection leaves the target unplaced', () => {
    expect(fromDetections(streaks(1)).target).toEqual({ x: null, y: null })
  })
  it('add / remove streaks and renumber', () => {
    let t = addStreak(full(2))
    expect(t.streaks).toHaveLength(3)
    expect(t.edited).toBe(true)
    expect(t.streaks[2]!.xs).toBeNull()
    t = removeStreak(t, 0)
    expect(t.streaks).toHaveLength(2)
    expect(toDetections(t).map(d => d.id)).toEqual(['T', 'S1']) // the unplaced row is not sent
    expect(removeStreak(t, 9)).toBe(t)
  })
  it('setCell: blank clears, junk clears, BRT clamps, target has no BRT', () => {
    let t = full(1)
    t = setCell(t, { kind: 'S', idx: 0 }, 'xs', '12.5')
    expect(t.streaks[0]!.xs).toBe(12.5)
    t = setCell(t, { kind: 'S', idx: 0 }, 'xs', 'abc')
    expect(t.streaks[0]!.xs).toBeNull()
    t = setCell(t, { kind: 'S', idx: 0 }, 'brt', '9')
    expect(t.streaks[0]!.brt).toBe(5)
    t = setCell(t, { kind: 'S', idx: 0 }, 'brt', '0')
    expect(t.streaks[0]!.brt).toBe(1)
    expect(setCell(t, { kind: 'T' }, 'brt', '2')).toBe(t)
    expect(setCell(t, { kind: 'T' }, 'x', '77').target.x).toBe(77)
  })
  it('fmtCell', () => {
    expect([fmtCell(null), fmtCell(504), fmtCell(1.26)]).toEqual(['', '504', '1.3'])
  })
})

describe('mapping + overlay', () => {
  it('preview <-> image pixels', () => {
    expect(previewToImage(100, 0.25)).toBe(400)
    expect(imageToPreview(400, 0.25)).toBe(100)
    expect(previewToImage(7, 0)).toBe(7)
  })
  it('overlay in preview px, complete rows only', () => {
    const t = addStreak(full(1))
    const o = overlayShapes(t, 0.5)
    expect(o.streaks).toHaveLength(1)
    expect(o.streaks[0]).toMatchObject({ idx: 0, label: 'S1', x1: 5, y1: 10, x2: 25, y2: 30 })
    expect(o.target).toEqual({ x: 2.5, y: 3 })
    expect(overlayShapes(emptyTable(), 1).target).toBeNull()
  })
})

describe('specify', () => {
  it('focus arms: target X / Y, streak S / E pair, BRT does not arm', () => {
    expect(specFor({ kind: 'T' }, 'y')).toEqual({ kind: 'T' })
    expect(specFor({ kind: 'S', idx: 2 }, 'ys')).toEqual({ kind: 'S', idx: 2, part: 'start' })
    expect(specFor({ kind: 'S', idx: 2 }, 'xe')).toEqual({ kind: 'S', idx: 2, part: 'end' })
    expect(specFor({ kind: 'S', idx: 2 }, 'brt')).toBeNull()
    expect(specifyLabel({ kind: 'S', idx: 1, part: 'end' })).toBe('Click the image to place the END of S2')
  })
  it('applies a rounded click to the target', () => {
    const t = applySpecify(emptyTable(), { kind: 'T' }, 10.4, 20.6, [100, 100])
    expect(t.target).toEqual({ x: 10, y: 21 })
    expect(t.edited).toBe(true)
  })
  it('a new streak start auto-fills its end from the reference vector, kept inside the frame', () => {
    const t0 = addStreak(fromDetections([det('S1', 'star', 10, 10, 50, 30)])) // vector (40, 20)
    const t1 = applySpecify(t0, { kind: 'S', idx: 1, part: 'start' }, 20, 20, [100, 100])
    expect(t1.streaks[1]).toMatchObject({ xs: 20, ys: 20, xe: 60, ye: 40 })
    const t2 = applySpecify(t0, { kind: 'S', idx: 1, part: 'start' }, 80, 20, [100, 100])
    expect(t2.streaks[1]!.xe).toBe(100) // clipped at the frame edge
    const t3 = applySpecify(t0, { kind: 'S', idx: 1, part: 'end' }, 60, 40, [100, 100])
    expect(t3.streaks[1]).toMatchObject({ xs: 20, ys: 20, xe: 60, ye: 40 })
  })
  it('an end that is already set is not overwritten', () => {
    let t = addStreak(fromDetections([det('S1', 'star', 10, 10, 50, 30)]))
    t = setCell(t, { kind: 'S', idx: 1 }, 'xe', '90')
    t = applySpecify(t, { kind: 'S', idx: 1, part: 'start' }, 20, 20, [100, 100])
    expect(t.streaks[1]!.xe).toBe(90)
    expect(t.streaks[1]!.ye).toBeNull()
  })
  it('removal shifts / drops the armed + selected row', () => {
    expect(specAfterRemove({ kind: 'S', idx: 2, part: 'start' }, 0)).toEqual({ kind: 'S', idx: 1, part: 'start' })
    expect(specAfterRemove({ kind: 'S', idx: 1, part: 'start' }, 1)).toBeNull()
    expect(specAfterRemove({ kind: 'T' }, 0)).toEqual({ kind: 'T' })
    expect(rowAfterRemove({ kind: 'S', idx: 3 }, 1)).toEqual({ kind: 'S', idx: 2 })
    expect(rowAfterRemove({ kind: 'S', idx: 1 }, 1)).toBeNull()
  })
})

describe('RUN readiness (reason order)', () => {
  it('no image', () => expect(runReadiness(full(12), false, EPOCH).reason).toBe('Load an image'))
  it('no streaks', () => expect(runReadiness(emptyTable(), true, EPOCH).reason).toBe('Detect or add streaks'))
  it('fewer than 10 complete streaks counts only the complete ones', () => {
    const t = addStreak(full(9))
    expect(completeStreaks(t)).toBe(9)
    expect(runReadiness(t, true, EPOCH).reason).toBe('At least 10 streaks are needed (9 of 10)')
  })
  it('no target', () => {
    const t = fromDetections(streaks(10))
    expect(runReadiness(t, true, EPOCH).reason).toBe('Mark the target')
  })
  it('no epoch', () => {
    expect(runReadiness(full(10), true, null).reason).toBe('Enter the exposure start time')
    expect(runReadiness(full(10), true, new Date('x')).reason).toBe('Enter the exposure start time')
  })
  it('ready', () => expect(runReadiness(full(10), true, EPOCH)).toEqual({ ready: true, reason: null }))
})

describe('detect confirm + misc', () => {
  it('confirm only after an edit', () => {
    const t = full(10)
    expect(needsDetectConfirm(t)).toBe(false)
    expect(needsDetectConfirm(addStreak(t))).toBe(true)
  })
  it('parseEpoch assumes UTC, parseExposure needs a positive number', () => {
    expect(parseEpoch('2025-04-04T13:01:30')!.toISOString()).toBe('2025-04-04T13:01:30.000Z')
    expect(parseEpoch('2025-04-04T13:01:30+02:00')!.toISOString()).toBe('2025-04-04T11:01:30.000Z')
    expect(parseEpoch(null)).toBeNull()
    expect(parseEpoch('nope')).toBeNull()
    expect([parseExposure('2.5'), parseExposure(''), parseExposure('0'), parseExposure('x')]).toEqual([2.5, null, null, null])
  })
  it('accepted file types', () => {
    expect(['a.FITS', 'a.fit', 'a.tif', 'a.tiff', 'a.jpg', 'a.jpeg', 'a.png'].every(fileAllowed)).toBe(true)
    expect(fileAllowed('a.gif')).toBe(false)
    expect(fileAllowed('noext')).toBe(false)
  })
  it('error text', () => {
    expect(astrometryErrorText(404, 'x')).toBe('Image expired — load it again')
    expect(astrometryErrorText(422, 'Unsupported file type')).toBe('Unsupported file type')
    expect(astrometryErrorText(413, '')).toBe('File too large')
    expect(astrometryErrorText(0, null)).toBe('Analysis service unavailable')
  })
})

describe('detect status summary (T5.2d)', () => {
  it('is one short line; an edge-exclusion warning adds a count; the full text goes to the title', async () => {
    const { detectSummary, detectTitle } = await import('../../app/utils/astrometry')
    expect(detectSummary(40, true, [])).toBe('40 streaks detected, target found')
    expect(detectSummary(1, false, [])).toContain('1 streak detected, no target found')
    const w = ['excluded 8 frame-clipped (edge) star trail(s) from the solve set — their truncated endpoints make the midpoint unreliable', 'only 9 usable stars (< 10 recommended)']
    expect(detectSummary(40, true, w)).toBe('40 streaks detected, target found · 8 edge streaks excluded')
    expect(detectSummary(40, true, [w[0]!.replace('8', '1')])).toBe('40 streaks detected, target found · 1 edge streak excluded')
    expect(detectSummary(40, true, [w[1]!])).toBe('40 streaks detected, target found')
    expect(detectTitle(w)).toContain('only 9 usable stars')
  })
})
