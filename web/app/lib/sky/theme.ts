// Sky chart colours in one place (matched to concept/Main Dashboard 1.png).
// Night tones (sky disc at Sun <= -12 deg); see DAY_TONES for the light-sky counterparts.
export const SKY = {
  disc: '#0a0e17',
  horizon: '#8d9195',
  ring: '#3a4753',
  axis: '#323f4a',
  ringLabel: '#56636d',
  cardinal: '#7c8084',
  star: '#dcdde4',
  limEl: '#b88a3e',
  sun: '#f0b030',
  sunGlow: 'rgba(240,176,48,0.28)',
  keepout: '#e05252',
  moonDark: '#3b4048',
  moonLit: '#cfd3da',
  pointer: '#4aa3ff',
  selectRing: '#ffffff',
  note: '#6f8796',
  font: 'Barlow, ui-sans-serif, system-ui, sans-serif',
} as const

/**
 * Satellite layer colours: ssa-toolkit `ORBIT_CLASS_COLOR` (utils/orbit-class.ts @84f2ded).
 * LEO layer = LEO + SSO objects (ssa: LEO 4d9955 muted green; SSO 3d8f8f teal, a LEO sub-type, so
 * the layer takes the LEO green); MEO 3a6bab muted blue; GEO layer = GEO + HEO (ssa: GEO 7a4faa
 * muted purple; HEO a07830 amber, folded into the GEO layer, which takes the GEO purple).
 */
export const SAT_COLORS = { leo: '#4d9955', meo: '#3a6bab', geo: '#7a4faa' } as const

/** Satellite marker: square side (css px at the reference chart size) and fill alpha (outline is opaque). */
export const SAT_SQUARE_PX = 5
export const SAT_FILL_ALPHA = 0.85

/** While an object is selected every other satellite is dimmed to this alpha (ssa `.ov-sat.dim`). */
export const SAT_FOCUS_DIM = 0.22

/** Orbit-class chip colours (ssa-toolkit ORBIT_CLASS_COLOR, utils/orbit-class.ts @84f2ded). */
export const SAT_CLASS_COLORS: Record<string, string> = {
  LEO: '#4d9955', SSO: '#3d8f8f', MEO: '#3a6bab', GEO: '#7a4faa', HEO: '#a07830', UNK: '#888888',
}

/** Stars between the horizon and Lim EL are dimmed by this factor. */
export const BELOW_LIM_DIM = 0.35

/**
 * Sky disc colour by Sun altitude: [sun alt deg, RGB], highest first. Piecewise linear.
 * ssa-toolkit skyColor stops (observer-hemisphere.ts @84f2ded): 5 / 0 / -6 / -12, with the day end
 * made bluer (T2.1b): [170,205,245] from +20 deg and [205,225,250] at +5 deg. Night = SKY.disc.
 */
export const SKY_STOPS: ReadonlyArray<{ alt: number, rgb: readonly number[] }> = [
  { alt: 20, rgb: [170, 205, 245] },
  { alt: 5, rgb: [205, 225, 250] },
  { alt: 0, rgb: [220, 140, 100] },
  { alt: -6, rgb: [40, 30, 70] },
  { alt: -12, rgb: [10, 14, 23] },
  { alt: -90, rgb: [10, 14, 23] },
]

/**
 * Grid tones on a fully bright sky (SKY.* tones are the night ones). The chart interpolates night
 * -> day by sky brightness so lines stay readable on a light disc.
 */
export const DAY_TONES = {
  horizon: '#3d4a58',
  ring: '#5d7893',
  axis: '#7c93ab',
  ringLabel: '#2f4358',
  limEl: '#8a5a0a',
} as const

/** Star limiting magnitude vs Sun altitude: [sun alt deg, limiting V], highest first. */
export const LIM_MAG_STOPS: ReadonlyArray<readonly [number, number]> = [
  [5, -3], [0, 1], [-6, 4], [-18, 6.5],
]

/** Star alpha factor vs Sun altitude (sky brightness washes stars out). */
export const STAR_ALPHA_STOPS: ReadonlyArray<readonly [number, number]> = [
  [5, 0], [0, 0.3], [-12, 1],
]
