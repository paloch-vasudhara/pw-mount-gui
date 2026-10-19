// Ported from ssa-toolkit web/app/utils/dms.ts @84f2ded
// app/utils/dms.ts — the sexagesimal core of frontend/src/dms-input.js (779),
// extracted from the DOM (T10, §4b/T10). Every branch below is a line-for-line
// port of dms-input.js's _applyTotal / _writeState / _refreshSpinnerEnable /
// _step / _clampFieldsAndRender; the comments explaining WHY a branch exists
// are copied from the source. The DOM reads/writes (input.value, readonly
// attribute toggling, event wiring) are NOT ported here — they belong to the
// SFCs (DmsTriad.vue / DdInput.vue) that render this state.

export type DmsMode = 'cycle:24' | 'cycle:360' | 'limit:90' | 'limit:180'

export const MODE_TABLE: Record<DmsMode, { period: number | null; signed: boolean; limit: number | null }> = {
  'cycle:24':  { period: 24,  signed: false, limit: null },
  'cycle:360': { period: 360, signed: false, limit: null },
  'limit:90':  { period: null, signed: true,  limit: 90  },
  'limit:180': { period: null, signed: true,  limit: 180 },
}

/** The triad's value as four independent parts, exactly as the three boxes
 *  plus the sign chip hold it.  `d` is always a MAGNITUDE. */
export interface DmsState { sign: 1 | -1; d: number; m: number; s: number }

export interface DmsConfig {
  mode: DmsMode
  /** [min, max] in DEGREES, overriding the mode default (e.g. El [0, 90]). */
  range?: [number, number] | null
  hasCardinal: boolean
}

export const SEC_SPIN_STEP = 0.1     // arrow / wheel / spinner increment
export const SEC_DECIMALS  = 3       // typed-value display + round precision
export const SEC_ROUND     = 1e-3    // typed-value rounding grain

// Effective [minTotal, maxTotal] in ARCSECONDS: explicit range override, else
// mode defaults — mirrors the `minTotal`/`maxTotal` closure vars in initTriad.
function bounds(cfg: DmsConfig): { minTotal: number; maxTotal: number } {
  const mode = MODE_TABLE[cfg.mode]
  const minTotal = (cfg.range ? cfg.range[0] : (mode.signed ? -mode.limit! : 0)) * 3600
  const maxTotal = (cfg.range ? cfg.range[1] : (mode.signed ? +mode.limit! : mode.period!)) * 3600
  return { minTotal, maxTotal }
}

/** = the deg*3600 + m*60 + s total, signed. Mirrors `_getTotal`. */
export function toTotalArcsec(v: DmsState): number {
  return v.sign * (v.d * 3600 + v.m * 60 + v.s)
}

/** = `_applyTotal`: range reject, -180→+180 flip, cycle wrap, carry
 *  normalisation into (sign, d, m, s). Returns null when `total` is rejected
 *  (outside range, and not the -180→+180 flip case). */
export function fromTotalArcsec(total: number, cfg: DmsConfig): DmsState | null {
  const mode = MODE_TABLE[cfg.mode]
  const { minTotal, maxTotal } = bounds(cfg)

  if (total > maxTotal + 1e-6 || total < minTotal - 1e-6) {
    // Lon special: -180 auto-flips to +180 (only place crossing minTotal is
    // meaningful).
    if (mode.limit === 180 && Math.abs(total + maxTotal) < 1e-6) total = maxTotal
    else return null
  }
  // Cycle wrap.
  if (mode.period != null) {
    const c = mode.period * 3600
    total = ((total % c) + c) % c
  }
  const sign: 1 | -1 = total < 0 ? -1 : 1
  let abs = Math.abs(total)
  abs = Math.round(abs / SEC_ROUND) * SEC_ROUND
  let dInt = Math.floor(abs / 3600)
  const rem = abs - dInt * 3600
  let mInt = Math.floor(rem / 60)
  let sVal = +(rem - mInt * 60).toFixed(SEC_DECIMALS)
  if (sVal >= 60) { sVal -= 60; mInt += 1 }
  if (mInt >= 60) { mInt -= 60; dInt += 1 }
  return writeState({ sign, d: dInt, m: mInt, s: sVal }, cfg)
}

