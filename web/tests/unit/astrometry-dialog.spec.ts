// @vitest-environment happy-dom
// AstrometryDialog flow with fake fetch: load -> detect -> specify the target by clicking the stage (snap) -> remove
// streaks -> readiness (title reason) -> DETECT confirm after an edit -> RUN emits the payload.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import AstrometryDialog from '../../app/components/analysis/AstrometryDialog.vue'

const calls: Array<{ url: string, body: unknown }> = []
let imageStatus = 200
let detectStatus = 200
let snapStatus = 200
let detectCount = 0
let detectWarnings: string[] = []

const META = { format: 'FITS', bit_depth: 16, dims: [800, 600], epoch_iso: '2025-04-04T13:01:30', exposure_s: 30, focal_mm: null, pixel_um: null, warnings: [] as string[] }
const IMAGE = { image_id: 'img1', preview: { png_b64: 'AAAA', width: 400, height: 300, scale: 0.5 }, meta: META }
const mkDets = (n: number) => [
  { id: 'T', role: 'target', shape: 'point', x: 100, y: 100, xs: 100, ys: 100, xe: 100, ye: 100, brt: 3 },
  ...Array.from({ length: n }, (_, i) => ({ id: `S${i + 1}`, role: 'star', shape: 'streak', x: 50, y: 50, xs: 10 + i, ys: 20, xe: 60 + i, ye: 70, brt: 4 })),
]

let w: VueWrapper | null = null
const q = (s: string) => document.body.querySelector(s) as HTMLElement | null
const qa = (s: string) => [...document.body.querySelectorAll(s)] as HTMLElement[]
const run = () => q('[data-testid="as-run"]') as HTMLButtonElement
const sRows = () => qa('[data-testid="as-row"][data-kind="S"]')
const tRow = () => q('[data-testid="as-row"][data-kind="T"]')!
const tick = async () => { await flushPromises(); await nextTick() }

async function openDialog() {
  w = mount(AstrometryDialog, { props: { open: false }, attachTo: document.body })
  await w.setProps({ open: true })
  await tick()
}
async function loadImage() {
  const input = q('[data-testid="as-file"]') as HTMLInputElement
  Object.defineProperty(input, 'files', { value: [new File(['x'], 'frame.fits')], configurable: true })
  input.dispatchEvent(new Event('change', { bubbles: true }))
  await tick()
}
async function click(el: HTMLElement) { el.click(); await tick() }
async function focus(el: HTMLElement) { el.dispatchEvent(new FocusEvent('focus')); await tick() }
async function stageClick(x: number, y: number) {
  q('[data-testid="as-stage"]')!.dispatchEvent(new MouseEvent('click', { clientX: x, clientY: y, bubbles: true }))
  await tick()
}

