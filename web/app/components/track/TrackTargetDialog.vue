<script setup lang="ts">
// TRACK TARGET modal (T4.3c, A5c-ii, concept/Track target Modal.png, satellite tab). Search / filter
// the live catalogue (analysis /api/sats/search, 500 cap), pick a row, TRACK SELECTED TARGET.
// The row selection lives here only (Q23); the dashboard selection changes when tracking starts.
// The gateway check (POST /api/mount/check track_object) of the selected row runs at 1 Hz and
// decides the button + status line; the answer only counts while it matches the selected row.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { SelectContent, SelectItem, SelectItemText, SelectPortal, SelectRoot, SelectTrigger, SelectViewport } from 'reka-ui'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import FlagBadge from '~/components/track/FlagBadge.vue'
import StaleBadge from '~/components/track/StaleBadge.vue'
import { useGatewayApi } from '~/composables/useGatewayApi'
import { useMountStore } from '~/stores/mount'
import { useSelectionStore } from '~/stores/selection'
import type { ClockMode } from '~/utils/sky-clock'
import type { SkySnapshotLike } from '~/utils/sky-chart'
import {
  ANY, buildSearchQuery, DATA_UNAVAILABLE, DEFAULT_SORT, EMPTY_FILTERS, filterOwners, FOOTER_CAPPED, formatEpoch, isCapped,
  moveSelection, nextSort, sortRows, targetStatus, type SatRow, type SortKey, type SortState,
} from '~/utils/track-target'
import { ownerName } from '~/utils/owner-names'
import { parseTrackVerdict, UNREACHABLE_VERDICT, type TrackVerdict } from '~/utils/track-verdict'

const open = defineModel<boolean>('open', { default: false })

const mount = useMountStore()
const sel = useSelectionStore()
const api = useGatewayApi()
const base = useRuntimeConfig().app.baseURL || '/'

const FETCH_TIMEOUT_MS = 8_000
const CLOCK_KEY = 'pw-mount.sky.clockMode' // same key as SkyClock.vue

const q = ref('')
const orbit = ref(ANY)
const country = ref(ANY)
const countries = ref<Array<{ code: string, count: number }>>([])
const rows = ref<SatRow[]>([])
const loading = ref(false)
const error = ref(false)
const sort = ref<SortState>({ ...DEFAULT_SORT })
const selected = ref<number | null>(null)
const clockMode = ref<ClockMode>('utc')
const sending = ref(false)
const ackRefusal = ref<string | null>(null)
const verdict = ref<TrackVerdict | null>(null)
const tableEl = ref<HTMLElement | null>(null)

const tz = computed<string | null>(() => (mount.snapshot as SkySnapshotLike | null)?.site?.timezone ?? null)
const sorted = computed(() => sortRows(rows.value, sort.value))
const capped = computed(() => isCapped(rows.value.length))
const status = computed(() => targetStatus({
  selected: selected.value, verdict: verdict.value, sending: sending.value, ackRefusal: ackRefusal.value,
}))

// ----- search. A newer request supersedes (older answers dropped).
let searchSeq = 0
async function runSearch(): Promise<void> {
  const my = ++searchSeq
  loading.value = true
  error.value = false
  try {
    const res = await fetch(`/api/sats/search?${buildSearchQuery({ q: q.value, orbit: orbit.value, country: country.value })}`, {
      cache: 'no-store',
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    })
    if (!res.ok) throw new Error(`search HTTP ${res.status}`)
    const body = await res.json() as SatRow[]
    if (my !== searchSeq) return
    rows.value = Array.isArray(body) ? body : []
    if (selected.value !== null && !rows.value.some(r => r.norad === selected.value)) selectRow(null)
  }
  catch {
    if (my !== searchSeq) return
    rows.value = []
    error.value = true
    selectRow(null)
  }
  if (my === searchSeq) loading.value = false
}

let countriesBusy = false
async function loadCountries(): Promise<void> {
  if (countriesBusy || countries.value.length) return
  countriesBusy = true
  try {
    const res = await fetch('/api/sats/countries', { cache: 'no-store', signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) })
    if (res.ok) {
      const body = await res.json() as Array<{ code: string, count: number }>
      if (Array.isArray(body)) countries.value = body
    }
  }
  catch { /* the Origin select just stays at "any"; retried on the next open */ }
  finally {
    countriesBusy = false
  }
}

