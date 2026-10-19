<script setup lang="ts">
// Ported from ssa-toolkit web/app/components/widgets/DmsTriad.vue @84f2ded
// DmsTriad.vue — sexagesimal (DMS/HMS) input triad with carry/borrow (T10,
// §4b/T10). Ports frontend/src/dms-input.js's `initTriad` + `initDdInput`'s
// sibling chip mechanic onto the pure core in `utils/dms.ts`. The DOM-scan/
// wrap machinery (`_wrapInput`, MutationObserver, `_skip`) is gone — this
// component IS the wrap.
//
// DEVIATION (reverted at T10b — see supervisor review): an earlier revision
// of this component made the degrees box `type="text"` for ALL modes,
// matching minutes/seconds. That diverged from the legacy deg box
// (frontend/src/dms-input.js's `initTriad` never retypes `deg` — only
// `min`/`sec` are switched to `type="text"`, see lines ~115-116; the deg box
// stays whatever index.html declared it as, always `type="number"`). The
// deg box below is `type="number"` again, with `step="1"` and the same
// min/max attribute logic dms-input.js applies (`deg.setAttribute("min"/
// "max", ...)` when an explicit `range` is given; no attribute — i.e.
// unbounded — for cycle modes, matching `deg.removeAttribute("min"/"max")`).
// Its `input`/`blur` handlers are the SAME generic per-box handlers min/sec
// use (dms-input.js wires all three boxes identically via one `[[deg,...],
// [min,...], [sec,...]].forEach` loop) — there is no deg-specific "no
// rewrite while typing" mechanic to port; the plain `beforeinput` char
// restriction plus the shared onBlur clamp is everything the legacy deg box
// ever had.

import { computed, onMounted, ref, useId, watch } from 'vue'
import {
  MODE_TABLE, type DmsMode, type DmsConfig, type DmsState,
  toTotalArcsec, fromTotalArcsec, writeState, isFrozen, stepBox, clampFields,
  spinnerEnable, padMin, padSec,
} from '@/utils/dms'
import { useSignGroup } from '@/composables/useSignGroup'
import { isAllSelected } from '@/utils/selection'
import SpinnerColumn from './SpinnerColumn.vue'

const props = withDefaults(defineProps<{
  modelValue: number
  mode: DmsMode
  cardinal?: [string, string] | null
  range?: [number, number] | null
  signGroup?: string | null
  disabled?: boolean
  ariaLabel?: string
  /** Red value text + red border (e.g. a G7 limit violation). */
  invalid?: boolean
  /** Per-box inline pixel widths, matching legacy's per-field inline
   *  `style="width:_px;"` (index.html survey: deg 56/min 42/sec 64 for
   *  pass/obs lat-lon — the default shape here; other modals use 38/46/47/
   *  54/60/70, and datetime d/h boxes use 26). The boxes are fixed-width,
   *  NOT flex-stretched, exactly like legacy's `.ov-dms-box` (no `flex`
   *  declared on it — width comes only from this inline style). */
  widths?: { deg: number; min: number; sec: number }
}>(), {
  cardinal: null,
  range: null,
  signGroup: null,
  disabled: false,
  ariaLabel: undefined,
  invalid: false,
  widths: () => ({ deg: 56, min: 42, sec: 64 }),
})

const emit = defineEmits<{
  'update:modelValue': [value: number]
}>()

const autoId = useId()

const minTotalDeg = computed(() => {
  const m = MODE_TABLE[props.mode]
  return props.range ? props.range[0] : (m.signed ? -(m.limit ?? 0) : 0)
})
const hasCardinal = computed(() => !!props.cardinal && minTotalDeg.value < 0)
// The chip is shown whenever a cardinal pair is given on a non-cycle triad;
// it is LOCKED (no toggle, "+") when the range cannot go negative (El 0..90).
const showChip = computed(() => !!props.cardinal && MODE_TABLE[props.mode].period == null)
const chipLocked = computed(() => showChip.value && !hasCardinal.value)

// Deg box min/max attributes — mirrors dms-input.js's initTriad: an
// explicit `range` sets `min`/`max` (hasCardinal clamps min to 0, since the
// box always displays a magnitude); cycle modes (RA hours / Az) carry no
// min/max (`deg.removeAttribute("min"/"max")` — they wrap instead of
// clamping); a limit mode with no explicit range uses the mode's own
// signed limit, matching the min/max baked into index.html's markup for
// that field (e.g. pass-lat-d: min="-90" max="90" for limit:90).
const degMaxDeg = computed(() => {
  const m = MODE_TABLE[props.mode]
  return props.range ? props.range[1] : (m.signed ? (m.limit ?? 0) : (m.period ?? 0))
})
const degMinAttr = computed<number | null>(() => {
  const m = MODE_TABLE[props.mode]
  if (m.period != null) return null
  return hasCardinal.value ? 0 : minTotalDeg.value
})
const degMaxAttr = computed<number | null>(() => {
  const m = MODE_TABLE[props.mode]
  if (m.period != null) return null
  return degMaxDeg.value
})

