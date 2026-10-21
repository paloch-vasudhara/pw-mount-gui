// @vitest-environment happy-dom
// T1.2: station-settings logic (validation, payload shape, mismatch text) + dialog behaviour.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import StationSettingsDialog from '../../app/components/settings/StationSettingsDialog.vue'
import {
  buildPayload, buildSiteCheckBody, formFromDto, isDirty, siteWarning, validateForm,
  type SettingsResponse, type StationSettingsDto,
} from '../../app/utils/station-settings'

const ZONES = ['UTC', 'Asia/Bangkok', 'Europe/London']

const DTO: StationSettingsDto = {
  version: 1,
  location: { lat_deg: 13.7862, lon_deg: 101.4639, alt_m: 40.8, timezone: 'Asia/Bangkok' },
  hardware: { mount: 'PW L-350', ota: 'Celestron RASA11', lim_el_deg: 15 },
  providers: { astrometry: 'astrometry.net', satellite_db: 'DELV Aerospace' },
  display: { lat_format: 'dd', lon_format: 'dd', alt_unit: 'm' },
}

describe('station-settings utils', () => {
  it('a valid form has no errors', () => {
    expect(validateForm(formFromDto(DTO), ZONES)).toEqual({})
  })

  it('flags each out-of-range field', () => {
    const f = formFromDto(DTO)
    f.lat = 91
    f.lon = -181
    f.altM = 9001
    f.limEl = 90.5
    f.timezone = 'Mars/Base'
    expect(Object.keys(validateForm(f, ZONES)).sort()).toEqual(['alt', 'lat', 'limEl', 'lon', 'timezone'])
    f.lat = null
    f.limEl = null
    expect(validateForm(f, ZONES).lat).toBeTruthy()
    expect(validateForm(f, ZONES).limEl).toBeTruthy()
  })

  it('accepts the range edges', () => {
    const f = formFromDto(DTO)
    f.lat = -90
    f.lon = 180
    f.altM = -500
    f.limEl = 0
    expect(validateForm(f, ZONES)).toEqual({})
    f.altM = 9000
    f.limEl = 90
    expect(validateForm(f, ZONES)).toEqual({})
  })

  it('payload omits api_key when untouched, sends the typed one, "" when cleared', () => {
    const f = formFromDto(DTO)
    expect('api_key' in buildPayload(f)).toBe(false)
    f.apiKey = 'abc'
    expect(buildPayload(f).api_key).toBe('abc')
    f.apiKey = ''
    f.clearKey = true
    expect(buildPayload(f).api_key).toBe('')
  })

  it('payload shape matches the gateway schema', () => {
    const p = buildPayload(formFromDto(DTO))
    expect(p).toEqual(DTO)
  })

  it('site warning only when ok is false', () => {
    const base = {
      pwi4_reachable: true, lat_diff_deg: -0.5618, lon_diff_deg: 0.8983, alt_diff_m: 28.8,
      tolerance: { lat_lon_deg: 0.01, alt_m: 100 },
    }
    expect(siteWarning({ ...base, ok: true })).toBeNull()
    expect(siteWarning({ ...base, ok: null })).toBeNull()
    expect(siteWarning(undefined)).toBeNull()
    const w = siteWarning({ ...base, ok: false })!
    expect(w).toContain('differs from PWI4')
    expect(w).toContain('-0.5618')
    expect(w).toContain('+0.8983')
    expect(w).toContain('+28.8 m')
  })
})

