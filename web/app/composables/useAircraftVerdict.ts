// TRACK AIRCRAFT verdict (T5.6b): the gateway's check of `track_aircraft` for one ICAO, polled at 1 Hz
// like the satellite verdict on the Track Info card (in-flight skip, a generation counter that drops the
// answers of a stale selection, an unreachable verdict when the request fails). It only polls while
// `active` (ADS-B green and the aircraft in view): the local branches of aircraftTrackVerdict need no request.
import { onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { useGatewayApi } from '~/composables/useGatewayApi'
import { parseAircraftVerdict } from '~/utils/aircraft-track'
import { UNREACHABLE_VERDICT, type TrackVerdict } from '~/utils/track-verdict'

export function useAircraftVerdict(icao: Ref<string | null>, active: Ref<boolean>) {
  const api = useGatewayApi()
  const verdict = ref<TrackVerdict | null>(null)
  let gen = 0
  let timer: ReturnType<typeof setInterval> | null = null
  let inFlight = false

  async function run(i: string, g: number) {
    if (inFlight) return
    inFlight = true
    let v: TrackVerdict
    try {
      v = parseAircraftVerdict(await api.post<unknown>('/api/mount/check', { cmd: 'track_aircraft', args: { icao: i } }))
    }
    catch {
      v = UNREACHABLE_VERDICT
    }
    if (g === gen) {
      inFlight = false
      verdict.value = v
    }
  }

  function stop() {
    gen += 1
    inFlight = false
    if (timer) clearInterval(timer)
    timer = null
    verdict.value = null
  }

  watch([icao, active], ([i, on]) => {
    stop()
    if (i === null || !on) return
    const g = gen
    void run(i, g)
    timer = setInterval(() => void run(i, g), 1000)
  }, { immediate: true })
  onBeforeUnmount(stop)

  return { verdict }
}
