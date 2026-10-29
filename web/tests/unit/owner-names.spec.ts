import { describe, expect, it } from 'vitest'
import { OWNER_NAMES, ownerName } from '../../app/utils/owner-names'
import { COMBO_FLAG_MAP, OWNER_TO_ISO } from '../../app/utils/country-flag'
import { filterOwners } from '../../app/utils/track-target'

describe('owner names', () => {
  it('known codes', () => {
    expect(ownerName('US')).toBe('United States')
    expect(ownerName('PRC')).toBe('People\'s Republic of China')
    expect(ownerName('cis')).toContain('Commonwealth')
    expect(ownerName('TBD')).toBe('To be determined')
  })
  it('falls back to the code', () => {
    expect(ownerName('ZZZZ')).toBe('ZZZZ')
    expect(ownerName(null)).toBe('')
  })
  it('covers every flag-map key', () => {
    for (const k of [...Object.keys(OWNER_TO_ISO), ...Object.keys(COMBO_FLAG_MAP)]) expect(OWNER_NAMES[k], k).toBeTruthy()
  })
})

describe('filterOwners', () => {
  const list = ['US', 'PRC', 'CIS', 'UK', 'SKOR']
  it('empty query returns all', () => expect(filterOwners(list, '  ')).toEqual(list))
  it('matches code or name, case-insensitive', () => {
    expect(filterOwners(list, 'kor')).toEqual(['SKOR'])
    expect(filterOwners(list, 'united')).toEqual(['US', 'UK'])
    expect(filterOwners(list, 'people')).toEqual(['PRC'])
    expect(filterOwners(list, 'zzz')).toEqual([])
  })
})
