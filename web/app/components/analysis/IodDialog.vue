<script setup lang="ts">
// INITIAL ORBIT DETERMINATION modal (T5.3b, A2b-ii, concept/IOD.png). Three angles-only observation rows
// (Add Manual; Add Astrometry opens the Astrometry dialog and the row stays pending until the job pill's job finishes, T5.2d), INITIAL ORBIT DETERMINATION (POST /api/iod, analysis),
// the P2 state vector and a coarse TLE with copy buttons. Add Manual: the row turns into inputs (epoch =
// now, UTC); typing in one angle pair clears the other; Enter / clicking outside confirms and the other pair
// is computed by H4 (POST /api/transform, gateway) at the ROW epoch and shown dimmed; Esc cancels. Edit Entry reopens a filled row with its bright pair at full precision (becomes Manual). Adding to /
// removing an occupied valid row asks first. Reopening recalls the rows + results (module memo, saved on
// close only); a refresh clears them. Editing a row after a result greys the outputs as stale.
import { computed, nextTick, onBeforeUnmount, ref, toRaw, watch } from 'vue'
import AstrometryDialog from '@/components/analysis/AstrometryDialog.vue'
import DateTimeField from '@/components/widgets/DateTimeField.vue'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { useGatewayApi } from '~/composables/useGatewayApi'
import { useAstrometryJobStore } from '~/stores/astrometry-job'
import { useMountStore } from '~/stores/mount'
import type { AstrometryRunPayload } from '~/utils/astrometry'
import { failureText, isActiveState, relayErrorText, type AstrometryJob } from '~/utils/astrometry-job'
import {
  applyIodTransform, astrometryFailed, astrometryPending, buildIodRequest, buildIodTransform, checkDraft, confirmText, doneText, draftFromRow, draftToRow, editDraft,
  emptyRow, emptyRows, epochArrow, ERR_IOD_UNAVAILABLE, fmtAngle, fmtEpoch, fmtStateVector, fmtTle, iodErrorText, iodReadiness, isDim,
  menuState, needsConfirm, newDraft, nextEpochDir, NOT_CONVERGED_TEXT, parseIodResponse, rowDisplay, rowFromJobResult, rowValid, sortedSlots, SLOTS, STALE_TEXT,
  type DraftField, type EpochDir, type IodDraft, type IodResponse, type IodRow,
} from '~/utils/iod'
import { getIodMemo, setIodMemo, type IodStatus } from '~/utils/iod-memo'
import { siteFromSnapshot } from '~/utils/pass-analysis'

const open = defineModel<boolean>('open', { default: false })

const mount = useMountStore()
const api = useGatewayApi()
const astro = useAstrometryJobStore()

const FETCH_TIMEOUT_MS = 30_000
const COPIED_MS = 1500
const MENU_W = 150
const FIELDS: Array<{ key: DraftField, label: string, signed: boolean, tip: string }> = [
  { key: 'az', label: 'Az.', signed: false, tip: 'Az. 0 to 360°' },
  { key: 'el', label: 'El.', signed: true, tip: 'El. 0 to 90° (above the horizon)' },
  { key: 'ra', label: 'R.A.', signed: false, tip: 'R.A. 0 to 360° (J2000)' },
  { key: 'dec', label: 'Dec.', signed: true, tip: 'Dec. -90 to +90° (J2000)' },
]

const rows = ref<IodRow[]>(emptyRows())
const dir = ref<EpochDir>('asc')
const editing = ref<{ slot: number, draft: IodDraft, showErr: boolean } | null>(null)
const confirm = ref<{ kind: 'replace' | 'remove', slot: number, via?: 'manual' | 'astrometry' } | null>(null)
const menu = ref<{ slot: number, x: number, y: number } | null>(null)
const converting = ref<boolean[]>(Array.from({ length: SLOTS }, () => false))
const result = ref<IodResponse | null>(null)
const stale = ref(false)
const running = ref(false)
const status = ref<IodStatus>({ kind: 'none', text: '' })
const copied = ref({ state: false, tle: false })
const bodyEl = ref<HTMLElement | null>(null)
const astroOpen = ref(false)
const astroSlot = ref<number | null>(null)
const astroBusy = ref(false)
const astroStatus = ref<{ kind: 'info' | 'error', text: string } | null>(null)

