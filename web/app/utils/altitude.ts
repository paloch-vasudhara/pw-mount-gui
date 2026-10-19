// Altitude unit conversion. The MODEL is always metres; the unit only changes
// how the value is displayed and typed.
export type AltUnit = 'ft' | 'm' | 'km'

export const ALT_UNITS: AltUnit[] = ['ft', 'm', 'km']
export const ALT_LABEL: Record<AltUnit, string> = { ft: 'FT', m: 'M', km: 'KM' }
const METRES_PER: Record<AltUnit, number> = { ft: 0.3048, m: 1, km: 1000 }
const DECIMALS: Record<AltUnit, number> = { ft: 2, m: 2, km: 4 }
export const ALT_STEP: Record<AltUnit, number> = { ft: 1, m: 1, km: 0.001 }

/** metres -> display value in `unit` (rounded for display only). */
export function metersToUnit(m: number, unit: AltUnit): number {
  return +(m / METRES_PER[unit]).toFixed(DECIMALS[unit])
}

/** value typed in `unit` -> metres. */
export function unitToMeters(v: number, unit: AltUnit): number {
  return v * METRES_PER[unit]
}
