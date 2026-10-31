<script setup lang="ts">
import { computed } from 'vue'
import { staleTleDays } from '~/utils/tle-stale'

const props = defineProps<{ ageDays: number | null }>()
const days = computed(() => staleTleDays(props.ageDays))
</script>

<template>
  <span v-if="days !== null" class="stale" data-testid="stale-badge" :title="`TLE is ${days} days old: the position may be off`">TLE {{ days }} d</span>
</template>

<style scoped>
.stale { display: inline-block; padding: 0 0.6em; border-radius: 1em; font-size: 0.8em; font-weight: 800; line-height: 1.45; color: #1c1d20; background: #f5a623; white-space: nowrap; }
</style>