const station = computed(() => siteFromSnapshot(mount.snapshot))
const readiness = computed(() => iodReadiness(rows.value, !!station.value))
const canRun = computed(() => readiness.value.ready && !running.value)
const order = computed(() => sortedSlots(rows.value, dir.value))
const draftCheck = computed(() => (editing.value?.showErr ? checkDraft(editing.value.draft) : null))
const badFields = computed(() => new Set<string>(draftCheck.value?.kind === 'invalid' ? draftCheck.value.bad : []))
const editReason = computed(() => (draftCheck.value?.kind === 'invalid' ? draftCheck.value.reason : null))
const stateText = computed(() => (result.value ? fmtStateVector(result.value.r_km, result.value.v_kms) : ''))
const tleText = computed(() => (result.value ? fmtTle(result.value.tle) : ''))
const statusText = computed(() => (stale.value && result.value ? STALE_TEXT : status.value.text))

// ----- row actions
const seq = [0, 0, 0]

/** While the modal is closed the memo is the row store: keep it current when a job / H4 answer lands. */
function persistIfClosed(): void {
  if (!open.value) setIodMemo({ rows: rows.value, result: result.value, stale: stale.value, status: { ...status.value }, dir: dir.value })
}

function markStale(): void {
  if (result.value) stale.value = true
}

function setRow(slot: number, r: IodRow): void {
  rows.value = rows.value.map((x, i) => (i === slot ? r : x))
  markStale()
}

async function convert(slot: number, row: IodRow): Promise<void> {
  const site = station.value?.site
  const req = site ? buildIodTransform(row, site) : null
  if (!req) return
  const my = ++seq[slot]!
  converting.value = converting.value.map((c, i) => (i === slot ? true : c))
  let next: IodRow | null = null
  try {
    next = applyIodTransform(row, (await api.post<{ result?: unknown }>('/api/transform', req))?.result)
  }
  catch { /* the other pair stays empty ("—") */ }
  if (my !== seq[slot] || toRaw(rows.value[slot]) !== toRaw(row)) return
  converting.value = converting.value.map((c, i) => (i === slot ? false : c))
  if (next) rows.value = rows.value.map((x, i) => (i === slot ? next! : x))
  persistIfClosed()
}

function cancelEdit(): void {
  editing.value = null
}

function startEdit(slot: number, draft: IodDraft = newDraft(Date.now())): void {
  confirm.value = null
  editing.value = { slot, draft, showErr: false }
  const first: DraftField = draft.ra !== '' ? 'ra' : 'az'
  void nextTick(() => (document.body.querySelector(`[data-testid="iod-row"][data-slot="${slot + 1}"] [data-testid="iod-${first}"]`) as HTMLElement | null)?.focus())
}

/** (...) > Edit Entry: the row's epoch + bright pair at full precision; Esc leaves the row untouched (it changes only on commit). */
function onEditEntry(slot: number): void {
  menu.value = null
  const d = menuState(rows.value[slot]!).edit ? draftFromRow(rows.value[slot]!) : null
  if (!d) return
  cancelEdit()
  startEdit(slot, d)
}

function commitEdit(): void {
  const e = editing.value
  if (!e) return
  const c = checkDraft(e.draft)
  if (c.kind === 'blank') { cancelEdit(); return }
  if (c.kind === 'invalid') { e.showErr = true; return }
  const row = draftToRow(e.draft)
  if (!row) return
  editing.value = null
  seq[e.slot] = (seq[e.slot] ?? 0) + 1
  converting.value = converting.value.map((c2, i) => (i === e.slot ? false : c2))
  setRow(e.slot, row)
  void convert(e.slot, row)
}

function onField(f: DraftField, ev: Event): void {
  const e = editing.value
  if (!e) return
  e.draft = editDraft(e.draft, f, (ev.target as HTMLInputElement).value)
  ;(ev.target as HTMLInputElement).value = e.draft[f]
}

function onEnter(): void {
  ;(document.activeElement as HTMLElement | null)?.blur?.() // commits a DateTimeField box first
  commitEdit()
}

