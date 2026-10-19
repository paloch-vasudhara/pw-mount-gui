<script setup lang="ts">
// Ported from ssa-toolkit web/app/components/widgets/AngleField.vue @84f2ded
// AngleField.vue — composite lat/lon/RA/Dec/Az/El row: a label, an
// `.ov-fmt-sel` format <select>, and the mutually-exclusive `.ov-input-group`
// legs it toggles between (T10d, §4b/T10d — GAP FIX on already-shipped T10
// work, found in owner review of C2 2026-09-10). Composes the already-shipped
// <DdInput>/<DmsTriad>; it does NOT re-implement any input behaviour (no
// keystroke handling, no spinners, no sign chip, no clamping — all of that
// already lives in the two leaf widgets and in utils/dms.ts).
//
// Ports frontend/src/observer-view.js's _decToSplit / _ovReadCurrentValue /
// _ovSetupRA / _ovSetupDMS / _setLatLonValue (~200-400) and the markup at
// frontend/index.html ~720-760 (pass-lat/-lon) + ~1976-1999
// (ov-img-ra/-dec/-az/-el).
//
// NATIVE <select> DEFERRAL (dated 2026-09-10, superseded 2026-09-11 by
// T12-d): this note originally said the format select would stay a plain
// native <select class="ov-fmt-sel"> until T18 replaced every app select
// with shadcn/reka Select. T12-d (§4c/T12-d, carry-over C3-b) PULLED THAT
// SWAP FORWARD for this one control: the C2 sign-off's font-size hypothesis
// (10px, taken from the `.ov-fmt-sel` rule alone) was measured and found
// wrong — the legacy on-screen box is a 13px cs2 button because
// `.form-row select` outranks the bare `.ov-fmt-sel` class and
// custom-select.js copies the resolved 13px onto the wrapper — and a native
// select cannot reach that target's chevron/panel/ellipsis behaviour at all.
// So as of T12-d this component renders <FormatSelect> (reka Select styled
// to the derived 13px metrics, see shell.css §7). T18 is unaffected for
// every OTHER select in the app; only this one was moved early.
//
// FOUR DEVIATIONS recorded here (mechanism changed, behaviour identical):
//  1. Sign handling collapses. Legacy's `_ovSetupDMS` branches on
//     `hasSignGrp()` and writes MAGNITUDES into both DD and DMS boxes when a
//     shared sign chip is registered. Here <DdInput>/<DmsTriad> already own
//     the magnitude/chip split via useSignGroup, and AngleField's model is
//     signed canonical, so AngleField never touches sign at all — it just
//     passes the same `signGroup` id to both children and lets the shared
//     ref do it.
//  2. `sel._prevFmt` bookkeeping is gone. It existed only so legacy could
//     know which DOM boxes to read BEFORE the switch. With a single model
//     there is no "before" — `convert()` always reads the current canonical
//     value directly.
//  3. Zero renders as 0, not blank. Legacy blanks the box on a format switch
//     when the value is 0 (`hours ? ... : ""`). The Vue children always
//     render a number, so a zeroed field shows "0" where legacy showed an
//     empty box. The *arithmetic* (utils/angle-format.ts) keeps the
//     `? : ''` branch verbatim; only the DOM-level blanking is not
//     reproduced (open question for the owner, see docs/nuxt_m1_build.html
//     §4b/T10d).
//  4. Triad formatting on a format switch. Legacy writes RAW unpadded values
//     into the DMS/HMS boxes on switch ("13","45","0" — see
//     legacy-pass-triads.png), because `_ovSetupDMS`/`_ovSetupRA` set
//     `input.value` directly from the split integers. Here <DmsTriad>
//     renders through `writeState`/`padMin`/`padSec` on EVERY render,
//     switch included, so the same switch shows "13","45","00.000" — the
//     triad's normal rest-state formatting (its own T10 contract), not a
//     switch-specific behaviour. Kept as-is: the padded form is what the
//     triad always shows once mounted (e.g. loading a scenario), so a
//     switch producing unpadded digits for one frame would itself be the
//     inconsistency.

import { computed, ref, useId } from 'vue'
import DdInput from './DdInput.vue'
import DmsTriad from './DmsTriad.vue'
import FormatSelect from './FormatSelect.vue'
import {
  FORMATS_FOR, FORMAT_LABEL, convert,
  type AngleKind, type AngleFormat,
} from '@/utils/angle-format'
import type { DmsMode } from '@/utils/dms'