const cfg = computed<DmsConfig>(() => ({
  mode: props.mode,
  range: props.range ?? null,
  hasCardinal: hasCardinal.value,
}))

const groupId = computed(() => props.signGroup || autoId)
const grp = hasCardinal.value || props.cardinal
  ? useSignGroup(groupId.value, props.cardinal ?? null)
  : null
// Non-cardinal triads (RA hours, Azimuth, Elevation) have no chip and no
// shared sign — a local always-positive ref stands in for `_getSign() === 1`.
const localSign = ref<1 | -1>(1)
const sign = grp ? grp.sign : localSign

const degText = ref('')
const minText = ref('')
const secText = ref('')

const degRef = ref<HTMLInputElement | null>(null)
// "-" typed over a fully selected degrees value: box cleared so the next digits
// replace it; the sign watcher must not re-render over the empty box.
const pendingReplace = ref(false)
const minRef = ref<HTMLInputElement | null>(null)
const secRef = ref<HTMLInputElement | null>(null)

function readD(): number { const v = parseFloat(degText.value); return isFinite(v) ? Math.abs(v) : 0 }
function readM(): number { const v = parseFloat(minText.value); return isFinite(v) ? v : 0 }
function readS(): number { const v = parseFloat(secText.value); return isFinite(v) ? v : 0 }

function renderState(v: DmsState): void {
  degText.value = hasCardinal.value ? String(v.d) : String(v.sign * v.d)
  minText.value = padMin(v.m)
  secText.value = padSec(v.s)
}

// Non-wrapping range clamp of the signed total (range caps).
function clampToRange(next: DmsState): DmsState {
  const r = props.range
  if (!r || MODE_TABLE[props.mode].period != null) return next
  const total = toTotalArcsec(next)
  const lo = r[0] * 3600
  const hi = r[1] * 3600
  if (total >= lo - 1e-6 && total <= hi + 1e-6) return next
  const bound = total < lo ? lo : hi
  return fromTotalArcsec(bound, cfg.value) ?? next
}

function applyAndEmit(raw: DmsState): void {
  const next = clampToRange(raw)
  if (grp) grp.setSign(next.sign)
  else localSign.value = next.sign
  renderState(next)
  emit('update:modelValue', toTotalArcsec(next) / 3600 + 0)   // + 0 normalises -0
}

function initFromModel(): void {
  const total = props.modelValue * 3600
  const st = fromTotalArcsec(total, cfg.value) ?? { sign: 1 as const, d: 0, m: 0, s: 0 }
  if (grp && st.sign < 0) grp.setSign(-1)
  else if (!grp) localSign.value = st.sign
  renderState({ ...st, sign: sign.value })
}

onMounted(initFromModel)

// Re-render (and re-emit) whenever the shared sign changes — whether WE
// flipped it (chip click) or a paired widget in the same signGroup did. This
// is the collapsed public/silent split described in useSignGroup.ts: there
// is one reactive source, so one handler reacts to every write to it.
watch(sign, (newSign) => {
  if (pendingReplace.value) return
  const next = writeState({ sign: newSign, d: readD(), m: readM(), s: readS() }, cfg.value)
  renderState(next)
  emit('update:modelValue', toTotalArcsec(next) / 3600 + 0)
})

// External modelValue changes (programmatic, e.g. scenario load) re-render.
watch(() => props.modelValue, () => {
  if (pendingReplace.value) return   // see DdInput: keep the cleared degrees box
  const total = props.modelValue * 3600
  const st = fromTotalArcsec(total, cfg.value)
  if (st) renderState({ ...st, sign: sign.value })
})

function currentState(): DmsState {
  return { sign: sign.value, d: readD(), m: readM(), s: readS() }
}

function step(box: 'd' | 'm' | 's', dir: 1 | -1): void {
  const next = stepBox(currentState(), box, dir, cfg.value)
  applyAndEmit(next)
}

function onBlur(): void {
  pendingReplace.value = false
  // degText carries a literal leading "-" when the user typed one on a
  // cardinal deg box (sign is allowed there) — pass it through as-is so
  // clampFields' sign-absorption branch (rawDeg < 0) sees it, mirroring
  // `_clampFieldsAndRender`'s `parseFloat(deg.value)` read.
  const rawD = parseFloat(degText.value)
  const next = clampFields({ d: isFinite(rawD) ? rawD : 0, m: readM(), s: readS(), sign: sign.value }, cfg.value)
  applyAndEmit(next)
}

function onWheel(box: 'd' | 'm' | 's', el: HTMLInputElement | null, e: WheelEvent): void {
  if (document.activeElement !== el) return
  e.preventDefault()
  step(box, e.deltaY < 0 ? 1 : -1)
}

