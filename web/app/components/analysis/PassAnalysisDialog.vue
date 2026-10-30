<script setup lang="ts">
// SATELLITE PASS ANALYSIS modal (T5.1c, A2b-i, concept/Pass Analysis.png). Pick a satellite (SEARCH =
// the Track Target table in picker mode), OPTICAL / LINE OF SIGHT, a UTC window, LIST PASSES
// (POST /api/passes, analysis) and a sortable table. Rows in progress or rising within 5 min get a
// live [Track] button (client clock at 1 Hz; enabled by the gateway check of the object, also 1 Hz).
// The result is a snapshot: changing the mode / window keeps it until the next LIST PASSES (its
// header and durations follow the mode it was listed with); changing the satellite clears it.
// Reopening recalls the last state (satellite, mode, window, result); the first open of a page load
// prefills the dashboard's selected / tracked object. Rises / sets outside the listed window show "—".
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import DateTimeField from '@/components/widgets/DateTimeField.vue'
import SegToggle from '@/components/widgets/SegToggle.vue'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import StaleBadge from '~/components/track/StaleBadge.vue'
import TrackTargetDialog from '~/components/track/TrackTargetDialog.vue'
import { useGatewayApi } from '~/composables/useGatewayApi'
import { useMountStore } from '~/stores/mount'
import { useSelectionStore } from '~/stores/selection'
import { useSkyStore } from '~/stores/sky'
import {
  buildPassRequest, checkWindow, DEFAULT_PASS_SORT, defaultEnd, defaultStart, ERR_UNAVAILABLE, fmtDeg, fmtDuration, fmtRise,
  clipToWindow, listBlockReason, NONE_FOUND_LOS, NONE_FOUND_OPTICAL, nextPassSort, passErrorText, rowDuration, siteFromSnapshot, sortPasses,
  trackEligible, type PassMode, type PassResponse, type PassRow, type PassSortKey, type PassSort, type PassWindow,
} from '~/utils/pass-analysis'
import { getPassModalMemo, setPassModalMemo } from '~/utils/pass-analysis-memo'
import type { SatRow } from '~/utils/track-target'
import { isObjectTrackActive, type ObjectTrackLike } from '~/utils/track-state'
import { parseTrackVerdict, UNREACHABLE_VERDICT, type TrackVerdict } from '~/utils/track-verdict'

const open = defineModel<boolean>('open', { default: false })

const mount = useMountStore()
const sel = useSelectionStore()
const sky = useSkyStore()
const api = useGatewayApi()

const FETCH_TIMEOUT_MS = 30_000 // the analysis service itself gives up at 20 s
const TICK_MS = 1000

interface Picked { norad: number, name: string, ageDays: number | null }

const sat = ref<Picked | null>(null)
const mode = ref<PassMode>('optical')
const start = ref<Date | null>(null)
const end = ref<Date | null>(null)
const pickerOpen = ref(false)
const loading = ref(false)
const error = ref<string | null>(null)
const result = ref<PassResponse | null>(null)
const resultMode = ref<PassMode>('optical')
const resultWin = ref<PassWindow | null>(null)
const sort = ref<PassSort>({ ...DEFAULT_PASS_SORT })
const nowS = ref(Date.now() / 1000)
const verdict = ref<TrackVerdict | null>(null)
const sending = ref(false)
const ackRefusal = ref<string | null>(null)

const MODE_OPTIONS = [{ value: 'optical', label: 'OPTICAL' }, { value: 'los', label: 'LINE OF SIGHT' }]

const station = computed(() => siteFromSnapshot(mount.snapshot))
const win = computed(() => checkWindow(start.value, end.value))
const blockReason = computed(() => listBlockReason({
  norad: sat.value?.norad ?? null, site: station.value?.site ?? null, limEl: station.value?.limEl ?? null, start: start.value, end: end.value,
}))
const canList = computed(() => blockReason.value === null && !loading.value)
const satText = computed(() => (sat.value ? `${sat.value.name} (${sat.value.norad})` : ''))
const sorted = computed(() => (result.value ? sortPasses(result.value.passes, sort.value, resultMode.value, resultWin.value) : []))
const shown = (p: PassRow): PassRow => clipToWindow(p, resultWin.value)
const durLabel = computed(() => ((result.value ? resultMode.value : mode.value) === 'optical' ? 'Visible Duration' : 'Duration'))
const emptyText = computed(() => (resultMode.value === 'optical' ? NONE_FOUND_OPTICAL : NONE_FOUND_LOS))
const trackedNorad = computed<number | null>(() => {
  const t = (mount.snapshot?.track ?? null) as ObjectTrackLike | null
  return isObjectTrackActive(t) && typeof t?.norad === 'number' ? t.norad : null
})

