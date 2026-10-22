import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createAckTracker } from '../../app/utils/mount-socket'

describe('createAckTracker', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('resolves the matching ack and ignores unknown ids', async () => {
    const t = createAckTracker(1000)
    const a = t.register('stop')
    const b = t.register('tracking')
    expect(a.id).not.toBe(b.id)
    expect(t.settle({ id: 'nope', cmd: 'x', ok: true, reason: null })).toBe(false)
    expect(t.settle({ id: b.id, cmd: 'tracking', ok: false, reason: 'Another window has control' })).toBe(true)
    expect((await b.promise).reason).toBe('Another window has control')
    expect(t.size()).toBe(1)
    t.settle({ id: a.id, cmd: 'stop', ok: true, reason: null })
    expect((await a.promise).ok).toBe(true)
    expect(t.size()).toBe(0)
  })

  it('times out with a reason instead of hanging or rejecting', async () => {
    const t = createAckTracker(500)
    const a = t.register('goto_altaz')
    vi.advanceTimersByTime(501)
    const ack = await a.promise
    expect(ack.ok).toBe(false)
    expect(ack.reason).toContain('timed out')
    expect(t.size()).toBe(0)
  })

  it('fails every pending command when the link is lost', async () => {
    const t = createAckTracker(5000)
    const a = t.register('goto_altaz')
    const b = t.register('stop')
    t.failAll('Gateway link lost')
    expect((await a.promise).reason).toBe('Gateway link lost')
    expect((await b.promise).ok).toBe(false)
    expect(t.size()).toBe(0)
    vi.advanceTimersByTime(10_000) // timers were cleared: no late double-resolve
  })

  it('a late ack after timeout is ignored', async () => {
    const t = createAckTracker(100)
    const a = t.register('stop')
    vi.advanceTimersByTime(150)
    await a.promise
    expect(t.settle({ id: a.id, cmd: 'stop', ok: true, reason: null })).toBe(false)
  })

  it('ids are unique and at most 64 chars', () => {
    const t = createAckTracker(1000)
    const ids = new Set(Array.from({ length: 50 }, () => t.register('stop').id))
    expect(ids.size).toBe(50)
    for (const id of ids) expect(id.length).toBeLessThanOrEqual(64)
    t.failAll('x')
  })
})
