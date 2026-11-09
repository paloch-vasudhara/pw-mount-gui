// @vitest-environment happy-dom
// T5.7: mount CCTV view. Pure rules (options, memory, backoff, state texts), the feed state machine
// (fake timers), the player (streams only while visible) and the toggles on the panel / dialog.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import CctvPlayer from '../../app/components/mount/CctvPlayer.vue'
import MountDetailsDialog from '../../app/components/mount/MountDetailsDialog.vue'
import MountStatusPanel from '../../app/components/mount/MountStatusPanel.vue'
import { CctvFeed, type FeedIO } from '../../app/utils/cctv-feed'
import {
  backoffMs, chipText, loadView, overlayText, resolveView, saveView, snapshotUrl, streamUrl,
  VIEW_KEY_EXPANDED, VIEW_KEY_PANEL, viewOptions,
} from '../../app/utils/cctv'

const hlsInstances: Array<{ url: string, destroyed: boolean, onError: ((e: string, d: { fatal: boolean }) => void) | null }> = []
vi.mock('hls.js', () => {
  class Hls {
    static Events = { ERROR: 'hlsError' }
    static isSupported = () => true
    rec = { url: '', destroyed: false, onError: null as ((e: string, d: { fatal: boolean }) => void) | null }
    constructor() { hlsInstances.push(this.rec) }
    on(_ev: string, fn: (e: string, d: { fatal: boolean }) => void) { this.rec.onError = fn }
    loadSource(u: string) { this.rec.url = u }
    attachMedia() {}
    destroy() { this.rec.destroyed = true }
  }
  return { default: Hls }
})

// The WebGL view is not under test here: a stub that shows the `active` prop it was given.
vi.mock('../../app/components/mount/MountHemisphere3D.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      props: { mode: { type: String, default: 'compact' }, active: { type: Boolean, default: true } },
      setup: p => () => h('div', { 'data-testid': 'mount-3d', 'data-active': String(p.active) }),
    }),
  }
})

// The live control group is not under test either (it pulls in the track dialog and the sky stores).
vi.mock('../../app/components/mount/MountControlPanel.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ setup: () => () => h('div', { 'data-testid': 'control-stub' }) }) }
})

describe('cctv rules', () => {
  it('toggle options: 2 on the panel, 3 in the expanded view', () => {
    expect(viewOptions(false).map(o => o.label)).toEqual(['3D', 'CCTV'])
    expect(viewOptions(true).map(o => o.label)).toEqual(['3D', 'CCTV', 'Side by side'])
  })

  it('the remembered view falls back to 3D when CCTV is not configured or the option is not offered', () => {
    expect(resolveView('cctv', false, true)).toBe('cctv')
    expect(resolveView('cctv', false, false)).toBe('3d')
    expect(resolveView('both', true, true)).toBe('both')
    expect(resolveView('both', false, true)).toBe('3d') // the panel has no side by side
    expect(resolveView(null, true, true)).toBe('3d') // default 3D
    expect(resolveView('nonsense', true, true)).toBe('3d')
  })

  it('memory is per view and survives a broken localStorage', () => {
    localStorage.clear()
    expect(loadView(false)).toBeNull()
    saveView(false, 'cctv')
    saveView(true, 'both')
    expect(localStorage.getItem(VIEW_KEY_PANEL)).toBe('cctv')
    expect(localStorage.getItem(VIEW_KEY_EXPANDED)).toBe('both')
    expect(loadView(true)).toBe('both')
    vi.stubGlobal('localStorage', {
      getItem: () => { throw new Error('blocked') },
      setItem: () => { throw new Error('blocked') },
    })
    expect(loadView(false)).toBeNull()
    expect(() => saveView(false, '3d')).not.toThrow()
    vi.unstubAllGlobals()
  })

  it('backoff 2 -> 30 s', () => {
    expect([1, 2, 3, 4, 5, 6, 9].map(backoffMs)).toEqual([2000, 4000, 8000, 16000, 30000, 30000, 30000])
  })

  it('state texts and chips', () => {
    expect(overlayText('connecting', false)).toBe('Connecting…')
    expect(overlayText('retrying', false)).toBe('CCTV feed unavailable — retrying')
    expect(overlayText('live', false)).toBeNull()
    expect(overlayText('snapshot', true)).toBeNull()
    expect(overlayText('snapshot', false)).toBe('CCTV feed unavailable — retrying')
    expect(chipText('live', false)).toBe('LIVE')
    expect(chipText('snapshot', true)).toBe('SNAPSHOT')
    expect(chipText('snapshot', false)).toBeNull()
    expect(chipText('retrying', false)).toBeNull()
  })

  it('stream URLs carry only the ticket', () => {
    expect(streamUrl('mjpeg', 'a b')).toBe('/api/mount/cctv/mjpeg?t=a%20b')
    expect(streamUrl('hls', 'T')).toBe('/api/mount/cctv/hls/entry.m3u8?t=T')
    expect(snapshotUrl('T', 3)).toBe('/api/mount/cctv/snapshot?t=T&n=3')
  })
})

