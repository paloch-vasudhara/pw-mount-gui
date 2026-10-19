// Ported from ssa-toolkit web/tests/unit/dms-core.spec.ts @84f2ded
// @vitest-environment node
import { describe, it, expect } from 'vitest'
import {
  toTotalArcsec, fromTotalArcsec, writeState, isFrozen, stepBox, clampFields,
  spinnerEnable, padMin, padSec, decimalsForStep,
  type DmsConfig, type DmsState,
} from '../../app/utils/dms'

const limit90: DmsConfig = { mode: 'limit:90', hasCardinal: true }
const limit180: DmsConfig = { mode: 'limit:180', hasCardinal: true }
const cycle24: DmsConfig = { mode: 'cycle:24', hasCardinal: false }
const cycle360: DmsConfig = { mode: 'cycle:360', hasCardinal: false }
const elevation: DmsConfig = { mode: 'limit:90', hasCardinal: false, range: [0, 90] }

describe('1. Spinner carries; typing does not', () => {
  it('stepBox cascades a borrow into the box above (sec underflow -> minute borrow)', () => {
    const v: DmsState = { sign: 1, d: 0, m: 1, s: 0 }
    const next = stepBox(v, 's', -1, limit90)
    expect(next).toEqual({ sign: 1, d: 0, m: 0, s: 59.9 })
  })
  it('clampFields registers each field at its own max and never carries', () => {
    const next = clampFields({ d: 0, m: 0, s: 75.6, sign: 1 }, limit90)
    expect(next.s).toBe(59.999)
    expect(next.m).toBe(0) // unchanged, no carry
  })
})

describe('2. UP always increases the SIGNED total', () => {
  it('in negative mode, DOWN grows the displayed magnitude (magDir inversion)', () => {
    const v: DmsState = { sign: -1, d: 10, m: 0, s: 0 }
    const down = stepBox(v, 'd', -1, limit90)
    expect(down.d).toBe(11) // DOWN grew the magnitude
    const up = stepBox(v, 'd', 1, limit90)
    expect(up.d).toBe(9) // UP shrank the magnitude
  })
})

describe('3. S-preserved cross-zero', () => {
  it('[N] 0°0\' DOWN -> [S] 0°1\', seconds untouched', () => {
    const v: DmsState = { sign: 1, d: 0, m: 0, s: 12.5 }
    const next = stepBox(v, 'm', -1, limit90)
    expect(next).toEqual({ sign: -1, d: 0, m: 1, s: 12.5 })
  })
  it('[S] 0°1\' UP -> [N] 0°0\', seconds untouched', () => {
    const v: DmsState = { sign: -1, d: 0, m: 1, s: 12.5 }
    const next = stepBox(v, 'm', 1, limit90)
    expect(next).toEqual({ sign: 1, d: 0, m: 0, s: 12.5 })
  })
  it('explicit: stepBox({1,0,0,0},"m",-1,limit90) -> {-1,0,1,0}', () => {
    expect(stepBox({ sign: 1, d: 0, m: 0, s: 0 }, 'm', -1, limit90))
      .toEqual({ sign: -1, d: 0, m: 1, s: 0 })
  })
  it('explicit: stepBox({-1,0,1,0},"m",+1,limit90) -> {1,0,0,0}', () => {
    expect(stepBox({ sign: -1, d: 0, m: 1, s: 0 }, 'm', 1, limit90))
      .toEqual({ sign: 1, d: 0, m: 0, s: 0 })
  })
})

describe('4. Boundary freeze', () => {
  it('at |limit|, M and S are forced to 0', () => {
    const next = writeState({ sign: 1, d: 90, m: 5, s: 5 }, limit90)
    expect(next).toEqual({ sign: 1, d: 90, m: 0, s: 0 })
    expect(isFrozen(next, limit90)).toBe(true)
  })
  it('leaving the boundary is not frozen', () => {
    const v: DmsState = { sign: 1, d: 45, m: 0, s: 0 }
    expect(isFrozen(v, limit90)).toBe(false)
  })
})

describe('5. Longitude cycles, latitude clamps', () => {
  it('limit:180 steps the signed total and wraps through the antimeridian, +180 UP (sec) -> -179 59 59', () => {
    const v: DmsState = { sign: 1, d: 180, m: 0, s: 0 }
    const next = stepBox(v, 's', 1, limit180)
    expect(next.sign).toBe(-1)
    expect(next.d).toBe(179)
    expect(next.m).toBe(59)
    expect(next.s).toBeCloseTo(59.9, 5)
  })
  it('keeps +180 canonical (not -180)', () => {
    const v: DmsState = { sign: 1, d: 179, m: 59, s: 59.9 }
    const next = stepBox(v, 's', 1, limit180)
    expect(next.sign).toBe(1)
    expect(next.d).toBe(180)
  })
  it('limit:90 stops dead at +-90 (stepBox on deg at +90 is a no-op)', () => {
    const v: DmsState = { sign: 1, d: 90, m: 0, s: 0 }
    const next = stepBox(v, 'd', 1, limit90)
    expect(next).toEqual(v)
  })
})

