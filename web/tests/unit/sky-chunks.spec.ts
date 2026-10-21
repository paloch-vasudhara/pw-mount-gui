// T2.2b: chunk decoder, position lookup and the per-layer scheduler (fake timers).
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  backoffMs, decodeChunk, findChunk, LayerScheduler, positionsAt,
  type ChunkRequest, type SchedulerDeps,
} from '../../app/utils/sky-chunks'

// vitest runs from web/
const fx = (name: string) => resolve(process.cwd(), 'tests/fixtures', name)

/** Mirror of analysis/chunks.py compute_chunk's wire format (little-endian, 4-byte aligned). */
function encode(opts: {
  layer?: string
  t0: number
  step: number
  nSteps: number
  norad: number[]
  tracks: Array<{ obj: number, start: number, pts: Array<[number, number]> }>
}): ArrayBuffer {
  const n_seg = opts.tracks.length
  const n_samples = opts.tracks.reduce((a, t) => a + t.pts.length, 0)
  const json = JSON.stringify({
    layer: opts.layer ?? 'leo', t0_unix: opts.t0, step_s: opts.step, n_steps: opts.nSteps, n_seg, n_samples, norad: opts.norad,
  })
  const hb = new TextEncoder().encode(json)
  const hlen = hb.length + ((4 - ((4 + hb.length) % 4)) % 4)
  const buf = new ArrayBuffer(4 + hlen + n_seg * 12 + n_samples * 8)
  const dv = new DataView(buf)
  dv.setUint32(0, hlen, true)
  const u8 = new Uint8Array(buf)
  u8.fill(0x20, 4, 4 + hlen)
  u8.set(hb, 4)
  let off = 4 + hlen
  for (const t of opts.tracks) {
    dv.setInt32(off, t.obj, true)
    dv.setInt32(off + 4, t.start, true)
    dv.setInt32(off + 8, t.pts.length, true)
    off += 12
  }
  for (const t of opts.tracks) {
    for (const [az, alt] of t.pts) {
      dv.setFloat32(off, az, true)
      dv.setFloat32(off + 4, alt, true)
      off += 8
    }
  }
  return buf
}

const outs = (n = 16) => ({ az: new Float64Array(n), alt: new Float64Array(n), idx: new Int32Array(n) })

describe('decodeChunk', () => {
  it('decodes the real analysis fixture (Python-generated) and matches the reference', () => {
    const buf = readFileSync(fx('sky-chunk-leo.bin'))
    const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) as ArrayBuffer
    const exp = JSON.parse(readFileSync(fx('sky-chunk-leo.expected.json'), 'utf8'))
    const c = decodeChunk(ab, 'leo')
    expect(c.t0).toBe(exp.header.t0_unix)
    expect(c.stepS).toBe(exp.header.step_s)
    expect(c.nSteps).toBe(exp.header.n_steps)
    expect(c.nSeg).toBe(exp.header.n_seg)
    expect(c.nSamples).toBe(exp.header.n_samples)
    expect(c.norad).toEqual(exp.header.norad)
    expect(c.end).toBe(c.t0 + (c.nSteps - 1) * c.stepS)
    expect(Array.from(c.segs.slice(0, 3))).toEqual(exp.first_segments[0])
    expect(c.samples[0]).toBeCloseTo(exp.first_samples[0][0], 4)
    expect(c.samples[1]).toBeCloseTo(exp.first_samples[0][1], 4)
    const o = outs(c.nObj)
    for (const p of exp.points) {
      const n = positionsAt(c, p.t, o.az, o.alt, o.idx)
      let found = false
      for (let i = 0; i < n; i++) {
        if (c.norad[o.idx[i]!] !== p.norad) continue
        found = true
        const dAz = Math.abs(((o.az[i]! - p.az + 540) % 360) - 180)
        expect(dAz).toBeLessThan(2e-4)
        expect(Math.abs(o.alt[i]! - p.alt)).toBeLessThan(2e-4)
      }
      expect(found).toBe(true)
    }
  })

  it('builds per-object segment ranges and sample offsets', () => {
    const c = decodeChunk(encode({
      t0: 1000, step: 10, nSteps: 20, norad: [11, 22, 33],
      tracks: [
        { obj: 0, start: 0, pts: [[10, 5], [11, 6]] },
        { obj: 0, start: 10, pts: [[20, 5], [21, 6], [22, 7]] },
        { obj: 2, start: 3, pts: [[30, 8], [31, 9]] },
      ],
    }), 'leo')
    expect(Array.from(c.objSeg)).toEqual([0, 2, 2, 3])
    expect(Array.from(c.segStart)).toEqual([0, 10, 3])
    expect(Array.from(c.segOff)).toEqual([0, 2, 5])
    expect(c.vec.length).toBe(7 * 3)
  })

  it('rejects truncated / inconsistent buffers', () => {
    const good = encode({ t0: 0, step: 10, nSteps: 5, norad: [1], tracks: [{ obj: 0, start: 0, pts: [[1, 1], [2, 2]] }] })
    expect(() => decodeChunk(good.slice(0, good.byteLength - 4), 'leo')).toThrow()
    expect(() => decodeChunk(new ArrayBuffer(2), 'leo')).toThrow()
  })

  it('decodes an empty chunk', () => {
    const c = decodeChunk(encode({ t0: 100, step: 10, nSteps: 13, norad: [], tracks: [] }), 'leo')
    expect(c.nObj).toBe(0)
    expect(positionsAt(c, 105, new Float64Array(1), new Float64Array(1), new Int32Array(1))).toBe(0)
  })
})

