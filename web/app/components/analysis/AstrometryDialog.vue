<script setup lang="ts">
// ASTROMETRY FROM IMAGE modal (T5.2c, A2b-iv, concept/Astrometry.png). Left: Epoch (UTC) + Exposure (s) above the
// Data Entries table (T = target, S1..Sn start / end + BRT); right: Image Preview with the toolbar (streak overlay
// toggle, - / zoom % / +, wheel-to-cursor zoom, drag-pan). Focusing a coordinate arms "specify": the next click
// on the image sets that point (the target is snapped by /snap). DETECT STREAKS replaces the table (a confirm
// when it has edits); CLEAR starts over; RUN ASTROMETRY emits `run` once >= 10 streaks + a target + an epoch.
// What RUN does (gateway relay, job pill, IOD row) belongs to T5.2d: this dialog only emits. Image source (T5.2f): Station | Other
// setup; a file whose FITS optics differ from the station's by > 25 % in plate scale switches it to Other on load (the user's
// change wins until the next load).
import { computed, nextTick, ref, watch } from 'vue'
import DateTimeField from '@/components/widgets/DateTimeField.vue'
import SegToggle from '@/components/widgets/SegToggle.vue'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { useGatewayApi } from '~/composables/useGatewayApi'
import { useZoomPane, ZOOM_MAX, ZOOM_MIN, ZOOM_STEP } from '~/composables/useZoomPane'
import {
  ACCEPT_ATTR, addStreak, applySpecify, astrometryErrorText, DETECT_CONFIRM_TEXT, emptyTable, ERR_EXPIRED, ERR_UNSUPPORTED,
  fileAllowed, fmtCell, fromDetections, needsDetectConfirm, overlayShapes, parseEpoch, parseExposure, previewToImage, removeStreak, rowAfterRemove,
  runReadiness, setCell, SOURCE_OPTIONS, specAfterRemove, specFor, specifyLabel, specSame, suggestOther, toDetections, detectSummary, detectTitle,
  type AsTable, type AstrometryRunPayload, type CellKey, type Detection, type ImageInfo, type ImageMeta, type ImageSource, type RowRef, type Spec,
} from '~/utils/astrometry'

const open = defineModel<boolean>('open', { default: false })
const props = withDefaults(defineProps<{
  /** The IOD row (0-based) this dialog was opened for; passed back in the run payload. */
  iodSlot?: number | null
  /** The parent is sending the job: RUN stays disabled. */
  busy?: boolean
  /** A line from the parent (e.g. a 409 / 503 from the relay); shown when nothing else is going on. */
  runStatus?: { kind: 'info' | 'error', text: string } | null
}>(), { iodSlot: null, busy: false, runStatus: null })
const emit = defineEmits<{ run: [payload: AstrometryRunPayload] }>()

const api = useGatewayApi()
const FETCH_TIMEOUT_MS = 120_000
const COLS: Array<{ key: CellKey, label: string }> = [{ key: 'x', label: 'X' }, { key: 'y', label: 'Y' }]

interface LoadedImage { id: string, src: string, pw: number, ph: number, scale: number, dims: [number, number] }

const image = ref<LoadedImage | null>(null)
const meta = ref<ImageMeta | null>(null)
const table = ref<AsTable>(emptyTable())
const epoch = ref<Date | null>(null)
const exposureText = ref('')
const spec = ref<Spec | null>(null)
const sel = ref<RowRef | null>(null)
const status = ref<{ kind: 'info' | 'error', text: string, title?: string }>({ kind: 'info', text: '' })
const loading = ref(false)
const detecting = ref(false)
const confirmDetect = ref(false)
const overlayOn = ref(true)
const paneEl = ref<HTMLElement | null>(null)
const fileEl = ref<HTMLInputElement | null>(null)
const source = ref<ImageSource>('station')

const content = computed(() => (image.value ? { w: image.value.pw, h: image.value.ph } : null))
const zp = useZoomPane(paneEl, content)

const working = computed(() => loading.value || detecting.value)
const readiness = computed(() => runReadiness(table.value, !!image.value, epoch.value))
const canRun = computed(() => readiness.value.ready && !working.value && !props.busy)
const zoomPct = computed(() => `${Math.round(zp.z.value * 100)}%`)
const shown = computed(() => {
  if (spec.value && image.value) return { kind: 'info' as const, text: specifyLabel(spec.value) }
  // a refused RUN (no key, a solve already running, analysis down) must not hide behind the detect summary
  if (props.runStatus?.kind === 'error') return props.runStatus
  if (status.value.text) return status.value
  return props.runStatus ?? status.value
})