function onRowFocusOut(ev: FocusEvent): void {
  const e0 = editing.value
  if (!e0) return
  const rowEl = ev.currentTarget as HTMLElement
  const inside = (n: EventTarget | null): boolean => {
    const el = n as HTMLElement | null
    return !!el && (rowEl.contains(el) || !!el.closest?.('#dt-pop'))
  }
  if (inside(ev.relatedTarget)) return
  // a focus loss with no new focused element (a removed button, e.g. the confirm's Yes; focus parked on the body / the dialog box itself) is not "leaving the row"
  setTimeout(() => {
    const a = document.activeElement
    if (editing.value === e0 && a && a !== document.body && a.getAttribute('data-testid') !== 'iod-dialog' && !inside(a)) commitEdit()
  }, 0)
}

/** A pointer press outside the edited row (and the epoch popover / the row menu) commits, also on a non-focusable spot. */
function onEditPointer(ev: Event): void {
  const e = editing.value
  if (!e) return
  const t = ev.target as HTMLElement | null
  const rowEl = document.body.querySelector(`[data-testid="iod-row"][data-slot="${e.slot + 1}"]`)
  if (!t || rowEl?.contains(t) || t.closest?.('#dt-pop') || t.closest?.('[data-testid="iod-menu"]') || t.closest?.('[data-testid="iod-menu-btn"]')) return
  if (!t.closest?.('[data-testid="iod-dialog"]')) return
  commitEdit()
}

function onEscape(ev: Event): void {
  if (editing.value || confirm.value || menu.value) {
    ev.preventDefault() // the first Esc only backs out of the edit / confirm / menu
    if (menu.value) menu.value = null
    else if (confirm.value) confirm.value = null
    else cancelEdit()
  }
}

// ----- (...) menu
function onDocPointer(ev: Event): void {
  const t = ev.target as HTMLElement | null
  if (t?.closest?.('[data-testid="iod-menu"]') || t?.closest?.('[data-testid="iod-menu-btn"]')) return
  menu.value = null
}

function openMenu(slot: number, ev: MouseEvent): void {
  if (menu.value?.slot === slot) { menu.value = null; return }
  const b = (ev.currentTarget as HTMLElement).getBoundingClientRect()
  const p = bodyEl.value?.getBoundingClientRect()
  menu.value = { slot, x: b.right - (p?.left ?? 0) - MENU_W, y: b.bottom - (p?.top ?? 0) + 2 }
}

watch(() => !!editing.value, (on) => {
  if (on) document.addEventListener('pointerdown', onEditPointer, true)
  else document.removeEventListener('pointerdown', onEditPointer, true)
})

watch(menu, (m) => {
  if (m) document.addEventListener('pointerdown', onDocPointer, true)
  else document.removeEventListener('pointerdown', onDocPointer, true)
})

function onAddManual(slot: number): void {
  menu.value = null
  cancelEdit()
  if (needsConfirm(rows.value[slot]!)) confirm.value = { kind: 'replace', slot }
  else startEdit(slot)
}

function onAddAstrometry(slot: number): void {
  menu.value = null
  cancelEdit()
  if (needsConfirm(rows.value[slot]!)) confirm.value = { kind: 'replace', slot, via: 'astrometry' }
  else openAstrometry(slot)
}

function openAstrometry(slot: number): void {
  astroSlot.value = slot
  astroStatus.value = null
  astroOpen.value = true
}

/** RUN in the Astrometry dialog: the gateway relay; 202 -> the row is pending and the pill follows the job. */
async function onAstroRun(p: AstrometryRunPayload): Promise<void> {
  const slot = p.slot ?? astroSlot.value
  if (slot === null || astroBusy.value) return
  astroBusy.value = true
  astroStatus.value = { kind: 'info', text: 'Sending…' }
  const body: Record<string, unknown> = { image_id: p.image_id, detections: p.detections, epoch_iso: p.epoch_iso, exposure_s: p.exposure_s }
  if (p.meta.focal_mm != null) body.focal_mm = p.meta.focal_mm
  if (p.meta.pixel_um != null) body.pixel_um = p.meta.pixel_um
  try {
    const res = await api.post<{ job_id?: string, state?: AstrometryJob['state'] }>('/api/mount/astrometry/run', body)
    if (!res || typeof res.job_id !== 'string') throw new Error('no job id')
    const start = Date.parse(p.epoch_iso)
    const t = Number.isFinite(start) ? Math.round((start + (p.exposure_s ?? 0) * 500) / 1000) : null
    seq[slot] = (seq[slot] ?? 0) + 1
    converting.value = converting.value.map((c, i) => (i === slot ? false : c))
    setRow(slot, astrometryPending(t, res.job_id))
    astro.start(res.job_id, res.state)
    astroStatus.value = null
    astroOpen.value = false
  }
  catch (e) {
    const err = e as { status?: unknown, detail?: unknown }
    astroStatus.value = { kind: 'error', text: relayErrorText(typeof err.status === 'number' ? err.status : 0, err.detail) }
  }
  astroBusy.value = false
}

