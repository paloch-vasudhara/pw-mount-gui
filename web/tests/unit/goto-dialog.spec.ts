// @vitest-environment happy-dom
// GotoDialog behaviour with the REAL AngleField widgets and a fake gateway: check/transform
// scheduling, active-group rules, format changes (T3.4a gate fixes).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import GotoDialog from '../../app/components/mount/GotoDialog.vue'
import { useMountStore } from '../../app/stores/mount'
import { _resetSignGroupsForTests } from '../../app/composables/useSignGroup'

interface FakeBody { cmd?: string, dir?: string, args: Record<string, number>, values: Record<string, number> }
const calls: Array<{ path: string, body: FakeBody }> = []
let latencyMs = 5
let transformMs = 5
let celestialRefused = false
let dynamic = false // transform result follows the request (regression: the dimmed pair must follow edits)

// Fake gateway. Sun pinned at az 152 / el 45: a target within 15 deg of it is refused (sun_target),
// a path to az < 140 passes near it (sun_path). Transform: a negative dec so the Dec sign flips.
function fakeApi() {
  return {
    post: async (path: string, body: FakeBody) => {
      calls.push({ path, body })
      await new Promise(r => setTimeout(r, path === '/api/transform' ? transformMs : latencyMs))
      if (path === '/api/mount/check') {
        if (body.cmd === 'goto_radec_j2000' && celestialRefused) return { ok: false, reason: 'Within 15° of the Sun', code: 'sun_target' }
        const az = body.args.az ?? 156
        if (body.cmd === 'goto_altaz' && Math.abs(az - 152) <= 15) return { ok: false, reason: 'Within 15° of the Sun', code: 'sun_target' }
        if (body.cmd === 'goto_altaz' && az < 140) return { ok: false, reason: 'Slew path passes within 15° of the Sun', code: 'sun_path' }
        return { ok: true }
      }
      if (body.dir === 'altaz_to_radec') {
        if (dynamic) return { result: { ra_h: (body.values.az ?? 0) / 15, dec_deg: -(body.values.az ?? 0) / 10 } }
        return { result: { ra_h: 22.543391, dec_deg: -28.253377 } }
      }
      if (dynamic) return { result: { alt: 20 + (body.values.ra_h ?? 0), az: 100 + (body.values.dec_deg ?? 0) } }
      return { result: { alt: 45.123456, az: 156.654321 } }
    },
  }
}
vi.mock('@/composables/useGatewayApi', () => ({
  GatewayError: class extends Error { status = 0 },
  useGatewayApi: () => fakeApi(),
}))


let w: VueWrapper | null = null
const body = () => document.body
const q = (sel: string) => body().querySelector(sel) as HTMLElement | null
const status = () => q('[data-testid="gt-status"]')!.textContent!.trim()
const isActive = (g: 'local' | 'celestial') => q(`[data-testid="gt-${g}"]`)!.classList.contains('gt-active')
const wait = async (ms: number) => { await vi.advanceTimersByTimeAsync(ms); await nextTick() }

// The Azimuth DD box: type a value then blur (Tab), as a user does.
async function typeAz(v: number) {
  const input = q('[data-testid="gt-local"] .ov-input-group:not(.hidden) input') as HTMLInputElement
  input.focus()
  input.value = String(v)
  input.dispatchEvent(new Event('input', { bubbles: true }))
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
  input.blur()
  await nextTick()
}

async function openDialog() {
  const store = useMountStore()
  store.snapshot = { state: 'TRACKING', mount: { az: 156, alt: 45, ra_j2000_h: 22.5, dec_j2000_deg: 10 } } as never
  w = mount(GotoDialog, { props: { gateReason: null, open: false }, attachTo: document.body, global: { stubs: {} } })
  await w.setProps({ open: true })
  await nextTick()
}