const eligible = (p: PassRow): boolean => !!result.value && trackEligible(p, nowS.value)
const anyButton = computed(() => !!result.value && result.value.passes.some(p => trackEligible(p, nowS.value)))
const isTracking = computed(() => !!result.value && trackedNorad.value === result.value.norad)

// ----- satellite: prefill + picker
async function lookup(norad: number): Promise<Picked> {
  try {
    const res = await fetch(`/api/sats/search?q=${norad}&limit=5`, { cache: 'no-store', signal: AbortSignal.timeout(8000) })
    if (res.ok) {
      const rows = await res.json() as SatRow[]
      const r = Array.isArray(rows) ? rows.find(x => x.norad === norad) : undefined
      if (r) return { norad, name: r.name, ageDays: r.tle_age_days }
    }
  }
  catch { /* fall through to the sky metadata name */ }
  return { norad, name: sky.meta.get(norad)?.name ?? `NORAD ${norad}`, ageDays: null }
}

function setSat(p: Picked | null): void {
  if (p?.norad === sat.value?.norad) { sat.value = p; return }
  sat.value = p
  result.value = null // a table of another satellite would mislead
  resultWin.value = null
  error.value = null
  ackRefusal.value = null
}

function onPicked(r: SatRow): void {
  setSat({ norad: r.norad, name: r.name, ageDays: r.tle_age_days })
}

// ----- LIST PASSES
let reqSeq = 0
async function onList(): Promise<void> {
  const st = station.value
  const s = start.value
  const e = end.value
  if (!canList.value || !st || !s || !e || !sat.value) return
  const my = ++reqSeq
  const norad = sat.value.norad
  const m = mode.value
  loading.value = true
  error.value = null
  ackRefusal.value = null
  try {
    const res = await fetch('/api/passes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      body: JSON.stringify(buildPassRequest({ norad, site: st.site, limEl: st.limEl, start: s, end: e, mode: m })),
    })
    const body = await res.json().catch(() => null) as (PassResponse & { detail?: unknown }) | null
    if (my !== reqSeq) return
    if (!res.ok || !body || !Array.isArray(body.passes)) {
      result.value = null
      error.value = passErrorText(res.ok ? 0 : res.status, body?.detail)
    }
    else {
      result.value = body
      resultMode.value = m
      resultWin.value = { start: Math.floor(s.getTime() / 1000), end: Math.floor(e.getTime() / 1000) }
      sort.value = { ...DEFAULT_PASS_SORT }
      if (sat.value?.norad === norad) sat.value = { ...sat.value, ageDays: body.tle_age_days ?? sat.value.ageDays }
    }
  }
  catch {
    if (my !== reqSeq) return
    result.value = null
    error.value = ERR_UNAVAILABLE
  }
  if (my === reqSeq) loading.value = false
}

function onSort(k: PassSortKey): void {
  sort.value = nextPassSort(sort.value, k)
}

// ----- [Track]: the gateway check of the listed object at 1 Hz while a button shows
let checkGen = 0
let inFlight = false
let timer: ReturnType<typeof setInterval> | null = null

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
  verdict.value = null
}

function tick(): void {
  nowS.value = Date.now() / 1000
  if (anyButton.value && result.value && !isTracking.value) void runVerdict(result.value.norad, checkGen)
}

watch(open, (o, was) => {
  if (timer) clearInterval(timer)
  timer = null
  stopVerdict()
  reqSeq++
  if (!o) {
    pickerOpen.value = false
    if (was) {
      setPassModalMemo({
        sat: sat.value, mode: mode.value, start: start.value, end: end.value, result: result.value,
        resultMode: resultMode.value, resultWin: resultWin.value, sort: { ...sort.value },
      })
    }
    return
  }
  error.value = null
  ackRefusal.value = null
  loading.value = false
  sending.value = false
  nowS.value = Date.now() / 1000
  const memo = getPassModalMemo()
  if (memo) {
    sat.value = memo.sat
    mode.value = memo.mode
    start.value = memo.start
    end.value = memo.end
    result.value = memo.result
    resultMode.value = memo.resultMode
    resultWin.value = memo.resultWin
    sort.value = { ...memo.sort }
  }
  else {
    const n = sel.selectedNorad ?? trackedNorad.value
    sat.value = null
    result.value = null
    resultWin.value = null
    mode.value = 'optical'
    sort.value = { ...DEFAULT_PASS_SORT }
    const s = defaultStart(Date.now())
    start.value = s
    end.value = defaultEnd(s)
    if (n !== null) void lookup(n).then((p) => { if (open.value && sat.value === null) sat.value = p })
  }
  timer = setInterval(tick, TICK_MS)
}, { immediate: true })

