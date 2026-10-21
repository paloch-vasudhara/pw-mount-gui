// Satellite chunk store (T2.2b): owns the fetching for all three layers (hidden ones too, so a
// layer toggled on is already populated). The pure policy lives in utils/sky-chunks.ts; this store
// wires it to fetch(), the mount snapshot's site, and a version counter per layer that the chart
// watches to redraw as soon as new data lands.
//
// Test/diagnostic hook: `window.__skyStats` = { events: [...] } records every chunk request
// (layer, kind, t0, duration, fetch/decode ms, bytes) so gates can read timings and watch the
// next-chunk fetch. It is written unconditionally and is otherwise unused.
import { defineStore } from 'pinia'
import { computed, reactive, ref, shallowRef, watch } from 'vue'
import { useMountStore } from './mount'
import {
  decodeChunk, LayerScheduler, REQUEST_TIMEOUT_MS, SKY_LAYERS, skyNowMs,
  type Chunk, type ChunkRequest, type SkyLayer, type SkySite,
} from '../utils/sky-chunks'

export interface SkyStatEvent {
  layer: SkyLayer
  kind: 'head' | 'full'
  t0: number
  duration: number
  at: number // performance.now() when the chunk was installed
  fetchMs: number
  decodeMs: number
  bytes: number
  ok: boolean
  error?: string
}

function stats(): { events: SkyStatEvent[] } | null {
  if (typeof window === 'undefined') return null
  const w = window as unknown as { __skyStats?: { events: SkyStatEvent[] } }
  return (w.__skyStats ??= { events: [] })
}

async function fetchChunkBytes(req: ChunkRequest, signal: AbortSignal): Promise<ArrayBuffer> {
  // Time-limited: the dev proxy never answers while the analysis service is down.
  const res = await fetch('/api/sky/chunk', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      layer: req.layer,
      site: req.site,
      t0_unix: req.t0_unix,
      duration_s: req.duration_s,
    }),
    cache: 'no-store',
    signal: AbortSignal.any([signal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)]),
  })
  if (!res.ok) throw Object.assign(new Error(`chunk HTTP ${res.status}`), { status: res.status })
  return await res.arrayBuffer()
}

/** Live-set metadata row of /api/sky/objects: [norad, name, cospar, country, orbit_class]. */
export interface SatMeta {
  norad: number
  name: string
  cospar: string | null
  country: string | null
  orbit_class: string
  layer: SkyLayer
}

/** /api/sky/state response (T2.3). */
export interface SkyState {
  norad: number
  name: string | null
  cospar: string | null
  country: string | null
  object_type: string | null
  orbit_class: string | null
  az: number | null
  alt: number | null
  range_km: number | null
  illuminated: boolean | null
  tle_epoch: string | null
  tle_age_days: number | null
}

const META_TIMEOUT_MS = 30_000
const STATE_TIMEOUT_MS = 4_000
const STATE_CACHE_MS = 1_000

