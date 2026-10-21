<script setup lang="ts">
// SKY VIEWER chart (T2.1): three stacked canvases, all render-on-demand.
//   base    disc, grid, cardinals, Lim EL ring      -> resize / Lim EL change
//   stars   Hipparcos stars (alt/az from the site)   -> 1 Hz, resize, site change
//   overlay Sun, Moon, Sun keep-out, mount circle    -> when those values change
// Sky colour and star visibility follow the Sun altitude computed client-side (site + PC clock,
// so it keeps working while the link is stale).
// TEST HOOK (inert unless set): `window.__skyTestSunAlt = <deg>` overrides the Sun altitude, then
// `window.dispatchEvent(new Event('sky-test-refresh'))` re-renders at once (otherwise the 1 Hz tick
// picks it up). `delete window.__skyTestSunAlt` + the event restores the real Sun.
// Satellites (T2.2b): three more canvases (leo / meo / geo) between `stars` and `overlay`, fed by
// the sky store's chunks (utils/sky-chunks.ts). Cadence: LEO 1 s, MEO 5 s, GEO 20 s (staggered),
// plus an immediate redraw when a layer's data version changes. Dot ~2 px (scaled like the stars),
// alpha x 0.35 below Lim EL, no daylight fading, no labels. Time = skyNowMs() (honours the test
// hook `window.__skyTestClockOffsetMs`, see utils/sky-chunks.ts).
// Per-layer frame counts are exposed as data-frames-* (and drawn satellite counts as
// data-sats-leo|meo|geo) on the wrapper for the gates.
// Interaction (T2.3): every satellite draw keeps its screen positions in reusable typed arrays
// (`screen`); hover/click pick the NEAREST drawn marker within ~10 px across the visible layers
// (utils/sky-pick.ts), labels are never hit targets. A `labels` canvas (own canvas, LEO cadence)
// draws decluttered names (priority selected > hovered > GEO > MEO > LEO) and the hover highlight.
// The selected object (stores/selection.ts, keyed by NORAD) gets a white ring, the others dim to 0.22.
// TEST HOOKS: data-hover-norad / data-selected-norad on the wrapper, data-ms-labels (last label
// pass cost), data-labels-drawn, and `window.__skyPick(x, y)` -> the NORAD the picker would choose
// for chart-relative CSS px (or null). It only reads the last drawn positions; it changes nothing.
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useMountStore } from '~/stores/mount'
import { useSkyStore, type SkyState } from '~/stores/sky'
import { useSelectionStore } from '~/stores/selection'
import { BELOW_LIM_DIM, DAY_TONES, SAT_COLORS, SAT_FILL_ALPHA, SAT_FOCUS_DIM, SAT_SQUARE_PX, SKY } from '~/lib/sky/theme'
import {
  BoxGrid, ensureScreenCapacity, indexOfNorad, makePickResult, makeScreenLayer, pickNearest, type ScreenLayer,
} from '~/utils/sky-pick'
import { formatAz, formatEl, formatRange, formatTleAge, illuminationOf, ILLUMINATION_LABEL } from '~/utils/track-format'
import {
  altAzFromVectors, jdFromMs, limitingMagnitude, mixHex, projPolar, projXY, quantiseSunAlt, skyBrightness,
  skyColor, smallCircle, starAlphaFactor, starDot, sunElevationApprox, unitVectors,
  type SkySnapshotLike,
} from '~/utils/sky-chart'
import { findChunk, positionsAt, skyNowMs, type SkyLayer } from '~/utils/sky-chunks'

const props = withDefaults(defineProps<{
  showStars?: boolean
  showBodies?: boolean
  showLeo?: boolean
  showMeo?: boolean
  showGeo?: boolean
  showLabels?: boolean // false: only the hovered / selected label is drawn (T2.3b)
}>(), {
  showStars: true,
  showBodies: true,
  showLeo: true,
  showMeo: true,
  showGeo: true,
  showLabels: true,
})

const store = useMountStore()
const sky = useSkyStore()
const selection = useSelectionStore()
const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

const box = ref<HTMLElement | null>(null)
const wrap = ref<HTMLElement | null>(null)
const baseEl = ref<HTMLCanvasElement | null>(null)
const starsEl = ref<HTMLCanvasElement | null>(null)
const overlayEl = ref<HTMLCanvasElement | null>(null)
const labelsEl = ref<HTMLCanvasElement | null>(null)
const tipEl = ref<HTMLElement | null>(null)
const satEls = { leo: ref<HTMLCanvasElement | null>(null), meo: ref<HTMLCanvasElement | null>(null), geo: ref<HTMLCanvasElement | null>(null) }
const SAT_LAYERS: readonly SkyLayer[] = ['leo', 'meo', 'geo']

const size = ref(0) // chart square, css px
const dpr = ref(1)

