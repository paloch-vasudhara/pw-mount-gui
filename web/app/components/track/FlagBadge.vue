<script setup lang="ts">
// Flag badge, minimal port of ssa-toolkit components/common/FlagBadge.vue @84f2ded: a combo code
// renders two half flags in one 20x15 box, a plain code one flag, an unknown code nothing.
// (ORG logo branch not ported, see utils/country-flag.ts.) Deviation from ssa: flags are LOCAL
// SVGs in public/flags/<iso>.svg (flag-icons 7.2.3, MIT), no flagcdn.com requests (user 10-21).
import { computed } from 'vue'
import { resolveFlag } from '~/utils/country-flag'

const props = defineProps<{ code: string | null | undefined }>()
const plan = computed(() => resolveFlag(props.code))
const base = useRuntimeConfig().app.baseURL || '/'
const flagUrl = (iso: string) => `${base}flags/${iso.toLowerCase()}.svg`
const hide = (e: Event) => { (e.target as HTMLImageElement).style.display = 'none' }
</script>

<template>
  <span
    v-if="plan?.kind === 'combo'"
    class="flag-combo"
    :title="code ?? ''"
    style="display:inline-block;position:relative;width:20px;height:15px;overflow:hidden;vertical-align:middle;"
  >
    <img
      :src="flagUrl(plan.leftIso)"
      :alt="plan.leftIso"
      style="position:absolute;top:0;width:20px;height:15px;object-fit:cover;left:0;clip-path:inset(0 50% 0 0);"
    >
    <img
      :src="flagUrl(plan.rightIso)"
      :alt="plan.rightIso"
      style="position:absolute;top:0;width:20px;height:15px;object-fit:cover;right:0;clip-path:inset(0 0 0 50%);"
    >
  </span>
  <img
    v-else-if="plan?.kind === 'single'"
    :src="flagUrl(plan.iso)"
    width="20"
    height="15"
    :alt="plan.iso"
    :title="code ?? ''"
    style="object-fit:contain;display:inline-block;vertical-align:middle;"
    @error="hide"
  >
</template>
