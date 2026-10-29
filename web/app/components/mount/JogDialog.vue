<script setup lang="ts">
// JOG MOUNT modal (T3.4c, A5c-iii, concept/Job Modal.png). JOG: press-and-hold an arrow (keep-alive every
// 100 ms, jog_stop on release / blur / hidden tab / close). STEP: one step per press, serialized. The
// modal stays open; the status line shows the motion, refusals (red) and "Ready". The gateway decides
// everything; this only sends and reflects.
import { useWindowSize } from '@vueuse/core'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useGatewayApi } from '@/composables/useGatewayApi'
import { useMountStore } from '@/stores/mount'
import { parseCheck, type CheckResult } from '@/utils/goto-modal'
import {
  createJogSender, jogGateReason, DEFAULT_SPEED, DEFAULT_STEP, fmtAz, fmtEl, fmtSize, fmtSpeed, isTurning, jogStatus, keyToPad,
  sizeAtEnd, speedAtEnd, stepBlocked, stepSize, stepSpeed, turnCheckNote, WRAP_TITLE, wrapPromptText, wrapPromptVisible, type JogMode, type JogSnapshotLike, type Pad,
} from '@/utils/jog-modal'

const props = defineProps<{
  /** The pad is disabled for this reason (not connected / other window / busy / axes / not homed / stalled); null = usable. */
  gateReason: string | null
}>()
const open = defineModel<boolean>('open', { default: false })

const store = useMountStore()
const api = useGatewayApi()

const mode = ref<JogMode>('jog')
const speed = ref<number>(DEFAULT_SPEED)
const size = ref<number>(DEFAULT_STEP)
const held = ref<Pad | null>(null)
const refusal = ref<string | null>(null)
const pending = ref(false)

const snap = computed(() => store.snapshot as (JogSnapshotLike & { mount?: { az?: number | null, alt?: number | null } }) | null)
const sender = createJogSender(
  (cmd, args) => store.sendCommand(cmd, args),
  (r) => { refusal.value = r },
  (h) => { held.value = h },
)

const gate = computed(() => jogGateReason(props.gateReason, snap.value))
const disabled = computed(() => gate.value !== null)
const turning = computed(() => isTurning(snap.value))
const blocked = computed(() => disabled.value || turning.value || (mode.value === 'step' && stepBlocked(snap.value, pending.value)))
const status = computed(() => jogStatus({
  reason: gate.value, refusal: refusal.value, mode: mode.value, snapshot: snap.value, held: held.value, pending: pending.value,
}))

// T3.4e: wrap-turn prompt. `dismissed` hides it after a refused turn (jog_limit stays); jogging again or a new limit re-arms it.
const dismissed = ref(false)
const promptOpen = computed(() => wrapPromptVisible(snap.value, dismissed.value))
watch(() => !!snap.value?.jog_limit, (has) => { if (!has) dismissed.value = false })

// T3.5f: one dry check of the turn when the prompt appears / re-arms / the limit changes (no polling).
const turnCheck = ref<CheckResult>({ kind: 'pending' })
const turnNote = computed(() => turnCheckNote(turnCheck.value))
const promptText = computed(() => wrapPromptText(snap.value, turnNote.value.detour))
let checkSeq = 0
async function runTurnCheck(): Promise<void> {
  const my = ++checkSeq
  turnCheck.value = { kind: 'pending' }
  let res: CheckResult
  try {
    res = parseCheck(await api.post<unknown>('/api/mount/check', { cmd: 'wrap_turn', args: {} }))
  }
  catch {
    res = { kind: 'unreachable' }
  }
  if (my === checkSeq) turnCheck.value = res
}
watch(
  () => (promptOpen.value && open.value ? JSON.stringify(snap.value?.jog_limit ?? null) : null),
  (key) => {
    if (key === null) { checkSeq++; turnCheck.value = { kind: 'pending' } }
    else void runTurnCheck()
  },
  { immediate: true },
)
async function doTurn(): Promise<void> {
  dismissed.value = true
  const ack = await store.sendCommand('wrap_turn', {})
  if (!ack.ok) refusal.value = ack.reason ?? 'Command refused'
  else refusal.value = null
}
function stayHere(): void {
  dismissed.value = true
  refusal.value = null
  void store.sendCommand('wrap_turn_cancel', {})
}
const isActive = (axis: number, dir: number) => held.value?.axis === axis && held.value?.dir === dir

