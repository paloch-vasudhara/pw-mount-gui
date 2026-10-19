<script setup lang="ts">
// Ported from ssa-toolkit web/app/components/widgets/DateTimePopover.vue @84f2ded
// DateTimePopover.vue — the two-step popover BODY (calendar pane -> Next ->
// time pane -> Confirm), ported from frontend/src/datetime-picker.js's
// `_buildPopup` / `_gotoDateStep` / `_gotoTimeStep` / `_confirmPicker` /
// `_pickNow` (§4b/T11). The popover CHROME (trigger, portal, open/close,
// P4's open-auto-focus) is DateTimeField.vue's job — this component only
// owns the two panes + footer and is agnostic to how it got opened.
//
// P9: the time pane stays bespoke — segmented H:Mi:S boxes with their own
// `.dt-time-spin` ▲/▼ mechanic, `type="text"` always. Never wrap these in
// <NumberField> (the legacy `.ov-epoch-box` skip rule from number-spinner.js
// becomes "simply do not use NumberField here").
// P10: Now (UTC) / Next / Back / Confirm flow and the two-step
// date -> time progression, unchanged.
// P11: all date math is UTC — see utils/datetime-fields.ts's file banner.

import { computed, ref, watch } from 'vue'
import {
  type DtFields, dateFieldsValid, utcDateToFields, padSec, pad2, clampNum,
} from '@/utils/datetime-fields'
import AppCalendar from './AppCalendar.vue'

const props = defineProps<{
  /** The field's current value (from DateTimeField's segmented boxes),
   *  ALWAYS read fresh on open — the popover re-seeds itself from this each
   *  time it becomes visible via `seed()` below. */
  seedFields: DtFields
  fallback: Date
}>()

const emit = defineEmits<{
  confirm: [DtFields]
  now: []
  close: []
  'refocus-day': []
}>()

// Seconds precision only (no fractional seconds).
const dec = 0

const step = ref<'date' | 'time'>('date')

// ── Date step ───────────────────────────────────────────────────────
const pickedY = ref(0)
const pickedMo = ref(0)
const pickedD = ref(0)

const pickedDate = computed(() => new Date(Date.UTC(pickedY.value, pickedMo.value - 1, pickedD.value)))
const calendarFallback = computed(() => props.fallback)

function onPickDate(d: Date): void {
  pickedY.value = d.getUTCFullYear()
  pickedMo.value = d.getUTCMonth() + 1
  pickedD.value = d.getUTCDate()
}

// ── Time step ───────────────────────────────────────────────────────
const hText = ref('00')
const miText = ref('00')
const sText = ref(padSec(0, 0))

function seed(): void {
  const f = props.seedFields
  if (dateFieldsValid(f)) {
    pickedY.value = f.y
    pickedMo.value = f.mo
    pickedD.value = f.d
  } else {
    const sc = utcDateToFields(props.fallback)
    pickedY.value = sc.y
    pickedMo.value = sc.mo
    pickedD.value = sc.d
  }
  hText.value = pad2(f.h)
  miText.value = pad2(f.mi)
  sText.value = padSec(Math.floor(f.s), dec)
  step.value = 'date'
}
seed()
// Re-seed only when the field VALUES change. DateTimeField passes a fresh
// `currentFields()` object on every parent render, so watching the object's
// identity re-ran seed() (resetting `step` to 'date') right after "Next" was
// clicked and the time pane could never be reached.
watch(
  () => { const f = props.seedFields; return [f.y, f.mo, f.d, f.h, f.mi, f.s].join('|') },
  seed,
)

function stepTimeOfDay(dir: 1 | -1, unit: 'h' | 'mi' | 's'): void {
  const stepSec = unit === 'h' ? 3600 : unit === 'mi' ? 60 : 1
  let total = (parseInt(hText.value, 10) || 0) * 3600
    + (parseInt(miText.value, 10) || 0) * 60
    + (parseFloat(sText.value) || 0)
  total = ((total + dir * stepSec) % 86400 + 86400) % 86400
  let hh = Math.floor(total / 3600)
  const rem = total - hh * 3600
  let mm = Math.floor(rem / 60)
  let ss = Math.floor(rem - mm * 60)
  if (ss >= 60) { ss -= 60; mm += 1 }
  if (mm >= 60) { mm -= 60; hh += 1 }
  if (hh >= 24) hh -= 24
  hText.value = pad2(hh)
  miText.value = pad2(mm)
  sText.value = padSec(ss, 0)
}
function onTimeKeydown(unit: 'h' | 'mi' | 's', e: KeyboardEvent): void {
  if (e.key === 'ArrowUp') { e.preventDefault(); stepTimeOfDay(1, unit) }
  else if (e.key === 'ArrowDown') { e.preventDefault(); stepTimeOfDay(-1, unit) }
}
function onTimeWheel(unit: 'h' | 'mi' | 's', el: HTMLInputElement | null, e: WheelEvent): void {
  if (document.activeElement !== el) return
  e.preventDefault()
  stepTimeOfDay(e.deltaY < 0 ? 1 : -1, unit)
}
function restrictChars(e: InputEvent): void {
  if (typeof e.data !== 'string' || e.data === '') return
  for (const ch of e.data) {
    if (ch >= '0' && ch <= '9') continue
    e.preventDefault()
    return
  }
}
function onTimeBlur(unit: 'h' | 'mi' | 's'): void {
  const box = unit === 'h' ? hText : unit === 'mi' ? miText : sText
  const raw = box.value.trim()
  if (raw === '' || raw === '.') return
  if (unit === 's') {
    const v = parseFloat(raw)
    if (!Number.isFinite(v)) return
    sText.value = padSec(clampNum(Math.floor(v), 0, 59), 0)
    return
  }
  const v = parseInt(raw, 10)
  if (!Number.isFinite(v)) return
  box.value = pad2(clampNum(v, 0, unit === 'h' ? 23 : 59))
}

