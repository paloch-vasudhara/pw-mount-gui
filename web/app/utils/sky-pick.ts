// Picking and label declutter for the Sky Viewer (T2.3). Pure, allocation-free in the hot paths.

/** Screen positions of the markers a layer drew last, in reusable typed arrays (chart CSS px). */
export interface ScreenLayer {
  n: number
  x: Float32Array
  y: Float32Array
  az: Float32Array
  alt: Float32Array
  norad: Int32Array
}

export function makeScreenLayer(): ScreenLayer {
  return {
    n: 0,
    x: new Float32Array(0),
    y: new Float32Array(0),
    az: new Float32Array(0),
    alt: new Float32Array(0),
    norad: new Int32Array(0),
  }
}

/** Grow (never shrink) so the layer can hold `cap` markers. Contents are not preserved. */
export function ensureScreenCapacity(l: ScreenLayer, cap: number): void {
  if (l.x.length >= cap) return
  l.x = new Float32Array(cap)
  l.y = new Float32Array(cap)
  l.az = new Float32Array(cap)
  l.alt = new Float32Array(cap)
  l.norad = new Int32Array(cap)
}

export interface PickResult {
  /** Index into `layers` of the winning layer, -1 when nothing was hit. */
  layer: number
  index: number
  norad: number
  dist2: number
}

export function makePickResult(): PickResult {
  return { layer: -1, index: -1, norad: 0, dist2: 0 }
}

/**
 * Nearest drawn marker to (px, py) within `radius` px, across all `layers` (the caller passes only
 * the visible ones; a hidden layer has n = 0). Never the topmost: strictly the closest wins.
 * Ties keep the first. Fills `out` and returns true on a hit.
 */
export function pickNearest(
  layers: ReadonlyArray<ScreenLayer>,
  px: number,
  py: number,
  radius: number,
  out: PickResult,
): boolean {
  let best = radius * radius
  out.layer = -1
  out.index = -1
  for (let li = 0; li < layers.length; li++) {
    const l = layers[li]!
    const xs = l.x
    const ys = l.y
    for (let i = 0; i < l.n; i++) {
      const dx = xs[i]! - px
      const dy = ys[i]! - py
      const d2 = dx * dx + dy * dy
      if (d2 <= best && (out.layer < 0 || d2 < best)) {
        best = d2
        out.layer = li
        out.index = i
        out.norad = l.norad[i]!
        out.dist2 = d2
      }
    }
  }
  return out.layer >= 0
}

/** Index of `norad` in the layer's drawn markers, or -1. */
export function indexOfNorad(l: ScreenLayer, norad: number): number {
  for (let i = 0; i < l.n; i++) if (l.norad[i] === norad) return i
  return -1
}

/**
 * Uniform-grid occupancy of axis-aligned boxes for greedy label placement: `add` registers a box
 * (a marker or a placed label), `overlaps` asks whether a candidate box touches any registered one.
 * All storage is reused between frames (`reset` only clears the cell heads).
 */
export class BoxGrid {
  private cell: number
  private cols = 0
  private rows = 0
  private heads = new Int32Array(0)
  private boxes = new Float32Array(0) // x0 y0 x1 y1 per box
  private nBoxes = 0
  private entryBox = new Int32Array(0)
  private entryNext = new Int32Array(0)
  private nEntries = 0

  constructor(cell = 32) {
    this.cell = cell
  }

  reset(width: number, height: number): void {
    const cols = Math.max(1, Math.ceil(width / this.cell))
    const rows = Math.max(1, Math.ceil(height / this.cell))
    if (cols * rows !== this.heads.length) this.heads = new Int32Array(cols * rows)
    this.cols = cols
    this.rows = rows
    this.heads.fill(-1)
    this.nBoxes = 0
    this.nEntries = 0
  }

  private cx(v: number): number {
    const c = Math.floor(v / this.cell)
    return c < 0 ? 0 : c >= this.cols ? this.cols - 1 : c
  }

  private cy(v: number): number {
    const c = Math.floor(v / this.cell)
    return c < 0 ? 0 : c >= this.rows ? this.rows - 1 : c
  }

  add(x0: number, y0: number, x1: number, y1: number): void {
    if (this.nBoxes * 4 + 4 > this.boxes.length) {
      const nb = new Float32Array(Math.max(1024, this.boxes.length * 2))
      nb.set(this.boxes)
      this.boxes = nb
    }
    const b = this.nBoxes++
    this.boxes[4 * b] = x0
    this.boxes[4 * b + 1] = y0
    this.boxes[4 * b + 2] = x1
    this.boxes[4 * b + 3] = y1
    const c0 = this.cx(x0)
    const c1 = this.cx(x1)
    const r0 = this.cy(y0)
    const r1 = this.cy(y1)
    for (let r = r0; r <= r1; r++) {
      for (let c = c0; c <= c1; c++) {
        if (this.nEntries >= this.entryBox.length) {
          const n = Math.max(4096, this.entryBox.length * 2)
          const eb = new Int32Array(n)
          const en = new Int32Array(n)
          eb.set(this.entryBox)
          en.set(this.entryNext)
          this.entryBox = eb
          this.entryNext = en
        }
        const e = this.nEntries++
        const h = r * this.cols + c
        this.entryBox[e] = b
        this.entryNext[e] = this.heads[h]!
        this.heads[h] = e
      }
    }
  }

  overlaps(x0: number, y0: number, x1: number, y1: number): boolean {
    const c0 = this.cx(x0)
    const c1 = this.cx(x1)
    const r0 = this.cy(y0)
    const r1 = this.cy(y1)
    const boxes = this.boxes
    for (let r = r0; r <= r1; r++) {
      for (let c = c0; c <= c1; c++) {
        for (let e = this.heads[r * this.cols + c]!; e >= 0; e = this.entryNext[e]!) {
          const b = 4 * this.entryBox[e]!
          if (x0 < boxes[b + 2]! && x1 > boxes[b]! && y0 < boxes[b + 3]! && y1 > boxes[b + 1]!) return true
        }
      }
    }
    return false
  }
}
