// T0.2 supervisor-gate regressions (found in a real browser, missed by happy-dom).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import AngleField from '../../app/components/widgets/AngleField.vue'
import DateTimeField from '../../app/components/widgets/DateTimeField.vue'
import DdInput from '../../app/components/widgets/DdInput.vue'
import DmsTriad from '../../app/components/widgets/DmsTriad.vue'
import { _resetSignGroupsForTests } from '../../app/composables/useSignGroup'

const MINUS = '−'
const PM: [string, string] = ['+', MINUS]
let wrappers: VueWrapper[] = []

beforeEach(() => _resetSignGroupsForTests())
afterEach(() => {
  wrappers.forEach(w => w.unmount())
  wrappers = []
  vi.restoreAllMocks()
})

function live(comp: object, props: Record<string, unknown> & { modelValue: number }) {
  const w = mount(comp, {
    props: { ...props, 'onUpdate:modelValue': (v: number) => w.setProps({ modelValue: v }) },
    attachTo: document.body,
  })
  wrappers.push(w)
  return w
}
const last = (w: VueWrapper) => w.emitted('update:modelValue')!.at(-1)![0] as number

/** Emulate Chrome: type=number exposes no selectionStart/End, and
 *  getSelection().toString() is the selected text of the focused input. */
function selectAll(input: HTMLInputElement): void {
  Object.defineProperty(input, 'selectionStart', { value: null, configurable: true })
  Object.defineProperty(input, 'selectionEnd', { value: null, configurable: true })
  vi.spyOn(window, 'getSelection').mockReturnValue({ toString: () => input.value } as unknown as Selection)
}
function noSelection(input: HTMLInputElement): void {
  Object.defineProperty(input, 'selectionStart', { value: null, configurable: true })
  Object.defineProperty(input, 'selectionEnd', { value: null, configurable: true })
  vi.spyOn(window, 'getSelection').mockReturnValue({ toString: () => '' } as unknown as Selection)
}

describe('BUG 1 - typing "-" over a selected value (legacy parity)', () => {
  it('DdInput: "-" makes the chip negative and clears the box so digits REPLACE', async () => {
    const w = live(DdInput, { modelValue: 51.41, cardinal: PM, range: [-90, 90], signGroup: 'r1', step: 0.0001 })
    const input = w.find('input')
    const el = input.element as HTMLInputElement
    selectAll(el)
    await input.trigger('keydown', { key: '-' })
    expect(w.find('.dms-sign-indicator').classes()).toContain('neg')
    expect(el.value).toBe('')
    await input.setValue('7')
    await input.trigger('blur')
    expect(last(w)).toBe(-7)
    expect(el.value).toBe('7')
  })

  it('"-12.5" over a selection gives -12.5 even if the chip was already negative', async () => {
    const w = live(DdInput, { modelValue: -51.41, cardinal: PM, range: [-90, 90], signGroup: 'r2', step: 0.0001 })
    const input = w.find('input')
    const el = input.element as HTMLInputElement
    selectAll(el)
    await input.trigger('keydown', { key: '-' })
    await input.setValue('12.5')
    await input.trigger('blur')
    expect(last(w)).toBe(-12.5)
  })

  it('NOT all selected: "-" keeps the toggle-only behaviour and the text', async () => {
    const w = live(DdInput, { modelValue: 51.41, cardinal: PM, range: [-90, 90], signGroup: 'r3', step: 0.0001 })
    const input = w.find('input')
    noSelection(input.element as HTMLInputElement)
    await input.trigger('keydown', { key: '-' })
    expect(last(w)).toBe(-51.41)
    expect((input.element as HTMLInputElement).value).toBe('51.41')
  })

  it('locked chip (min >= 0): "-" over a selection does nothing', async () => {
    const w = live(DdInput, { modelValue: 51.41, cardinal: PM, range: [0, 90], signGroup: 'r4', step: 0.0001 })
    const input = w.find('input')
    selectAll(input.element as HTMLInputElement)
    await input.trigger('keydown', { key: '-' })
    expect((input.element as HTMLInputElement).value).toBe('51.41')
    expect(w.emitted('update:modelValue')).toBeUndefined()
  })

  it('AngleField (DD leg + paired DMS leg echoing the sign): the cleared box is not re-rendered over', async () => {
    const w = live(AngleField, { modelValue: 51.41, kind: 'angle', label: 'El', signed: true, step: 0.0001 })
    const input = w.findAll('input')[0]!
    const el = input.element as HTMLInputElement
    selectAll(el)
    await input.trigger('keydown', { key: '-' })
    await flushPromises()
    expect(el.value).toBe('')
    await input.setValue('7')
    await input.trigger('blur')
    await flushPromises()
    expect(last(w)).toBe(-7)
    expect(el.value).toBe('7')
  })

  it('DmsTriad degrees box: same treatment', async () => {
    const w = live(DmsTriad, { modelValue: 51.41, mode: 'limit:90', cardinal: PM, range: [-90, 90], signGroup: 'r5' })
    await w.vm.$nextTick()   // onMounted render of the boxes
    const deg = w.findAll('input')[0]!
    const el = deg.element as HTMLInputElement
    selectAll(el)
    await deg.trigger('keydown', { key: '-' })
    expect(w.find('.dms-sign-indicator').classes()).toContain('neg')
    expect(el.value).toBe('')
    await deg.setValue('12')
    await deg.trigger('blur')
    // 12 deg + the untouched 24' 36" of 51.41 => -12.41
    expect(last(w)).toBeCloseTo(-12.41, 4)
  })

  it('DmsTriad degrees box: NOT all selected keeps toggle-only', async () => {
    const w = live(DmsTriad, { modelValue: 51.41, mode: 'limit:90', cardinal: PM, range: [-90, 90], signGroup: 'r6' })
    await w.vm.$nextTick()
    const deg = w.findAll('input')[0]!
    noSelection(deg.element as HTMLInputElement)
    await deg.trigger('keydown', { key: '-' })
    expect(last(w)).toBeCloseTo(-51.41, 4)
    expect((deg.element as HTMLInputElement).value).toBe('51')
  })
})

