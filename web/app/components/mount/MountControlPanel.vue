<script setup lang="ts">
// MOUNT CONTROL panel (T3.2, concept/Main Dashboard 1.png): Connection (A5a), Pointing Mode (A5b,
// STOP / TRACK, T3.3) and Commands (A5c: HOME MOUNT live; TRACK TARGET / JOG / GOTO
// later). The gateway decides everything; this panel shows its state, sequence progress and every
// refusal reason (G7) on one status line.
import { computed, ref } from 'vue'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useMountStore } from '~/stores/mount'
import {
  buildControlView, homedDialogSince, needsHomeConfirm, refusalFromAck,
  TIP_GOTO, TIP_JOG, TIP_TRACK_TARGET,
  type ControlSnapshotLike, type Refusal,
} from '~/utils/mount-control'
import type { CommandName } from '~/utils/mount-socket'

// Only one instance (the dashboard) raises the "Mount not homed" dialog; the Details modal embeds
// the same panel without it so the two never stack.
const props = withDefaults(defineProps<{ homedDialog?: boolean }>(), { homedDialog: false })
const store = useMountStore()
const refusal = ref<Refusal | null>(null)
const confirm = ref<'disconnect' | 'no_home' | null>(null)
const homeQuestion = ref('')
const pending = ref(false)

const linkUp = computed(() => store.connection === 'open' && !store.linkStale)
const view = computed(() => buildControlView(store.snapshot as ControlSnapshotLike | null, {
  linkUp: linkUp.value,
  holder: store.controlHolder,
  refusal: refusal.value,
  now: store.now,
}))

async function run(cmd: CommandName, args: Record<string, unknown> = {}) {
  pending.value = true
  try {
    const ack = await store.sendCommand(cmd, args)
    if (needsHomeConfirm(ack)) {
      homeQuestion.value = ack.reason ?? 'Home is blocked; disconnect without homing?'
      confirm.value = 'no_home'
      return ack
    }
    refusal.value = refusalFromAck(ack, Date.now())
    return ack
  }
  finally {
    pending.value = false
  }
}

const onConnect = () => run('connect')
const onHome = () => run('home')
const onStop = () => run('stop')
const onTrack = () => run('tracking', { on: true })
const onOverride = () => run('homed_override')

// "Mount not homed" dialog (Q3): open while a not-yet-dismissed `homed.since` is current.
const homedSince = computed(() => props.homedDialog
  ? homedDialogSince(store.snapshot as ControlSnapshotLike | null, { linkUp: linkUp.value, dismissedSince: store.homedDialogDismissedSince })
  : null)
function dismissHomed() {
  store.homedDialogDismissedSince = homedSince.value ?? store.homedDialogDismissedSince
}
async function homedHome() {
  dismissHomed()
  await onHome()
}
async function homedProceed() {
  dismissHomed()
  await onOverride()
}
const onTakeControl = () => run('take_control')

function onDisconnect() {
  refusal.value = null
  confirm.value = 'disconnect'
}

async function acceptConfirm() {
  const which = confirm.value
  confirm.value = null
  if (which === 'disconnect') await run('disconnect')
  else if (which === 'no_home') await run('disconnect', { skip_home: true })
}

const TONE_CLASS = { normal: 'mc-tone-normal', warn: 'mc-tone-warn', danger: 'mc-tone-danger' } as const
</script>