// ----- the astrometry job -> rows (works while this modal is closed: it is mounted once with the Tools group)
function applyJob(slot: number, j: AstrometryJob): void {
  seq[slot] = (seq[slot] ?? 0) + 1
  converting.value = converting.value.map((c, i) => (i === slot ? false : c))
  if (j.state === 'done' && j.result) {
    const row = rowFromJobResult(j.job_id, j.result)
    setRow(slot, row)
    void convert(slot, row)
  }
  else setRow(slot, astrometryFailed(rows.value[slot]!.t, failureText(j), j.job_id))
  persistIfClosed()
}

function resolveJob(): void {
  const j = astro.job
  if (!j || isActiveState(j.state)) return
  const slot = rows.value.findIndex(r => r.state === 'pending' && r.jobId === j.job_id)
  if (slot >= 0) applyJob(slot, j)
}

/** The pill's "apply to a row...": the first empty row, else the first row that cannot feed the IOD (failed / pending / partial). */
function applyToFreeRow(): void {
  astro.applyRequested = false
  const j = astro.job
  if (!j || j.state !== 'done' || !j.result || rows.value.some(r => r.jobId === j.job_id)) return
  let slot = rows.value.findIndex(r => r.state === 'empty')
  if (slot < 0) slot = rows.value.findIndex(r => !rowValid(r))
  if (slot < 0) { status.value = { kind: 'error', text: 'All rows are filled: remove one to use the astrometry result' }; return }
  applyJob(slot, j)
}

const jobSlot = computed(() => {
  const id = astro.job?.job_id
  if (!id) return null
  const i = rows.value.findIndex(r => r.jobId === id)
  return i < 0 ? null : i
})
watch(jobSlot, (v) => { astro.row = v }, { immediate: true })
watch(() => astro.job, resolveJob, { immediate: true })
// Retry started a new job for the same submission: the row that held the old job id follows it (pending again)
watch(() => astro.resumed, (r) => {
  if (!r) return
  const slot = rows.value.findIndex(x => x.jobId === r.from)
  if (slot < 0) return
  seq[slot] = (seq[slot] ?? 0) + 1
  converting.value = converting.value.map((c, i) => (i === slot ? false : c))
  setRow(slot, astrometryPending(rows.value[slot]!.t, r.to))
  persistIfClosed()
})
watch(() => astro.applyRequested, (v) => { if (v && open.value) applyToFreeRow() })

function doRemove(slot: number): void {
  seq[slot] = (seq[slot] ?? 0) + 1
  converting.value = converting.value.map((c, i) => (i === slot ? false : c))
  setRow(slot, emptyRow())
}

function onRemove(slot: number): void {
  menu.value = null
  if (!menuState(rows.value[slot]!).remove) return
  cancelEdit()
  if (needsConfirm(rows.value[slot]!)) confirm.value = { kind: 'remove', slot }
  else doRemove(slot)
}

function onConfirmYes(): void {
  const c = confirm.value
  confirm.value = null
  if (!c) return
  if (c.kind === 'replace') { if (c.via === 'astrometry') openAstrometry(c.slot); else startEdit(c.slot) }
  else doRemove(c.slot)
}

// ----- run
let runSeq = 0
async function onRun(): Promise<void> {
  const st = station.value
  const req = st ? buildIodRequest(st.site, rows.value) : null
  if (!canRun.value || !req) return
  const my = ++runSeq
  running.value = true
  status.value = { kind: 'none', text: 'Running…' }
  try {
    const res = await fetch('/api/iod', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      body: JSON.stringify(req),
    })
    const body = await res.json().catch(() => null) as (Record<string, unknown> & { detail?: unknown }) | null
    if (my !== runSeq) return
    const parsed = res.ok ? parseIodResponse(body) : null
    if (parsed) {
      result.value = parsed
      stale.value = false
      status.value = { kind: 'done', text: doneText(parsed) }
    }
    else {
      result.value = null
      stale.value = false
      status.value = { kind: 'error', text: iodErrorText(res.ok ? 0 : res.status, body?.detail) }
    }
  }
  catch {
    if (my !== runSeq) return
    result.value = null
    stale.value = false
    status.value = { kind: 'error', text: ERR_IOD_UNAVAILABLE }
  }
  if (my === runSeq) running.value = false
}

