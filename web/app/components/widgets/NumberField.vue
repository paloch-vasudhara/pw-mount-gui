<script setup lang="ts">
// Ported from ssa-toolkit web/app/components/widgets/NumberField.vue @84f2ded
// NumberField.vue — unified custom spinner for a plain numeric field (T10,
// §4b/T10). Ports frontend/src/number-spinner.js's `enhance` — the
// DOM-scanning half (`enhanceAll`, the MutationObserver, `DD_MANAGED`, the
// `_skip` eligibility list, the `getComputedStyle` fixed-width detection)
// disappears: in Vue this widget is used by being written, and `fixedWidth`
// is now an explicit prop instead of a runtime measurement.

import { computed, ref, watch } from 'vue'
import { decimalsForStep } from '@/utils/dms'
import SpinnerColumn from './SpinnerColumn.vue'

const props = withDefaults(defineProps<{
  modelValue: number | null
  min?: number | null
  max?: number | null
  step?: number
  cycle?: boolean
  fixedWidth?: string | null
  placeholder?: string
  disabled?: boolean
  readonly?: boolean
  /** Forwards to the inner `<input>`'s `id` (T15-2 addition). A bare `id`
   *  attribute on `<NumberField>` falls through to the ROOT `.dms-box-wrap`
   *  span, not the input inside it — legacy markup put the id directly on
   *  the `<input>` (e.g. `#re-mass`), so a caller that needs that exact id
   *  (an e2e locator, a `<label for>`) uses this prop instead of a bare
   *  `id=` attribute. Optional — every pre-T15-2 call site is unaffected. */
  inputId?: string
  /** Signed-spinner mode (T15-6, style.css:5344-5367 `.mnv-sign` — mirrors
   *  the lat/lon N/S `.dms-sign-indicator`). When true, `modelValue` is the
   *  SIGNED total; the `<input>` box only ever shows/steps the MAGNITUDE and
   *  a separate "+"/"−" chip carries the sign (mnv-tool.js `signedVal`/
   *  `setSignedVal`/`_flipSign`/`_componentStep`). A typed negative commits
   *  as a sign flip + magnitude (mnv-tool.js `_onComponentCommit`); arrow/
   *  wheel steps the SIGNED total so crossing zero flips the chip live
   *  (`_componentStep`). Readonly boxes still allow the chip to flip (a
   *  locked preset direction can be reversed in place) — mirrors the
   *  legacy comment on `.mnv-axis-fld .mnv-sign-row`. */
  signed?: boolean
  /** Red value text + red border (e.g. a G7 limit violation). */
  invalid?: boolean
  /** Overrides the INITIAL displayed text only (T15-2-fix R2). Legacy's
   *  static markup sometimes initialises a numeric field with extra
   *  trailing-zero precision the bare number wouldn't produce on its own
   *  (`#re-cd` starts as the string "2.200", not "2.2", while the payload
   *  stays the plain number 2.2). Purely cosmetic — the numeric
   *  modelValue/payload is unaffected; once the user edits or the value
   *  changes externally, the normal `String(modelValue)` formatting takes
   *  over via the existing watcher below. */
  initialText?: string
}>(), {
  min: null,
  max: null,
  step: 1,
  cycle: false,
  fixedWidth: null,
  placeholder: undefined,
  disabled: false,
  readonly: false,
  inputId: undefined,
  initialText: undefined,
  signed: false,
  invalid: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: number | null]
  change: [value: number | null]
}>()

const decimals = computed(() => decimalsForStep(props.step))
const hasMin = computed(() => props.min != null)
const hasMax = computed(() => props.max != null)
const cyclic = computed(() => props.cycle && hasMin.value && hasMax.value && (props.max as number) > (props.min as number))
const cycleSpan = computed(() => (props.max as number) - (props.min as number))

// Signed mode: the box shows |v| (fixed-3, mnv-tool.js `fmt3`), a separate
// chip carries the sign. Non-signed mode: unchanged pre-T15-6 behaviour.
const sign = ref<1 | -1>(props.modelValue != null && props.modelValue < 0 ? -1 : 1)
function fmt3(v: number): string { return Number.isFinite(v) ? v.toFixed(3) : String(v) }
function magText(v: number | null): string {
  return v == null ? '' : (props.signed ? fmt3(Math.abs(v)) : String(v))
}
const text = ref(props.initialText ?? magText(props.modelValue))

watch(() => props.modelValue, (v) => {
  if (props.signed && v != null) sign.value = v < 0 ? -1 : 1
  text.value = magText(v)
})

function signedTotal(): number {
  const mag = parseFloat(text.value) || 0
  return sign.value * mag
}
function flipSign(): void {
  if (props.disabled) return
  sign.value = sign.value === 1 ? -1 : 1
  emit('update:modelValue', signedTotal())
  emit('change', signedTotal())
}

function wrap(v: number): number {
  const span = cycleSpan.value
  const min = props.min as number
  return min + (((v - min) % span) + span) % span
}
function clampVal(v: number): number {
  if (cyclic.value) return wrap(v)
  if (hasMin.value && v < (props.min as number)) v = props.min as number
  if (hasMax.value && v > (props.max as number)) v = props.max as number
  return v
}