function releaseAll(): void {
  sender.release()
}

function press(pad: Pad): void {
  if (blocked.value) return
  if (mode.value === 'jog') {
    if (sender.press(pad, speed.value)) { refusal.value = null; dismissed.value = false }
  }
  else void doStep(pad)
}

async function doStep(pad: Pad): Promise<void> {
  pending.value = true
  refusal.value = null
  try {
    const ack = await store.sendCommand('step', { axis: pad.axis, dir: pad.dir, size_arcmin: size.value })
    if (!ack.ok) refusal.value = ack.reason ?? 'Command refused'
  }
  finally {
    pending.value = false
  }
}

function release(pad?: Pad): void {
  if (mode.value === 'jog') sender.release(pad)
}

function onKeyDown(e: KeyboardEvent): void {
  const pad = keyToPad(e.key)
  if (!pad) return
  e.preventDefault()
  if (e.repeat) return
  press(pad)
}
function onKeyUp(e: KeyboardEvent): void {
  const pad = keyToPad(e.key)
  if (!pad) return
  e.preventDefault()
  release(pad)
}

const padEvents = (axis: 0 | 1, dir: -1 | 1) => ({
  onPointerdown: (e: PointerEvent) => { if (e.button === 0 || e.pointerType !== 'mouse') press({ axis, dir }) },
  onPointerup: () => release({ axis, dir }),
  onPointercancel: () => release({ axis, dir }),
  onPointerleave: () => release({ axis, dir }),
})

function setMode(m: JogMode): void {
  if (m === mode.value) return
  releaseAll()
  mode.value = m
  refusal.value = null
}

// Safety: every way the hold can end releases the jog.
function onVisibility(): void {
  if (document.visibilityState === 'hidden') releaseAll()
}
watch(open, (o) => {
  if (typeof window === 'undefined') return
  if (o) {
    refusal.value = null
    pending.value = false
    window.addEventListener('blur', releaseAll)
    document.addEventListener('visibilitychange', onVisibility)
  }
  else {
    releaseAll()
    window.removeEventListener('blur', releaseAll)
    document.removeEventListener('visibilitychange', onVisibility)
  }
}, { immediate: true })
watch(disabled, (d) => { if (d) releaseAll() })
// T3.4e: the gateway ended the jog at the mechanical limit: drop the hold (no more keep-alives
// to be refused) and clear any refusal they caused, so only the limit prompt speaks.
watch(() => !!snap.value?.jog_limit, (has) => { if (has) { releaseAll(); refusal.value = null } })
onBeforeUnmount(() => {
  releaseAll()
  window.removeEventListener('blur', releaseAll)
  document.removeEventListener('visibilitychange', onVisibility)
})

