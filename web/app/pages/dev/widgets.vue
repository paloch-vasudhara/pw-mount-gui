<script setup lang="ts">
// Dev-only widget harness (unlinked). Every widget has a data-testid and a live
// JSON readout of its model (`<testid>-out`), for headless-browser driving.
import AltitudeField from '~/components/widgets/AltitudeField.vue'
import AngleField from '~/components/widgets/AngleField.vue'
import DateTimeField from '~/components/widgets/DateTimeField.vue'
import FormatSelect from '~/components/widgets/FormatSelect.vue'
import NumberField from '~/components/widgets/NumberField.vue'
import SegToggle from '~/components/widgets/SegToggle.vue'
import type { AltUnit } from '~/utils/altitude'
import { fmtThousands } from '~/utils/format'

const az = ref(157.2349)
const elSigned = ref(51.41)
const elCapped = ref(51.41)
const dec = ref(-28.2625)
const ra = ref(5.3723)
const lat = ref(13.786)
const lon = ref(101.464)
const altM = ref(1500)
const altUnit = ref<AltUnit>('m')
const num = ref(-1.5)
const seg = ref('stop')
const fmt = ref('dd')
const dt = ref<Date | null>(new Date(Date.UTC(2026, 9, 19, 8, 30, 15)))
const invalidEl = ref(-28.7)

const segOptions = [
  { value: 'stop', label: 'Stop' },
  { value: 'tracking', label: 'Tracking' },
]
const fmtOptions = [
  { value: 'dd', label: 'DD' },
  { value: 'dms', label: 'DMS' },
]
const out = (v: unknown) => JSON.stringify(v instanceof Date ? v.toISOString() : v)
</script>

<template>
  <main class="min-h-screen bg-page p-8">
    <h1 class="mb-4 text-xl font-semibold">Widget harness</h1>
    <div class="grid max-w-[1100px] grid-cols-1 gap-x-10 gap-y-3 lg:grid-cols-2">
      <section>
        <AngleField v-model="az" data-testid="w-az" kind="angle" label="Az" :range="[0, 360]" :step="0.0001" wide />
        <pre data-testid="w-az-out" class="out">{{ out(az) }}</pre>
      </section>

      <section>
        <AngleField v-model="elSigned" data-testid="w-el" kind="angle" label="El" signed :step="0.0001" wide />
        <pre data-testid="w-el-out" class="out">{{ out(elSigned) }}</pre>
      </section>

      <section>
        <AngleField
          v-model="elCapped" data-testid="w-el-capped" kind="angle" label="El 0-90" signed
          :range="[0, 90]" :step="0.0001" wide
        />
        <pre data-testid="w-el-capped-out" class="out">{{ out(elCapped) }}</pre>
      </section>

      <section>
        <AngleField
          v-model="dec" data-testid="w-dec" kind="angle" label="Dec" signed default-format="dms"
          :step="0.0001" wide
        />
        <pre data-testid="w-dec-out" class="out">{{ out(dec) }}</pre>
      </section>

      <section>
        <AngleField v-model="ra" data-testid="w-ra" kind="ra" label="RA" default-format="hms" :step="0.0001" wide />
        <pre data-testid="w-ra-out" class="out">{{ out(ra) }}</pre>
      </section>

      <section>
        <AngleField
          v-model="lat" data-testid="w-lat" kind="latlon" label="Latitude" :cardinal="['N', 'S']"
          :range="[-90, 90]" :step="0.0001" wide
        />
        <pre data-testid="w-lat-out" class="out">{{ out(lat) }}</pre>
      </section>

      <section>
        <AngleField
          v-model="lon" data-testid="w-lon" kind="latlon" label="Longitude" :cardinal="['E', 'W']"
          :range="[-180, 180]" :step="0.0001" wide
        />
        <pre data-testid="w-lon-out" class="out">{{ out(lon) }}</pre>
      </section>

      <section>
        <AngleField
          v-model="invalidEl" data-testid="w-el-invalid" kind="angle" label="El (invalid)" signed invalid
          :step="0.0001" wide
        />
        <pre data-testid="w-el-invalid-out" class="out">{{ out(invalidEl) }}</pre>
      </section>

      <section>
        <AltitudeField v-model="altM" v-model:unit="altUnit" data-testid="w-alt" />
        <pre data-testid="w-alt-out" class="out">{{ out({ metres: altM, unit: altUnit, fmt: fmtThousands(altM) }) }}</pre>
      </section>

      <section>
        <div class="form-row">
          <label>Signed number</label>
          <NumberField v-model="num" data-testid="w-num" signed :step="0.1" />
        </div>
        <pre data-testid="w-num-out" class="out">{{ out(num) }}</pre>
      </section>

      <section>
        <SegToggle v-model="seg" data-testid="w-seg" :options="segOptions" />
        <pre data-testid="w-seg-out" class="out">{{ out(seg) }}</pre>
      </section>

      <section>
        <FormatSelect v-model="fmt" data-testid="w-fmt" :options="fmtOptions" :width="60" />
        <pre data-testid="w-fmt-out" class="out">{{ out(fmt) }}</pre>
      </section>

      <section class="lg:col-span-2">
        <div class="form-row">
          <label>UTC time</label>
          <DateTimeField v-model="dt" data-testid="w-dt" label="UTC time" />
        </div>
        <pre data-testid="w-dt-out" class="out">{{ out(dt) }}</pre>
      </section>
    </div>
  </main>
</template>

<style scoped>
.out {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--muted-foreground);
}
</style>
