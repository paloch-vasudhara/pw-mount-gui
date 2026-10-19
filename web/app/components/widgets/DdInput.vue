<script setup lang="ts">
// Ported from ssa-toolkit web/app/components/widgets/DdInput.vue @84f2ded
// DdInput.vue — decimal-degrees single input with cardinal chip + custom
// spinner, sharing sign state with a paired DmsTriad via `signGroup` (T10,
// §4b/T10). Ports `initDdInput` (signed) and `initCycleDdInput` (unsigned
// wrap, via the `cycle` prop) from frontend/src/dms-input.js onto the pure
// core in `utils/dms.ts` (`decimalsForStep` is reused verbatim for the
// display-precision derivation).
//
// DEVIATION (reverted at T10c): the box was `type="text"`. index.html's
// bare DD inputs (`pass-lat`, `launch-lat`, etc.) are always `type="number"`,
// and `initDdInput` never retypes them (only the DMS triad's min/sec boxes
// get switched to text — see dms-input.js ~line 115). The box below is
// `type="number"` again, with `:step` bound from the `step` prop (legacy's
// inline `step="0.01"`/etc.); no `min`/`max` attribute, matching legacy
// (range is enforced in JS only, via `_refreshDdEnable`/`enable` here, never
// as a native attribute on the bare DD input).

import { computed, onMounted, ref, useId, watch } from 'vue'
import { decimalsForStep } from '@/utils/dms'
import { useSignGroup } from '@/composables/useSignGroup'
import { isAllSelected } from '@/utils/selection'
import SpinnerColumn from './SpinnerColumn.vue'

const props = withDefaults(defineProps<{
  modelValue: number
  signGroup?: string | null
  cardinal?: [string, string] | null
  range?: [number, number] | null
  spinnerStep?: number
  step?: number
  /** period for an unsigned cycle DD input (e.g. Azimuth); mutually
   *  exclusive with `cardinal`. */
  cycle?: number | null
  disabled?: boolean
  /** Red value text + red border (e.g. a G7 limit violation). */
  invalid?: boolean
}>(), {
  signGroup: null,
  cardinal: null,
  range: null,
  spinnerStep: 1,
  step: 1,
  cycle: null,
  disabled: false,
  invalid: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: number]
}>()

const autoId = useId()
const isSigned = computed(() => !!props.cardinal && !props.cycle)
// Sign chip locked to "+" when the range cannot go negative (e.g. El 0..90).
const chipLocked = computed(() => isSigned.value && !!props.range && props.range[0] >= 0)
const groupId = computed(() => props.signGroup || autoId)
const grp = isSigned.value ? useSignGroup(groupId.value, props.cardinal ?? null) : null
const localSign = ref<1 | -1>(1)
const sign = grp ? grp.sign : localSign

const displayDecimals = computed(() => decimalsForStep(props.step))
const text = ref('0')
const inputRef = ref<HTMLInputElement | null>(null)
// True after "-" was typed over a fully selected value: the box was cleared so
// the next digits REPLACE it; the sign watcher must not re-render over that.
const pendingReplace = ref(false)

function render(mag: number): void {
  text.value = String(+Math.abs(mag).toFixed(displayDecimals.value))
}

// Non-wrapping range clamp of the signed total (range caps, Goto El 0..90).
function clampTotal(total: number): number {
  const r = props.range
  if (!r || props.cycle || r[1] - r[0] >= 360 - 1e-9) return total
  return Math.min(r[1], Math.max(r[0], total))
}

function emitFromMagnitude(mag: number, s: 1 | -1): void {
  const raw = s * mag
  const total = clampTotal(raw)
  if (total !== raw) {
    // Clamped: adopt the bound's sign (0 -> "+") and magnitude.
    const ns: 1 | -1 = total < 0 ? -1 : 1
    if (ns !== s) { if (grp) grp.setSign(ns); else localSign.value = ns }
    render(Math.abs(total))
    emit('update:modelValue', total + 0)
    return
  }
  render(mag)
  emit('update:modelValue', raw + 0)   // + 0 normalises -0
}

function initFromModel(): void {
  const v = props.modelValue
  const mag = Math.abs(v)
  if (isSigned.value) {
    if (v < 0 && grp) grp.setSign(-1)
  } else if (props.cycle) {
    localSign.value = 1
  }
  render(mag)
}
onMounted(initFromModel)

watch(() => props.modelValue, (v) => {
  // While a typed "-" is waiting for replacement digits, the paired DMS leg
  // (same sign group) may echo the sign change back as a model update; it must
  // not re-render over the cleared box. Blur ends the pending state.
  if (pendingReplace.value) return
  render(Math.abs(v))
  // An external (programmatic) model change must move the chip too. Zero keeps
  // the current chip (a user may have toggled it at 0).
  if (isSigned.value && v !== 0 && !chipLocked.value) {
    const want: 1 | -1 = v < 0 ? -1 : 1
    if (want !== sign.value) {
      if (grp) grp.setSign(want)
      else localSign.value = want
    }
  }
})

// Sign changes from a paired DmsTriad (or our own chip) re-render + re-emit.
watch(sign, (newSign) => {
  if (pendingReplace.value) return
  const mag = Math.abs(parseFloat(text.value) || 0)
  emitFromMagnitude(mag, newSign)
})

