<script setup lang="ts">
// Detailed Mount Status modal (A4-ii, concept/Detialed Info.png): expanded 3D view on the left,
// live parameters and the live mount-control groups (MountControlPanel, T3.2) on the right.
import { computed } from 'vue'
import MountControlPanel from '@/components/mount/MountControlPanel.vue'
import MountHemisphere3D from '@/components/mount/MountHemisphere3D.vue'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { useMountStore } from '@/stores/mount'
import { buildDetailsDisplay, SUP_H, SUP_M, SUP_S } from '@/utils/mount-details-format'
import type { DisplayField, ValueTone } from '@/utils/mount-format'

const open = defineModel<boolean>('open', { default: false })

const store = useMountStore()
const d = computed(() => buildDetailsDisplay(store.snapshot, store.linkStale))

const TONE: Record<ValueTone, string> = {
  normal: 'text-foreground',
  positive: 'text-accent-green-border',
  danger: 'text-danger',
  dim: 'text-foreground opacity-40',
}

const SUP_MAP: Record<string, string> = { [SUP_H]: 'h', [SUP_M]: 'm', [SUP_S]: 's' }

/** Splits `16ʰ 25ᵐ 43.32ˢ` into text and superscript parts so the unit letters render as <sup>. */
function parts(text: string): Array<{ t: string, sup: boolean }> {
  return text.split(/([ʰᵐˢ])/).filter(Boolean).map(t => (t in SUP_MAP ? { t: SUP_MAP[t]!, sup: true } : { t, sup: false }))
}

interface Row { key: string, label: string, field: DisplayField, title?: string }
const groups = computed<Row[][]>(() => [
  [
    { key: 'mjd', label: 'Modified Julian Date', field: d.value.mjd },
    { key: 'lst', label: 'Local Sidereal Time', field: d.value.lst },
  ],
  [
    { key: 'az', label: 'Azimuth', field: d.value.azimuth },
    { key: 'el', label: 'Elevation', field: d.value.elevation },
    { key: 'ra', label: 'R.A. (J2000)', field: d.value.ra },
    { key: 'dec', label: 'Declination (J2000)', field: d.value.dec },
    { key: 'ha', label: 'Hour Angle', field: d.value.hourAngle },
  ],
  [
    { key: 'status', label: 'Mount Status', field: d.value.status, title: d.value.statusReason ?? undefined },
    { key: 'ax0', label: 'Axis 0 Status', field: d.value.axis0Status },
    { key: 'ax1', label: 'Axis 1 Status', field: d.value.axis1Status },
    { key: 'rms0', label: 'Axis 0 RMS', field: d.value.axis0Rms },
    { key: 'rms1', label: 'Axis 1 RMS', field: d.value.axis1Rms },
    { key: 'sun', label: 'Sun Distance', field: d.value.sunDistance },
  ],
])

</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent
      class="mount-dialog gap-0 p-0"
      data-testid="mount-details-dialog"
    >
      <DialogTitle class="md-chip">
        MOUNT STATUS
      </DialogTitle>
      <DialogDescription class="sr-only">
        Detailed mount status: 3D pointing view and live mount parameters
      </DialogDescription>

      <div class="md-body">
        <div class="md-view" data-testid="mount-details-3d">
          <MountHemisphere3D mode="expanded" />
        </div>

        <div class="md-side">
          <fieldset class="md-group" data-testid="md-parameters">
            <legend>Parameters</legend>
            <div v-for="(g, gi) in groups" :key="gi" class="md-rows">
              <div v-for="r in g" :key="r.key" class="md-row" :data-testid="`md-${r.key}`">
                <span class="md-label">{{ r.label }}</span>
                <span class="md-value" :class="TONE[r.field.tone]" :title="r.title">
                  <i v-if="r.field.dot" class="md-dot" :class="r.field.dot === 'on' ? 'md-dot-on' : 'md-dot-off'" />
                  <template v-for="(p, pi) in parts(r.field.text)" :key="pi">
                    <sup v-if="p.sup">{{ p.t }}</sup>
                    <template v-else>{{ p.t }}</template>
                  </template>
                </span>
              </div>
            </div>
          </fieldset>

          <!-- Same live control as the dashboard MOUNT CONTROL panel (T3.2): one component, so the
               modal and the dashboard can never disagree. It sizes from this wrapper's height. -->
          <div class="md-control" data-testid="md-control">
            <MountControlPanel />
          </div>
        </div>
      </div>
    </DialogContent>
  </Dialog>
</template>

<style>
.mount-dialog { background: var(--page-bg); border: 1px solid var(--panel-border); width: 100%; max-width: min(1240px, calc(100vw - 2rem)); max-height: calc(100vh - 1.5rem); overflow-y: auto; }
.mount-dialog .md-chip { position: absolute; top: -1px; left: -1px; z-index: 10; border: 1px solid var(--panel-border); padding: 1px 6px; font-size: 13px; line-height: 1.25; font-weight: 700; letter-spacing: 0.05em; color: var(--chip-text); text-transform: uppercase; }
.mount-dialog .md-body { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 24px; padding: 44px 24px 24px 24px; }
.mount-dialog .md-view { position: relative; height: min(800px, calc(100vh - 8rem)); min-height: 360px; background: #08090b; overflow: hidden; }
.mount-dialog .md-side { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.mount-dialog .md-group { border: 1px solid var(--panel-border); border-radius: 6px; padding: 8px 16px 10px; margin: 0; min-width: 0; }
.mount-dialog .md-group legend { padding: 0 6px; font-size: 15px; font-weight: 600; color: var(--label-slate); }
.mount-dialog .md-rows + .md-rows { margin-top: 18px; }
.mount-dialog .md-row { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; font-size: 16px; line-height: 28px; }
.mount-dialog .md-label { color: var(--foreground); white-space: nowrap; }
.mount-dialog .md-value { font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; text-align: left; min-width: 118px; }
.mount-dialog .md-value sup { font-size: 0.62em; vertical-align: super; line-height: 0; margin-left: 1px; }
.mount-dialog .md-dot { display: inline-block; width: 12px; height: 12px; border-radius: 9999px; margin-right: 6px; vertical-align: -1px; }
.mount-dialog .md-dot-on { background: var(--accent-green-border); }
.mount-dialog .md-dot-off { background: var(--disabled); }
/* MountControlPanel sizes from its container height; 380 px = its 1920x1080 design height. */
.mount-dialog .md-control { height: 380px; flex: none; }
@media (max-height: 900px) { .mount-dialog .md-row { line-height: 20px; font-size: 15px; } .mount-dialog .md-rows + .md-rows { margin-top: 10px; } .mount-dialog .md-control { height: 300px; } .mount-dialog .md-side { gap: 8px; } .mount-dialog .md-body { padding-top: 36px; } .mount-dialog .md-view { height: min(800px, calc(100vh - 6rem)); } }
@media (max-width: 960px) { .mount-dialog .md-body { grid-template-columns: minmax(0, 1fr); } .mount-dialog .md-view { height: 50vh; } }
</style>