describe('BUG 2 - DateTimePopover must reach the time pane and confirm', () => {
  it('pick day -> Next -> time pane stays (even across a parent re-render) -> Confirm emits', async () => {
    const w = mount(DateTimeField, {
      props: { modelValue: new Date(Date.UTC(2026, 9, 19, 8, 30, 15)), label: 'a' },
      attachTo: document.body,
    })
    wrappers.push(w)
    await w.find('.dt-cal-btn').trigger('click')
    await flushPromises()
    const pop = document.getElementById('dt-pop')!
    expect(pop).not.toBeNull()

    const day = Array.from(pop.querySelectorAll<HTMLElement>('.dtc-day'))
      .find(b => b.textContent?.trim() === '22' && !b.hasAttribute('data-outside-view'))!
    day.click()
    await flushPromises()

    const next = Array.from(pop.querySelectorAll('button')).find(b => b.textContent?.includes('Next'))!
    next.click()
    await flushPromises()
    expect(pop.querySelector('#dt-pop-title')?.textContent).toBe('Select time (UTC)')

    // The real-browser failure: the PARENT re-rendered (fresh `currentFields()`
    // object passed as seedFields) and the popover re-seeded back to 'date'.
    // `fallback` is read by the slot, so the slot re-renders and builds a fresh
    // currentFields() object with identical values.
    await w.setProps({ label: 'b', fallback: new Date(Date.UTC(2000, 0, 1)) })
    await flushPromises()
    expect(pop.querySelector('#dt-pop-title')?.textContent).toBe('Select time (UTC)')
    const confirm = Array.from(pop.querySelectorAll('button')).find(b => b.textContent?.includes('Confirm'))
    expect(confirm).toBeTruthy()

    confirm!.click()
    await flushPromises()
    const emitted = w.emitted('update:modelValue')!.at(-1)![0] as Date
    expect(emitted.toISOString()).toBe('2026-10-22T08:30:15.000Z')
  })

  it('pointerdown/up/click sequence on Next does not close the popover', async () => {
    const w = mount(DateTimeField, {
      props: { modelValue: new Date(Date.UTC(2026, 9, 19, 8, 30, 15)) },
      attachTo: document.body,
    })
    wrappers.push(w)
    await w.find('.dt-cal-btn').trigger('click')
    await flushPromises()
    const pop = document.getElementById('dt-pop')!
    const next = Array.from(pop.querySelectorAll('button')).find(b => b.textContent?.includes('Next'))!
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      next.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true }))
    }
    await flushPromises()
    expect(document.getElementById('dt-pop')).not.toBeNull()
    expect(document.getElementById('dt-pop-title')?.textContent).toBe('Select time (UTC)')
  })
})