beforeEach(() => {
  setActivePinia(createPinia())
  _resetSignGroupsForTests()
  calls.length = 0
  latencyMs = 5
  transformMs = 5
  dynamic = false
  celestialRefused = false
  vi.useFakeTimers()
})
afterEach(() => {
  w?.unmount(); w = null
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('GotoDialog scheduling', () => {
  it('open: Local stays active and the first check lands within 0.5 s', async () => {
    await openDialog()
    await wait(500)
    expect(isActive('local')).toBe(true)
    expect(status()).toBe('Within 15° of the Sun')
    await wait(2500)
    expect(isActive('local')).toBe(true)
    expect(status()).toBe('Within 15° of the Sun')
  })

  it('shows the answer for the LAST typed input, not the previous one', async () => {
    await openDialog()
    await wait(500)
    await typeAz(100)
    await wait(2500)
    expect(status()).toBe('Slew path passes within 15° of the Sun')
    await typeAz(150)
    await wait(4000)
    expect(status()).toBe('Within 15° of the Sun')
    expect(isActive('local')).toBe(true)
    await typeAz(200)
    await wait(1500)
    expect(status()).toBe('Ready')
  })

  it('a format change on the dimmed group does not activate it nor re-check', async () => {
    await openDialog()
    await wait(1500)
    const n = calls.length
    const celestial = q('[data-testid="gt-celestial"]')!
    const sel = (w!.findAllComponents({ name: 'FormatSelect' }))[2]!
    sel.vm.$emit('update:modelValue', 'dd')
    await wait(10)
    expect(isActive('local')).toBe(true)
    expect(celestial.classList.contains('gt-active')).toBe(false)
    expect(calls.filter(c => c.body.cmd === 'goto_radec_j2000')).toHaveLength(0)
    void n
  })

  it('focusing and leaving a dimmed field without typing does not activate it', async () => {
    await openDialog()
    await wait(1500)
    const input = q('[data-testid="gt-celestial"] .ov-input-group:not(.hidden) input') as HTMLInputElement
    input.focus(); input.blur()
    await wait(600)
    expect(isActive('local')).toBe(true)
  })

  it('a slow transform never delays the check (first result < 0.5 s, newest answer shown)', async () => {
    transformMs = 4000 // the check answers in ms; the transform of the dimmed pair is slow
    await openDialog()
    await wait(500)
    expect(status()).toBe('Within 15° of the Sun')
    await typeAz(100)
    await wait(500)
    expect(status()).toBe('Slew path passes within 15° of the Sun')
    await typeAz(150)
    await wait(500)
    expect(status()).toBe('Within 15° of the Sun')
    expect(isActive('local')).toBe(true)
  })

  it('never shows the answer of the previous input: Checking... until the new answer arrives', async () => {
    latencyMs = 2000
    await openDialog()
    await wait(2100)
    expect(status()).toBe('Within 15° of the Sun')
    await typeAz(200)
    await wait(300) // debounce fired, answer still in flight
    expect(status()).toBe('Checking…')
    await wait(2100)
    expect(status()).toBe('Ready')
  })

  it('the 1 Hz refresh keeps running after a cycle was superseded', async () => {
    latencyMs = 1500
    await openDialog()
    await wait(300)
    await typeAz(100) // supersedes the open-time check while it is in flight
    await wait(6000)
    const n = calls.filter(c => c.path === '/api/mount/check').length
    await wait(4000)
    expect(calls.filter(c => c.path === '/api/mount/check').length).toBeGreaterThanOrEqual(n + 2)
  })

  it('a user edit of the dimmed group (typing, spinner) activates it; the widget re-emit of a computed value does not', async () => {
    await openDialog()
    await wait(1500)
    const input = q('[data-testid="gt-celestial"] .ov-input-group:not(.hidden) input') as HTMLInputElement
    input.focus()
    input.value = '23'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.blur()
    await wait(300)
    expect(isActive('celestial')).toBe(true)
    expect(calls.some(c => c.body.cmd === 'goto_radec_j2000')).toBe(true)
    // spinner click on Local
    const up = q('[data-testid="gt-local"] .ov-input-group:not(.hidden) .dms-spin-up') as HTMLElement
    up.click()
    await wait(300)
    expect(isActive('local')).toBe(true)
  })
})

describe('GotoDialog live update of the inactive group', () => {
  const inputsOf = (g: string) => Array.from(document.querySelectorAll(`[data-testid="gt-${g}"] .ov-input-group:not(.hidden) input`)) as HTMLInputElement[]
  const shown = (g: string) => inputsOf(g).map(i => i.value).join('|')

  it('Local edits: the Celestial fields follow every new transform result', async () => {
    dynamic = true
    await openDialog()
    await wait(1500)
    const seen: string[] = []
    for (const az of [195, 230, 120]) {
      await typeAz(az)
      await wait(1600)
      expect(isActive('local')).toBe(true)
      seen.push(shown('celestial'))
      expect(shown('celestial')).toMatch(new RegExp(`^${Math.floor(az / 15)}`))
    }
    expect(new Set(seen).size).toBe(3)
  })

  it('Celestial edits: the Local fields follow every new transform result', async () => {
    dynamic = true
    await openDialog()
    await wait(1500)
    const seen: string[] = []
    for (const ra of ['3', '5', '8']) {
      const input = inputsOf('celestial')[0]!
      input.focus()
      input.value = ra
      input.dispatchEvent(new Event('input', { bubbles: true }))
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
      input.blur()
      await nextTick()
      await wait(1600)
      expect(isActive('celestial')).toBe(true)
      seen.push(shown('local'))
    }
    expect(new Set(seen).size).toBe(3)
  })

  it('Local edits keep updating when the transform is slower than the 1 Hz tick', async () => {
    dynamic = true
    transformMs = 1500
    await openDialog()
    await wait(3000)
    const seen: string[] = []
    for (const az of [195, 230, 120]) {
      await typeAz(az)
      await wait(4500)
      seen.push(shown('celestial'))
    }
    expect(new Set(seen).size).toBe(3)
  })
})

describe('GotoDialog group activation by click', () => {
  const pointerDown = async (el: Element) => {
    el.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    await nextTick()
  }
  const allValues = () => Array.from(document.querySelectorAll('.goto-dialog input')).map(i => (i as HTMLInputElement).value).join('|')

  it('a pointerdown anywhere in the dimmed group (legend, format select, padding) activates it without changing any value, and re-checks', async () => {
    dynamic = true
    await openDialog()
    await wait(1500)
    for (const target of ['legend', '.ov-fmt-sel', '.ov-input-group input', null]) {
      // back to Local first
      await pointerDown(q('[data-testid="gt-local"] legend')!)
      await wait(1500)
      expect(isActive('local')).toBe(true)
      const before = allValues()
      const n = calls.filter(c => c.body.cmd === 'goto_radec_j2000').length
      const root = q('[data-testid="gt-celestial"]')!
      await pointerDown(target ? root.querySelector(target)! : root)
      expect(isActive('celestial')).toBe(true)
      expect(isActive('local')).toBe(false)
      expect(allValues()).toBe(before)
      await wait(10)
      expect(calls.filter(c => c.body.cmd === 'goto_radec_j2000').length).toBeGreaterThan(n)
    }
  })

  it('after activating Celestial the Local group shows the computed pair', async () => {
    dynamic = true
    await openDialog()
    await wait(1500)
    await pointerDown(q('[data-testid="gt-celestial"] legend')!)
    await wait(1600)
    const local = Array.from(document.querySelectorAll('[data-testid="gt-local"] .ov-input-group:not(.hidden) input')).map(i => (i as HTMLInputElement).value)
    expect(local.join('|')).not.toBe('')
    expect(calls.some(c => c.path === '/api/transform' && c.body.dir === 'radec_to_altaz')).toBe(true)
  })

  it('the Local Elevation box is red when the CELESTIAL target fails the check (dimmed group)', async () => {
    dynamic = true
    await openDialog()
    await wait(1500)
    await typeAz(200) // a Local target that passes (the open pointing is inside the fake Sun disc)
    await wait(1600)
    expect(q('[data-testid="gt-el"] .is-invalid')).toBeNull()
    celestialRefused = true
    await pointerDown(q('[data-testid="gt-celestial"] legend')!)
    await wait(1600)
    expect(status()).toBe('Within 15° of the Sun')
    expect(q('[data-testid="gt-local"].gt-dim [data-testid="gt-el"] .is-invalid, [data-testid="gt-local"].gt-dim .is-invalid')).not.toBeNull()
  })
})
