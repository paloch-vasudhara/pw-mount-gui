<script setup lang="ts">
// Track Info card (A1a/A1c, concept/Track Cards.png), read-only. Values refresh at 1 Hz from
// /api/sky/state (polled by stores/selection.ts while an object is selected).
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import FlagBadge from '~/components/track/FlagBadge.vue'
import StaleBadge from '~/components/track/StaleBadge.vue'
import { useMountStore } from '~/stores/mount'
import { trackCardState, type ObjectTrackLike } from '~/utils/track-state'
import { useGatewayApi } from '~/composables/useGatewayApi'
import { useSelectionStore } from '~/stores/selection'
import { useSkyStore } from '~/stores/sky'
import { SAT_CLASS_COLORS } from '~/lib/sky/theme'
import { parseTrackVerdict, UNREACHABLE_VERDICT, type TrackVerdict } from '~/utils/track-verdict'
import { formatAz, formatEl, formatRange, illuminationOf, ILLUMINATION_LABEL } from '~/utils/track-format'

const base = useRuntimeConfig().app.baseURL || '/'
const sel = useSelectionStore()
const sky = useSkyStore()
const mount = useMountStore()

// A1e: the main card (pinned tracked object, or the selection); the strip is the tracked A under a selected B
const norad = computed(() => sel.cardNorad)
const pinned = computed(() => sel.panel.main?.pinned === true)
const strip = computed(() => sel.panel.strip)
// A1e: the tracked object's pinned card shown with nothing selected re-focuses the Sky Viewer on it
// when clicked (like the strip under a selected card; user 10-29)
const refocusable = computed(() => pinned.value && norad.value !== null && sel.selectedNorad !== norad.value)
function onCardClick() {
  if (refocusable.value && norad.value !== null) sel.select(norad.value)
}
const stripCountry = computed(() => {
  void sky.metaVersion
  const st = sel.panel.strip
  return st ? (sky.meta.get(st.norad)?.country ?? null) : null
})
const tleAge = computed(() => (sel.state && sel.state.norad === norad.value ? sel.state.tle_age_days : null))
// metadata from the live-set list until the first state answer arrives (state wins afterwards)
const info = computed(() => {
  void sky.metaVersion
  const n = norad.value
  if (n === null) return null
  const m = sky.meta.get(n)
  const s = sel.state && sel.state.norad === n ? sel.state : null
  return {
    name: s?.name ?? m?.name ?? `NORAD ${n}`,
    cospar: s?.cospar ?? m?.cospar ?? null,
    country: s?.country ?? m?.country ?? null,
    cls: s?.orbit_class ?? m?.orbit_class ?? null,
    az: s?.az ?? null,
    alt: s?.alt ?? null,
    range: s?.range_km ?? null,
    lit: s?.illuminated ?? null,
  }
})
const illum = computed(() => (info.value ? illuminationOf(info.value.alt, info.value.lit) : 'unknown'))
const chipColor = computed(() => SAT_CLASS_COLORS[(info.value?.cls ?? '').toUpperCase()] ?? '#888888')

// TRACK OBJECT verdict (T4.1b): the gateway's check of `track_object` for the selected object, polled
// at 1 Hz (the api helper's own fetch timeout applies). `checkGen` drops answers of a stale selection;
// a tick is skipped while the previous request is still in flight.
const api = useGatewayApi()
const verdict = ref<TrackVerdict | null>(null)
let checkGen = 0
let checkTimer: ReturnType<typeof setInterval> | null = null
let inFlight = false

