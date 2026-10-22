<script setup lang="ts">
// Dev-only harness (unlinked, T3.1a): drives the gateway command path.
// Lock status, Take/Release control, STOP, goto alt/az, goto RA/Dec (J2000), tracking, acks.
// Not part of the product UI; the real controls arrive in T3.2-T3.4.
import { storeToRefs } from 'pinia'
import { useMountStore } from '~/stores/mount'
import type { CommandName } from '~/utils/mount-socket'

const store = useMountStore()
const { snapshot, connection, controlHolder, acks } = storeToRefs(store)

const alt = ref('45')
const az = ref('120')
const raH = ref('3.5')
const decDeg = ref('20')
const busy = ref(false)

const num = (v: string) => (v.trim() === '' ? Number.NaN : Number(v))

async function send(cmd: CommandName, args: Record<string, unknown> = {}) {
  busy.value = cmd !== 'stop' // STOP is never disabled
  try {
    await store.sendCommand(cmd, args)
  }
  finally {
    busy.value = false
  }
}

const state = computed(() => snapshot.value?.state ?? '-')
const reason = computed(() => (snapshot.value as { state_reason?: string | null } | null)?.state_reason ?? '')
const axes = computed(() => (snapshot.value as { axes?: { position_deg: number | null, velocity_deg_s: number | null }[] } | null)?.axes ?? [])
const mountPos = computed(() => (snapshot.value as { mount?: { alt: number | null, az: number | null } } | null)?.mount)
const fmt = (v: number | null | undefined, d = 2) => (typeof v === 'number' ? v.toFixed(d) : '-')
</script>

<template>
  <main class="min-h-screen bg-page p-8 text-sm">
    <h1 class="mb-4 text-xl font-semibold">Mount control (dev)</h1>

    <section class="mb-4 space-y-1">
      <p data-testid="ctl-connection">connection: {{ connection }}</p>
      <p data-testid="ctl-state">state: {{ state }} <span class="text-muted-foreground">{{ reason }}</span></p>
      <p data-testid="ctl-holder">control: {{ controlHolder }}</p>
      <p data-testid="ctl-pos">
        alt {{ fmt(mountPos?.alt) }} / az {{ fmt(mountPos?.az) }} ·
        axis0 {{ fmt(axes[0]?.position_deg) }} ({{ fmt(axes[0]?.velocity_deg_s) }}°/s) ·
        axis1 {{ fmt(axes[1]?.position_deg) }} ({{ fmt(axes[1]?.velocity_deg_s) }}°/s)
      </p>
    </section>

    <section class="mb-4 flex flex-wrap items-center gap-2">
      <button
        data-testid="ctl-take" class="rounded border px-3 py-1"
        :disabled="controlHolder === 'you'" @click="send('take_control')"
      >
        Take control
      </button>
      <button
        data-testid="ctl-release" class="rounded border px-3 py-1"
        :disabled="controlHolder !== 'you'" @click="send('release_control')"
      >
        Release control
      </button>
      <button
        data-testid="ctl-stop"
        class="rounded border border-red-600 bg-red-600 px-4 py-1 font-semibold text-white"
        @click="send('stop')"
      >
        STOP
      </button>
    </section>

    <section class="mb-3 flex flex-wrap items-center gap-2">
      <label>Alt <input v-model="alt" data-testid="ctl-alt" class="w-20 rounded border px-1"></label>
      <label>Az <input v-model="az" data-testid="ctl-az" class="w-20 rounded border px-1"></label>
      <button
        data-testid="ctl-goto-altaz" class="rounded border px-3 py-1" :disabled="busy"
        @click="send('goto_altaz', { alt: num(alt), az: num(az) })"
      >
        Goto alt/az
      </button>
    </section>

    <section class="mb-3 flex flex-wrap items-center gap-2">
      <label>RA (h, J2000) <input v-model="raH" data-testid="ctl-ra" class="w-20 rounded border px-1"></label>
      <label>Dec (°) <input v-model="decDeg" data-testid="ctl-dec" class="w-20 rounded border px-1"></label>
      <button
        data-testid="ctl-goto-radec" class="rounded border px-3 py-1" :disabled="busy"
        @click="send('goto_radec_j2000', { ra_h: num(raH), dec_deg: num(decDeg) })"
      >
        Goto RA/Dec
      </button>
    </section>

    <section class="mb-4 flex items-center gap-2">
      <button
        data-testid="ctl-track-on" class="rounded border px-3 py-1" :disabled="busy"
        @click="send('tracking', { on: true })"
      >
        Tracking on
      </button>
      <button
        data-testid="ctl-track-off" class="rounded border px-3 py-1" :disabled="busy"
        @click="send('tracking', { on: false })"
      >
        Tracking off
      </button>
    </section>

    <h2 class="mb-1 font-semibold">Last acks</h2>
    <ul data-testid="ctl-acks" class="space-y-0.5 font-mono text-xs">
      <li v-for="(a, i) in acks" :key="`${a.id}-${i}`" :data-ok="a.ok">
        {{ a.ok ? 'OK ' : 'REFUSED ' }}{{ a.cmd }}<span v-if="a.reason"> - {{ a.reason }}</span>
      </li>
      <li v-if="!acks.length" class="text-muted-foreground">none yet</li>
    </ul>
  </main>
</template>