describe('CctvFeed', () => {
  let io: FeedIO & { opens: string[], closes: number, snaps: Array<[string, number]>, state: string, ticketFails: boolean }
  let feed: CctvFeed
  let n = 0

  beforeEach(() => {
    vi.useFakeTimers()
    n = 0
    io = {
      opens: [],
      closes: 0,
      snaps: [],
      state: 'live',
      ticketFails: false,
      fetchTicket: async () => {
        if (io.ticketFails) throw new Error('down')
        return `T${++n}`
      },
      openStream(t) { io.opens.push(t) },
      closeStream() { io.closes++ },
      showSnapshot(t, c) { io.snaps.push([t, c]) },
      fetchState: async () => io.state,
      changed() {},
    }
    feed = new CctvFeed(io)
  })
  afterEach(() => {
    feed.stop()
    vi.useRealTimers()
  })

  it('opens a stream on start and is live once the player says so', async () => {
    feed.start()
    expect(feed.phase).toBe('connecting')
    await vi.advanceTimersByTimeAsync(0)
    expect(io.opens).toEqual(['T1'])
    feed.notifyLive()
    expect(feed.phase).toBe('live')
  })

  it('a failure retries after 2 s, then 4 s; the third failure shows snapshots every 5 s', async () => {
    feed.start()
    await vi.advanceTimersByTimeAsync(0)
    feed.notifyError() // failure 1
    expect(feed.phase).toBe('retrying')
    expect(io.closes).toBe(1)
    await vi.advanceTimersByTimeAsync(1999)
    expect(io.opens.length).toBe(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(io.opens.length).toBe(2) // retry after 2 s
    feed.notifyError() // failure 2
    await vi.advanceTimersByTimeAsync(3999)
    expect(io.opens.length).toBe(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(io.opens.length).toBe(3) // retry after 4 s
    feed.notifyError() // failure 3
    expect(feed.phase).toBe('snapshot')
    await vi.advanceTimersByTimeAsync(0)
    expect(io.snaps.length).toBe(1) // one at once
    await vi.advanceTimersByTimeAsync(5000)
    expect(io.snaps.length).toBe(2)
    await vi.advanceTimersByTimeAsync(5000)
    expect(io.snaps.length).toBe(3)
    // the stream keeps retrying in the background (8 s after failure 3) and takes over again
    expect(io.opens.length).toBeGreaterThanOrEqual(4)
    feed.notifyLive()
    expect(feed.phase).toBe('live')
    const snaps = io.snaps.length
    await vi.advanceTimersByTimeAsync(20000)
    expect(io.snaps.length).toBe(snaps) // snapshot refresh stopped
  })

  it('snapshot ok / not ok follows the snapshot image', async () => {
    feed.start()
    for (let i = 0; i < 3; i++) {
      await vi.advanceTimersByTimeAsync(0)
      feed.notifyError()
      await vi.advanceTimersByTimeAsync(30000)
    }
    expect(feed.phase).toBe('snapshot')
    expect(feed.snapshotOk).toBe(false)
    feed.notifySnapshot(true)
    expect(feed.snapshotOk).toBe(true)
  })

  it('no picture within 15 s counts as a failure', async () => {
    feed.start()
    await vi.advanceTimersByTimeAsync(14999)
    expect(feed.phase).toBe('connecting')
    await vi.advanceTimersByTimeAsync(1)
    expect(feed.phase).toBe('retrying')
  })

  it('a live feed the gateway reports unreachable is treated as lost', async () => {
    feed.start()
    await vi.advanceTimersByTimeAsync(0)
    feed.notifyLive()
    await vi.advanceTimersByTimeAsync(4000)
    expect(feed.phase).toBe('live') // state live: nothing happens
    io.state = 'unreachable'
    await vi.advanceTimersByTimeAsync(4000)
    expect(feed.phase).toBe('retrying')
  })

  it('a ticket failure is a failed attempt too', async () => {
    io.ticketFails = true
    feed.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(feed.phase).toBe('retrying')
    expect(io.opens).toEqual([])
  })

  it('stop closes the stream and nothing runs afterwards', async () => {
    feed.start()
    await vi.advanceTimersByTimeAsync(0)
    feed.notifyLive()
    const closes = io.closes
    feed.stop()
    expect(io.closes).toBe(closes + 1)
    const opens = io.opens.length
    await vi.advanceTimersByTimeAsync(120000)
    expect(io.opens.length).toBe(opens)
    expect(io.snaps.length).toBe(0)
    feed.start() // and it can start again
    await vi.advanceTimersByTimeAsync(0)
    expect(io.opens.length).toBe(opens + 1)
  })

  it('a stop during the ticket request never opens a stream', async () => {
    feed.start()
    feed.stop()
    await vi.advanceTimersByTimeAsync(0)
    expect(io.opens).toEqual([])
  })
})

const BLANK_PREFIX = 'data:image/gif'

function stubGateway(opts: { configured?: boolean, type?: 'mjpeg' | 'hls' } = {}) {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push(`${init?.method ?? 'GET'} ${url}`)
    const json = (o: unknown) => ({ ok: true, status: 200, json: async () => o }) as Response
    if (url === '/api/mount/session') return json({ token: 'tok' })
    if (url === '/api/mount/cctv/ticket') return json({ ticket: 'TK', expires_in: 60 })
    if (url === '/api/mount/cctv/status') {
      return json({ configured: opts.configured ?? true, type: opts.type ?? 'mjpeg', state: 'live', error: null, viewers: 0 })
    }
    return json({})
  }))
  return calls
}