export const useSkyStore = defineStore('sky', () => {
  const mount = useMountStore()
  const versions = reactive<Record<SkyLayer, number>>({ leo: 0, meo: 0, geo: 0 })
  const lastError = reactive<Record<SkyLayer, string | null>>({ leo: null, meo: null, geo: null })

  // Observer site (Settings site from the snapshot).
  const site = shallowRef<SkySite | null>(null)
  const stateCache = new Map<number, { at: number, data: SkyState | null, p: Promise<SkyState | null> | null }>()

  const schedulers = {} as Record<SkyLayer, LayerScheduler>
  for (const layer of SKY_LAYERS) {
    const timing = { fetchStart: 0, fetchMs: 0, bytes: 0 }
    const s: LayerScheduler = new LayerScheduler(layer, {
      fetchChunk: async (req, signal) => {
        timing.fetchStart = performance.now()
        const buf = await fetchChunkBytes(req, signal)
        timing.fetchMs = performance.now() - timing.fetchStart
        timing.bytes = buf.byteLength
        return buf
      },
      decode: (buf, l, kind) => {
        const t = performance.now()
        const c = decodeChunk(buf, l, kind)
        stats()?.events.push({
          layer: l,
          kind,
          t0: c.t0,
          duration: c.end - c.t0,
          at: performance.now(),
          fetchMs: Math.round(timing.fetchMs * 10) / 10,
          decodeMs: Math.round((performance.now() - t) * 100) / 100,
          bytes: timing.bytes,
          ok: true,
        })
        return c
      },
      onChange: () => {
        versions[layer] = s.version
        lastError[layer] = null
      },
      onError: (m) => {
        lastError[layer] = m
        stats()?.events.push({
          layer, kind: 'full', t0: 0, duration: 0, at: performance.now(), fetchMs: 0, decodeMs: 0, bytes: 0, ok: false, error: m,
        })
      },
    })
    schedulers[layer] = s
  }

  // Site from the gateway snapshot (Settings site); a lat/lon move > 1e-4 deg flushes in the scheduler.
  const siteKey = computed(() => {
    const s = (mount.snapshot as { site?: { lat?: number, lon?: number, height_m?: number } } | null)?.site
    if (!s || typeof s.lat !== 'number' || typeof s.lon !== 'number') return ''
    return `${s.lat}|${s.lon}|${typeof s.height_m === 'number' ? s.height_m : 0}`
  })
  watch(siteKey, (k) => {
    if (!k) return
    const [lat, lon, h] = k.split('|').map(Number) as [number, number, number]
    const st: SkySite = { lat, lon, alt_m: h }
    site.value = st
    stateCache.clear()
    for (const layer of SKY_LAYERS) schedulers[layer].setSite(st)
  }, { immediate: true })

  // ---- object metadata (names, class, country) for labels / tooltip / card -------------------
  const meta = new Map<number, SatMeta>() // non-reactive; watch metaVersion
  const metaVersion = ref(0)
  const metaLoaded: Record<SkyLayer, boolean> = { leo: false, meo: false, geo: false }
  let metaTimer: ReturnType<typeof setTimeout> | null = null

  async function loadMeta() {
    metaTimer = null
    let pending = false
    for (const layer of SKY_LAYERS) {
      if (metaLoaded[layer]) continue
      try {
        const res = await fetch(`/api/sky/objects?layer=${layer}`, {
          cache: 'no-store',
          signal: AbortSignal.timeout(META_TIMEOUT_MS),
        })
        if (!res.ok) throw new Error(`objects HTTP ${res.status}`)
        const rows = await res.json() as Array<[number, string, string | null, string | null, string]>
        for (const r of rows) {
          meta.set(r[0], { norad: r[0], name: r[1], cospar: r[2], country: r[3], orbit_class: r[4], layer })
        }
        metaLoaded[layer] = true
        metaVersion.value += 1
      }
      catch {
        pending = true
      }
    }
    if (pending && started) metaTimer = setTimeout(loadMeta, 2000)
  }

  // ---- single-object state (range, illumination, TLE age), cached ~1 s per NORAD ---------------

  /** State of one object now; shares in-flight requests and caches for 1 s. null when unknown/failed. */
  function fetchState(norad: number, maxAgeMs = STATE_CACHE_MS): Promise<SkyState | null> {
    const st = site.value
    if (!st) return Promise.resolve(null)
    const hit = stateCache.get(norad)
    if (hit?.p) return hit.p
    if (hit && performance.now() - hit.at < maxAgeMs) return Promise.resolve(hit.data)
    const entry: { at: number, data: SkyState | null, p: Promise<SkyState | null> | null } = {
      at: hit?.at ?? 0, data: hit?.data ?? null, p: null,
    }
    entry.p = (async () => {
      try {
        const res = await fetch('/api/sky/state', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ norad, site: st, t_unix: skyNowMs() / 1000 }),
          cache: 'no-store',
          signal: AbortSignal.timeout(STATE_TIMEOUT_MS),
        })
        entry.data = res.ok ? await res.json() as SkyState : null
      }
      catch {
        entry.data = null
      }
      entry.at = performance.now()
      entry.p = null
      return entry.data
    })()
    stateCache.set(norad, entry)
    return entry.p
  }

  let started = false
  /** Idempotent: starts the 1 s fetch/evict ticker for all layers. */
  function start() {
    if (started) return
    started = true
    for (const layer of SKY_LAYERS) schedulers[layer].start(1000)
    void loadMeta()
  }
  function stop() {
    started = false
    if (metaTimer) clearTimeout(metaTimer)
    for (const layer of SKY_LAYERS) schedulers[layer].stop()
  }

  /** The current (non-reactive) chunk list for a layer; redraw on `versions[layer]`. */
  function chunks(layer: SkyLayer): readonly Chunk[] {
    return schedulers[layer].chunks
  }

  return { versions, lastError, site, meta, metaVersion, start, stop, chunks, fetchState }
})