const enable = computed(() => {
  if (props.cycle) return { up: true, dn: true }
  if (!props.range) return { up: true, dn: true }
  const span = props.range[1] - props.range[0]
  if (span >= 360 - 1e-9) return { up: true, dn: true }
  const total = sign.value * (parseFloat(text.value) || 0)
  return {
    up: !(total >= props.range[1] - 1e-9),
    dn: !(total <= props.range[0] + 1e-9),
  }
})

function stepSigned(dir: 1 | -1): void {
  const s = sign.value
  const mag = Math.abs(parseFloat(text.value) || 0)
  let total = s * mag + dir * props.spinnerStep
  if (props.range) {
    const span = props.range[1] - props.range[0]
    if (span >= 360 - 1e-9) {
      total = ((total - props.range[0]) % span + span) % span + props.range[0]
      if (total <= props.range[0]) total = props.range[1]
    } else {
      if (total > props.range[1]) total = props.range[1]
      if (total < props.range[0]) total = props.range[0]
    }
  }
  const newSign: 1 | -1 = total < 0 ? -1 : 1
  if (newSign !== s && grp) grp.setSign(newSign)
  else if (newSign !== s) localSign.value = newSign
  emitFromMagnitude(Math.abs(total), newSign)
}

function stepCycle(dir: 1 | -1): void {
  const period = props.cycle!
  const v = parseFloat(text.value) || 0
  let next = v + dir * props.spinnerStep
  next = ((next % period) + period) % period
  render(next)
  emit('update:modelValue', next)
}

function doStep(dir: 1 | -1): void {
  if (props.cycle) stepCycle(dir)
  else stepSigned(dir)
}

function onWheel(e: WheelEvent): void {
  if (document.activeElement !== inputRef.value) return
  e.preventDefault()
  doStep(e.deltaY < 0 ? 1 : -1)
}

function onKeydown(e: KeyboardEvent): void {
  if ((e.key === '-' || e.key === '−') && isSigned.value && !e.ctrlKey && !e.metaKey) {
    // Typing "-" toggles the chip (the box only ever holds a magnitude).
    e.preventDefault()
    if (isAllSelected(inputRef.value) && !chipLocked.value && !props.disabled) {
      // Legacy parity: "-12.5" typed over a selected value gives -12.5 - the
      // "-" makes the chip negative and the next digits REPLACE the value.
      if (grp) grp.setSign(-1)
      else localSign.value = -1
      pendingReplace.value = true
      text.value = ''
    }
    else {
      flipChip()
    }
    return
  }
  if (e.key === 'ArrowUp') { e.preventDefault(); doStep(1) }
  else if (e.key === 'ArrowDown') { e.preventDefault(); doStep(-1) }
}

function restrictChars(e: InputEvent): void {
  if (typeof e.data !== 'string' || e.data === '') return
  for (const ch of e.data) {
    if (ch >= '0' && ch <= '9') continue
    if (ch === '.') continue
    if (isSigned.value && !chipLocked.value && ch === '-') continue
    e.preventDefault()
    return
  }
}

function onInput(): void {
  if (text.value !== '') pendingReplace.value = false
  if (text.value === '' || text.value === '-' || text.value === '.') return
  const v = parseFloat(text.value)
  if (!isFinite(v)) return
  if (v < 0 && isSigned.value) {
    if (!chipLocked.value) {
      if (grp) grp.setSign(-1)
      else localSign.value = -1
    }
    text.value = String(Math.abs(v))
  }
  if (props.range && !props.cycle) {
    // Cap the typed magnitude at the bound on the active side of zero.
    const lim = sign.value < 0 ? -props.range[0] : props.range[1]
    const mag = Math.abs(parseFloat(text.value) || 0)
    if (lim >= 0 && mag > lim) text.value = String(lim)
  }
}

function onBlur(): void {
  pendingReplace.value = false
  if (props.cycle) {
    const v = parseFloat(text.value)
    if (!isFinite(v)) { render(0); emit('update:modelValue', 0); return }
    const wrapped = ((v % props.cycle) + props.cycle) % props.cycle
    render(wrapped)
    emit('update:modelValue', wrapped)
    return
  }
  if (text.value === '' || text.value === '-' || text.value === '.') {
    text.value = '0'
  }
  const mag = Math.abs(parseFloat(text.value) || 0)
  emitFromMagnitude(mag, sign.value)
}

function flipChip(): void {
  if (!grp || chipLocked.value || props.disabled) return
  grp.flip()
}
</script>

<template>
  <span
    class="dms-box-wrap dms-dd-wrap"
    :class="{ 'dms-box-wrap-cardinal': isSigned, 'is-invalid': invalid }"
  >
    <span
      v-if="isSigned && cardinal"
      class="dms-sign-indicator"
      :class="{ neg: sign < 0, locked: chipLocked }"
      data-sign-chip
      :title="chipLocked ? 'Sign fixed by range' : 'Click to flip sign'"
      @click="flipChip"
    >{{ sign < 0 ? cardinal[1] : cardinal[0] }}</span>
    <input
      ref="inputRef"
      v-model="text"
      class="dms-dd-input-cardinal"
      type="number"
      :step="step"
      :disabled="disabled"
      @beforeinput="restrictChars($event as InputEvent)"
      @input="onInput"
      @blur="onBlur"
      @keydown="onKeydown"
      @wheel="onWheel"
    >
    <SpinnerColumn
      :up-disabled="disabled || !enable.up"
      :dn-disabled="disabled || !enable.dn"
      @up="doStep(1)"
      @dn="doStep(-1)"
    />
  </span>
</template>