describe('CctvPlayer', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    hlsInstances.length = 0
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
    HTMLMediaElement.prototype.canPlayType = () => ''
    HTMLMediaElement.prototype.load = () => {}
    HTMLMediaElement.prototype.play = () => Promise.resolve()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it('MJPEG: <img> opens with the ticket, shows Connecting…, then LIVE', async () => {
    stubGateway()
    const w = mount(CctvPlayer, { props: { type: 'mjpeg' } })
    await flushPromises()
    const img = w.get('[data-testid="cctv-stream"]')
    expect(img.element.tagName).toBe('IMG')
    expect(img.attributes('src')).toBe('/api/mount/cctv/mjpeg?t=TK')
    expect(w.get('[data-testid="cctv-overlay"]').text()).toBe('Connecting…')
    await img.trigger('load')
    expect(w.find('[data-testid="cctv-overlay"]').exists()).toBe(false)
    expect(w.get('[data-testid="cctv-chip"]').text()).toBe('LIVE')
    w.unmount()
  })

  it('an img error shows "CCTV feed unavailable — retrying" and closes the stream', async () => {
    stubGateway()
    const w = mount(CctvPlayer, { props: { type: 'mjpeg' } })
    await flushPromises()
    await w.get('[data-testid="cctv-stream"]').trigger('error')
    expect(w.get('[data-testid="cctv-overlay"]').text()).toBe('CCTV feed unavailable — retrying')
    expect(w.get('[data-testid="cctv-stream"]').attributes('src')).toContain(BLANK_PREFIX)
    expect(w.find('[data-testid="cctv-chip"]').exists()).toBe(false)
    w.unmount()
  })

  it('the stream is open only while visible: inactive, tab hidden and unmount all close it', async () => {
    const calls = stubGateway()
    const w = mount(CctvPlayer, { props: { type: 'mjpeg', active: true } })
    await flushPromises()
    const src = () => w.get('[data-testid="cctv-stream"]').attributes('src')!
    expect(src()).toContain('/mjpeg?t=TK')
    await w.setProps({ active: false })
    expect(src()).toContain(BLANK_PREFIX)
    await w.setProps({ active: true })
    await flushPromises()
    expect(src()).toContain('/mjpeg?t=TK')
    // the tab goes to the background
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(src()).toContain(BLANK_PREFIX)
    const tickets = calls.filter(c => c.includes('/ticket')).length
    await new Promise(r => setTimeout(r, 30))
    expect(calls.filter(c => c.includes('/ticket')).length).toBe(tickets) // nothing asked while hidden
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(src()).toContain('/mjpeg?t=TK')
    const el = w.get('[data-testid="cctv-stream"]').element as HTMLImageElement
    w.unmount()
    expect(el.getAttribute('src')).toContain(BLANK_PREFIX) // dialog closed / panel left
  })

  it('HLS: <video muted playsinline> + hls.js on the proxied playlist; destroyed when hidden', async () => {
    stubGateway({ type: 'hls' })
    const w = mount(CctvPlayer, { props: { type: 'hls' } })
    await flushPromises()
    await new Promise(r => setTimeout(r, 0))
    const video = w.get('video')
    expect(video.attributes('playsinline')).toBeDefined()
    expect((video.element as HTMLVideoElement).muted).toBe(true)
    expect(hlsInstances.length).toBe(1)
    expect(hlsInstances[0]!.url).toBe('/api/mount/cctv/hls/entry.m3u8?t=TK')
    await video.trigger('playing')
    expect(w.get('[data-testid="cctv-chip"]').text()).toBe('LIVE')
    await w.setProps({ active: false })
    expect(hlsInstances[0]!.destroyed).toBe(true)
    w.unmount()
  })

  it('HLS: a fatal hls.js error is a lost feed', async () => {
    stubGateway({ type: 'hls' })
    const w = mount(CctvPlayer, { props: { type: 'hls' } })
    await flushPromises()
    await new Promise(r => setTimeout(r, 0))
    hlsInstances[0]!.onError!('hlsError', { fatal: false })
    await flushPromises()
    expect(w.get('[data-testid="cctv-overlay"]').text()).toBe('Connecting…')
    hlsInstances[0]!.onError!('hlsError', { fatal: true })
    await flushPromises()
    expect(hlsInstances[0]!.destroyed).toBe(true)
    expect(w.get('[data-testid="cctv-overlay"]').text()).toBe('CCTV feed unavailable — retrying')
    w.unmount()
  })
})

