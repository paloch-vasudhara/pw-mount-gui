// Gateway snapshot -> 3D view inputs (pure, unit-tested).
import { haDecToAltAz, wrap360 } from './coords'
import { DEFAULT_SUN_KEEPOUT_DEG } from './config'

export interface AltAzDeg { alt: number, az: number }

export interface HemisphereInputs {
  /** Reported mount pointing; null hides the arrow/markers (link stale or mount not connected). */
  mount: AltAzDeg | null
  sun: AltAzDeg | null
  moon: AltAzDeg | null
  /** Lim EL ring altitude; null = no ring. */
  limElDeg: number | null
  keepoutDeg: number
  /** Slew target as alt/az; only set while SLEWING. */
  target: AltAzDeg | null
}

export interface HemisphereSnapshotLike {
  state?: string | null
  site?: { lat?: number | null } | null
  time?: { lmst_hours?: number | null } | null
  mount?: {
    az?: number | null
    alt?: number | null
    target_ra_apparent_h?: number | null
    target_dec_apparent_deg?: number | null
  } | null
  sun?: { alt?: number | null, az?: number | null } | null
  moon?: { alt?: number | null, az?: number | null } | null
  limits?: { lim_el_deg?: number | null, sun_keepout_deg?: number | null } | null
}

export const EMPTY_INPUTS: HemisphereInputs = {
  mount: null, sun: null, moon: null, limElDeg: null, keepoutDeg: DEFAULT_SUN_KEEPOUT_DEG, target: null,
}

function finite(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

function pair(p: { alt?: number | null, az?: number | null } | null | undefined): AltAzDeg | null {
  return p && finite(p.alt) && finite(p.az) ? { alt: p.alt, az: p.az } : null
}

export function buildHemisphereInputs(
  snapshot: HemisphereSnapshotLike | null | undefined,
  linkStale: boolean,
): HemisphereInputs {
  if (!snapshot) return EMPTY_INPUTS
  const keepout = snapshot.limits?.sun_keepout_deg
  const limEl = snapshot.limits?.lim_el_deg
  const base: HemisphereInputs = {
    mount: null,
    sun: pair(snapshot.sun),
    moon: pair(snapshot.moon),
    limElDeg: finite(limEl) ? limEl : null,
    keepoutDeg: finite(keepout) ? keepout : DEFAULT_SUN_KEEPOUT_DEG,
    target: null,
  }
  // Link down: the celestial scene stays, the pointer goes (mount shown at its last pose).
  if (linkStale) return base
  const mount = pair(snapshot.mount)
  base.mount = mount
  if (mount && snapshot.state === 'SLEWING') {
    const ra = snapshot.mount?.target_ra_apparent_h
    const dec = snapshot.mount?.target_dec_apparent_deg
    const lmst = snapshot.time?.lmst_hours
    const lat = snapshot.site?.lat
    if (finite(ra) && finite(dec) && finite(lmst) && finite(lat)) {
      const t = haDecToAltAz(lmst - ra, dec, lat)
      base.target = { alt: t.alt, az: wrap360(t.az) }
    }
  }
  return base
}
