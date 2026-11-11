// Selected object (A3a; a satellite by NORAD or, T5.5, an aircraft by ICAO, never both): shared by the Sky Viewer (white ring, focus dim) and the Track Info
// card. The selection is keyed by NORAD, so it survives chunk refreshes; it stays when the object's
// layer is hidden or the object sets (the chart just draws no ring; the card stays).
// While something is selected the live state (az/el/range/illumination) is polled at 1 Hz from
// /api/sky/state (time-limited fetch, stopped on deselect).
import { defineStore } from 'pinia'
import { computed, ref, shallowRef, watch } from 'vue'
import { useSkyStore, type SkyState } from './sky'
import { useMountStore } from './mount'
import { trackPanel } from '../utils/track-panel'
import type { ObjectTrackLike } from '../utils/track-state'

const POLL_MS = 1000

export const useSelectionStore = defineStore('selection', () => {
  const sky = useSkyStore()
  const selectedNorad = ref<number | null>(null)
  // T5.5: ONE selected object at a time, shared by satellites and aircraft; the aircraft one is keyed by ICAO.
  const selectedIcao = ref<string | null>(null)
  /** Latest state for the selected object (null until the first answer). */
  const state = shallowRef<SkyState | null>(null)
  let timer: ReturnType<typeof setInterval> | null = null
  let gen = 0

  async function poll(norad: number, g: number) {
    // maxAge 0: the card wants a fresh value every second (the hover tooltip shares the cache)
    const s = await sky.fetchState(norad, 0)
    if (g === gen && s) state.value = s
  }

  // A1e: the main card's NORAD (the pinned tracked object when nothing/itself is selected, else the
  // selection). The state poll follows it.
  const mount = useMountStore()
  const panel = computed(() => trackPanel(
    selectedNorad.value, (mount.snapshot?.track ?? null) as ObjectTrackLike | null, mount.now / 1000, selectedIcao.value !== null,
  ))
  const cardNorad = computed(() => panel.value.main?.norad ?? null)

  watch(cardNorad, (n) => {
    gen += 1
    if (timer) clearInterval(timer)
    timer = null
    state.value = null
    if (n === null) return
    const g = gen
    void poll(n, g)
    timer = setInterval(() => void poll(n, g), POLL_MS)
  }, { immediate: true })

  function select(norad: number) {
    selectedIcao.value = null
    selectedNorad.value = norad
  }

  function selectAircraft(icao: string) {
    selectedNorad.value = null
    selectedIcao.value = icao
  }

  function deselect() {
    selectedNorad.value = null
    selectedIcao.value = null
  }

  /** Every successful track_object (card, modal, Pass Analysis) brings up the tracked object's card. */
  function afterTrackStarted(norad: number) {
    select(norad)
  }

  /** A successful track_aircraft keeps the aircraft selected (its A1b card shows the phase). */
  function afterAircraftTrackStarted(icao: string) {
    selectAircraft(icao)
  }

  return {
    selectedNorad, selectedIcao, cardNorad, panel, state, select, selectAircraft, deselect, afterTrackStarted,
    afterAircraftTrackStarted,
  }
})