const drawn = computed(() => {
  const im = image.value
  if (!im) return null
  const o = overlayShapes(table.value, im.scale)
  return {
    streaks: o.streaks.map(s => ({ ...s, a: zp.project(s.x1, s.y1), b: zp.project(s.x2, s.y2) })),
    target: o.target ? zp.project(o.target.x, o.target.y) : null,
  }
})
const imgStyle = computed(() => {
  const f = zp.fit.value
  return {
    width: `${f.baseW}px`, height: `${f.baseH}px`,
    transform: `translate(${f.offX + zp.tx.value}px, ${f.offY + zp.ty.value}px) scale(${zp.z.value})`,
  }
})

const isSel = (r: RowRef): boolean => !!sel.value && sel.value.kind === r.kind && (r.kind === 'T' || (sel.value.kind === 'S' && sel.value.idx === r.idx))
const isArmed = (r: RowRef): boolean => !!spec.value && spec.value.kind === r.kind && (r.kind === 'T' || (spec.value.kind === 'S' && spec.value.idx === r.idx))
const setStatus = (kind: 'info' | 'error', text: string, title?: string): void => { status.value = { kind, text, title } }

// ----- fetch helpers
async function post(path: string, init: RequestInit): Promise<{ ok: boolean, status: number, body: Record<string, unknown> | null }> {
  try {
    const res = await fetch(path, { method: 'POST', cache: 'no-store', signal: AbortSignal.timeout(FETCH_TIMEOUT_MS), ...init })
    const body = await res.json().catch(() => null) as Record<string, unknown> | null
    return { ok: res.ok, status: res.status, body }
  }
  catch {
    return { ok: false, status: 0, body: null }
  }
}

/** The station's pixel size (Settings), for the auto-suggest only; null = unknown or unreachable (focal length alone is compared). */
async function stationPixelUm(): Promise<number | null> {
  try {
    const d = await api.get<{ hardware?: { camera_pixel_um?: number | null } }>('/api/mount/settings')
    const v = d?.hardware?.camera_pixel_um
    return typeof v === 'number' && v > 0 ? v : null
  }
  catch {
    return null
  }
}

let seq = 0
function expire(): void {
  image.value = null
  meta.value = null
  spec.value = null
  setStatus('error', ERR_EXPIRED)
}

// ----- load an image
function pickFile(): void {
  if (!working.value) fileEl.value?.click()
}

function onFile(ev: Event): void {
  const input = ev.target as HTMLInputElement
  const f = input.files?.[0]
  input.value = ''
  if (f) void loadFile(f)
}

function asImageInfo(b: Record<string, unknown> | null): ImageInfo | null {
  const p = b?.preview as ImageInfo['preview'] | undefined
  const m = b?.meta as ImageMeta | undefined
  if (!b || typeof b.image_id !== 'string' || !p || typeof p.png_b64 !== 'string' || !m || !Array.isArray(m.dims)) return null
  if (!(p.width > 0 && p.height > 0)) return null
  return { image_id: b.image_id, preview: p, meta: m }
}

async function loadFile(file: File): Promise<void> {
  if (!fileAllowed(file.name)) { setStatus('error', ERR_UNSUPPORTED); return }
  const my = ++seq
  loading.value = true
  setStatus('info', 'Loading image…')
  const fd = new FormData()
  fd.append('file', file)
  const r = await post('/api/astrometry/image', { body: fd })
  if (my !== seq) return
  loading.value = false
  const info = r.ok ? asImageInfo(r.body) : null
  if (!info) {
    setStatus('error', astrometryErrorText(r.ok ? 500 : r.status, r.body?.detail))
    return
  }
  const p = info.preview
  image.value = {
    id: info.image_id, pw: p.width, ph: p.height, scale: p.scale > 0 ? p.scale : 1, dims: [info.meta.dims[0], info.meta.dims[1]],
    src: p.png_b64.startsWith('data:') ? p.png_b64 : `data:image/png;base64,${p.png_b64}`,
  }
  meta.value = info.meta
  table.value = emptyTable()
  spec.value = null
  sel.value = null
  confirmDetect.value = false
  epoch.value = parseEpoch(info.meta.epoch_iso)
  const note = info.meta.focal_mm != null || info.meta.pixel_um != null ? suggestOther(info.meta, await stationPixelUm()) : null
  if (my !== seq) return
  source.value = note ? 'other' : 'station'
  exposureText.value = typeof info.meta.exposure_s === 'number' ? String(info.meta.exposure_s) : ''
  zp.reset()
  setStatus('info', [note, ...(info.meta.warnings ?? [])].filter(Boolean).join(' '))
  void nextTick(zp.measure)
}

