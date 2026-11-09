// CCTV feed state machine (T5.7), framework-free so it is unit-tested with fake timers.
//
// start() opens the stream (ticket -> openStream) and waits for the player to report live. A
// failure (player error, no picture in time, or the gateway reporting the camera unreachable)
// closes the stream and retries with a 2 -> 30 s backoff. After 3 failed attempts a snapshot is
// shown and refreshed every 5 s (the stream keeps retrying in the background and takes over
// again when it works). stop() closes everything: streams are open only while the feed is shown.
import {
  backoffMs, HEALTH_INTERVAL_MS, OPEN_TIMEOUT_MS, SNAPSHOT_AFTER_FAILURES, SNAPSHOT_INTERVAL_MS,
  type FeedPhase,
} from '~/utils/cctv'

export interface FeedIO {
  fetchTicket: () => Promise<string>
  /** Attach the stream (img src / hls.js) for this ticket. */
  openStream: (ticket: string) => void
  /** Detach it, which closes the connection to the gateway. */
  closeStream: () => void
  /** Point the snapshot image at a fresh frame. */
  showSnapshot: (ticket: string, n: number) => void
  /** The gateway's view of the camera: 'unreachable' means the running stream has died. */
  fetchState: () => Promise<string>
  /** Called after every change of phase / snapshotOk. */
  changed: () => void
}

export class CctvFeed {
  phase: FeedPhase = 'connecting'
  snapshotOk = false
  failures = 0

  private running = false
  private run = 0 // bumps on every start/stop: stale async results are ignored
  private openTimer: ReturnType<typeof setTimeout> | null = null
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private snapTimer: ReturnType<typeof setInterval> | null = null
  private healthTimer: ReturnType<typeof setInterval> | null = null
  private snapCount = 0

  constructor(private readonly io: FeedIO) {}

  start(): void {
    if (this.running) return
    this.running = true
    this.run++
    this.failures = 0
    this.setPhase('connecting')
    void this.attempt()
  }

  stop(): void {
    this.running = false
    this.run++
    this.clearTimers(false)
    this.io.closeStream()
    this.snapshotOk = false
    this.phase = 'connecting'
    this.io.changed()
  }

  /** The player shows pictures. */
  notifyLive(): void {
    if (!this.running || this.phase === 'live') return
    this.clearTimers(false)
    this.failures = 0
    this.setPhase('live')
    this.healthTimer = setInterval(() => void this.checkHealth(), HEALTH_INTERVAL_MS)
  }

  /** The player failed (img error, fatal hls.js error). */
  notifyError(): void {
    if (this.running) this.fail()
  }

  notifySnapshot(ok: boolean): void {
    if (!this.running || this.snapshotOk === ok) return
    this.snapshotOk = ok
    this.io.changed()
  }

  private setPhase(p: FeedPhase): void {
    this.phase = p
    this.io.changed()
  }

  private async attempt(): Promise<void> {
    const run = this.run
    let ticket: string
    try {
      ticket = await this.io.fetchTicket()
    }
    catch {
      if (run === this.run) this.fail()
      return
    }
    if (run !== this.run || !this.running) return
    this.io.openStream(ticket)
    this.openTimer = setTimeout(() => this.fail(), OPEN_TIMEOUT_MS)
  }

  private async checkHealth(): Promise<void> {
    const run = this.run
    try {
      const state = await this.io.fetchState()
      if (run === this.run && this.phase === 'live' && state === 'unreachable') this.fail()
    }
    catch {
      // the status poll failing alone says nothing about the picture
    }
  }

  private fail(): void {
    if (!this.running) return
    this.clearTimers(true)
    this.io.closeStream()
    this.failures++
    if (this.failures >= SNAPSHOT_AFTER_FAILURES) this.enterSnapshot()
    else this.setPhase('retrying')
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null
      void this.attempt()
    }, backoffMs(this.failures))
  }

  private enterSnapshot(): void {
    if (this.phase !== 'snapshot') {
      this.setPhase('snapshot')
      this.snapshotOk = false
      void this.takeSnapshot()
      this.snapTimer = setInterval(() => void this.takeSnapshot(), SNAPSHOT_INTERVAL_MS)
    }
  }

  private async takeSnapshot(): Promise<void> {
    const run = this.run
    try {
      const ticket = await this.io.fetchTicket()
      if (run === this.run && this.phase === 'snapshot') this.io.showSnapshot(ticket, ++this.snapCount)
    }
    catch {
      if (run === this.run) this.notifySnapshot(false)
    }
  }

  /** keepSnapshot: a failure while showing snapshots leaves the snapshot refresh running. */
  private clearTimers(keepSnapshot: boolean): void {
    if (this.openTimer) clearTimeout(this.openTimer)
    if (this.healthTimer) clearInterval(this.healthTimer)
    this.openTimer = this.healthTimer = null
    if (this.retryTimer) clearTimeout(this.retryTimer)
    this.retryTimer = null
    if (!keepSnapshot) {
      if (this.snapTimer) clearInterval(this.snapTimer)
      this.snapTimer = null
    }
  }
}
