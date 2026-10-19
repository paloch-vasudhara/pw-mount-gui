// Ported from ssa-toolkit web/app/composables/useSignGroup.ts @84f2ded
// app/composables/useSignGroup.ts — the shared sign-group registry (T10,
// §4b/T10), replacing dms-input.js's module-level `SIGN_GROUPS` Map with the
// same key convention but Vue-native: a shared `ref` held in a module-scope
// Map, so any two widgets sharing a `signGroup` id re-render off the SAME
// reactive source instead of an explicit listener callback.
//
// DEVIATION (recorded per the design doc's instruction — "this is the one
// place where the mechanism legitimately changes; the observable behaviour
// must not"): the legacy public/silent split (`_publicSetSign` fires
// listeners, `_silentSetSign` does not, to avoid an owner calling back into
// itself) COLLAPSES here. A shared `ref` propagates to every reader by Vue's
// own reactivity; the loop the split existed to prevent cannot occur because
// a value owner (DmsTriad/DdInput) WRITES the ref directly rather than
// invoking a callback that re-enters itself. Observable behaviour (flipping
// one chip flips every chip in the group; value owners re-render with the
// new sign) is unchanged.

import { ref, type Ref } from 'vue'

interface SignGroupEntry {
  sign: Ref<1 | -1>
  cardinal: [string, string] | null
}

const REGISTRY = new Map<string, SignGroupEntry>()

function entry(id: string, cardinal: [string, string] | null = null): SignGroupEntry {
  let e = REGISTRY.get(id)
  if (!e) {
    e = { sign: ref<1 | -1>(1), cardinal }
    REGISTRY.set(id, e)
  } else if (cardinal && !e.cardinal) {
    e.cardinal = cardinal
  }
  return e
}

export function useSignGroup(id: string, cardinal: [string, string] | null = null) {
  const e = entry(id, cardinal)
  return {
    sign: e.sign,
    cardinal: e.cardinal,
    flip(): void { e.sign.value = e.sign.value === 1 ? -1 : 1 },
    setSign(s: 1 | -1): void { e.sign.value = s },
  }
}

// ── Public accessors (observer-view format-toggle, T16) ────────────────────
export function hasSignGroup(id: string): boolean { return REGISTRY.has(id) }
export function getSign(id: string): 1 | -1 { return REGISTRY.get(id)?.sign.value ?? 1 }
export function setSign(id: string, newSign: 1 | -1): void {
  const e = REGISTRY.get(id)
  if (e) e.sign.value = newSign
}

// Test-only: clear the module-scope registry between specs.
export function _resetSignGroupsForTests(): void { REGISTRY.clear() }
