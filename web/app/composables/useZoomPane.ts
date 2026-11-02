// Zoom / pan for an image pane (T5.2c). Port of ssa-toolkit frontend/src/zoom-pane.js `ZoomPane`: the content
// (w x h px) is contained in the pane, z = 1 is "fit", wheel zooms to the cursor, the +/- buttons zoom about the
// centre, drag pans when zoomed. Overlays are drawn in PANE px through project(), so they ride pan / zoom
// without being scaled. The caller maps content px <-> image px (utils/astrometry previewToImage).
import { computed, onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'

export const ZOOM_MIN = 1
export const ZOOM_MAX = 20
export const ZOOM_STEP = 1.25
const DRAG_PX = 3

export function useZoomPane(paneEl: Ref<HTMLElement | null>, content: Ref<{ w: number, h: number } | null>) {
  const z = ref(1)
  const tx = ref(0)
  const ty = ref(0)
  const paneW = ref(0)
  const paneH = ref(0)
  const dragging = ref(false)
  let moved = false
  let px = 0
  let py = 0
  let ro: ResizeObserver | null = null

  const fit = computed(() => {
    const c = content.value
    if (!c || !c.w || !c.h || !paneW.value || !paneH.value) return { s: 1, baseW: 0, baseH: 0, offX: 0, offY: 0 }
    const s = Math.min(paneW.value / c.w, paneH.value / c.h)
    const baseW = c.w * s
    const baseH = c.h * s
    return { s, baseW, baseH, offX: (paneW.value - baseW) / 2, offY: (paneH.value - baseH) / 2 }
  })

  function measure(): void {
    const el = paneEl.value
    if (!el) return
    paneW.value = el.clientWidth
    paneH.value = el.clientHeight
  }

  /** Content px -> pane px. */
  function project(cx: number, cy: number): { x: number, y: number } {
    const f = fit.value
    return { x: f.offX + tx.value + cx * f.s * z.value, y: f.offY + ty.value + cy * f.s * z.value }
  }

  /** Pane px -> content px. */
  function unproject(x: number, y: number): { cx: number, cy: number } {
    const f = fit.value
    const k = f.s * z.value || 1
    return { cx: (x - f.offX - tx.value) / k, cy: (y - f.offY - ty.value) / k }
  }

  function clamp(): void {
    if (z.value <= ZOOM_MIN) { tx.value = 0; ty.value = 0; return }
    const f = fit.value
    const w = f.baseW * z.value
    const h = f.baseH * z.value
    tx.value = Math.min(-f.offX, Math.max(paneW.value - f.offX - w, tx.value))
    ty.value = Math.min(-f.offY, Math.max(paneH.value - f.offY - h, ty.value))
  }

  function reset(): void {
    z.value = 1; tx.value = 0; ty.value = 0
  }

  function zoomAt(x: number, y: number, factor: number): void {
    const nz = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z.value * factor))
    const k = nz / z.value
    const f = fit.value
    const sx = f.offX + tx.value
    const sy = f.offY + ty.value
    tx.value = x - (x - sx) * k - f.offX
    ty.value = y - (y - sy) * k - f.offY
    z.value = nz
    clamp()
  }

  const zoomBy = (factor: number): void => zoomAt(paneW.value / 2, paneH.value / 2, factor)

  function onWheel(e: WheelEvent): void {
    if (!content.value) return
    const r = paneEl.value!.getBoundingClientRect()
    zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * 0.0015))
  }

  function onDown(e: MouseEvent): void {
    moved = false
    if (z.value <= ZOOM_MIN || !content.value) return
    dragging.value = true
    px = e.clientX; py = e.clientY
    e.preventDefault()
  }

  function onMove(e: MouseEvent): void {
    if (!dragging.value) return
    const dx = e.clientX - px
    const dy = e.clientY - py
    if (!moved && Math.hypot(dx, dy) < DRAG_PX) return
    moved = true
    tx.value += dx; ty.value += dy
    px = e.clientX; py = e.clientY
    clamp()
  }

  const onUp = (): void => { dragging.value = false }

  /** True once after a drag-pan, so the click that ends a drag is not taken as a click on the image. */
  function consumeDrag(): boolean {
    const m = moved
    moved = false
    return m
  }

  onMounted(() => {
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  })
  // the pane lives in a dialog portal: it appears / disappears with the dialog
  watch(paneEl, (el) => {
    ro?.disconnect()
    ro = null
    if (!el) return
    measure()
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(() => { measure(); clamp() })
      ro.observe(el)
    }
  }, { flush: 'post', immediate: true })
  onBeforeUnmount(() => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    ro?.disconnect()
  })

  return { z, tx, ty, paneW, paneH, dragging, fit, measure, project, unproject, reset, zoomAt, zoomBy, onWheel, onDown, consumeDrag }
}
