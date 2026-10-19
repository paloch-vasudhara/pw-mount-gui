// Ported from ssa-toolkit web/tests/unit/widget-angle-field.spec.ts @84f2ded
// @vitest-environment happy-dom
// T12-d (§4c/T12-d): AngleField now renders <FormatSelect> (a reka Select)
// instead of a native <select class="ov-fmt-sel">. reka's SelectContent
// teleports and only renders its option list while OPEN — real open/close/
// click interaction through the portal is exercised by Playwright in
// tests/e2e/shell-visual.spec.ts (c3-formatselect.png), where a real
// browser lays out and measures it. Here, the DOM-level assertions that
// used to `select.setValue(...)` and read `<option>` now assert on
// <FormatSelect>'s PROPS (options/modelValue/disabled/title) and drive
// format switches via its `update:modelValue` emit — the same contract
// AngleField's onSelectChange() consumes either way. This is a recorded
// DEVIATION from the pre-T12-d version of this file, not a silent test
// weakening: the LOCK rule (value inputs disable, the select itself never
// does) and the conversion/toggle logic are still asserted end-to-end.
import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import AngleField from '../../app/components/widgets/AngleField.vue'
import DdInput from '../../app/components/widgets/DdInput.vue'
import DmsTriad from '../../app/components/widgets/DmsTriad.vue'
import FormatSelect from '../../app/components/widgets/FormatSelect.vue'
import { _resetSignGroupsForTests } from '../../app/composables/useSignGroup'
import { FORMATS_FOR, FORMAT_LABEL } from '../../app/utils/angle-format'

beforeEach(() => {
  _resetSignGroupsForTests()
})

function getFormatSelect(wrapper: ReturnType<typeof mount>) {
  return wrapper.getComponent(FormatSelect)
}
async function switchFormat(wrapper: ReturnType<typeof mount>, fmt: string) {
  await getFormatSelect(wrapper).vm.$emit('update:modelValue', fmt)
}

describe('markup parity', () => {
  it('latlon: one FormatSelect, one label-group, two input-groups, exactly one visible', () => {
    const wrapper = mount(AngleField, {
      props: { modelValue: 13.75, kind: 'latlon', label: 'Latitude', wide: true, cardinal: ['N', 'S'], range: [-90, 90] },
    })
    expect(wrapper.findAllComponents(FormatSelect)).toHaveLength(1)
    const labelGroups = wrapper.findAll('span.ov-label-group')
    expect(labelGroups).toHaveLength(1)
    expect(labelGroups[0]!.classes()).toContain('ov-label-group-wide')
    const groups = wrapper.findAll('span.ov-input-group')
    expect(groups).toHaveLength(2)
    const hiddenCount = groups.filter((g) => g.classes().includes('hidden')).length
    expect(hiddenCount).toBe(1)
    const fmtSelect = getFormatSelect(wrapper)
    expect(fmtSelect.props('options').map((o: { value: string }) => o.value)).toEqual(FORMATS_FOR.latlon)
    expect(fmtSelect.props('options').map((o: { label: string }) => o.label)).toEqual(FORMATS_FOR.latlon.map((f) => FORMAT_LABEL[f]))
    expect(fmtSelect.props('title')).toBeTruthy()
    expect(fmtSelect.props('width')).toBe(60) // wide=true
  })

  it('ra: three input-groups, exactly one visible', () => {
    const wrapper = mount(AngleField, {
      props: { modelValue: 6.5, kind: 'ra', label: 'RA' },
    })
    const groups = wrapper.findAll('span.ov-input-group')
    expect(groups).toHaveLength(3)
    const hiddenCount = groups.filter((g) => g.classes().includes('hidden')).length
    expect(hiddenCount).toBe(2)
    const fmtSelect = getFormatSelect(wrapper)
    expect(fmtSelect.props('options').map((o: { value: string }) => o.value)).toEqual(FORMATS_FOR.ra)
  })

  it('non-wide label group lacks the -wide modifier', () => {
    const wrapper = mount(AngleField, { props: { modelValue: 0, kind: 'angle', label: 'Dec', cardinal: ['N', 'S'], range: [-90, 90] } })
    expect(wrapper.get('span.ov-label-group').classes()).not.toContain('ov-label-group-wide')
    // OV UI pass 2 (item 6, GLOBAL) — default 48 -> 58: 48px could never fit
    // the longest format label ("DMS"/"HMS", 3 chars) at this select's real
    // 13px rendered font.
    expect(getFormatSelect(wrapper).props('width')).toBe(58)
  })
})

