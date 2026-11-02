// The last INITIAL ORBIT DETERMINATION modal state (T5.3, Q34): reopening the modal recalls it; a page
// refresh forgets it (module scope, deliberately not browser storage).
import type { EpochDir, IodResponse, IodRow } from './iod'

export interface IodStatus { kind: 'none' | 'done' | 'error', text: string }

export interface IodModalMemo {
  rows: IodRow[]
  result: IodResponse | null
  stale: boolean
  status: IodStatus
  dir: EpochDir
}

let memo: IodModalMemo | null = null

export const getIodMemo = (): IodModalMemo | null => memo
export function setIodMemo(m: IodModalMemo): void { memo = m }
/** Forget the recalled state (tests). */
export function resetIodMemo(): void { memo = null }
