<script setup lang="ts">
// Ported from ssa-toolkit web/app/components/widgets/DateTimeField.vue @84f2ded
// DateTimeField.vue — segmented UTC Y-Mo-D H:Mi:S boxes + the calendar-icon
// trigger, one component replacing the 22 DT_BLOCKS call sites of
// frontend/src/datetime-picker.js (674) (§4b/T11). The id-prefix plumbing
// (`DT_BLOCKS`, `_box(prefix, suf)`) disappears entirely — each call site is
// now just `<DateTimeField v-model="..." label="..." />`.
//
// Legacy prefix -> future call site map (for the panel waves wiring these
// in): opt-scen-start/opt-scen-end -> scenario setup dialog start/end;
// scen-start/scen-end -> main scenario bar start/end; def-epoch -> Add
// Satellite default epoch; conj-start/conj-end -> Conjunction Analysis;
// re-endtime -> Reentry end time; pass-start/pass-end -> Pass Analysis;
// export-epoch/export-start/export-end -> Export dialogs; launch-epoch ->
// Launch tool; mnvin/mnvct/mnvtr -> Maneuver tool (initial/current/target);
// ssa-jumpto -> the SSA timeline jump-to box; ov-img-epoch -> Imaging
// Simulator epoch; ov-epoch -> Observer View "Now" replacement; tdm-def-
// epoch/tdm-cap-epoch -> TDM Generator default epoch / image capture time.
//
// Segmented boxes are ALWAYS type="text" (matching `_initScroll`'s `box.type
// = "text"` applied uniformly to all six units, unlike the DMS triad's deg
// box which the legacy code never retypes — see DmsTriad.vue's T10b
// DEVIATION note. There is no special case here: y/mo/d/h/mi/s are all text
// + inputmode, all width-constrained, no spinner buttons).

import { ref, watch } from 'vue'
import {
  MIN_YEAR, maxYear, stepField, readFields, dateFieldsValid,
  utcDateToFields, fieldsToUtcDate, clampField, clampDayToMonth, pad2, padSec,
  type DtFields, type DtUnit,
} from '@/utils/datetime-fields'
import { Popover, PopoverTrigger, PopoverContent } from '@/components/ui/popover'
import DateTimePopover from './DateTimePopover.vue'

const props = withDefaults(defineProps<{
  modelValue: Date | null
  showSeconds?: boolean
  /** Seed for the popover when the field is empty (= `_scenarioStart()`). */
  fallback?: Date
  label?: string
  disabled?: boolean
}>(), {
  showSeconds: true,
  fallback: () => new Date(),
  label: undefined,
  disabled: false,
})

const emit = defineEmits<{
  'update:modelValue': [Date | null]
}>()

// Seconds precision only: whole seconds, no fractional part.
const dec = 0

const yText = ref('')
const moText = ref('')
const dText = ref('')
const hText = ref('')
const miText = ref('')
const sText = ref('')

const yRef = ref<HTMLInputElement | null>(null)
const moRef = ref<HTMLInputElement | null>(null)
const dRef = ref<HTMLInputElement | null>(null)
const hRef = ref<HTMLInputElement | null>(null)
const miRef = ref<HTMLInputElement | null>(null)
const sRef = ref<HTMLInputElement | null>(null)

function renderFields(f: DtFields): void {
  yText.value = String(f.y)
  moText.value = pad2(f.mo)
  dText.value = pad2(f.d)
  hText.value = pad2(f.h)
  miText.value = pad2(f.mi)
  sText.value = padSec(Math.floor(f.s), dec)
}

function renderFromModel(): void {
  if (props.modelValue) {
    renderFields(utcDateToFields(props.modelValue))
  } else {
    // Genuinely blank — legacy's markup never carries a `value` attribute
    // until `_segDtSet` actually runs (analysis-passes.js:openPassesModal),
    // so an unset field (no scenario, no Cesium clock fallback available,
    // e.g. the /dev/tools harness) shows a truly empty box, not a
    // zero-filled one (C4-d). Force-filling "00"/"00.000" here would render
    // narrower-looking content than legacy's blank, same-width box.
    yText.value = ''
    moText.value = ''
    dText.value = ''
    hText.value = ''
    miText.value = ''
    sText.value = ''
  }
}
renderFromModel()
watch(() => props.modelValue, renderFromModel)

function currentFields(): DtFields {
  return readFields(yText.value, moText.value, dText.value, hText.value, miText.value, sText.value)
}