// A different listed object (or none): forget the verdict of the previous one.
watch(() => result.value?.norad, () => { stopVerdict(); ackRefusal.value = null })
onBeforeUnmount(() => { if (timer) clearInterval(timer); stopVerdict(); reqSeq++ })

const btn = computed<{ text: string, disabled: boolean, title: string }>(() => {
  if (isTracking.value) return { text: 'TRACKING', disabled: true, title: 'This object is being tracked' }
  const v = verdict.value
  if (sending.value) return { text: 'TRACK', disabled: true, title: 'Starting track…' }
  if (ackRefusal.value) return { text: 'TRACK', disabled: !v?.enabled, title: ackRefusal.value }
  if (!v) return { text: 'TRACK', disabled: true, title: 'Checking…' }
  if (!v.enabled) return { text: 'TRACK', disabled: true, title: v.reason ?? 'Command refused' }
  return { text: 'TRACK', disabled: false, title: v.notes.length ? v.notes.join(' · ') : 'Track this object' }
})

async function onTrack(): Promise<void> {
  const n = result.value?.norad
  if (n === undefined || btn.value.disabled || sending.value) return
  sending.value = true
  ackRefusal.value = null
  try {
    const ack = await mount.sendCommand('track_object', { norad: n })
    if (ack.ok) sel.afterTrackStarted(n)
    else ackRefusal.value = ack.reason ?? 'Command refused'
  }
  finally {
    sending.value = false
  }
}