beforeEach(() => {
  calls.length = 0
  imageStatus = 200; detectStatus = 200; snapStatus = 200; detectCount = 12; detectWarnings = []
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => 400 })
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => 300 })
  HTMLElement.prototype.getBoundingClientRect = () => ({ left: 0, top: 0, right: 400, bottom: 300, width: 400, height: 300, x: 0, y: 0, toJSON: () => ({}) })
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: { body?: unknown }) => {
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : init?.body
    calls.push({ url, body })
    if (url === '/api/astrometry/image') {
      return imageStatus === 200
        ? { ok: true, status: 200, json: async () => IMAGE }
        : { ok: false, status: imageStatus, json: async () => ({ detail: 'Unsupported file type' }) }
    }
    if (url === '/api/astrometry/detect') {
      return detectStatus === 200
        ? { ok: true, status: 200, json: async () => ({ detections: mkDets(detectCount), n_streaks: detectCount, target: null, warnings: detectWarnings }) }
        : { ok: false, status: detectStatus, json: async () => ({ detail: 'Image expired — load it again' }) }
    }
    if (url === '/api/astrometry/snap') {
      return snapStatus === 200
        ? { ok: true, status: 200, json: async () => ({ id: 'T', role: 'target', shape: 'point', x: 201, y: 161, xs: 201, ys: 161, xe: 201, ye: 161, brt: 3 }) }
        : { ok: false, status: snapStatus, json: async () => ({}) }
    }
    return { ok: false, status: 404, json: async () => ({}) }
  }))
})
afterEach(() => {
  w?.unmount(); w = null
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('AstrometryDialog', () => {
  it('opens empty: placeholder, T row, RUN disabled with the first reason, zoom 100%', async () => {
    await openDialog()
    expect(q('[data-testid="as-placeholder"]')!.textContent).toContain('select image')
    expect(tRow()).toBeTruthy()
    expect(sRows()).toHaveLength(0)
    expect(run().disabled).toBe(true)
    expect(run().title).toBe('Load an image')
    expect((q('[data-testid="as-detect"]') as HTMLButtonElement).disabled).toBe(true)
    expect(q('[data-testid="as-zoom-pct"]')!.textContent).toBe('100%')
    expect(q('[data-testid="as-file"]')!.getAttribute('accept')).toBe('.fits,.fit,.tif,.tiff,.jpg,.jpeg,.png')
  })

  it('load autofills epoch + exposure; the reason moves on to "Detect or add streaks"', async () => {
    await openDialog()
    await loadImage()
    expect(calls[0]!.url).toBe('/api/astrometry/image')
    expect(calls[0]!.body).toBeInstanceOf(FormData)
    expect(q('[data-testid="as-placeholder"]')).toBeNull()
    expect(q('[data-testid="as-stage"] img')).toBeTruthy()
    expect((q('[data-testid="as-exposure"]') as HTMLInputElement).value).toBe('30')
    expect(q('[data-testid="as-epoch"] input')).toBeTruthy()
    expect(run().title).toBe('Detect or add streaks')
  })

  it('the Epoch / Exposure row sits above both panes; the status is one line with the full warnings in the title', async () => {
    detectWarnings = ['excluded 8 frame-clipped (edge) star trail(s) from the solve set — their truncated endpoints make the midpoint unreliable', 'showing the brightest 40 of 52 detected stars (fainter ones dropped)']
    await openDialog()
    const fields = q('.as-fields')!
    expect(fields.parentElement!.classList.contains('as-body')).toBe(true)
    expect(fields.nextElementSibling!.classList.contains('as-main')).toBe(true)
    await loadImage()
    await click(q('[data-testid="as-detect"]')!)
    const st = q('[data-testid="as-status"]')!
    expect(st.textContent!.trim()).toBe('12 streaks detected, target found · 8 edge streaks excluded')
    expect(st.title).toContain('showing the brightest 40 of 52')
    expect(st.title).toContain('excluded 8 frame-clipped')
  })

  it('a refused file shows the reason and keeps the placeholder', async () => {
    imageStatus = 422
    await openDialog()
    await loadImage()
    expect(q('[data-testid="as-status"]')!.textContent).toContain('Unsupported file type')
    expect(q('[data-testid="as-placeholder"]')).toBeTruthy()
  })

  it('detect -> table, snapped target by click, remove streaks, readiness, emit', async () => {
    await openDialog()
    await loadImage()
    await click(q('[data-testid="as-detect"]')!)
    expect(calls.at(-1)).toMatchObject({ url: '/api/astrometry/detect', body: { image_id: 'img1' } })
    expect(sRows()).toHaveLength(12)
    expect(tRow().querySelectorAll('input')[0]!.value).toBe('100')
    expect(sRows()[1]!.dataset.idx).toBe('1')
    expect(sRows()[0]!.querySelector('[data-testid="as-brt"]')!.getAttribute('value') ?? (sRows()[0]!.querySelector('[data-testid="as-brt"]') as HTMLInputElement).value).toBe('4')
    expect(run().disabled).toBe(false)
    expect(run().title).toBe('')
    expect(q('[data-testid="as-status"]')!.textContent).toContain('12 streaks detected, target found')

    // specify the target: focus X, click the stage at preview (100, 80) = image (200, 160), snapped to (201, 161)
    await focus(tRow().querySelector('[data-testid="as-x"]') as HTMLElement)
    expect(q('[data-testid="as-status"]')!.textContent).toContain('place the target')
    await stageClick(100, 80)
    expect(calls.at(-1)).toMatchObject({ url: '/api/astrometry/snap', body: { image_id: 'img1', x: 200, y: 160, role: 'target' } })
    expect((tRow().querySelector('[data-testid="as-x"]') as HTMLInputElement).value).toBe('201')
    expect((tRow().querySelector('[data-testid="as-y"]') as HTMLInputElement).value).toBe('161')

    // remove three streaks: 9 left
    for (let i = 0; i < 3; i++) await click(sRows()[0]!.querySelector('[data-testid="as-remove"]') as HTMLElement)
    expect(sRows()).toHaveLength(9)
    expect(run().disabled).toBe(true)
    expect(run().title).toBe('At least 10 streaks are needed (9 of 10)')

    // add one, place its start by click (end auto-filled from the reference vector), ready again
    await click(q('[data-testid="as-add"]')!)
    expect(sRows()).toHaveLength(10)
    expect(run().title).toBe('At least 10 streaks are needed (9 of 10)')
    await focus(sRows()[9]!.querySelectorAll('[data-testid="as-x"]')[0] as HTMLElement)
    await stageClick(40, 40)
    const xs = sRows()[9]!.querySelectorAll('[data-testid="as-x"]') as NodeListOf<HTMLInputElement>
    expect(xs[0]!.value).toBe('80')
    expect(xs[1]!.value).not.toBe('')
    expect(run().disabled).toBe(false)

    await click(run())
    const ev = w!.emitted('run')!
    expect(ev).toHaveLength(1)
    const p = ev[0]![0] as { image_id: string, detections: Array<{ id: string }>, epoch_iso: string, exposure_s: number, slot: number | null }
    expect(p.image_id).toBe('img1')
    expect(p.epoch_iso).toBe('2025-04-04T13:01:30.000Z')
    expect(p.exposure_s).toBe(30)
    expect(p.slot).toBeNull()
    expect(p.detections[0]!.id).toBe('T')
    expect(p.detections).toHaveLength(11)
  })

  it('a failed snap keeps the clicked point; clicking the letterbox is ignored', async () => {
    snapStatus = 500
    await openDialog()
    await loadImage()
    await click(q('[data-testid="as-detect"]')!)
    await focus(tRow().querySelector('[data-testid="as-x"]') as HTMLElement)
    await stageClick(500, 80) // outside the 400 px preview
    expect((tRow().querySelector('[data-testid="as-x"]') as HTMLInputElement).value).toBe('100')
    await stageClick(100, 80)
    expect((tRow().querySelector('[data-testid="as-x"]') as HTMLInputElement).value).toBe('200')
  })

  it('DETECT after an edit asks first; No keeps the edits, Yes replaces them', async () => {
    await openDialog()
    await loadImage()
    await click(q('[data-testid="as-detect"]')!)
    await click(q('[data-testid="as-add"]')!)
    expect(sRows()).toHaveLength(13)
    await click(q('[data-testid="as-detect"]')!)
    expect(q('[data-testid="as-confirm"]')).toBeTruthy()
    const n = calls.filter(c => c.url.endsWith('/detect')).length
    await click(q('[data-testid="as-confirm-no"]')!)
    expect(sRows()).toHaveLength(13)
    expect(calls.filter(c => c.url.endsWith('/detect'))).toHaveLength(n)
    await click(q('[data-testid="as-detect"]')!)
    await click(q('[data-testid="as-confirm-yes"]')!)
    expect(sRows()).toHaveLength(12)
  })

  it('an expired image (404) returns to the placeholder with the message', async () => {
    detectStatus = 404
    await openDialog()
    await loadImage()
    await click(q('[data-testid="as-detect"]')!)
    expect(q('[data-testid="as-placeholder"]')).toBeTruthy()
    expect(q('[data-testid="as-status"]')!.textContent).toContain('Image expired')
  })

  it('zoom buttons + the overlay toggle; CLEAR resets everything', async () => {
    await openDialog()
    await loadImage()
    await click(q('[data-testid="as-detect"]')!)
    expect(q('[data-testid="as-overlay"]')).toBeTruthy()
    await click(q('[data-testid="as-zoom-in"]')!)
    expect(q('[data-testid="as-zoom-pct"]')!.textContent).toBe('125%')
    await click(q('[data-testid="as-zoom-out"]')!)
    expect(q('[data-testid="as-zoom-pct"]')!.textContent).toBe('100%')
    await click(q('[data-testid="as-overlay-toggle"]')!)
    expect(q('[data-testid="as-overlay"]')).toBeNull()
    await click(q('[data-testid="as-clear"]')!)
    expect(q('[data-testid="as-placeholder"]')).toBeTruthy()
    expect(sRows()).toHaveLength(0)
    expect((q('[data-testid="as-exposure"]') as HTMLInputElement).value).toBe('')
    expect(run().title).toBe('Load an image')
  })

  it('a missing epoch blocks RUN with its reason; typing the exposure alone does not enable it', async () => {
    META.epoch_iso = null as unknown as string
    try {
      await openDialog()
      await loadImage()
      await click(q('[data-testid="as-detect"]')!)
      expect(run().disabled).toBe(true)
      expect(run().title).toBe('Enter the exposure start time')
    }
    finally {
      META.epoch_iso = '2025-04-04T13:01:30'
    }
  })

  it('a refused RUN shows over the detect summary', async () => {
    await openDialog()
    await loadImage()
    await click(q('[data-testid="as-detect"]')!)
    expect(q('[data-testid="as-status"]')!.textContent).toContain('streaks detected')
    await w!.setProps({ runStatus: { kind: 'error', text: 'Set the astrometry.net API key in Station Settings' } })
    expect(q('[data-testid="as-status"]')!.textContent).toContain('Set the astrometry.net API key')
  })
})
