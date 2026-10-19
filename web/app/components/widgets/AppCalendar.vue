<script setup lang="ts">
// Ported from ssa-toolkit web/app/components/widgets/AppCalendar.vue @84f2ded
// AppCalendar.vue — the shadcn-vue / reka-ui Calendar, restyled to the app's
// tokens per §4b/T11's parity map (docs/datepicker_calendar.html §2):
//   .dtc → CalendarRoot · .dtc-header → CalendarHeader · .dtc-caption →
//   CalendarHeading · .dtc-nav/.dtc-prev/.dtc-next → CalendarPrevButton /
//   CalendarNextButton · .dtc-grid → CalendarGrid · .dtc-weekday →
//   CalendarHeadCell · .dtc-day → CalendarCellTrigger.
//
// This does NOT use the generated app/components/ui/calendar/Calendar.vue
// wrapper (shadcn's all-in-one component) — that file's default template
// already renders a static `<CalendarHeading>` (no dropdowns) when no
// `layout` prop is passed, which would satisfy P1/P2, but its header markup
// is Tailwind utility classes + absolutely-positioned nav (`class="flex
// justify-center pt-1 relative items-center w-full px-8"` + `"absolute top-0
// inset-x-0"` nav), not the lifted `.dtc-header` CSS's explicit `grid-
// template-columns: 24px 1fr 24px` (P1's literal requirement). Composing the
// primitives directly here, with our own `.dtc-*` classes on each one (they
// all accept a `class` prop, appended via `cn()` after their Tailwind
// defaults), gives byte-exact parity with the lifted CSS. The `#dt-pop`
// ancestor id (rendered by the caller, DateTimePopover.vue) gives our
// `#dt-pop .dtc-*` selectors ID-level specificity, so they win over the
// primitives' own Tailwind utility classes regardless of source order —
// no need to strip or fight the generated classes.
//
// BINDING UTC boundary (§4b/T11): `CalendarDate` (from `@internationalized/
// date`) NEVER leaves this component. The only two conversions in the file
// are `utcDateToCalendarDate` and `calendarDateToUtcDate` below, and neither
// calls `toDate()`, `today()`, `getLocalTimeZone()`, or `parseDate` on a
// local string — a grep for those across app/components/ must come back
// empty (verified in the T11 report). `locale="en-US"` is set explicitly on
// CalendarRoot so the week starts Sunday, matching the prototype's
// `startDow` (`new Date(Date.UTC(...)).getUTCDay()`) logic.

import { computed, nextTick, ref, watch } from 'vue'
import { CalendarRoot, type DateValue } from 'reka-ui'
import { CalendarDate } from '@internationalized/date'
import CalendarHeader from '@/components/ui/calendar/CalendarHeader.vue'
import CalendarHeading from '@/components/ui/calendar/CalendarHeading.vue'
import CalendarPrevButton from '@/components/ui/calendar/CalendarPrevButton.vue'
import CalendarNextButton from '@/components/ui/calendar/CalendarNextButton.vue'
import CalendarGrid from '@/components/ui/calendar/CalendarGrid.vue'
import CalendarGridHead from '@/components/ui/calendar/CalendarGridHead.vue'
import CalendarGridBody from '@/components/ui/calendar/CalendarGridBody.vue'
import CalendarGridRow from '@/components/ui/calendar/CalendarGridRow.vue'
import CalendarHeadCell from '@/components/ui/calendar/CalendarHeadCell.vue'
import CalendarCell from '@/components/ui/calendar/CalendarCell.vue'
import CalendarCellTrigger from '@/components/ui/calendar/CalendarCellTrigger.vue'
import { MIN_YEAR, maxYear } from '@/utils/datetime-fields'

const props = defineProps<{
  /** The picked date, UTC y/mo/d only (h/mi/s are the time pane's concern —
   *  this component never sees them). `null` while nothing is picked yet. */
  modelValue: Date | null
  /** The month to bring into view when nothing is picked yet (mirrors the
   *  legacy `_scenarioStart()` seed). */
  fallback: Date
}>()

const emit = defineEmits<{
  'update:modelValue': [Date]
}>()

// ── UTC <-> CalendarDate boundary (the ONLY place this conversion happens) ──
function utcDateToCalendarDate(d: Date): CalendarDate {
  return new CalendarDate(d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate())
}
function calendarDateToUtcDate(cd: DateValue): Date {
  return new Date(Date.UTC(cd.year, cd.month - 1, cd.day))
}

const selected = ref<DateValue | undefined>(
  props.modelValue ? utcDateToCalendarDate(props.modelValue) : undefined,
)
const placeholder = ref<DateValue>(
  utcDateToCalendarDate(props.modelValue ?? props.fallback),
)

// CalendarDate from our @internationalized/date vs reka's DateValue: same shape,
// nominally different declarations - bridge the type once here.
const selectedDv = computed(() => selected.value as unknown as DateValue | undefined)
const placeholderDv = computed(() => placeholder.value as unknown as DateValue)