// ----- detect
function onDetect(): void {
  if (!image.value || working.value) return
  if (needsDetectConfirm(table.value)) confirmDetect.value = true
  else void runDetect()
}

async function runDetect(): Promise<void> {
  confirmDetect.value = false
  const im = image.value
  if (!im || working.value) return
  const my = ++seq
  detecting.value = true
  setStatus('info', 'Detecting streaks…')
  const r = await post('/api/astrometry/detect', { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image_id: im.id }) })
  if (my !== seq) return
  detecting.value = false
  if (r.status === 404) { expire(); return }
  const dets = r.ok && Array.isArray(r.body?.detections) ? [...(r.body!.detections as Detection[])] : null
  if (!dets) { setStatus('error', astrometryErrorText(r.ok ? 500 : r.status, r.body?.detail)); return }
  const t = r.body?.target as Detection | null | undefined
  if (t && typeof t === 'object' && !dets.some(d => d.role === 'target')) dets.push({ ...t, role: 'target' })
  table.value = fromDetections(dets)
  spec.value = null
  sel.value = null
  const w = Array.isArray(r.body?.warnings) ? (r.body!.warnings as string[]) : []
  setStatus('info', detectSummary(table.value.streaks.length, table.value.target.x !== null, w), detectTitle(w))
}

// ----- clear
function onClear(): void {
  seq++
  snapSeq++
  loading.value = false
  detecting.value = false
  image.value = null
  meta.value = null
  table.value = emptyTable()
  epoch.value = null
  exposureText.value = ''
  source.value = 'station'
  spec.value = null
  sel.value = null
  confirmDetect.value = false
  setStatus('info', '')
  zp.reset()
}

// ----- table edits
let snapSeq = 0

function onCell(row: RowRef, key: CellKey, ev: Event): void {
  const el = ev.target as HTMLInputElement
  if (row.kind === 'T') snapSeq++
  table.value = setCell(table.value, row, key, el.value)
  const v = row.kind === 'T' ? table.value.target[key as 'x' | 'y'] : table.value.streaks[row.idx]?.[key as 'xs' | 'brt']
  el.value = key === 'brt' ? String(v ?? '') : fmtCell((v ?? null) as number | null)
}

function arm(row: RowRef, key: CellKey): void {
  sel.value = row
  const s = specFor(row, key)
  if (!s) { spec.value = null; return }
  spec.value = specSame(spec.value, s) ? spec.value : s
}

function onAdd(): void {
  table.value = addStreak(table.value)
  spec.value = null
  const idx = table.value.streaks.length - 1
  sel.value = { kind: 'S', idx }
  void nextTick(() => {
    const rows = document.body.querySelectorAll('[data-testid="as-row"][data-kind="S"]')
    const r = rows[idx] as HTMLElement | undefined
    r?.scrollIntoView?.({ block: 'nearest' })
  })
}

function onRemove(idx: number): void {
  table.value = removeStreak(table.value, idx)
  spec.value = specAfterRemove(spec.value, idx)
  sel.value = rowAfterRemove(sel.value, idx)
}

// ----- click the image to place the armed point
async function onStageClick(ev: MouseEvent): Promise<void> {
  if (zp.consumeDrag()) return
  const im = image.value
  const sp = spec.value
  if (!im || !sp || !paneEl.value) return
  const r = paneEl.value.getBoundingClientRect()
  const { cx, cy } = zp.unproject(ev.clientX - r.left, ev.clientY - r.top)
  const ix = previewToImage(cx, im.scale)
  const iy = previewToImage(cy, im.scale)
  if (ix < 0 || iy < 0 || ix > im.dims[0] || iy > im.dims[1]) return // the letterbox is not the image
  table.value = applySpecify(table.value, sp, ix, iy, im.dims)
  spec.value = null
  if (sp.kind === 'T') await snapTarget(im.id, Math.round(ix), Math.round(iy))
}

