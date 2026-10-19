<script setup lang="ts">
// Ported from ssa-toolkit web/app/components/widgets/SpinnerColumn.vue @84f2ded
// SpinnerColumn.vue — the shared ▴/▾ spinner column injected by
// dms-input.js's `_wrapInput` and number-spinner.js's `enhance` (T10,
// §4b/T10). Markup parity is non-negotiable: `tabindex="-1"` on both
// buttons (never in the tab order — the owning input is), glyphs are
// U+25B4 / U+25BE as literal text (not an icon component, the CSS sizes
// them as text).

defineProps<{
  upDisabled?: boolean
  dnDisabled?: boolean
}>()

const emit = defineEmits<{
  up: []
  dn: []
}>()
</script>

<template>
  <span class="dms-spinner">
    <button
      type="button"
      class="dms-spin-up"
      tabindex="-1"
      :disabled="upDisabled"
      @click="emit('up')"
    >▴</button>
    <button
      type="button"
      class="dms-spin-dn"
      tabindex="-1"
      :disabled="dnDisabled"
      @click="emit('dn')"
    >▾</button>
  </span>
</template>
