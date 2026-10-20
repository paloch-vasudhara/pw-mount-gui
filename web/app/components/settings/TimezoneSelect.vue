<script setup lang="ts">
// Searchable IANA time-zone dropdown (T1.2). Trigger reuses the FormatSelect skin.
import { computed, ref, watch } from 'vue'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

const props = defineProps<{
  modelValue: string
  zones: readonly string[]
  invalid?: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const open = ref(false)
const query = ref('')
const filtered = computed(() => {
  const q = query.value.trim().toLowerCase().replace(/\s+/g, '_')
  return q ? props.zones.filter(z => z.toLowerCase().includes(q)) : props.zones
})

watch(open, (o) => {
  if (o) query.value = ''
})

function pick(z: string): void {
  emit('update:modelValue', z)
  open.value = false
}
</script>

<template>
  <Popover v-model:open="open">
    <PopoverTrigger as-child>
      <button
        type="button"
        class="ov-fmt-sel fmt-select-trigger tz-trigger"
        :class="{ 'is-invalid-box': invalid }"
        data-testid="tz-trigger"
        title="Time zone"
      >
        <span class="fmt-select-text">{{ modelValue || 'Select time zone' }}</span>
      </button>
    </PopoverTrigger>
    <PopoverContent
      align="start"
      class="tz-panel fmt-select-content"
      :style="{ zIndex: 'var(--z-popover)' }"
      data-testid="tz-panel"
    >
      <input
        v-model="query"
        class="ov-dms-box tz-search"
        type="text"
        placeholder="Search time zone"
        data-testid="tz-search"
        autocomplete="off"
        spellcheck="false"
      >
      <div class="tz-list" role="listbox">
        <button
          v-for="z in filtered"
          :key="z"
          type="button"
          role="option"
          class="fmt-select-item tz-item"
          :data-state="z === modelValue ? 'checked' : 'unchecked'"
          :aria-selected="z === modelValue"
          @click="pick(z)"
        >
          {{ z }}
        </button>
        <p v-if="!filtered.length" class="tz-none">
          No match
        </p>
      </div>
    </PopoverContent>
  </Popover>
</template>

<style>
.tz-trigger { width: auto !important; flex: 1 1 0 !important; min-width: 0; text-align: left; margin-right: 0; }
.tz-trigger.is-invalid-box { border-color: var(--danger); color: var(--danger); }
.tz-panel.fmt-select-content { width: 280px; padding: 6px; display: flex; flex-direction: column; gap: 6px; }
.tz-search { width: 100%; padding: 0 6px; }
.tz-list { max-height: 220px; overflow-y: auto; display: flex; flex-direction: column; }
.tz-item { text-align: left; background: transparent; border: 0; width: 100%; }
.tz-item:hover, .tz-item:focus-visible { background: var(--secondary); outline: none; }
.tz-none { padding: 6px 8px; color: var(--muted-foreground); font-size: 13px; }
</style>
