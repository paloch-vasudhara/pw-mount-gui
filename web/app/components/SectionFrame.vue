<script setup lang="ts">
defineProps<{ title: string }>()
</script>

<template>
  <section class="relative flex min-h-0 flex-col border border-panel-border">
    <h2
      class="absolute -top-px -left-px z-10 bg-chip px-1.5 py-px text-[13px] leading-tight font-semibold tracking-wider text-page uppercase"
    >
      {{ title }}
    </h2>
    <div class="min-h-0 flex-1 p-3 pt-8">
      <slot />
    </div>
  </section>
</template>
