// Ported from ssa-toolkit web/tests/unit/widget-seg-toggle.spec.ts @84f2ded
// @vitest-environment happy-dom
// tests/unit/widget-seg-toggle.spec.ts — T15-0b.
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import SegToggle from '../../app/components/widgets/SegToggle.vue'

const options = [
  { value: 'catalog', label: 'Catalog' },
  { value: 'pair', label: 'Pair' },
]

describe('SegToggle.vue', () => {
  it('renders legacy DOM: .vt-seg > button[data-val] per option', () => {
    const wrapper = mount(SegToggle, { props: { modelValue: 'catalog', options } })
    expect(wrapper.find('.vt-seg').exists()).toBe(true)
    const buttons = wrapper.findAll('button')
    expect(buttons.length).toBe(2)
    expect(buttons[0]!.attributes('data-val')).toBe('catalog')
    expect(buttons[1]!.attributes('data-val')).toBe('pair')
  })

  it('the active class tracks the model', () => {
    const wrapper = mount(SegToggle, { props: { modelValue: 'pair', options } })
    const buttons = wrapper.findAll('button')
    expect(buttons[0]!.classes()).not.toContain('active')
    expect(buttons[1]!.classes()).toContain('active')
  })

  it('click emits update:modelValue and change with the clicked value', async () => {
    const wrapper = mount(SegToggle, { props: { modelValue: 'catalog', options } })
    await wrapper.findAll('button')[1]!.trigger('click')
    expect(wrapper.emitted('update:modelValue')).toEqual([['pair']])
    expect(wrapper.emitted('change')).toEqual([['pair']])
  })

  it('disabled suppresses emission (native button group parity)', async () => {
    const wrapper = mount(SegToggle, { props: { modelValue: 'catalog', options, disabled: true } })
    const buttons = wrapper.findAll('button')
    expect(buttons[0]!.attributes('disabled')).toBeDefined()
    await buttons[1]!.trigger('click')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('segClass composes onto the root .vt-seg element (per-tool width overrides target it)', () => {
    const wrapper = mount(SegToggle, { props: { modelValue: 'catalog', options, segClass: 'conj-seg' } })
    const root = wrapper.find('.vt-seg')
    expect(root.classes()).toContain('vt-seg')
    expect(root.classes()).toContain('conj-seg')
  })

  it('keyboard parity with a native button group: each option is a real <button>, no custom key handling needed', () => {
    const wrapper = mount(SegToggle, { props: { modelValue: 'catalog', options } })
    for (const b of wrapper.findAll('button')) {
      expect(b.element.tagName).toBe('BUTTON')
      expect(b.attributes('type')).toBe('button')
    }
  })
})
