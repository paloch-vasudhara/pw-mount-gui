// Ported from ssa-toolkit web/tests/unit/widget-number-field.spec.ts @84f2ded
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import NumberField from '../../app/components/widgets/NumberField.vue'

describe('NumberField', () => {
  it('step respects `step` and rounds to decimalsForStep (0.1 steps produce 0.3, not 0.30000000000000004)', async () => {
    const w = mount(NumberField, { props: { modelValue: 0.2, step: 0.1 } })
    await w.find('.dms-spin-up').trigger('click')
    const last = w.emitted('update:modelValue')!.at(-1)![0]
    expect(last).toBe(0.3)
  })

  it('clamps at min/max', async () => {
    const w = mount(NumberField, { props: { modelValue: 9, min: 0, max: 10, step: 1 } })
    await w.find('.dms-spin-up').trigger('click')
    await w.find('.dms-spin-up').trigger('click')
    const last = w.emitted('update:modelValue')!.at(-1)![0]
    expect(last).toBe(10)
  })

  it('cycle wraps and never dims', async () => {
    const w = mount(NumberField, { props: { modelValue: 350, min: 0, max: 360, step: 20, cycle: true } })
    expect(w.find('.dms-spin-up').attributes('disabled')).toBeUndefined()
    await w.find('.dms-spin-up').trigger('click')
    const last = w.emitted('update:modelValue')!.at(-1)![0]
    expect(last).toBe(10)
  })

  it('an empty field seeds from min (else 0) on the first step', async () => {
    const w = mount(NumberField, { props: { modelValue: null, min: 5, step: 1 } })
    await w.find('.dms-spin-up').trigger('click')
    const last = w.emitted('update:modelValue')!.at(-1)![0]
    expect(last).toBe(6)
  })

  it('ArrowUp/Down and focused-wheel step; unfocused wheel does not', async () => {
    const w = mount(NumberField, { props: { modelValue: 5, step: 1 }, attachTo: document.body })
    const input = w.find('input')
    await input.trigger('wheel', { deltaY: -1 })
    expect(w.emitted('update:modelValue')).toBeFalsy()
    ;(input.element as HTMLInputElement).focus()
    await input.trigger('wheel', { deltaY: -1 })
    expect(w.emitted('update:modelValue')).toBeTruthy()
    w.unmount()
  })

  it('fixedWidth puts the width on the wrap and not the input', () => {
    const w = mount(NumberField, { props: { modelValue: 5, fixedWidth: '80px' } })
    const wrap = w.find('.num-spin-wrap')
    expect((wrap.element as HTMLElement).style.width).toBe('80px')
    expect((w.find('input').element as HTMLInputElement).style.width).toBe('')
  })

  it('the change event fires alongside the model update', async () => {
    const w = mount(NumberField, { props: { modelValue: 5, step: 1 } })
    await w.find('.dms-spin-up').trigger('click')
    expect(w.emitted('change')).toBeTruthy()
    expect(w.emitted('update:modelValue')).toBeTruthy()
  })

  // ── T15-6 signed-spinner mode (mnv-tool.js signedVal/setSignedVal/
  //    _flipSign/_componentStep/_onComponentCommit) ──────────────────────
  describe('signed mode', () => {
    it('renders a magnitude in the box and a "+" chip for a positive value', () => {
      const w = mount(NumberField, { props: { modelValue: 5, signed: true } })
      expect((w.find('input').element as HTMLInputElement).value).toBe('5.000')
      expect(w.find('.mnv-sign').text()).toBe('+')
      expect(w.find('.mnv-sign').classes()).not.toContain('neg')
    })

    it('renders a magnitude in the box and a "−" chip for a negative value', () => {
      const w = mount(NumberField, { props: { modelValue: -5, signed: true } })
      expect((w.find('input').element as HTMLInputElement).value).toBe('5.000')
      expect(w.find('.mnv-sign').text()).toBe('−')
      expect(w.find('.mnv-sign').classes()).toContain('neg')
    })

    it('clicking the chip flips the sign of the emitted SIGNED total, magnitude unchanged', async () => {
      const w = mount(NumberField, { props: { modelValue: 5, signed: true } })
      await w.find('.mnv-sign').trigger('click')
      expect(w.emitted('update:modelValue')!.at(-1)![0]).toBe(-5)
      expect((w.find('input').element as HTMLInputElement).value).toBe('5.000')
    })

    it('typing a negative magnitude and blurring flips the chip and normalizes the box to |v| (_onComponentCommit)', async () => {
      const w = mount(NumberField, { props: { modelValue: 5, signed: true } })
      const input = w.find('input')
      await input.setValue('-3')
      await input.trigger('blur')
      expect(w.emitted('update:modelValue')!.at(-1)![0]).toBe(-3)
      expect((input.element as HTMLInputElement).value).toBe('3.000')
      expect(w.find('.mnv-sign').text()).toBe('−')
    })

    it('stepping up crosses zero and flips the chip live while the box keeps a magnitude (_componentStep)', async () => {
      const w = mount(NumberField, { props: { modelValue: -0.5, step: 1, signed: true } })
      await w.find('.dms-spin-up').trigger('click')
      // signed total: -0.5 + 1 = 0.5 -> positive
      expect(w.emitted('update:modelValue')!.at(-1)![0]).toBe(0.5)
      expect(w.find('.mnv-sign').text()).toBe('+')
      expect((w.find('input').element as HTMLInputElement).value).toBe('0.500')
    })

    it('a readonly signed box still allows the chip to flip (locked preset direction reversal)', async () => {
      const w = mount(NumberField, { props: { modelValue: 1, signed: true, readonly: true } })
      await w.find('.mnv-sign').trigger('click')
      expect(w.emitted('update:modelValue')!.at(-1)![0]).toBe(-1)
    })

    it('a disabled signed box does not allow the chip to flip', async () => {
      const w = mount(NumberField, { props: { modelValue: 1, signed: true, disabled: true } })
      await w.find('.mnv-sign').trigger('click')
      expect(w.emitted('update:modelValue')).toBeFalsy()
    })
  })
})
