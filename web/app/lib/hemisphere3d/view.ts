// HemisphereView: one self-contained three.js scene (own renderer, camera, controls, render
// loop) mounted into a container element. Ported from scope-pointer/src/main.js; the mock
// source/panel are gone, inputs arrive through setInputs(). Render-on-demand: a frame is drawn
// only on a meaningful data change, while display smoothing is settling, or on orbit interaction.
// dispose() frees every GPU resource and force-releases the WebGL context.

import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'

import { altAzToDir, angularSep, shortestDelta, wrap360 } from './coords'
import { DISPLAY_TAU } from './config'
import { createHemisphere } from './hemisphere'
import { createMount } from './mount'
import { createCelestial } from './celestial'
import { createPointer } from './pointer'
import { EMPTY_INPUTS, type AltAzDeg, type HemisphereInputs } from './inputs'

export type HemisphereMode = 'compact' | 'expanded'

// Pose / body changes below these are invisible (sub-pixel) and must not wake the render loop.
const POSE_EPS_DEG = 0.02
const BODY_EPS_DEG = 0.1
const SETTLE_EPS_DEG = 1e-3

const VIEW = { target: [0, 52, 0] as const, dist: 390, alt: 27, az: 160 }
const EXPANDED_BACKDROP = 0x08090b
const DEFAULT_POSE: AltAzDeg = { alt: 45, az: 135 }

const differs = (a: AltAzDeg | null, b: AltAzDeg | null, eps: number): boolean => {
  if (a === null || b === null) return a !== b
  return Math.abs(shortestDelta(a.az, b.az)) > eps || Math.abs(a.alt - b.alt) > eps
}

export class HemisphereView {
  private readonly container: HTMLElement
  private readonly renderer: THREE.WebGLRenderer
  private readonly scene = new THREE.Scene()
  private readonly camera = new THREE.PerspectiveCamera(38, 1, 1, 3000)
  private readonly controls: OrbitControls
  private readonly hemi = createHemisphere()
  private readonly mount = createMount()
  private readonly celestial = createCelestial()
  private readonly pointer = createPointer()
  private readonly resizeObserver: ResizeObserver

  private inputs: HemisphereInputs = EMPTY_INPUTS
  private pose: AltAzDeg = { ...DEFAULT_POSE } // latest reported (target of the smoothing)
  private disp: AltAzDeg = { ...DEFAULT_POSE } // smoothed, drawn
  private havePose = false
  private pointerVisible = false
  private shownTarget: AltAzDeg | null = null
  private shownSun: AltAzDeg | null = null
  private shownMoon: AltAzDeg | null = null
  private shownLimEl: number | null = null
  private shownKeepout = -1
  private limElDirty = true

  private raf = 0
  private last = 0
  private framesRendered = 0
  private viewFitted = false
  private disposed = false
  private readonly viewDist: number