// ---- last-known site / limits (stars keep running while the link is stale) -----------------
const site = ref<{ lat: number, lon: number } | null>(null)
const limEl = ref<number | null>(null)
watch(
  () => store.snapshot,
  (snap) => {
    const s = snap as SkySnapshotLike | null
    const lat = s?.site?.lat
    const lon = s?.site?.lon
    if (num(lat) && num(lon)) {
      if (!site.value || site.value.lat !== lat || site.value.lon !== lon) site.value = { lat, lon }
    }
    const le = s?.limits?.lim_el_deg
    limEl.value = num(le) ? le : limEl.value
  },
  { immediate: true },
)

// ---- Sun altitude (sky colour, star visibility) ----------------------------------------------
const sunAlt = ref(-90) // quantised to 0.5 deg
const skyRgb = ref<string>(skyColor(-90).join(','))

function updateSun() {
  const forced = (window as unknown as { __skyTestSunAlt?: unknown }).__skyTestSunAlt
  let a: number | null = null
  if (typeof forced === 'number' && Number.isFinite(forced)) a = forced
  else if (site.value) a = sunElevationApprox(new Date(), site.value.lat, site.value.lon)
  if (a === null) return
  const q = quantiseSunAlt(a)
  sunAlt.value = q
  skyRgb.value = skyColor(q).join(',') // only changes (and triggers a base redraw) when the colour does
}

// ---- geometry -------------------------------------------------------------------------------
function geom() {
  const S = size.value
  const margin = Math.round(Math.min(40, Math.max(18, S * 0.045)))
  return { S, cx: S / 2, cy: S / 2, R: Math.max(10, S / 2 - margin), margin }
}
const geomJson = computed(() => {
  const g = geom()
  return JSON.stringify({ cx: g.cx, cy: g.cy, R: g.R })
})
const scale = (R: number) => Math.min(1.4, Math.max(0.8, R / 340))

// ---- frame counters (non-reactive; written straight to the DOM) ----------------------------
type Layer = 'base' | 'stars' | 'overlay' | 'labels' | SkyLayer
const frames: Record<Layer, number> = { base: 0, stars: 0, overlay: 0, labels: 0, leo: 0, meo: 0, geo: 0 }
function bump(layer: Layer) {
  frames[layer] += 1
  wrap.value?.setAttribute(`data-frames-${layer}`, String(frames[layer]))
}

function prep(canvas: HTMLCanvasElement | null) {
  if (!canvas || size.value <= 0) return null
  const ctx = canvas.getContext('2d')
  if (!ctx) return null
  ctx.setTransform(dpr.value, 0, 0, dpr.value, 0, 0)
  ctx.clearRect(0, 0, size.value, size.value)
  return ctx
}

function resizeCanvases() {
  for (const c of [baseEl.value, starsEl.value, overlayEl.value, labelsEl.value, satEls.leo.value, satEls.meo.value, satEls.geo.value]) {
    if (!c) continue
    c.width = Math.max(1, Math.round(size.value * dpr.value))
    c.height = Math.max(1, Math.round(size.value * dpr.value))
  }
}

// ---- base layer -----------------------------------------------------------------------------
const CARDINALS: Array<[number, string]> = [
  [0, 'N'], [45, 'NE'], [90, 'E'], [135, 'SE'], [180, 'S'], [225, 'SW'], [270, 'W'], [315, 'NW'],
]

function drawBase() {
  const ctx = prep(baseEl.value)
  if (!ctx) return
  const { S, cx, cy, R, margin } = geom()
  const bright = skyBrightness(sunAlt.value)
  const tone = (night: string, day: string) => mixHex(night, day, bright)
  ctx.fillStyle = `rgb(${skyRgb.value})`
  ctx.beginPath()
  ctx.arc(cx, cy, R, 0, 2 * Math.PI)
  ctx.fill()

  const k = scale(R)
  // elevation rings every 15 deg (the 0 deg horizon is drawn last, heavier)
  ctx.strokeStyle = tone(SKY.ring, DAY_TONES.ring)
  ctx.lineWidth = 1.2
  for (let el = 15; el <= 75; el += 15) {
    ctx.beginPath()
    ctx.arc(cx, cy, R * (1 - el / 90), 0, 2 * Math.PI)
    ctx.stroke()
  }
  // N/S/E/W axes and diagonals
  ctx.strokeStyle = tone(SKY.axis, DAY_TONES.axis)
  ctx.lineWidth = 1
  for (const [az] of CARDINALS) {
    const p = projXY(az, 0, cx, cy, R)
    ctx.beginPath()
    ctx.moveTo(cx, cy)
    ctx.lineTo(p.x, p.y)
    ctx.stroke()
  }
  // ring labels along az 22.5 deg
  const rf = Math.round(Math.min(13, Math.max(9, 11 * k)))
  ctx.font = `500 ${rf}px ${SKY.font}`
  ctx.fillStyle = tone(SKY.ringLabel, DAY_TONES.ringLabel)
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  for (let el = 15; el <= 75; el += 15) {
    const p = projXY(22.5, el, cx, cy, R)
    ctx.fillText(`${el}°`, p.x, p.y - rf * 0.7)
  }
  // Lim EL dashed ring
  if (limEl.value !== null && limEl.value > 0 && limEl.value < 90) {
    ctx.strokeStyle = tone(SKY.limEl, DAY_TONES.limEl)
    ctx.lineWidth = 1.4
    ctx.setLineDash([6, 4])
    ctx.beginPath()
    ctx.arc(cx, cy, R * (1 - limEl.value / 90), 0, 2 * Math.PI)
    ctx.stroke()
    ctx.setLineDash([])
  }
  // horizon
  ctx.strokeStyle = tone(SKY.horizon, DAY_TONES.horizon)
  ctx.lineWidth = 2.5
  ctx.beginPath()
  ctx.arc(cx, cy, R, 0, 2 * Math.PI)
  ctx.stroke()
  // cardinals outside the rim
  const cf = Math.round(Math.min(18, Math.max(11, S * 0.02)))
  ctx.font = `700 ${cf}px ${SKY.font}`
  ctx.fillStyle = SKY.cardinal
  for (const [az, label] of CARDINALS) {
    const p = projPolar(az, R + Math.min(margin * 0.6, 20) + (label.length > 1 ? 4 : 0), cx, cy)
    ctx.fillText(label, p.x, p.y)
  }
  // no site yet: grid only + a note
  if (!site.value) {
    ctx.font = `500 ${Math.round(14 * Math.min(1.2, k))}px ${SKY.font}`
    ctx.fillStyle = SKY.note
    ctx.fillText('Waiting for gateway', cx, cy + R * 0.12)
  }
  bump('base')
}

