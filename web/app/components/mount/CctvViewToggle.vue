<script setup lang="ts">
// The 3D | CCTV toggle of the mount view (T5.7); the expanded dialog adds "Side by side".
// Rendered by the caller only when a camera is configured.
import SegToggle from '@/components/widgets/SegToggle.vue'
import { viewOptions, type CctvView } from '@/utils/cctv'

const props = defineProps<{ modelValue: CctvView, expanded?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: CctvView] }>()
</script>

<template>
  <SegToggle
    :model-value="props.modelValue"
    :options="viewOptions(!!props.expanded)"
    :seg-class="['cctv-seg', props.expanded ? 'cctv-seg-lg' : 'cctv-seg-sm']"
    data-testid="cctv-toggle"
    @update:model-value="v => emit('update:modelValue', v as CctvView)"
  />
</template>

<style>
/* Provisional sizes (T5.7): the compact panel toggle follows the panel width (container query of
   .msp-root), the expanded one is the standard 26 px segmented toggle. */
.cctv-seg { background: rgba(8, 9, 11, 0.72); }
.cctv-seg-sm button {
  height: clamp(17px, 5.2cqw, 21px); padding: 0 clamp(6px, 2cqw, 8px);
  font-size: clamp(10px, 3cqw, 12px); letter-spacing: 0.2px;
}
.cctv-seg-lg button { padding: 0 12px; }
</style>