function onClear(): void {
  q.value = ''
  orbit.value = EMPTY_FILTERS.orbit
  country.value = EMPTY_FILTERS.country
  sort.value = { ...DEFAULT_SORT }
  void runSearch()
}

function onSort(key: SortKey): void {
  sort.value = nextSort(sort.value, key)
}

// ----- the 1 Hz check of the selected row (generation + in-flight handling as in TrackCard).
let checkGen = 0
let checkTimer: ReturnType<typeof setInterval> | null = null
let inFlight = false

async function runVerdict(n: number, g: number): Promise<void> {
  if (inFlight) return
  inFlight = true
  let v: TrackVerdict
  try {
    v = parseTrackVerdict(await api.post<unknown>('/api/mount/check', { cmd: 'track_object', args: { norad: n } }))
  }
  catch {
    v = UNREACHABLE_VERDICT
  }
  if (g === checkGen) {
    inFlight = false
    verdict.value = v
  }
}

function stopVerdict(): void {
  checkGen += 1
  inFlight = false
  if (checkTimer) clearInterval(checkTimer)
  checkTimer = null
  verdict.value = null
}

function selectRow(n: number | null): void {
  if (n === selected.value && (n === null || checkTimer)) return
  stopVerdict()
  selected.value = n
  ackRefusal.value = null
  if (n === null) return
  const g = checkGen
  void runVerdict(n, g)
  checkTimer = setInterval(() => void runVerdict(n, g), 1000)
}

// ----- tracking
async function onTrack(): Promise<void> {
  const n = selected.value
  if (n === null || !status.value.canTrack || sending.value) return
  sending.value = true
  ackRefusal.value = null
  try {
    const ack = await mount.sendCommand('track_object', { norad: n })
    if (ack.ok) {
      sel.afterTrackStarted(n)
      open.value = false
    }
    else {
      ackRefusal.value = ack.reason ?? 'Command refused'
    }
  }
  finally {
    sending.value = false
  }
}

// ----- keyboard (Esc is the dialog's own)
function scrollToSelected(): void {
  void nextTick(() => {
    tableEl.value?.querySelector<HTMLElement>('[data-selected="true"]')?.scrollIntoView({ block: 'nearest' })
  })
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault()
    const n = moveSelection(sorted.value.map(r => r.norad), selected.value, e.key === 'ArrowDown' ? 1 : -1)
    if (n !== null) { selectRow(n); scrollToSelected() }
    return
  }
  if (e.key !== 'Enter') return
  const t = e.target as HTMLElement | null
  if (t?.closest('[data-testid="tt-search"]')) { e.preventDefault(); void runSearch(); return }
  if (t?.closest('button, [role="combobox"]')) return // Enter on a focused button / select keeps its own meaning
  e.preventDefault()
  void onTrack()
}

function onDoubleClick(n: number): void {
  selectRow(n)
  void nextTick(() => void onTrack())
}

watch(open, (o) => {
  stopVerdict()
  searchSeq++
  if (!o) return
  try {
    const v = localStorage.getItem(CLOCK_KEY)
    clockMode.value = v === 'local' ? 'local' : 'utc'
  }
  catch { clockMode.value = 'utc' }
  q.value = ''
  orbit.value = ANY
  country.value = ANY
  sort.value = { ...DEFAULT_SORT }
  rows.value = []
  selected.value = null
  ackRefusal.value = null
  sending.value = false
  void loadCountries()
  void runSearch()
}, { immediate: true })
onBeforeUnmount(() => { stopVerdict(); searchSeq++ })