/** = `_writeState` WITHOUT the DOM writes: cap check, boundary snap, El
 *  zero-floor, lon flip, cycle wrap. The DOM version can REJECT (return
 *  false) when `d` exceeds the cap — that check is the caller's
 *  responsibility here (see `stepBox`, `clampFields`), which is why this
 *  always succeeds: by the time `writeState` is called, `next.d` is already
 *  known to be <= cap. */
export function writeState(next: DmsState, cfg: DmsConfig): DmsState {
  const mode = MODE_TABLE[cfg.mode]
  const { minTotal, maxTotal } = bounds(cfg)
  let sign = next.sign
  let dMag = next.d
  let mv = next.m
  let sv = next.s

  const cap = maxTotal / 3600
  if (dMag === cap && (mv > 0 || sv > 0)) {
    // Snap to exact boundary.
    mv = 0; sv = 0
  }
  // When range disallows negative (e.g. El [0, 90]) any underflow snaps the
  // WHOLE triad to the 0 floor (instead of being silently rejected, which
  // felt like the spinner was stuck). So pressing DOWN on deg at `0° 5' 0"`
  // drops to `0° 0' 0.0"` in one click, etc.
  if (sign < 0 && minTotal >= 0) {
    sign = 1; dMag = 0; mv = 0; sv = 0
  }
  // Lon -180 → +180 auto-flip.
  if (mode.limit === 180 && dMag === 180 && sign < 0) sign = 1
  // Cycle modes: wrap (defence in depth).
  if (mode.period != null) {
    if (dMag >= mode.period) dMag = dMag % mode.period
    if (sign < 0) { sign = 1; dMag = 0 }   // unsigned modes
  }

  // Boundary freeze — at |limit|, M and S are zero (readonly is `isFrozen`'s
  // job, computed from the returned state by the SFC).
  const atLimit = (mode.limit != null && dMag === mode.limit) || dMag === cap
  if (atLimit) { mv = 0; sv = 0 }

  return { sign, d: dMag, m: mv, s: sv }
}

/** The boundary freeze — at |limit|, M and S are 0 + readonly. */
export function isFrozen(v: DmsState, cfg: DmsConfig): boolean {
  const mode = MODE_TABLE[cfg.mode]
  const { maxTotal } = bounds(cfg)
  const cap = maxTotal / 3600
  return (mode.limit != null && v.d === mode.limit) || v.d === cap
}

/** = `_step`, INCLUDING the limit:180 signed-total antimeridian branch and
 *  the S-preserved cross-zero. Returns `v` UNCHANGED when the step is
 *  rejected (mirrors `_writeState` returning false and the DOM being left
 *  untouched). */
