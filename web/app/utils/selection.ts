/** True when the input's WHOLE (non-empty) value is selected. type=number
 *  inputs expose no selectionStart/End (null in Chrome), but
 *  `getSelection().toString()` returns the selected text of a focused input in
 *  Chrome/Firefox, so that is used as the fallback. */
export function isAllSelected(el: HTMLInputElement | null): boolean {
  if (!el || el.value === '') return false
  const a = el.selectionStart
  const b = el.selectionEnd
  if (typeof a === 'number' && typeof b === 'number') return a === 0 && b === el.value.length
  return String(globalThis.getSelection?.() ?? '') === el.value
}
