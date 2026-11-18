// @vitest-environment happy-dom
// T6.1a: "Sun position unavailable" (the gateway has no ephemeris: snapshot.sun_known === false).
// The rules (chip, reason on every motion button), the Mount Status chip, and the Sky Viewer chip.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import MountStatusPanel from '../../app/components/mount/MountStatusPanel.vue'
import { useMountStore } from '../../app/stores/mount'
import {
  buildControlView, SUN_UNKNOWN_REASON, SUN_UNKNOWN_TITLE, sunUnavailableChip, sunUnknown,
  type ControlSnapshotLike,
} from '../../app/utils/mount-control'

vi.mock('../../app/components/mount/MountHemisphere3D.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ setup: () => () => h('div', { 'data-testid': 'mount-3d' }) }) }
})

const CONNECTED: ControlSnapshotLike = {
  state: 'STOPPED',
  pwi4: { reachable: true },
  mount: { connected: true, alt: 45 },
  axes: [{ enabled: true }, { enabled: true }],
  homed: { state: 'homed' },
}
const view = (s: ControlSnapshotLike) => buildControlView(s, { linkUp: true, holder: 'you', now: 0 })

describe('rules', () => {
  it('only an explicit false is "unknown" (an older gateway sends no field)', () => {
    expect(sunUnknown({ sun_known: false })).toBe(true)
    expect(sunUnknown({ sun_known: true })).toBe(false)
    expect(sunUnknown({})).toBe(false)
    expect(sunUnknown({ sun_known: null })).toBe(false)
    expect(sunUnknown(null)).toBe(false)
  })

  it('the chip text and title', () => {
    expect(sunUnavailableChip({ sun_known: false })).toEqual({
      show: true,
      text: 'Sun position unavailable',
      title: 'Ephemeris file missing: motion is disabled until data/ephemeris/de421.bsp is in place',
    })
    expect(SUN_UNKNOWN_TITLE).toBe(sunUnavailableChip({ sun_known: false }).title)
    expect(sunUnavailableChip({ sun_known: true }).show).toBe(false)
    expect(sunUnavailableChip(null).show).toBe(false)
  })

  it('the gateway reason text is mirrored', () => {
    expect(SUN_UNKNOWN_REASON).toBe('Sun position unavailable (ephemeris missing)')
  })

  it('TRACK, GOTO, JOG and HOME are disabled with the reason; CONNECT / DISCONNECT / STOP are not', () => {
    const v = view({ ...CONNECTED, sun_known: false })
    for (const b of [v.track, v.goto, v.jog, v.home]) {
      expect(b).toEqual({ enabled: false, reason: SUN_UNKNOWN_REASON })
    }
    expect(v.disconnect.enabled).toBe(true)
    expect(v.stopEnabled).toBe(true)
    const off = buildControlView({ ...CONNECTED, mount: { connected: false }, sun_known: false }, { linkUp: true, holder: 'you', now: 0 })
    expect(off.connect.enabled).toBe(true)
  })

  it('with the Sun known (or no field) the buttons are enabled as before', () => {
    for (const s of [{ ...CONNECTED, sun_known: true }, CONNECTED]) {
      const v = view(s)
      expect([v.track, v.goto, v.jog, v.home].every(b => b.enabled && b.reason === null)).toBe(true)
    }
  })

  it('the status line explains the TRACK refusal', () => {
    expect(view({ ...CONNECTED, sun_known: false }).line?.text).toBe(`TRACK: ${SUN_UNKNOWN_REASON}`)
  })
})

describe('Mount Status chip', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ configured: false }) }) as Response))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it('shows the red chip instead of the Sun Distance value, and hides it again', async () => {
    const store = useMountStore()
    store.snapshot = { state: 'STOPPED', sun_distance_deg: null, sun_known: false } as never
    const w = mount(MountStatusPanel)
    await flushPromises()
    const chip = w.get('[data-testid="mount-sun-unavailable"]')
    expect(chip.text()).toBe('Sun position unavailable')
    expect(chip.attributes('title')).toBe(SUN_UNKNOWN_TITLE)
    expect(chip.classes()).toContain('text-danger')
    expect(w.find('[data-testid="mount-sun"]').exists()).toBe(false)
    store.snapshot = { state: 'STOPPED', sun_distance_deg: 90, sun_known: true } as never
    await flushPromises()
    expect(w.find('[data-testid="mount-sun-unavailable"]').exists()).toBe(false)
    expect(w.find('[data-testid="mount-sun"]').exists()).toBe(true)
    w.unmount()
  })

  it('an older gateway (no field) shows no chip', async () => {
    const store = useMountStore()
    store.snapshot = { state: 'STOPPED', sun_distance_deg: 90 } as never
    const w = mount(MountStatusPanel)
    await flushPromises()
    expect(w.find('[data-testid="mount-sun-unavailable"]').exists()).toBe(false)
    w.unmount()
  })
})

describe('Sky Viewer chip', () => {
  // SkyChart needs a canvas / WebGL-free environment it cannot get here: the template is checked.
  const src = readFileSync(resolve(__dirname, '../../app/components/sky/SkyChart.vue'), 'utf8')
  const strip = src.slice(src.indexOf('<div v-if="simBadge'))

  it('the Sun chip sits in the ADS-B chip strip, in front of the feed-down chip, in the same red style', () => {
    const container = strip.slice(0, strip.indexOf('</div>'))
    expect(container).toContain('class="sky-chips"')
    const sun = container.indexOf('data-testid="sun-unavailable"')
    const feed = container.indexOf('data-testid="adsb-feed-down"')
    const sim = container.indexOf('data-testid="adsb-simulated"')
    expect(sun).toBeGreaterThan(-1)
    expect(sun).toBeLessThan(feed)
    expect(feed).toBeLessThan(sim)
    const line = container.slice(container.lastIndexOf('<span', sun), container.indexOf('</span>', sun))
    expect(line).toContain('sky-chip sky-chip-down')
    expect(line).toContain(':title="sunMissing.title"')
    expect(line).toContain('v-if="sunMissing.show"')
  })

  it('the strip is shown for the Sun chip alone and wraps (flex-wrap) so chips never overlap', () => {
    expect(strip.slice(0, 120)).toContain('simBadge || feedDown.show || connecting.show || sunMissing.show')
    expect(src).toMatch(/\.sky-chips \{[^}]*display: flex; flex-wrap: wrap;/)
  })
})