// ---- stars ----------------------------------------------------------------------------------
let vec: Float64Array = new Float64Array(0)
let vmag: Float64Array = new Float64Array(0)
let alt: Float64Array = new Float64Array(0)
let az: Float64Array = new Float64Array(0)
let nStars = 0
const starsReady = ref(false)

function drawStars() {
  const ctx = prep(starsEl.value)
  if (!ctx) return
  updateSun()
  const limMag = limitingMagnitude(sunAlt.value)
  const dayFactor = starAlphaFactor(sunAlt.value)
  if (props.showStars && site.value && nStars > 0 && dayFactor > 0) {
    const { cx, cy, R } = geom()
    altAzFromVectors(vec, nStars, jdFromMs(Date.now()), site.value.lat, site.value.lon, alt, az)
    const lim = limEl.value !== null && limEl.value > 0 ? limEl.value : 0
    const k = scale(R)
    ctx.fillStyle = SKY.star
    // faint first so bright stars end up on top; catalogue is sorted bright -> faint
    for (let i = nStars - 1; i >= 0; i--) {
      if (vmag[i]! > limMag) continue
      const a = alt[i]!
      if (a < 0) continue
      const rr = R * (1 - a / 90)
      const ang = (az[i]! * Math.PI) / 180
      const x = cx - rr * Math.sin(ang)
      const y = cy - rr * Math.cos(ang)
      const d = starDot(vmag[i]!)
      ctx.globalAlpha = (a < lim ? d.alpha * BELOW_LIM_DIM : d.alpha) * dayFactor
      ctx.beginPath()
      ctx.arc(x, y, d.radius * k, 0, 2 * Math.PI)
      ctx.fill()
    }
    ctx.globalAlpha = 1
  }
  bump('stars')
}

async function loadStars() {
  try {
    const base = useRuntimeConfig().app.baseURL || '/'
    const res = await fetch(`${base}data/stars.json`)
    const doc = await res.json()
    const rows: number[][] = doc.stars
    const n = rows.length
    const ra = new Float64Array(n)
    const dec = new Float64Array(n)
    vmag = new Float64Array(n)
    for (let i = 0; i < n; i++) {
      ra[i] = rows[i]![0]!
      dec[i] = rows[i]![1]!
      vmag[i] = rows[i]![2]!
    }
    vec = unitVectors(ra, dec)
    alt = new Float64Array(n)
    az = new Float64Array(n)
    nStars = n
    starsReady.value = true
  }
  catch (e) {
    console.warn('sky: could not load stars.json', e)
  }
}

// ---- satellites -----------------------------------------------------------------------------
// Reused output buffers (grown only when a chunk has more objects than before): no per-frame allocation.
let satAz = new Float32Array(0)
let satAlt = new Float32Array(0)
let satIdx = new Int32Array(0)
const SAT_SHOW: Record<SkyLayer, () => boolean> = {
  leo: () => props.showLeo,
  meo: () => props.showMeo,
  geo: () => props.showGeo,
}

// Screen positions of the markers each layer drew last (picking, labels, tooltip).
const screen: Record<SkyLayer, ScreenLayer> = { leo: makeScreenLayer(), meo: makeScreenLayer(), geo: makeScreenLayer() }

