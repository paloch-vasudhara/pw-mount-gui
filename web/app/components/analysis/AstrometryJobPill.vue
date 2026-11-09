<script setup lang="ts">
// Astrometry job pill (T5.2d): a small two-line pill at the bottom-left of the Sky Viewer frame, mounted once (user 11-02:
// the title on top, the status below at full width). Running: "Astrometry" / "<stage>" + spinner + Cancel. Done: "Astrometry
// solved" / "row N" (click opens IOD) or, when no IOD row holds the job (after a refresh), "apply to a row…" (click opens IOD
// and puts the result into the first free row). Done with a solved scale that differs from the station's (T5.2f): "Astrometry
// solved — scale differs" / the comparison + Use / Discard until the user chooses (Use with no row = the "apply to a row…" path,
// applied directly). Failed: "Astrometry failed" / the readable reason (or the last Retry error) + Retry when the job is resumable. The x dismisses a finished
// job (client-side). The job itself lives in the astrometry-job store (polled there).
import { computed, onMounted } from 'vue'
import { useAstrometryJobStore } from '~/stores/astrometry-job'
import { failureText, isActiveState, stageLabel } from '~/utils/astrometry-job'
import { reviewText } from '~/utils/iod'

const store = useAstrometryJobStore()

onMounted(() => { void store.reattach() })

const job = computed(() => store.job)
const kind = computed<'running' | 'done' | 'review' | 'failed' | null>(() => {
  const j = job.value
  if (!j) return null
  if (isActiveState(j.state)) return 'running'
  if (j.state === 'done') return store.needsReview ? 'review' : 'done'
  return 'failed'
})
const reviewDetail = computed(() => {
  const c = job.value?.result?.scale_check
  return c ? reviewText(c) : ''
})
const doneText = computed(() => (store.row !== null ? `row ${store.row + 1}` : 'apply to a row…'))
const failText = computed(() => store.resumeError ?? (job.value ? failureText(job.value) : ''))
const canRetry = computed(() => !!job.value?.resumable)

function onDone(): void {
  if (store.row !== null) store.requestOpenIod()
  else store.requestApply()
}
</script>

<template>
  <div v-if="kind" class="as-pill" :class="`is-${kind}`" data-testid="as-pill" :data-kind="kind" role="status">
    <template v-if="kind === 'running' && job">
      <span class="as-pill-spin" aria-hidden="true" />
      <span class="as-pill-body">
        <span class="as-pill-title" data-testid="as-pill-title">Astrometry</span>
        <span class="as-pill-text" data-testid="as-pill-text">{{ stageLabel(job) }}</span>
      </span>
      <button type="button" class="as-pill-btn" data-testid="as-pill-cancel" @click="store.cancel()">
        Cancel
      </button>
    </template>
    <template v-else-if="kind === 'review'">
      <span class="as-pill-body" :title="reviewDetail">
        <span class="as-pill-title" data-testid="as-pill-title">Astrometry solved — scale differs</span>
        <span class="as-pill-text" data-testid="as-pill-text">{{ reviewDetail }}</span>
      </span>
      <button type="button" class="as-pill-btn as-pill-use" data-testid="as-pill-use" title="Use this result" @click="store.reviewUse()">
        Use
      </button>
      <button type="button" class="as-pill-btn" data-testid="as-pill-discard" title="Discard this result" @click="store.reviewDiscard()">
        Discard
      </button>
    </template>
    <template v-else-if="kind === 'done'">
      <button
        type="button" class="as-pill-body as-pill-link"
        :title="store.row !== null ? 'Open Initial Orbit Determination' : 'Open Initial Orbit Determination and use the result in a free row'"
        @click="onDone"
      >
        <span class="as-pill-title" data-testid="as-pill-title">Astrometry solved</span>
        <span class="as-pill-text" data-testid="as-pill-text">{{ doneText }}</span>
      </button>
      <button type="button" class="as-pill-x" data-testid="as-pill-dismiss" title="Dismiss" aria-label="Dismiss" @click="store.dismiss()">
        ✕
      </button>
    </template>
    <template v-else>
      <span class="as-pill-body" :title="failText">
        <span class="as-pill-title" data-testid="as-pill-title">Astrometry failed</span>
        <span class="as-pill-text" data-testid="as-pill-text">{{ failText }}</span>
      </span>
      <button v-if="canRetry" type="button" class="as-pill-btn" data-testid="as-pill-retry" :disabled="store.resuming" title="Re-poll the same astrometry.net submission (no new upload)" @click="store.resume()">
        Retry
      </button>
      <button type="button" class="as-pill-x" data-testid="as-pill-dismiss" title="Dismiss" aria-label="Dismiss" @click="store.dismiss()">
        ✕
      </button>
    </template>
  </div>
</template>

<style scoped>
.as-pill {
  position: absolute; left: 8px; bottom: 8px; z-index: 40; display: flex; align-items: center; gap: 8px; max-width: min(460px, calc(50% - 120px));
  padding: 4px 6px 4px 10px; background: var(--page-bg); border: 1px solid var(--control-border); border-radius: 8px;
  font-size: 12px; font-weight: 600; color: var(--foreground); box-shadow: 0 2px 8px rgb(0 0 0 / 0.35);
}
.as-pill.is-done { border-color: var(--accent-green-border); }
.as-pill.is-review { border-color: var(--accent-green-border); }
.as-pill.is-review .as-pill-title { color: var(--accent-green-border); }
.as-pill-use { background: var(--accent-green-border); border-color: var(--accent-green-border); color: #fff; }
.as-pill-use:hover { border-color: var(--accent-green-border); filter: brightness(1.1); }
.as-pill.is-failed { border-color: var(--danger); }
.as-pill-body { display: flex; flex-direction: column; min-width: 0; line-height: 1.25; }
.as-pill-title { font-size: 11px; font-weight: 600; color: var(--muted-foreground); white-space: nowrap; }
.as-pill-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.as-pill.is-done .as-pill-title { color: var(--accent-green-border); }
.as-pill.is-failed .as-pill-title, .as-pill.is-failed .as-pill-text { color: var(--danger); }
.as-pill-link { background: none; border: 0; padding: 0; font: inherit; color: inherit; cursor: pointer; text-align: left; }
.as-pill-link:hover { color: var(--accent-green-border); }
.as-pill-btn {
  flex: none; height: 18px; padding: 0 8px; border: 1px solid var(--control-border); background: var(--secondary); color: var(--foreground);
  font-size: 12px; font-weight: 700; border-radius: 9px; cursor: pointer;
}
.as-pill-btn:hover { border-color: var(--danger); }
.as-pill-x { flex: none; width: 18px; height: 18px; background: none; border: 0; color: var(--muted-foreground); font-size: 12px; cursor: pointer; }
.as-pill-x:hover { color: var(--danger); }
.as-pill-spin {
  flex: none; width: 12px; height: 12px; border: 2px solid var(--control-border); border-top-color: var(--accent-green-border);
  border-radius: 50%; animation: as-pill-rot 0.9s linear infinite;
}
@keyframes as-pill-rot { to { transform: rotate(360deg); } }
</style>