describe('positionsAt', () => {
  const o = outs()
  const chunk = decodeChunk(encode({
    t0: 1000, step: 10, nSteps: 10, norad: [1, 2],
    tracks: [
      { obj: 0, start: 2, pts: [[350, 10], [10, 10], [30, 20]] }, // steps 2..4, crosses az 0
      { obj: 1, start: 0, pts: [[100, 30], [110, 31]] }, // steps 0..1
      { obj: 1, start: 6, pts: [[200, 40], [210, 41]] }, // steps 6..7
    ],
  }), 'leo')

  it('interpolates across the az wrap in unit-vector space', () => {
    const n = positionsAt(chunk, 1000 + 25, o.az, o.alt, o.idx) // step 2.5: midway 350 -> 10 az
    expect(n).toBe(1)
    expect(o.idx[0]).toBe(0)
    const d = Math.min(o.az[0]!, 360 - o.az[0]!)
    expect(d).toBeLessThan(0.01)
    expect(o.alt[0]!).toBeGreaterThan(9.9)
    expect(o.alt[0]!).toBeLessThan(10.2)
  })

  it('returns exact samples on grid steps and omits objects outside their segments', () => {
    expect(positionsAt(chunk, 1000 + 30, o.az, o.alt, o.idx)).toBe(1) // step 3: obj 0 only
    expect(o.az[0]!).toBeCloseTo(10, 3)
    expect(positionsAt(chunk, 1000 + 45, o.az, o.alt, o.idx)).toBe(0) // 4.5: no object (obj 0 ends at 4)
  })

  it('handles several segments of one object and the segment last sample', () => {
    expect(positionsAt(chunk, 1000 + 5, o.az, o.alt, o.idx)).toBe(1) // step 0.5 -> obj 1
    expect(o.idx[0]).toBe(1)
    expect(positionsAt(chunk, 1000 + 65, o.az, o.alt, o.idx)).toBe(1) // step 6.5 -> obj 1 second seg
    expect(o.az[0]!).toBeCloseTo(205, 0)
    expect(positionsAt(chunk, 1000 + 40, o.az, o.alt, o.idx)).toBe(1) // step 4.0 exactly the last sample of obj 0
    expect(o.idx[0]).toBe(0)
  })

  it('is null outside the chunk and picks the later chunk on the shared boundary', () => {
    expect(positionsAt(chunk, 999, o.az, o.alt, o.idx)).toBe(0)
    expect(positionsAt(chunk, 1000 + 91, o.az, o.alt, o.idx)).toBe(0)
    const next = decodeChunk(encode({ t0: 1090, step: 10, nSteps: 5, norad: [1], tracks: [] }), 'leo')
    expect(findChunk([chunk, next], 1090)).toBe(next)
    expect(findChunk([chunk, next], 1050)).toBe(chunk)
    expect(findChunk([chunk, next], 2000)).toBeNull()
  })
})