describe('toggle on the mount panel and the expanded dialog', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  const active = (root: ParentNode) => root.querySelector('[data-testid="mount-3d"]')!.getAttribute('data-active')

  it('not configured: no toggle, no CCTV, the 3D view as today', async () => {
    stubGateway({ configured: false })
    const w = mount(MountStatusPanel)
    await flushPromises()
    expect(w.find('[data-testid="cctv-toggle"]').exists()).toBe(false)
    expect(w.find('[data-testid="cctv-player"]').exists()).toBe(false)
    expect(active(w.element)).toBe('true')
    w.unmount()
  })

  it('a remembered CCTV choice falls back to 3D when CCTV is no longer configured', async () => {
    saveView(false, 'cctv')
    stubGateway({ configured: false })
    const w = mount(MountStatusPanel)
    await flushPromises()
    expect(w.find('[data-testid="cctv-player"]').exists()).toBe(false)
    expect(active(w.element)).toBe('true')
    w.unmount()
  })

  it('configured: a 3D | CCTV toggle inside the 3D slot; CCTV hides the 3D renderer and 3D closes the feed', async () => {
    stubGateway()
    const w = mount(MountStatusPanel)
    await flushPromises()
    const toggle = w.get('[data-testid="mount-3d-slot"] [data-testid="cctv-toggle"]')
    expect(toggle.findAll('button').map(b => b.text())).toEqual(['3D', 'CCTV'])
    expect(toggle.get('button.active').text()).toBe('3D') // default
    expect(w.find('[data-testid="cctv-player"]').exists()).toBe(false)
    await toggle.findAll('button')[1]!.trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="cctv-player"]').exists()).toBe(true)
    expect(active(w.element)).toBe('false') // the renderer stops drawing
    expect(localStorage.getItem(VIEW_KEY_PANEL)).toBe('cctv')
    await toggle.findAll('button')[0]!.trigger('click')
    expect(w.find('[data-testid="cctv-player"]').exists()).toBe(false)
    expect(active(w.element)).toBe('true')
    w.unmount()
  })

  it('the remembered choice is restored (per viewer)', async () => {
    saveView(false, 'cctv')
    stubGateway()
    const w = mount(MountStatusPanel)
    await flushPromises()
    expect(w.find('[data-testid="cctv-player"]').exists()).toBe(true)
    expect(w.get('[data-testid="cctv-toggle"] button.active').text()).toBe('CCTV')
    w.unmount()
  })

  it('expanded dialog: 3D | CCTV | Side by side; side by side shows both halves; closing ends the feed', async () => {
    stubGateway()
    const w = mount(MountDetailsDialog, { props: { open: true }, attachTo: document.body })
    await flushPromises()
    const q = (id: string) => document.body.querySelector(`[data-testid="${id}"]`) as HTMLElement | null
    const buttons = () => Array.from(q('cctv-toggle')!.querySelectorAll('button'))
    expect(buttons().map(b => b.textContent)).toEqual(['3D', 'CCTV', 'Side by side'])
    expect(q('md-pane-cctv')).toBeNull()
    buttons()[2]!.click() // side by side
    await flushPromises()
    expect(q('md-pane-cctv')).not.toBeNull()
    expect(q('md-pane-3d')!.style.display).not.toBe('none')
    expect(active(document.body)).toBe('true')
    expect(localStorage.getItem(VIEW_KEY_EXPANDED)).toBe('both')
    buttons()[1]!.click() // CCTV only
    await flushPromises()
    expect(q('md-pane-3d')!.style.display).toBe('none')
    expect(active(document.body)).toBe('false')
    const img = q('cctv-stream') as HTMLImageElement
    expect(img.getAttribute('src')).toContain('/mjpeg?t=TK')
    await w.setProps({ open: false }) // dialog closes: the feed closes
    await flushPromises()
    expect(document.body.querySelector('[data-testid="cctv-player"]')).toBeNull()
    expect(img.getAttribute('src')).toContain(BLANK_PREFIX)
    w.unmount()
  })

  it('expanded dialog without a camera: no toggle', async () => {
    stubGateway({ configured: false })
    const w = mount(MountDetailsDialog, { props: { open: true }, attachTo: document.body })
    await flushPromises()
    expect(document.body.querySelector('[data-testid="cctv-toggle"]')).toBeNull()
    expect(document.body.querySelector('[data-testid="md-pane-cctv"]')).toBeNull()
    w.unmount()
  })
})
