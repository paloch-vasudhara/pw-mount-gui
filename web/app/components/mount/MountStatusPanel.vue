<script setup lang="ts">
// MOUNT STATUS panel (A4-i): 3D slot, Azimuth/Elevation boxes, Status + Sun Distance rows.
import { computed } from 'vue'
import MountHemisphere3D from '~/components/mount/MountHemisphere3D.vue'
import { useMountStore } from '~/stores/mount'
import type { ValueTone } from '~/utils/mount-format'

const emit = defineEmits<{ 'open-details': [] }>()

const store = useMountStore()
const d = computed(() => store.display)

const TONE: Record<ValueTone, string> = {
  normal: 'text-foreground',
  positive: 'text-accent-green-border',
  danger: 'text-danger',
  dim: 'text-foreground opacity-40',
}
</script>

<template>
  <div class="msp-root flex h-full min-h-0 flex-col" data-testid="mount-status-panel">
    <div class="min-h-0 flex-[6]" data-testid="mount-3d-slot">
      <MountHemisphere3D mode="compact" />
    </div>

    <div class="msp-cols flex shrink-0 justify-center pt-2">
      <div class="flex flex-col items-center gap-1.5">
        <div
          class="msp-val flex items-center justify-center border border-control-border px-2 leading-none font-bold tabular-nums"
          :class="TONE[d.azimuth.tone]"
          data-testid="mount-az"
        >
          {{ d.azimuth.text }}
        </div>
        <span class="msp-cap leading-none font-medium text-foreground">AZIMUTH (°)</span>
      </div>
      <div class="flex flex-col items-center gap-1.5">
        <div
          class="msp-val flex items-center justify-center border border-control-border px-2 leading-none font-bold tabular-nums"
          :class="TONE[d.elevation.tone]"
          data-testid="mount-el"
        >
          {{ d.elevation.text }}
        </div>
        <span class="msp-cap leading-none font-medium text-foreground">ELEVATION (°)</span>
      </div>
    </div>

    <div class="msp-rows mt-3 flex shrink-0 flex-col gap-2 px-4 leading-none">
      <div class="flex items-baseline justify-between">
        <span class="text-foreground">Status</span>
        <span
          class="font-semibold"
          :class="TONE[d.status.tone]"
          :title="d.statusReason ?? undefined"
          data-testid="mount-status"
        >{{ d.status.text }}</span>
      </div>
      <div class="flex items-baseline justify-between">
        <span class="text-foreground">Sun Distance</span>
        <span
          class="font-semibold tabular-nums"
          :class="TONE[d.sunDistance.tone]"
          data-testid="mount-sun"
        >{{ d.sunDistance.text }}</span>
      </div>
    </div>

    <div class="flex shrink-0 justify-center pt-2">
      <button
        type="button"
        class="h-[14px] w-[46px] rounded-full border border-control-border text-[11px] leading-[6px] tracking-widest text-foreground hover:border-foreground"
        title="Detailed mount status"
        data-testid="mount-details"
        @click="emit('open-details')"
      >
        …
      </button>
    </div>
  </div>
</template>

<style scoped>
/* Sizes follow the panel's own width. Factors are anchored on the 1920x1080 panel content width
   (404.5px) where every clamp hits today's value (its max): 20/404.5 = 4.95cqw etc. */
.msp-root { container-type: inline-size; }
.msp-cols { gap: clamp(45px, 16.08cqw, 65px); }
.msp-val {
  font-size: clamp(14px, 4.95cqw, 20px);
  min-width: clamp(80px, 24.75cqw, 100px);
  height: clamp(26px, 7.92cqw, 32px);
}
.msp-cap { font-size: clamp(11px, 3.47cqw, 14px); }
.msp-rows { font-size: clamp(12px, 3.96cqw, 16px); }
</style>
