// Satellite sky chunks (T2.2b): binary decoder, per-layer fetch scheduler, position lookup.
// Pure and framework-free. The wire format is documented in analysis/chunks.py (module docstring):
//   uint32 H | H bytes UTF-8 JSON header (space padded so 4 + H is a multiple of 4)
//   Int32[n_seg * 3] (obj_idx, start_step, n) | Float32[n_samples * 2] (az_deg, alt_deg)
// Step k is at unix time t0_unix + k * step_s, k = 0 .. n_steps - 1.
//
// Per frame the lookup is O(objects) with no allocation: everything it needs (per-object segment
// ranges, per-segment start/length/sample offset, unit vectors of every sample) is built once at
// decode time.

export type SkyLayer = 'leo' | 'meo' | 'geo'
export const SKY_LAYERS: readonly SkyLayer[] = ['leo', 'meo', 'geo']

export const HEAD_S = 120
export const FULL_S = 1800
/** Request the next chunk when the coverage left is below this. */
export const REFILL_LEFT_S = 600
export const SITE_EPS_DEG = 1e-4
export const BACKOFF_BASE_MS = 500
export const BACKOFF_MAX_MS = 30_000
export const REQUEST_TIMEOUT_MS = 30_000
/** Retry delay while the analysis service answers 503 (catalogue still loading). */
export const LOADING_RETRY_MS = 1000

const DEG = Math.PI / 180

// ---- test hook ---------------------------------------------------------------------------------
/**
 * TEST HOOK (inert unless set): `window.__skyTestClockOffsetMs = <ms>` shifts the "now" used by the
 * satellite scheduler and by the satellite drawing, so a gate can watch the next-chunk fetch happen
 * (e.g. +21 min leaves 9 min of a fresh 30-min chunk) without waiting. `delete` it to restore.
 */
export function skyNowMs(): number {
  const off = typeof window !== 'undefined'
    ? (window as unknown as { __skyTestClockOffsetMs?: unknown }).__skyTestClockOffsetMs
    : undefined
  return Date.now() + (typeof off === 'number' && Number.isFinite(off) ? off : 0)
}

// ---- decoder -----------------------------------------------------------------------------------
export interface ChunkHeader {
  layer: string
  t0_unix: number
  step_s: number
  n_steps: number
  n_seg: number
  n_samples: number
  norad: number[]
}

export interface Chunk {
  layer: SkyLayer
  kind: 'head' | 'full'
  t0: number
  stepS: number
  nSteps: number
  /** Unix time of the last grid step (shared with the next chunk's first). */
  end: number
  nObj: number
  norad: number[]
  nSeg: number
  nSamples: number
  /** Raw wire arrays. */
  segs: Int32Array
  samples: Float32Array
  /** Lookup tables: segments of object i are objSeg[i] .. objSeg[i + 1] - 1. */
  objSeg: Int32Array
  segStart: Int32Array
  segN: Int32Array
  segOff: Int32Array
  /** Unit vectors (east, north, up) of every sample, 3 per sample. */
  vec: Float32Array
}

export function decodeChunk(buf: ArrayBuffer, layer: SkyLayer, kind: 'head' | 'full' = 'full'): Chunk {
  if (buf.byteLength < 4) throw new Error('chunk: truncated')
  const hlen = new DataView(buf).getUint32(0, true)
  if (4 + hlen > buf.byteLength || (4 + hlen) % 4 !== 0) throw new Error('chunk: bad header length')
  const h = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, hlen))) as ChunkHeader
  const nSeg = h.n_seg | 0
  const nSamples = h.n_samples | 0
  const segOffBytes = 4 + hlen
  const smpOffBytes = segOffBytes + nSeg * 12
  if (smpOffBytes + nSamples * 8 !== buf.byteLength) throw new Error('chunk: size mismatch')
  const segs = new Int32Array(buf, segOffBytes, nSeg * 3)
  const samples = new Float32Array(buf, smpOffBytes, nSamples * 2)
  const nObj = h.norad.length

  const objSeg = new Int32Array(nObj + 1)
  const segStart = new Int32Array(nSeg)
  const segN = new Int32Array(nSeg)
  const segOff = new Int32Array(nSeg)
  let cursor = 0
  let lastObj = -1
  for (let s = 0; s < nSeg; s++) {
    const obj = segs[3 * s]!
    if (obj < lastObj || obj >= nObj) throw new Error('chunk: segments not sorted by object')
    while (lastObj < obj) objSeg[++lastObj] = s
    segStart[s] = segs[3 * s + 1]!
    segN[s] = segs[3 * s + 2]!
    segOff[s] = cursor
    cursor += segN[s]!
  }
  while (lastObj < nObj) objSeg[++lastObj] = nSeg
  // fill the trailing entries (objects without segments cannot occur, but stay safe)
  if (cursor !== nSamples) throw new Error('chunk: segment lengths do not match samples')

  const vec = new Float32Array(nSamples * 3)
  for (let i = 0; i < nSamples; i++) {
    const a = samples[2 * i]! * DEG
    const e = samples[2 * i + 1]! * DEG
    const ce = Math.cos(e)
    vec[3 * i] = ce * Math.sin(a)
    vec[3 * i + 1] = ce * Math.cos(a)
    vec[3 * i + 2] = Math.sin(e)
  }
  return {
    layer,
    kind,
    t0: h.t0_unix,
    stepS: h.step_s,
    nSteps: h.n_steps,
    end: h.t0_unix + (h.n_steps - 1) * h.step_s,
    nObj,
    norad: h.norad,
    nSeg,
    nSamples,
    segs,
    samples,
    objSeg,
    segStart,
    segN,
    segOff,
    vec,
  }
}

