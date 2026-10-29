// Stale-TLE badge threshold (shared with the analysis 30-day cutoff).
export const STALE_TLE_DAYS = 30

/** Whole days to show when the TLE is stale (> STALE_TLE_DAYS), else null. */
export function staleTleDays(ageDays: number | null | undefined): number | null {
  if (typeof ageDays !== 'number' || !Number.isFinite(ageDays) || ageDays <= STALE_TLE_DAYS) return null
  return Math.floor(ageDays)
}
