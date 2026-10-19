import { beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AngleField from '../../app/components/widgets/AngleField.vue'
import DdInput from '../../app/components/widgets/DdInput.vue'
import DmsTriad from '../../app/components/widgets/DmsTriad.vue'
import { _resetSignGroupsForTests } from '../../app/composables/useSignGroup'

beforeEach(() => _resetSignGroupsForTests())

const MINUS = '−'
const PM: [string, string] = ['+', MINUS]
const S = 0.0001 // DD box step (display precision)

/** Mount with a live v-model so emitted values flow back in. */
function live(comp: object, props: Record<string, unknown> & { modelValue: number }) {
  const w = mount(comp, {
    props: { ...props, 'onUpdate:modelValue': (v: number) => w.setProps({ modelValue: v }) },
    attachTo: document.body,
  })
  return w
}
type W = ReturnType<typeof mount>
const last = (w: W) => w.emitted('update:modelValue')!.at(-1)![0] as number
const chipText = (w: W, i = 0) => w.findAll('.dms-sign-indicator')[i]!.text()

describe('sign chip - DdInput (signed El/Dec)', () => {
  it('shows + for positive and the minus sign for negative models', async () => {
    const w = live(DdInput, { modelValue: 10, cardinal: PM, range: [-90, 90], signGroup: 'a', step: S })
    expect(chipText(w)).toBe('+')
    await w.setProps({ modelValue: -10 })
    await w.vm.$nextTick()
    expect(w.find('.dms-sign-indicator').classes()).toContain('neg')
  })

  it('clicking the chip toggles the sign and emits the signed decimal', async () => {
    const w = live(DdInput, { modelValue: 12.5, cardinal: PM, range: [-90, 90], signGroup: 'b', step: S })
    await w.find('.dms-sign-indicator').trigger('click')
    expect(last(w)).toBe(-12.5)
    expect(chipText(w)).toBe(MINUS)
    await w.find('.dms-sign-indicator').trigger('click')
    expect(last(w)).toBe(12.5)
  })

  it('typing "-" toggles the chip (keydown is swallowed, box stays a magnitude)', async () => {
    const w = live(DdInput, { modelValue: 12.5, cardinal: PM, range: [-90, 90], signGroup: 'c', step: S })
    await w.find('input').trigger('keydown', { key: '-' })
    expect(last(w)).toBe(-12.5)
    expect((w.find('input').element as HTMLInputElement).value).toBe('12.5')
    await w.find('input').trigger('keydown', { key: '-' })
    expect(last(w)).toBe(12.5)
  })

  it('arrow stepping through zero flips the chip live (both directions)', async () => {
    const w = live(DdInput, { modelValue: 0.5, cardinal: PM, range: [-90, 90], signGroup: 'd', spinnerStep: 1, step: S })
    const input = w.find('input')
    await input.trigger('keydown', { key: 'ArrowDown' })
    expect(last(w)).toBeCloseTo(-0.5, 9)
    expect(chipText(w)).toBe(MINUS)
    await input.trigger('keydown', { key: 'ArrowUp' })
    expect(last(w)).toBeCloseTo(0.5, 9)
    expect(chipText(w)).toBe('+')
  })

  it('wheel (focused) through zero flips the chip', async () => {
    const w = live(DdInput, { modelValue: 0.25, cardinal: PM, range: [-90, 90], signGroup: 'e', spinnerStep: 1, step: S })
    const input = w.find('input')
    ;(input.element as HTMLInputElement).focus()
    await input.trigger('wheel', { deltaY: 100 })
    expect(last(w)).toBeCloseTo(-0.75, 9)
    expect(chipText(w)).toBe(MINUS)
  })

  it('an external model change to a negative value moves the chip', async () => {
    const w = live(DdInput, { modelValue: 10, cardinal: PM, range: [-90, 90], signGroup: 'ext', step: S })
    await w.setProps({ modelValue: -25 })
    await w.vm.$nextTick()
    expect(chipText(w)).toBe(MINUS)
    expect((w.find('input').element as HTMLInputElement).value).toBe('25')
  })

  it('toggling at exactly 0 never emits -0', async () => {
    const w = live(DdInput, { modelValue: 0, cardinal: PM, range: [-90, 90], signGroup: 'z', step: S })
    await w.find('.dms-sign-indicator').trigger('click')
    expect(Object.is(last(w), -0)).toBe(false)
  })

  it('unsigned kinds (Az) have no chip', () => {
    const w = mount(DdInput, { props: { modelValue: 10, range: [0, 360] } })
    expect(w.find('.dms-sign-indicator').exists()).toBe(false)
  })
})

describe('range caps - DdInput', () => {
  it('typing is clamped to the cap', async () => {
    const w = live(DdInput, { modelValue: 10, cardinal: PM, range: [0, 90], signGroup: 'f', step: S })
    const input = w.find('input')
    await input.setValue('95')
    expect((input.element as HTMLInputElement).value).toBe('90')
    await input.trigger('blur')
    expect(last(w)).toBe(90)
  })

  it('min >= 0 locks the chip to + (click and "-" do nothing)', async () => {
    const w = live(DdInput, { modelValue: 10, cardinal: PM, range: [0, 90], signGroup: 'g', step: S })
    const chip = w.find('.dms-sign-indicator')
    expect(chip.text()).toBe('+')
    expect(chip.classes()).toContain('locked')
    await chip.trigger('click')
    await w.find('input').trigger('keydown', { key: '-' })
    expect(w.emitted('update:modelValue')).toBeUndefined()
    expect(chipText(w)).toBe('+')
  })

  it('stepping and wheeling clamp at 0 and 90 and the chip stays +', async () => {
    const w = live(DdInput, { modelValue: 0.5, cardinal: PM, range: [0, 90], signGroup: 'h', spinnerStep: 1, step: S })
    const input = w.find('input')
    await input.trigger('keydown', { key: 'ArrowDown' })
    expect(last(w)).toBe(0)
    expect(chipText(w)).toBe('+')
    ;(input.element as HTMLInputElement).focus()
    await input.trigger('wheel', { deltaY: 100 })
    expect(last(w)).toBe(0)
    await w.setProps({ modelValue: 90 })
    await input.trigger('keydown', { key: 'ArrowUp' })
    expect(last(w)).toBe(90)
  })

  it('a pasted negative is dropped to its magnitude when locked', async () => {
    const w = live(DdInput, { modelValue: 10, cardinal: PM, range: [0, 90], signGroup: 'i', step: S })
    await w.find('input').setValue('-30')
    await w.find('input').trigger('blur')
    expect(last(w)).toBe(30)
  })

  it('a narrower asymmetric range caps the negative side too', async () => {
    const w = live(DdInput, { modelValue: -20, cardinal: PM, range: [-30, 60], signGroup: 'j', step: S })
    const input = w.find('input')
    await input.setValue('45')
    expect((input.element as HTMLInputElement).value).toBe('30')
    await input.trigger('blur')
    expect(last(w)).toBe(-30)
  })
})

describe('sign chip - DmsTriad', () => {
  it('chip toggles by click and "-" in the degrees box; model stays signed decimal degrees', async () => {
    const w = live(DmsTriad, { modelValue: 51.41, mode: 'limit:90', cardinal: PM, range: [-90, 90], signGroup: 'k' })
    expect(chipText(w)).toBe('+')
    await w.find('.dms-sign-indicator').trigger('click')
    expect(last(w)).toBeCloseTo(-51.41, 6)
    await w.findAll('input')[0]!.trigger('keydown', { key: '-' })
    expect(last(w)).toBeCloseTo(51.41, 6)
  })

  it('stepping seconds through zero flips the chip', async () => {
    const w = live(DmsTriad, { modelValue: 0, mode: 'limit:90', cardinal: PM, range: [-90, 90], signGroup: 'l' })
    await w.findAll('input')[2]!.trigger('keydown', { key: 'ArrowDown' })
    expect(chipText(w)).toBe(MINUS)
    expect(last(w)).toBeLessThan(0)
    await w.findAll('input')[2]!.trigger('keydown', { key: 'ArrowUp' })
    expect(chipText(w)).toBe('+')
  })

  it('min >= 0 shows a locked + chip; typed degrees clamp to the cap', async () => {
    const w = live(DmsTriad, { modelValue: 10, mode: 'limit:90', cardinal: PM, range: [0, 90], signGroup: 'm' })
    const chip = w.find('.dms-sign-indicator')
    expect(chip.text()).toBe('+')
    expect(chip.classes()).toContain('locked')
    await chip.trigger('click')
    expect(w.emitted('update:modelValue')).toBeUndefined()
    const deg = w.findAll('input')[0]!
    await deg.setValue('95')
    await deg.trigger('blur')
    expect(last(w)).toBe(90)
  })

  it('stepping down at 0 with min >= 0 stays 0 and +', async () => {
    const w = live(DmsTriad, { modelValue: 0, mode: 'limit:90', cardinal: PM, range: [0, 90], signGroup: 'n' })
    await w.findAll('input')[2]!.trigger('keydown', { key: 'ArrowDown' })
    expect(last(w)).toBe(0)
    expect(chipText(w)).toBe('+')
  })

  it('an asymmetric range caps the negative side when stepping', async () => {
    const w = live(DmsTriad, { modelValue: -30, mode: 'limit:90', cardinal: PM, range: [-30, 60], signGroup: 'o' })
    await w.findAll('input')[0]!.trigger('keydown', { key: 'ArrowDown' })
    expect(last(w)).toBe(-30)
  })
})

describe('sign chip - AngleField signed (El/Dec)', () => {
  it('signed adds the +/- chip in BOTH DD and DMS modes and shares one sign', async () => {
    const w = live(AngleField, { modelValue: 20, kind: 'angle', label: 'El', signed: true })
    const chips = w.findAll('.dms-sign-indicator')
    expect(chips).toHaveLength(2)
    expect(chips.map(c => c.text())).toEqual(['+', '+'])
    await chips[0]!.trigger('click')
    expect(last(w)).toBe(-20)
    expect(w.findAll('.dms-sign-indicator').map(c => c.text())).toEqual([MINUS, MINUS])
  })

  it('defaults to a +-90 range', async () => {
    const w = live(AngleField, { modelValue: 10, kind: 'angle', label: 'Dec', signed: true })
    const input = w.find('input')
    await input.setValue('120')
    expect((input.element as HTMLInputElement).value).toBe('90')
  })

  it('range [0, 90] locks both chips', () => {
    const w = live(AngleField, { modelValue: 10, kind: 'angle', label: 'El', signed: true, range: [0, 90] })
    const chips = w.findAll('.dms-sign-indicator')
    expect(chips.every(c => c.classes().includes('locked'))).toBe(true)
  })

  it('Az (unsigned) has no chip', () => {
    const w = live(AngleField, { modelValue: 10, kind: 'angle', label: 'Az', range: [0, 360] })
    expect(w.find('.dms-sign-indicator').exists()).toBe(false)
  })

  it('invalid adds the invalid class on both legs', () => {
    const w = live(AngleField, { modelValue: 10, kind: 'angle', label: 'El', signed: true, invalid: true })
    expect(w.findAll('.is-invalid').length).toBeGreaterThanOrEqual(2)
  })
})
