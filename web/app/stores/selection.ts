// Selected satellite (A3a): shared by the Sky Viewer (white ring, focus dim) and the Track Info
// card. The selection is keyed by NORAD, so it survives chunk refreshes; it stays when the object's
// layer is hidden or the object sets (the chart just draws no ring; the card stays).
// While something is selected the live state (az/el/range/illumination) is polled at 1 Hz from
// /api/sky/state (time-limited fetch, stopped on deselect).
import { defineStore } from 'pinia'
import { ref, shallowRef } from 'vue'
import { useSkyStore, type SkyState } from './sky'

const POLL_MS = 1000

export const useSelectionStore = defineStore('selection', () => {
  const sky = useSkyStore()
  const selectedNorad = ref<number | null>(null)
  /** Latest state for the selected object (null until the first answer). */
  const state = shallowRef<SkyState | null>(null)
  let timer: ReturnType<typeof setInterval> | null = null
  let gen = 0

  async function poll(norad: number, g: number) {
    // maxAge 0: the card wants a fresh value every second (the hover tooltip shares the cache)
    const s = await sky.fetchState(norad, 0)
    if (g === gen && selectedNorad.value === norad && s) state.value = s
  }

  function select(norad: number) {
    if (selectedNorad.value === norad) return
    deselect()
    selectedNorad.value = norad
    const g = ++gen
    void poll(norad, g)
    timer = setInterval(() => void poll(norad, g), POLL_MS)
  }

  function deselect() {
    gen += 1
    if (timer) clearInterval(timer)
    timer = null
    selectedNorad.value = null
    state.value = null
  }

  return { selectedNorad, state, select, deselect }
})
