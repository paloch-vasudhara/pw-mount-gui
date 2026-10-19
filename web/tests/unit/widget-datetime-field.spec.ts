// Ported from ssa-toolkit web/tests/unit/widget-datetime-field.spec.ts @84f2ded
import { describe, it, expect, afterEach, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import DateTimeField from '../../app/components/widgets/DateTimeField.vue'

// Every test unmounts its own wrapper (`afterEach`) — the popover portal
// teleports `#dt-pop` onto `document.body`, OUTSIDE the wrapper's own root,
// so a leftover instance from a previous test would otherwise leave a STALE
// `#dt-pop` node behind that `document.getElementById('dt-pop')` could pick
// up instead of the current test's freshly-opened one.
let current: VueWrapper | null = null
afterEach(() => {
  current?.unmount()
  current = null
})

function mountField(modelValue: Date | null): VueWrapper {
  current = mount(DateTimeField, { props: { modelValue }, attachTo: document.body }) as VueWrapper
  return current
}

async function openPopover(w: VueWrapper): Promise<HTMLElement> {
  await w.find('.dt-cal-btn').trigger('click')
  await flushPromises()
  await w.vm.$nextTick()
  await flushPromises()
  await w.vm.$nextTick()
  const pop = document.getElementById('dt-pop')
  if (!pop) throw new Error('#dt-pop not found after opening the popover')
  return pop
}

function findButton(pop: HTMLElement, label: string): HTMLButtonElement {
  const btn = Array.from(pop.querySelectorAll('button')).find(b => b.textContent?.trim().includes(label))
  if (!btn) throw new Error(`no button containing "${label}" (saw: ${Array.from(pop.querySelectorAll('button')).map(b => JSON.stringify(b.textContent)).join(', ')})`)
  return btn
}

describe('DateTimeField — P4 real-flow regression (§5b)', () => {
  // The legacy bug: arrow-key nav did nothing in the live app because the
  // keydown listener lived on the calendar host but opening the popup left
  // focus on the trigger BUTTON, outside the popup. The first (false-passing)
  // harness caught nothing because it dispatched keydown directly onto the
  // grid instead of driving the REAL open path. This test drives the real
  // path: click the trigger, read `document.activeElement`, THEN press a key
  // and confirm the ACTUAL focused element moved — never a synthetic dispatch
  // straight onto a day cell found by other means.
  it('click trigger -> document.activeElement is a day cell -> ArrowRight moves the focused day', async () => {
    const w = mountField(new Date(Date.UTC(2026, 8, 8, 7, 30, 12)))
    const pop = await openPopover(w)

    expect(document.activeElement).not.toBeNull()
    expect(document.activeElement).toBe(pop.querySelector('[data-selected]'))
    expect(document.activeElement?.textContent).toBe('8')

    document.activeElement!.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowRight', code: 'ArrowRight', bubbles: true }),
    )
    await flushPromises()
    await w.vm.$nextTick()

    expect(document.activeElement?.textContent).toBe('9')
  })

  it('Back returns to the calendar with focus re-armed', async () => {
    const w = mountField(new Date(Date.UTC(2026, 8, 8, 7, 30, 12)))
    const pop = await openPopover(w)

    findButton(pop, 'Next').click()
    await flushPromises()
    await w.vm.$nextTick()
    expect(pop.querySelector('#dt-pop-title')?.textContent).toBe('Select time (UTC)')

    findButton(pop, 'Back').click()
    await flushPromises()
    await w.vm.$nextTick()

    expect(pop.querySelector('#dt-pop-title')?.textContent).toBe('Select date')
    // Focus re-armed on the roving day (not left on the Back button).
    expect(document.activeElement).toBe(pop.querySelector('[data-selected]'))
  })
})

describe('DateTimeField — Confirm / Now (§5, P10)', () => {
  it('Confirm writes y/mo/d/h/mi/s and closes', async () => {
    const w = mountField(new Date(Date.UTC(2026, 8, 8, 7, 30, 12)))
    const pop = await openPopover(w)

    const day15 = Array.from(pop.querySelectorAll<HTMLElement>('.dtc-day'))
      .find(d => d.textContent === '15' && !d.hasAttribute('data-outside-view'))
    if (!day15) throw new Error('day 15 not found')
    day15.click()
    await flushPromises()

    findButton(pop, 'Next').click()
    await flushPromises()
    await w.vm.$nextTick()

    const hInput = pop.querySelector<HTMLInputElement>('input[title="Hour"]')
    if (!hInput) throw new Error('hour input not found')
    hInput.value = '11'
    hInput.dispatchEvent(new Event('input'))
    await flushPromises()

    findButton(pop, 'Confirm').click()
    await flushPromises()
    await w.vm.$nextTick()

    const emitted = w.emitted('update:modelValue')
    expect(emitted).toBeTruthy()
    const last = emitted!.at(-1)![0] as Date
    expect(last.getUTCFullYear()).toBe(2026)
    expect(last.getUTCMonth()).toBe(8)
    expect(last.getUTCDate()).toBe(15)
    expect(last.getUTCHours()).toBe(11)

    // Closed — the portal-teleported content is gone.
    expect(document.getElementById('dt-pop')).toBeNull()
  })

  it('Now (UTC) seeds from a faked clock', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(Date.UTC(2030, 2, 3, 4, 5, 6)))
    try {
      const w = mountField(null)
      const pop = await openPopover(w)
      findButton(pop, 'Now').click()
      await flushPromises()
      await w.vm.$nextTick()

      const emitted = w.emitted('update:modelValue')
      expect(emitted).toBeTruthy()
      const last = emitted!.at(-1)![0] as Date
      expect(last.toISOString()).toBe('2030-03-03T04:05:06.000Z')
      expect(document.getElementById('dt-pop')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('DateTimeField — segmented boxes (P9)', () => {
  it('the six segmented boxes are type="text" and carry no .dms-spinner', () => {
    const w = mountField(new Date(Date.UTC(2026, 8, 8, 7, 30, 12)))
    const boxes = w.findAll('.ov-epoch-box')
    expect(boxes.length).toBe(6)
    for (const b of boxes) {
      expect((b.element as HTMLInputElement).type).toBe('text')
    }
    expect(w.find('.dms-spinner').exists()).toBe(false)
  })

  it('emits nothing while typing; once on blur', async () => {
    const w = mountField(new Date(Date.UTC(2026, 8, 8, 7, 30, 12)))
    const hourBox = w.findAll('.ov-epoch-box')[3]!   // y, mo, d, h, mi, s
    await hourBox.setValue('11')
    expect(w.emitted('update:modelValue')).toBeFalsy()
    await hourBox.trigger('blur')
    expect(w.emitted('update:modelValue')?.length).toBe(1)
    const last = w.emitted('update:modelValue')!.at(-1)![0] as Date
    expect(last.getUTCHours()).toBe(11)
  })
})