const hRef = ref<HTMLInputElement | null>(null)
const miRef = ref<HTMLInputElement | null>(null)
const sRef = ref<HTMLInputElement | null>(null)

// ── Footer ──────────────────────────────────────────────────────────
function goToTimeStep(): void {
  step.value = 'time'
}
function goToDateStep(): void {
  step.value = 'date'
  emit('refocus-day')   // returning from the time step re-arms arrow nav (P4)
}
function onConfirm(): void {
  const h = clampNum(parseInt(hText.value, 10) || 0, 0, 23)
  const mi = clampNum(parseInt(miText.value, 10) || 0, 0, 59)
  const s = clampNum(Math.floor(parseFloat(sText.value) || 0), 0, 59)
  emit('confirm', { y: pickedY.value, mo: pickedMo.value, d: pickedD.value, h, mi, s })
}
function onNow(): void {
  emit('now')
}

defineExpose({ seed, step })
</script>

<template>
  <div class="dt-pop-head">
    <span id="dt-pop-title">{{ step === 'date' ? 'Select date' : 'Select time (UTC)' }}</span>
    <button type="button" class="dt-pop-x" title="Close" @click="emit('close')">&times;</button>
  </div>

  <div v-show="step === 'date'" class="dt-pop-pane">
    <AppCalendar :model-value="pickedDate" :fallback="calendarFallback" @update:model-value="onPickDate" />
  </div>

  <div v-show="step === 'time'" class="dt-pop-pane">
    <div class="dt-pop-time-row seg-dt-row">
      <div class="dt-time-unit">
        <button type="button" class="dt-time-spin" @click="stepTimeOfDay(1, 'h')">
          &#9650;
        </button>
        <input
          ref="hRef" v-model="hText" class="ov-epoch-box" type="text" inputmode="numeric"
          min="0" max="23" style="width:44px;" title="Hour"
          @beforeinput="restrictChars($event as InputEvent)"
          @keydown="onTimeKeydown('h', $event)" @wheel="onTimeWheel('h', hRef, $event)" @blur="onTimeBlur('h')"
        >
        <button type="button" class="dt-time-spin" @click="stepTimeOfDay(-1, 'h')">
          &#9660;
        </button>
      </div>
      <span class="ov-epoch-sep">:</span>
      <div class="dt-time-unit">
        <button type="button" class="dt-time-spin" @click="stepTimeOfDay(1, 'mi')">
          &#9650;
        </button>
        <input
          ref="miRef" v-model="miText" class="ov-epoch-box" type="text" inputmode="numeric"
          min="0" max="59" style="width:44px;" title="Minute"
          @beforeinput="restrictChars($event as InputEvent)"
          @keydown="onTimeKeydown('mi', $event)" @wheel="onTimeWheel('mi', miRef, $event)" @blur="onTimeBlur('mi')"
        >
        <button type="button" class="dt-time-spin" @click="stepTimeOfDay(-1, 'mi')">
          &#9660;
        </button>
      </div>
      <span class="ov-epoch-sep">:</span>
      <div class="dt-time-unit">
        <button type="button" class="dt-time-spin" @click="stepTimeOfDay(1, 's')">
          &#9650;
        </button>
        <input
          ref="sRef" v-model="sText" class="ov-epoch-box" type="text" inputmode="numeric"
          min="0" max="59" style="width:44px;" title="Seconds"
          @beforeinput="restrictChars($event as InputEvent)"
          @keydown="onTimeKeydown('s', $event)" @wheel="onTimeWheel('s', sRef, $event)" @blur="onTimeBlur('s')"
        >
        <button type="button" class="dt-time-spin" @click="stepTimeOfDay(-1, 's')">
          &#9660;
        </button>
      </div>
    </div>
  </div>

  <div class="dt-pop-foot">
    <button type="button" class="dt-pop-btn" @click="onNow">
      Now (UTC)
    </button>
    <span class="dt-pop-foot-right">
      <button v-if="step === 'time'" type="button" class="dt-pop-btn" @click="goToDateStep">
        Back
      </button>
      <button v-if="step === 'date'" type="button" class="dt-pop-btn primary" @click="goToTimeStep">
        Next &rsaquo;
      </button>
      <button v-if="step === 'time'" type="button" class="dt-pop-btn primary" @click="onConfirm">
        Confirm
      </button>
    </span>
  </div>
</template>
