// App-wide gateway telemetry store (T1.3): owns the single gateway socket.
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  createMountSocket,
  type CommandAck,
  type CommandName,
  type ControlHolder,
  type MountConnection,
} from '../utils/mount-socket'
import { buildMountDisplay, isLinkStale, type MountSnapshotLike } from '../utils/mount-format'

const CLOCK_TICK_MS = 250

export const useMountStore = defineStore('mount', () => {
  const snapshot = ref<(Record<string, unknown> & MountSnapshotLike) | null>(null)
  const hello = ref<Record<string, unknown> | null>(null)
  const connection = ref<MountConnection>('connecting')
  const messageCount = ref(0)
  const lastError = ref<string | null>(null)
  const lastStatusAt = ref<number | null>(null)
  // Control path (T3.1a): last acks (newest first), for the dev control page and later UI.
  const acks = ref<CommandAck[]>([])
  const ACK_HISTORY = 10
  // Wall-clock "now", ticked so staleness is reactive even when no messages arrive.
  const now = ref(Date.now())

  let clock: ReturnType<typeof setInterval> | null = null
  let socket: ReturnType<typeof createMountSocket> | null = null

  const linkStale = computed(() => isLinkStale(connection.value, lastStatusAt.value, now.value))
  /** Who holds the control lock, from this window's point of view (per-client status field). */
  const controlHolder = computed<ControlHolder>(() => {
    const h = (snapshot.value as { control?: { holder?: string } } | null)?.control?.holder
    return h === 'you' || h === 'other' ? h : 'none'
  })
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

  function recordAck(ack: CommandAck) {
    acks.value = [ack, ...acks.value].slice(0, ACK_HISTORY)
    const h = ack.control?.holder
    const snap = snapshot.value as { control?: { holder?: string } } | null
    if (h && snap) snap.control = { holder: h }
  }

  /** Send a command; resolves with the gateway ack (ok:false + reason when refused/failed). */
  async function sendCommand(
    cmd: CommandName,
    args: Record<string, unknown> = {},
    timeoutMs?: number,
  ): Promise<CommandAck> {
    if (!socket) {
      return { id: null, cmd, ok: false, reason: 'Not connected to the gateway' }
    }
    const ack = await socket.sendCommand(cmd, args, timeoutMs)
    // Acks that came off the wire were recorded by onAck; record local failures (timeout, link lost).
    if (ack.id === null || !acks.value.some(a => a.id === ack.id)) recordAck(ack)
    return ack
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
      onAck: recordAck,
    })
    socket.start()
  }

  function stop() {
    socket?.stop()
    socket = null
    stopClock()
  }

  return {
    snapshot, hello, connection, messageCount, lastError, lastStatusAt, acks, controlHolder,
    linkStale, display, sendCommand, ingestStatus, setConnection, startClock, stopClock, start, stop,
  }
})
