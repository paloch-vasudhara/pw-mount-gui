// Gateway telemetry socket client (framework-free; the Pinia store owns one instance).
// Fetches the per-launch token from /api/mount/session, opens /api/mount/ws, sends the auth
// message first, then forwards hello/status messages. Reconnects with backoff (capped 5 s);
// the session fetch and the socket open are both time-limited so a dead gateway cannot stall it.

export type MountConnection = 'connecting' | 'open' | 'closed'

export const SESSION_TIMEOUT_MS = 3_000
export const OPEN_TIMEOUT_MS = 5_000
export const MAX_BACKOFF_MS = 5_000

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
      }
      sock.onclose = (ev) => {
        if (openTimer) clearTimeout(openTimer)
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
  }

  return { start, stop }
}
