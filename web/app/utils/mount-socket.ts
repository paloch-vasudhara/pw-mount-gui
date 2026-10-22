// Gateway telemetry socket client (framework-free; the Pinia store owns one instance).
// Fetches the per-launch token from /api/mount/session, opens /api/mount/ws, sends the auth
// message first, then forwards hello/status messages. Reconnects with backoff (capped 5 s);
// the session fetch and the socket open are both time-limited so a dead gateway cannot stall it.

export type MountConnection = 'connecting' | 'open' | 'closed'

export const SESSION_TIMEOUT_MS = 3_000
export const OPEN_TIMEOUT_MS = 5_000
export const MAX_BACKOFF_MS = 5_000

export const ACK_TIMEOUT_MS = 6_000

export type ControlHolder = 'you' | 'other' | 'none'

/** Gateway answer to one `cmd` message (T3.1a). `ok:false` always carries a G7 `reason`. */
export interface CommandAck {
  id: string | null
  cmd: string | null
  ok: boolean
  reason: string | null
  control?: { holder: ControlHolder }
}

export type CommandName =
  | 'take_control' | 'release_control' | 'stop'
  | 'goto_altaz' | 'goto_radec_j2000' | 'tracking'

/**
 * Pending-command bookkeeping: each sent command gets a promise that settles with the matching
 * ack, a local timeout, or a "link lost" refusal. It never rejects, so callers can always show
 * `reason`.
 */
export function createAckTracker(timeoutMs = ACK_TIMEOUT_MS) {
  const pending = new Map<string, { resolve: (a: CommandAck) => void, timer: ReturnType<typeof setTimeout>, cmd: string }>()
  let counter = 0

  function newId(): string {
    counter += 1
    const rnd = typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2)
    return `${counter}-${rnd}`.slice(0, 64)
  }

  function register(cmd: string, timeout = timeoutMs): { id: string, promise: Promise<CommandAck> } {
    const id = newId()
    const promise = new Promise<CommandAck>((resolve) => {
      const timer = setTimeout(() => {
        pending.delete(id)
        resolve({ id, cmd, ok: false, reason: 'No answer from the gateway (timed out)' })
      }, timeout)
      pending.set(id, { resolve, timer, cmd })
    })
    return { id, promise }
  }

  function settle(ack: CommandAck): boolean {
    const p = ack.id ? pending.get(ack.id) : undefined
    if (!p) return false
    clearTimeout(p.timer)
    pending.delete(ack.id as string)
    p.resolve(ack)
    return true
  }

  function failAll(reason: string) {
    for (const [id, p] of pending) {
      clearTimeout(p.timer)
      p.resolve({ id, cmd: p.cmd, ok: false, reason })
    }
    pending.clear()
  }

  return { register, settle, failAll, size: () => pending.size }
}

export interface MountEnvelope<T = unknown> {
  type: string
  seq: number
  ts: string
  data: T
}

export interface MountSocketHandlers {
  onConnection: (c: MountConnection) => void
  onHello: (data: Record<string, unknown>) => void
  onStatus: (data: Record<string, unknown>) => void
  onError: (msg: string | null) => void
  /** Every ack that arrives (including ones whose promise already timed out). */
  onAck?: (ack: CommandAck) => void
}

export interface MountSocketOptions {
  /** ws(s) base URL; empty = same origin as the page. */
  gatewayWs: string
}

export function createMountSocket(opts: MountSocketOptions, h: MountSocketHandlers) {
  let ws: WebSocket | null = null
  let timer: ReturnType<typeof setTimeout> | null = null
  let openTimer: ReturnType<typeof setTimeout> | null = null
  let attempt = 0
  let stopped = true
  const acks = createAckTracker()

  function scheduleReconnect() {
    if (stopped) return
    h.onConnection('closed')
    const delay = Math.min(MAX_BACKOFF_MS, 500 * 2 ** attempt)
    attempt += 1
    timer = setTimeout(connect, delay)
  }

  async function connect() {
    if (stopped) return
    h.onConnection('connecting')
    try {
      // The session request must time out: while the gateway is down, the dev proxy
      // never answers, and an un-timed await would stall reconnection forever.
      const res = await fetch('/api/mount/session', {
        cache: 'no-store',
        signal: AbortSignal.timeout(SESSION_TIMEOUT_MS),
      })
      if (!res.ok) throw new Error(`session HTTP ${res.status}`)
      const { token } = await res.json()
      if (stopped) return
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const base = opts.gatewayWs || `${proto}://${location.host}`
      const sock = new WebSocket(`${base}/api/mount/ws`)
      ws = sock
      // A socket that never reaches `hello` is abandoned and retried.
      openTimer = setTimeout(() => sock.close(), OPEN_TIMEOUT_MS)
      sock.onopen = () => {
        sock.send(JSON.stringify({ type: 'auth', token }))
      }
      sock.onmessage = (ev) => {
        let msg: MountEnvelope
        try {
          msg = JSON.parse(ev.data)
        }
        catch {
          return
        }
        if (msg.type === 'hello') {
          if (openTimer) clearTimeout(openTimer)
          h.onHello(msg.data as Record<string, unknown>)
          h.onConnection('open')
          attempt = 0
          h.onError(null)
        }
        else if (msg.type === 'status') {
          h.onStatus(msg.data as Record<string, unknown>)
        }
        else if (msg.type === 'ack') {
          const ack = msg as unknown as CommandAck
          acks.settle(ack)
          h.onAck?.(ack)
        }
      }
      sock.onclose = (ev) => {
        if (openTimer) clearTimeout(openTimer)
        acks.failAll('Gateway link lost')
        if (ws === sock) ws = null
        h.onError(`closed (${ev.code})`)
        scheduleReconnect()
      }
      sock.onerror = () => {
        // onclose follows and schedules the reconnect
      }
    }
    catch (e) {
      h.onError(String(e))
      scheduleReconnect()
    }
  }

  /** Send one command; resolves with the ack (never rejects). */
  function sendCommand(
    cmd: CommandName,
    args: Record<string, unknown> = {},
    timeoutMs = ACK_TIMEOUT_MS,
  ): Promise<CommandAck> {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return Promise.resolve({ id: null, cmd, ok: false, reason: 'Not connected to the gateway' })
    }
    const { id, promise } = acks.register(cmd, timeoutMs)
    ws.send(JSON.stringify({ type: 'cmd', id, cmd, args }))
    return promise
  }

  function start() {
    if (!stopped) return
    stopped = false
    attempt = 0
    void connect()
  }

  function stop() {
    stopped = true
    if (timer) clearTimeout(timer)
    if (openTimer) clearTimeout(openTimer)
    ws?.close()
    ws = null
    acks.failAll('Gateway link closed')
  }

  return { start, stop, sendCommand }
}
