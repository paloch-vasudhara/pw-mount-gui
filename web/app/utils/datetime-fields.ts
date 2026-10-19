// Ported from ssa-toolkit web/app/utils/datetime-fields.ts @84f2ded
// app/utils/datetime-fields.ts — the pure field math of
// frontend/src/datetime-picker.js (674), extracted the same way T10 extracts
// dms.ts: every branch below is a line-for-line port of `_step` / `_daysInMonth`
// / `_padSec` / `_secDecimals` / `_clampNum` / `_ymd` / `_calLo` / `_calHi` /
// `_readFields` / `_writeFromUtcDate`. No DOM, no state — the SFCs
// (DateTimeField.vue / DateTimePopover.vue / AppCalendar.vue) own the reads/
// writes onto the six boxes and the CalendarDate <-> Date conversion.
//
// BINDING UTC boundary (§4b/T11): every function here operates on UTC fields
// or a UTC-only `Date` (via `Date.UTC(...)` / `getUTC*()`); nothing here
// touches `CalendarDate`, `getLocalTimeZone()`, `today()`, `toDate()`, or a
// locale/timezone-aware API of any kind — that conversion happens only in
// AppCalendar.vue, at the boundary, and only via `new CalendarDate(y, mo, d)`
// / `Date.UTC(cd.year, cd.month - 1, cd.day)`.

/** = MIN_YEAR / maxYear() — the legal year range, recomputed live (never
 *  hard-coded) so the tool never goes stale. */
export const MIN_YEAR = 1900
export function maxYear(): number {
  return new Date().getUTCFullYear() + 100
}

/** = `_daysInMonth`: day-count of `monthIdx` (0-based) in `year`, via the
 *  "day 0 of next month" UTC trick — leap years and month lengths come free
 *  from `Date`. */
export function daysInMonth(year: number, monthIdx: number): number {
  return new Date(Date.UTC(year, monthIdx + 1, 0)).getUTCDate()
}

export interface DtFields { y: number; mo: number; d: number; h: number; mi: number; s: number }
export type DtUnit = 'y' | 'mo' | 'd' | 'h' | 'mi' | 's'

/** 2-digit zero pad (month/day/hour/minute, and the whole-number part of
 *  seconds). Year is never padded (4 digits, or more once >9999 — untouched,
 *  matching `_pad2`, which the legacy code never applies to `y`). */
export function pad2(v: number): string {
  return String(v).padStart(2, '0')
}

/** = `_padSec`: zero-pad the integer part to 2 digits; `dec` decimals when
 *  given (0 → whole seconds, rounded). */
export function padSec(v: number, dec: number): string {
  const s = dec ? Number(v).toFixed(dec) : String(Math.round(v))
  const dot = s.indexOf('.')
  return dot < 0 ? s.padStart(2, '0') : s.slice(0, dot).padStart(2, '0') + s.slice(dot)
}

/** = `_secDecimals`: derive the seconds box's display precision from its
 *  `step` attribute (>=1 -> 0 decimals; 0.001 -> 3; capped at 3, matching the
 *  legacy popup's fixed 3-decimal time pane and the segmented box's own
 *  `step`-driven precision). */
export function secDecimals(step: number): number {
  if (step >= 1) return 0
  return Math.max(0, Math.min(3, -Math.floor(Math.log10(step) - 1e-9)))
}

/** = `_clampNum`. */
export function clampNum(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v))
}

/** = `_ymd` / `_calLo` / `_calHi`: a single sortable integer key
 *  (y*10000 + mo*100 + d) so date comparisons across a year boundary are
 *  plain numeric comparisons. */
export function ymd(y: number, mo: number, d: number): number {
  return y * 10000 + mo * 100 + d
}
export function calLo(): number {
  return ymd(MIN_YEAR, 1, 1)
}
export function calHi(): number {
  return ymd(maxYear(), 12, 31)
}

/** = `_dateFieldsValid`. */
export function dateFieldsValid(f: Pick<DtFields, 'y' | 'mo' | 'd'>): boolean {
  return [f.y, f.mo, f.d].every(Number.isFinite)
}

/** = `_readFields`, given the six box strings directly (the SFC owns the DOM
 *  read). */
export function readFields(y: string, mo: string, d: string, h: string, mi: string, s: string): DtFields {
  return {
    y: parseInt(y, 10),
    mo: parseInt(mo, 10),
    d: parseInt(d, 10),
    h: parseInt(h, 10) || 0,
    mi: parseInt(mi, 10) || 0,
    s: parseFloat(s) || 0,
  }
}

/** = `_writeFromUtcDate`'s field half: read a UTC `Date` into `DtFields`
 *  (fractional seconds carried in `s`, matching
 *  `dt.getUTCSeconds() + dt.getUTCMilliseconds() / 1000`). */