  constructor(container: HTMLElement, mode: HemisphereMode) {
    this.container = container
    const compact = mode === 'compact'
    this.viewDist = compact ? VIEW.dist * 0.9 : VIEW.dist
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: compact, preserveDrawingBuffer: false })
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    this.renderer.localClippingEnabled = true
    if (compact) this.renderer.setClearColor(0x000000, 0)
    else this.scene.background = new THREE.Color(EXPANDED_BACKDROP)
    const canvas = this.renderer.domElement
    canvas.style.display = 'block'
    canvas.style.width = '100%'
    canvas.style.height = '100%'
    container.appendChild(canvas)
    container.dataset.frames = '0'

    const controls = new OrbitControls(this.camera, canvas)
    controls.enableDamping = true
    controls.dampingFactor = 0.12
    controls.minDistance = 45
    controls.maxDistance = 520
    controls.maxPolarAngle = Math.PI * 0.495
    this.controls = controls

    const { scene } = this
    scene.add(new THREE.HemisphereLight(0xDFE8FF, 0x2A2A22, 1.5))
    const key = new THREE.DirectionalLight(0xFFFFFF, 2.2)
    key.position.set(90, 170, 130)
    scene.add(key)
    const fill = new THREE.DirectionalLight(0xBFD0FF, 0.7)
    fill.position.set(-120, 60, -90)
    scene.add(fill)
    scene.add(this.hemi.object, this.mount.object, this.celestial.object, this.pointer.object)
    this.pointer.object.visible = false

    controls.addEventListener('change', this.requestFrame)

    this.resizeObserver = new ResizeObserver(() => this.resize())
    this.resizeObserver.observe(container)
    this.resize()
    this.requestFrame()
  }

  /** Number of frames actually rendered (also mirrored to `data-frames` on the container). */
  get frames(): number {
    return this.framesRendered
  }

  setInputs(next: HemisphereInputs): void {
    if (this.disposed) return
    let changed = false

    if (next.mount === null) {
      if (this.pointerVisible) { this.pointerVisible = false; changed = true }
    }
    else {
      if (!this.pointerVisible) { this.pointerVisible = true; changed = true }
      if (!this.havePose) {
        this.havePose = true
        this.pose = { ...next.mount }
        this.disp = { ...next.mount } // first real data: snap, don't sweep from the default pose
        changed = true
      }
      else if (differs(this.pose, next.mount, POSE_EPS_DEG)) {
        this.pose = { ...next.mount }
        changed = true
      }
    }

    if (differs(this.shownSun, next.sun, BODY_EPS_DEG)) { this.shownSun = next.sun; changed = true }
    if (differs(this.shownMoon, next.moon, BODY_EPS_DEG)) { this.shownMoon = next.moon; changed = true }
    if (differs(this.shownTarget, next.target, BODY_EPS_DEG)) { this.shownTarget = next.target; changed = true }
    if (next.limElDeg !== this.shownLimEl) { this.shownLimEl = next.limElDeg; this.limElDirty = true; changed = true }
    if (next.keepoutDeg !== this.shownKeepout) { this.shownKeepout = next.keepoutDeg; changed = true }

    this.inputs = next
    if (changed) this.requestFrame()
  }

  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    if (this.raf) cancelAnimationFrame(this.raf)
    this.raf = 0
    this.resizeObserver.disconnect()
    this.controls.removeEventListener('change', this.requestFrame)
    this.controls.dispose()

    const seen = new Set<unknown>()
    const disposeMaterial = (m: THREE.Material) => {
      if (seen.has(m)) return
      seen.add(m)
      for (const v of Object.values(m)) {
        if (v instanceof THREE.Texture) v.dispose()
      }
      if (m instanceof THREE.ShaderMaterial) {
        for (const u of Object.values(m.uniforms)) {
          if (u.value instanceof THREE.Texture) u.value.dispose()
        }
      }
      m.dispose()
    }
    this.scene.traverse((o) => {
      const obj = o as THREE.Mesh
      if (obj.geometry && !seen.has(obj.geometry)) {
        seen.add(obj.geometry)
        obj.geometry.dispose()
      }
      const mat = obj.material as THREE.Material | THREE.Material[] | undefined
      if (Array.isArray(mat)) mat.forEach(disposeMaterial)
      else if (mat) disposeMaterial(mat)
    })
    this.scene.clear()

    const canvas = this.renderer.domElement
    this.renderer.dispose()
    this.renderer.forceContextLoss()
    canvas.remove()
  }

  // --- internals ---------------------------------------------------------------------------

  private readonly requestFrame = (): void => {
    if (this.disposed || this.raf) return
    this.raf = requestAnimationFrame(this.frame)
  }

  private fitView(w: number, h: number): void {
    // Initial overview camera, pulled back for narrow containers so the whole dome stays in view.
    const aspect = w / h
    const dist = this.viewDist * Math.max(1, 1.15 / aspect)
    const target = new THREE.Vector3(...VIEW.target)
    const [x, y, z] = altAzToDir(VIEW.alt, VIEW.az)
    this.camera.position.set(x, y, z).multiplyScalar(dist).add(target)
    this.controls.target.copy(target)
    this.controls.update()
  }

  private resize(): void {
    if (this.disposed) return
    const { clientWidth: w, clientHeight: h } = this.container
    if (!w || !h) return
    this.renderer.setSize(w, h, false)
    this.camera.aspect = w / h
    this.camera.updateProjectionMatrix()
    if (!this.viewFitted) {
      this.viewFitted = true
      this.fitView(w, h)
    }
    this.requestFrame()
  }

  /** Smooth the drawn pose toward the reported one; returns true while still settling. */
  private updateView(dt: number): boolean {
    const k = 1 - Math.exp(-dt / DISPLAY_TAU)
    const dAz = shortestDelta(this.disp.az, this.pose.az)
    const dAlt = this.pose.alt - this.disp.alt
    this.disp.az = wrap360(this.disp.az + dAz * k)
    this.disp.alt += dAlt * k
    const settling = Math.abs(dAz) > SETTLE_EPS_DEG || Math.abs(dAlt) > SETTLE_EPS_DEG
    if (!settling) {
      this.disp.az = this.pose.az
      this.disp.alt = this.pose.alt
    }

    this.mount.setPose(this.disp.alt, this.disp.az)
    this.pointer.object.visible = this.pointerVisible
    if (this.pointerVisible) {
      const { sun, keepoutDeg } = this.inputs
      const tgt = this.inputs.target
      const danger = sun !== null && angularSep(this.disp.alt, this.disp.az, sun.alt, sun.az) <= keepoutDeg
      const targetDanger = tgt !== null && sun !== null && angularSep(tgt.alt, tgt.az, sun.alt, sun.az) <= keepoutDeg
      this.pointer.update(this.mount.apertureWorld(), this.disp, tgt, {
        showTarget: tgt !== null,
        danger,
        targetDanger,
      })
    }
    return settling
  }

  private readonly frame = (t: number): void => {
    this.raf = 0
    if (this.disposed) return
    const dt = this.last ? Math.min((t - this.last) / 1000, 0.1) : 1 / 60
    this.last = t

    const viewActive = this.updateView(dt)
    const ctlActive = this.controls.update()
    if (this.limElDirty) {
      this.limElDirty = false
      this.hemi.setLimEl(this.inputs.limElDeg)
    }
    // After the camera moves: depth cue + moon limb direction.
    this.celestial.update(this.inputs.sun, this.inputs.moon, this.inputs.keepoutDeg, this.camera.position)

    this.renderer.render(this.scene, this.camera)
    this.framesRendered += 1
    this.container.dataset.frames = String(this.framesRendered)

    if (viewActive || ctlActive) this.requestFrame()
    else this.last = 0
  }
}
