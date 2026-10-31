<script setup lang="ts">
// GO TO COORDINATES modal (T3.4a, A5c-iv, concept/Goto Modal.png). Two groups, Local Angular
// (Az, El) and Celestial (RA, Dec J2000); the group last edited is active (green border) and is
// the one SLEW sends. The other group shows the H4 pair computed at "now" (POST /api/transform),
// dimmed, refreshed at 1 Hz. POST /api/mount/check (same verdict as the real command) runs on every
// input change (debounced) and every second: SLEW stays disabled with the gateway's reason when it
// refuses. The modal stays open after SLEW (Q7); the status line follows the slew.
import { useWindowSize } from '@vueuse/core'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import AngleField from '@/components/widgets/AngleField.vue'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { GatewayError, useGatewayApi } from '@/composables/useGatewayApi'
import { useMountStore } from '@/stores/mount'
import type { AngleFormat } from '@/utils/angle-format'
import {
  activateGroup, applyTransformResult, buildGotoCommand, buildTransformRequest, checkBody, editField, elevationInvalid, EL_MAX, EL_MIN,
  gotoStatus, initialState, isRouting, observeState, parseCheck, quickCheck, startSlewTrack,
  type CheckResult, type GotoField, type GotoGroup, type GotoState, type SlewTrack,
} from '@/utils/goto-modal'

const props = defineProps<{
  /** GO TO is disabled for this reason (not homed / stalled / other window / busy ...); null = allowed. */
  gateReason: string | null
}>()
const open = defineModel<boolean>('open', { default: false })

const store = useMountStore()
const api = useGatewayApi()

const DEBOUNCE_MS = 250
const TICK_MS = 1000

const st = ref<GotoState>(initialState(null))
const azFmt = ref<AngleFormat>('dd')
const elFmt = ref<AngleFormat>('dms')
const raFmt = ref<AngleFormat>('hms')
const decFmt = ref<AngleFormat>('dms')

const check = ref<CheckResult>({ kind: 'pending' })
const ackRefusal = ref<string | null>(null)
const sending = ref(false)
const slew = ref<SlewTrack | null>(null)

const quick = computed(() => quickCheck(st.value))
const status = computed(() => gotoStatus({
  gateReason: props.gateReason,
  quick: quick.value,
  ackRefusal: ackRefusal.value,
  sending: sending.value,
  slew: slew.value,
  state: store.snapshot?.state,
  routing: isRouting(store.snapshot as Parameters<typeof isRouting>[0]),
  check: checkShown.value,
  now: store.now,
}))
const redEl = computed(() => elevationInvalid(checkShown.value))
const localActive = computed(() => st.value.active === 'local')
const elRange = computed<[number, number]>(() => (localActive.value ? [EL_MIN, EL_MAX] : [-90, 90]))

// ----- scheduling. The check (fast, decides SLEW) and the transform (the dimmed group, may be slow)
// are independent requests: the check never waits for the transform. Each has its own sequence
// number: a newer request supersedes (its answer wins, older ones are dropped) and the 1 Hz tick
// skips a request kind only while that kind is still in flight. `running*` is reset only by the
// request that owns it, so a superseded request can never wedge the tick.
let checkSeq = 0
let checkRunning = 0
let trSeq = 0
let trRunning = 0
let debounceTimer: ReturnType<typeof setTimeout> | null = null
let tickTimer: ReturnType<typeof setInterval> | null = null

/** Identity of what a check answers: the answer only counts while the inputs still match. */
const keyOf = (s: GotoState): string => JSON.stringify(checkBody(s))
const checkedKey = ref<string | null>(null)
const checkShown = computed<CheckResult>(() => (checkedKey.value === keyOf(st.value) ? check.value : { kind: 'pending' }))

async function runCheck(): Promise<void> {
  const my = ++checkSeq
  checkRunning = my
  const sent = st.value
  if (quickCheck(sent)) {
    checkRunning = 0
    return
  }
  let res: CheckResult
  try {
    res = parseCheck(await api.post<unknown>('/api/mount/check', checkBody(sent)))
  }
  catch (e) {
    res = e instanceof GatewayError && e.status === 422 ? { kind: 'refused', reason: 'Invalid coordinates' } : { kind: 'unreachable' }
  }
  if (my !== checkSeq) return // superseded by a newer check, or the modal closed
  checkRunning = 0
  check.value = res
  checkedKey.value = keyOf(sent)
}