// ---- position lookup ---------------------------------------------------------------------------
/** The chunk that covers `t` (unix s), or null. Prefers the later chunk on a shared boundary. */
export function findChunk(chunks: readonly Chunk[], t: number): Chunk | null {
  let hit: Chunk | null = null
  for (let i = 0; i < chunks.length; i++) {
    const c = chunks[i]!
    if (t >= c.t0 && t <= c.end && (hit === null || c.t0 > hit.t0)) hit = c
  }
  return hit
}

/**
 * Az/alt (deg) of every object that has a segment covering `t`, interpolated between the two
 * bracketing samples in unit-vector space (so the az wrap needs no special case). Writes into
 * `outAz` / `outAlt` / `outIdx` (index into `chunk.norad`) and returns how many were written.
 * The out arrays need `chunk.nObj` capacity. No allocation.
 */
export function positionsAt(
  chunk: Chunk,
  t: number,
  outAz: Float32Array | Float64Array,
  outAlt: Float32Array | Float64Array,
  outIdx: Int32Array,
): number {
  const f = (t - chunk.t0) / chunk.stepS
  if (!(f >= 0) || t > chunk.end || chunk.nSteps < 2) return 0
  let k = Math.floor(f)
  let u = f - k
  if (k >= chunk.nSteps - 1) {
    k = chunk.nSteps - 2
    u = 1
  }
  const { objSeg, segStart, segN, segOff, vec } = chunk
  let n = 0
  for (let i = 0; i < chunk.nObj; i++) {
    for (let s = objSeg[i]!, e = objSeg[i + 1]!; s < e; s++) {
      const st = segStart[s]!
      if (k < st) break // segments of an object are sorted by start
      const last = st + segN[s]! - 1
      if (k > last || (k === last && u !== 0)) continue
      const a = 3 * (segOff[s]! + (k - st))
      // on the very last sample of a segment (u == 0) there is no right neighbour to read
      const w = k === last ? 0 : u
      const b3 = k === last ? 0 : 3
      const x = vec[a]! + (vec[a + b3]! - vec[a]!) * w
      const y = vec[a + 1]! + (vec[a + 1 + b3]! - vec[a + 1]!) * w
      const z = vec[a + 2]! + (vec[a + 2 + b3]! - vec[a + 2]!) * w
      const len = Math.hypot(x, y, z) || 1
      let az = Math.atan2(x, y) / DEG
      if (az < 0) az += 360
      outAz[n] = az
      outAlt[n] = Math.asin(Math.max(-1, Math.min(1, z / len))) / DEG
      outIdx[n] = i
      n++
      break
    }
  }
  return n
}

// ---- scheduler ---------------------------------------------------------------------------------
export interface SkySite {
  lat: number
  lon: number
  alt_m: number
}

export interface ChunkRequest {
  layer: SkyLayer
  site: SkySite
  t0_unix: number
  duration_s: number
}

export interface SchedulerDeps {
  /** Fetch one chunk's raw bytes; must honour `signal`. */
  fetchChunk: (req: ChunkRequest, signal: AbortSignal) => Promise<ArrayBuffer>
  /** Decode (sync or async, e.g. via a worker). Default: decodeChunk. */
  decode?: (buf: ArrayBuffer, layer: SkyLayer, kind: 'head' | 'full') => Chunk | Promise<Chunk>
  /** "Now" in unix ms (chunk time; the test hook shifts it). Default: skyNowMs. */
  nowMs?: () => number
  /** Monotonic-ish wall clock for backoff, unix ms. Default Date.now. */
  clockMs?: () => number
  /** Called when the set of drawable data changed (new chunk or flush). */
  onChange?: () => void
  onError?: (msg: string) => void
}

export function backoffMs(failures: number): number {
  return Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** Math.max(0, failures - 1))
}

/**
 * One layer's chunk cache + fetch policy:
 *   no chunks     -> fetch the 120 s head at now (draw at once)
 *   head only     -> fetch the full 1800 s chunk at now
 *   full, coverage left < 10 min -> fetch the next chunk starting at the coverage end
 *   chunks whose end < now are evicted; a site change > 1e-4 deg flushes everything (and aborts).
 * Errors retry with exponential backoff (0.5 s .. 30 s); HTTP 503 (catalogue loading) retries every 1 s. `tick()` is cheap and idempotent: call it
 * from a 1 s timer (`start`) and after any change.
 */