function drawSats(layer: SkyLayer) {
  const t0 = performance.now()
  const ctx = prep(satEls[layer].value)
  if (!ctx) return
  const sc = screen[layer]
  sc.n = 0
  let drawn = 0
  const selN = selection.selectedNorad
  const focus = selN !== null
  if (SAT_SHOW[layer]() && site.value) {
    const t = skyNowMs() / 1000
    const chunk = findChunk(sky.chunks(layer), t)
    if (chunk) {
      if (satAz.length < chunk.nObj) {
        satAz = new Float32Array(chunk.nObj)
        satAlt = new Float32Array(chunk.nObj)
        satIdx = new Int32Array(chunk.nObj)
      }
      const n = positionsAt(chunk, t, satAz, satAlt, satIdx)
      ensureScreenCapacity(sc, n)
      const { cx, cy, R } = geom()
      // Small squares (Main Dashboard 2.png), ~5 px at the reference size, scaled like the stars.
      // Class-colour fill + an outline that contrasts with the sky: a lighter tint on the dark
      // night disc, a darker shade on the light day disc.
      const k = scale(R)
      const side = SAT_SQUARE_PX * k
      const h = side / 2
      const lim = limEl.value !== null && limEl.value > 0 ? limEl.value : 0
      const color = SAT_COLORS[layer]
      const dayDisc = skyBrightness(sunAlt.value) > 0.5
      const outline = dayDisc ? mixHex(color, '#000000', 0.45) : mixHex(color, '#ffffff', 0.4)
      // pass 1: screen positions of everything above the horizon (also feeds picking / labels)
      let selK = -1
      let m = 0
      for (let i = 0; i < n; i++) {
        const a = satAlt[i]!
        if (a < 0) continue
        const rr = R * (1 - a / 90)
        const ang = (satAz[i]! * Math.PI) / 180
        const nr = chunk.norad[satIdx[i]!]!
        sc.x[m] = cx - rr * Math.sin(ang)
        sc.y[m] = cy - rr * Math.cos(ang)
        sc.az[m] = satAz[i]!
        sc.alt[m] = a
        sc.norad[m] = nr
        if (nr === selN) selK = m
        m++
      }
      sc.n = m
      drawn = m
      ctx.fillStyle = color
      ctx.strokeStyle = outline
      ctx.lineWidth = 1
      // pass 2: two batched paths, full alpha and dimmed below Lim EL (everything dims to 0.22
      // while another object is selected)
      for (let pass = 0; pass < 2; pass++) {
        const dim = pass === 1
        const alpha = focus ? SAT_FOCUS_DIM : dim ? BELOW_LIM_DIM : 1
        ctx.beginPath()
        let any = false
        for (let i = 0; i < m; i++) {
          if ((sc.alt[i]! < lim) !== dim) continue
          ctx.rect(sc.x[i]! - h, sc.y[i]! - h, side, side)
          any = true
        }
        if (any) {
          ctx.globalAlpha = alpha * SAT_FILL_ALPHA
          ctx.fill()
          ctx.globalAlpha = alpha
          ctx.stroke()
        }
      }
      // the selected object: full brightness on top, plus the white ring
      if (selK >= 0) {
        const x = sc.x[selK]!
        const y = sc.y[selK]!
        ctx.beginPath()
        ctx.rect(x - h, y - h, side, side)
        ctx.globalAlpha = SAT_FILL_ALPHA
        ctx.fill()
        ctx.globalAlpha = 1
        ctx.stroke()
        ctx.strokeStyle = SKY.selectRing
        ctx.lineWidth = 1.6
        ctx.beginPath()
        ctx.arc(x, y, Math.max(8, side * 1.05), 0, 2 * Math.PI)
        ctx.stroke()
      }
      ctx.globalAlpha = 1
    }
  }
  wrap.value?.setAttribute(`data-sats-${layer}`, String(drawn))
  wrap.value?.setAttribute(`data-ms-${layer}`, (performance.now() - t0).toFixed(2)) // gate hook: last draw cost
  bump(layer)
}

// ---- labels (decluttered names + hover highlight) --------------------------------------------
const grid = new BoxGrid(32)
const labelWidths = new Map<number, number>() // NORAD -> text width at `labelFont` (measured once)
let labelFont = ''
const PICK_ORDER: readonly SkyLayer[] = ['geo', 'meo', 'leo'] // label priority after selected/hovered
const LABEL_PRIORITY: readonly SkyLayer[] = PICK_ORDER
const MAX_MEASURE_PER_PASS = 120

function findOnScreen(norad: number | null): { layer: SkyLayer, i: number } | null {
  if (norad === null) return null
  for (const l of SAT_LAYERS) {
    const i = indexOfNorad(screen[l], norad)
    if (i >= 0) return { layer: l, i }
  }
  return null
}