function onKeydown(box: 'd' | 'm' | 's', e: KeyboardEvent): void {
  if (box === 'd' && (e.key === '-' || e.key === '−') && showChip.value && !e.ctrlKey && !e.metaKey) {
    // Typing "-" in the degrees box toggles the chip (box holds a magnitude).
    e.preventDefault()
    if (isAllSelected(degRef.value) && !chipLocked.value && !props.disabled && grp) {
      // "-12" typed over a selected value: chip negative, next digits replace.
      grp.setSign(-1)
      pendingReplace.value = true
      degText.value = ''
      bumpDisplay()
    }
    else {
      flipChip()
    }
    return
  }
  if (e.key === 'ArrowUp') { e.preventDefault(); step(box, 1) }
  else if (e.key === 'ArrowDown') { e.preventDefault(); step(box, -1) }
}

function restrictChars(e: InputEvent, opts: { decimal: boolean; sign: boolean }): void {
  if (typeof e.data !== 'string' || e.data === '') return
  for (const ch of e.data) {
    if (ch >= '0' && ch <= '9') continue
    if (opts.decimal && ch === '.') continue
    if (opts.sign && ch === '-') continue
    e.preventDefault()
    return
  }
}

function flipChip(): void {
  if (!grp || chipLocked.value || props.disabled) return
  grp.flip()
}

const frozen = computed(() => isFrozen(currentStateForDisplay(), cfg.value))
// A display-only reactive snapshot so `frozen`/`enable` recompute when the
// text refs change (they are not part of Vue's dependency graph otherwise).
const displayTick = ref(0)
function currentStateForDisplay(): DmsState {
  // touch displayTick so computed re-evaluates after every render/typed input
  void displayTick.value
  return currentState()
}
function bumpDisplay(): void { displayTick.value++ }

const enable = computed(() => spinnerEnable(currentStateForDisplay(), cfg.value))

function onInput(): void {
  if (degText.value !== '') pendingReplace.value = false
  bumpDisplay()
}

defineExpose({ step, onBlur })
</script>

<template>
  <span class="dms-triad" :class="{ 'is-invalid': invalid }" :aria-label="ariaLabel" role="group">
    <span class="dms-box-wrap" :class="{ 'dms-box-wrap-cardinal': showChip }">
      <span
        v-if="showChip && cardinal"
        class="dms-sign-indicator"
        :class="{ neg: sign < 0, locked: chipLocked }"
        data-sign-chip
        :title="chipLocked ? 'Sign fixed by range' : 'Click to flip sign'"
        @click="flipChip"
      >{{ sign < 0 ? cardinal[1] : cardinal[0] }}</span>
      <input
        ref="degRef"
        v-model="degText"
        class="ov-dms-box"
        type="number"
        step="1"
        :min="degMinAttr ?? undefined"
        :max="degMaxAttr ?? undefined"
        :style="{ width: widths.deg + 'px' }"
        :disabled="disabled"
        @beforeinput="restrictChars($event as InputEvent, { decimal: false, sign: hasCardinal })"
        @input="onInput"
        @blur="onBlur"
        @keydown="onKeydown('d', $event)"
        @wheel="onWheel('d', degRef, $event)"
      >
      <SpinnerColumn
        :up-disabled="disabled || !enable.up"
        :dn-disabled="disabled || !enable.dn"
        @up="step('d', 1)"
        @dn="step('d', -1)"
      />
    </span>
    <span class="dms-box-wrap">
      <input
        ref="minRef"
        v-model="minText"
        class="ov-dms-box"
        type="text"
        inputmode="numeric"
        :style="{ width: widths.min + 'px' }"
        :readonly="frozen"
        :disabled="disabled"
        @beforeinput="restrictChars($event as InputEvent, { decimal: false, sign: false })"
        @input="onInput"
        @blur="onBlur"
        @keydown="onKeydown('m', $event)"
        @wheel="onWheel('m', minRef, $event)"
      >
      <SpinnerColumn
        :up-disabled="disabled || !enable.up"
        :dn-disabled="disabled || !enable.dn"
        @up="step('m', 1)"
        @dn="step('m', -1)"
      />
    </span>
    <span class="dms-box-wrap">
      <input
        ref="secRef"
        v-model="secText"
        class="ov-dms-box"
        type="text"
        inputmode="decimal"
        :style="{ width: widths.sec + 'px' }"
        :readonly="frozen"
        :disabled="disabled"
        @beforeinput="restrictChars($event as InputEvent, { decimal: true, sign: false })"
        @input="onInput"
        @blur="onBlur"
        @keydown="onKeydown('s', $event)"
        @wheel="onWheel('s', secRef, $event)"
      >
      <SpinnerColumn
        :up-disabled="disabled || !enable.up"
        :dn-disabled="disabled || !enable.dn"
        @up="step('s', 1)"
        @dn="step('s', -1)"
      />
    </span>
  </span>
</template>