async function runTransform(): Promise<void> {
  const my = ++trSeq
  trRunning = my
  const sent = st.value
  if (quickCheck(sent)) {
    trRunning = 0
    return
  }
  const req = buildTransformRequest(sent, Date.now())
  let result: unknown = null
  try {
    result = (await api.post<{ result?: unknown }>('/api/transform', req))?.result
  }
  catch { /* the dimmed pair simply keeps its last value */ }
  if (my !== trSeq) return
  trRunning = 0
  // Only while the source pair is still the one that was sent (an edit since makes it stale; the
  // answer is dropped and the pair re-requested at once, so the dimmed group never waits for the tick).
  if (st.value.active !== sent.active || JSON.stringify(buildTransformRequest(st.value, 0).values) !== JSON.stringify(req.values)) {
    if (open.value && !quickCheck(st.value)) void runTransform()
    return
  }
  const next = applyTransformResult(st.value, result)
  if (next) st.value = next
}

function cycle(): void {
  void runCheck()
  void runTransform()
}

function scheduleCycle(): void {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    debounceTimer = null
    cycle()
  }, DEBOUNCE_MS)
}

function stopAll(): void {
  checkSeq++
  trSeq++
  checkRunning = 0
  trRunning = 0
  if (debounceTimer) clearTimeout(debounceTimer)
  if (tickTimer) clearInterval(tickTimer)
  debounceTimer = null
  tickTimer = null
}

watch(open, (o) => {
  stopAll()
  if (!o) return
  const m = (store.snapshot as { mount?: Parameters<typeof initialState>[0] } | null)?.mount
  st.value = initialState(m)
  check.value = { kind: 'pending' }
  checkedKey.value = null
  ackRefusal.value = null
  slew.value = null
  sending.value = false
  typed.clear()
  transientUntil = {}
  cycle()
  tickTimer = setInterval(() => {
    if (!checkRunning) void runCheck()
    if (!trRunning) void runTransform()
  }, TICK_MS)
}, { immediate: true })
onBeforeUnmount(stopAll)

// Follow the slew: remember that the mount reported SLEWING.
watch(() => store.snapshot?.state, (s) => { slew.value = observeState(slew.value, s) })

// ----- user gestures. The widgets also emit update:modelValue for non-edits: blur re-emits the
// display-rounded value, a format change re-emits the converted value, and a sign flip caused by
// a programmatic (computed) value re-emits from both legs. Only an emit that follows a real user
// gesture in that field's row (typing, a key such as an arrow, the wheel, a click on the spinner or
// sign chip) is an edit; a format change (inside .ov-label-group) never is.
const typed = new Set<GotoField>() // typed into, until focus leaves the row (the blur emit comes then)
let transientUntil: Partial<Record<GotoField, number>> = {}
const GESTURE_MS = 150

function mark(field: GotoField, e: Event): void {
  const t = e.target as HTMLElement | null
  if (t?.closest?.('.ov-label-group')) return
  if (e.type === 'input') { typed.add(field); return }
  if (e.type === 'keydown') {
    const k = (e as KeyboardEvent).key
    if (k === 'Tab' || k === 'Shift' || k === 'Control' || k === 'Alt' || k === 'Meta') return
  }
  transientUntil[field] = Date.now() + GESTURE_MS
}
function leave(field: GotoField, e: FocusEvent): void {
  const row = e.currentTarget as HTMLElement | null
  if (row && e.relatedTarget instanceof Node && row.contains(e.relatedTarget)) return
  // The blur emit runs in this same task; forget the typing right after it.
  setTimeout(() => { typed.delete(field) }, 0)
}
const rowEvents = (field: GotoField) => ({
  onInputCapture: (e: Event) => mark(field, e),
  onKeydownCapture: (e: Event) => mark(field, e),
  onWheelCapture: (e: Event) => mark(field, e),
  onClickCapture: (e: Event) => mark(field, e),
  onFocusout: (e: FocusEvent) => leave(field, e),
})

