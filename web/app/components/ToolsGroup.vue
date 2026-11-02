<script setup lang="ts">
// Tools group (A2b). Satellite Path Analysis (T5.1c) is live; Initial Orbit Determination (T5.3b) opens the IOD modal.
import { ref, watch } from 'vue'
import IodDialog from '@/components/analysis/IodDialog.vue'
import PassAnalysisDialog from '@/components/analysis/PassAnalysisDialog.vue'
import StationSettingsDialog from '@/components/settings/StationSettingsDialog.vue'
import { useAstrometryJobStore } from '~/stores/astrometry-job'

const settingsOpen = ref(false)
const passesOpen = ref(false)
const iodOpen = ref(false)

// the astrometry job pill asks for IOD (T5.2d)
const astro = useAstrometryJobStore()
watch(() => astro.openIodTick, () => { iodOpen.value = true })
</script>

<template>
  <fieldset class="tools-group" data-testid="tools-group">
    <legend>Tools</legend>
    <button type="button" class="tool-btn" data-testid="open-pass-analysis" @click="passesOpen = true">
      SATELLITE PATH ANALYSIS
    </button>
    <button type="button" class="tool-btn" data-testid="open-iod" @click="iodOpen = true">
      INITIAL ORBIT DETERMINATION
    </button>
    <button type="button" class="tool-btn" data-testid="open-station-settings" @click="settingsOpen = true">
      STATION SETTINGS
    </button>
    <StationSettingsDialog v-model:open="settingsOpen" />
    <PassAnalysisDialog v-model:open="passesOpen" />
    <IodDialog v-model:open="iodOpen" />
  </fieldset>
</template>

<style scoped>
/* Sizes follow the FILTERS panel height through --u (see VisibilityFilters.vue); max = today's values. */
.tools-group {
  display: flex; flex-direction: column; gap: clamp(2px, calc(var(--u, 1px) * 6), 6px); border: 1px solid var(--panel-border);
  padding: clamp(4px, calc(var(--u, 1px) * 10), 10px); margin: 0; flex: none; min-width: 0;
}
.tools-group legend { padding: 0 6px; font-size: clamp(11px, calc(var(--u, 1px) * 13), 13px); line-height: clamp(13px, calc(var(--u, 1px) * 19.5), 19.5px); font-weight: 600; color: var(--label-slate); }
.tool-btn {
  height: clamp(20px, calc(var(--u, 1px) * 28), 28px); width: 100%; border: 1px solid var(--control-border); background: transparent;
  color: var(--foreground); font-size: clamp(11px, calc(var(--u, 1px) * 13), 13px); font-weight: 600; letter-spacing: 0.02em; cursor: pointer;
}
.tool-btn:hover:not(:disabled) { background: var(--secondary); border-color: var(--accent-green-border); }
.tool-btn:disabled { color: var(--disabled); border-color: var(--panel-border); cursor: not-allowed; }
</style>
