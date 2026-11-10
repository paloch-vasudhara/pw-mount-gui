<script setup lang="ts">
// TRACK TARGET modal, AIRCRAFT tab (T5.5, Q42/Q43): the aircraft currently in view within a range preset
// (20 / 50 / 100 / 200 / 350 km, default 100), search by callsign / ICAO (exact match first, like the
// satellite tab), sortable columns (default: range, nearest first). The list follows the 1 Hz data.
// Clicking a row selects the aircraft in the Sky Viewer (shared selection). TRACK SELECTED TARGET is
// present but disabled with the same reason as the card until T5.6b (utils/aircraft.ts verdict).
import { computed, ref } from 'vue'
import { SelectContent, SelectItem, SelectItemText, SelectPortal, SelectRoot, SelectTrigger, SelectViewport } from 'reka-ui'
import { useAircraftStore } from '~/stores/aircraft'
import { useSelectionStore } from '~/stores/selection'
import { aircraftTrackVerdict, formatAltFt, formatRangeKm } from '~/utils/aircraft'
import {
  aircraftTabRows, DEFAULT_AIRCRAFT_SORT, nextAircraftSort, TARGET_DEFAULT_RANGE_KM, TARGET_RANGE_PRESETS_KM,
  type AircraftSortKey, type AircraftSortState,
} from '~/utils/aircraft-target'
import { formatEl } from '~/utils/track-format'
import { SELECT_TARGET } from '~/utils/track-target'

const air = useAircraftStore()
const sel = useSelectionStore()
const base = useRuntimeConfig().app.baseURL || '/'

const q = ref('')
const rangeKm = ref(String(TARGET_DEFAULT_RANGE_KM))
const sort = ref<AircraftSortState>({ ...DEFAULT_AIRCRAFT_SORT })

const rows = computed(() => aircraftTabRows(air.rows, q.value, Number(rangeKm.value), sort.value))
const selectedRow = computed(() => (sel.selectedIcao === null ? null : air.get(sel.selectedIcao)?.row ?? null))
const verdict = computed(() => aircraftTrackVerdict(selectedRow.value, air.adsb))
const statusText = computed(() => (sel.selectedIcao === null ? SELECT_TARGET : (verdict.value.reason ?? 'Ready')))

const COLS: Array<{ key: AircraftSortKey, label: string }> = [
  { key: 'callsign', label: 'CALLSIGN' },
  { key: 'icao', label: 'ICAO' },
  { key: 'alt', label: 'ALTITUDE' },
  { key: 'range', label: 'RANGE' },
  { key: 'el', label: 'ELEVATION' },
]
const arrow = (k: AircraftSortKey): string => (sort.value.key === k ? (sort.value.dir === 'desc' ? ' ▼' : ' ▲') : '')
const ariaSort = (k: AircraftSortKey): 'ascending' | 'descending' | 'none' =>
  sort.value.key === k ? (sort.value.dir === 'desc' ? 'descending' : 'ascending') : 'none'

function onSort(k: AircraftSortKey): void {
  sort.value = nextAircraftSort(sort.value, k)
}

function onClear(): void {
  q.value = ''
  rangeKm.value = String(TARGET_DEFAULT_RANGE_KM)
  sort.value = { ...DEFAULT_AIRCRAFT_SORT }
}
</script>

<template>
  <div class="tt-filters">
    <input
      v-model="q" class="ov-dms-box tt-search" type="text" placeholder="Search callsign or ICAO" data-testid="tt-air-search"
      autocomplete="off" spellcheck="false"
    >
    <span class="tt-lbl">Range</span>
    <SelectRoot v-model="rangeKm">
      <SelectTrigger class="ov-fmt-sel fmt-select-trigger tt-sel tt-sel-range" data-testid="tt-air-range" title="Range">
        <span class="fmt-select-text">{{ rangeKm }} km</span>
      </SelectTrigger>
      <SelectPortal>
        <SelectContent class="fmt-select-content" :style="{ zIndex: 'var(--z-popover)' }" position="popper">
          <SelectViewport>
            <SelectItem
              v-for="r in TARGET_RANGE_PRESETS_KM" :key="r" class="fmt-select-item" :value="String(r)"
              :data-testid="`tt-air-range-${r}`"
            >
              <SelectItemText>{{ r }} km</SelectItemText>
            </SelectItem>
          </SelectViewport>
        </SelectContent>
      </SelectPortal>
    </SelectRoot>
    <button type="button" class="tt-btn" data-testid="tt-air-clear" @click="onClear">
      CLEAR
    </button>
  </div>

  <div class="tt-table" data-testid="tt-air-table">
    <table>
      <thead>
        <tr>
          <th class="tt-ic" />
          <th v-for="c in COLS" :key="c.key" :class="`tt-c-air-${c.key}`" :aria-sort="ariaSort(c.key)">
            <button type="button" class="tt-th" :data-testid="`tt-air-sort-${c.key}`" @click="onSort(c.key)">
              {{ c.label }}{{ arrow(c.key) }}
            </button>
          </th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="r in rows" :key="r.icao" class="tt-row" :class="{ 'tt-sel-row': r.icao === sel.selectedIcao }"
          data-testid="tt-air-row" :data-icao="r.icao" :data-selected="r.icao === sel.selectedIcao" @click="sel.selectAircraft(r.icao)"
        >
          <td class="tt-ic">
            <img :src="`${base}icons/aircraft.svg`" alt="" aria-hidden="true">
          </td>
          <td class="tt-c-air-callsign" :title="r.callsign ?? undefined">
            {{ r.callsign?.trim() || '—' }}
          </td>
          <td class="tt-c-air-icao">
            {{ r.icao }}
          </td>
          <td class="tt-c-air-alt">
            {{ formatAltFt(r.alt_ft) }}
          </td>
          <td class="tt-c-air-range">
            {{ formatRangeKm(r.range_m) }}
          </td>
          <td class="tt-c-air-el">
            {{ formatEl(r.el) }}
          </td>
        </tr>
      </tbody>
    </table>
    <p v-if="!rows.length" class="tt-note" data-testid="tt-air-empty">
      {{ air.rows.length ? 'No aircraft match' : 'No aircraft in view' }}
    </p>
  </div>

  <span class="tt-go-wrap" :title="verdict.reason ?? undefined">
    <button type="button" class="tt-go" data-testid="tt-air-track" :disabled="!verdict.enabled || sel.selectedIcao === null">
      TRACK SELECTED TARGET
    </button>
  </span>
  <p class="tt-status" data-testid="tt-air-status" role="status">
    {{ statusText }}
  </p>
</template>

<style>
.tt-dialog .tt-sel-range { width: 100px; }
.tt-dialog .tt-go-wrap { display: flex; flex-direction: column; flex: none; }
.tt-dialog .tt-c-air-icao { width: 100px; }
.tt-dialog .tt-c-air-alt { width: 140px; }
.tt-dialog .tt-c-air-range { width: 140px; }
.tt-dialog .tt-c-air-el { width: 120px; }
.tt-dialog .tt-c-air-callsign { width: auto; }
</style>