function edit(field: GotoField, v: number): void {
  if (!typed.has(field) && Date.now() > (transientUntil[field] ?? 0)) return // not a user edit
  if (!Number.isFinite(v) || Math.abs(v - st.value[field]) < 1e-12) return // no real change (e.g. a re-emit)
  st.value = editField(st.value, field, v)
  ackRefusal.value = null
  slew.value = null
  scheduleCycle()
}

// A click (pointerdown) anywhere in a group's area - label, padding, format selects, fields -
// makes it the active group at once. No value changes: it only switches which pair is sent and
// which one is computed (the other group then shows the computed pair), and re-checks.
function activate(group: GotoGroup): void {
  if (st.value.active === group) return
  st.value = activateGroup(st.value, group)
  ackRefusal.value = null
  slew.value = null
  if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null }
  cycle()
}

async function onSlew(): Promise<void> {
  if (!status.value.canSlew) return
  const c = buildGotoCommand(st.value)
  const kind = st.value.active
  sending.value = true
  ackRefusal.value = null
  slew.value = null
  try {
    const ack = await store.sendCommand(c.cmd, c.args)
    if (ack.ok) slew.value = observeState(startSlewTrack(kind, Date.now()), store.snapshot?.state)
    else ackRefusal.value = ack.reason ?? 'Command refused'
  }
  finally {
    sending.value = false
  }
}

// ----- sizing: the widgets are fixed-px, so the whole body scales (zoom) to fit the window; 1 at
// 1920x1080 and anything with room, down to 0.5 on very small viewports.
const DESIGN_W = 712
const DESIGN_H = 600
const { width: vw, height: vh } = useWindowSize()
const zoom = computed(() => Math.max(0.5, Math.min(1, (vw.value - 24) / DESIGN_W, (vh.value - 24) / DESIGN_H)))
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent class="goto-dialog gap-0 p-0" data-testid="goto-dialog" :style="{ zoom }">
      <div class="gt-head">
        <DialogTitle class="gt-title">
          GO TO COORDINATES
        </DialogTitle>
        <DialogDescription class="sr-only">
          Slew the mount to local angular or celestial coordinates
        </DialogDescription>
      </div>

      <div class="gt-body">
        <fieldset class="gt-group" :class="{ 'gt-active': st.active === 'local', 'gt-dim': st.active !== 'local' }" data-testid="gt-local" @pointerdown.capture="activate('local')">
          <legend>Local Angular Coordinates</legend>
          <AngleField
            v-model:format="azFmt" :model-value="st.az" data-testid="gt-az" v-bind="rowEvents('az')" kind="angle" label="Azimuth"
            :range="[0, 360]" :step="0.0001" wide row-class="form-row form-row-latlon"
            @update:model-value="(v: number) => edit('az', v)"
          />
          <AngleField
            v-model:format="elFmt" :model-value="st.el" data-testid="gt-el" v-bind="rowEvents('el')" kind="angle" label="Elevation"
            signed :range="elRange" :step="0.0001" wide row-class="form-row form-row-latlon" :invalid="redEl"
            @update:model-value="(v: number) => edit('el', v)"
          />
        </fieldset>

        <fieldset class="gt-group" :class="{ 'gt-active': st.active === 'celestial', 'gt-dim': st.active !== 'celestial' }" data-testid="gt-celestial" @pointerdown.capture="activate('celestial')">
          <legend>Celestial Coordinates</legend>
          <AngleField
            v-model:format="raFmt" :model-value="st.ra" data-testid="gt-ra" v-bind="rowEvents('ra')" kind="ra" label="Right Ascension"
            :formats="['hms', 'dd']" default-format="hms" :step="0.0001" wide row-class="form-row form-row-latlon"
            @update:model-value="(v: number) => edit('ra', v)"
          />
          <AngleField
            v-model:format="decFmt" :model-value="st.dec" data-testid="gt-dec" v-bind="rowEvents('dec')" kind="angle" label="Declination"
            signed :range="[-90, 90]" :step="0.0001" wide row-class="form-row form-row-latlon"
            @update:model-value="(v: number) => edit('dec', v)"
          />
        </fieldset>

        <button type="button" class="gt-slew" data-testid="gt-slew" :disabled="!status.canSlew" @click="onSlew">
          SLEW TO COORDINATES
        </button>
        <p class="gt-status" :class="status.tone === 'danger' ? 'gt-status-err' : ''" data-testid="gt-status" role="status">
          {{ status.text }}
        </p>
      </div>
    </DialogContent>
  </Dialog>
