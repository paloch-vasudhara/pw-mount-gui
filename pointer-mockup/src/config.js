// Central constants. Scene units are inches (taken from the L-350 plan drawing).

export const SITE = { latDeg: 13, lonDeg: 100 };

export const ALT_AXIS_HEIGHT = 33.5;   // drawing: alt axis centre above base
export const DOME_RADIUS = 110;        // observer hemisphere radius (~3.3x axis height)

export const SUN_KEEPOUT_DEG = 10;

// Declination extremes the sun/moon sliders are bounded to.
export const SUN_DEC_MAX = 23.44;                 // obliquity of the ecliptic
export const MOON_DEC_MAX = 23.44 + 5.145;        // obliquity + lunar orbit inclination (~28.6°)

// Simulated mount kinematics (stand-in for PWI4-reported motion).
export const SLEW_PRESETS = { slow: 5, normal: 12, fast: 30 };   // deg/s per axis
export const SLEW_ACCEL = 10;                                    // deg/s² per axis

// Display smoothing applied to *reported* positions (meaningful once PWI4 polls at a few Hz).
export const DISPLAY_TAU = 0.08;  // seconds

// Far-side depth cue for dome bodies (camera outside the dome): brightness of the body
// and of its additive glow at full far side; blended smoothly across the dome limb.
export const FAR_SIDE_DIM = { body: 0.45, glow: 0.25 };

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
};
