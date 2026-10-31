// The last Satellite Pass Analysis modal state (user 10-30): reopening the modal recalls it; a page
// refresh forgets it (module scope, deliberately not browser storage).
import type { PassMode, PassResponse, PassSort, PassWindow } from './pass-analysis'

export interface PassModalMemo {
  sat: { norad: number, name: string, ageDays: number | null } | null
  mode: PassMode
  start: Date | null
  end: Date | null
  result: PassResponse | null
  resultMode: PassMode
  resultWin: PassWindow | null
  sort: PassSort
}

let memo: PassModalMemo | null = null

export const getPassModalMemo = (): PassModalMemo | null => memo
export function setPassModalMemo(m: PassModalMemo): void { memo = m }
/** Forget the recalled state (tests). */
export function resetPassModalMemo(): void { memo = null }