<template>
  <div class="mc-root" data-testid="mount-control-panel">
    <div class="mc-inner">
      <!-- Connection (A5a) -->
      <div class="mc-group" data-testid="mc-connection">
        <span class="mc-legend">Connection</span>
        <div class="mc-seg">
          <template v-if="view.connected">
            <button
              type="button" class="mc-cell" data-testid="mc-disconnect"
              :disabled="!view.disconnect.enabled || pending" :title="view.disconnect.reason ?? undefined"
              @click="onDisconnect"
            >
              DISCONNECT
            </button>
            <div class="mc-cell mc-active mc-active-green" data-testid="mc-conn-label">
              CONNECTED
            </div>
          </template>
          <template v-else>
            <div class="mc-cell mc-active" data-testid="mc-conn-label">
              DISCONNECTED
            </div>
            <button
              type="button" class="mc-cell" data-testid="mc-connect"
              :disabled="!view.connect.enabled || pending" :title="view.connect.reason ?? undefined"
              @click="onConnect"
            >
              CONNECT
            </button>
          </template>
        </div>
      </div>

      <!-- Pointing Mode (A5b): STOP is live (it also cancels a running sequence); TRACK = T3.3 -->
      <div class="mc-group" data-testid="mc-pointing">
        <span class="mc-legend">Pointing Mode</span>
        <span v-if="view.notHomed" class="mc-chip" data-testid="mc-not-homed-chip" title="Mount not homed: positions may be wrong">NOT HOMED</span>
        <div class="mc-seg">
          <div v-if="view.stopLabel" class="mc-cell mc-active" data-testid="mc-stop-label">
            STOPPED
          </div>
          <button
            v-else type="button" class="mc-cell" data-testid="mc-stop" :disabled="!view.stopEnabled"
            title="Stop the mount (accepted from any window)" @click="onStop"
          >
            STOP
          </button>
          <div v-if="view.trackingLabel" class="mc-cell mc-active mc-active-green" data-testid="mc-tracking-label">
            TRACKING
          </div>
          <button
            v-else type="button" class="mc-cell" data-testid="mc-track" :disabled="!view.track.enabled || pending"
            :title="view.track.reason ?? 'Track the current RA/Dec'" @click="onTrack"
          >
            TRACK
          </button>
        </div>
      </div>

      <!-- Commands (A5c) -->
      <div class="mc-group" data-testid="mc-commands">
        <span class="mc-legend">Commands</span>
        <div class="mc-grid">
          <button
            type="button" data-testid="mc-home" :disabled="!view.home.enabled || pending"
            :title="view.home.reason ?? 'Stop, then home the mount'" @click="onHome"
          >
            HOME MOUNT
          </button>
          <button type="button" data-testid="mc-track-target" disabled :title="TIP_TRACK_TARGET">
            TRACK TARGET
          </button>
          <button type="button" data-testid="mc-jog" disabled :title="TIP_JOG">
            JOG
          </button>
          <button type="button" data-testid="mc-goto" disabled :title="TIP_GOTO">
            GOTO
          </button>
        </div>
      </div>

      <!-- Status line: sequence step / failed step / warning / refusal reason (G7) + control lock -->
      <div class="mc-status" data-testid="mc-status-row">
        <span
          class="mc-line" :class="view.line ? TONE_CLASS[view.line.tone] : ''"
          :title="view.line?.text" data-testid="mc-status-line"
        >{{ view.line?.text ?? '' }}</span>
        <template v-if="view.notHomed && !view.busy && !view.takeControl">
          <button type="button" class="mc-take" data-testid="mc-inline-home" :disabled="!view.home.enabled || pending" @click="onHome">
            HOME
          </button>
          <button type="button" class="mc-take" data-testid="mc-inline-override" :disabled="pending" @click="onOverride">
            PROCEED
          </button>
        </template>
        <button
          v-if="view.takeControl" type="button" class="mc-take" data-testid="mc-take-control"
          :disabled="pending" @click="onTakeControl"
        >
          TAKE CONTROL
        </button>
      </div>
    </div>

    <Dialog :open="homedSince !== null" @update:open="(v: boolean) => { if (!v) dismissHomed() }">
      <DialogContent :show-close-button="false" data-testid="mc-homed-dialog">
        <DialogHeader>
          <DialogTitle>Mount not homed</DialogTitle>
          <DialogDescription>Mount not homed — positions may be wrong.</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" data-testid="mc-homed-proceed" @click="homedProceed">
            Proceed anyway
          </Button>
          <Button data-testid="mc-homed-home" @click="homedHome">
            Home mount
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog :open="confirm !== null" @update:open="(v: boolean) => { if (!v) confirm = null }">
      <DialogContent :show-close-button="false" data-testid="mc-confirm-dialog">
        <DialogHeader>
          <DialogTitle>{{ confirm === 'no_home' ? 'Home is blocked' : 'Disconnect the mount?' }}</DialogTitle>
          <DialogDescription data-testid="mc-confirm-text">
            <template v-if="confirm === 'no_home'">
              {{ homeQuestion }}
            </template>
            <template v-else>
              The mount is homed, both axes are disabled and the mount is disconnected. PWI4 stays open.
            </template>
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" data-testid="mc-confirm-no" @click="confirm = null">
            {{ confirm === 'no_home' ? 'NO' : 'CANCEL' }}
          </Button>
          <Button data-testid="mc-confirm-yes" @click="acceptConfirm">
            {{ confirm === 'no_home' ? 'YES' : 'DISCONNECT' }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>

<style scoped>
/* Sized by the panel body height (same approach as FILTERS): --u = body height / the 1920x1080
   design height (364 px of content + 1 px borders; 380 leaves the slack). Every dimension is clamp(floor, u*N, N), so N is what you
   get at 1920x1080 (u >= 1) and the floors keep it readable and inside the panel on small screens. */
.mc-root { container-type: size; height: 100%; min-height: 0; }
.mc-inner {
  --u: calc(100cqh / 380);
  height: 100%; display: flex; flex-direction: column;
  gap: clamp(8px, calc(var(--u) * 24), 24px); padding-top: clamp(6px, calc(var(--u) * 8), 8px);
}
.mc-group {
  position: relative; flex: none; border: 1px solid var(--panel-border);
  padding: clamp(4px, calc(var(--u) * 15), 15px) clamp(8px, calc(var(--u) * 18), 18px);
}
.mc-legend {
  position: absolute; left: clamp(8px, calc(var(--u) * 18), 18px); top: 0; transform: translateY(-52%);
  padding: 0 6px; background: var(--page-bg); color: var(--label-slate); font-weight: 600; line-height: 1;
  font-size: clamp(11px, calc(var(--u) * 16), 16px);
}
.mc-seg { display: grid; grid-template-columns: 1fr 1fr; }
.mc-cell, .mc-grid button {
  display: flex; align-items: center; justify-content: center; min-width: 0; background: transparent;
  color: var(--foreground); font-weight: 700; letter-spacing: 0.02em; white-space: nowrap; cursor: pointer;
  height: clamp(19px, calc(var(--u) * 38), 38px); font-size: clamp(11px, calc(var(--u) * 16), 16px);
}
.mc-cell { border: 1px solid var(--control-border); }
.mc-cell + .mc-cell { margin-left: -1px; }
button.mc-cell:hover:not(:disabled), .mc-grid button:hover:not(:disabled) { background: var(--secondary); }
.mc-cell:disabled, .mc-grid button:disabled { color: var(--disabled); cursor: not-allowed; }
/* The state label of a segment (not a button): outlined, never clickable. */
.mc-active { position: relative; z-index: 1; cursor: default; border-color: var(--foreground); box-shadow: inset 0 0 0 1px var(--foreground); }
.mc-active-green { color: var(--accent-green-text); border-color: var(--accent-green-border); box-shadow: inset 0 0 0 1px var(--accent-green-border); }
.mc-grid { display: grid; grid-template-columns: 1fr 1fr; border: 1px solid var(--control-border); }
.mc-grid button { border: 0; border-right: 1px solid var(--control-border); border-bottom: 1px solid var(--control-border); }
.mc-grid button:nth-child(2n) { border-right: 0; }
.mc-grid button:nth-last-child(-n + 2) { border-bottom: 0; }
.mc-status {
  flex: none; margin-top: auto; display: flex; align-items: center; gap: 8px;
  height: clamp(14px, calc(var(--u) * 34), 34px); font-size: clamp(11px, calc(var(--u) * 14), 14px); line-height: 1.2;
}
.mc-line {
  flex: 1; min-width: 0; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden;
}
@container (max-height: 230px) { .mc-line { -webkit-line-clamp: 1; } }
/* Page minimum height (480 px): the panel body is only ~168 px, tighten the spacing further. */
@container (max-height: 200px) {
  .mc-inner { gap: 7px; }
  .mc-group { padding-top: 3px; padding-bottom: 3px; }
  .mc-status { height: 14px; }
}
.mc-chip {
  position: absolute; right: clamp(8px, calc(var(--u) * 18), 18px); top: 0; transform: translateY(-52%);
  padding: 0 6px; background: var(--page-bg); color: #e0a63c; font-weight: 700; line-height: 1;
  font-size: clamp(10px, calc(var(--u) * 13), 13px); letter-spacing: 0.04em;
}
.mc-tone-normal { color: var(--foreground); }
.mc-tone-warn { color: #e0a63c; }
.mc-tone-danger { color: var(--danger); }
.mc-take {
  flex: none; border: 1px solid var(--control-border); background: transparent; color: var(--foreground); font-weight: 700;
  padding: 0 8px; height: clamp(16px, calc(var(--u) * 24), 24px); font-size: clamp(10px, calc(var(--u) * 12), 12px); cursor: pointer;
}
.mc-take:hover:not(:disabled) { background: var(--secondary); border-color: var(--accent-green-border); }
</style>
