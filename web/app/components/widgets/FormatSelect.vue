<script setup lang="ts">
// Ported from ssa-toolkit web/app/components/widgets/FormatSelect.vue @84f2ded
// FormatSelect.vue — T12-d (carry-over C3-b). Replaces the native
// `<select class="ov-fmt-sel">` with a shadcn/reka Select styled to the
// LEGACY *rendered* metrics, not the stale `.ov-fmt-sel` stylesheet rule.
//
// See app/assets/css/shell.css section 7 for the full cascade derivation:
// `.form-row select` (frontend/style.css 1946-1950, specificity 0,1,1)
// outranks the bare `.ov-fmt-sel` class (2232-2238, 0,1,0) on every native
// select in the legacy DOM, so the resolved font-size is 13px, and
// custom-select.js:53/64 stamps that resolved 13px onto the `.cs2` wrapper.
// The on-screen legacy box is therefore a 13px cs2 button, not the 10px the
// stylesheet alone would suggest. This component targets that 13px box.
//
// Built directly on reka-ui's Select primitives (NOT the generated
// app/components/ui/select/* wrapper) because that wrapper hardcodes
// Tailwind utility classes and a lucide chevron icon — the contract calls
// for the verbatim legacy chevron data-URI and the exact cs2 box metrics,
// so this component supplies its own classes (`.ov-fmt-sel.fmt-select-*`,
// declared in shell.css) instead of shadcn's default skin.
//
// SelectContent teleports to <body> (reka Portal) — its styling lives in a
// GLOBAL class (`.fmt-select-content` / `.fmt-select-item`), never
// `:deep()`/`scoped`, because scoped styles do not reach teleported
// content. z-index comes from the `--z-popover` token (shell.css §1).

import {
  SelectContent, SelectItem, SelectItemText,
  SelectPortal, SelectRoot, SelectTrigger, SelectValue, SelectViewport,
} from 'reka-ui'

export interface FormatSelectOption {
  value: string
  label: string
}

const props = withDefaults(defineProps<{
  modelValue: string
  options: FormatSelectOption[]
  /** Trigger width in px. Default 48 (`.ov-label-group`); AngleField passes
   *  60 for kind="latlon" (`.ov-label-group-wide`). */
  width?: number
  title?: string
  disabled?: boolean
}>(), {
  width: 48,
  title: undefined,
  disabled: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

function onUpdate(v: unknown): void {
  if (typeof v === 'string') emit('update:modelValue', v)
}
</script>

<template>
  <SelectRoot
    :model-value="props.modelValue"
    :disabled="props.disabled"
    @update:model-value="onUpdate"
  >
    <SelectTrigger
      class="ov-fmt-sel fmt-select-trigger"
      :style="{ width: `${props.width}px` }"
      :title="props.title"
    >
      <SelectValue />
    </SelectTrigger>
    <SelectPortal>
      <SelectContent class="fmt-select-content" :style="{ zIndex: 'var(--z-popover)' }" position="popper">
        <SelectViewport>
          <SelectItem
            v-for="opt in props.options"
            :key="opt.value"
            class="fmt-select-item"
            :value="opt.value"
          >
            <SelectItemText>{{ opt.label }}</SelectItemText>
          </SelectItem>
        </SelectViewport>
      </SelectContent>
    </SelectPortal>
  </SelectRoot>
</template>
