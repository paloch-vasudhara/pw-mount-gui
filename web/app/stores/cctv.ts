// Mount CCTV (T5.7): whether a camera is configured (the web never learns its URL or login) and
// the per-viewer view choice, kept in localStorage (always try/catch).
import { defineStore } from 'pinia'
import { computed, reactive, ref } from 'vue'
import { useGatewayApi } from '../composables/useGatewayApi'
import { loadView, resolveView, saveView, type CctvStatusDto, type CctvType, type CctvView } from '../utils/cctv'

export const useCctvStore = defineStore('cctv', () => {
  const api = useGatewayApi()
  const status = ref<CctvStatusDto | null>(null)
  const stored = reactive<{ panel: string | null, expanded: string | null }>({
    panel: loadView(false),
    expanded: loadView(true),
  })

  const configured = computed(() => status.value?.configured === true)
  const type = computed<CctvType>(() => status.value?.type ?? 'mjpeg')
  /** A remembered CCTV choice falls back to 3D while CCTV is not configured. */
  const panelView = computed<CctvView>(() => resolveView(stored.panel, false, configured.value))
  const expandedView = computed<CctvView>(() => resolveView(stored.expanded, true, configured.value))

  function setView(expanded: boolean, v: CctvView): void {
    if (expanded) stored.expanded = v
    else stored.panel = v
    saveView(expanded, v)
  }

  /** Ask the gateway; on failure keep what we knew (a gateway blip must not hide the toggle). */
  async function refresh(): Promise<void> {
    try {
      status.value = await api.get<CctvStatusDto>('/api/mount/cctv/status')
    }
    catch {
      // unchanged
    }
  }

  return { status, configured, type, panelView, expandedView, setView, refresh }
})