// ----- copy
const timers: Array<ReturnType<typeof setTimeout>> = []
async function copy(which: 'state' | 'tle'): Promise<void> {
  const text = which === 'state' ? stateText.value : tleText.value
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    copied.value = { ...copied.value, [which]: true }
    timers.push(setTimeout(() => { copied.value = { ...copied.value, [which]: false } }, COPIED_MS))
  }
  catch { /* clipboard unavailable: no "Copied" */ }
}

// ----- recall (Q34): saved only on an open -> closed transition
watch(open, (o, was) => {
  menu.value = null
  confirm.value = null
  cancelEdit()
  runSeq++
  if (!o) {
    if (was) setIodMemo({ rows: rows.value, result: result.value, stale: stale.value, status: { ...status.value }, dir: dir.value })
    return
  }
  running.value = false
  converting.value = Array.from({ length: SLOTS }, () => false)
  const memo = getIodMemo()
  rows.value = memo ? memo.rows : emptyRows()
  result.value = memo ? memo.result : null
  stale.value = memo ? memo.stale : false
  status.value = memo ? { ...memo.status } : { kind: 'none', text: '' }
  dir.value = memo ? memo.dir : 'asc'
  // a row whose conversion was cut off by closing: ask H4 again
  rows.value.forEach((r, i) => { if (r.state === 'partial') void convert(i, r) })
  resolveJob()
  if (astro.applyRequested) applyToFreeRow()
}, { immediate: true })

onBeforeUnmount(() => {
  runSeq++
  timers.forEach(clearTimeout)
  document.removeEventListener('pointerdown', onDocPointer, true)
  document.removeEventListener('pointerdown', onEditPointer, true)
})