</template>

<style>
/* Row grid: label column | format | 1px separator | value. Same construction as the Settings modal. */
.goto-dialog {
  --gt-label: 150px; --gt-fmt: 60px; --gt-field: 220px; --gt-pad: 22px;
  background: var(--page-bg); border: 1px solid var(--control-border); width: 712px; max-width: none;
}
.goto-dialog .gt-head { padding: 12px 18px; border-bottom: 1px solid var(--control-border); }
.goto-dialog .gt-title { font-size: 16px; font-weight: 700; letter-spacing: 0.02em; text-transform: uppercase; }
.goto-dialog .gt-body { padding: 16px 22px 14px; display: flex; flex-direction: column; gap: 14px; }
.goto-dialog .gt-group { position: relative; border: 2px solid var(--panel-border); margin: 0; min-width: 0; padding: 12px var(--gt-pad) 6px; }
.goto-dialog .gt-group legend { padding: 0 6px; font-size: 16px; font-weight: 500; color: var(--foreground); }
.goto-dialog .gt-group.gt-active { border-color: var(--accent-green-border); }
.goto-dialog .gt-group::before { content: ''; position: absolute; left: calc(var(--gt-pad) + var(--gt-label) + var(--gt-fmt) + 5px); top: 22px; bottom: 14px; width: 1px; background: var(--control-border); z-index: 1; }
.goto-dialog .form-row { gap: 0; margin-bottom: 10px; }
.goto-dialog .ov-label-group-wide { width: calc(var(--gt-label) + var(--gt-fmt) + 2px); flex: 0 0 auto; gap: 0; justify-content: flex-start; }
.goto-dialog .ov-label-group-wide label { width: var(--gt-label) !important; font-size: 15px; font-weight: 700; color: var(--foreground); }
.goto-dialog .ov-label-group-wide .ov-fmt-sel.fmt-select-trigger { width: var(--gt-fmt); flex: 0 0 var(--gt-fmt); }
.goto-dialog .ov-input-group { flex: 0 0 var(--gt-field); width: var(--gt-field); margin-left: 8px; }
/* The inactive group (computed pair): dimmed, still editable. Red boxes stay red (.is-invalid wins). */
.goto-dialog .gt-dim legend, .goto-dialog .gt-dim .ov-label-group-wide label { color: var(--disabled); }
.goto-dialog .gt-dim input, .goto-dialog .gt-dim .fmt-select-trigger { color: var(--disabled); border-color: var(--panel-border); }
.goto-dialog .gt-dim .dms-sign-indicator { color: var(--disabled); }
/* ...but a red (invalid) box stays bright red inside the dimmed group: these out-rank the dim rules above. */
.goto-dialog .gt-dim .is-invalid input, .goto-dialog .gt-dim .is-invalid .ov-dms-box { color: var(--danger); border-color: var(--danger); }
.goto-dialog .gt-dim .is-invalid .dms-sign-indicator { color: var(--danger); }
.goto-dialog .gt-slew {
  height: 44px; border: 1px solid var(--accent-green-border); background: var(--accent-green-border); color: #fff;
  font-weight: 700; font-size: 16px; letter-spacing: 0.02em; cursor: pointer;
}
.goto-dialog .gt-slew:hover:not(:disabled) { filter: brightness(1.1); }
.goto-dialog .gt-slew:disabled { background: #7a7a7a; border-color: #7a7a7a; color: #d0d0d0; cursor: not-allowed; }
.goto-dialog .gt-status { margin: -4px 0 0; min-height: 20px; font-size: 14px; color: var(--foreground); }
.goto-dialog .gt-status-err { color: var(--danger); }
</style>