function drawLabels() {
  const t0 = performance.now()
  const ctx = prep(labelsEl.value)
  if (!ctx) return
  let placed = 0
  let measured = 0
  if (site.value) {
    const { S, cx, cy, R } = geom()
    const k = scale(R)
    const fs = Math.max(9, Math.round(9 * k * 10) / 10)
    const h = (SAT_SQUARE_PX * k) / 2 + 1
    const font = `700 ${fs}px ${SKY.font}`
    ctx.font = font
    if (font !== labelFont) {
      labelFont = font
      labelWidths.clear()
    }
    ctx.textAlign = 'left'
    ctx.textBaseline = 'middle'
    ctx.lineJoin = 'round'
    ctx.lineWidth = 2.5
    ctx.strokeStyle = 'rgba(0,0,0,0.85)'
    ctx.fillStyle = '#ffffff'
    grid.reset(S, S)
    // every drawn marker occupies its box
    for (const l of SAT_LAYERS) {
      const sc = screen[l]
      for (let i = 0; i < sc.n; i++) grid.add(sc.x[i]! - h, sc.y[i]! - h, sc.x[i]! + h, sc.y[i]! + h)
    }
    const halfH = fs / 2 + 1
    const rim2 = (R + 6) * (R + 6)
    const place = (norad: number, x: number, y: number, force: boolean): void => {
      const name = sky.meta.get(norad)?.name
      if (!name) return
      let w = labelWidths.get(norad)
      if (w === undefined) {
        // Width is measured once per object; cap the measurements per pass so the first pass after
        // the names arrive stays cheap (the rest fill in over the next 100 ms frames).
        if (!force && measured >= MAX_MEASURE_PER_PASS) return
        measured++
        w = ctx.measureText(name).width
        labelWidths.set(norad, w)
      }
      const x0 = x + h + 2
      const x1 = x0 + w
      const y0 = y - halfH
      const y1 = y + halfH
      if (!force) {
        const dx = x1 - cx
        const dy = y - cy
        if (dx * dx + dy * dy > rim2 || x1 > S) return
        if (grid.overlaps(x0, y0, x1, y1)) return
      }
      grid.add(x0, y0, x1, y1)
      ctx.strokeText(name, x0, y)
      ctx.fillText(name, x0, y)
      placed++
    }
    const selN = selection.selectedNorad
    const hovN = hoverNorad.value
    const sel = findOnScreen(selN)
    const hov = hovN !== selN ? findOnScreen(hovN) : null
    // selected first, then hovered (both always drawn), then GEO > MEO > LEO at the focus alpha
    if (sel) place(selN!, screen[sel.layer].x[sel.i]!, screen[sel.layer].y[sel.i]!, true)
    if (hov) place(hovN!, screen[hov.layer].x[hov.i]!, screen[hov.layer].y[hov.i]!, true)
    ctx.globalAlpha = selN !== null ? SAT_FOCUS_DIM : 1
    for (const l of props.showLabels ? LABEL_PRIORITY : []) {
      const sc = screen[l]
      for (let i = 0; i < sc.n; i++) {
        const nr = sc.norad[i]!
        if (nr === selN || nr === hovN) continue
        place(nr, sc.x[i]!, sc.y[i]!, false)
      }
    }
    ctx.globalAlpha = 1
    // hover highlight (ssa `.ov-sat:hover rect`: white 1.5 px outline)
    const hh = findOnScreen(hovN)
    if (hh) {
      const sc = screen[hh.layer]
      ctx.strokeStyle = '#ffffff'
      ctx.lineWidth = 1.5
      ctx.strokeRect(sc.x[hh.i]! - h, sc.y[hh.i]! - h, 2 * h, 2 * h)
      refreshTip(sc.az[hh.i]!, sc.alt[hh.i]!)
    }
  }
  wrap.value?.setAttribute('data-labels-drawn', String(placed))
  wrap.value?.setAttribute('data-ms-labels', (performance.now() - t0).toFixed(2))
  bump('labels')
}

// ---- hover / pick / select ---------------------------------------------------------------------
const hoverNorad = ref<number | null>(null)
const pickRes = makePickResult()
const PICK_LAYERS: ScreenLayer[] = [screen.geo, screen.meo, screen.leo]
const PICK_RADIUS_PX = 10
const tip = reactive({ visible: false, x: 0, y: 0, norad: 0, az: NaN, alt: NaN, state: null as SkyState | null })
let hoverTimer: ReturnType<typeof setTimeout> | null = null
let hoverPoll: ReturnType<typeof setInterval> | null = null

/** Nearest drawn marker to chart-relative CSS px (x, y), or null. Labels are not targets. */
function pickAt(x: number, y: number): number | null {
  const r = PICK_RADIUS_PX * scale(geom().R)
  return pickNearest(PICK_LAYERS, x, y, r, pickRes) ? pickRes.norad : null
}

function toLocal(e: PointerEvent | MouseEvent) {
  const r = wrap.value!.getBoundingClientRect()
  return { x: e.clientX - r.left, y: e.clientY - r.top, w: r.width, h: r.height }
}

function refreshTip(az: number, alt: number) {
  if (!tip.visible) return
  if (Math.abs(az - tip.az) > 0.004 || Math.abs(alt - tip.alt) > 0.004 || Number.isNaN(tip.az)) {
    tip.az = az
    tip.alt = alt
  }
}

function pollHoverState() {
  const n = hoverNorad.value
  if (n === null) return
  void sky.fetchState(n).then((s) => {
    if (hoverNorad.value === n && s) tip.state = s
  })
}