async function snapTarget(id: string, x: number, y: number): Promise<void> {
  const my = ++snapSeq
  const r = await post('/api/astrometry/snap', { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image_id: id, x, y, role: 'target' }) })
  if (my !== snapSeq || image.value?.id !== id) return
  if (r.status === 404) { expire(); return }
  const sx = r.body?.x
  const sy = r.body?.y
  if (r.ok && typeof sx === 'number' && typeof sy === 'number' && Number.isFinite(sx) && Number.isFinite(sy)) {
    table.value = { ...table.value, target: { x: Math.round(sx), y: Math.round(sy) } } // the clicked point stays when the snap fails
  }
}

function onBodyPointer(ev: Event): void {
  if (!spec.value) return
  const t = ev.target as HTMLElement | null
  if (t?.closest?.('.as-stage, .as-tools, input.as-co')) return
  spec.value = null
}

function onEscape(ev: Event): void {
  if (spec.value || confirmDetect.value) {
    ev.preventDefault() // the first Esc only backs out of the specify / confirm
    if (confirmDetect.value) confirmDetect.value = false
    else spec.value = null
  }
}

// ----- run
function onRun(): void {
  const im = image.value
  const m = meta.value
  const ep = epoch.value
  if (!canRun.value || !im || !m || !ep) return
  emit('run', {
    image_id: im.id,
    detections: toDetections(table.value),
    epoch_iso: ep.toISOString(),
    exposure_s: parseExposure(exposureText.value),
    meta: m,
    slot: props.iodSlot,
    source: source.value,
  })
}

