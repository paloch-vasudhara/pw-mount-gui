// Ported from ssa-toolkit web/tests/unit/widget-dms-triad.spec.ts @84f2ded
import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import DmsTriad from '../../app/components/widgets/DmsTriad.vue'
import { _resetSignGroupsForTests } from '../../app/composables/useSignGroup'

beforeEach(() => {
  _resetSignGroupsForTests()
})

describe('DmsTriad — markup parity', () => {
  it('renders 3 .dms-box-wrap, exactly 1 .dms-sign-indicator on the deg box, 6 spinner buttons all tabindex=-1', () => {
    const w = mount(DmsTriad, {
      props: { modelValue: 45, mode: 'limit:90', cardinal: ['N', 'S'] },
    })
    expect(w.findAll('.dms-box-wrap').length).toBe(3)
    expect(w.findAll('.dms-sign-indicator').length).toBe(1)
    expect(w.find('.dms-box-wrap-cardinal .dms-sign-indicator').exists()).toBe(true)
    const buttons = w.findAll('.dms-spin-up, .dms-spin-dn')
    expect(buttons.length).toBe(6)
    for (const b of buttons) expect(b.attributes('tabindex')).toBe('-1')
  })
})

describe('DmsTriad — stepping', () => {
  it('ArrowUp on the seconds input emits an updated model', async () => {
    const w = mount(DmsTriad, { props: { modelValue: 10, mode: 'limit:90', cardinal: ['N', 'S'] } })
    const sec = w.findAll('.ov-dms-box')[2]
    await sec.trigger('keydown', { key: 'ArrowUp' })
    expect(w.emitted('update:modelValue')).toBeTruthy()
    const last = w.emitted('update:modelValue')!.at(-1)![0] as number
    expect(last).toBeCloseTo(10 + 0.1 / 3600, 6)
  })
  it('wheel while focused steps; wheel while not focused does nothing', async () => {
    const w = mount(DmsTriad, { props: { modelValue: 10, mode: 'limit:90', cardinal: ['N', 'S'] }, attachTo: document.body })
    const deg = w.findAll('.ov-dms-box')[0]
    // Not focused: no emit.
    await deg.trigger('wheel', { deltaY: -1 })
    expect(w.emitted('update:modelValue')).toBeFalsy()
    // Focused: steps.
    ;(deg.element as HTMLInputElement).focus()
    await deg.trigger('wheel', { deltaY: -1 })
    expect(w.emitted('update:modelValue')).toBeTruthy()
    w.unmount()
  })
})

describe('DmsTriad — sign chip', () => {
  it('clicking the chip flips the sign AND the chip text N -> S with class neg', async () => {
    const w = mount(DmsTriad, { props: { modelValue: 10, mode: 'limit:90', cardinal: ['N', 'S'] } })
    const chip = w.find('.dms-sign-indicator')
    expect(chip.text()).toBe('N')
    await chip.trigger('click')
    expect(chip.text()).toBe('S')
    expect(chip.classes()).toContain('neg')
  })
  it('two triads sharing a signGroup: flipping one flips the others chip', async () => {
    const a = mount(DmsTriad, { props: { modelValue: 10, mode: 'limit:90', cardinal: ['N', 'S'], signGroup: 'shared-1' } })
    const b = mount(DmsTriad, { props: { modelValue: 20, mode: 'limit:90', cardinal: ['N', 'S'], signGroup: 'shared-1' } })
    await a.find('.dms-sign-indicator').trigger('click')
    expect(a.find('.dms-sign-indicator').text()).toBe('S')
    expect(b.find('.dms-sign-indicator').text()).toBe('S')
  })
})

describe('DmsTriad — boundary freeze', () => {
  it('at the boundary the min/sec inputs gain readonly', async () => {
    const w = mount(DmsTriad, { props: { modelValue: 90, mode: 'limit:90', cardinal: ['N', 'S'] } })
    await w.vm.$nextTick()
    const min = w.findAll('.ov-dms-box')[1]
    const sec = w.findAll('.ov-dms-box')[2]
    expect(min.attributes('readonly')).toBeDefined()
    expect(sec.attributes('readonly')).toBeDefined()
  })
})

describe('DmsTriad — character restriction', () => {
  it('beforeinput carrying "a" is prevented', () => {
    const w = mount(DmsTriad, { props: { modelValue: 10, mode: 'limit:90', cardinal: ['N', 'S'] } })
    const deg = w.findAll('.ov-dms-box')[0].element as HTMLInputElement
    const evt = new InputEvent('beforeinput', { data: 'a', cancelable: true })
    deg.dispatchEvent(evt)
    expect(evt.defaultPrevented).toBe(true)
  })
  it('"-" is allowed on a cardinal deg box', () => {
    const w = mount(DmsTriad, { props: { modelValue: 10, mode: 'limit:90', cardinal: ['N', 'S'] } })
    const deg = w.findAll('.ov-dms-box')[0].element as HTMLInputElement
    const evt = new InputEvent('beforeinput', { data: '-', cancelable: true })
    deg.dispatchEvent(evt)
    expect(evt.defaultPrevented).toBe(false)
  })
  it('"-" is prevented on an El-ranged (range:[0,90]) deg box', () => {
    const w = mount(DmsTriad, { props: { modelValue: 10, mode: 'limit:90', cardinal: ['N', 'S'], range: [0, 90] } })
    const deg = w.findAll('.ov-dms-box')[0].element as HTMLInputElement
    const evt = new InputEvent('beforeinput', { data: '-', cancelable: true })
    deg.dispatchEvent(evt)
    expect(evt.defaultPrevented).toBe(true)
  })
})

describe('DmsTriad — widths (T10c)', () => {
  it('defaults to the lat/lon shape (deg 56 / min 42 / sec 64px)', () => {
    const w = mount(DmsTriad, { props: { modelValue: 10, mode: 'limit:90', cardinal: ['N', 'S'] } })
    const [deg, min, sec] = w.findAll('.ov-dms-box')
    expect((deg.element as HTMLInputElement).style.width).toBe('56px')
    expect((min.element as HTMLInputElement).style.width).toBe('42px')
    expect((sec.element as HTMLInputElement).style.width).toBe('64px')
  })
  it('applies an explicit widths prop (e.g. the Imaging Simulator 47/38/60 shape)', () => {
    const w = mount(DmsTriad, {
      props: { modelValue: 10, mode: 'limit:90', range: [0, 90], widths: { deg: 47, min: 38, sec: 60 } },
    })
    const [deg, min, sec] = w.findAll('.ov-dms-box')
    expect((deg.element as HTMLInputElement).style.width).toBe('47px')
    expect((min.element as HTMLInputElement).style.width).toBe('38px')
    expect((sec.element as HTMLInputElement).style.width).toBe('60px')
  })
})

describe('DmsTriad — typing vs blur', () => {
  it('typing does not emit per keystroke; blur emits once', async () => {
    const w = mount(DmsTriad, { props: { modelValue: 10, mode: 'limit:90', cardinal: ['N', 'S'] } })
    const deg = w.findAll('.ov-dms-box')[0]
    await deg.setValue('5')
    expect(w.emitted('update:modelValue')).toBeFalsy()
    await deg.trigger('blur')
    expect(w.emitted('update:modelValue')?.length).toBe(1)
  })
})