const rowsLabel = (slot: number): string => `${slot + 1}`
const ariaSort = computed(() => (dir.value === 'desc' ? 'descending' : 'ascending'))
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent class="iod-dialog gap-0 p-0" data-testid="iod-dialog" @escape-key-down="onEscape">
      <div class="iod-head">
        <DialogTitle class="iod-title">
          INITIAL ORBIT DETERMINATION
        </DialogTitle>
        <DialogDescription class="sr-only">
          Estimate an orbit from three angles-only observations
        </DialogDescription>
      </div>

      <div ref="bodyEl" class="iod-body">
        <span class="iod-lbl">Data Entries</span>
        <div class="iod-table" data-testid="iod-table">
          <table>
            <colgroup>
              <col class="iod-c-slot">
              <col class="iod-c-ep">
              <col class="iod-c-ang">
              <col class="iod-c-ang">
              <col class="iod-c-ang">
              <col class="iod-c-ang">
              <col class="iod-c-src">
              <col class="iod-c-menu">
            </colgroup>
            <thead>
              <tr>
                <th />
                <th :aria-sort="ariaSort">
                  <button type="button" class="iod-th" data-testid="iod-sort-epoch" @click="dir = nextEpochDir(dir)">
                    Epoch (UTC){{ epochArrow(dir) }}
                  </button>
                </th>
                <th v-for="f in FIELDS" :key="f.key">
                  {{ f.label }}
                </th>
                <th>Source</th>
                <th />
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="slot in order" :key="slot" class="iod-row" data-testid="iod-row" :data-slot="slot + 1"
                :data-state="rows[slot]!.state" :class="{ 'is-editing': editing?.slot === slot }"
                @focusout="editing?.slot === slot ? onRowFocusOut($event) : undefined"
                @keydown.enter.prevent="editing?.slot === slot ? onEnter() : undefined"
              >
                <td class="iod-slot">
                  {{ rowsLabel(slot) }} <span class="iod-chev">›</span>
                </td>

                <template v-if="confirm?.slot === slot">
                  <td colspan="6" class="iod-confirm" data-testid="iod-confirm">
                    <span>{{ confirmText(confirm.kind, slot) }}</span>
                    <button type="button" class="iod-cbtn iod-cyes" data-testid="iod-confirm-yes" @click="onConfirmYes">
                      Yes
                    </button>
                    <button type="button" class="iod-cbtn" data-testid="iod-confirm-no" @click="confirm = null">
                      No
                    </button>
                  </td>
                </template>

                <template v-else-if="editing?.slot === slot">
                  <td>
                    <span class="iod-dt" :class="{ 'is-invalid': badFields.has('t') }" data-testid="iod-epoch">
                      <DateTimeField v-model="editing.draft.t" label="Epoch (UTC)" />
                    </span>
                  </td>
                  <td v-for="f in FIELDS" :key="f.key">
                    <input
                      class="iod-in" :class="{ 'iod-bad': badFields.has(f.key) }" type="text" inputmode="decimal" autocomplete="off"
                      :data-testid="`iod-${f.key}`" :value="editing.draft[f.key]" :title="badFields.has(f.key) ? (editReason ?? f.tip) : f.tip"
                      :aria-label="f.label" @input="onField(f.key, $event)"
                    >
                  </td>
                  <td>Manual</td>
                </template>

                <template v-else>
                  <td data-testid="iod-epoch">
                    {{ fmtEpoch(rows[slot]!.t) }}
                  </td>
                  <template v-if="rowDisplay(rows[slot]!).kind === 'status'">
                    <td colspan="4" class="iod-pending" data-testid="iod-row-status" :title="rows[slot]!.reason ?? undefined">
                      {{ (rowDisplay(rows[slot]!) as { text: string }).text }}
                    </td>
                  </template>
                  <template v-else-if="rowDisplay(rows[slot]!).kind === 'values'">
                    <td
                      v-for="f in FIELDS" :key="f.key" :class="{ 'iod-dim': isDim(rows[slot]!, f.key) }"
                      :data-testid="`iod-${f.key}`" :data-dim="isDim(rows[slot]!, f.key) ? '1' : '0'"
                    >
                      {{ fmtAngle(rows[slot]![f.key], f.signed && f.key === 'dec') }}
                    </td>
                  </template>
                  <template v-else>
                    <td colspan="4" />
                  </template>
                  <td data-testid="iod-source">
                    {{ rows[slot]!.source ?? '' }}
                  </td>
                </template>

                <td class="iod-c-menu">
                  <button type="button" class="iod-dots" data-testid="iod-menu-btn" title="Row options" aria-label="Row options" @click="openMenu(slot, $event)">
                    ···
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <button
          type="button" class="iod-run" data-testid="iod-run" :class="{ 'is-ready': canRun }" :disabled="!canRun"
          :title="readiness.reason ?? undefined" @click="onRun"
        >
          {{ running ? 'RUNNING…' : 'INITIAL ORBIT DETERMINATION' }}
        </button>
        <p class="iod-status" :class="{ 'is-err': status.kind === 'error' && !stale }" data-testid="iod-status" role="status">
          {{ statusText }}
        </p>
        <p v-if="editReason" class="iod-hint" data-testid="iod-edit-error">
          {{ editReason }}
        </p>

        <div class="iod-sep" />

        <div class="iod-out-head">
          <span class="iod-lbl">P<sub>2</sub> State Vector</span>
        </div>
        <div class="iod-box iod-box-state" :class="{ 'is-stale': stale && result }">
          <pre data-testid="iod-state">{{ stateText }}</pre>
          <button type="button" class="iod-copy" data-testid="iod-copy-state" :disabled="!result" @click="copy('state')">
            {{ copied.state ? 'Copied' : 'Copy' }}
          </button>
        </div>

        <div class="iod-out-head">
          <span class="iod-lbl">Coarse TLE Estimation</span>
          <span v-if="result && result.tle_fit && !result.tle_fit.converged" class="iod-note" data-testid="iod-tle-note">{{ NOT_CONVERGED_TEXT }}</span>
        </div>
        <div class="iod-box iod-box-tle" :class="{ 'is-stale': stale && result }">
          <pre data-testid="iod-tle">{{ tleText }}</pre>
          <button type="button" class="iod-copy" data-testid="iod-copy-tle" :disabled="!result" @click="copy('tle')">
            {{ copied.tle ? 'Copied' : 'Copy' }}
          </button>
        </div>

        <div
          v-if="menu" class="iod-menu" data-testid="iod-menu" role="menu"
          :style="{ left: `${menu.x}px`, top: `${menu.y}px`, width: `${MENU_W}px` }"
        >
          <button type="button" class="iod-mi" data-testid="iod-add-manual" role="menuitem" @click="onAddManual(menu.slot)">
            Add Manual
          </button>
          <button
            type="button" class="iod-mi" data-testid="iod-add-astrometry" role="menuitem" @click="onAddAstrometry(menu.slot)"
          >
            Add Astrometry
          </button>
          <button
            type="button" class="iod-mi" data-testid="iod-edit" role="menuitem" :disabled="!menuState(rows[menu.slot]!).edit"
            @click="onEditEntry(menu.slot)"
          >
            Edit Entry
          </button>
          <button
            type="button" class="iod-mi iod-mi-red" data-testid="iod-remove" role="menuitem" :disabled="!menuState(rows[menu.slot]!).remove"
            @click="onRemove(menu.slot)"
          >
            Remove Entry
          </button>
        </div>
      </div>
    </DialogContent>
  </Dialog>
  <AstrometryDialog v-model:open="astroOpen" :iod-slot="astroSlot" :busy="astroBusy" :run-status="astroStatus" @run="onAstroRun" />