function emitCommitted(f: DtFields): void {
  renderFields(f)
  emit('update:modelValue', fieldsToUtcDate(f))
}

// Blank-block seeding (mirrors `_step`'s "seed a blank block from the
// scenario start, then step from there").
function seedIfBlank(): DtFields {
  let f = currentFields()
  if (!dateFieldsValid(f)) {
    renderFields(utcDateToFields(props.fallback))
    f = currentFields()
  }
  return f
}

function step(unit: DtUnit, dir: 1 | -1): void {
  const f = seedIfBlank()
  const next = stepField(f, unit, dir, 1)
  if (next) emitCommitted(next)
  else renderFields(f)   // refused at a bound — re-render unchanged (no-op)
}

const UNIT_REFS: Record<DtUnit, () => HTMLInputElement | null> = {
  y: () => yRef.value, mo: () => moRef.value, d: () => dRef.value,
  h: () => hRef.value, mi: () => miRef.value, s: () => sRef.value,
}

function onKeydown(unit: DtUnit, e: KeyboardEvent): void {
  if (e.key === 'ArrowUp') { e.preventDefault(); step(unit, 1) }
  else if (e.key === 'ArrowDown') { e.preventDefault(); step(unit, -1) }
}
function onWheel(unit: DtUnit, e: WheelEvent): void {
  if (document.activeElement !== UNIT_REFS[unit]()) return
  e.preventDefault()
  step(unit, e.deltaY < 0 ? 1 : -1)
}
function restrictChars(e: InputEvent): void {
  if (typeof e.data !== 'string' || e.data === '') return
  for (const ch of e.data) {
    if (ch >= '0' && ch <= '9') continue
    e.preventDefault()
    return
  }
}

// Blur (commit): clamp each field to ITS OWN max — no carry (typing sets an
// exact value; only the scroll steppers own carry). Matches `_initScroll`'s
// blur handler + the mo/y -> day re-clamp follow-up.
function onBlur(unit: DtUnit): void {
  // Collapse the seconds box's display back to whole-seconds BEFORE reading
  // `f`/committing — `currentFields()` below reads `sText` as typed (full
  // precision, e.g. "15.250"), so flipping this flag first only affects the
  // POST-commit render (via `emitCommitted` -> `renderFields`), never the
  // value being committed.
  const raw = (unit === 'y' ? yText : unit === 'mo' ? moText : unit === 'd' ? dText
    : unit === 'h' ? hText : unit === 'mi' ? miText : sText).value.trim()
  if (raw === '' || raw === '.') return
  const f = currentFields()
  if (unit === 's') {
    const v = parseInt(raw, 10)
    if (!Number.isFinite(v)) return
    f.s = Math.floor(clampField('s', v, f))
    emitCommitted(f)
    return
  }
  const v = parseInt(raw, 10)
  if (!Number.isFinite(v)) return
  const clamped = clampField(unit, v, f)
  ;(f as unknown as Record<DtUnit, number>)[unit] = clamped
  if (unit === 'mo' || unit === 'y') {
    f.d = clampDayToMonth(f)
  }
  if (dateFieldsValid(f)) emitCommitted(f)
  else renderFields(f)
}

// ── Calendar popover ────────────────────────────────────────────────
const popoverOpen = ref(false)

function onConfirm(f: DtFields): void {
  emitCommitted(f)
  popoverOpen.value = false
}
function onNow(): void {
  emitCommitted(utcDateToFields(new Date()))
  popoverOpen.value = false
}

// P4: focus must land on the roving calendar day when the popover OPENS —
// the real open path (reka's PopoverContent `@open-auto-focus`), never a
// synthetic keydown dispatched straight onto the grid (that is exactly how
// the legacy harness gave a false pass while live arrow-nav did nothing).
function onOpenAutoFocus(e: Event): void {
  e.preventDefault()
  focusRovingDay()
}
function focusRovingDay(): void {
  const host = popoverBodyEl.value
  if (!host) return
  const target = (host.querySelector('[data-selected]') as HTMLElement | null)
    ?? (host.querySelector('[tabindex="0"]') as HTMLElement | null)
  target?.focus()
}
const popoverBodyEl = ref<HTMLElement | null>(null)
</script>