describe('isDirty', () => {
  it('unchanged form is clean', () => {
    expect(isDirty(formFromDto(DTO), formFromDto(DTO))).toBe(false)
  })
  it('any edited field, a typed key, clear-key and a format-only change are dirty', () => {
    const base = formFromDto(DTO)
    const edit = (fn: (f: ReturnType<typeof formFromDto>) => void) => {
      const f = formFromDto(DTO)
      fn(f)
      return isDirty(f, base)
    }
    expect(edit((f) => { f.lat = 13.5 })).toBe(true)
    expect(edit((f) => { f.limEl = 20 })).toBe(true)
    expect(edit((f) => { f.timezone = 'UTC' })).toBe(true)
    expect(edit((f) => { f.latFormat = 'dms' })).toBe(true)
    expect(edit((f) => { f.altUnit = 'ft' })).toBe(true)
    expect(edit((f) => { f.apiKey = 'k' })).toBe(true)
    expect(edit((f) => { f.clearKey = true })).toBe(true)
    expect(edit((f) => { f.lat = 13.7862 + 1e-9 })).toBe(false)
  })
  it('site-check body is just the proposed location', () => {
    expect(buildSiteCheckBody(formFromDto(DTO))).toEqual({ lat_deg: 13.7862, lon_deg: 101.4639, alt_m: 40.8 })
  })
})