export class LayerScheduler {
  readonly layer: SkyLayer
  chunks: Chunk[] = []
  /** Bumped whenever the drawable data changes (new chunk, flush). */
  version = 0
  private deps: SchedulerDeps
  private site: SkySite | null = null
  private abort: AbortController | null = null
  private inflight = false
  private gen = 0
  private failures = 0
  private retryAt = 0
  private timer: ReturnType<typeof setInterval> | null = null

  constructor(layer: SkyLayer, deps: SchedulerDeps) {
    this.layer = layer
    this.deps = deps
  }

  private now(): number {
    return (this.deps.nowMs ?? skyNowMs)() / 1000
  }

  private clock(): number {
    return (this.deps.clockMs ?? Date.now)()
  }

  /** Set the observer site; flushes and refetches when lat/lon moved by more than 1e-4 deg. */
  setSite(site: SkySite): void {
    const prev = this.site
    this.site = { ...site }
    if (prev && Math.abs(prev.lat - site.lat) <= SITE_EPS_DEG && Math.abs(prev.lon - site.lon) <= SITE_EPS_DEG) {
      this.site = { ...site, lat: prev.lat, lon: prev.lon } // keep the reference the data was built for
      return
    }
    this.flush()
    this.tick()
  }

  flush(): void {
    this.gen += 1
    this.abort?.abort()
    this.abort = null
    this.inflight = false
    this.failures = 0
    this.retryAt = 0
    if (this.chunks.length) this.chunks = []
    this.version += 1
    this.deps.onChange?.()
  }

  private coverageEnd(): number | null {
    let end: number | null = null
    for (const c of this.chunks) if (c.kind === 'full' && (end === null || c.end > end)) end = c.end
    return end
  }

  /** What to fetch next, or null. Exposed for tests. */
  plan(): { t0: number, duration: number, kind: 'head' | 'full' } | null {
    const now = this.now()
    if (this.chunks.length === 0) return { t0: now, duration: HEAD_S, kind: 'head' }
    const end = this.coverageEnd()
    if (end === null) return { t0: now, duration: FULL_S, kind: 'full' }
    if (end - now < REFILL_LEFT_S) return { t0: end, duration: FULL_S, kind: 'full' }
    return null
  }

  tick(): void {
    // evict what is entirely in the past
    const now = this.now()
    if (this.chunks.some(c => c.end < now)) this.chunks = this.chunks.filter(c => c.end >= now)
    if (!this.site || this.inflight) return
    if (this.clock() < this.retryAt) return
    const p = this.plan()
    if (!p) return
    void this.fetchOne(p)
  }

  private async fetchOne(p: { t0: number, duration: number, kind: 'head' | 'full' }): Promise<void> {
    const gen = this.gen
    const ctl = new AbortController()
    this.abort = ctl
    this.inflight = true
    const req: ChunkRequest = { layer: this.layer, site: { ...this.site! }, t0_unix: p.t0, duration_s: p.duration }
    try {
      const buf = await this.deps.fetchChunk(req, ctl.signal)
      if (gen !== this.gen) return
      const decode = this.deps.decode ?? decodeChunk
      const chunk = await decode(buf, this.layer, p.kind)
      if (gen !== this.gen) return
      this.failures = 0
      this.retryAt = 0
      this.add(chunk)
    }
    catch (e) {
      if (gen !== this.gen) return
      if ((e as { status?: number }).status === 503) {
        // The service is up but its catalogue is still loading: poll every second, no backoff.
        this.retryAt = this.clock() + LOADING_RETRY_MS
      }
      else {
        this.failures += 1
        this.retryAt = this.clock() + backoffMs(this.failures)
      }
      this.deps.onError?.(String(e))
    }
    finally {
      if (gen === this.gen) {
        this.inflight = false
        this.abort = null
      }
    }
    if (gen === this.gen) this.tick() // chain: head -> full without waiting for the timer
  }

  private add(chunk: Chunk): void {
    if (chunk.kind === 'full') this.chunks = this.chunks.filter(c => c.kind !== 'head')
    this.chunks = this.chunks.filter(c => c.t0 !== chunk.t0 || c.kind !== chunk.kind).concat(chunk)
    this.chunks.sort((a, b) => a.t0 - b.t0)
    this.version += 1
    this.deps.onChange?.()
  }

  /** Run `tick()` every `ms` (default 1 s). */
  start(ms = 1000): void {
    if (this.timer) return
    this.timer = setInterval(() => this.tick(), ms)
    this.tick()
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer)
    this.timer = null
    this.gen += 1
    this.abort?.abort()
    this.abort = null
    this.inflight = false
  }
}