// Sizing like GotoDialog: the body scales (zoom) to fit the window; 1 at 1920x1080.
const DESIGN_W = 560
const DESIGN_H = 560
const { width: vw, height: vh } = useWindowSize()
const zoom = computed(() => Math.max(0.5, Math.min(1, (vw.value - 24) / DESIGN_W, (vh.value - 24) / DESIGN_H)))
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent
      class="jog-dialog gap-0 p-0" data-testid="jog-dialog" :style="{ zoom }"
      @keydown="onKeyDown" @keyup="onKeyUp"
    >
      <div class="jg-head">
        <DialogTitle class="jg-title">
          JOG MOUNT
        </DialogTitle>
        <DialogDescription class="sr-only">
          Move the mount with press-and-hold jog or fixed steps
        </DialogDescription>
      </div>

      <div class="jg-body">
        <div class="jg-toggle" role="group" aria-label="Mode">
          <button type="button" data-testid="jg-mode-jog" :class="{ 'jg-on': mode === 'jog' }" :aria-pressed="mode === 'jog'" @click="setMode('jog')">
            JOG
          </button>
          <button type="button" data-testid="jg-mode-step" :class="{ 'jg-on': mode === 'step' }" :aria-pressed="mode === 'step'" @click="setMode('step')">
            STEP
          </button>
        </div>

        <div class="jg-pad" :class="{ 'jg-disabled': blocked }" data-testid="jg-pad">
          <div class="jg-hline" />
          <div class="jg-left">
            <span class="jg-cap">{{ mode === 'jog' ? 'SPEED (°/s)' : 'STEP' }}</span>
            <div class="jg-stepper">
              <template v-if="mode === 'jog'">
                <button type="button" data-testid="jg-minus" aria-label="Slower" :disabled="speedAtEnd(speed, -1)" @click="speed = stepSpeed(speed, -1)">
                  −
                </button>
                <span data-testid="jg-value">{{ fmtSpeed(speed) }}</span>
                <button type="button" data-testid="jg-plus" aria-label="Faster" :disabled="speedAtEnd(speed, 1)" @click="speed = stepSpeed(speed, 1)">
                  +
                </button>
              </template>
              <template v-else>
                <button type="button" data-testid="jg-minus" aria-label="Smaller step" :disabled="sizeAtEnd(size, -1)" @click="size = stepSize(size, -1)">
                  −
                </button>
                <span data-testid="jg-value">{{ fmtSize(size) }}</span>
                <button type="button" data-testid="jg-plus" aria-label="Larger step" :disabled="sizeAtEnd(size, 1)" @click="size = stepSize(size, 1)">
                  +
                </button>
              </template>
            </div>
          </div>

          <button type="button" class="jg-arrow jg-up" data-testid="jg-up" :class="{ 'jg-held': isActive(1, 1) }" :disabled="blocked" aria-label="Up" v-bind="padEvents(1, 1)">
            <svg viewBox="0 0 24 24"><path d="M12 4 L21 19 H3 Z" /></svg>
          </button>
          <button type="button" class="jg-arrow jg-lft" data-testid="jg-left" :class="{ 'jg-held': isActive(0, -1) }" :disabled="blocked" aria-label="Left" v-bind="padEvents(0, -1)">
            <svg viewBox="0 0 24 24"><path d="M4 12 L19 3 V21 Z" /></svg>
          </button>
          <button type="button" class="jg-arrow jg-rgt" data-testid="jg-right" :class="{ 'jg-held': isActive(0, 1) }" :disabled="blocked" aria-label="Right" v-bind="padEvents(0, 1)">
            <svg viewBox="0 0 24 24"><path d="M20 12 L5 3 V21 Z" /></svg>
          </button>
          <button type="button" class="jg-arrow jg-dwn" data-testid="jg-down" :class="{ 'jg-held': isActive(1, -1) }" :disabled="blocked" aria-label="Down" v-bind="padEvents(1, -1)">
            <svg viewBox="0 0 24 24"><path d="M12 20 L3 5 H21 Z" /></svg>
          </button>

          <div class="jg-right">
            <span class="jg-box" data-testid="jg-az">{{ fmtAz(snap?.mount?.az) }}</span>
            <span class="jg-cap">AZIMUTH (°)</span>
          </div>
          <div class="jg-elev">
            <span class="jg-box" data-testid="jg-el">{{ fmtEl(snap?.mount?.alt) }}</span>
            <span class="jg-cap">ELEVATION (°)</span>
          </div>
        </div>

        <p class="jg-status" :class="status.tone === 'danger' ? 'jg-status-err' : ''" data-testid="jg-status" role="status">
          {{ status.text }}
        </p>
      </div>

    <Dialog :open="open && promptOpen" @update:open="(v: boolean) => { if (!v) stayHere() }">
      <DialogContent :show-close-button="false" data-testid="jg-wrap-dialog">
        <DialogHeader>
          <DialogTitle>{{ WRAP_TITLE }}</DialogTitle>
          <DialogDescription data-testid="jg-wrap-text">
            {{ promptText }}
          </DialogDescription>
          <p
            v-if="turnNote.line" class="text-sm" :class="turnNote.blocked ? 'text-destructive' : 'text-foreground'"
            data-testid="jg-wrap-check" role="status"
          >
            {{ turnNote.line }}
          </p>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" data-testid="jg-wrap-stay" @click="stayHere">
            Stay here
          </Button>
          <Button data-testid="jg-wrap-turn" :disabled="turnNote.blocked" @click="doTurn">
            Turn 360°
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    </DialogContent>
  </Dialog>
</template>

