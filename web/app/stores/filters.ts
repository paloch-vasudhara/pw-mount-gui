// Visibility filters (A2a), persisted per viewer in localStorage (always try/catch).
import { defineStore } from 'pinia'
import { computed, reactive, watch } from 'vue'
import {
  DEFAULT_FILTERS, effectiveVisibility, FILTERS_STORAGE_KEY, parseFilters, serializeFilters,
  type FilterState,
} from '../utils/filters'

function load(): FilterState {
  try {
    return parseFilters(localStorage.getItem(FILTERS_STORAGE_KEY))
  }
  catch {
    return { ...DEFAULT_FILTERS }
  }
}

export const useFiltersStore = defineStore('filters', () => {
  const filters = reactive<FilterState>(load())
  const visibility = computed(() => effectiveVisibility(filters))
  watch(filters, () => {
    try {
      localStorage.setItem(FILTERS_STORAGE_KEY, serializeFilters({ ...filters }))
    }
    catch {
      // storage unavailable: the choice just is not remembered
    }
  })
  return { filters, visibility }
})
