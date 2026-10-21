<script setup lang="ts">
// SKY VIEWER clock (A3): `YYYY-MM-DD | HH : MM : SS`, PC clock at 1 Hz, UTC or site-local.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useMountStore } from '~/stores/mount'
import { formatClock, type ClockMode } from '~/utils/sky-clock'
import type { SkySnapshotLike } from '~/utils/sky-chart'

const STORAGE_KEY = 'pw-mount.sky.clockMode'

const store = useMountStore()
const mode = ref<ClockMode>('utc')
const nowMs = ref(Date.now())
let timer: ReturnType<typeof setInterval> | null = null

const tz = computed<string | null>(() => (store.snapshot as SkySnapshotLike | null)?.site?.timezone ?? null)
const t = computed(() => formatClock(nowMs.value, mode.value, tz.value))

function toggle() {
  mode.value = mode.value === 'utc' ? 'local' : 'utc'
  try {
    localStorage.setItem(STORAGE_KEY, mode.value)
  }
  catch { /* storage unavailable: per-session only */ }
}

onMounted(() => {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (v === 'utc' || v === 'local') mode.value = v
  }
  catch { /* ignore */ }
  nowMs.value = Date.now()
  timer = setInterval(() => { nowMs.value = Date.now() }, 1000)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  timer = null
})
</script>

<template>
  <div class="flex shrink-0 flex-col items-center gap-1 pt-2 leading-none" data-testid="sky-clock" :data-mode="mode">
    <div class="flex items-baseline gap-2 text-[20px] tabular-nums">
      <span class="text-foreground/90">{{ t.date }}</span>
      <span class="text-muted-foreground">|</span>
      <span class="font-bold text-foreground">{{ t.hh }} : {{ t.mm }} : {{ t.ss }}</span>
    </div>
    <button
      type="button"
      class="cursor-pointer text-[14px] text-foreground/80 hover:text-foreground"
      :title="mode === 'local' ? (tz ?? 'PC time zone') : 'Click for local time'"
      data-testid="sky-clock-toggle"
      @click="toggle"
    >
      {{ mode === 'utc' ? 'Time UTC' : 'Local Time' }}
    </button>
  </div>
</template>
