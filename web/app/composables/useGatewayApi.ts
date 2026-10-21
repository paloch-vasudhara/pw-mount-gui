// REST helper for the gateway (T1.2). Gets the per-launch token from /api/mount/session
// (time-limited, like utils/mount-socket.ts: a dead dev proxy never answers) and sends it as
// X-Mount-Token. A 401 (gateway restarted -> new token) refetches the token once.

const SESSION_TIMEOUT_MS = 3_000
const REQUEST_TIMEOUT_MS = 5_000

export class GatewayError extends Error {
  status: number
  detail: unknown
  constructor(status: number, detail: unknown) {
    super(`gateway HTTP ${status}`)
    this.status = status
    this.detail = detail
  }
}

let cachedToken: string | null = null

async function fetchToken(): Promise<string> {
  const res = await fetch('/api/mount/session', {
    cache: 'no-store',
    signal: AbortSignal.timeout(SESSION_TIMEOUT_MS),
  })
  if (!res.ok) throw new GatewayError(res.status, null)
  cachedToken = (await res.json()).token as string
  return cachedToken
}

export function useGatewayApi() {
  async function request<T>(method: string, path: string, body?: unknown, retry = true): Promise<T> {
    const token = cachedToken ?? await fetchToken()
    const res = await fetch(path, {
      method,
      cache: 'no-store',
      headers: {
        'X-Mount-Token': token,
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    })
    if (res.status === 401 && retry) {
      cachedToken = null
      return request<T>(method, path, body, false)
    }
    if (!res.ok) {
      let detail: unknown = null
      try {
        detail = await res.json()
      }
      catch { /* no body */ }
      throw new GatewayError(res.status, detail)
    }
    return await res.json() as T
  }

  return {
    get: <T>(path: string) => request<T>('GET', path),
    put: <T>(path: string, body: unknown) => request<T>('PUT', path, body),
    post: <T>(path: string, body: unknown) => request<T>('POST', path, body),
  }
}
