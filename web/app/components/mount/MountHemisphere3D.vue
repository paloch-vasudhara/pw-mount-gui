<script setup lang="ts">
// 3D hemisphere view (A4x), fed by the telemetry store. Each instance owns its own
// three.js renderer and disposes it fully on unmount (the modal is opened and closed often).
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useMountStore } from '~/stores/mount'
import { HemisphereView, type HemisphereMode } from '~/lib/hemisphere3d/view'
import { buildHemisphereInputs, type HemisphereSnapshotLike } from '~/lib/hemisphere3d/inputs'

const props = withDefaults(defineProps<{ mode?: HemisphereMode }>(), { mode: 'compact' })

const store = useMountStore()
const host = ref<HTMLElement | null>(null)
let view: HemisphereView | null = null
const failed = ref(false)

function push(): void {
  view?.setInputs(buildHemisphereInputs(store.snapshot as HemisphereSnapshotLike | null, store.linkStale))
}

onMounted(() => {
  if (!host.value) return
  try {
    view = new HemisphereView(host.value, props.mode)
  }
  catch {
    failed.value = true // WebGL unavailable: leave the slot empty rather than break the dashboard
    return
  }
  push()
})

watch(() => [store.snapshot, store.linkStale] as const, push)

onBeforeUnmount(() => {
  view?.dispose()
  view = null
})
</script>

<template>
  <div class="relative h-full w-full" :data-mode="props.mode" data-testid="mount-3d">
    <div ref="host" class="absolute inset-0" data-testid="mount-3d-canvas" />
    <p
      v-if="failed"
      class="absolute inset-0 flex items-center justify-center text-[12px] text-muted-foreground"
    >
      3D view unavailable (WebGL)
    </p>
  </div>
</template>