export function stepBox(v: DmsState, box: 'd' | 'm' | 's', dir: 1 | -1, cfg: DmsConfig): DmsState {
  const mode = MODE_TABLE[cfg.mode]
  const { maxTotal } = bounds(cfg)
  const cap = maxTotal / 3600

  // Longitude cycles across the antimeridian: step the SIGNED total and wrap
  // by ±360° so UP past +180° continues at −179°59'59" (E→W) and DOWN past
  // −180° continues at +179°59'59" (W→E), instead of freezing at ±180. UP
  // always increases the signed total; unit is per targeted box.
  if (mode.limit === 180) {
    const unit = box === 'd' ? 3600 : (box === 'm' ? 60 : SEC_SPIN_STEP)
    const half = maxTotal          // 180° in arcsec
    const full = half * 2          // 360°
    let total = toTotalArcsec(v) + dir * unit
    total = ((total + half) % full + full) % full - half  // → (−180, 180]
    if (total <= -half) total += full                     // keep +180 canonical
    return fromTotalArcsec(total, cfg) ?? v
  }

  const dMag = v.d
  const mv = v.m
  const sv = v.s
  const isSigned = mode.signed
  const sign = v.sign
  const magDir = (sign > 0 || !isSigned) ? dir : -dir

  if (box === 'd') {
    let newDMag = dMag + magDir
    let newSign = sign
    if (newDMag < 0) {
      if (isSigned) { newSign = -sign as 1 | -1; newDMag = 0 }
      else if (mode.period != null) { newDMag = mode.period - 1 }
    } else if (mode.period != null && newDMag >= mode.period) {
      newDMag = newDMag % mode.period
    }
    if (newDMag > cap) return v
    return writeState({ sign: newSign, d: newDMag, m: mv, s: sv }, cfg)
  }

  if (box === 'm') {
    let newMv = mv + magDir
    let newDMag = dMag
    let newSign = sign
    const newSv = sv
    if (newMv < 0) {
      if (dMag === 0) {
        if (isSigned) {
          // Forward cross-zero: e.g. [N] 0°0' DOWN → [S] 0°1' (S preserved).
          newSign = -sign as 1 | -1
          newDMag = 0
          newMv = 1
        } else if (mode.period != null) {
          newDMag = mode.period - 1
          newMv = 59
        }
      } else {
        newDMag -= 1
        newMv = 59
      }
    } else if (
      isSigned && sign < 0 && dMag === 0 && mv === 1 && newMv === 0 && magDir < 0
    ) {
      // Reverse cross-zero: e.g. [S] 0°1' UP → [N] 0°0' (S preserved).
      newSign = 1
      newDMag = 0
      newMv = 0
    } else if (newMv >= 60) {
      newDMag += 1
      newMv = 0
      if (mode.period != null && newDMag >= mode.period) newDMag = newDMag % mode.period
    }
    if (newDMag > cap) return v
    return writeState({ sign: newSign, d: newDMag, m: newMv, s: newSv }, cfg)
  }

  // SEC spinner
  let newSv = +(sv + magDir * SEC_SPIN_STEP).toFixed(SEC_DECIMALS)
  let newMv = mv
  let newDMag = dMag
  let newSign = sign
  const maxSec = +(60 - SEC_SPIN_STEP).toFixed(SEC_DECIMALS)
  if (newSv < 0) {
    if (mv === 0 && dMag === 0) {
      if (isSigned) {
        newSign = -sign as 1 | -1
        newSv = SEC_SPIN_STEP
      } else if (mode.period != null) {
        newDMag = mode.period - 1
        newMv = 59
        newSv = maxSec
      }
    } else if (mv === 0) {
      newDMag -= 1
      newMv = 59
      newSv = maxSec
    } else {
      newMv -= 1
      newSv = maxSec
    }
  } else if (
    isSigned && sign < 0 && dMag === 0 && mv === 0 && newSv === 0 && magDir < 0
  ) {
    // Reverse cross-zero for seconds (T0.2): [-] 0°0'0.1" UP -> [+] 0°0'0.0",
    // mirroring the minutes branch above, so stepping up through zero flips
    // the chip instead of parking on a negative zero.
    newSign = 1
  } else if (newSv >= 60) {
    newSv = 0
    newMv += 1
    if (newMv >= 60) {
      newMv = 0
      newDMag += 1
      if (mode.period != null && newDMag >= mode.period) newDMag = newDMag % mode.period
    }
  }
  if (newDMag > cap) return v
  return writeState({ sign: newSign, d: newDMag, m: newMv, s: newSv }, cfg)
}