export function utcDateToFields(dt: Date): DtFields {
  return {
    y: dt.getUTCFullYear(),
    mo: dt.getUTCMonth() + 1,
    d: dt.getUTCDate(),
    h: dt.getUTCHours(),
    mi: dt.getUTCMinutes(),
    s: dt.getUTCSeconds() + dt.getUTCMilliseconds() / 1000,
  }
}

/** = `_writeFromUtcDate`'s the other way: build a UTC `Date` from the six
 *  fields. */
export function fieldsToUtcDate(f: DtFields): Date {
  const secInt = Math.floor(f.s)
  const ms = Math.round((f.s - secInt) * 1000)
  return new Date(Date.UTC(f.y, f.mo - 1, f.d, f.h, f.mi, secInt, ms))
}

/** = `_step`: step `unit` by `dir`, INCLUDING the day-clamp-to-month rule
 *  (year/month carries clamp the day into the target month rather than
 *  rolling over — 2024-02-29 year-stepped -> 2025-02-28, never rolling to
 *  March) and the [MIN_YEAR, maxYear()] refusal (returns `null`, mirroring
 *  the legacy `return;` no-op, when a carry would cross the bounds). Other
 *  units (`d`/`h`/`mi`/`s`) use native UTC `Date` carry, so month lengths and
 *  leap years are handled automatically by the engine, not by hand.
 *  `secStep` is the seconds box's `step` attribute (arcsecond-style
 *  decoupled increment, matching `dt-pop-s`'s `step="0.001"` / the segmented
 *  box's own `step`). */
export function stepField(f: DtFields, unit: DtUnit, dir: 1 | -1, secStep = 1): DtFields | null {
  const secInt = Math.floor(f.s)
  const ms = Math.round((f.s - secInt) * 1000)
  const maxY = maxYear()
  let dt: Date

  if (unit === 'y') {
    const ny = f.y + dir
    if (ny < MIN_YEAR || ny > maxY) return null
    const dd = Math.min(f.d, daysInMonth(ny, f.mo - 1))
    dt = new Date(Date.UTC(ny, f.mo - 1, dd, f.h, f.mi, secInt, ms))
  } else if (unit === 'mo') {
    let mIdx = f.mo - 1 + dir
    let ny = f.y
    if (mIdx < 0) { mIdx = 11; ny -= 1 }
    else if (mIdx > 11) { mIdx = 0; ny += 1 }
    if (ny < MIN_YEAR || ny > maxY) return null
    const dd = Math.min(f.d, daysInMonth(ny, mIdx))
    dt = new Date(Date.UTC(ny, mIdx, dd, f.h, f.mi, secInt, ms))
  } else {
    dt = new Date(Date.UTC(f.y, f.mo - 1, f.d, f.h, f.mi, secInt, ms))
    if (unit === 'd') dt.setUTCDate(dt.getUTCDate() + dir)
    else if (unit === 'h') dt.setUTCHours(dt.getUTCHours() + dir)
    else if (unit === 'mi') dt.setUTCMinutes(dt.getUTCMinutes() + dir)
    else if (unit === 's') dt.setUTCMilliseconds(dt.getUTCMilliseconds() + dir * Math.round(secStep * 1000))
    const yy = dt.getUTCFullYear()
    if (yy < MIN_YEAR || yy > maxY) return null
  }

  return utcDateToFields(dt)
}

/** = the segmented box's `blur` clamp (`_initScroll`'s per-field clamp): NO
 *  carry — a typed value is clamped to its OWN max only; the day-clamp-to-
 *  month re-check (only fires after `mo`/`y` changed) is the caller's job via
 *  `clampDayToMonth`, matching the legacy `if (unit === "mo" || unit ===
 *  "y") { ... }` follow-up block. */
export function clampField(unit: DtUnit, value: number, f: DtFields): number {
  if (unit === 's') return clampNum(value, 0, 59.999)
  if (unit === 'y') return clampNum(value, MIN_YEAR, maxYear())
  if (unit === 'mo') return clampNum(value, 1, 12)
  if (unit === 'h') return clampNum(value, 0, 23)
  if (unit === 'mi') return clampNum(value, 0, 59)
  // unit === 'd'
  const dim = dateFieldsValid({ y: f.y, mo: f.mo, d: f.d }) ? daysInMonth(f.y, f.mo - 1) : 31
  return clampNum(value, 1, dim)
}

/** = the legacy `if (unit === "mo" || unit === "y")` day re-clamp: when the
 *  month or year field commits, re-clamp `d` to the new month's length
 *  (leap-aware — Feb 30 -> 28/29, Apr 31 -> 30). Returns the (possibly
 *  unchanged) day. */
export function clampDayToMonth(f: DtFields): number {
  if (!dateFieldsValid({ y: f.y, mo: f.mo, d: f.d })) return f.d
  const dim = daysInMonth(f.y, f.mo - 1)
  return f.d > dim ? dim : f.d
}