</template>

<style>
.iod-dialog {
  background: var(--page-bg); border: 1px solid var(--control-border); width: 660px; max-width: calc(100vw - 24px);
  max-height: calc(100vh - 24px); display: flex; flex-direction: column; overflow: hidden;
}
.iod-dialog .iod-head { flex: none; padding: 12px 18px; border-bottom: 1px solid var(--control-border); }
.iod-dialog .iod-title { font-size: 16px; font-weight: 700; letter-spacing: 0.02em; text-transform: uppercase; }
.iod-dialog .iod-body {
  position: relative; flex: 1 1 auto; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; padding: 14px 16px 12px;
}
.iod-dialog .iod-lbl { font-size: 15px; font-weight: 600; color: var(--foreground); }
.iod-dialog .iod-lbl sub { font-size: 10px; }
.iod-dialog .iod-table { flex: none; background: color-mix(in srgb, var(--page-bg) 70%, #000); padding: 0 0 4px; }
.iod-dialog table { width: 100%; border-collapse: separate; border-spacing: 0; margin: 0; table-layout: fixed; font-size: 13px; }
.iod-dialog .iod-c-slot { width: 30px; }
.iod-dialog .iod-c-ep { width: 244px; }
.iod-dialog .iod-c-ang { width: 62px; }
.iod-dialog .iod-c-menu { width: 34px; }
.iod-dialog thead th {
  text-align: left; padding: 8px 0 6px; font-size: 13px; font-weight: 600; letter-spacing: 0.02em; white-space: nowrap; border: 0;
  box-shadow: inset 0 -1px 0 var(--control-border);
}
.iod-dialog thead th:first-child { padding-left: 8px; }
.iod-dialog .iod-th { padding: 0; font: inherit; color: inherit; background: none; border: 0; cursor: pointer; white-space: nowrap; }
.iod-dialog td {
  padding: 0 4px 0 0; height: 30px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-variant-numeric: tabular-nums;
}
.iod-dialog .iod-slot { padding-left: 8px; font-weight: 700; }
.iod-dialog .iod-chev { font-size: 10px; font-weight: 400; }
.iod-dialog .iod-dim { opacity: 0.55; }
.iod-dialog .iod-pending { color: var(--muted-foreground); opacity: 0.8; }
.iod-dialog .iod-dt { display: flex; align-items: center; }
.iod-dialog .iod-dt.is-invalid input { color: var(--danger); border-color: var(--danger); }
.iod-dialog .iod-in {
  box-sizing: border-box; width: 56px; height: 24px; padding: 0 3px; background: var(--field-bg); border: 1px solid var(--control-border);
  border-radius: 0; color: var(--foreground); font-family: inherit; font-size: 13px; font-weight: 600; text-align: left;
}
.iod-dialog .iod-in:focus { outline: none; border-color: var(--accent-green-border); }
.iod-dialog .iod-in.iod-bad { color: var(--danger); border-color: var(--danger); }
.iod-dialog .iod-row:hover:not(.is-editing) { background: color-mix(in srgb, var(--foreground) 8%, transparent); }
.iod-dialog .iod-dots { width: 28px; height: 24px; background: none; border: 0; color: var(--foreground); font-weight: 700; cursor: pointer; letter-spacing: 1px; }
.iod-dialog .iod-dots:hover { color: var(--accent-green-border); }
.iod-dialog .iod-confirm { color: var(--foreground); }
.iod-dialog .iod-confirm > span { margin-right: 10px; font-weight: 600; }
.iod-dialog .iod-cbtn {
  height: 22px; padding: 0 10px; margin-right: 6px; border: 1px solid var(--control-border); background: var(--secondary);
  color: var(--foreground); font-size: 12px; font-weight: 700; cursor: pointer;
}
.iod-dialog .iod-cbtn.iod-cyes { background: var(--accent-green-border); border-color: var(--accent-green-border); color: #fff; }
.iod-dialog .iod-run {
  flex: none; height: 40px; border: 1px solid #7a7a7a; background: #7a7a7a; color: #d0d0d0; font-weight: 700; font-size: 15px;
  letter-spacing: 0.02em; cursor: not-allowed; margin-top: 4px;
}
.iod-dialog .iod-run.is-ready { background: var(--accent-green-border); border-color: var(--accent-green-border); color: #fff; cursor: pointer; }
.iod-dialog .iod-run.is-ready:hover { filter: brightness(1.1); }
.iod-dialog .iod-status { flex: none; margin: 0; min-height: 18px; font-size: 13px; color: var(--muted-foreground); }
.iod-dialog .iod-status.is-err { color: var(--danger); }
.iod-dialog .iod-hint { margin: 0; font-size: 12px; color: var(--danger); }
.iod-dialog .iod-sep { flex: none; border-top: 1px solid var(--control-border); margin: 2px 0 4px; }
.iod-dialog .iod-out-head { display: flex; align-items: baseline; gap: 12px; }
.iod-dialog .iod-note { font-size: 12px; color: var(--muted-foreground); }
.iod-dialog .iod-box {
  position: relative; flex: none; background: color-mix(in srgb, var(--page-bg) 70%, #000); padding: 6px 64px 6px 10px;
}
.iod-dialog .iod-box pre {
  margin: 0; min-height: 18px; font-family: ui-monospace, Consolas, 'Courier New', monospace; font-size: 12px; line-height: 18px;
  white-space: pre-wrap; word-break: break-all; color: var(--foreground);
}
.iod-dialog .iod-box-tle pre { min-height: 36px; }
.iod-dialog .iod-box.is-stale pre { opacity: 0.4; }
.iod-dialog .iod-copy {
  position: absolute; top: 4px; right: 4px; height: 20px; padding: 0 8px; border: 1px solid var(--control-border); background: var(--secondary);
  color: var(--foreground); font-size: 11px; font-weight: 600; cursor: pointer;
}
.iod-dialog .iod-copy:disabled { color: var(--disabled); cursor: not-allowed; }
.iod-dialog .iod-copy:hover:not(:disabled) { border-color: var(--accent-green-border); }
.iod-dialog .iod-menu {
  position: absolute; z-index: 20; background: var(--page-bg); border: 1px solid var(--foreground); padding: 3px; display: flex; flex-direction: column;
}
.iod-dialog .iod-mi { text-align: left; height: 28px; padding: 0 10px; background: none; border: 0; color: var(--foreground); font-size: 13px; font-weight: 600; cursor: pointer; }
.iod-dialog .iod-mi:hover:not(:disabled) { background: var(--secondary); }
.iod-dialog .iod-mi:disabled { color: var(--disabled); cursor: not-allowed; }
.iod-dialog .iod-mi-red { color: var(--danger); }
@media (max-height: 520px) {
  .iod-dialog .iod-head { padding: 8px 18px; }
  .iod-dialog .iod-body { gap: 3px; padding: 8px 16px 6px; }
  .iod-dialog td { height: 26px; }
  .iod-dialog thead th { padding: 5px 0 4px; }
  .iod-dialog .iod-run { height: 32px; margin-top: 2px; }
  .iod-dialog .iod-status { min-height: 16px; }
  .iod-dialog .iod-lbl { font-size: 14px; }
  .iod-dialog .iod-box pre { line-height: 16px; }
  .iod-dialog .iod-box-tle pre { min-height: 32px; }
}
</style>