watch(open, (o) => {
  spec.value = null
  confirmDetect.value = false
  if (o) void nextTick(zp.measure)
})
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent class="as-dialog gap-0 p-0" data-testid="as-dialog" @escape-key-down="onEscape">
      <div class="as-head">
        <DialogTitle class="as-title">
          ASTROMETRY FROM IMAGE
        </DialogTitle>
        <DialogDescription class="sr-only">
          Detect the star streaks and the target in a frame and solve it on astrometry.net
        </DialogDescription>
      </div>

      <div class="as-body" @pointerdown="onBodyPointer">
        <div class="as-fields">
          <label class="as-flbl">Epoch (UTC)</label>
          <span class="as-epoch" data-testid="as-epoch">
            <DateTimeField v-model="epoch" label="Epoch (UTC)" />
          </span>
          <label class="as-flbl" for="as-exp">Exposure (s)</label>
          <input
            id="as-exp" v-model="exposureText" class="as-exp" data-testid="as-exposure" type="text" inputmode="decimal" autocomplete="off"
            aria-label="Exposure (s)"
          >
          <label class="as-flbl as-src-lbl">Image source</label>
          <SegToggle v-model="source" :options="SOURCE_OPTIONS" seg-class="as-seg" data-testid="as-source" />
        </div>
        <div class="as-main">
          <div class="as-left">
            <span class="as-lbl">Data Entries</span>
            <div class="as-table" data-testid="as-table">
              <table>
                <colgroup>
                  <col class="as-c-id">
                  <col class="as-c-se">
                  <col class="as-c-co">
                  <col class="as-c-co">
                  <col class="as-c-brt">
                  <col class="as-c-del">
                </colgroup>
                <thead>
                  <tr>
                    <th /><th />
                    <th v-for="c in COLS" :key="c.key">
                      {{ c.label }}
                    </th>
                    <th>BRT</th>
                    <th />
                  </tr>
                </thead>
                <tbody
                  class="as-row" :class="{ 'is-sel': isSel({ kind: 'T' }), 'is-armed': isArmed({ kind: 'T' }) }" data-testid="as-row" data-kind="T"
                  data-idx="-1" @click="sel = { kind: 'T' }"
                >
                  <tr>
                    <td class="as-id">
                      T
                    </td>
                    <td />
                    <td v-for="c in COLS" :key="c.key">
                      <input
                        class="as-co" :data-testid="`as-${c.key}`" type="text" inputmode="decimal" autocomplete="off" :aria-label="`Target ${c.label}`"
                        :value="fmtCell(table.target[c.key as 'x' | 'y'])" @focus="arm({ kind: 'T' }, c.key)" @change="onCell({ kind: 'T' }, c.key, $event)"
                      >
                    </td>
                    <td colspan="2" />
                  </tr>
                </tbody>
                <tbody
                  v-for="(s, i) in table.streaks" :key="s.uid" class="as-row"
                  :class="{ 'is-sel': isSel({ kind: 'S', idx: i }), 'is-armed': isArmed({ kind: 'S', idx: i }) }" data-testid="as-row" data-kind="S"
                  :data-idx="i" @click="sel = { kind: 'S', idx: i }"
                >
                  <tr>
                    <td class="as-id" rowspan="2">
                      S{{ i + 1 }}
                    </td>
                    <td class="as-se">
                      S
                    </td>
                    <td>
                      <input
                        class="as-co" data-testid="as-x" data-part="start" type="text" inputmode="decimal" autocomplete="off" :aria-label="`S${i + 1} start X`"
                        :value="fmtCell(s.xs)" @focus="arm({ kind: 'S', idx: i }, 'xs')" @change="onCell({ kind: 'S', idx: i }, 'xs', $event)"
                      >
                    </td>
                    <td>
                      <input
                        class="as-co" data-testid="as-y" data-part="start" type="text" inputmode="decimal" autocomplete="off" :aria-label="`S${i + 1} start Y`"
                        :value="fmtCell(s.ys)" @focus="arm({ kind: 'S', idx: i }, 'ys')" @change="onCell({ kind: 'S', idx: i }, 'ys', $event)"
                      >
                    </td>
                    <td rowspan="2" class="as-brt">
                      <input
                        class="as-co as-brt-in" data-testid="as-brt" type="text" inputmode="numeric" autocomplete="off" :aria-label="`S${i + 1} brightness`"
                        :value="s.brt" @focus="arm({ kind: 'S', idx: i }, 'brt')" @change="onCell({ kind: 'S', idx: i }, 'brt', $event)"
                      >
                    </td>
                    <td rowspan="2" class="as-del">
                      <button type="button" class="as-x" data-testid="as-remove" :title="`Remove S${i + 1}`" :aria-label="`Remove S${i + 1}`" @click.stop="onRemove(i)">
                        ✕
                      </button>
                    </td>
                  </tr>
                  <tr>
                    <td class="as-se">
                      E
                    </td>
                    <td>
                      <input
                        class="as-co" data-testid="as-x" data-part="end" type="text" inputmode="decimal" autocomplete="off" :aria-label="`S${i + 1} end X`"
                        :value="fmtCell(s.xe)" @focus="arm({ kind: 'S', idx: i }, 'xe')" @change="onCell({ kind: 'S', idx: i }, 'xe', $event)"
                      >
                    </td>
                    <td>
                      <input
                        class="as-co" data-testid="as-y" data-part="end" type="text" inputmode="decimal" autocomplete="off" :aria-label="`S${i + 1} end Y`"
                        :value="fmtCell(s.ye)" @focus="arm({ kind: 'S', idx: i }, 'ye')" @change="onCell({ kind: 'S', idx: i }, 'ye', $event)"
                      >
                    </td>
                  </tr>
                </tbody>
              </table>
              <button type="button" class="as-add" data-testid="as-add" @click="onAdd">
                + add streak
              </button>
            </div>
          </div>

          <div class="as-right">
            <div class="as-rhead">
              <span class="as-lbl">Image Preview</span>
              <div class="as-tools">
                <button
                  type="button" class="as-tbtn as-tog" :class="{ 'is-off': !overlayOn }" data-testid="as-overlay-toggle" :aria-pressed="overlayOn"
                  title="Show / hide the streak overlay" aria-label="Streak overlay" @click="overlayOn = !overlayOn"
                >
                  <svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true"><path d="M1 9 L6 2 M4 12 L10 3 M8 13 L13 6" stroke="currentColor" stroke-width="1.3" fill="none" /></svg>
                </button>
                <span class="as-zoom">
                  <button
                    type="button" class="as-tbtn" data-testid="as-zoom-out" aria-label="Zoom out" :disabled="!image || zp.z.value <= ZOOM_MIN"
                    @click="zp.zoomBy(1 / ZOOM_STEP)"
                  >−</button>
                  <span class="as-pct" data-testid="as-zoom-pct">{{ zoomPct }}</span>
                  <button
                    type="button" class="as-tbtn" data-testid="as-zoom-in" aria-label="Zoom in" :disabled="!image || zp.z.value >= ZOOM_MAX"
                    @click="zp.zoomBy(ZOOM_STEP)"
                  >+</button>
                </span>
              </div>
            </div>
            <div
              ref="paneEl" class="as-stage" :class="{ 'is-armed': !!spec && !!image, 'is-pan': zp.z.value > 1, 'is-panning': zp.dragging.value }"
              data-testid="as-stage" @wheel.prevent="zp.onWheel" @mousedown="zp.onDown" @click="onStageClick"
            >
              <button v-if="!image" type="button" class="as-placeholder" data-testid="as-placeholder" :disabled="working" @click.stop="pickFile">
                {{ loading ? 'loading…' : 'select image' }}
              </button>
              <template v-else>
                <img class="as-img" :src="image.src" :style="imgStyle" alt="Image preview" draggable="false">
                <svg v-if="overlayOn && drawn" class="as-ov" data-testid="as-overlay" aria-hidden="true">
                  <g v-for="s in drawn.streaks" :key="s.idx" :class="{ 'is-sel': isSel({ kind: 'S', idx: s.idx }) }" class="as-streak">
                    <line :x1="s.a.x" :y1="s.a.y" :x2="s.b.x" :y2="s.b.y" :stroke-width="1 + s.brt * 0.4" />
                    <text :x="s.a.x - 5" :y="s.a.y - 4" text-anchor="end">S</text>
                    <text :x="s.b.x + 5" :y="s.b.y - 4">E</text>
                    <text v-if="isSel({ kind: 'S', idx: s.idx })" class="as-idl" :x="(s.a.x + s.b.x) / 2" :y="(s.a.y + s.b.y) / 2 - 8" text-anchor="middle">S{{ s.idx + 1 }}</text>
                  </g>
                  <g v-if="drawn.target" class="as-target" :class="{ 'is-sel': isSel({ kind: 'T' }) }">
                    <circle :cx="drawn.target.x" :cy="drawn.target.y" r="9" />
                    <text :x="drawn.target.x + 12" :y="drawn.target.y - 8">T</text>
                  </g>
                </svg>
              </template>
            </div>
          </div>
        </div>
        <input ref="fileEl" class="sr-only" type="file" :accept="ACCEPT_ATTR" data-testid="as-file" tabindex="-1" @change="onFile">

        <p class="as-status" :class="{ 'is-err': shown.kind === 'error' }" data-testid="as-status" role="status" :title="(shown as { title?: string }).title || undefined">
          <template v-if="confirmDetect">
            <span data-testid="as-confirm">{{ DETECT_CONFIRM_TEXT }}</span>
            <button type="button" class="as-cbtn as-cyes" data-testid="as-confirm-yes" @click="runDetect">Yes</button>
            <button type="button" class="as-cbtn" data-testid="as-confirm-no" @click="confirmDetect = false">No</button>
          </template>
          <span v-else class="as-stext">{{ shown.text }}</span>
        </p>

        <div class="as-foot">
          <button type="button" class="as-btn as-btn-detect" data-testid="as-detect" :disabled="!image || working" @click="onDetect">
            DETECT STREAKS
          </button>
          <span class="as-spacer" />
          <button type="button" class="as-btn as-btn-clear" data-testid="as-clear" @click="onClear">
            CLEAR
          </button>
          <button
            type="button" class="as-btn as-btn-run" data-testid="as-run" :class="{ 'is-ready': canRun }" :disabled="!canRun"
            :title="readiness.reason ?? undefined" @click="onRun"
          >
            RUN ASTROMETRY
          </button>
        </div>
      </div>
    </DialogContent>
  </Dialog>
