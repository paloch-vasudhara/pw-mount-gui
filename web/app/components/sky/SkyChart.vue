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
// Per-layer frame counts are exposed as data-frames-* on the wrapper for the gates.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useMountStore } from '~/stores/mount'
import { BELOW_LIM_DIM, DAY_TONES, SKY } from '~/lib/sky/theme'
import {
  altAzFromVectors, jdFromMs, limitingMagnitude, mixHex, projPolar, projXY, quantiseSunAlt, skyBrightness,
  skyColor, smallCircle, starAlphaFactor, starDot, sunElevationApprox, unitVectors,
  type SkySnapshotLike,
} from '~/utils/sky-chart'

const props = withDefaults(defineProps<{ showStars?: boolean, showBodies?: boolean }>(), {
  showStars: true,
  showBodies: true,
})

const store = useMountStore()
const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

const box = ref<HTMLElement | null>(null)
const wrap = ref<HTMLElement | null>(null)
const baseEl = ref<HTMLCanvasElement | null>(null)
const starsEl = ref<HTMLCanvasElement | null>(null)
const overlayEl = ref<HTMLCanvasElement | null>(null)

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
const frames = { base: 0, stars: 0, overlay: 0 }
function bump(layer: 'base' | 'stars' | 'overlay') {
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
  for (const c of [baseEl.value, starsEl.value, overlayEl.value]) {
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
const dirty = { base: false, stars: false, overlay: false }
let raf = 0
function flush() {
  raf = 0
  if (dirty.base) { dirty.base = false; drawBase() }
  if (dirty.stars) { dirty.stars = false; drawStars() }
  if (dirty.overlay) { dirty.overlay = false; drawOverlay() }
}
function schedule(...layers: Array<'base' | 'stars' | 'overlay'>) {
  for (const l of layers) dirty[l] = true
  if (!raf) raf = requestAnimationFrame(flush)
}

watch(limEl, () => schedule('base'))
watch(skyRgb, () => schedule('base'))
watch(site, () => { updateSun(); schedule('base', 'stars', 'overlay') })
watch(overlayKey, () => schedule('overlay'))
watch(() => props.showStars, () => schedule('stars'))
watch(starsReady, () => schedule('stars'))

// ---- lifecycle ------------------------------------------------------------------------------
let ro: ResizeObserver | null = null
let timer: ReturnType<typeof setInterval> | null = null

function measure() {
  const el = box.value
  if (!el) return
  const s = Math.floor(Math.min(el.clientWidth, el.clientHeight))
  const d = window.devicePixelRatio || 1
  if (s === size.value && d === dpr.value) return
  size.value = Math.max(0, s)
  dpr.value = d
  resizeCanvases() // resetting width/height clears the canvases
  schedule('base', 'stars', 'overlay')
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
  window.addEventListener('sky-test-refresh', onTestRefresh)
  updateSun()
  loadStars()
  document.fonts?.ready.then(() => schedule('base'))
})

onBeforeUnmount(() => {
  window.removeEventListener('sky-test-refresh', onTestRefresh)
  if (timer) clearInterval(timer)
  timer = null
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
      :style="{ width: `${size}px`, height: `${size}px`, left: '50%', top: '50%', transform: 'translate(-50%, -50%)' }"
      data-testid="sky-chart"
      :data-geom="geomJson"
      data-frames-base="0"
      data-frames-stars="0"
      data-frames-overlay="0"
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
        ref="overlayEl"
        class="absolute inset-0"
        :style="{ width: `${size}px`, height: `${size}px` }"
        data-testid="sky-canvas-overlay"
      />
    </div>
  </div>
</template>