describe('toggle', () => {
  it('setting format to dms moves hidden to the DD leg, emits update:format once, keeps both children mounted', async () => {
    const wrapper = mount(AngleField, {
      props: { modelValue: 13.75, kind: 'latlon', label: 'Latitude', cardinal: ['N', 'S'], range: [-90, 90] },
    })
    const ddBefore = wrapper.findComponent(DdInput)
    const triadBefore = wrapper.findComponent(DmsTriad)
    expect(ddBefore.exists()).toBe(true)
    expect(triadBefore.exists()).toBe(true)

    await switchFormat(wrapper, 'dms')

    const groups = wrapper.findAll('span.ov-input-group')
    expect(groups[0]!.classes()).toContain('hidden')      // DD leg now hidden
    expect(groups[1]!.classes()).not.toContain('hidden')  // DMS leg now visible
    expect(wrapper.emitted('update:format')).toEqual([['dms']])

    // v-if trap: same component instances, not remounted.
    expect(wrapper.findComponent(DdInput).vm).toStrictEqual(ddBefore.vm)
    expect(wrapper.findComponent(DmsTriad).vm).toStrictEqual(triadBefore.vm)
  })
})

describe('conversion', () => {
  it('ra: switching to dd passes 97.5 to the DD child while the emitted model stays 6.5', async () => {
    const wrapper = mount(AngleField, { props: { modelValue: 6.5, kind: 'ra', label: 'RA' } })
    await switchFormat(wrapper, 'dd')
    const ddChild = wrapper.findAllComponents(DdInput)[1]! // hd child is [0], dd child is [1]
    expect(ddChild.props('modelValue')).toBe(97.5)
    expect(wrapper.emitted('update:modelValue')).toBeFalsy()
  })

  it('ra: switching back to hd emits nothing new (idempotent toggle does not dirty)', async () => {
    const wrapper = mount(AngleField, { props: { modelValue: 6.5, kind: 'ra', label: 'RA', format: 'dd' } })
    await switchFormat(wrapper, 'hd')
    expect(wrapper.emitted('update:modelValue')).toBeFalsy()
  })

  it('latlon: dms then dd round-trips within the 2dp-second grain', async () => {
    let model = 13.756789
    const wrapper = mount(AngleField, {
      props: {
        modelValue: model, kind: 'latlon', label: 'Latitude', cardinal: ['N', 'S'], range: [-90, 90],
        'onUpdate:modelValue': (v: number) => { model = v; wrapper.setProps({ modelValue: v }) },
      },
    })
    await switchFormat(wrapper, 'dms')
    await switchFormat(wrapper, 'dd')
    // Final leg is 'dd', whose own display rounding (+deg.toFixed(4)) is the
    // coarser grain (1e-4 deg), not the DMS leg's 2dp-arcsec grain.
    expect(Math.abs(model - 13.756789)).toBeLessThan(1e-4)
  })
})

describe('sign group', () => {
  it('flipping the DMS leg chip is reflected by the DD leg', async () => {
    const wrapper = mount(AngleField, {
      props: {
        modelValue: 13.75, kind: 'latlon', label: 'Latitude', cardinal: ['N', 'S'], range: [-90, 90],
        signGroup: 'af-lat-test', format: 'dms',
      },
    })
    const chip = wrapper.get('.dms-sign-indicator')
    await chip.trigger('click')
    const ddChip = wrapper.findAll('.dms-sign-indicator')
    // Both DD leg's chip and DMS leg's chip share the group -> both negative.
    for (const c of ddChip) expect(c.classes()).toContain('neg')
  })
})