async function runVerdict(n: number, g: number) {
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

function stopVerdict() {
  checkGen += 1
  inFlight = false
  if (checkTimer) clearInterval(checkTimer)
  checkTimer = null
  verdict.value = null
}

watch(norad, (n) => {
  stopVerdict()
  if (n === null) return
  const g = checkGen
  void runVerdict(n, g)
  checkTimer = setInterval(() => void runVerdict(n, g), 1000)
}, { immediate: true })
onBeforeUnmount(stopVerdict)

// T4.2e: object-track state from the snapshot (`track`), clocked by the store (wall clock, 250 ms tick).
const REFUSAL_SHOW_MS = 3000
const refusal = ref<{ text: string, norad: number, at: number } | null>(null)
const sending = ref(false)
const cardState = computed(() => trackCardState(
  (mount.snapshot?.track ?? null) as ObjectTrackLike | null, norad.value, mount.now / 1000, verdict.value,
))
const trackEnabled = computed(() => cardState.value.buttonEnabled && !sending.value)
const trackReason = computed(() => {
  const r = refusal.value
  if (r && r.norad === norad.value && mount.now - r.at < REFUSAL_SHOW_MS && !cardState.value.isTrackedCard) return r.text
  return cardState.value.line
})
const tracking = computed(() => cardState.value.isTrackedCard)

async function onTrack() {
  const n = norad.value
  if (n === null || !trackEnabled.value) return
  sending.value = true
  refusal.value = null
  try {
    const ack = await mount.sendCommand('track_object', { norad: n })
    if (ack.ok) sel.afterTrackStarted(n)
    else refusal.value = { text: ack.reason ?? 'Command refused', norad: n, at: Date.now() }
  }
  finally {
    sending.value = false
  }
}
</script>

<template>
  <div class="wrap">
  <div
    v-if="info" class="card" :class="{ refocus: refocusable }" data-testid="track-card"
    :data-refocus="refocusable ? 'true' : 'false'" :title="refocusable ? 'Show the tracked object in the Sky Viewer' : undefined"
    @click="onCardClick"
  >
    <div class="head">
      <img class="icon" :src="`${base}icons/satellite.svg`" alt="" aria-hidden="true">
      <div class="titles">
        <div class="name" data-testid="track-name">
          <span>{{ info.name }}</span>
          <FlagBadge :code="info.country" />
        </div>
        <div class="ids">
          <span data-testid="track-ids">{{ info.cospar ?? '—' }} ({{ norad }})</span>
          <span v-if="info.cls" class="chip" data-testid="track-class" :style="{ background: chipColor }">{{ info.cls }}</span>
          <StaleBadge :age-days="tleAge" />
        </div>
      </div>
      <button v-if="!pinned" type="button" class="close" aria-label="Deselect" data-testid="track-close" @click="sel.deselect()">
        &#x2715;
      </button>
    </div>
    <div class="sep" />
    <dl class="rows">
      <dt>Azimuth</dt>
      <dd data-testid="track-az">
        {{ formatAz(info.az) }}
      </dd>
      <dt>Elevation</dt>
      <dd data-testid="track-el">
        {{ formatEl(info.alt) }}
      </dd>
      <dt>Range</dt>
      <dd data-testid="track-range">
        {{ formatRange(info.range) }}
      </dd>
      <dt>Illumination</dt>
      <dd data-testid="track-illum" :data-state="illum">
        <i v-if="illum !== 'unknown'" class="dot" :class="illum" />{{ ILLUMINATION_LABEL[illum] }}
      </dd>
    </dl>
    <span class="track-wrap" :title="trackReason ?? undefined" :class="{ on: trackEnabled }">
      <button
        type="button" class="track-btn" :class="{ on: trackEnabled, active: tracking }" :disabled="!trackEnabled"
        :data-enabled="trackEnabled ? 'true' : 'false'" :data-tracking="tracking ? 'true' : 'false'" data-testid="track-object"
        @click="onTrack"
      >{{ cardState.buttonLabel }}</button>
    </span>
    <div v-if="trackReason" class="track-reason" :title="trackReason" data-testid="track-reason">{{ trackReason }}</div>
  </div>
  <div v-else class="empty" data-testid="track-empty">
    No object selected
  </div>
  <div class="strip-slot">
    <button
      v-if="strip" type="button" class="strip" data-testid="track-strip" :title="`${strip.name} · ${strip.line}`"
      @click="sel.select(strip.norad)"
    >
      <span class="strip-text">
        <img class="strip-icon" :src="`${base}icons/satellite.svg`" alt="" aria-hidden="true">
        <span class="strip-name">{{ strip.name }}</span>
        <FlagBadge class="strip-flag" :code="stripCountry" />
        <span class="strip-line">&nbsp;&middot; {{ strip.line }}</span>
      </span>
    </button>
  </div>
  </div>
</template>

<style scoped>
/* Everything is sized from --u, derived from the panel's own height (container query), so the card
   fits the short Track Info panel of a 1280x720 @1.5 laptop and is unchanged (14 px) at 1920x1080. */
/* --strip-reserve = the strip slot (22 px strip + 4 px gap), reserved ALL THE TIME under the card so nothing jumps when a
   track starts / ends; the card's --u is computed from the panel height minus it, so the card never changes size. */
.wrap { --strip-h: 22px; --strip-gap: 4px; --strip-reserve: calc(var(--strip-h) + var(--strip-gap)); height: 100%; min-height: 0; container-type: size; display: flex; flex-direction: column; overflow: hidden; }
.wrap > .card { flex: none; }
.wrap > .empty { flex: 1; }
.empty { height: 100%; min-height: 0; display: grid; place-items: center; color: var(--muted-foreground); font-size: 15px; }
.card {
  --u: clamp(9px, calc((100cqh - var(--strip-reserve)) * 0.064), 14px);
  font-size: var(--u); border: 1px solid var(--panel-border); padding: 0.7em 0.9em 0.8em;
  display: flex; flex-direction: column; gap: 0.5em; min-width: 0;
}
.head { display: flex; align-items: flex-start; gap: var(--icon-gap); }
/* icon column = icon + gap; the data rows are indented by the same amount so Azimuth lines up with
   the satellite name (Track Cards.png). Icon file: public/icons/satellite.svg (editable). */
.card { --icon-w: 1.7em; --icon-gap: 0.7em; }
.icon { flex: none; width: var(--icon-w); height: var(--icon-w); margin-top: 0.1em; }
.titles { flex: 1; min-width: 0; }
.name { display: flex; align-items: center; gap: 0.5em; font-size: 1.15em; font-weight: 700; color: var(--foreground); line-height: 1.2; }
.name span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ids { display: flex; align-items: center; gap: 0.5em; font-size: 1.05em; color: var(--foreground); line-height: 1.3; }
.chip { display: inline-block; padding: 0 0.6em; border-radius: 1em; font-size: 0.8em; font-weight: 800; line-height: 1.45; color: #1c1d20; }
.close { flex: none; background: none; border: 0; color: var(--foreground); font-size: 1.3em; cursor: pointer; padding: 0 2px; line-height: 1; }
.close:hover { color: #fff; }
.sep { border-top: 1px solid var(--control-border); }
.rows {
  /* labels start under the name; label column = widest label, then at least one tab (4 ch) */
  display: grid; grid-template-columns: max-content 1fr; column-gap: max(4ch, 2em); row-gap: 0.2em;
  margin: 0; padding: 0 0 0 calc(var(--icon-w) + var(--icon-gap)); font-size: 1em; line-height: 1.25;
}
.rows dt { color: var(--foreground); }
.rows dd { margin: 0; font-weight: 700; color: var(--foreground); font-variant-numeric: tabular-nums; }
.dot { display: inline-block; width: 0.75em; height: 0.75em; border-radius: 50%; margin-right: 0.45em; background: #888; }
.dot.illuminated { background: #ffc233; }
.dot.umbra, .dot.below { background: #8a8a8a; }
.track-wrap { display: block; margin-top: 0.3em; cursor: not-allowed; }
.track-wrap.on { cursor: default; }
.track-btn.on { background: linear-gradient(var(--primary-from), var(--primary-to)); color: #fff; cursor: pointer; }
.track-btn.active { background: linear-gradient(var(--primary-from), var(--primary-to)); color: #fff; opacity: 0.85; cursor: default; }
.track-reason { margin-top: -0.2em; font-size: 0.85em; line-height: 1.2; color: var(--muted-foreground); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.strip-slot { flex: none; height: var(--strip-h); margin-top: var(--strip-gap); min-width: 0; }
.strip {
  width: 100%; height: 100%; padding: 0 0.9em; border: 1px solid var(--panel-border); background: none; color: var(--foreground);
  font-size: 11px; text-align: left; cursor: pointer; display: flex; align-items: center; min-width: 0;
}
.strip:hover { border-color: var(--control-border); }
.card.refocus { cursor: pointer; }
.card.refocus:hover { border-color: var(--control-border); }
/* ONE line: icon, name, flag, " · " phase. The name shrinks (ellipsis) first so the phase / countdown stays readable. */
.strip-text { display: flex; align-items: center; gap: 6px; min-width: 0; width: 100%; white-space: nowrap; line-height: 16px; }
.strip-icon { flex: none; width: 14px; height: 14px; }
.strip-name { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; font-weight: 700; }
.strip-flag { flex: none; }
.strip-line { flex: none; margin-left: -6px; }
.track-btn { width: 100%; height: 2.2em; border: 0; background: #7b7b7b; color: #a8a8a8; font-size: 1.05em; font-weight: 700; letter-spacing: 0.02em; cursor: not-allowed; }
/* Short panel: the card's own height budget (panel minus the strip reserve) is under 160 px, i.e. --u is at/near its
   9 px floor (1280x600 @1.5): tighter spacing so the card and the T4.1 reason line stay inside the panel.
   186 = 160 + 26 (strip reserve); 1920x1080 is unaffected. */
@container (max-height: 186px) {
  .card { gap: 0.3em; padding: 0.4em 0.9em 0.4em; }
  .rows { row-gap: 0.12em; }
  .track-wrap { margin-top: 0; }
  .track-reason { margin-top: -0.3em; }
}
</style>
