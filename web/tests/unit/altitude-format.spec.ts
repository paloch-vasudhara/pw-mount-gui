import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AltitudeField from '../../app/components/widgets/AltitudeField.vue'
import FormatSelect from '../../app/components/widgets/FormatSelect.vue'
import { metersToUnit, unitToMeters } from '../../app/utils/altitude'
import { fmtThousands } from '../../app/utils/format'

describe('altitude conversion', () => {
  it('converts metres <-> ft/km', () => {
    expect(metersToUnit(1000, 'km')).toBe(1)
    expect(metersToUnit(304.8, 'ft')).toBe(1000)
    expect(unitToMeters(1, 'km')).toBe(1000)
    expect(unitToMeters(1000, 'ft')).toBeCloseTo(304.8, 9)
    expect(metersToUnit(12.5, 'm')).toBe(12.5)
  })
})

describe('AltitudeField', () => {
  const input = (w: ReturnType<typeof mount>) => w.find('input').element as HTMLInputElement

  it('model is metres; switching unit converts the display, never the model', async () => {
    const w = mount(AltitudeField, { props: { modelValue: 1500 } })
    expect(input(w).value).toBe('1500')
    w.findComponent(FormatSelect).vm.$emit('update:modelValue', 'ft')
    await w.vm.$nextTick()
    expect(input(w).value).toBe('4921.26')
    w.findComponent(FormatSelect).vm.$emit('update:modelValue', 'km')
    await w.vm.$nextTick()
    expect(input(w).value).toBe('1.5')
    expect(w.emitted('update:modelValue')).toBeUndefined()
    expect(w.emitted('update:unit')!.map(e => e[0])).toEqual(['ft', 'km'])
  })

  it('typing in the selected unit emits metres', async () => {
    const w = mount(AltitudeField, { props: { modelValue: 0, defaultUnit: 'km' } })
    await w.find('input').setValue('2')
    expect(w.emitted('update:modelValue')!.at(-1)![0]).toBe(2000)
    w.findComponent(FormatSelect).vm.$emit('update:modelValue', 'ft')
    await w.vm.$nextTick()
    await w.find('input').setValue('1000')
    expect(w.emitted('update:modelValue')!.at(-1)![0] as number).toBeCloseTo(304.8, 9)
  })
})

describe('fmtThousands', () => {
  it('groups the integer part with single spaces', () => {
    expect(fmtThousands(0)).toBe('0')
    expect(fmtThousands(999)).toBe('999')
    expect(fmtThousands(1000)).toBe('1 000')
    expect(fmtThousands(1234567)).toBe('1 234 567')
    expect(fmtThousands(35786000)).toBe('35 786 000')
  })
  it('keeps sign and decimals untouched', () => {
    expect(fmtThousands(-1234567.891)).toBe('-1 234 567.891')
    expect(fmtThousands('12345.5000')).toBe('12 345.5000')
  })
  it('passes through empties and non-numerics', () => {
    expect(fmtThousands(null)).toBe('')
    expect(fmtThousands(undefined)).toBe('')
    expect(fmtThousands('n/a')).toBe('n/a')
  })
})
