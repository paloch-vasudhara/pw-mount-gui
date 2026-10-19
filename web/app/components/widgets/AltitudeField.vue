<script setup lang="ts">
// Altitude row: NumberField + FormatSelect (FT / M / KM). The model is ALWAYS
// metres; switching the unit converts the DISPLAY only, never the model.
import { computed, ref } from 'vue'
import FormatSelect from './FormatSelect.vue'
import NumberField from './NumberField.vue'
import {
  ALT_LABEL, ALT_STEP, ALT_UNITS, metersToUnit, unitToMeters, type AltUnit,
} from '@/utils/altitude'

const props = withDefaults(defineProps<{
  /** Altitude in METRES. */
  modelValue: number | null
  unit?: AltUnit
  defaultUnit?: AltUnit
  label?: string
  disabled?: boolean
  invalid?: boolean
}>(), {
  unit: undefined,
  defaultUnit: 'm',
  label: 'Altitude',
  disabled: false,
  invalid: false,
})

const emit = defineEmits<{
  'update:modelValue': [metres: number | null]
  'update:unit': [unit: AltUnit]
}>()

const internalUnit = ref<AltUnit>(props.defaultUnit)
const currentUnit = computed<AltUnit>(() => props.unit ?? internalUnit.value)

const display = computed(() =>
  props.modelValue == null ? null : metersToUnit(props.modelValue, currentUnit.value))

const options = ALT_UNITS.map(u => ({ value: u, label: ALT_LABEL[u] }))

function onValue(v: number | null): void {
  emit('update:modelValue', v == null ? null : unitToMeters(v, currentUnit.value))
}
function onUnit(u: string): void {
  internalUnit.value = u as AltUnit
  emit('update:unit', u as AltUnit)
}
</script>

<template>
  <div class="form-row form-row-latlon">
    <span class="ov-label-group ov-label-group-wide">
      <label>{{ label }}</label>
      <FormatSelect
        :model-value="currentUnit"
        :options="options"
        :width="60"
        :title="`${label} unit`"
        :disabled="disabled"
        @update:model-value="onUnit"
      />
    </span>
    <NumberField
      :model-value="display"
      :step="ALT_STEP[currentUnit]"
      :disabled="disabled"
      :invalid="invalid"
      @update:model-value="onValue"
    />
  </div>
</template>