const props = withDefaults(defineProps<{
  /** CANONICAL value. degrees for kind 'latlon' | 'angle';
   *  HOURS (0-24, signed for symmetry) for kind 'ra' — this matches what the
   *  legacy modals read back (_ovReadCurrentValue: "For RA: returns
   *  hour-decimal (0-24). For Dec/Az/El: returns degrees."). Consumers that
   *  want RA in degrees multiply by 15 at the call site, as legacy does. */
  modelValue: number
  /** Drives the format list, the child modes and the child units. */
  kind: AngleKind
  /** Which of FORMATS_FOR[kind] is showing. v-model:format — OPTIONAL:
   *  uncontrolled (internal ref seeded from `defaultFormat`) when unbound. */
  format?: AngleFormat
  defaultFormat?: AngleFormat        // default: FORMATS_FOR[kind][0]  ('dd' / 'hd')
  /** Restrict the offered formats (a subset of FORMATS_FOR[kind], same order). */
  formats?: AngleFormat[]
  /** Row label text; a `label` slot overrides it (tooltip case, below). */
  label?: string
  /** Passed to BOTH children so the DD box and the triad share one chip. */
  signGroup?: string | null
  /** Passed through to the children unchanged. */
  cardinal?: [string, string] | null   // e.g. ['N','S'] / ['E','W']
  range?: [number, number] | null      // e.g. [-90,90] / [0,90]
  step?: number                        // DD box step (legacy: 0.01 / 0.0001 / 0.001)
  widths?: { deg: number; min: number; sec: number }
  /** LOCK: disables the VALUE inputs only. The format select stays live. */
  disabled?: boolean
  /** `.ov-label-group-wide` instead of `.ov-label-group` (Pass / Observer /
   *  Add-Equipment rows); false for the compact Imaging rows. */
  wide?: boolean
  /** Row class pass-through — `form-row-latlon` for lat/lon rows (5px gap),
   *  `form-row compact` for the compact Imaging rows. */
  rowClass?: string
  /** The select's accessible title. Defaults to `${label} format`. */
  selectTitle?: string
  /** Signed kind with a "+"/"-" sign chip (El, Dec): shorthand for
   *  `cardinal=['+','−']` with a default range of [-90, 90]. Narrow the
   *  `range` (e.g. [0, 90]) to cap it; with min >= 0 the chip is locked to "+". */
  signed?: boolean
  /** Red value text + red border (e.g. a G7 limit violation). */
  invalid?: boolean
}>(), {
  format: undefined,
  defaultFormat: undefined,
  formats: undefined,
  label: '',
  signGroup: null,
  cardinal: null,
  range: null,
  step: 0.01,
  widths: undefined,
  disabled: false,
  wide: false,
  rowClass: 'form-row form-row-latlon',
  selectTitle: undefined,
  signed: false,
  invalid: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: number]
  'update:format': [fmt: AngleFormat]
}>()

const effCardinal = computed<[string, string] | null>(
  () => props.cardinal ?? (props.signed ? ['+', '−'] : null),
)
const effRange = computed<[number, number] | null>(
  () => props.range ?? (props.signed ? [-90, 90] : null),
)
// DD and DMS legs share ONE sign chip: default to a private group id.
const autoGroup = useId()
const effGroup = computed(() => props.signGroup ?? (effCardinal.value ? autoGroup : null))

const offeredFormats = computed<AngleFormat[]>(() => {
  const all = FORMATS_FOR[props.kind]
  if (!props.formats || props.formats.length === 0) return all
  return all.filter((f) => props.formats!.includes(f))
})

function resolveDefault(): AngleFormat {
  const offered = offeredFormats.value
  if (props.defaultFormat && offered.includes(props.defaultFormat)) return props.defaultFormat
  return offered[0]
}

const internalFormat = ref<AngleFormat>(resolveDefault())
const currentFormat = computed<AngleFormat>({
  get: () => (props.format !== undefined ? props.format : internalFormat.value),
  set: (v) => { internalFormat.value = v },
})
// An out-of-list defaultFormat / an out-of-list current controlled format
// falls back to formats[0] rather than rendering an empty select.
const effectiveFormat = computed<AngleFormat>(() => {
  const offered = offeredFormats.value
  return offered.includes(currentFormat.value) ? currentFormat.value : offered[0]
})

const selTitle = computed(() => props.selectTitle ?? `${props.label} format`)

function onSelectChange(newFmt: string): void {
  const fmt = newFmt as AngleFormat
  const next = convert(props.modelValue, props.kind, fmt)
  if (next !== props.modelValue) emit('update:modelValue', next)
  currentFormat.value = fmt
  emit('update:format', fmt)
}

