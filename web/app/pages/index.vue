<script setup lang="ts">
import { ref } from 'vue'
import SectionFrame from '~/components/SectionFrame.vue'
import ToolsGroup from '~/components/ToolsGroup.vue'
import MountControlPanel from '~/components/mount/MountControlPanel.vue'
import MountDetailsDialog from '~/components/mount/MountDetailsDialog.vue'
import MountStatusPanel from '~/components/mount/MountStatusPanel.vue'
import SkyChart from '~/components/sky/SkyChart.vue'
import SkyClock from '~/components/sky/SkyClock.vue'
import VisibilityFilters from '~/components/filters/VisibilityFilters.vue'
import TrackCard from '~/components/track/TrackCard.vue'
import { useFiltersStore } from '~/stores/filters'

const detailsOpen = ref(false)
const filters = useFiltersStore()
</script>

<template>
  <main
    class="grid h-screen min-h-[480px] grid-cols-[minmax(280px,22.9%)_minmax(0,1fr)_minmax(280px,22.9%)] gap-3 bg-page p-5"
  >
    <div class="grid min-h-0 grid-rows-[minmax(0,0.87fr)_minmax(0,1.9fr)] gap-3">
      <SectionFrame title="Track Info">
        <TrackCard />
      </SectionFrame>
      <SectionFrame title="Filters">
        <div class="filters-body">
          <div class="filters-inner">
            <VisibilityFilters />
            <ToolsGroup class="tools-anchor" />
          </div>
        </div>
      </SectionFrame>
    </div>
    <SectionFrame title="Sky Viewer">
      <div class="flex h-full min-h-0 flex-col">
        <div class="min-h-0 flex-1">
          <SkyChart
            :show-leo="filters.visibility.showLeo"
            :show-meo="filters.visibility.showMeo"
            :show-geo="filters.visibility.showGeo"
            :show-stars="filters.visibility.showStars"
            :show-bodies="filters.visibility.showBodies"
            :show-labels="filters.visibility.showLabels"
          />
        </div>
        <SkyClock />
      </div>
    </SectionFrame>
    <div class="grid min-h-0 grid-rows-[minmax(0,1fr)_minmax(0,1fr)] gap-3">
      <SectionFrame title="Mount Status">
        <MountStatusPanel @open-details="detailsOpen = true" />
        <MountDetailsDialog v-model:open="detailsOpen" />
      </SectionFrame>
      <SectionFrame title="Mount Control">
        <MountControlPanel homed-dialog />
      </SectionFrame>
    </div>
  </main>
</template>

<style scoped>
/* FILTERS body: sized by the panel height. --u = panel body height / the content height at today's
   sizes (Visibility + Labels row 293.5 + gap 12 + Tools 136.5 = 442, plus slack for the fixed 1 px
   borders); every child clamp(min, u*N, N) hits N at 1920x1080. Tools sits at the bottom. */
.filters-body { container-type: size; height: 100%; min-height: 0; }
.filters-inner {
  --u: calc(100cqh / 450);
  height: 100%; display: flex; flex-direction: column; gap: clamp(4px, calc(var(--u) * 12), 12px);
}
.tools-anchor { margin-top: auto; }
</style>
