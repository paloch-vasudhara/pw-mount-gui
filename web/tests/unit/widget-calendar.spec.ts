// Ported from ssa-toolkit web/tests/unit/widget-calendar.spec.ts @84f2ded
import { describe, it, expect } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import AppCalendar from '../../app/components/widgets/AppCalendar.vue'

// The §5 functional list, mechanically (§4b/T11 test contract). Seeded on
// 2026-09-08 (a Tuesday) unless a test says otherwise. `data-selected` /
// `data-today` / `data-outside-view` / `data-disabled` are reka-ui's OWN
// attribute names (docs/datepicker_calendar.html §2) — the legacy bespoke
// calendar was built to this anatomy on purpose, so these selectors are
// identical to the ones the vanilla-JS calendar used.

function mountCal(modelValue: Date, fallback = modelValue) {
  return mount(AppCalendar, {
    props: { modelValue, fallback },
    attachTo: document.body,
  })
}

describe('AppCalendar — functional (§5)', () => {
  it('caption text equals "September 2026"', () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    expect(w.find('.dtc-caption').text()).toBe('September 2026')
  })

  it('header contains 0 <input>, 0 <select>, exactly 2 nav buttons', () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    const header = w.find('.dtc-header')
    expect(header.findAll('input').length).toBe(0)
    expect(header.findAll('select').length).toBe(0)
    expect(header.findAll('.dtc-nav').length).toBe(2)
  })

  it('7 CalendarHeadCells reading Sun...Sat', () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    const cells = w.findAll('.dtc-weekday')
    expect(cells.length).toBe(7)
    expect(cells.map(c => c.text())).toEqual(['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'])
  })

  it('42 day cells, always (fixedWeeks)', () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    expect(w.findAll('.dtc-day').length).toBe(42)
  })

  it('the correct cell carries data-selected', () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    const sel = w.find('.dtc-day[data-selected]')
    expect(sel.exists()).toBe(true)
    expect(sel.text()).toBe('8')
  })

  it('the leading cell for September 2026 is 2026-08-30 and carries data-outside-view', () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    const first = w.findAll('.dtc-day')[0]!
    expect(first.text()).toBe('30')
    expect(first.attributes('data-outside-view')).toBeDefined()
  })

  it('renders a real table: 1 weekday header row of 7 <th>, and 6 week <tr> rows of 7 <td> each (C2 fix)', () => {
    // Guards the C2 defect: reka's CalendarGrid/-Head/-Body/-Row/-Cell render
    // actual table/thead/tbody/tr/th/td, so the day grid must come out as
    // 6 <tr> x 7 <td> (grouped by AppCalendar.vue's `month.rows`), not one
    // flat row of 42 cells (that was the bug — `display:grid` had landed on
    // <tbody> instead of on each per-week <tr>).
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    expect(w.find('table.dtc-grid').exists()).toBe(true)
    expect(w.find('thead').exists()).toBe(true)
    expect(w.find('tbody.dtc-days').exists()).toBe(true)

    const headRows = w.find('thead').findAll('tr')
    expect(headRows.length).toBe(1)
    expect(headRows[0]!.findAll('th').length).toBe(7)

    const weekRows = w.find('tbody').findAll('tr.dtc-week')
    expect(weekRows.length).toBe(6)
    for (const row of weekRows) {
      expect(row.findAll('td').length).toBe(7)
    }
  })

  it('next/prev nav changes the caption', async () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    await w.find('.dtc-next').trigger('click')
    expect(w.find('.dtc-caption').text()).toBe('October 2026')
    await w.find('.dtc-prev').trigger('click')
    await w.find('.dtc-prev').trigger('click')
    expect(w.find('.dtc-caption').text()).toBe('August 2026')
  })
})