/** = `_clampFieldsAndRender`: per-field clamp, NO carry. Each field
 *  registers as its own max if exceeded (seconds → 59.999, minutes → 59,
 *  degrees → cap with freeze); cycle modes (RA hours / Az) wrap. `raw.d` may
 *  carry a typed leading sign (sign absorption, cardinal triads only) — see
 *  the `cfg.hasCardinal` branch below, mirroring `rawDeg < 0` in the source. */
export function clampFields(raw: { d: number; m: number; s: number; sign: 1 | -1 }, cfg: DmsConfig): DmsState {
  const mode = MODE_TABLE[cfg.mode]
  const { maxTotal } = bounds(cfg)

  let sign = raw.sign
  if (cfg.hasCardinal && isFinite(raw.d) && raw.d < 0) sign = -1
  let dMag = Math.abs(isFinite(raw.d) ? raw.d : 0)
  let mv = raw.m
  let sv = raw.s

  // Seconds: clamp [0, 59.999] and round to precision.
  sv = Math.max(0, Math.round(Math.max(0, sv) / SEC_ROUND) * SEC_ROUND)
  const secMax = +(60 - SEC_ROUND).toFixed(SEC_DECIMALS)   // 59.999
  if (sv > secMax) sv = secMax
  sv = +sv.toFixed(SEC_DECIMALS)

  // Minutes: clamp [0, 59].
  mv = Math.max(0, Math.min(59, Math.floor(mv)))

  // Degrees / hours: cycle modes wrap; limit modes clamp to the cap.
  if (mode.period != null) {
    dMag = ((dMag % mode.period) + mode.period) % mode.period
  } else {
    const cap = maxTotal / 3600
    if (dMag > cap) dMag = cap
  }

  // writeState applies the boundary freeze, El zero-floor, lon flip, and
  // resolves the sign chip.
  return writeState({ sign, d: dMag, m: mv, s: sv }, cfg)
}

/** = `_refreshSpinnerEnable` dim rules. `true` = enabled (not dimmed). */
export function spinnerEnable(v: DmsState, cfg: DmsConfig): { up: boolean; dn: boolean } {
  const mode = MODE_TABLE[cfg.mode]
  const { minTotal, maxTotal } = bounds(cfg)

  // Fields that cycle/wrap never hit a hard stop, so their arrows never dim:
  //   - cycle modes (RA hours, Az) wrap 0↔period;
  //   - longitude (limit:180) wraps across the antimeridian (±180), flipping
  //     the sign and continuing.
  if (mode.period != null || mode.limit === 180) {
    return { up: true, dn: true }
  }
  // Otherwise dim ONLY at a true ± hard cap (Lat/Dec poles ±90, El zenith
  // 90). There is deliberately NO "at zero" disable: a signed field crosses
  // zero via the sign flip, and a positive-floored range (El [0, 90]) simply
  // clamps at 0 on DOWN with the arrow left active — values can be scrolled
  // through/at 0 rather than the button locking there.
  const dMag = v.d
  const cap = maxTotal / 3600
  const minCap = minTotal / 3600
  const sign = v.sign
  const atPos = sign >= 0 && dMag === cap                            // UP at +cap
  const atNeg = sign <= 0 && minCap < 0 && dMag === Math.abs(minCap)  // DOWN at −cap
  return { up: !atPos, dn: !atNeg }
}

/** 2-digit zero pad — arcmin display ("3" → "03"). */
export function padMin(m: number): string {
  return String(m).padStart(2, '0')
}

/** 2-digit integer pad, 3 decimals — arcsec display ("3.26" → "03.260"). */
export function padSec(s: number): string {
  const str = Number(s).toFixed(SEC_DECIMALS)
  const dot = str.indexOf('.')
  return dot < 0 ? str.padStart(2, '0') : str.slice(0, dot).padStart(2, '0') + str.slice(dot)
}

export function decimalsForStep(step: number): number {
  if (step >= 1) return 0
  return Math.max(0, Math.min(6, -Math.floor(Math.log10(step) - 1e-9)))
}
