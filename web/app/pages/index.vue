<script setup lang="ts">
import SectionFrame from '~/components/SectionFrame.vue'
</script>

<template>
  <main
    class="grid h-screen min-h-[640px] grid-cols-[minmax(280px,22.9%)_minmax(0,1fr)_minmax(280px,22.9%)] gap-3 bg-page p-5"
  >
    <div class="grid min-h-0 grid-rows-[minmax(0,0.87fr)_minmax(0,1.9fr)] gap-3">
      <SectionFrame title="Track Info" />
      <SectionFrame title="Filters" />
    </div>
    <SectionFrame title="Sky Viewer" />
    <div class="grid min-h-0 grid-rows-[minmax(0,1fr)_minmax(0,1fr)] gap-3">
      <SectionFrame title="Mount Status" />
      <SectionFrame title="Mount Control" />
    </div>
  </main>
</template>
