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
  note: '#6f8796',
  font: 'Barlow, ui-sans-serif, system-ui, sans-serif',
} as const

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