// ---- scheduler -------------------------------------------------------------------------------
describe('LayerScheduler', () => {
  const T0 = 1_800_000_000 // unix s
  let nowMs = 0
  let reqs: ChunkRequest[]
  let signals: AbortSignal[]
  let failNext = 0
  let held: Array<() => void> | null = null

  function fakeChunk(req: ChunkRequest): ArrayBuffer {
    const step = 10
    const t0 = Math.floor(req.t0_unix / step) * step
    const nSteps = Math.ceil(req.duration_s / step) + 1
    return encode({ t0, step, nSteps, norad: [1], tracks: [{ obj: 0, start: 0, pts: [[10, 10], [11, 11]] }] })
  }

  function deps(extra: Partial<SchedulerDeps> = {}): SchedulerDeps {
    return {
      nowMs: () => nowMs,
      clockMs: () => Date.now(),
      fetchChunk: async (req, signal) => {
        reqs.push(req)
        signals.push(signal)
        if (held) await new Promise<void>(r => held!.push(r))
        if (signal.aborted) throw new Error('aborted')
        if (failNext > 0) {
          failNext -= 1
          throw new Error('boom')
        }
        return fakeChunk(req)
      },
      ...extra,
    }
  }

  const SITE = { lat: 14.3479, lon: 100.5656, alt_m: 12 }
  const settle = async () => { await vi.advanceTimersByTimeAsync(0) }

  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(T0 * 1000))
    nowMs = T0 * 1000
    reqs = []
    signals = []
    failNext = 0
    held = null
  })
  afterEach(() => { vi.useRealTimers() })

  it('does nothing without a site; then head (120 s) first, then the full chunk', async () => {
    const s = new LayerScheduler('leo', deps())
    s.start()
    await settle()
    expect(reqs.length).toBe(0)
    s.setSite(SITE)
    await settle()
    expect(reqs.map(r => r.duration_s)).toEqual([120, 1800])
    expect(reqs[0]!.t0_unix).toBe(T0)
    expect(s.chunks.length).toBe(1) // the head was replaced by the full chunk
    expect(s.chunks[0]!.kind).toBe('full')
    expect(s.version).toBeGreaterThanOrEqual(3) // setSite flush + head + full
    s.stop()
  })

  it('fetches the next chunk when less than 10 min is left, at the coverage end', async () => {
    const s = new LayerScheduler('leo', deps())
    s.setSite(SITE)
    s.start()
    await settle()
    const n0 = reqs.length
    expect(n0).toBe(2)
    const end = s.chunks[0]!.end
    expect(end).toBe(T0 + 1800)
    // 19 min later: 11 min left -> still nothing
    nowMs += 19 * 60_000
    await vi.advanceTimersByTimeAsync(2000)
    expect(reqs.length).toBe(n0)
    // 21 min later: 9 min left -> next chunk, starting at the old end
    nowMs += 2 * 60_000
    await vi.advanceTimersByTimeAsync(2000)
    expect(reqs.length).toBe(n0 + 1)
    expect(reqs[n0]!.t0_unix).toBe(end)
    expect(reqs[n0]!.duration_s).toBe(1800)
    expect(s.chunks.length).toBe(2)
    expect(s.chunks[1]!.t0).toBe(end)
    s.stop()
  })

  it('evicts chunks that ended before now', async () => {
    const s = new LayerScheduler('leo', deps())
    s.setSite(SITE)
    s.start()
    await settle()
    nowMs += 21 * 60_000
    await vi.advanceTimersByTimeAsync(2000)
    expect(s.chunks.length).toBe(2)
    nowMs += 10 * 60_000 // first chunk (ends +30 min) is now 31 min old -> evicted
    await vi.advanceTimersByTimeAsync(1500)
    expect(s.chunks.length).toBe(1)
    expect(s.chunks[0]!.t0).toBe(T0 + 1800)
    s.stop()
  })

  it('flushes and refetches (aborting) on a site change > 1e-4 deg, ignores smaller moves', async () => {
    const s = new LayerScheduler('leo', deps())
    s.setSite(SITE)
    s.start()
    await settle()
    expect(reqs.length).toBe(2)
    s.setSite({ ...SITE, lat: SITE.lat + 5e-5 })
    await settle()
    expect(reqs.length).toBe(2)
    expect(s.chunks.length).toBe(1)
    // a big move while a request is in flight: that request is aborted and its result dropped
    held = []
    const v = s.version
    s.setSite({ ...SITE, lon: SITE.lon + 0.01 })
    await settle()
    expect(s.chunks.length).toBe(0)
    expect(s.version).toBeGreaterThan(v)
    expect(reqs.length).toBe(3)
    const inflightSignal = signals[2]!
    s.setSite({ ...SITE, lon: SITE.lon + 0.02 })
    expect(inflightSignal.aborted).toBe(true)
    await settle()
    held!.forEach(r => r()) // release both held fetches
    held = null
    await vi.advanceTimersByTimeAsync(10)
    // only data for the second new site may land: its head request carries the new longitude
    expect(reqs[reqs.length - 1]!.site.lon).toBeCloseTo(SITE.lon + 0.02, 6)
    expect(s.chunks.every(c => c.layer === 'leo')).toBe(true)
    s.stop()
  })

  it('retries failures with exponential backoff capped at 30 s', async () => {
    expect([1, 2, 3, 4, 5, 6, 7, 8, 20].map(backoffMs)).toEqual([500, 1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000])
    const errors: string[] = []
    const s = new LayerScheduler('leo', deps({ onError: m => errors.push(m) }))
    failNext = 3
    s.setSite(SITE)
    s.start()
    await settle()
    expect(reqs.length).toBe(1)
    expect(errors.length).toBe(1)
    await vi.advanceTimersByTimeAsync(1000) // 500 ms backoff has passed (tick at 1 s)
    expect(reqs.length).toBe(2)
    await vi.advanceTimersByTimeAsync(1000) // backoff now 1 s: retried on the next tick
    expect(reqs.length).toBe(3)
    await vi.advanceTimersByTimeAsync(1000) // backoff now 2 s: not yet
    expect(reqs.length).toBe(3)
    await vi.advanceTimersByTimeAsync(2000)
    expect(reqs.length).toBeGreaterThanOrEqual(4)
    expect(s.chunks.length).toBeGreaterThan(0) // recovered
    s.stop()
  })

  it('polls every second (no backoff growth) while the service answers 503', async () => {
    let n = 0
    const s = new LayerScheduler('leo', deps({
      fetchChunk: async (req) => {
        n += 1
        if (n <= 5) throw Object.assign(new Error('chunk HTTP 503'), { status: 503 })
        return fakeChunk(req)
      },
    }))
    s.setSite(SITE)
    s.start()
    await vi.advanceTimersByTimeAsync(5500)
    expect(n).toBeGreaterThanOrEqual(6) // 1 request per second, not 0.5/1/2/4/8 s
    expect(s.chunks.length).toBeGreaterThan(0)
    s.stop()
  })

  it('exposes a plan() that mirrors the policy', () => {
    const s = new LayerScheduler('geo', deps())
    expect(s.plan()).toEqual({ t0: T0, duration: 120, kind: 'head' })
  })
})
