/** Single-space thousand separator: 1234567.891 -> "1 234 567.891". Only the
 *  integer part is grouped; the sign and any decimals are preserved. */
export function fmtThousands(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === '') return ''
  const s = typeof value === 'number' ? String(value) : value.trim()
  const m = /^([+-]?)(\d+)(\.\d+)?$/.exec(s)
  if (!m) return s
  const int = m[2]!.replace(/\B(?=(\d{3})+(?!\d))/g, ' ')
  return `${m[1]}${int}${m[3] ?? ''}`
}