</template>

<style>
.as-dialog {
  background: var(--page-bg); border: 1px solid var(--control-border); width: 750px; max-width: calc(100vw - 24px);
  max-height: calc(100vh - 24px); display: flex; flex-direction: column; overflow: hidden;
}
.as-dialog .as-head { flex: none; padding: 12px 18px; border-bottom: 1px solid var(--control-border); }
.as-dialog .as-title { font-size: 16px; font-weight: 700; letter-spacing: 0.02em; text-transform: uppercase; }
.as-dialog .as-body { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; gap: 8px; padding: 14px 14px 12px 17px; overflow: hidden; }
.as-dialog .as-main {
  flex: none; display: grid; grid-template-columns: 244px minmax(0, 1fr); gap: 13px; height: clamp(300px, calc(100vh - 247px), 440px);
}
.as-dialog .as-left, .as-dialog .as-right { min-height: 0; display: flex; flex-direction: column; gap: 6px; }
.as-dialog .as-lbl { font-size: 15px; font-weight: 600; color: var(--foreground); line-height: 20px; }
.as-dialog .as-fields { flex: none; display: flex; align-items: center; gap: 8px; }
.as-dialog .as-flbl { font-size: 13px; font-weight: 600; white-space: nowrap; }
.as-dialog .as-epoch { display: flex; align-items: center; min-width: 0; margin-right: 14px; }
.as-dialog .as-exp {
  box-sizing: border-box; width: 72px; height: 24px; padding: 0 4px; background: var(--field-bg); border: 1px solid var(--control-border);
  border-radius: 0; color: var(--foreground); font-family: inherit; font-size: 13px; font-weight: 600;
}
.as-dialog .as-src-lbl { margin-left: auto; }
.as-dialog .as-seg { flex: none; }
.as-dialog .as-seg button { flex: none; width: 74px; }
.as-dialog .as-exp:focus { outline: none; border-color: var(--accent-green-border); }
.as-dialog .as-table {
  flex: 1 1 auto; min-height: 0; overflow-y: auto; background: color-mix(in srgb, var(--page-bg) 70%, #000); padding: 0 0 4px;
}
.as-dialog .as-table table { width: 100%; border-collapse: separate; border-spacing: 0; margin: 0; table-layout: fixed; font-size: 13px; }
.as-dialog .as-c-id { width: 34px; }
.as-dialog .as-c-se { width: 20px; }
.as-dialog .as-c-co { width: 56px; }
.as-dialog .as-c-brt { width: 34px; }
.as-dialog .as-c-del { width: 28px; }
.as-dialog .as-table thead th {
  position: sticky; top: 0; z-index: 1; text-align: left; padding: 8px 0 6px; font-size: 13px; font-weight: 600; letter-spacing: 0.02em;
  background: color-mix(in srgb, var(--page-bg) 70%, #000); box-shadow: inset 0 -1px 0 var(--control-border);
}
.as-dialog .as-table td { padding: 0; height: 18px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.as-dialog .as-row { cursor: default; }
.as-dialog .as-row > tr:first-child > td { padding-top: 6px; }
.as-dialog .as-row > tr:last-child > td { padding-bottom: 6px; }
.as-dialog .as-row.is-sel { background: color-mix(in srgb, var(--foreground) 8%, transparent); }
.as-dialog .as-row.is-armed { box-shadow: inset 2px 0 0 var(--accent-green-border); }
.as-dialog .as-id { padding-left: 8px !important; font-weight: 700; vertical-align: top; }
.as-dialog .as-se { font-weight: 600; }
.as-dialog .as-co {
  box-sizing: border-box; width: 50px; height: 18px; padding: 0 2px; background: transparent; border: 1px solid transparent; border-radius: 0;
  color: var(--foreground); font-family: inherit; font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums;
}
.as-dialog .as-co:hover { border-color: var(--control-border); }
.as-dialog .as-co:focus { outline: none; background: var(--field-bg); border-color: var(--accent-green-border); }
.as-dialog .as-brt-in { width: 28px; text-align: center; }
.as-dialog .as-brt, .as-dialog .as-del { vertical-align: middle; text-align: center; }
.as-dialog .as-x { width: 22px; height: 22px; background: none; border: 0; color: var(--muted-foreground); font-size: 12px; cursor: pointer; }
.as-dialog .as-x:hover { color: var(--danger); }
.as-dialog .as-add {
  display: block; width: 100%; height: 26px; margin-top: 2px; background: none; border: 0; text-align: left; padding: 0 8px;
  color: var(--muted-foreground); font-size: 12px; font-weight: 600; cursor: pointer;
}
.as-dialog .as-add:hover { color: var(--accent-green-border); }
.as-dialog .as-rhead { flex: none; display: flex; align-items: center; justify-content: space-between; min-height: 24px; }
.as-dialog .as-tools { display: flex; align-items: center; gap: 6px; }
.as-dialog .as-zoom { display: inline-flex; align-items: center; border: 1px solid var(--control-border); height: 22px; }
.as-dialog .as-tbtn {
  width: 22px; height: 20px; display: inline-flex; align-items: center; justify-content: center; padding: 0; background: var(--secondary);
  border: 0; color: var(--foreground); font-size: 15px; font-weight: 700; line-height: 1; cursor: pointer;
}
.as-dialog .as-tbtn:disabled { color: var(--disabled); cursor: not-allowed; }
.as-dialog .as-tog { border: 1px solid var(--control-border); height: 22px; }
.as-dialog .as-tog.is-off { color: var(--disabled); }
.as-dialog .as-pct { min-width: 40px; text-align: center; font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; }
.as-dialog .as-stage {
  position: relative; flex: 1 1 auto; min-height: 0; overflow: hidden; background: color-mix(in srgb, var(--page-bg) 70%, #000); user-select: none;
}
.as-dialog .as-stage.is-pan { cursor: grab; }
.as-dialog .as-stage.is-panning { cursor: grabbing; }
.as-dialog .as-stage.is-armed { cursor: crosshair; }
.as-dialog .as-img { position: absolute; left: 0; top: 0; transform-origin: 0 0; max-width: none; image-rendering: auto; }
.as-dialog .as-ov { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; overflow: hidden; }
.as-dialog .as-streak line { stroke: #5ee08a; stroke-linecap: round; opacity: 0.85; }
.as-dialog .as-streak text { fill: #5ee08a; font-size: 10px; font-weight: 700; }
.as-dialog .as-streak.is-sel line { stroke: #ffd43b; opacity: 1; }
.as-dialog .as-streak.is-sel text { fill: #ffd43b; }
.as-dialog .as-target circle { fill: none; stroke: #ff7a3d; stroke-width: 2; }
.as-dialog .as-target text { fill: #ff7a3d; font-size: 11px; font-weight: 700; }
.as-dialog .as-target.is-sel circle { stroke: #ffd43b; }
.as-dialog .as-placeholder {
  position: absolute; inset: 0; width: 100%; height: 100%; background: none; border: 0; color: var(--muted-foreground);
  font-size: 15px; font-weight: 600; cursor: pointer;
}
.as-dialog .as-placeholder:hover:not(:disabled) { color: var(--foreground); }
.as-dialog .as-status { flex: none; margin: 0; min-height: 18px; font-size: 13px; color: var(--muted-foreground); display: flex; align-items: center; gap: 8px; }
.as-dialog .as-status.is-err { color: var(--danger); }
.as-dialog .as-stext { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.as-dialog .as-cbtn {
  height: 22px; padding: 0 10px; border: 1px solid var(--control-border); background: var(--secondary); color: var(--foreground);
  font-size: 12px; font-weight: 700; cursor: pointer;
}
.as-dialog .as-cbtn.as-cyes { background: var(--accent-green-border); border-color: var(--accent-green-border); color: #fff; }
.as-dialog .as-foot { flex: none; display: flex; align-items: center; gap: 8px; }
.as-dialog .as-spacer { flex: 1 1 auto; }
.as-dialog .as-btn {
  height: 30px; padding: 0 14px; border: 1px solid var(--control-border); background: var(--secondary); color: var(--foreground);
  font-weight: 700; font-size: 13px; letter-spacing: 0.02em; cursor: pointer;
}
.as-dialog .as-btn:hover:not(:disabled) { border-color: var(--accent-green-border); }
.as-dialog .as-btn:disabled { color: var(--disabled); cursor: not-allowed; }
.as-dialog .as-btn-run { width: 178px; border-color: #7a7a7a; background: #7a7a7a; color: #d0d0d0; cursor: not-allowed; }
.as-dialog .as-btn-run:disabled { color: #d0d0d0; }
.as-dialog .as-btn-run.is-ready { background: var(--accent-green-border); border-color: var(--accent-green-border); color: #fff; cursor: pointer; }
.as-dialog .as-btn-run.is-ready:hover { filter: brightness(1.1); }
@media (max-height: 520px) {
  .as-dialog .as-head { padding: 8px 18px; }
  .as-dialog .as-body { gap: 4px; padding: 8px 14px 6px 17px; }
  .as-dialog .as-main { height: clamp(220px, calc(100vh - 197px), 440px); }
  .as-dialog .as-status { min-height: 16px; }
}
</style>