function setHover(n: number | null) {
  if (hoverNorad.value === n) return
  hoverNorad.value = n
  tip.state = null
  tip.az = NaN
  tip.alt = NaN
  tip.norad = n ?? 0
  tip.visible = n !== null
  if (hoverTimer) clearTimeout(hoverTimer)
  hoverTimer = null
  if (n !== null) hoverTimer = setTimeout(pollHoverState, 100) // skip the fetch while sweeping across markers
  schedule('labels')
}

function onPointerMove(e: PointerEvent) {
  if (!wrap.value) return
  const p = toLocal(e)
  setHover(pickAt(p.x, p.y))
  if (tip.visible) {
    const tw = tipEl.value?.offsetWidth ?? 0
    const th = tipEl.value?.offsetHeight ?? 0
    let x = p.x + 14
    let y = p.y + 14
    if (x + tw > p.w) x = p.x - 14 - tw
    if (x < 0) x = 0
    if (y + th > p.h) y = p.y - 14 - th
    if (y < 0) y = 0
    tip.x = x
    tip.y = y
  }
}

function onPointerLeave() {
  setHover(null)
}

function onClick(e: MouseEvent) {
  if (!wrap.value) return
  const p = toLocal(e)
  const n = pickAt(p.x, p.y)
  if (n === null) selection.deselect()
  else selection.select(n)
}

const tipLines = computed(() => {
  const st = tip.state
  const meta = sky.meta.get(tip.norad)
  void sky.metaVersion
  const alt = Number.isNaN(tip.alt) ? st?.alt : tip.alt
  const ill = illuminationOf(alt, st?.illuminated)
  return {
    name: meta?.name ?? st?.name ?? `NORAD ${tip.norad}`,
    az: Number.isNaN(tip.az) ? '—' : formatAz(tip.az),
    el: Number.isNaN(tip.alt) ? '—' : formatEl(tip.alt),
    range: st ? formatRange(st.range_km) : '…',
    cls: meta?.orbit_class ?? st?.orbit_class ?? '',
    ill,
    illLabel: st ? ILLUMINATION_LABEL[ill] : '…',
    age: st ? formatTleAge(st.tle_age_days) : '…',
  }
})

// ---- overlay --------------------------------------------------------------------------------
interface Body { alt: number, az: number }

const overlayState = computed(() => {
  const s = store.snapshot as SkySnapshotLike | null
  const stale = store.linkStale
  // Quantised so sub-pixel drift (Sun/Moon creep, mount tracking jitter) does not redraw.
  const q = (v: unknown, step: number) => (num(v) ? Math.round(v / step) * step : null)
  const body = (b?: { alt?: number | null, az?: number | null } | null, step = 0.05): Body | null => {
    const a = q(b?.alt, step)
    const z = q(b?.az, step)
    return a === null || z === null ? null : { alt: a, az: z }
  }
  return {
    stale,
    sun: body(s?.sun),
    moon: body(s?.moon),
    illum: q(s?.moon?.illumination, 0.01),
    keepout: num(s?.limits?.sun_keepout_deg) ? s.limits.sun_keepout_deg : 15,
    mount: s?.mount?.connected === false ? null : body(s?.mount, 0.01),
  }
})
const overlayKey = computed(() => JSON.stringify(overlayState.value) + String(props.showBodies))

function drawMoon(ctx: CanvasRenderingContext2D, x: number, y: number, r: number, illum: number, towardSun: number) {
  ctx.save()
  ctx.translate(x, y)
  ctx.rotate(towardSun)
  ctx.fillStyle = SKY.moonDark
  ctx.beginPath()
  ctx.arc(0, 0, r, 0, 2 * Math.PI)
  ctx.fill()
  const f = Math.max(0, Math.min(1, illum))
  if (f > 0.01) {
    const kk = 1 - 2 * f
    ctx.fillStyle = SKY.moonLit
    ctx.beginPath()
    ctx.arc(0, 0, r, -Math.PI / 2, Math.PI / 2, false)
    ctx.ellipse(0, 0, Math.abs(kk) * r, r, 0, Math.PI / 2, -Math.PI / 2, kk > 0)
    ctx.closePath()
    ctx.fill()
  }
  ctx.restore()
}