const formatOptions = computed(() => offeredFormats.value.map((f) => ({ value: f, label: FORMAT_LABEL[f] })))
// OV UI pass 2 (item 6, GLOBAL) — 48 -> 58. 48px could never actually fit
// the longest format label ("DMS"/"HMS", 3 chars) at this select's real
// 13px rendered font (FormatSelect's own header note: the resolved legacy
// size was always 13px, not the stylesheet's stale 10px) — the old default
// was only ever viable at 11px. observer-hemisphere-sidebar.css used to
// patch this to 58px for the OV sidebar alone via a higher-specificity
// class rule, but that rule could never win against this prop's OWN inline
// `width` style on FormatSelect's trigger (inline always beats a class
// selector) — it was dead CSS, deleted alongside this fix. `wide`'s 60
// (Pass/Observer/Add-Equipment rows) is untouched.
const selectWidth = computed(() => (props.wide ? 60 : 58))

// ── RA leg wiring — the DD leg is the ONLY place the child sees a
// non-canonical unit: AngleField multiplies by 15 going in and divides by
// 15 coming out (_ovSetupRA's dd branch), so the DD child's own cycle:360
// never has to know it is really hours underneath.
const raDdValue = computed(() => props.modelValue * 15)
function onRaDdUpdate(deg: number): void {
  emit('update:modelValue', deg / 15)
}

// ── DMS/HMS triad mode, per kind — mirrors the mode the consumer already
// states via `range`/`cardinal` for kind='angle'; fixed modes for the other
// two kinds.
const triadMode = computed<DmsMode>(() => {
  if (props.kind === 'ra') return 'cycle:24'
  // Az (kind='angle', no cardinal, full-circle range) cycles 0-360.
  if (!effCardinal.value && effRange.value && effRange.value[1] - effRange.value[0] >= 360 - 1e-9) {
    return 'cycle:360'
  }
  // Longitude (kind='latlon', range [-180,180]) is the only limit:180 case;
  // everything else signed (Latitude, Dec, El) is limit:90.
  if (effRange.value && Math.abs(effRange.value[1]) >= 180 - 1e-9) return 'limit:180'
  return 'limit:90'
})
</script>

<template>
  <div :class="rowClass">
    <span class="ov-label-group" :class="{ 'ov-label-group-wide': wide }">
      <slot name="label">
        <label>{{ label }}</label>
      </slot>
      <FormatSelect
        :model-value="effectiveFormat"
        :options="formatOptions"
        :width="selectWidth"
        :title="selTitle"
        @update:model-value="onSelectChange"
      />
    </span>

    <!-- latlon / angle: DD leg + DMS leg -->
    <template v-if="kind !== 'ra'">
      <span class="ov-input-group" :class="{ hidden: effectiveFormat !== 'dd' }">
        <DdInput
          :model-value="modelValue"
          :sign-group="effGroup"
          :cardinal="effCardinal"
          :range="effRange"
          :step="step"
          :disabled="disabled"
          :invalid="invalid"
          @update:model-value="(v: number) => emit('update:modelValue', v)"
        />
      </span>
      <span class="ov-input-group" :class="{ hidden: effectiveFormat !== 'dms' }">
        <DmsTriad
          :model-value="modelValue"
          :mode="triadMode"
          :cardinal="effCardinal"
          :range="effRange"
          :sign-group="effGroup"
          :widths="widths"
          :disabled="disabled"
          :invalid="invalid"
          @update:model-value="(v: number) => emit('update:modelValue', v)"
        />
      </span>
    </template>

    <!-- ra: HD leg + HMS leg + DD leg (degrees, ×15 conversion at the edge) -->
    <template v-else>
      <span class="ov-input-group" :class="{ hidden: effectiveFormat !== 'hd' }">
        <DdInput
          :model-value="modelValue"
          :cycle="24"
          :step="step"
          :disabled="disabled"
          :invalid="invalid"
          @update:model-value="(v: number) => emit('update:modelValue', v)"
        />
      </span>
      <span class="ov-input-group" :class="{ hidden: effectiveFormat !== 'hms' }">
        <DmsTriad
          :model-value="modelValue"
          mode="cycle:24"
          :widths="widths"
          :disabled="disabled"
          :invalid="invalid"
          @update:model-value="(v: number) => emit('update:modelValue', v)"
        />
      </span>
      <span class="ov-input-group" :class="{ hidden: effectiveFormat !== 'dd' }">
        <DdInput
          :model-value="raDdValue"
          :cycle="360"
          :step="step"
          :disabled="disabled"
          :invalid="invalid"
          @update:model-value="onRaDdUpdate"
        />
      </span>
    </template>
  </div>
</template>
