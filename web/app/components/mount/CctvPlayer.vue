<script setup lang="ts">
// Mount CCTV player (T5.7): the camera feed through the gateway, fitted to its box
// (object-fit: contain). MJPEG = <img>, HLS = <video muted playsinline> + hls.js (or native HLS).
// The stream is open only while `active` and the tab is visible; CctvFeed owns connect / retry /
// snapshot fallback, this component only wires it to the elements and draws the state text.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useGatewayApi } from '@/composables/useGatewayApi'
import { CctvFeed } from '@/utils/cctv-feed'
import {
  chipText, overlayText, snapshotUrl, streamUrl,
  type CctvStatusDto, type CctvType, type FeedPhase,
} from '@/utils/cctv'

const props = withDefaults(defineProps<{ type: CctvType, active?: boolean }>(), { active: true })

const api = useGatewayApi()
const BLANK = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=='

const img = ref<HTMLImageElement | null>(null)
const video = ref<HTMLVideoElement | null>(null)
const snap = ref<HTMLImageElement | null>(null)
const phase = ref<FeedPhase>('connecting')
const snapshotOk = ref(false)
const snapshotSrc = ref('')

let streaming = false // an MJPEG src is attached (its load / error events are about it)
let hls: { destroy: () => void } | null = null
let openGen = 0

async function openHls(ticket: string, gen: number): Promise<void> {
  const el = video.value
  if (!el) return
  const url = streamUrl('hls', ticket)
  if (el.canPlayType('application/vnd.apple.mpegurl')) {
    el.src = url
    void el.play().catch(() => {})
    return
  }
  const { default: Hls } = await import('hls.js')
  if (gen !== openGen || !Hls.isSupported()) {
    if (gen === openGen) feed.notifyError()
    return
  }
  const h = new Hls({ maxBufferLength: 10, liveSyncDurationCount: 3, manifestLoadingMaxRetry: 1, levelLoadingMaxRetry: 1, fragLoadingMaxRetry: 2 })
  hls = h
  h.on(Hls.Events.ERROR, (_e, data) => { if (data.fatal && gen === openGen) feed.notifyError() })
  h.loadSource(url)
  h.attachMedia(el)
  void el.play().catch(() => {})
}

function closeHls(): void {
  openGen++
  hls?.destroy()
  hls = null
  const el = video.value
  if (el) {
    el.removeAttribute('src')
    el.load()
  }
}

const feed = new CctvFeed({
  fetchTicket: async () => (await api.post<{ ticket: string }>('/api/mount/cctv/ticket', {})).ticket,
  openStream(ticket) {
    if (props.type === 'hls') {
      void openHls(ticket, ++openGen)
      return
    }
    streaming = true
    if (img.value) img.value.src = streamUrl('mjpeg', ticket)
  },
  closeStream() {
    if (props.type === 'hls') closeHls()
    else if (img.value) {
      streaming = false
      img.value.src = BLANK // a src swap closes the connection to the gateway
    }
  },
  showSnapshot: (ticket, n) => { snapshotSrc.value = snapshotUrl(ticket, n) },
  fetchState: async () => (await api.get<CctvStatusDto>('/api/mount/cctv/status')).state,
  changed() {
    phase.value = feed.phase
    snapshotOk.value = feed.snapshotOk
  },
})

const overlay = computed(() => overlayText(phase.value, snapshotOk.value))
const chip = computed(() => chipText(phase.value, snapshotOk.value))

function sync(): void {
  if (props.active && !document.hidden) feed.start()
  else feed.stop()
}

watch(() => props.active, sync)
watch(() => props.type, () => { feed.stop(); sync() })
onMounted(() => {
  document.addEventListener('visibilitychange', sync)
  sync()
})
onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', sync)
  feed.stop()
})
</script>

<template>
  <div class="cctv-player" :data-phase="phase" data-testid="cctv-player">
    <img
      v-if="type === 'mjpeg'" ref="img" class="cctv-media" :class="{ 'cctv-off': phase !== 'live' }" alt="" draggable="false"
      data-testid="cctv-stream" @load="streaming && feed.notifyLive()" @error="streaming && feed.notifyError()"
    >
    <video
      v-else ref="video" class="cctv-media" :class="{ 'cctv-off': phase !== 'live' }" muted playsinline
      data-testid="cctv-stream" @playing="feed.notifyLive()" @error="feed.notifyError()"
    />
    <img
      v-if="snapshotSrc && phase === 'snapshot'" ref="snap" class="cctv-media" :class="{ 'cctv-off': !snapshotOk }"
      :src="snapshotSrc" alt="" draggable="false" data-testid="cctv-snapshot"
      @load="feed.notifySnapshot(true)" @error="feed.notifySnapshot(false)"
    >
    <p v-if="overlay" class="cctv-overlay" role="status" data-testid="cctv-overlay">
      {{ overlay }}
    </p>
    <span v-if="chip" class="cctv-chip" :class="{ 'cctv-chip-live': chip === 'LIVE' }" data-testid="cctv-chip">{{ chip }}</span>
  </div>
</template>

<style scoped>
.cctv-player { position: absolute; inset: 0; background: #08090b; overflow: hidden; }
.cctv-media { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; }
.cctv-off { visibility: hidden; }
.cctv-overlay {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; margin: 0; padding: 0 12px;
  text-align: center; font-size: 13px; color: var(--muted-foreground);
}
.cctv-chip {
  position: absolute; left: 6px; top: 6px; padding: 1px 6px; font-size: 11px; font-weight: 700; letter-spacing: 0.06em;
  line-height: 1.4; border: 1px solid var(--control-border); background: rgba(8, 9, 11, 0.7); color: var(--foreground);
}
.cctv-chip-live { color: var(--accent-green-text); border-color: var(--accent-green-border); }
</style>