const COLS: Array<{ key: SortKey, label: string }> = [
  { key: 'norad', label: 'NORAD ID' },
  { key: 'name', label: 'NAME' },
  { key: 'orbit', label: 'ORBIT' },
  { key: 'origin', label: 'ORIGIN' },
  { key: 'epoch', label: 'EPOCH' },
]
const arrow = (k: SortKey): string => (sort.value.key === k ? (sort.value.dir === 'desc' ? ' ▼' : ' ▲') : '')
const ariaSort = (k: SortKey): 'ascending' | 'descending' | 'none' =>
  sort.value.key === k ? (sort.value.dir === 'desc' ? 'descending' : 'ascending') : 'none'
const ORBITS = [{ value: ANY, label: 'any' }, { value: 'leo', label: 'LEO' }, { value: 'meo', label: 'MEO' }, { value: 'geo', label: 'GEO' }]
// ----- Origin combobox (type-to-filter; the list shows full names, the box and the table keep the code)
const originOpen = ref(false)
const originQuery = ref('')
const originActive = ref(0)
const originWrap = ref<HTMLElement | null>(null)
const originInput = ref<HTMLInputElement | null>(null)
const originListEl = ref<HTMLElement | null>(null)
const originOptions = computed<string[]>(() => [ANY, ...filterOwners(countries.value.map(c => c.code), originQuery.value)])
watch(originQuery, () => { originActive.value = originOptions.value.length > 1 ? 1 : 0 })
function scrollOriginActive(): void {
  void nextTick(() => originListEl.value?.querySelector<HTMLElement>('[data-active="true"]')?.scrollIntoView({ block: 'nearest' }))
}
function openOrigin(): void {
  originQuery.value = ''
  originActive.value = Math.max(0, originOptions.value.indexOf(country.value))
  originOpen.value = true
  void nextTick(() => { originInput.value?.focus(); scrollOriginActive() })
}
function closeOrigin(refocus = true): void {
  if (!originOpen.value) return
  originOpen.value = false
  if (refocus) void nextTick(() => originWrap.value?.querySelector<HTMLElement>('[data-testid="tt-origin"]')?.focus())
}
function pickOrigin(code: string): void {
  country.value = code
  closeOrigin()
}
function onOriginKey(e: KeyboardEvent): void {
  const n = originOptions.value.length
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault(); e.stopPropagation()
    if (!originOpen.value) { openOrigin(); return }
    originActive.value = (originActive.value + (e.key === 'ArrowDown' ? 1 : -1) + n) % n
    scrollOriginActive()
  }
  else if (e.key === 'Enter' && originOpen.value) {
    e.preventDefault(); e.stopPropagation()
    const c = originOptions.value[originActive.value]
    if (c !== undefined) pickOrigin(c)
  }
  else if (e.key === 'Escape' && originOpen.value) {
    e.preventDefault(); e.stopPropagation()
    closeOrigin()
  }
}
function onDocPointerDown(e: PointerEvent): void {
  if (originOpen.value && originWrap.value && !originWrap.value.contains(e.target as Node)) closeOrigin(false)
}
onMounted(() => document.addEventListener('pointerdown', onDocPointerDown, true))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocPointerDown, true))
watch(open, () => { originOpen.value = false })
const orbitLabel = computed(() => ORBITS.find(o => o.value === orbit.value)?.label ?? 'any')
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent class="tt-dialog gap-0 p-0" data-testid="tt-dialog" @keydown="onKeydown">
      <div class="tt-head">
        <DialogTitle class="tt-title">
          TRACK TARGET
        </DialogTitle>
        <DialogDescription class="sr-only">
          Search the satellite catalogue and track the selected target
        </DialogDescription>
      </div>

      <div class="tt-body">
        <div class="tt-tabs" role="tablist">
          <button type="button" role="tab" aria-selected="true" class="tt-tab tt-tab-on" data-testid="tt-tab-satellite">
            SATELLITE
          </button>
          <button type="button" role="tab" aria-selected="false" class="tt-tab" disabled title="No ADS-B source configured" data-testid="tt-tab-aircraft">
            AIRCRAFT
          </button>
        </div>

        <div class="tt-filters">
          <input
            v-model="q" class="ov-dms-box tt-search" type="text" placeholder="Search name or NORAD ID" data-testid="tt-search"
            autocomplete="off" spellcheck="false"
          >
          <span class="tt-lbl">Orbit</span>
          <SelectRoot v-model="orbit">
            <SelectTrigger class="ov-fmt-sel fmt-select-trigger tt-sel tt-sel-orbit" data-testid="tt-orbit" title="Orbit">
              <span class="fmt-select-text">{{ orbitLabel }}</span>
            </SelectTrigger>
            <SelectPortal>
              <SelectContent class="fmt-select-content" :style="{ zIndex: 'var(--z-popover)' }" position="popper">
                <SelectViewport>
                  <SelectItem v-for="o in ORBITS" :key="o.value" class="fmt-select-item" :value="o.value" :data-testid="`tt-orbit-${o.value}`">
                    <SelectItemText>{{ o.label }}</SelectItemText>
                  </SelectItem>
                </SelectViewport>
              </SelectContent>
            </SelectPortal>
          </SelectRoot>
          <span class="tt-lbl">Origin</span>
          <div ref="originWrap" class="tt-origin-wrap" @keydown="onOriginKey">
            <button
              type="button" class="ov-fmt-sel fmt-select-trigger tt-sel tt-sel-origin" data-testid="tt-origin" title="Origin"
              role="combobox" aria-haspopup="listbox" :aria-expanded="originOpen" @click="originOpen ? closeOrigin() : openOrigin()"
            >
              <span class="fmt-select-text tt-flagtext">
                <FlagBadge v-if="country !== ANY" :code="country" />
                {{ country === ANY ? 'any' : country }}
              </span>
            </button>
            <div v-if="originOpen" class="tt-origin-pop" data-testid="tt-origin-pop">
              <input
                ref="originInput" v-model="originQuery" class="tt-origin-search" type="text" placeholder="Type to filter"
                data-testid="tt-origin-search" autocomplete="off" spellcheck="false" aria-label="Filter origin"
              >
              <ul ref="originListEl" class="tt-origin-list" role="listbox">
                <li
                  v-for="(c, i) in originOptions" :key="c" class="tt-origin-item" role="option" :aria-selected="c === country"
                  :data-active="i === originActive" :data-testid="c === ANY ? 'tt-origin-any' : `tt-origin-${c}`"
                  @click="pickOrigin(c)" @pointermove="originActive = i"
                >
                  <span class="tt-origin-flag"><FlagBadge v-if="c !== ANY" :code="c" /></span>
                  <span class="tt-origin-text">{{ c === ANY ? 'any' : ownerName(c) }}</span>
                </li>
                <li v-if="originOptions.length === 1" class="tt-origin-none">No match</li>
              </ul>
            </div>
          </div>
          <button type="button" class="tt-btn" data-testid="tt-filter" @click="runSearch">
            FILTER
          </button>
          <button type="button" class="tt-btn" data-testid="tt-clear" @click="onClear">
            CLEAR
          </button>
        </div>

        <div ref="tableEl" class="tt-table" data-testid="tt-table">
          <table>
            <thead>
              <tr>
                <th class="tt-ic" />
                <th v-for="c in COLS" :key="c.key" :class="`tt-c-${c.key}`" :aria-sort="ariaSort(c.key)">
                  <button type="button" class="tt-th" :data-testid="`tt-sort-${c.key}`" @click="onSort(c.key)">
                    {{ c.label }}{{ arrow(c.key) }}
                  </button>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="r in sorted" :key="r.norad" class="tt-row" :class="{ 'tt-sel-row': r.norad === selected }"
                data-testid="tt-row" :data-norad="r.norad" :data-selected="r.norad === selected" @click="selectRow(r.norad)" @dblclick="onDoubleClick(r.norad)"
              >
                <td class="tt-ic">
                  <img :src="`${base}icons/satellite.svg`" alt="" aria-hidden="true">
                </td>
                <td class="tt-c-norad">
                  {{ r.norad }}
                </td>
                <td class="tt-c-name" :title="r.name">
                  {{ r.name }}
                </td>
                <td class="tt-c-orbit">
                  {{ r.orbit_class ?? '—' }}
                </td>
                <td class="tt-c-origin">
                  <span class="tt-flagtext" :title="r.country ? ownerName(r.country) : undefined"><FlagBadge :code="r.country" />{{ r.country ?? '—' }}</span>
                </td>
                <td class="tt-c-epoch">
                  <span class="tt-flagtext">{{ formatEpoch(r.tle_epoch, clockMode, tz) }}<StaleBadge :age-days="r.tle_age_days" /></span>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-if="loading && !rows.length" class="tt-note" data-testid="tt-loading">
            Loading…
          </p>
          <p v-else-if="error" class="tt-note tt-note-err" data-testid="tt-error">
            {{ DATA_UNAVAILABLE }}
          </p>
          <p v-else-if="!loading && !rows.length" class="tt-note" data-testid="tt-empty">
            No matches
          </p>
          <p v-if="capped" class="tt-foot" data-testid="tt-footer">
            {{ FOOTER_CAPPED }}
          </p>
        </div>

        <button type="button" class="tt-go" data-testid="tt-track" :disabled="!status.canTrack" @click="onTrack">
          TRACK SELECTED TARGET
        </button>
        <p class="tt-status" :class="status.tone === 'danger' ? 'tt-status-err' : ''" data-testid="tt-status" role="status">
          {{ status.text }}
        </p>
      </div>
    </DialogContent>
  </Dialog>
