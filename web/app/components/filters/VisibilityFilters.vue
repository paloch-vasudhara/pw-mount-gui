<script setup lang="ts">
// Visibility filter tree (A2a). State + persistence live in stores/filters.ts, semantics in
// utils/filters.ts: a parent off hides its children but keeps their own checks (rendered greyed).
import { useFiltersStore } from '~/stores/filters'

const f = useFiltersStore().filters
</script>

<template>
  <fieldset class="vis" data-testid="visibility-filters">
    <legend>Visibility</legend>

    <label class="row top">
      <input v-model="f.satellites" type="checkbox" data-testid="flt-satellites">
      <span>Satellites</span>
    </label>
    <div class="kids" :class="{ off: !f.satellites }">
      <label class="row kid">
        <input v-model="f.leo" type="checkbox" data-testid="flt-leo">
        <span>Low Earth Satellites</span>
      </label>
      <label class="row kid">
        <input v-model="f.meo" type="checkbox" data-testid="flt-meo">
        <span>Medium Earth Satellites</span>
      </label>
      <label class="row kid">
        <input v-model="f.geo" type="checkbox" data-testid="flt-geo">
        <span>Geosynchronous Satellites</span>
      </label>
    </div>

    <label class="row top disabled" title="No ADS-B source configured">
      <input v-model="f.aircrafts" type="checkbox" disabled data-testid="flt-aircrafts">
      <span>Aircrafts</span>
    </label>

    <label class="row top">
      <input v-model="f.celestial" type="checkbox" data-testid="flt-celestial">
      <span>Celestial Objects</span>
    </label>
    <div class="kids" :class="{ off: !f.celestial }">
      <label class="row kid">
        <input v-model="f.planets" type="checkbox" data-testid="flt-planets">
        <span>Planetary Bodies</span>
      </label>
      <label class="row kid">
        <input v-model="f.stars" type="checkbox" data-testid="flt-stars">
        <span>Stars</span>
      </label>
    </div>

    <label class="row top">
      <input v-model="f.labels" type="checkbox" data-testid="flt-labels">
      <span>Labels</span>
    </label>
  </fieldset>
</template>

<style scoped>
/* Every size follows the FILTERS panel height: --u (set by the panel body in pages/index.vue) is the
   panel height / the content height at today's sizes, so each clamp(min, u*N, N) hits N (today's
   value, the max) at 1920x1080 and shrinks to its floor (text 11 px) on short panels. Without --u
   (any other host) everything stays at the max. If even the floors do not fit, the tree scrolls. */
.vis {
  --cb: clamp(12px, calc(var(--u, 1px) * 16), 16px);
  --ind-ml: clamp(8px, calc(var(--u, 1px) * 11), 11px);
  --ind-pad: clamp(10px, calc(var(--u, 1px) * 14), 14px);
  display: flex; flex-direction: column; gap: clamp(2px, calc(var(--u, 1px) * 6), 6px); border: 1px solid var(--panel-border);
  padding: clamp(4px, calc(var(--u, 1px) * 10), 10px) clamp(9px, calc(var(--u, 1px) * 14), 14px); margin: 0; min-width: 0;
  min-height: 0; flex: 0 1 auto; overflow-y: auto; scrollbar-width: thin; scrollbar-color: var(--control-border) transparent;
}
.vis legend { padding: 0 6px; font-size: clamp(11px, calc(var(--u, 1px) * 13), 13px); line-height: clamp(13px, calc(var(--u, 1px) * 19.5), 19.5px); font-weight: 600; color: var(--label-slate); }
.row {
  display: flex; align-items: center; gap: clamp(6px, calc(var(--u, 1px) * 10), 10px); flex: none;
  height: clamp(14px, calc(var(--u, 1px) * 21), 21px);
  font-size: clamp(11px, calc(var(--u, 1px) * 14), 14px); color: var(--foreground); cursor: pointer; user-select: none;
}
.row.top { font-weight: 600; margin-top: clamp(1px, calc(var(--u, 1px) * 4), 4px); }
.row span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.row.disabled { color: var(--disabled); cursor: not-allowed; }
.kids {
  display: flex; flex-direction: column; gap: clamp(2px, calc(var(--u, 1px) * 6), 6px); flex: none;
  margin-left: var(--ind-ml); padding-left: var(--ind-pad); border-left: 1px solid var(--control-border);
}
.kid { position: relative; font-weight: 400; }
.kid::before { content: ''; position: absolute; left: calc(-1 * var(--ind-pad)); top: 50%; width: var(--ind-ml); border-top: 1px solid var(--control-border); }
.kids.off .kid { color: var(--disabled); }

input[type='checkbox'] {
  appearance: none; flex: none; width: var(--cb); height: var(--cb); margin: 0;
  border: 1px solid var(--control-border); background: transparent; display: grid; place-content: center; cursor: inherit;
}
input[type='checkbox']::after {
  content: ''; width: calc(var(--cb) * 0.5625); height: calc(var(--cb) * 0.3125);
  border-left: 2px solid var(--accent-green-text); border-bottom: 2px solid var(--accent-green-text);
  transform: translateY(-1px) rotate(-45deg) scale(0);
}
input[type='checkbox']:checked::after { transform: translateY(-1px) rotate(-45deg) scale(1); }
.kids.off input[type='checkbox']::after { border-color: var(--disabled); }
input[type='checkbox']:disabled { border-color: var(--panel-border); }
</style>