function drawOverlay() {
  const ctx = prep(overlayEl.value)
  if (!ctx) return
  const o = overlayState.value
  const { cx, cy, R } = geom()
  const k = scale(R)
  if (site.value && !o.stale) {
    ctx.save()
    ctx.beginPath()
    ctx.arc(cx, cy, R + 1, 0, 2 * Math.PI)
    ctx.clip()
    if (props.showBodies) {
      if (o.sun && o.sun.alt > -o.keepout) {
        ctx.strokeStyle = SKY.keepout
        ctx.lineWidth = 1.2
        ctx.setLineDash([5, 4])
        ctx.beginPath()
        smallCircle(o.sun.az, o.sun.alt, o.keepout).forEach((p, i) => {
          const q = projXY(p.az, p.alt, cx, cy, R)
          if (i === 0) ctx.moveTo(q.x, q.y)
          else ctx.lineTo(q.x, q.y)
        })
        ctx.closePath()
        ctx.stroke()
        ctx.setLineDash([])
      }
      if (o.sun && o.sun.alt >= 0) {
        const p = projXY(o.sun.az, o.sun.alt, cx, cy, R)
        ctx.fillStyle = SKY.sunGlow
        ctx.beginPath()
        ctx.arc(p.x, p.y, 11 * k, 0, 2 * Math.PI)
        ctx.fill()
        ctx.fillStyle = SKY.sun
        ctx.beginPath()
        ctx.arc(p.x, p.y, 6.5 * k, 0, 2 * Math.PI)
        ctx.fill()
      }
      if (o.moon && o.moon.alt >= 0) {
        const p = projXY(o.moon.az, o.moon.alt, cx, cy, R)
        let toward = 0
        if (o.sun) {
          const s = projXY(o.sun.az, o.sun.alt, cx, cy, R)
          toward = Math.atan2(s.y - p.y, s.x - p.x)
        }
        drawMoon(ctx, p.x, p.y, 6.5 * k, o.illum ?? 1, toward)
      }
    }
    if (o.mount) {
      const p = projXY(o.mount.az, o.mount.alt, cx, cy, R)
      // broken ring: 4 arcs, gaps at N/E/S/W, no centre dot (the tracked target stays visible)
      ctx.strokeStyle = SKY.pointer
      ctx.lineWidth = 2.6
      ctx.lineCap = 'butt'
      const gap = (16 * Math.PI) / 180
      for (let i = 0; i < 4; i++) {
        ctx.beginPath()
        ctx.arc(p.x, p.y, 9 * k, i * (Math.PI / 2) + gap, (i + 1) * (Math.PI / 2) - gap)
        ctx.stroke()
      }
    }
    ctx.restore()
  }
  bump('overlay')
}

// ---- scheduling (coalesced through rAF) -----------------------------------------------------
const dirty: Record<Layer, boolean> = { base: false, stars: false, overlay: false, labels: false, leo: false, meo: false, geo: false }
let raf = 0
function flush() {
  raf = 0
  if (dirty.base) { dirty.base = false; drawBase() }
  if (dirty.stars) { dirty.stars = false; drawStars() }
  for (const l of SAT_LAYERS) {
    if (dirty[l]) { dirty[l] = false; drawSats(l) }
  }
  if (dirty.labels) { dirty.labels = false; drawLabels() }
  if (dirty.overlay) { dirty.overlay = false; drawOverlay() }
}
function schedule(...layers: Layer[]) {
  for (const l of layers) dirty[l] = true
  if (!raf) raf = requestAnimationFrame(flush)
}

watch(limEl, () => schedule('base', 'labels', ...SAT_LAYERS))
watch(() => props.showLabels, () => schedule('labels'))
watch(skyRgb, () => schedule('base'))
watch(site, () => { updateSun(); schedule('base', 'stars', 'overlay', 'labels', ...SAT_LAYERS) })
watch(overlayKey, () => schedule('overlay'))
watch(() => props.showStars, () => schedule('stars'))
watch(starsReady, () => schedule('stars'))
// New data for a layer (chunk landed / flushed on a site change) draws at once, whatever the cadence.
for (const l of SAT_LAYERS) {
  watch(() => sky.versions[l], () => schedule(l))
  watch(() => SAT_SHOW[l](), () => schedule(l, 'labels'))
}
// selection changes re-dim every layer and move the ring / label; names arriving draw labels
watch(() => selection.selectedNorad, () => schedule('labels', ...SAT_LAYERS))
watch(() => sky.metaVersion, () => schedule('labels'))

// ---- lifecycle ------------------------------------------------------------------------------
let ro: ResizeObserver | null = null
let timer: ReturnType<typeof setInterval> | null = null
const satTimers: Array<ReturnType<typeof setTimeout>> = []
// [cadence ms, start offset ms]: staggered so the layers do not land on the same frame.
// LEO at 10 Hz: a fast mover near the zenith (~0.5 deg/s) then steps ~0.25 px per frame at 1920
// instead of ~2.4 px at 1 Hz. GEO (incl. HEO) at 10 s (user, 10-21).
// Positions are interpolated between the chunk samples at every draw.
const SAT_CADENCE: Record<SkyLayer, [number, number]> = { leo: [100, 25], meo: [5000, 50], geo: [10000, 75] }

function measure() {
  const el = box.value
  if (!el) return
  const s = Math.floor(Math.min(el.clientWidth, el.clientHeight))
  const d = window.devicePixelRatio || 1
  if (s === size.value && d === dpr.value) return
  size.value = Math.max(0, s)
  dpr.value = d
  resizeCanvases() // resetting width/height clears the canvases
  setHover(null) // the pointer position is stale after a resize
  schedule('base', 'stars', 'overlay', 'labels', ...SAT_LAYERS)
}

function onTestRefresh() {
  updateSun()
  schedule('base', 'stars', 'overlay')
}

