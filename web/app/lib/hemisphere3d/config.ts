// Central constants for the 3D hemisphere view (ported from scope-pointer/src/config.js).
// Scene units are inches (taken from the L-350 plan drawing).

export const ALT_AXIS_HEIGHT = 33.5 // drawing: alt axis centre above base
export const DOME_RADIUS = 110 // observer hemisphere radius (~3.3x axis height)

/** Used until the gateway reports `limits.sun_keepout_deg` (prototype used 10 deg; the app uses 15). */
export const DEFAULT_SUN_KEEPOUT_DEG = 15

// Display smoothing applied to *reported* positions (10 Hz reports).
export const DISPLAY_TAU = 0.08 // seconds

// Far-side depth cue for dome bodies (camera outside the dome): brightness of the body
// and of its additive glow at full far side; blended smoothly across the dome limb.
export const FAR_SIDE_DIM = { body: 0.45, glow: 0.25 }

export const COLORS = {
  scope: 0x5cc8ff,
  scopeTarget: 0x9fe0ff,
  danger: 0xff4d4d,
  sun: 0xffc94a,
  moonLit: 0xe9e6dc,
  moonDark: 0x1c2130,
  grid: 0x9fb4d9,
  horizon: 0xc8d6f0,
  ground: 0x1d2420,
  belowHorizon: 0x2a2f2a,
  sky: 0x0d1b33,
  /** Lim EL ring (new in the app; amber, distinct from the red keep-out). */
  limEl: 0xe6b84f,
}
