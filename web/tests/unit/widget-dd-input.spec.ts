// Ported from ssa-toolkit web/tests/unit/widget-dd-input.spec.ts @84f2ded
import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import DdInput from '../../app/components/widgets/DdInput.vue'
import { _resetSignGroupsForTests } from '../../app/composables/useSignGroup'

beforeEach(() => {
  _resetSignGroupsForTests()
})

describe('DdInput', () => {
  it('the box is type="number" (T10c — matches index.html\'s bare DD inputs; initDdInput never retypes it)', () => {
    const w = mount(DdInput, { props: { modelValue: 10, cardinal: ['N', 'S'], signGroup: 'g0' } })
    const input = w.find('input').element as HTMLInputElement
    expect(input.type).toBe('number')
  })

  it('DOWN in negative mode grows the displayed magnitude', async () => {
    const w = mount(DdInput, { props: { modelValue: -10, cardinal: ['N', 'S'], signGroup: 'g1' } })
    const input = w.find('input')
    await input.trigger('keydown', { key: 'ArrowDown' })
    const last = w.emitted('update:modelValue')!.at(-1)![0] as number
    expect(last).toBe(-11)
  })

  it('the box never displays a leading -', async () => {
    const w = mount(DdInput, { props: { modelValue: -10, cardinal: ['N', 'S'], signGroup: 'g2' } })
    const input = w.find('input').element as HTMLInputElement
    expect(input.value.startsWith('-')).toBe(false)
  })

  it('typing -45 writes 45 and sets the chip negative', async () => {
    const w = mount(DdInput, { props: { modelValue: 10, cardinal: ['N', 'S'], signGroup: 'g3' } })
    const input = w.find('input')
    await input.setValue('-45')
    const el = input.element as HTMLInputElement
    expect(el.value).toBe('45')
    expect(w.find('.dms-sign-indicator').classes()).toContain('neg')
  })

  it('blur on empty writes 0', async () => {
    const w = mount(DdInput, { props: { modelValue: 10, cardinal: ['N', 'S'], signGroup: 'g4' } })
    const input = w.find('input')
    await input.setValue('')
    await input.trigger('blur')
    const last = w.emitted('update:modelValue')!.at(-1)![0] as number
    expect(last).toBe(0)
  })

  it('a full-circle range (+-180) wraps instead of clamping and keeps +180 canonical', async () => {
    const w = mount(DdInput, { props: { modelValue: 180, cardinal: ['E', 'W'], range: [-180, 180], signGroup: 'g5' } })
    const input = w.find('input')
    await input.trigger('keydown', { key: 'ArrowUp' })
    const last = w.emitted('update:modelValue')!.at(-1)![0] as number
    expect(last).toBeCloseTo(-179, 5)
  })

  it('a +-90 range dims the correct arrow per sign', async () => {
    const w = mount(DdInput, { props: { modelValue: 90, cardinal: ['N', 'S'], range: [-90, 90], signGroup: 'g6' } })
    await w.vm.$nextTick()
    const up = w.find('.dms-spin-up')
    expect(up.attributes('disabled')).toBeDefined()
  })

  it('cycle mode (cycle:360) wraps on blur and never dims', async () => {
    const w = mount(DdInput, { props: { modelValue: 350, cycle: 360, spinnerStep: 20 } })
    const up = w.find('.dms-spin-up')
    const dn = w.find('.dms-spin-dn')
    expect(up.attributes('disabled')).toBeUndefined()
    expect(dn.attributes('disabled')).toBeUndefined()
    const input = w.find('input')
    await input.trigger('keydown', { key: 'ArrowUp' })
    const last = w.emitted('update:modelValue')!.at(-1)![0] as number
    expect(last).toBe(10)
  })
})