describe('AppCalendar — keyboard (§5, seeded on the 8th)', () => {
  // Each case re-mounts fresh (seeded on 2026-09-08) and drives one keyboard
  // move on the roving-tabindex day, asserting the new roving day's number —
  // verbatim against the §5 list. Reads the roving cell via
  // `.dtc-day[tabindex="0"]` (reka-ui's OWN roving-tabindex state), the same
  // technique the legacy `_calFocusDay` used to find the day to focus — NOT
  // `document.activeElement`, which real DOM `.focus()` timing under happy-
  // dom makes unreliable in a unit test; the REAL open-path + real-focus
  // regression (P4) is exercised separately in widget-datetime-field.spec.ts.
  async function press(w: ReturnType<typeof mountCal>, key: string): Promise<string> {
    const focused = w.find('.dtc-day[tabindex="0"]')
    // reka-ui's CalendarCellTrigger switches on `event.code` (not `.key`) for
    // arrow dispatch (`case kbd.ARROW_RIGHT:` etc, where kbd.ARROW_RIGHT is
    // the STRING "ArrowRight" — matched against `.code`, not `.key`). A real
    // browser KeyboardEvent always sets both to the same string for these
    // keys; a synthetic test dispatch does not unless told to.
    await focused.trigger('keydown', { key, code: key })
    // The v-model:placeholder round trip (CalendarRoot emits
    // update:placeholder -> our handler writes the ref back -> CalendarRoot
    // re-renders as a controlled prop) needs an extra flush beyond the
    // first nextTick to fully settle before the roving cell is read.
    await flushPromises()
    await w.vm.$nextTick()
    return w.find('.dtc-day[tabindex="0"]').text()
  }

  it('ArrowDown -> 15', async () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    expect(await press(w, 'ArrowDown')).toBe('15')
  })
  it('ArrowLeft -> 14 (start 15, -1)', async () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    await press(w, 'ArrowDown')   // -> 15
    expect(await press(w, 'ArrowLeft')).toBe('14')
  })
  it('ArrowRight x2 -> 16 (from 14)', async () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    await press(w, 'ArrowDown')   // 15
    await press(w, 'ArrowLeft')   // 14
    await press(w, 'ArrowRight')  // 15
    expect(await press(w, 'ArrowRight')).toBe('16')
  })
  it('ArrowUp -> 9 (from 16)', async () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    await press(w, 'ArrowDown'); await press(w, 'ArrowLeft')
    await press(w, 'ArrowRight'); await press(w, 'ArrowRight')
    expect(await press(w, 'ArrowUp')).toBe('9')
  })
  it('Home -> 6 (week edge, from 9)', async () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    await press(w, 'ArrowDown'); await press(w, 'ArrowLeft')
    await press(w, 'ArrowRight'); await press(w, 'ArrowRight'); await press(w, 'ArrowUp')
    expect(await press(w, 'Home')).toBe('6')
  })
  it('End -> 12 (week edge, from 6)', async () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    await press(w, 'ArrowDown'); await press(w, 'ArrowLeft')
    await press(w, 'ArrowRight'); await press(w, 'ArrowRight'); await press(w, 'ArrowUp'); await press(w, 'Home')
    expect(await press(w, 'End')).toBe('12')
  })
  it('PageDown -> October', async () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    await press(w, 'PageDown')
    expect(w.find('.dtc-caption').text()).toBe('October 2026')
  })
  it('PageUp (from PageDown) -> back to September', async () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    await press(w, 'PageDown')
    await press(w, 'PageUp')
    expect(w.find('.dtc-caption').text()).toBe('September 2026')
  })
  it('30x ArrowRight crosses into October', async () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    for (let i = 0; i < 30; i++) await press(w, 'ArrowRight')
    expect(w.find('.dtc-caption').text()).toBe('October 2026')
  })
  it('click selects the focused day', async () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    const day15 = w.findAll('.dtc-day').find(d => d.text() === '15' && !d.attributes('data-outside-view'))!
    await day15.trigger('click')
    expect(w.emitted('update:modelValue')).toBeTruthy()
    const last = w.emitted('update:modelValue')!.at(-1)![0] as Date
    expect(last.getUTCDate()).toBe(15)
  })
})

describe('AppCalendar — bounds (§5, 1900-01)', () => {
  it('at 1900-01 the caption is correct, prev is disabled, next is enabled', () => {
    const w = mountCal(new Date(Date.UTC(1900, 0, 15)))
    expect(w.find('.dtc-caption').text()).toBe('January 1900')
    expect(w.find('.dtc-prev').attributes('disabled')).toBeDefined()
    expect(w.find('.dtc-next').attributes('disabled')).toBeUndefined()
  })
  it('December-1899 leading days are data-disabled', () => {
    const w = mountCal(new Date(Date.UTC(1900, 0, 15)))
    const days = w.findAll('.dtc-day')
    // LEADING outside-view cells only (December 1899, before the 1st) — the
    // grid's TRAILING outside-view cells (February 1900) are also
    // data-outside-view but are NOT disabled (maxYear() is far in the
    // future), so this must not use a blanket "any outside-view cell" filter.
    const leading: typeof days = []
    for (const d of days) {
      if (d.attributes('data-outside-view') === undefined) break
      leading.push(d)
    }
    expect(leading.length).toBeGreaterThan(0)
    for (const d of leading) expect(d.attributes('data-disabled')).toBeDefined()
  })
  it('<- at the floor is refused (focus stays on Jan 1, 1900)', async () => {
    const w = mountCal(new Date(Date.UTC(1900, 0, 1)))
    const focused = w.find('.dtc-day[tabindex="0"]')
    expect(focused.text()).toBe('1')
    await focused.trigger('keydown', { key: 'ArrowLeft', code: 'ArrowLeft' })
    await w.vm.$nextTick()
    // Still on the 1st — the floor refused the move.
    expect(w.find('.dtc-day[tabindex="0"]').text()).toBe('1')
  })
})

// CHANGED from ssa-toolkit: the D-DIN `font-body` assertions are dropped (this app
// uses Barlow via the body font); the cells only need to carry the dtc-* hooks.
describe('AppCalendar — font hooks', () => {
  it('day cells, weekday cells and the caption carry their dtc-* classes', () => {
    const w = mountCal(new Date(Date.UTC(2026, 8, 8)))
    expect(w.find('.dtc-day').exists()).toBe(true)
    expect(w.find('.dtc-weekday').exists()).toBe(true)
    expect(w.find('.dtc-caption').exists()).toBe(true)
  })
})
