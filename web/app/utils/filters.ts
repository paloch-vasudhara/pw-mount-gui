// Visibility filter semantics (A2a), pure.
//
// Tree: Satellites > (LEO, MEO, GEO) · Aircrafts (disabled, no ADS-B yet) · Celestial Objects >
// (Planetary Bodies, Stars) · Labels (top level, no children). Unchecking a parent hides everything under it but KEEPS the children's
// own checks; a child is visible only when its parent AND itself are on.

export interface FilterState {
  satellites: boolean
  leo: boolean
  meo: boolean
  geo: boolean
  aircrafts: boolean
  celestial: boolean
  planets: boolean
  stars: boolean
  labels: boolean // decluttered names; off keeps only the hovered / selected label
}

export const DEFAULT_FILTERS: Readonly<FilterState> = {
  satellites: true,
  leo: true,
  meo: true,
  geo: true,
  aircrafts: false, // present but disabled until an ADS-B source exists
  celestial: true,
  planets: true,
  stars: true,
  labels: true,
}

export const FILTERS_STORAGE_KEY = 'pw-mount-control.sky-filters.v1'

export interface Visibility {
  showLeo: boolean
  showMeo: boolean
  showGeo: boolean
  showStars: boolean
  showBodies: boolean
  showLabels: boolean
}

export function effectiveVisibility(f: FilterState): Visibility {
  return {
    showLeo: f.satellites && f.leo,
    showMeo: f.satellites && f.meo,
    showGeo: f.satellites && f.geo,
    showStars: f.celestial && f.stars,
    showBodies: f.celestial && f.planets,
    showLabels: f.labels,
  }
}

/** Parse a stored value; unknown / malformed / missing keys fall back to the defaults. */
export function parseFilters(raw: string | null | undefined): FilterState {
  const out: FilterState = { ...DEFAULT_FILTERS }
  if (!raw) return out
  try {
    const o = JSON.parse(raw) as Record<string, unknown>
    for (const k of Object.keys(DEFAULT_FILTERS) as Array<keyof FilterState>) {
      if (k !== 'aircrafts' && typeof o[k] === 'boolean') out[k] = o[k] as boolean
    }
  }
  catch {
    // keep defaults
  }
  return out
}

export function serializeFilters(f: FilterState): string {
  return JSON.stringify(f)
}
