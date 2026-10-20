// App-wide gateway telemetry store (T1.3): owns the single gateway socket.
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { createMountSocket, type MountConnection } from '../utils/mount-socket'
import { buildMountDisplay, isLinkStale, type MountSnapshotLike } from '../utils/mount-format'

const CLOCK_TICK_MS = 250

export const useMountStore = defineStore('mount', () => {
  const snapshot = ref<(Record<string, unknown> & MountSnapshotLike) | null>(null)
  const hello = ref<Record<string, unknown> | null>(null)
  const connection = ref<MountConnection>('connecting')
  const messageCount = ref(0)
  const lastError = ref<string | null>(null)
  const lastStatusAt = ref<number | null>(null)
  // Wall-clock "now", ticked so staleness is reactive even when no messages arrive.
  const now = ref(Date.now())

  let clock: ReturnType<typeof setInterval> | null = null
  let socket: ReturnType<typeof createMountSocket> | null = null

  const linkStale = computed(() => isLinkStale(connection.value, lastStatusAt.value, now.value))
  const display = computed(() => buildMountDisplay(snapshot.value, linkStale.value))

  function ingestStatus(data: Record<string, unknown>) {
    snapshot.value = data
    lastStatusAt.value = Date.now()
    now.value = lastStatusAt.value
    messageCount.value += 1
  }

  function setConnection(c: MountConnection) {
    connection.value = c
    now.value = Date.now()
  }

  function startClock() {
    if (clock) return
    clock = setInterval(() => {
      now.value = Date.now()
    }, CLOCK_TICK_MS)
  }

  function stopClock() {
    if (clock) clearInterval(clock)
    clock = null
  }

  /** Idempotent: the first caller opens the socket, later callers share it. */
  function start(gatewayWs = '') {
    if (socket) return
    startClock()
    socket = createMountSocket({ gatewayWs }, {
      onConnection: setConnection,
      onHello: (d) => { hello.value = d },
      onStatus: ingestStatus,
      onError: (m) => { lastError.value = m },
    })
    socket.start()
  }

  function stop() {
    socket?.stop()
    socket = null
    stopClock()
  }

  return {
    snapshot, hello, connection, messageCount, lastError, lastStatusAt,
    linkStale, display, ingestStatus, setConnection, startClock, stopClock, start, stop,
  }
})