const COLS: Array<{ key: PassSortKey, cls: string }> = [
  { key: 'rise', cls: 'pa-c-rise' }, { key: 'rise_az', cls: 'pa-c-az' }, { key: 'max_el', cls: 'pa-c-el' },
  { key: 'set', cls: 'pa-c-set' }, { key: 'set_az', cls: 'pa-c-az' }, { key: 'dur', cls: 'pa-c-dur' },
]
const colLabel = (k: PassSortKey): string => ({
  rise: 'Rises (UTC)', rise_az: 'Rise Az', max_el: 'Max El', set: 'Sets (UTC)', set_az: 'Set Az', dur: durLabel.value,
})[k]
const arrow = (k: PassSortKey): string => (sort.value.key === k ? (sort.value.dir === 'desc' ? ' ▼' : ' ▲') : '')
const ariaSort = (k: PassSortKey): 'ascending' | 'descending' | 'none' =>
  sort.value.key === k ? (sort.value.dir === 'desc' ? 'descending' : 'ascending') : 'none'
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent class="pa-dialog gap-0 p-0" data-testid="pa-dialog">
      <div class="pa-head">
        <DialogTitle class="pa-title">
          SATELLITE PASS ANALYSIS
        </DialogTitle>
        <DialogDescription class="sr-only">
          List the passes of a satellite over the station in a UTC window
        </DialogDescription>
      </div>

      <div class="pa-body">
        <div class="pa-form">
          <span class="pa-lbl">Satellite</span>
          <div class="pa-sat-wrap">
            <div
              class="ov-dms-box pa-sat" :class="{ 'pa-sat-empty': !sat }" data-testid="pa-sat" :title="satText || 'Search a satellite'"
              role="button" tabindex="0" @click="pickerOpen = true" @keydown.enter.prevent="pickerOpen = true"
            >
              <span v-if="sat" class="pa-sat-text">{{ satText }}</span>
              <span v-else class="pa-sat-text">no satellite selected</span>
              <StaleBadge v-if="sat" :age-days="sat.ageDays" />
            </div>
            <button type="button" class="pa-search" data-testid="pa-search" @click="pickerOpen = true">
              SEARCH
            </button>
          </div>
          <span class="pa-lbl pa-lbl-r">Start</span>
          <span class="pa-dt" :class="{ 'is-invalid': win.startInvalid }" data-testid="pa-start">
            <DateTimeField v-model="start" label="Start (UTC)" />
          </span>

          <span class="pa-lbl">Mode</span>
          <SegToggle v-model="mode" :options="MODE_OPTIONS" seg-class="pa-seg" data-testid="pa-mode" />
          <span class="pa-lbl pa-lbl-r">End</span>
          <span class="pa-dt" :class="{ 'is-invalid': win.endInvalid }" data-testid="pa-end">
            <DateTimeField v-model="end" label="End (UTC)" />
          </span>
        </div>

        <button type="button" class="pa-list" data-testid="pa-list" :disabled="!canList" :title="blockReason ?? undefined" @click="onList">
          {{ loading ? 'LISTING…' : 'LIST PASSES' }}
        </button>

        <div class="pa-table" data-testid="pa-table">
          <table>
            <thead>
              <tr>
                <th v-for="c in COLS" :key="c.key" :class="c.cls" :aria-sort="ariaSort(c.key)">
                  <button type="button" class="pa-th" :data-testid="`pa-sort-${c.key}`" @click="onSort(c.key)">
                    {{ colLabel(c.key) }}{{ arrow(c.key) }}
                  </button>
                </th>
                <th class="pa-c-trk" />
              </tr>
            </thead>
            <tbody>
              <tr v-for="p in sorted" :key="`${p.t_rise ?? 'x'}-${p.t_peak}`" class="pa-row" data-testid="pa-row" :data-t-rise="p.t_rise ?? ''">
                <td class="pa-c-rise">
                  {{ fmtRise(shown(p).t_rise) }}
                </td>
                <td class="pa-c-az">
                  {{ fmtDeg(shown(p).rise_az) }}
                </td>
                <td class="pa-c-el">
                  {{ fmtDeg(p.max_el) }}
                </td>
                <td class="pa-c-set">
                  {{ fmtRise(shown(p).t_set) }}
                </td>
                <td class="pa-c-az">
                  {{ fmtDeg(shown(p).set_az) }}
                </td>
                <td class="pa-c-dur">
                  {{ fmtDuration(rowDuration(p, resultMode)) }}
                </td>
                <td class="pa-c-trk">
                  <button
                    v-if="eligible(p)" type="button" class="pa-track" data-testid="pa-track" :disabled="btn.disabled"
                    :title="btn.title" @click="onTrack"
                  >
                    {{ btn.text }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-if="error" class="pa-note" data-testid="pa-error">
            {{ error }}
          </p>
          <p v-else-if="result && !result.passes.length" class="pa-note" data-testid="pa-empty">
            {{ emptyText }}
          </p>
        </div>

        <p class="pa-status" data-testid="pa-status" role="status">
          {{ ackRefusal ?? '' }}
        </p>
      </div>
    </DialogContent>
  </Dialog>
  <TrackTargetDialog v-model:open="pickerOpen" mode="pick" @picked="onPicked" />
</template>

<style>
.pa-dialog {
  background: var(--page-bg); border: 1px solid var(--control-border); width: 780px; max-width: calc(100vw - 24px);
  max-height: calc(100vh - 24px); display: flex; flex-direction: column; overflow: hidden;
}
.pa-dialog .pa-head { flex: none; padding: 12px 18px; border-bottom: 1px solid var(--control-border); }
.pa-dialog .pa-title { font-size: 16px; font-weight: 700; letter-spacing: 0.02em; text-transform: uppercase; }
.pa-dialog .pa-body { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; gap: 12px; padding: 16px 22px 10px; }
/* 10-30 user sizing: label 64 | satellite field 200 + SEARCH 72 (= Mode 272) | free | label 40 | date suite (flush right) */
.pa-dialog .pa-form { flex: none; display: grid; grid-template-columns: 64px 272px minmax(0, 1fr) 40px auto; gap: 10px; align-items: center; }
.pa-dialog .pa-lbl { font-size: 15px; font-weight: 700; color: var(--foreground); }
.pa-dialog .pa-lbl-r { grid-column: 4; justify-self: start; }
.pa-dialog .pa-sat-wrap { display: flex; min-width: 0; }
.pa-dialog .pa-sat {
  flex: 1 1 auto; min-width: 0; display: flex; align-items: center; gap: 6px; padding: 0 8px; text-align: left; font-weight: 500;
  border-right: 0; cursor: pointer;
}
.pa-dialog .pa-sat-empty { color: var(--muted-foreground); }
.pa-dialog .pa-sat-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pa-dialog .pa-search {
  flex: none; height: var(--field-h); padding: 0 10px; border: 1px solid var(--control-border); background: var(--secondary);
  color: var(--foreground); font-weight: 700; font-size: 13px; letter-spacing: 0.02em; cursor: pointer;
}
.pa-dialog .pa-search:hover { border-color: var(--accent-green-border); }
.pa-dialog .pa-seg { min-width: 0; }
.pa-dialog .pa-dt { grid-column: 5; display: flex; align-items: center; justify-self: end; }
.pa-dialog .pa-list {
  flex: none; height: 44px; border: 1px solid var(--accent-green-border); background: var(--accent-green-border); color: #fff;
  font-weight: 700; font-size: 16px; letter-spacing: 0.02em; cursor: pointer;
}
.pa-dialog .pa-list:hover:not(:disabled) { filter: brightness(1.1); }
.pa-dialog .pa-list:disabled { background: #7a7a7a; border-color: #7a7a7a; color: #d0d0d0; cursor: not-allowed; }
.pa-dialog .pa-table {
  flex: 1 1 auto; min-height: 80px; height: 340px; overflow: auto; background: color-mix(in srgb, var(--page-bg) 70%, #000); padding: 0;
}
.pa-dialog table { width: 100%; border-collapse: separate; border-spacing: 0; margin: 0; table-layout: fixed; font-size: 14px; }
/* header flush with the scroll box top (the Track Target fix, T4.3d): separate borders, solid background,
   bottom rule as an inset shadow, an upward shadow covers sub-pixel gaps. */
.pa-dialog thead th {
  position: sticky; top: 0; z-index: 2; background: color-mix(in srgb, var(--page-bg) 70%, #000);
  box-shadow: inset 0 -1px 0 var(--control-border), 0 -3px 0 color-mix(in srgb, var(--page-bg) 70%, #000);
  text-align: left; padding: 0; border: 0;
}
.pa-dialog tr > :first-child { padding-left: 14px; }
.pa-dialog tr > :last-child { padding-right: 14px; }
.pa-dialog .pa-th { width: 100%; text-align: left; padding: 10px 0 8px; font-size: 14px; font-weight: 600; letter-spacing: 0.02em; color: var(--foreground); background: none; border: 0; cursor: pointer; white-space: nowrap; }
.pa-dialog td { padding: 0; height: 32px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-variant-numeric: tabular-nums; }
/* 10-30 user sizing: each column = its widest content / header (incl. the arrow) + a 20 px gap (padding-right) */
.pa-dialog th, .pa-dialog td { padding-right: 20px; }
.pa-dialog .pa-c-rise { width: 164px; } /* 150 + the 14 px row inset */
.pa-dialog .pa-c-az { width: 80px; }
.pa-dialog .pa-c-el { width: 75px; }
.pa-dialog .pa-c-set { width: 150px; }
.pa-dialog .pa-c-dur { width: auto; min-width: 85px; } /* user 10-30: -40; a wide "Visible Duration" header runs into the empty [Track] header */
.pa-dialog thead th.pa-c-dur { z-index: 3; } /* paint the overflowing header over the [Track] header */
.pa-dialog .pa-c-trk { width: 98px; } /* 84 + the 14 px row inset */
.pa-dialog .pa-row:hover { background: color-mix(in srgb, var(--foreground) 10%, transparent); }
.pa-dialog .pa-track {
  height: 24px; padding: 0 10px; border: 1px solid var(--accent-green-border); background: var(--accent-green-border); color: #fff;
  font-weight: 700; font-size: 12px; letter-spacing: 0.02em; cursor: pointer; vertical-align: middle;
}
.pa-dialog .pa-track:hover:not(:disabled) { filter: brightness(1.1); }
.pa-dialog .pa-track:disabled { background: #7a7a7a; border-color: #7a7a7a; color: #d0d0d0; cursor: not-allowed; }
.pa-dialog .pa-note { margin: 0; padding: 18px 0; text-align: center; font-size: 14px; color: var(--muted-foreground); }
.pa-dialog .pa-status { flex: none; margin: 0; min-height: 20px; font-size: 13px; color: var(--muted-foreground); }
@media (max-height: 520px) {
  .pa-dialog .pa-body { gap: 8px; padding: 10px 22px 6px; }
  .pa-dialog .pa-head { padding: 8px 18px; }
  .pa-dialog .pa-list { height: 34px; }
}
</style>