onMounted(() => {
  measure()
  ro = new ResizeObserver(measure)
  if (box.value) ro.observe(box.value)
  timer = setInterval(() => schedule('stars'), 1000)
  for (const l of SAT_LAYERS) {
    const [every, offset] = SAT_CADENCE[l]
    satTimers.push(setTimeout(() => {
      schedule(l)
      // labels follow the LEO cadence (they use the markers' last drawn positions)
      const tick = l === 'leo' ? () => schedule(l, 'labels') : () => schedule(l)
      satTimers.push(setInterval(tick, every) as unknown as ReturnType<typeof setTimeout>)
    }, offset))
  }
  sky.start()
  hoverPoll = setInterval(pollHoverState, 1000)
  // test hook: `__skyPick(x, y)` plus `__skyPick.positions(layer)` -> [[x, y, norad, az, alt], ...]
  ;(window as unknown as { __skyPick?: unknown }).__skyPick = Object.assign(
    (x: number, y: number) => pickAt(x, y),
    {
      positions: (layer: SkyLayer) => {
        const sc = screen[layer]
        const out: number[][] = []
        for (let i = 0; i < sc.n; i++) out.push([sc.x[i]!, sc.y[i]!, sc.norad[i]!, sc.az[i]!, sc.alt[i]!])
        return out
      },
    },
  )
  window.addEventListener('sky-test-refresh', onTestRefresh)
  updateSun()
  loadStars()
  document.fonts?.ready.then(() => schedule('base'))
})

onBeforeUnmount(() => {
  delete (window as unknown as { __skyPick?: unknown }).__skyPick
  if (hoverPoll) clearInterval(hoverPoll)
  if (hoverTimer) clearTimeout(hoverTimer)
  window.removeEventListener('sky-test-refresh', onTestRefresh)
  if (timer) clearInterval(timer)
  timer = null
  for (const t of satTimers) {
    clearTimeout(t)
    clearInterval(t)
  }
  satTimers.length = 0
  ro?.disconnect()
  ro = null
  if (raf) cancelAnimationFrame(raf)
  raf = 0
})
</script>

<template>
  <div ref="box" class="relative h-full min-h-0 w-full">
    <div
      ref="wrap"
      class="absolute"
      data-testid="sky-chart"
      :data-geom="geomJson"
      data-frames-base="0"
      data-frames-stars="0"
      data-frames-overlay="0"
      data-frames-leo="0"
      data-frames-meo="0"
      data-frames-geo="0"
      data-sats-leo="0"
      data-sats-meo="0"
      data-sats-geo="0"
      data-frames-labels="0"
      :data-hover-norad="hoverNorad ?? ''"
      :data-selected-norad="selection.selectedNorad ?? ''"
      :style="{ width: `${size}px`, height: `${size}px`, left: '50%', top: '50%', transform: 'translate(-50%, -50%)', cursor: hoverNorad !== null ? 'pointer' : 'default' }"
      @pointermove="onPointerMove"
      @pointerleave="onPointerLeave"
      @click="onClick"
    >
      <canvas
        ref="baseEl"
        class="absolute inset-0"
        :style="{ width: `${size}px`, height: `${size}px` }"
        data-testid="sky-canvas-base"
      />
      <canvas
        ref="starsEl"
        class="absolute inset-0"
        :style="{ width: `${size}px`, height: `${size}px` }"
        data-testid="sky-canvas-stars"
      />
      <canvas
        v-for="l in SAT_LAYERS"
        :key="l"
        :ref="(el) => { satEls[l].value = el as HTMLCanvasElement | null }"
        class="absolute inset-0"
        :style="{ width: `${size}px`, height: `${size}px` }"
        :data-testid="`sky-canvas-${l}`"
      />
      <canvas
        ref="labelsEl"
        class="pointer-events-none absolute inset-0"
        :style="{ width: `${size}px`, height: `${size}px` }"
        data-testid="sky-canvas-labels"
      />
      <canvas
        ref="overlayEl"
        class="absolute inset-0"
        :style="{ width: `${size}px`, height: `${size}px` }"
        data-testid="sky-canvas-overlay"
      />
      <div
        v-if="tip.visible"
        ref="tipEl"
        class="sky-tip"
        data-testid="sky-tip"
        :style="{ left: `${tip.x}px`, top: `${tip.y}px` }"
      >
        <b>{{ tipLines.name }}</b> ({{ tip.norad }})<br>
        Az {{ tipLines.az }} El {{ tipLines.el }} | {{ tipLines.range }} | {{ tipLines.cls }}
        | <i class="tip-dot" :class="tipLines.ill" />{{ tipLines.illLabel }}<br>
        TLE age {{ tipLines.age }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.sky-tip {
  position: absolute; z-index: 5; pointer-events: none; white-space: nowrap;
  background: rgba(11, 18, 28, 0.87); border: 1px solid var(--panel-border); border-radius: 4px;
  padding: 4px 7px; font-size: 11px; line-height: 1.35; color: #cfe0f0;
}
.tip-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; background: #6a7a88; }
.tip-dot.illuminated { background: #ffd24a; }
</style>