<style>
.jog-dialog { background: var(--page-bg); border: 1px solid var(--control-border); width: 560px; max-width: none; }
.jog-dialog .jg-head { padding: 12px 18px; border-bottom: 1px solid var(--control-border); }
.jog-dialog .jg-title { font-size: 16px; font-weight: 700; letter-spacing: 0.02em; text-transform: uppercase; }
.jog-dialog .jg-body { padding: 22px 22px 14px; display: flex; flex-direction: column; align-items: center; gap: 14px; }
.jog-dialog .jg-toggle { display: grid; grid-template-columns: 1fr 1fr; width: 320px; border: 1px solid var(--control-border); }
.jog-dialog .jg-toggle button { height: 40px; background: transparent; color: var(--foreground); font-weight: 700; font-size: 17px; cursor: pointer; border: 2px solid transparent; }
.jog-dialog .jg-toggle button.jg-on { border-color: var(--accent-green-border); color: var(--accent-green-text); background: var(--secondary); }
/* The pad: a 64px-cell cross; the side boxes sit on the horizontal line. */
.jog-dialog .jg-pad { position: relative; width: 516px; height: 300px; }
.jog-dialog .jg-hline { position: absolute; left: 0; right: 0; top: 108px; height: 2px; background: var(--control-border); }
.jog-dialog .jg-arrow {
  position: absolute; width: 64px; height: 64px; border: 1px solid var(--control-border); background: var(--secondary);
  display: flex; align-items: center; justify-content: center; cursor: pointer; touch-action: none; user-select: none; padding: 0;
}
.jog-dialog .jg-arrow svg { width: 38px; height: 38px; fill: #fff; pointer-events: none; }
.jog-dialog .jg-up { left: 226px; top: 0; }
.jog-dialog .jg-lft { left: 150px; top: 77px; }
.jog-dialog .jg-rgt { left: 302px; top: 77px; }
.jog-dialog .jg-dwn { left: 226px; top: 154px; }
.jog-dialog .jg-arrow:hover:not(:disabled) { filter: brightness(1.2); }
.jog-dialog .jg-arrow.jg-held { border-color: var(--accent-green-border); background: var(--accent-green-border); }
/* Disabled = SOLID dimmed colours (no transparency, so the guide line behind never shows through). */
.jog-dialog .jg-arrow:disabled { cursor: not-allowed; background: color-mix(in srgb, var(--secondary) 55%, var(--page-bg)); border-color: color-mix(in srgb, var(--control-border) 55%, var(--page-bg)); }
.jog-dialog .jg-arrow:disabled svg { fill: var(--disabled); }
.jog-dialog .jg-left { position: absolute; left: 0; top: 90px; width: 140px; display: flex; flex-direction: column; align-items: center; }
.jog-dialog .jg-cap { font-size: 13px; font-weight: 600; color: var(--foreground); text-align: center; white-space: nowrap; }
.jog-dialog .jg-left .jg-cap { background: var(--page-bg); padding: 0 6px; line-height: 18px; position: relative; z-index: 1; }
.jog-dialog .jg-stepper { display: flex; align-items: stretch; border: 1px solid var(--control-border); height: 30px; margin-top: 12px; }
.jog-dialog .jg-stepper span { min-width: 54px; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 17px; border-left: 1px solid var(--control-border); border-right: 1px solid var(--control-border); }
.jog-dialog .jg-stepper button { width: 24px; background: transparent; color: var(--foreground); font-weight: 700; font-size: 16px; cursor: pointer; padding: 0; }
.jog-dialog .jg-stepper button:disabled { color: var(--disabled); cursor: not-allowed; }
.jog-dialog .jg-right { position: absolute; left: 386px; top: 92px; width: 130px; display: flex; flex-direction: column; align-items: center; gap: 4px; }
.jog-dialog .jg-right .jg-box { background: var(--page-bg); position: relative; z-index: 1; }
.jog-dialog .jg-elev { position: absolute; left: 196px; top: 230px; width: 124px; display: flex; flex-direction: column; align-items: center; gap: 4px; }
.jog-dialog .jg-box { border: 1px solid var(--control-border); min-width: 100px; height: 30px; padding: 0 10px; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 17px; font-variant-numeric: tabular-nums; }
.jog-dialog .jg-pad.jg-disabled .jg-box { color: var(--disabled); }
.jog-dialog .jg-status { margin: 0; min-height: 20px; font-size: 14px; color: var(--foreground); text-align: center; }
.jog-dialog .jg-status-err { color: var(--danger); }
</style>
