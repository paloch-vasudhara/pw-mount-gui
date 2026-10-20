// Pointing arrow (aperture -> dome) plus current / target markers on the dome.
// Ported from scope-pointer/src/scene/pointer.js.

import * as THREE from 'three'
import { COLORS } from './config'
import { domePoint } from './hemisphere'

const HEAD_LEN = 6
const HEAD_R = 1.7
const SHAFT_R = 0.5

// Markers are camera-facing sprites: a ring drawn flat on the dome turns edge-on when
// viewed from outside, so billboards stay legible from every orbit angle.
function markerTexture(dashed: boolean): THREE.CanvasTexture {
  const c = document.createElement('canvas')
  c.width = c.height = 128
  const ctx = c.getContext('2d')!
  ctx.strokeStyle = ctx.fillStyle = '#fff'
  ctx.lineCap = 'round'
  if (dashed) {
    ctx.lineWidth = 9
    ctx.setLineDash([16, 13])
    ctx.beginPath()
    ctx.arc(64, 64, 50, 0, Math.PI * 2)
    ctx.stroke()
    ctx.setLineDash([])
    ctx.lineWidth = 7
    ctx.beginPath()
    ctx.moveTo(64, 40)
    ctx.lineTo(64, 88)
    ctx.moveTo(40, 64)
    ctx.lineTo(88, 64)
    ctx.stroke()
  }
  else {
    ctx.lineWidth = 11
    ctx.beginPath()
    ctx.arc(64, 64, 46, 0, Math.PI * 2)
    ctx.stroke()
    ctx.beginPath()
    ctx.arc(64, 64, 13, 0, Math.PI * 2)
    ctx.fill()
  }
  const t = new THREE.CanvasTexture(c)
  t.colorSpace = THREE.SRGBColorSpace
  return t
}

function makeMarker(color: number, dashed = false) {
  const mat = new THREE.SpriteMaterial({ map: markerTexture(dashed), color, transparent: true, depthWrite: false })
  const group = new THREE.Sprite(mat)
  group.scale.setScalar(dashed ? 9 : 7.5)
  group.renderOrder = 2
  return { group, mat }
}

export interface PointerFlags {
  showTarget: boolean
  danger: boolean
  targetDanger: boolean
}

export interface AltAz { alt: number, az: number }

export function createPointer() {
  const object = new THREE.Group()

  const arrowMat = new THREE.MeshStandardMaterial({ color: COLORS.scope, emissive: COLORS.scope, emissiveIntensity: 0.55, roughness: 0.4 })
  const shaftGeo = new THREE.CylinderGeometry(SHAFT_R, SHAFT_R, 1, 12)
  shaftGeo.translate(0, 0.5, 0) // base at origin, extends +Y by 1
  const headGeo = new THREE.ConeGeometry(HEAD_R, HEAD_LEN, 20)
  headGeo.translate(0, -HEAD_LEN / 2, 0) // tip at origin
  const shaft = new THREE.Mesh(shaftGeo, arrowMat)
  const head = new THREE.Mesh(headGeo, arrowMat)
  const arrow = new THREE.Group()
  arrow.add(shaft, head)
  object.add(arrow)

  const current = makeMarker(COLORS.scope)
  const target = makeMarker(COLORS.scopeTarget, true)
  object.add(current.group, target.group)

  // Azimuth guide: meridian arc from the horizon up to the pointing marker (depth cue).
  const GUIDE_STEPS = 48
  const guideGeo = new THREE.BufferGeometry()
  guideGeo.setAttribute('position', new THREE.Float32BufferAttribute(new Float32Array((GUIDE_STEPS + 1) * 3), 3))
  const guideMat = new THREE.LineBasicMaterial({ color: COLORS.scope, transparent: true, opacity: 0.6, depthWrite: false })
  object.add(new THREE.Line(guideGeo, guideMat))
  const gp = new THREE.Vector3()
  const updateGuide = (alt: number, az: number) => {
    const pos = guideGeo.attributes.position as THREE.BufferAttribute
    for (let i = 0; i <= GUIDE_STEPS; i++) {
      domePoint((alt * i) / GUIDE_STEPS, az, 0.996, gp)
      pos.setXYZ(i, gp.x, gp.y, gp.z)
    }
    pos.needsUpdate = true
    guideGeo.computeBoundingSphere()
  }

  const up = new THREE.Vector3(0, 1, 0)
  const p1 = new THREE.Vector3()
  const d = new THREE.Vector3()

  const placeMarker = (m: { group: THREE.Sprite }, alt: number, az: number) => domePoint(alt, az, 0.985, m.group.position)

  return {
    object,
    /**
     * @param aperture world-space arrow origin
     * @param cur displayed pointing
     * @param tgt commanded target (ignored unless flags.showTarget)
     */
    update(aperture: THREE.Vector3, cur: AltAz, tgt: AltAz | null, { showTarget, danger, targetDanger }: PointerFlags) {
      placeMarker(current, cur.alt, cur.az)
      target.group.visible = showTarget && tgt !== null
      if (tgt) placeMarker(target, tgt.alt, tgt.az)
      updateGuide(cur.alt, cur.az)

      domePoint(cur.alt, cur.az, 0.975, p1)
      d.subVectors(p1, aperture)
      const len = d.length()
      arrow.position.copy(aperture)
      arrow.quaternion.setFromUnitVectors(up, d.normalize())
      shaft.scale.y = Math.max(0.01, len - HEAD_LEN)
      head.position.y = len

      const c = danger ? COLORS.danger : COLORS.scope
      arrowMat.color.setHex(c)
      arrowMat.emissive.setHex(c)
      current.mat.color.setHex(c)
      guideMat.color.setHex(c)
      target.mat.color.setHex(targetDanger ? COLORS.danger : COLORS.scopeTarget)
    },
  }
}
