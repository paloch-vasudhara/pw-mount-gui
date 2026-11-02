<script setup lang="ts">
// Dev-only harness (unlinked, T5.2c): opens the Astrometry modal for the supervisor's browser check.
// RUN only shows the emitted payload here; the real relay + job pill are T5.2d.
import AstrometryDialog from '~/components/analysis/AstrometryDialog.vue'
import type { AstrometryRunPayload } from '~/utils/astrometry'

const open = ref(false)
const last = ref<AstrometryRunPayload | null>(null)
</script>

<template>
  <main class="min-h-screen bg-page p-8 text-sm">
    <h1 class="mb-4 text-xl font-semibold">Astrometry from image (dev)</h1>
    <button type="button" data-testid="dev-as-open" class="border px-3 py-1" @click="open = true">
      Open ASTROMETRY FROM IMAGE
    </button>
    <pre v-if="last" data-testid="dev-as-run" class="mt-4 max-h-64 overflow-auto text-xs">{{ JSON.stringify({ ...last, detections: `${last.detections.length} detections` }, null, 2) }}</pre>
    <AstrometryDialog v-model:open="open" @run="last = $event" />
  </main>
</template>