describe('6. El zero-floor', () => {
  it('DOWN at 0 with range:[0,90] snaps the WHOLE triad to 0deg0m0.0s in one press', () => {
    const v: DmsState = { sign: 1, d: 0, m: 0, s: 0 }
    const next = stepBox(v, 'm', -1, elevation)
    expect(next).toEqual({ sign: 1, d: 0, m: 0, s: 0 })
  })
  it('stepBox DOWN at 0,0,0 with range:[0,90] -> all zeros, spinnerEnable().dn === true', () => {
    const v: DmsState = { sign: 1, d: 0, m: 0, s: 0 }
    const next = stepBox(v, 'd', -1, elevation)
    expect(next).toEqual({ sign: 1, d: 0, m: 0, s: 0 })
    expect(spinnerEnable(next, elevation).dn).toBe(true)
  })
})

describe('7. Dim rules', () => {
  it('cycle modes never dim', () => {
    const v: DmsState = { sign: 1, d: 0, m: 0, s: 0 }
    expect(spinnerEnable(v, cycle24)).toEqual({ up: true, dn: true })
    expect(spinnerEnable(v, cycle360)).toEqual({ up: true, dn: true })
  })
  it('limit:180 never dims', () => {
    const v: DmsState = { sign: 1, d: 180, m: 0, s: 0 }
    expect(spinnerEnable(v, limit180)).toEqual({ up: true, dn: true })
  })
  it('UP dims only at +cap, DOWN only at -cap; no dim at zero', () => {
    const zero: DmsState = { sign: 1, d: 0, m: 0, s: 0 }
    expect(spinnerEnable(zero, limit90)).toEqual({ up: true, dn: true })
    const atPos: DmsState = { sign: 1, d: 90, m: 0, s: 0 }
    expect(spinnerEnable(atPos, limit90)).toEqual({ up: false, dn: true })
    const atNeg: DmsState = { sign: -1, d: 90, m: 0, s: 0 }
    expect(spinnerEnable(atNeg, limit90)).toEqual({ up: true, dn: false })
  })
  it('spinnerEnable at +-90 dims exactly one arrow', () => {
    const atPos: DmsState = { sign: 1, d: 90, m: 0, s: 0 }
    const r = spinnerEnable(atPos, limit90)
    expect(r.up !== r.dn).toBe(true)
  })
})

describe('fromTotalArcsec', () => {
  it('rejects (returns null) outside range', () => {
    expect(fromTotalArcsec(91 * 3600, limit90)).toBeNull()
  })
  it('except the -180 -> +180 flip', () => {
    const next = fromTotalArcsec(-180 * 3600, limit180)
    expect(next).toEqual({ sign: 1, d: 180, m: 0, s: 0 })
  })
  it('round-trips toTotalArcsec', () => {
    const v: DmsState = { sign: -1, d: 45, m: 30, s: 15 }
    const total = toTotalArcsec(v)
    expect(fromTotalArcsec(total, limit90)).toEqual(v)
  })
})

describe('pad / decimals helpers', () => {
  it('padSec(3.26) === "03.260"', () => { expect(padSec(3.26)).toBe('03.260') })
  it('padMin(3) === "03"', () => { expect(padMin(3)).toBe('03') })
  // DEVIATION (report): the design doc's Test contract row states
  // decimalsForStep(0.001)===3 and (0.1)===1, but frontend/src/dms-input.js's
  // `_decimalsForStep` (ported verbatim, byte-for-byte) actually evaluates to
  // 4 and 2 respectively — `Math.log10(0.001) - 1e-9` and
  // `Math.log10(0.1) - 1e-9` both floor one integer lower than the doc
  // assumed (verified with `node -e`). This is a pre-existing float-precision
  // quirk already shipped in the production frontend, not introduced here.
  // Per the "port the algorithm verbatim" rule this was NOT "fixed" — the
  // assertions below match the true, verified behaviour of the source.
  it('decimalsForStep(0.001) === 4 (verbatim quirk, see deviation note above)', () => { expect(decimalsForStep(0.001)).toBe(4) })
  it('decimalsForStep(1) === 0', () => { expect(decimalsForStep(1)).toBe(0) })
  it('decimalsForStep(0.1) === 2 (verbatim quirk, see deviation note above)', () => { expect(decimalsForStep(0.1)).toBe(2) })
})