</template>

<style>
.tt-dialog {
  background: var(--page-bg); border: 1px solid var(--control-border); width: 860px; max-width: calc(100vw - 24px);
  max-height: calc(100vh - 24px); display: flex; flex-direction: column; overflow: hidden;
}
.tt-dialog .tt-head { flex: none; padding: 12px 18px; border-bottom: 1px solid var(--control-border); }
.tt-dialog .tt-title { font-size: 16px; font-weight: 700; letter-spacing: 0.02em; text-transform: uppercase; }
.tt-dialog .tt-body { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; gap: 10px; padding: 14px 22px 12px; }
.tt-dialog .tt-tabs { flex: none; display: flex; justify-content: center; }
.tt-dialog .tt-tab {
  min-width: 150px; height: 36px; padding: 0 16px; border: 2px solid var(--control-border); background: transparent;
  color: var(--foreground); font-weight: 700; font-size: 16px; letter-spacing: 0.02em; cursor: pointer;
}
.tt-dialog .tt-tab-on { border-color: var(--accent-green-border); color: var(--accent-green-text); background: color-mix(in srgb, var(--accent-green-border) 30%, transparent); }
.tt-dialog .tt-tab:disabled { color: var(--foreground); cursor: not-allowed; opacity: 0.75; }
.tt-dialog .tt-filters { flex: none; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; }
.tt-dialog .tt-search { flex: 1 1 200px; min-width: 160px; height: var(--field-h); padding: 0 8px; font-size: 14px; font-weight: 400; text-align: left; }
.tt-dialog .tt-lbl { font-size: 14px; font-weight: 600; color: var(--foreground); }
.tt-dialog .tt-sel { margin-right: 0; font-size: 14px; }
.tt-dialog .tt-sel-orbit { width: 80px; }
.tt-dialog .tt-sel-origin { width: 110px; }
.tt-dialog .tt-flagtext { display: inline-flex; align-items: center; gap: 6px; }
.tt-dialog .tt-btn {
  height: var(--field-h); padding: 0 12px; border: 1px solid var(--control-border); background: transparent;
  color: var(--foreground); font-weight: 700; font-size: 13px; cursor: pointer;
}
.tt-dialog .tt-btn:hover { background: var(--secondary); }
.tt-dialog .tt-origin-wrap { position: relative; }
.tt-dialog .tt-origin-pop {
  /* right-aligned to the Origin box so the wider list opens leftward, inside the dialog (anchored at
     left: 0 it overflowed the dialog's right edge and scrolled the dialog sideways, 10-29) */
  position: absolute; z-index: 20; top: calc(100% + 2px); right: 0; left: auto; width: 340px; max-width: calc(100vw - 48px);
  background: var(--page-bg); border: 1px solid var(--control-border); box-shadow: 0 6px 18px rgb(0 0 0 / 45%);
}
.tt-dialog .tt-origin-search {
  width: 100%; height: var(--field-h); padding: 0 8px; font-size: 14px; background: transparent; color: var(--foreground);
  border: 0; border-bottom: 1px solid var(--control-border); outline: none;
}
.tt-dialog .tt-origin-list { margin: 0; padding: 0; list-style: none; max-height: 240px; overflow-y: auto; }
.tt-dialog .tt-origin-item { display: flex; align-items: center; gap: 8px; padding: 4px 8px; font-size: 14px; cursor: pointer; }
.tt-dialog .tt-origin-item[data-active='true'] { background: var(--secondary); }
.tt-dialog .tt-origin-item[aria-selected='true'] { font-weight: 700; }
.tt-dialog .tt-origin-flag { flex: none; width: 20px; height: 15px; display: inline-flex; align-items: center; justify-content: center; }
.tt-dialog .tt-origin-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tt-dialog .tt-origin-none { padding: 6px 8px; font-size: 13px; color: var(--muted-foreground); }
.tt-dialog .tt-table {
  flex: 1 1 auto; min-height: 110px; max-height: 360px; overflow: auto; background: color-mix(in srgb, var(--page-bg) 70%, #000);
  padding: 0;
}
.tt-dialog table { width: 100%; border-collapse: separate; border-spacing: 0; margin: 0; table-layout: fixed; font-size: 14px; }
/* header flush with the scroll box top: separate borders (collapse made the sticky cell lose its border and let rows
   show through), solid background, bottom rule as an inset shadow, an upward shadow covers sub-pixel gaps. */
.tt-dialog thead th {
  position: sticky; top: 0; z-index: 2; background: color-mix(in srgb, var(--page-bg) 70%, #000);
  box-shadow: inset 0 -1px 0 var(--control-border), 0 -3px 0 color-mix(in srgb, var(--page-bg) 70%, #000);
  text-align: left; padding: 0; border: 0;
}
.tt-dialog tr > :first-child { padding-left: 14px; }
.tt-dialog tr > :last-child { padding-right: 14px; }
.tt-dialog .tt-th { width: 100%; text-align: left; padding: 10px 0 8px; font-size: 14px; font-weight: 600; letter-spacing: 0.02em; color: var(--foreground); background: none; border: 0; cursor: pointer; white-space: nowrap; }
.tt-dialog td { padding: 0; height: 30px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tt-dialog .tt-ic { width: 44px; }
.tt-dialog .tt-ic img { width: 16px; height: 16px; opacity: 0.85; vertical-align: middle; }
.tt-dialog .tt-c-norad { width: 90px; }
.tt-dialog .tt-c-name { width: auto; }
.tt-dialog .tt-c-orbit { width: 100px; }
.tt-dialog .tt-c-origin { width: 120px; }
.tt-dialog .tt-c-epoch { width: 264px; }
.tt-dialog .tt-row { cursor: pointer; }
.tt-dialog .tt-row:hover { background: color-mix(in srgb, var(--foreground) 10%, transparent); }
.tt-dialog .tt-sel-row, .tt-dialog .tt-sel-row:hover { background: #d4d4d4; color: #1c1d20; }
.tt-dialog .tt-note { margin: 0; padding: 18px 0; text-align: center; font-size: 14px; color: var(--muted-foreground); }
.tt-dialog .tt-note-err { color: var(--danger); }
.tt-dialog .tt-foot { margin: 0; padding: 8px 0; text-align: center; font-size: 13px; color: var(--muted-foreground); }
.tt-dialog .tt-go {
  flex: none; height: 42px; border: 1px solid var(--accent-green-border); background: var(--accent-green-border); color: #fff;
  font-weight: 700; font-size: 16px; letter-spacing: 0.02em; cursor: pointer;
}
.tt-dialog .tt-go:hover:not(:disabled) { filter: brightness(1.1); }
.tt-dialog .tt-go:disabled { background: #7a7a7a; border-color: #7a7a7a; color: #d0d0d0; cursor: not-allowed; }
.tt-dialog .tt-status { flex: none; margin: 0; min-height: 20px; font-size: 14px; color: var(--foreground); }
.tt-dialog .tt-status-err { color: var(--danger); }
</style>
