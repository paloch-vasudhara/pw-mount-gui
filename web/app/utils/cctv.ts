// Mount CCTV view (T5.7): types, per-viewer view memory and the feed state texts (pure, unit-tested).
// The camera's address / login never reach the web: it only learns {configured, type, state}.

export type CctvType = 'mjpeg' | 'hls'
/** What the mount slot shows: the 3D model, the camera, or (expanded only) both halves. */
export type CctvView = '3d' | 'cctv' | 'both'

export interface CctvStatusDto {
  configured: boolean
  type: CctvType
  state: 'not_configured' | 'connecting' | 'live' | 'unreachable'
  error: string | null
  viewers: number
}

/** The feed as the viewer sees it. */
export type FeedPhase = 'connecting' | 'live' | 'retrying' | 'snapshot'

export const VIEW_KEY_PANEL = 'pw-mount-control.cctv-view.panel.v1'
export const VIEW_KEY_EXPANDED = 'pw-mount-control.cctv-view.expanded.v1'

export const BACKOFF_FIRST_MS = 2_000
export const BACKOFF_MAX_MS = 30_000
export const SNAPSHOT_AFTER_FAILURES = 3
export const SNAPSHOT_INTERVAL_MS = 5_000
export const OPEN_TIMEOUT_MS = 15_000
export const HEALTH_INTERVAL_MS = 4_000

export interface ViewOption { value: CctvView, label: string }

/** Toggle options: 3D | CCTV on the panel, 3D | CCTV | Side by side in the expanded dialog. */
export function viewOptions(expanded: boolean): ViewOption[] {
  const base: ViewOption[] = [{ value: '3d', label: '3D' }, { value: 'cctv', label: 'CCTV' }]
  return expanded ? [...base, { value: 'both', label: 'Side by side' }] : base
}

/** The view to show: a remembered choice, but 3D when it is not offered here or CCTV is not configured. */
export function resolveView(stored: string | null | undefined, expanded: boolean, configured: boolean): CctvView {
  if (!configured) return '3d'
  const opt = viewOptions(expanded).find(o => o.value === stored)
  return opt ? opt.value : '3d'
}

export function viewKey(expanded: boolean): string {
  return expanded ? VIEW_KEY_EXPANDED : VIEW_KEY_PANEL
}

export function loadView(expanded: boolean): string | null {
  try {
    return localStorage.getItem(viewKey(expanded))
  }
  catch {
    return null
  }
}

export function saveView(expanded: boolean, view: CctvView): void {
  try {
    localStorage.setItem(viewKey(expanded), view)
  }
  catch {
    // storage unavailable: the choice just is not remembered
  }
}

/** Delay before stream attempt n+1 after n failures (n >= 1): 2 s, 4 s, 8 s ... capped at 30 s. */
export function backoffMs(failures: number): number {
  return Math.min(BACKOFF_FIRST_MS * 2 ** Math.max(0, failures - 1), BACKOFF_MAX_MS)
}

export const TEXT_CONNECTING = 'Connecting…'
export const TEXT_RETRYING = 'CCTV feed unavailable — retrying'

/** The state text over the video area (null = none: the picture speaks for itself). */
export function overlayText(phase: FeedPhase, snapshotOk: boolean): string | null {
  if (phase === 'connecting') return TEXT_CONNECTING
  if (phase === 'retrying') return TEXT_RETRYING
  if (phase === 'snapshot' && !snapshotOk) return TEXT_RETRYING
  return null
}

export function chipText(phase: FeedPhase, snapshotOk: boolean): 'LIVE' | 'SNAPSHOT' | null {
  if (phase === 'live') return 'LIVE'
  if (phase === 'snapshot' && snapshotOk) return 'SNAPSHOT'
  return null
}

export function streamUrl(type: CctvType, ticket: string): string {
  const t = encodeURIComponent(ticket)
  return type === 'hls' ? `/api/mount/cctv/hls/entry.m3u8?t=${t}` : `/api/mount/cctv/mjpeg?t=${t}`
}

export function snapshotUrl(ticket: string, n: number): string {
  return `/api/mount/cctv/snapshot?t=${encodeURIComponent(ticket)}&n=${n}`
}
