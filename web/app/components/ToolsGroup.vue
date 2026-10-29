<script setup lang="ts">
// Tools group (A2b). Analysis tools are present but disabled until Phase 5.
import { ref } from 'vue'
import StationSettingsDialog from '@/components/settings/StationSettingsDialog.vue'

const settingsOpen = ref(false)
</script>

<template>
  <fieldset class="tools-group" data-testid="tools-group">
    <legend>Tools</legend>
    <button type="button" class="tool-btn" disabled>
      SATELLITE PATH ANALYSIS
    </button>
    <button type="button" class="tool-btn" disabled>
      INITIAL ORBIT DETERMINATION
    </button>
    <button type="button" class="tool-btn" data-testid="open-station-settings" @click="settingsOpen = true">
      STATION SETTINGS
    </button>
    <StationSettingsDialog v-model:open="settingsOpen" />
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