describe('lock', () => {
  it('LOCK rule: disabled disables every value input in both legs but not the format select', () => {
    const wrapper = mount(AngleField, {
      props: {
        modelValue: 13.75, kind: 'latlon', label: 'Latitude', cardinal: ['N', 'S'], range: [-90, 90],
        disabled: true,
      },
    })
    const inputs = wrapper.findAll('input')
    expect(inputs.length).toBeGreaterThan(0)
    for (const i of inputs) expect(i.attributes('disabled')).toBeDefined()
    // The format select is NEVER disabled by AngleField's `disabled` prop —
    // FormatSelect only receives modelValue/options/width/title, never
    // `disabled`, from AngleField's template.
    expect(getFormatSelect(wrapper).props('disabled')).toBe(false)
  })

  it('switching format while disabled still works', async () => {
    const wrapper = mount(AngleField, {
      props: {
        modelValue: 13.75, kind: 'latlon', label: 'Latitude', cardinal: ['N', 'S'], range: [-90, 90],
        disabled: true,
      },
    })
    await switchFormat(wrapper, 'dms')
    expect(wrapper.emitted('update:format')).toEqual([['dms']])
  })
})

describe('label slot', () => {
  it('no slot: one <label> with the label text', () => {
    const wrapper = mount(AngleField, { props: { modelValue: 0, kind: 'angle', label: 'Dec', cardinal: ['N', 'S'], range: [-90, 90] } })
    const labels = wrapper.findAll('label')
    expect(labels).toHaveLength(1)
    expect(labels[0]!.text()).toBe('Dec')
  })

  it('#label slot: tooltip markup appears verbatim, no second label synthesised', () => {
    const wrapper = mount(AngleField, {
      props: { modelValue: 13.75, kind: 'latlon', cardinal: ['N', 'S'], range: [-90, 90] },
      slots: {
        label: '<label class="tooltip-host">Latitude<span class="tooltip">Geodetic latitude</span></label>',
      },
    })
    const labels = wrapper.findAll('label')
    expect(labels).toHaveLength(1)
    expect(labels[0]!.classes()).toContain('tooltip-host')
    expect(wrapper.find('.tooltip').exists()).toBe(true)
  })
})

describe('uncontrolled format', () => {
  it('toggles via internal ref and still emits update:format without v-model:format', async () => {
    const wrapper = mount(AngleField, { props: { modelValue: 13.75, kind: 'latlon', label: 'Latitude', cardinal: ['N', 'S'], range: [-90, 90] } })
    await switchFormat(wrapper, 'dms')
    expect(wrapper.emitted('update:format')).toEqual([['dms']])
    const groups = wrapper.findAll('span.ov-input-group')
    expect(groups[1]!.classes()).not.toContain('hidden')
  })
})

describe('restriction', () => {
  it('formats=["dd"] offers a single option', () => {
    const wrapper = mount(AngleField, {
      props: { modelValue: 13.75, kind: 'latlon', label: 'Latitude', cardinal: ['N', 'S'], range: [-90, 90], formats: ['dd'] },
    })
    const options = getFormatSelect(wrapper).props('options')
    expect(options).toHaveLength(1)
    expect(options[0].value).toBe('dd')
  })

  it('an out-of-list defaultFormat falls back to formats[0] rather than an empty select', () => {
    const wrapper = mount(AngleField, {
      props: {
        modelValue: 13.75, kind: 'latlon', label: 'Latitude', cardinal: ['N', 'S'], range: [-90, 90],
        formats: ['dd'], defaultFormat: 'dms',
      },
    })
    expect(getFormatSelect(wrapper).props('modelValue')).toBe('dd')
  })
})
