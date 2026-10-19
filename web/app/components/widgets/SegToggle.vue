<script setup lang="ts">
// Ported from ssa-toolkit web/app/components/widgets/SegToggle.vue @84f2ded
// SegToggle.vue — T15-0b, the wave's first shared widget. Replaces
// `vtSegGet`/`vtSegSet`/`vtSegPick` (frontend/src/ui-helpers.js 10-29) with
// a v-model'd component. Renders legacy's EXACT DOM
// (`<div class="vt-seg"><button class="active" data-val="…">…</button>…`)
// so the lifted CSS (widgets.css, style.css 965-992) AND every per-tool
// width override (style.css 1678-1717, e.g. `#passes-modal .pass-seg
// button { width: 120px }`) both land unchanged — those overrides target
// `button`, and this component still renders a real `<button>` per option.
//
// 6b.10 item 7: legacy's .vt-seg options were always native <button>s, so
// no new font-theme.css exception is needed here — checked (grep
// `font-theme.css` for `vt-seg`: no hit needed since the existing bare
// `button { font-family: var(--font-button) !important }` rule already
// covers it automatically).
import { computed } from 'vue'

export interface SegToggleOption {
  value: string
  label: string
}

const props = defineProps<{
  modelValue: string
  options: SegToggleOption[]
  /** Extra class(es) for the wrapping .vt-seg div — carries a per-tool
   *  width-override selector, e.g. "pass-seg" for `#passes-modal .pass-seg`. */
  segClass?: string | string[]
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
  change: [value: string]
}>()

const rootClass = computed(() => ['vt-seg', props.segClass].filter(Boolean))

function pick(value: string): void {
  if (props.disabled) return
  emit('update:modelValue', value)
  emit('change', value)
}
</script>

<template>
  <div :class="rootClass">
    <button
      v-for="opt in options"
      :key="opt.value"
      type="button"
      :class="{ active: modelValue === opt.value }"
      :data-val="opt.value"
      :disabled="disabled"
      @click="pick(opt.value)"
    >
      {{ opt.label }}
    </button>
  </div>
</template>
