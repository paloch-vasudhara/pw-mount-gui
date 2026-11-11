<script setup lang="ts">
// Visibility filter tree (A2a). State + persistence live in stores/filters.ts, semantics in
// utils/filters.ts: a parent off hides its children but keeps their own checks (rendered greyed).
// One tree per group (concept Main Dashboard 1, user 11-11): Satellites and Celestial Objects with their children;
// Aircrafts and Labels are leaves. Each tree's line starts under its parent's checkbox and ends at its last child.
import { computed } from 'vue'
import { useAircraftStore } from '~/stores/aircraft'
import { useFiltersStore } from '~/stores/filters'
import { aircraftFilterState } from '~/utils/aircraft'

const f = useFiltersStore().filters
// Aircrafts: usable only on a green ADS-B connection; otherwise disabled with the reason as its tooltip.
const air = computed(() => aircraftFilterState(useAircraftStore().adsb))
</script>

<template>
  <fieldset class="vis app-scroll" data-testid="visibility-filters">
    <legend>Visibility</legend>

    <div class="group" data-testid="flt-group-satellites">
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
    </div>

    <div class="group" data-testid="flt-group-aircrafts">
      <label class="row top" :class="{ disabled: !air.enabled }" :title="air.reason ?? undefined" data-testid="flt-aircrafts-row">
        <input v-model="f.aircrafts" type="checkbox" :disabled="!air.enabled" data-testid="flt-aircrafts">
        <span>Aircrafts</span>
      </label>
    </div>

    <div class="group" data-testid="flt-group-celestial">
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
    </div>

    <div class="group" data-testid="flt-group-labels">
      <label class="row top">
        <input v-model="f.labels" type="checkbox" data-testid="flt-labels">
        <span>Labels</span>
      </label>
    </div>
  </fieldset>
</template>

<style scoped>
/* Every size follows the FILTERS panel height: --u (set by the panel body in pages/index.vue) is the
   panel height / the content height at today's sizes, so each clamp(min, u*N, N) hits N (today's
   value, the max) at 1920x1080 and shrinks to its floor (text 11 px) on short panels. Without --u
   (any other host) everything stays at the max. If even the floors do not fit, the tree scrolls. */
.vis {
  --cb: clamp(12px, calc(var(--u, 1px) * 16), 16px);
  --gap: clamp(2px, calc(var(--u, 1px) * 6), 6px); /* between rows inside a tree */
  --rowh: clamp(14px, calc(var(--u, 1px) * 21), 21px);
  /* the tree line sits under the parent's checkbox centre; the child checkbox stays 25 px in */
  --ind-ml: calc(var(--cb) / 2 - 0.5px);
  --ind-pad: calc(clamp(18px, calc(var(--u, 1px) * 25), 25px) - var(--ind-ml));
  /* between trees: the concept's 48 px vs 36 px row pitch, scaled to our 27 px pitch */
  display: flex; flex-direction: column; gap: clamp(4px, calc(var(--u, 1px) * 15), 15px); border: 1px solid var(--panel-border);
  padding: clamp(4px, calc(var(--u, 1px) * 10), 10px) clamp(9px, calc(var(--u, 1px) * 14), 14px); margin: 0; min-width: 0;
  min-height: 0; flex: 0 1 auto;
}
.vis legend { padding: 0 6px; font-size: clamp(11px, calc(var(--u, 1px) * 13), 13px); line-height: clamp(13px, calc(var(--u, 1px) * 19.5), 19.5px); font-weight: 600; color: var(--label-slate); }
.row {
  display: flex; align-items: center; gap: clamp(6px, calc(var(--u, 1px) * 10), 10px); flex: none;
  height: var(--rowh);
  font-size: clamp(11px, calc(var(--u, 1px) * 14), 14px); color: var(--foreground); cursor: pointer; user-select: none;
}
.group { display: flex; flex-direction: column; gap: var(--gap); flex: none; }
.row.top { font-weight: 600; }
.row span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.row.disabled { color: var(--disabled); cursor: not-allowed; }
.kids {
  display: flex; flex-direction: column; gap: var(--gap); flex: none;
  margin-left: var(--ind-ml); padding-left: var(--ind-pad);
}
/* each child draws its own piece of the tree line: the elbow (::before) and the vertical run up to the row
   above (::after); the first reaches the parent's checkbox, the last stops at its elbow */
.kid { position: relative; font-weight: 400; }
.kid::before { content: ''; position: absolute; left: calc(-1 * var(--ind-pad)); top: 50%; width: calc(var(--ind-pad) - 3px); border-top: 1px solid var(--control-border); }
.kid::after {
  --up: var(--gap);
  content: ''; position: absolute; left: calc(-1 * var(--ind-pad)); top: calc(-1 * var(--up)); height: calc(100% + var(--up));
  border-left: 1px solid var(--control-border);
}
.kid:first-child::after { --up: calc(var(--gap) + (var(--rowh) - var(--cb)) / 2); }
.kid:last-child::after { height: calc(50% + var(--up)); }
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