const enable = computed(() => {
  if (cyclic.value) return { up: true, dn: true }
  const v = parseFloat(text.value)
  const upDisabled = hasMax.value && isFinite(v) && v >= (props.max as number)
  const dnDisabled = hasMin.value && isFinite(v) && v <= (props.min as number)
  return { up: !upDisabled, dn: !dnDisabled }
})

function doStep(dir: 1 | -1): void {
  if (props.disabled || props.readonly) return
  if (props.signed) {
    // Step the SIGNED total (mnv-tool.js `_componentStep`) so crossing zero
    // flips the chip live while the box keeps showing a magnitude.
    const next = signedTotal() + dir * props.step
    sign.value = next < 0 ? -1 : 1
    const mag = Math.abs(next)
    text.value = fmt3(mag)
    const out = sign.value * mag
    emit('update:modelValue', out)
    emit('change', out)
    return
  }
  let base = parseFloat(text.value)
  if (!isFinite(base)) base = hasMin.value ? (props.min as number) : 0
  const next = +(clampVal(base + dir * props.step)).toFixed(decimals.value)
  if (String(next) === text.value) return
  text.value = String(next)
  emit('update:modelValue', next)
  emit('change', next)
}

function onWheel(e: WheelEvent, el: HTMLInputElement | null): void {
  if (document.activeElement !== el) return
  e.preventDefault()
  doStep(e.deltaY < 0 ? 1 : -1)
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'ArrowUp') { e.preventDefault(); doStep(1) }
  else if (e.key === 'ArrowDown') { e.preventDefault(); doStep(-1) }
}

function onInput(): void {
  if (props.signed) return   // commit happens on blur (mnv-tool.js `_onComponentCommit`)
  const v = parseFloat(text.value)
  emit('update:modelValue', isFinite(v) ? v : null)
}

function onBlur(): void {
  if (props.signed) {
    // A typed negative in the box → flip the chip, keep the box a magnitude
    // (mnv-tool.js `_onComponentCommit`); otherwise just normalize to fmt3.
    const raw = parseFloat(text.value)
    if (Number.isFinite(raw) && raw < 0) {
      sign.value = sign.value === 1 ? -1 : 1
      text.value = fmt3(Math.abs(raw))
    } else if (Number.isFinite(raw)) {
      text.value = fmt3(raw)
    }
    const out = signedTotal()
    emit('update:modelValue', out)
    emit('change', out)
    return
  }
  const v = parseFloat(text.value)
  emit('update:modelValue', isFinite(v) ? v : null)
}

const inputRef = ref<HTMLInputElement | null>(null)

const wrapStyle = computed(() => props.fixedWidth ? { width: props.fixedWidth, flex: '0 0 auto' } : undefined)
</script>

<template>
  <!-- Signed mode wraps chip + spin-wrap in an outer .mnv-sign-row, matching
       legacy's DOM (`<div class="mnv-sign-row"><span class="mnv-sign">…</span>
       <input></div>`, the number-spinner enhancer injecting `.num-spin-wrap`
       around the `<input>` INSIDE that row) — maneuver.css's
       `.mnv-axis-fld .mnv-sign-row .num-spin-wrap` selector depends on
       `.num-spin-wrap` being a DESCENDANT of `.mnv-sign-row`, not the same node. -->
  <span v-if="signed" class="mnv-sign-row" :class="{ 'is-invalid': invalid }">
    <span
      class="mnv-sign"
      :class="{ neg: sign === -1 }"
      title="Toggle sign (direction)"
      @click="flipSign"
    >{{ sign === -1 ? '−' : '+' }}</span>
    <span class="dms-box-wrap num-spin-wrap" :class="{ 'is-invalid': invalid }" :style="wrapStyle">
      <input
        :id="inputId"
        ref="inputRef"
        v-model="text"
        type="number"
        class="num-spin-input"
        :min="min ?? undefined"
        :max="max ?? undefined"
        :step="step"
        :placeholder="placeholder"
        :disabled="disabled"
        :readonly="readonly"
        @input="onInput"
        @blur="onBlur"
        @keydown="onKeydown"
        @wheel="onWheel($event, inputRef)"
      >
      <SpinnerColumn
        :up-disabled="disabled || readonly || !enable.up"
        :dn-disabled="disabled || readonly || !enable.dn"
        @up="doStep(1)"
        @dn="doStep(-1)"
      />
    </span>
  </span>
  <span v-else class="dms-box-wrap num-spin-wrap" :class="{ 'is-invalid': invalid }" :style="wrapStyle">
    <input
      :id="inputId"
      ref="inputRef"
      v-model="text"
      type="number"
      class="num-spin-input"
      :min="min ?? undefined"
      :max="max ?? undefined"
      :step="step"
      :placeholder="placeholder"
      :disabled="disabled"
      :readonly="readonly"
      @input="onInput"
      @blur="onBlur"
      @keydown="onKeydown"
      @wheel="onWheel($event, inputRef)"
    >
    <SpinnerColumn
      :up-disabled="disabled || readonly || !enable.up"
      :dn-disabled="disabled || readonly || !enable.dn"
      @up="doStep(1)"
      @dn="doStep(-1)"
    />
  </span>
</template>
