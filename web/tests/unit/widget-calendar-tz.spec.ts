// Ported from ssa-toolkit web/tests/unit/widget-calendar-tz.spec.ts @84f2ded
// widget-calendar-tz.spec.ts — the C2 gate proof (§4b/T11's mitigation box,
// point 4): the SAME seeded render + the SAME three keyboard moves, executed
// with the process timezone forced to Pacific/Kiritimati (UTC+14) and
// Pacific/Niue (UTC-11) — the two edge zones a system clock can plausibly be
// in — and asserting the rendered caption, the selected cell, and the
// emitted `Date.toISOString()` come out byte-identical. This is the actual
// proof that AppCalendar.vue's UTC boundary (CalendarDate <-> Date.UTC(...))
// holds: if any code path leaked the system's local offset into a civil-date
// decision, one of these two zones (being on opposite sides of UTC by a
// wide margin) would show a shifted day where the other didn't.
//
// MECHANISM: plain `process.env.TZ = '<zone>'` assignment, set at the start
// of each test, BEFORE any `Date`/`Intl` use in that test. Verified
// empirically first (a throwaway probe spec, since removed) that Node 24's
// ICU/V8 in this environment re-reads `process.env.TZ` on every `Date`
// construction rather than caching the offset for the life of the process —
// `new Date().getTimezoneOffset()` changed correctly both on first use AND
// after a Date had already been constructed earlier in the same worker. No
// `vitest.config.ts` per-project `test.env.TZ` split was needed as a result;
// a plain env assignment inside each test is sufficient and keeps both zones
// in one file, one Vitest project, matching the contract's single spec file.
// (`vi.stubEnv('TZ', ...)` was also verified to work identically — either
// mechanism is fine here; this file uses the plain assignment since it is
// one line simpler and no other env var needs stub/restore semantics.)

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import AppCalendar from '../../app/components/widgets/AppCalendar.vue'

const ORIGINAL_TZ = process.env.TZ

afterEach(() => {
  process.env.TZ = ORIGINAL_TZ
})

interface Result { offset: number; caption: string; selectedCellText: string; iso: string }

async function runSeededScenario(tz: string): Promise<Result> {
  process.env.TZ = tz
  const offset = new Date().getTimezoneOffset()

  const w = mount(AppCalendar, {
    props: {
      modelValue: new Date(Date.UTC(2026, 8, 8)),
      fallback: new Date(Date.UTC(2026, 8, 8)),
    },
    attachTo: document.body,
  })

  // Three keyboard moves (ArrowDown, ArrowLeft, ArrowRight — a subset of the
  // widget-calendar.spec.ts §5 sequence, sufficient to exercise the roving-
  // focus + month-boundary machinery under each zone).
  for (const key of ['ArrowDown', 'ArrowLeft', 'ArrowRight']) {
    const focused = w.find('.dtc-day[tabindex="0"]')
    await focused.trigger('keydown', { key, code: key })
    await flushPromises()
    await w.vm.$nextTick()
  }

  const caption = w.find('.dtc-caption').text()
  const focusedCell = w.find('.dtc-day[tabindex="0"]')
  const selectedCellText = focusedCell.text()
  await focusedCell.trigger('click')
  await flushPromises()
  const emitted = w.emitted('update:modelValue')
  const iso = emitted ? (emitted.at(-1)![0] as Date).toISOString() : ''

  w.unmount()
  return { offset, caption, selectedCellText, iso }
}

describe('AppCalendar — timezone independence (C2 gate)', () => {
  let kiritimati: Result
  let niue: Result

  beforeEach(async () => {
    kiritimati = await runSeededScenario('Pacific/Kiritimati')   // UTC+14
    niue = await runSeededScenario('Pacific/Niue')                // UTC-11
  })

  it('the process timezone actually changed between runs (mechanism sanity check)', () => {
    // Kiritimati is UTC+14 -> getTimezoneOffset() === -840 (west-negative
    // convention). Niue is UTC-11 -> +660. If these are NOT what changed,
    // the "proof" below would be vacuous (both runs executing in the same
    // zone) — this assertion is what makes it non-vacuous.
    expect(kiritimati.offset).toBe(-840)
    expect(niue.offset).toBe(660)
    expect(kiritimati.offset).not.toBe(niue.offset)
  })

  it('the rendered caption is byte-identical across both zones', () => {
    expect(kiritimati.caption).toBe(niue.caption)
    expect(kiritimati.caption).toBe('September 2026')
  })

  it('the selected/focused cell text is byte-identical across both zones', () => {
    expect(kiritimati.selectedCellText).toBe(niue.selectedCellText)
  })

  it("the emitted Date.toISOString() is byte-identical across both zones", () => {
    expect(kiritimati.iso).toBe(niue.iso)
    expect(kiritimati.iso).not.toBe('')
  })
})