watch(() => props.modelValue, (v) => {
  selected.value = v ? utcDateToCalendarDate(v) : undefined
  if (v) placeholder.value = utcDateToCalendarDate(v)
})

function onUpdateModelValue(v: DateValue | undefined): void {
  if (!v) return
  selected.value = v
  emit('update:modelValue', calendarDateToUtcDate(v))
}

// P5: range [1900-01-01, currentYear+100-12-31], recomputed live (not
// hard-coded) via `maxYear()`.
const minValue = computed(() => new CalendarDate(MIN_YEAR, 1, 1))
const maxValue = computed(() => new CalendarDate(maxYear(), 12, 31))

// P3: Home/End (week edges) and PageUp/PageDown (±1 month). reka-ui 2.10.4's
// CalendarCellTrigger only wires ArrowUp/Down/Left/Right + Enter/Space (see
// its `withKeys(handleArrowKey, ["up","down","left","right","space","enter"])`
// — verified by reading node_modules/reka-ui/dist/Calendar/
// CalendarCellTrigger.js; there is no Home/End/PageUp/PageDown handling
// anywhere under dist/Calendar/). The legacy `_calKeydown`'s math for these
// four keys is ported here as a delegated listener on the CalendarRoot
// element (bubbles up from the day buttons, same delegation the legacy
// `_calWire` used) — everything else (arrows, click, Enter/Space) is left to
// reka natively.
const rootEl = ref<InstanceType<typeof CalendarRoot> | null>(null)

function currentRootEl(): HTMLElement | null {
  const inst = rootEl.value as unknown as { $el?: HTMLElement } | HTMLElement | null
  if (!inst) return null
  return (inst as HTMLElement).nodeType === 1 ? (inst as HTMLElement) : ((inst as { $el?: HTMLElement }).$el ?? null)
}

function refocusRovingDay(): void {
  nextTick(() => {
    const host = currentRootEl()
    const el = host?.querySelector('[tabindex="0"]') as HTMLElement | null
    el?.focus()
  })
}

function onGridKeydown(e: KeyboardEvent): void {
  if (e.key !== 'Home' && e.key !== 'End' && e.key !== 'PageUp' && e.key !== 'PageDown') return
  e.preventDefault()
  e.stopPropagation()
  const cur = placeholder.value as CalendarDate

  if (e.key === 'PageUp' || e.key === 'PageDown') {
    const next = cur.add({ months: e.key === 'PageUp' ? -1 : 1 })
    if (next.compare(minValue.value) < 0 || next.compare(maxValue.value) > 0) return
    placeholder.value = next
    refocusRovingDay()
    return
  }

  // Home/End: week edges, via the SAME UTC-getUTCDay() math the legacy
  // `_calKeydown`'s `dow()` closure used (`new Date(Date.UTC(y, mo-1, d))
  // .getUTCDay()`), not CalendarDate's own (locale-aware) day-of-week.
  const dow = new Date(Date.UTC(cur.year, cur.month - 1, cur.day)).getUTCDay()
  const delta = e.key === 'Home' ? -dow : (6 - dow)
  const next = cur.add({ days: delta })
  if (next.compare(minValue.value) < 0 || next.compare(maxValue.value) > 0) return   // refuse to leave the range
  placeholder.value = next
  refocusRovingDay()
}
</script>

<template>
  <CalendarRoot
    ref="rootEl"
    v-slot="{ grid, weekDays }"
    :model-value="selectedDv"
    :placeholder="placeholderDv"
    :min-value="minValue"
    :max-value="maxValue"
    locale="en-US"
    weekday-format="short"
    fixed-weeks
    class="dtc"
    @update:model-value="onUpdateModelValue"
    @update:placeholder="(v) => { if (v) placeholder = v }"
    @keydown="onGridKeydown"
  >
    <CalendarHeader class="dtc-header">
      <CalendarPrevButton class="dtc-nav dtc-prev" />
      <CalendarHeading class="dtc-caption" />
      <CalendarNextButton class="dtc-nav dtc-next" />
    </CalendarHeader>
    <CalendarGrid v-for="month in grid" :key="month.value.toString()" class="dtc-grid">
      <CalendarGridHead>
        <CalendarGridRow class="dtc-weekdays">
          <CalendarHeadCell v-for="day in weekDays" :key="day" class="dtc-weekday">
            {{ day }}
          </CalendarHeadCell>
        </CalendarGridRow>
      </CalendarGridHead>
      <CalendarGridBody class="dtc-days">
        <CalendarGridRow v-for="(weekDates, index) in month.rows" :key="`weekDate-${index}`" class="dtc-week">
          <CalendarCell
            v-for="weekDate in weekDates"
            :key="weekDate.toString()"
            :date="weekDate"
          >
            <CalendarCellTrigger
              :day="weekDate"
              :month="month.value"
              class="dtc-day"
            />
          </CalendarCell>
        </CalendarGridRow>
      </CalendarGridBody>
    </CalendarGrid>
  </CalendarRoot>
</template>
