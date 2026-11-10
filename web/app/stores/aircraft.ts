// Aircraft store (T5.5): the 1 Hz `aircraft` WebSocket message (via stores/mount.ts) and the ADS-B
// status block. Rows are kept with an interpolation anchor (utils/aircraft.ts) so the Sky Viewer can
// draw smooth positions between frames. Everything clears on the empty frame, when the link closes
// and whenever ADS-B is not green (status `adsb.green` false).
import { defineStore } from 'pinia'
import { computed, shallowRef, watch } from 'vue'
import { useMountStore } from './mount'
import type { AdsbInfo, AircraftMessageData, AircraftRow } from '../utils/adsb'
import { ingestFrame, type TrackedAircraft } from '../utils/aircraft'

export const useAircraftStore = defineStore('aircraft', () => {
  const mount = useMountStore()
  const tracked = shallowRef<ReadonlyMap<string, TrackedAircraft>>(new Map())
  /** Bumps on every frame / clear (the chart redraws on it). */
  const version = shallowRef(0)
  const messageAdsb = shallowRef<AdsbInfo | null>(null)

  /** The 10 Hz status block wins (fresher); the aircraft message's own block until the first status. */
  const adsb = computed<AdsbInfo | null>(() => {
    if (mount.connection !== 'open') return null
    const s = (mount.snapshot as { adsb?: AdsbInfo } | null)?.adsb
    return s && typeof s === 'object' ? s : messageAdsb.value
  })
  const green = computed(() => adsb.value?.green === true)
  const rows = computed<AircraftRow[]>(() => [...tracked.value.values()].map(t => t.row))

  function clear() {
    if (tracked.value.size === 0) return
    tracked.value = new Map()
    version.value += 1
  }

  /** One `aircraft` message. Not green (by the message or the live status) = nothing is shown. */
  function ingest(data: AircraftMessageData | null | undefined, nowMs = Date.now()) {
    if (!data || typeof data !== 'object') return
    if (data.adsb && typeof data.adsb === 'object') messageAdsb.value = data.adsb
    const statusGreen = (mount.snapshot as { adsb?: AdsbInfo } | null)?.adsb?.green
    if (data.adsb?.green !== true || statusGreen === false) {
      clear()
      return
    }
    const list = Array.isArray(data.aircraft) ? data.aircraft : []
    if (list.length === 0 && tracked.value.size === 0) return
    tracked.value = ingestFrame(tracked.value, list, nowMs)
    version.value += 1
  }

  function get(icao: string): TrackedAircraft | null {
    return tracked.value.get(icao) ?? null
  }

  // sync: a not-green status or a closed link empties the list before anything can draw it
  watch(green, (g) => {
    if (!g) clear()
  }, { flush: 'sync' })
  watch(() => mount.connection, (c) => {
    if (c !== 'open') {
      messageAdsb.value = null
      clear()
    }
  }, { flush: 'sync' })

  return { tracked, version, adsb, green, rows, ingest, clear, get }
})