<template>
  <span
    class="seg-dt-row"
    :class="{ 'seg-dt-disabled': disabled }"
    role="group" :aria-label="label"
  >
      <input
        ref="yRef" v-model="yText" class="ov-epoch-box" type="text" inputmode="numeric"
        title="Year" style="width:42px;" :disabled="disabled"
        :min="MIN_YEAR" :max="maxYear()"
        @beforeinput="restrictChars($event as InputEvent)"
        @blur="onBlur('y')" @keydown="onKeydown('y', $event)" @wheel="onWheel('y', $event)"
      >
      <span class="ov-epoch-sep">-</span>
      <input
        ref="moRef" v-model="moText" class="ov-epoch-box" type="text" inputmode="numeric"
        title="Month" style="width:26px;" :disabled="disabled"
        @beforeinput="restrictChars($event as InputEvent)"
        @blur="onBlur('mo')" @keydown="onKeydown('mo', $event)" @wheel="onWheel('mo', $event)"
      >
      <span class="ov-epoch-sep">-</span>
      <input
        ref="dRef" v-model="dText" class="ov-epoch-box" type="text" inputmode="numeric"
        title="Day" style="width:26px;" :disabled="disabled"
        @beforeinput="restrictChars($event as InputEvent)"
        @blur="onBlur('d')" @keydown="onKeydown('d', $event)" @wheel="onWheel('d', $event)"
      >
      <span class="ov-epoch-sep" style="margin-left:3px;" />
      <input
        ref="hRef" v-model="hText" class="ov-epoch-box" type="text" inputmode="numeric"
        title="Hour" style="width:26px;" :disabled="disabled"
        @beforeinput="restrictChars($event as InputEvent)"
        @blur="onBlur('h')" @keydown="onKeydown('h', $event)" @wheel="onWheel('h', $event)"
      >
      <span class="ov-epoch-sep">:</span>
      <input
        ref="miRef" v-model="miText" class="ov-epoch-box" type="text" inputmode="numeric"
        title="Minute" style="width:26px;" :disabled="disabled"
        @beforeinput="restrictChars($event as InputEvent)"
        @blur="onBlur('mi')" @keydown="onKeydown('mi', $event)" @wheel="onWheel('mi', $event)"
      >
      <template v-if="showSeconds">
        <span class="ov-epoch-sep">:</span>
        <input
          ref="sRef" v-model="sText" class="ov-epoch-box" type="text" inputmode="numeric"
          title="Seconds" style="width:26px;" :disabled="disabled"
          @beforeinput="restrictChars($event as InputEvent)"
          @blur="onBlur('s')" @keydown="onKeydown('s', $event)" @wheel="onWheel('s', $event)"
        >
      </template>

    <Popover v-model:open="popoverOpen">
      <PopoverTrigger as-child>
        <button type="button" class="dt-cal-btn" title="Pick from calendar" :disabled="disabled">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="4" width="18" height="18" rx="2" /><path d="M16 2v4M8 2v4M3 10h18" />
          </svg>
        </button>
      </PopoverTrigger>
      <PopoverContent
        class="dt-pop"
        :side-offset="4"
        side="bottom"
        align="start"
        :avoid-collisions="true"
        @open-auto-focus="onOpenAutoFocus"
      >
        <!-- `id="dt-pop"` lives on this plain wrapper div, not on
             <PopoverContent> itself: reka-ui's PopoverContent primitive
             generates its own internal `id` (`reka-popover-content-v-N`,
             used for aria-labelledby wiring) that wins over an `id` attr
             passed through the shadcn wrapper's `$attrs` fallthrough, so an
             `id` given directly on <PopoverContent> is silently dropped.
             `.dt-pop`'s own box styling (width/bg/border/shadow) still lands
             on <PopoverContent> via its `class` prop — that one composes
             normally. This inner div is the `#dt-pop` ancestor every lifted
             `#dt-pop .dtc-*` / `#dt-pop .dt-pop-time-row` selector in
             widgets.css needs (only a plain CSS-cascade-layers argument, not
             specificity, is actually required to beat Tailwind's utility
             classes here — widgets.css is a plain unlayered stylesheet
             [imported in app.vue] and Tailwind's utilities live in `@layer
             utilities`, so ANY unlayered rule already outranks ANY layered
             one regardless of specificity — but keeping the id keeps the
             selectors byte-identical to the lift, which is the point). -->
        <div id="dt-pop" ref="popoverBodyEl">
          <DateTimePopover
            :seed-fields="currentFields()"
            :fallback="fallback"
            @confirm="onConfirm"
            @now="onNow"
            @close="popoverOpen = false"
            @refocus-day="focusRovingDay"
          />
        </div>
      </PopoverContent>
    </Popover>
  </span>
</template>
