// Ported from ssa-toolkit web/app/utils/angle-format.ts @84f2ded
// app/utils/angle-format.ts — the format-toggle arithmetic of
// frontend/src/observer-view.js (_decToSplit / _ovSetupRA / _ovSetupDMS),
// extracted from the DOM (T10d, §4b/T10d — GAP FIX on already-shipped T10
// work, found in owner review of C2 2026-09-10). Ported verbatim, rounding
// included. No DOM, no Vue — <AngleField> composes this with the already-
// shipped <DdInput>/<DmsTriad> leaf widgets.

export type AngleKind = 'latlon' | 'ra' | 'angle'
export type AngleFormat = 'dd' | 'dms' | 'hd' | 'hms'

/** The format list per kind — the exact <option> sets in index.html. */
export const FORMATS_FOR: Record<AngleKind, AngleFormat[]> = {
  latlon: ['dd', 'dms'],          // pass-lat-fmt / pass-lon-fmt / obs-* / od-* / launch-* / aeq-lau-*
  ra:     ['hd', 'hms', 'dd'],    // ov-img-ra-fmt  (HD selected by default)
  angle:  ['dd', 'dms'],          // ov-img-dec-fmt / -az-fmt / -el-fmt
}

/** Option labels, verbatim from index.html. */
export const FORMAT_LABEL: Record<AngleFormat, string> = {
  dd: 'DD', dms: 'DMS', hd: 'HD', hms: 'HMS',
}

/** _decToSplit, verbatim (observer-view.js ~203). Note s is rounded to 2dp
 *  here and NOT re-normalised — 59.996" stays 60.00, exactly as legacy. */
export function decToSplit(dec: number): { d: number; m: number; s: number } {
  const sign = dec < 0 ? -1 : 1
  const abs = Math.abs(dec)
  const d = Math.floor(abs)
  const mf = (abs - d) * 60
  const m = Math.floor(mf)
  const s = (mf - m) * 60
  return { d: sign * d, m, s: +s.toFixed(2) }
}

/** RA display rounding — _ovSetupRA's two write branches, verbatim.
 *  Empty string for a falsy value is legacy behaviour (`hours ? ... : ""`)
 *  and covers BOTH 0 and NaN; it is why an RA of exactly 0 blanks the box. */
export function raDisplayHd(hours: number): number | '' {
  return hours ? +hours.toFixed(4) : ''
}
export function raDisplayDd(hours: number): number | '' {
  return hours ? +(hours * 15).toFixed(4) : ''
}

/** DD display rounding for the DD/DMS toggle — _ovSetupDMS, verbatim
 *  (out is rounded to 4dp, empty string when falsy). */
export function ddDisplay(deg: number): number | '' {
  return deg ? +deg.toFixed(4) : ''
}

/** Inverse of decToSplit — sign from d, magnitude from |d| + m/60 + s/3600.
 *  Mirrors the magnitude extraction in _ovReadCurrentValue / _ovSetupDMS. */
export function splitToDecimal(sp: { d: number; m: number; s: number }): number {
  const sign = sp.d < 0 ? -1 : 1
  return sign * (Math.abs(sp.d) + sp.m / 60 + sp.s / 3600)
}

/** The one function AngleField calls on a select change: re-round the
 *  canonical value into the precision the NEW format displays, and hand
 *  back the value the model should now hold.
 *  `value` and the return are in CANONICAL units for the kind:
 *  hours for kind 'ra', degrees for 'latlon' | 'angle'. */
export function convert(value: number, kind: AngleKind, to: AngleFormat): number {
  if (!Number.isFinite(value)) return 0
  if (kind === 'ra') {
    if (to === 'hd')  { const v = raDisplayHd(value); return v === '' ? 0 : v }
    if (to === 'dd')  { const v = raDisplayDd(value); return v === '' ? 0 : v / 15 }
    // hms — the triad's own state carries d/m/s; the canonical hours value
    // is the split recomposed, so the 2dp seconds rounding is visible.
    const sp = decToSplit(value)
    return splitToDecimal(sp)
  }
  if (to === 'dd') { const v = ddDisplay(value); return v === '' ? 0 : v }
  return splitToDecimal(decToSplit(value))
}