describe('StationSettingsDialog', () => {
  const calls: { method: string, url: string, body?: string, headers?: Record<string, string> }[] = []
  let putResponse: SettingsResponse
  let apiKeySet = false
  let checkResponse: Record<string, unknown> = { pwi4_reachable: false, ok: null }

  function q(testid: string): HTMLElement | null {
    return document.body.querySelector(`[data-testid="${testid}"]`)
  }

  async function typeKey(v = 'KEY-1') {
    const input = q('ss-apikey') as HTMLInputElement
    input.value = v
    input.dispatchEvent(new Event('input'))
    await flushPromises()
  }

  async function editLat(v: string) {
    const input = document.body.querySelector('[data-testid="ss-lat"] input') as HTMLInputElement
    input.value = v
    input.dispatchEvent(new Event('input'))
    input.dispatchEvent(new Event('change'))
    input.dispatchEvent(new Event('blur'))
    await flushPromises()
  }

  async function openDialog() {
    const w = mount(StationSettingsDialog, { props: { open: true }, attachTo: document.body })
    await flushPromises()
    await flushPromises()
    return w
  }

  beforeEach(() => {
    calls.length = 0
    apiKeySet = false
    checkResponse = { pwi4_reachable: false, ok: null }
    putResponse = { ...DTO, api_key_set: false }
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      calls.push({ method, url, body: init?.body as string, headers: init?.headers as Record<string, string> })
      const json = (o: unknown) => ({ ok: true, status: 200, json: async () => o }) as Response
      if (url === '/api/mount/session') return json({ token: 'tok' })
      if (method === 'POST') return json(checkResponse)
      if (method === 'PUT') return json(putResponse)
      return json({ ...DTO, api_key_set: apiKeySet })
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it('loads settings with the token header and renders the groups', async () => {
    const w = await openDialog()
    expect(q('station-settings-dialog')).not.toBeNull()
    expect(q('ss-location')).not.toBeNull()
    expect(q('ss-providers')).not.toBeNull()
    expect(q('ss-hardware')).not.toBeNull()
    expect(document.body.textContent).toContain('Section awaiting protocol template')
    const get = calls.find(c => c.url === '/api/mount/settings')!
    expect(get.headers!['X-Mount-Token']).toBe('tok')
    w.unmount()
  })

  it('Save is disabled when unchanged; after a change it checks, PUTs and closes', async () => {
    const w = await openDialog()
    const save = q('ss-save') as HTMLButtonElement
    expect(save.disabled).toBe(true) // nothing changed yet
    await typeKey()
    expect(save.disabled).toBe(false)
    save.click()
    await flushPromises()
    const put = calls.find(c => c.method === 'PUT')!
    expect(put.url).toBe('/api/mount/settings')
    const body = JSON.parse(put.body!)
    expect(body.api_key).toBe('KEY-1')
    expect(body.location.lat_deg).toBeCloseTo(13.7862)
    expect(w.emitted('update:open')!.at(-1)).toEqual([false])
    w.unmount()
  })

  it('typed API key is sent once and never rendered back', async () => {
    const w = await openDialog()
    await typeKey('SECRET-KEY-XYZ')
    putResponse = { ...DTO, api_key_set: true }
    ;(q('ss-save') as HTMLButtonElement).click()
    await flushPromises()
    expect(JSON.parse(calls.find(c => c.method === 'PUT')!.body!).api_key).toBe('SECRET-KEY-XYZ')
    expect(document.body.innerHTML).not.toContain('SECRET-KEY-XYZ')
    w.unmount()
  })

  it('placeholder shows "set" when a key exists; the box is empty', async () => {
    apiKeySet = true
    const w = await openDialog()
    const input = q('ss-apikey') as HTMLInputElement
    expect(input.value).toBe('')
    expect(input.placeholder).toContain('set')
    expect(q('ss-clearkey')).not.toBeNull()
    w.unmount()
  })

  const MISMATCH = {
    pwi4_reachable: true, ok: false, lat_diff_deg: -0.56, lon_diff_deg: 0.9, alt_diff_m: 28.8,
    tolerance: { lat_lon_deg: 0.01, alt_m: 100 },
  }

  it('mismatch: first SAVE warns without saving, buttons stay, second SAVE saves and closes', async () => {
    checkResponse = MISMATCH
    const w = await openDialog()
    await typeKey()
    ;(q('ss-save') as HTMLButtonElement).click()
    await flushPromises()
    expect(q('ss-warning')!.textContent).toContain('differs from PWI4')
    expect(calls.filter(c => c.method === 'POST')).toHaveLength(1)
    expect(JSON.parse(calls.find(c => c.method === 'POST')!.body!)).toEqual({ lat_deg: 13.7862, lon_deg: 101.4639, alt_m: 40.8 })
    expect(calls.some(c => c.method === 'PUT')).toBe(false)
    expect(w.emitted('update:open')).toBeUndefined()
    expect(q('ss-close')).toBeNull()
    expect(q('ss-cancel')).not.toBeNull()
    expect((q('ss-save') as HTMLButtonElement).disabled).toBe(false)
    ;(q('ss-save') as HTMLButtonElement).click()
    await flushPromises()
    expect(calls.filter(c => c.method === 'POST')).toHaveLength(1) // acknowledged: no second check
    expect(calls.some(c => c.method === 'PUT')).toBe(true)
    expect(w.emitted('update:open')!.at(-1)).toEqual([false])
    w.unmount()
  })

  it('editing a Location field clears the warning and the acknowledgement', async () => {
    checkResponse = MISMATCH
    const w = await openDialog()
    await editLat('13.5')
    ;(q('ss-save') as HTMLButtonElement).click()
    await flushPromises()
    expect(q('ss-warning')).not.toBeNull()
    await editLat('13.6')
    expect(q('ss-warning')).toBeNull()
    ;(q('ss-save') as HTMLButtonElement).click()
    await flushPromises()
    expect(calls.filter(c => c.method === 'POST')).toHaveLength(2) // checked again
    expect(q('ss-warning')).not.toBeNull()
    expect(calls.some(c => c.method === 'PUT')).toBe(false)
    w.unmount()
  })

  it('PWI4 unreachable: PUT and close at once', async () => {
    const w = await openDialog()
    await typeKey()
    ;(q('ss-save') as HTMLButtonElement).click()
    await flushPromises()
    expect(calls.some(c => c.method === 'PUT')).toBe(true)
    expect(q('ss-warning')).toBeNull()
    expect(w.emitted('update:open')!.at(-1)).toEqual([false])
    w.unmount()
  })

  it('null timezone offers the PC zone; a saved UTC is respected', async () => {
    const pc = Intl.DateTimeFormat().resolvedOptions().timeZone
    const orig = DTO.location.timezone
    DTO.location.timezone = null
    let w = await openDialog()
    expect(q('tz-trigger')!.textContent!.trim()).toBe(pc)
    w.unmount()
    DTO.location.timezone = 'UTC'
    w = await openDialog()
    expect(q('tz-trigger')!.textContent!.trim()).toBe('UTC')
    w.unmount()
    DTO.location.timezone = orig
  })

  it('Cancel closes without a PUT', async () => {
    const w = await openDialog()
    ;(q('ss-cancel') as HTMLButtonElement).click()
    await flushPromises()
    expect(calls.some(c => c.method === 'PUT')).toBe(false)
    expect(w.emitted('update:open')!.at(-1)).toEqual([false])
    w.unmount()
  })
})
