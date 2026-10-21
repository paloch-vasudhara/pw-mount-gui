// Track Info card value formatting (A1a). Azimuth reuses mount-format's formatter.
import { EM_DASH, formatAzimuth } from './mount-format'
import { fmtThousands } from './format'

const MINUS = '−'

function finite(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/** `241.16°` */
export function formatAz(v: number | null | undefined): string {
  return finite(v) ? `${formatAzimuth(v)}°` : EM_DASH
}

/** Signed: `+59.84°` / `−0.55°` (a value rounding to zero is `+0.00°`). */
export function formatEl(v: number | null | undefined): string {
  if (!finite(v)) return EM_DASH
  const r = Number(v.toFixed(2))
  return `${r < 0 ? MINUS : '+'}${Math.abs(r).toFixed(2)}°`
}

/** `36 540 km`: `.0f`, single-space thousands. */
export function formatRange(v: number | null | undefined): string {
  return finite(v) ? `${fmtThousands(Math.round(v).toString())} km` : EM_DASH
}

/** `TLE age 3.2 d` */
export function formatTleAge(v: number | null | undefined): string {
  return finite(v) ? `${v.toFixed(1)} d` : EM_DASH
}

export type Illumination = 'illuminated' | 'umbra' | 'below' | 'unknown'

/** Below horizon wins over the sunlit flag. */
export function illuminationOf(alt: number | null | undefined, lit: boolean | null | undefined): Illumination {
  if (finite(alt) && alt < 0) return 'below'
  if (lit === true) return 'illuminated'
  if (lit === false) return 'umbra'
  return 'unknown'
}

export const ILLUMINATION_LABEL: Record<Illumination, string> = {
  illuminated: 'Illuminated',
  umbra: 'Umbra',
  below: 'Below Horizon',
  unknown: EM_DASH,
}
