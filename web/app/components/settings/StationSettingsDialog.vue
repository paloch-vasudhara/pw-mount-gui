<script setup lang="ts">
// Station Settings modal (A2b-iii, concept/Settings.png). Cancel / X discards; Save (enabled only when something changed) checks the proposed site
// against PWI4 first, then PUTs. The API key is write-only: the box is always empty on open, "set" is only a placeholder.
// T5.7: the CCTV url / username / password are write-only the same way. The body scrolls inside the dialog (the title and
// the CANCEL / SAVE bar stay put).
import { computed, ref, watch } from 'vue'
import AltitudeField from '@/components/widgets/AltitudeField.vue'
import AngleField from '@/components/widgets/AngleField.vue'
import FormatSelect from '@/components/widgets/FormatSelect.vue'
import NumberField from '@/components/widgets/NumberField.vue'
import SegToggle from '@/components/widgets/SegToggle.vue'
import TimezoneSelect from '@/components/settings/TimezoneSelect.vue'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { GatewayError, useGatewayApi } from '@/composables/useGatewayApi'
import { useCctvStore } from '@/stores/cctv'
import {
  buildPayload, buildSiteCheckBody, formFromDto, isDirty, LIM_EL_MAX, LIM_EL_MIN, PIXEL_UM_MAX, PIXEL_UM_MIN, pcTimeZone, siteWarning, timeZoneList, validateForm,
  type SettingsForm, type SettingsResponse, type SiteCheckDto,
} from '@/utils/station-settings'

const open = defineModel<boolean>('open', { default: false })

const api = useGatewayApi()
const cctvStore = useCctvStore()
const zones = timeZoneList()

const form = ref<SettingsForm | null>(null)
const baseline = ref<SettingsForm | null>(null)
const apiKeySet = ref(false)
const cctvStatus = ref('Not configured')
const loading = ref(false)
const saving = ref(false)
const loadError = ref<string | null>(null)
const saveError = ref<string | null>(null)
const warning = ref<string | null>(null)

const errors = computed(() => (form.value ? validateForm(form.value, zones) : {}))
const valid = computed(() => form.value !== null && Object.keys(errors.value).length === 0)
const dirty = computed(() => form.value !== null && baseline.value !== null && isDirty(form.value, baseline.value))
const canSave = computed(() => valid.value && dirty.value && !saving.value && !loading.value)

const keyPlaceholder = computed(() => {
  if (form.value?.clearKey) return 'key will be cleared'
  return apiKeySet.value ? '•••• set' : 'not set'
})

const ON_OFF = [{ value: 'on', label: 'On' }, { value: 'off', label: 'Off' }]
const CCTV_TYPES = [{ value: 'mjpeg', label: 'MJPEG' }, { value: 'hls', label: 'HLS' }]
const cctvOn = computed({
  get: () => (form.value?.cctvEnabled ? 'on' : 'off'),
  set: (v: string) => { if (form.value) form.value.cctvEnabled = v === 'on' },
})
const cctvKind = computed({
  get: () => form.value?.cctvType ?? 'mjpeg',
  set: (v: string) => { if (form.value) form.value.cctvType = v === 'hls' ? 'hls' : 'mjpeg' },
})

type CctvSecret = 'Url' | 'User' | 'Pass'
function cctvPlaceholder(which: CctvSecret): string {
  const f = form.value
  if (!f) return ''
  if (f[`clearCctv${which}`]) return 'will be cleared'
  return f[`cctv${which}Set`] ? '•••• set' : 'not set'
}
function cctvTyped(which: CctvSecret, v: string): void {
  const f = form.value
  if (!f) return
  f[`cctv${which}`] = v
  if (v !== '') f[`clearCctv${which}`] = false
}
function cctvClear(which: CctvSecret): void {
  const f = form.value
  if (!f) return
  f[`cctv${which}`] = ''
  f[`clearCctv${which}`] = true
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = null
  saveError.value = null
  warning.value = null
  form.value = null
  baseline.value = null
  try {
    const d = await api.get<SettingsResponse>('/api/mount/settings')
    const f = formFromDto(d)
    baseline.value = formFromDto(d)
    // null = never chosen (first run): offer the PC zone. A saved zone (even UTC) is respected.
    if (d.location.timezone == null) {
      const pc = pcTimeZone()
      f.timezone = zones.includes(pc) ? pc : 'UTC'
    }
    form.value = f
    apiKeySet.value = d.api_key_set
    cctvStatus.value = d.cctv?.status ?? 'Not configured'
  }
  catch (e) {
    loadError.value = e instanceof GatewayError
      ? `Gateway refused the request (HTTP ${e.status})`
      : 'Gateway not reachable'
  }
  finally {
    loading.value = false
  }
}

watch(open, (o) => {
  if (o) void load()
  else {
    form.value = null // never keep a typed key around after close
    baseline.value = null
  }
}, { immediate: true })

function close(): void {
  open.value = false
}

// Any Location edit (lat, lon, alt and their formats) clears the mismatch warning, which also
// withdraws the acknowledgement: the next SAVE checks the new location again.
watch(
  () => form.value && [form.value.lat, form.value.lon, form.value.altM, form.value.latFormat, form.value.lonFormat, form.value.altUnit],
  () => { warning.value = null },
)

async function save(): Promise<void> {
  if (!form.value || !canSave.value) return
  saving.value = true
  saveError.value = null
  try {
    // First SAVE checks the PROPOSED location against PWI4 without saving anything. A mismatch
    // shows the warning and stops; SAVE again (warning still showing = acknowledged) goes through.
    if (warning.value === null) {
      const check = await api.post<SiteCheckDto>('/api/mount/site-check', buildSiteCheckBody(form.value))
      const w = siteWarning(check)
      if (w) {
        warning.value = w
        return
      }
    }
    const d = await api.put<SettingsResponse>('/api/mount/settings', buildPayload(form.value))
    apiKeySet.value = d.api_key_set
    void cctvStore.refresh() // the mount panel learns at once whether a camera is configured
    close()
  }
  catch (e) {
    saveError.value = e instanceof GatewayError && e.status === 422
      ? 'The gateway rejected a value; check the fields'
      : 'Could not save settings'
  }
  finally {
    saving.value = false
  }
}

function clearKey(): void {
  if (!form.value) return
  form.value.apiKey = ''
  form.value.clearKey = true
}

function onKeyInput(v: string): void {
  if (!form.value) return
  form.value.apiKey = v
  if (v !== '') form.value.clearKey = false
}

// Row grid (px): [label L][format FMT]2 | 1 | 2[value FIELD]. SPAN = everything after the label column.
const SS_LABEL = 100
const SS_FMT = 60
const SS_FIELD = 162
const SS_SPAN = SS_FMT + 2 + 1 + 2 + SS_FIELD
const SS_VARS = { '--ss-label': `${SS_LABEL}px`, '--ss-fmt': `${SS_FMT}px`, '--ss-field': `${SS_FIELD}px` }

const MOUNTS = [{ value: 'PW L-350', label: 'Planewave L-350' }]
const OTAS = [{ value: 'Celestron RASA11', label: 'Celestron RASA11 Astrograph' }]
const ASTROMETRY = [{ value: 'astrometry.net', label: 'Astrometry.net' }]
const SATDB = [{ value: 'DELV Aerospace', label: 'DELV Aerospace' }]
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent
      class="station-dialog gap-0 p-0"
      :style="SS_VARS"
      data-testid="station-settings-dialog"
    >
      <div class="ss-head">
        <DialogTitle class="ss-title">
          STATION SETTINGS
        </DialogTitle>
        <DialogDescription class="sr-only">
          Station location, hardware and provider settings
        </DialogDescription>
      </div>

      <div v-if="loading" class="ss-state" data-testid="ss-loading">
        Loading settings...
      </div>
      <div v-else-if="loadError" class="ss-state ss-state-err" data-testid="ss-load-error">
        {{ loadError }}
        <Button variant="outline" size="sm" class="ml-3" @click="load">
          RETRY
        </Button>
      </div>

      <form v-else-if="form" class="ss-body" novalidate @submit.prevent="save">
        <div class="ss-scroll app-scroll" data-testid="ss-scroll">
        <div class="ss-grid">
          <fieldset class="ss-group ss-loc" data-testid="ss-location">
            <legend>Location</legend>
            <AngleField
              v-model="form.lat" v-model:format="form.latFormat" data-testid="ss-lat" kind="latlon"
              label="Latitude" :cardinal="['N', 'S']" :range="[-90, 90]" :step="0.0001" wide
              :invalid="!!errors.lat"
            />
            <p v-if="errors.lat" class="ss-err" data-testid="ss-lat-err">
              {{ errors.lat }}
            </p>
            <AngleField
              v-model="form.lon" v-model:format="form.lonFormat" data-testid="ss-lon" kind="latlon"
              label="Longitude" :cardinal="['E', 'W']" :range="[-180, 180]" :step="0.0001" wide
              :invalid="!!errors.lon"
            />
            <p v-if="errors.lon" class="ss-err" data-testid="ss-lon-err">
              {{ errors.lon }}
            </p>
            <AltitudeField
              v-model="form.altM" v-model:unit="form.altUnit" data-testid="ss-alt"
              :invalid="!!errors.alt"
            />
            <p v-if="errors.alt" class="ss-err" data-testid="ss-alt-err">
              {{ errors.alt }}
            </p>
            <div class="form-row form-row-latlon">
              <span class="ov-label-group ov-label-group-wide"><label>Timezone</label></span>
              <TimezoneSelect v-model="form.timezone" :zones="zones" :invalid="!!errors.timezone" />
            </div>
            <p v-if="errors.timezone" class="ss-err" data-testid="ss-tz-err">
              {{ errors.timezone }}
            </p>
          </fieldset>

          <fieldset class="ss-group" data-testid="ss-providers">
            <legend>Providers</legend>
            <div class="form-row">
              <label class="ss-label">Astrometry</label>
              <FormatSelect
                v-model="form.astrometry" :options="ASTROMETRY" :width="SS_SPAN"
                title="Astrometry provider" disabled
              />
            </div>
            <div class="form-row">
              <label class="ss-label">API key</label>
              <span class="ss-key-wrap">
                <input
                  class="ov-dms-box ss-key" type="password" autocomplete="new-password"
                  :value="form.apiKey" :placeholder="keyPlaceholder" data-testid="ss-apikey"
                  spellcheck="false" @input="onKeyInput(($event.target as HTMLInputElement).value)"
                >
                <!-- inside the box (left; the key text is right-aligned) so the rows keep the Location pitch -->
                <button
                  v-if="apiKeySet && !form.clearKey && !form.apiKey" type="button" class="ss-link ss-clearkey"
                  data-testid="ss-clearkey" @click="clearKey"
                >
                  clear key
                </button>
              </span>
            </div>
            <div class="form-row">
              <label class="ss-label">Satellite DB</label>
              <FormatSelect
                v-model="form.satelliteDb" :options="SATDB" :width="SS_SPAN"
                title="Satellite database" disabled
              />
            </div>
          </fieldset>

          <fieldset class="ss-group" data-testid="ss-hardware">
            <legend>Hardware</legend>
            <div class="form-row">
              <label class="ss-label">Mount</label>
              <FormatSelect v-model="form.mount" :options="MOUNTS" :width="SS_SPAN" title="Mount" disabled />
            </div>
            <div class="form-row">
              <label class="ss-label">OTA</label>
              <FormatSelect v-model="form.ota" :options="OTAS" :width="SS_SPAN" title="OTA" disabled />
            </div>
            <div class="form-row">
              <label class="ss-label">Lim EL [ &deg;]</label>
              <NumberField
                v-model="form.limEl" data-testid="ss-limel" :min="LIM_EL_MIN"
                :max="LIM_EL_MAX" :step="0.1" fixed-width="90px" :invalid="!!errors.limEl"
              />
            </div>
            <p v-if="errors.limEl" class="ss-err" data-testid="ss-limel-err">
              {{ errors.limEl }}
            </p>
            <div class="form-row">
              <label class="ss-label" title="Camera pixel size">Pixel [ &micro;m]</label>
              <NumberField
                v-model="form.cameraPixelUm" data-testid="ss-pixel" :min="PIXEL_UM_MIN" :max="PIXEL_UM_MAX" :step="0.01"
                fixed-width="90px" placeholder="unknown" :invalid="!!errors.cameraPixelUm"
              />
            </div>
            <p v-if="errors.cameraPixelUm" class="ss-err" data-testid="ss-pixel-err">
              {{ errors.cameraPixelUm }}
            </p>
          </fieldset>

          <fieldset class="ss-group ss-ads" disabled>
            <legend>ADS-B</legend>
            <p>Section awaiting protocol template</p>
          </fieldset>

          <fieldset class="ss-group ss-cctv" data-testid="ss-cctv">
            <legend>CCTV</legend>
            <div class="ss-cctv-grid">
              <div>
                <div class="form-row">
                  <label class="ss-label">Enable</label>
                  <SegToggle v-model="cctvOn" :options="ON_OFF" seg-class="ss-seg" data-testid="ss-cctv-enable" />
                </div>
                <div class="form-row">
                  <label class="ss-label">Type</label>
                  <SegToggle v-model="cctvKind" :options="CCTV_TYPES" seg-class="ss-seg" data-testid="ss-cctv-type" />
                </div>
                <div class="form-row">
                  <label class="ss-label">Status</label>
                  <span class="ss-cctv-status" data-testid="ss-cctv-status" :title="cctvStatus">{{ cctvStatus }}</span>
                </div>
              </div>
              <div>
                <div class="form-row">
                  <label class="ss-label">Stream URL</label>
                  <span class="ss-key-wrap">
                    <input
                      class="ov-dms-box ss-key" type="text" autocomplete="off" spellcheck="false"
                      :value="form.cctvUrl" :placeholder="cctvPlaceholder('Url')" data-testid="ss-cctv-url"
                      @input="cctvTyped('Url', ($event.target as HTMLInputElement).value)"
                    >
                    <button
                      v-if="form.cctvUrlSet && !form.clearCctvUrl && !form.cctvUrl" type="button" class="ss-link ss-clearkey"
                      data-testid="ss-cctv-clear-url" @click="cctvClear('Url')"
                    >
                      clear
                    </button>
                  </span>
                </div>
                <p v-if="errors.cctvUrl" class="ss-err" data-testid="ss-cctv-url-err">
                  {{ errors.cctvUrl }}
                </p>
                <div class="form-row">
                  <label class="ss-label">Username</label>
                  <span class="ss-key-wrap">
                    <input
                      class="ov-dms-box ss-key" type="text" autocomplete="off" spellcheck="false"
                      :value="form.cctvUser" :placeholder="cctvPlaceholder('User')" data-testid="ss-cctv-user"
                      @input="cctvTyped('User', ($event.target as HTMLInputElement).value)"
                    >
                    <button
                      v-if="form.cctvUserSet && !form.clearCctvUser && !form.cctvUser" type="button" class="ss-link ss-clearkey"
                      data-testid="ss-cctv-clear-user" @click="cctvClear('User')"
                    >
                      clear
                    </button>
                  </span>
                </div>
                <div class="form-row">
                  <label class="ss-label">Password</label>
                  <span class="ss-key-wrap">
                    <input
                      class="ov-dms-box ss-key" type="password" autocomplete="new-password" spellcheck="false"
                      :value="form.cctvPass" :placeholder="cctvPlaceholder('Pass')" data-testid="ss-cctv-pass"
                      @input="cctvTyped('Pass', ($event.target as HTMLInputElement).value)"
                    >
                    <button
                      v-if="form.cctvPassSet && !form.clearCctvPass && !form.cctvPass" type="button" class="ss-link ss-clearkey"
                      data-testid="ss-cctv-clear-pass" @click="cctvClear('Pass')"
                    >
                      clear
                    </button>
                  </span>
                </div>
              </div>
            </div>
          </fieldset>
        </div>

        <p v-if="warning" class="ss-warn" role="alert" data-testid="ss-warning">
          {{ warning }}
        </p>
        <p v-if="saveError" class="ss-err ss-save-err" role="alert" data-testid="ss-save-error">
          {{ saveError }}
        </p>
        </div>

        <div class="ss-actions">
          <Button
            type="button" variant="outline" class="ss-btn" data-testid="ss-cancel" @click="close"
          >
            CANCEL
          </Button>
          <Button type="submit" class="ss-btn ss-save" :disabled="!canSave" data-testid="ss-save">
            SAVE SETTINGS
          </Button>
        </div>
      </form>
    </DialogContent>
  </Dialog>
</template>

<style>
/* Row grid: label column --ss-label | format --ss-fmt | 2px | 1px separator | 2px | value --ss-field.
   Span after the label column = fmt + 5 + field (227px); columns = label + span + padding + border. */
.station-dialog {
  --ss-pad: 16px;
  --ss-span: calc(var(--ss-fmt) + 5px + var(--ss-field));
  --ss-col: calc(var(--ss-label) + var(--ss-span) + 2 * var(--ss-pad) + 2px);
  background: var(--page-bg); border: 1px solid var(--control-border); width: 100%;
  max-width: min(calc(2 * var(--ss-col) + 16px + 48px + 2px), calc(100vw - 2rem));
  display: flex; flex-direction: column; max-height: calc(100dvh - 2rem);
}
.station-dialog .ss-head { flex: none; padding: 12px 14px; border-bottom: 1px solid var(--control-border); }
.station-dialog .ss-title { font-size: 16px; font-weight: 700; letter-spacing: 0.02em; text-transform: uppercase; }
/* The title and the CANCEL / SAVE bar are fixed; only .ss-scroll (app-scroll: the thin app scrollbar) moves. */
.station-dialog .ss-body { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
.station-dialog .ss-scroll { flex: 1 1 auto; min-height: 0; padding: 16px 24px 20px; }
.station-dialog .ss-state { padding: 40px 24px; text-align: center; color: var(--muted-foreground); font-size: 13px; }
.station-dialog .ss-state-err { color: var(--danger); }
.station-dialog .ss-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
.station-dialog .ss-group { border: 1px solid var(--panel-border); padding: 14px var(--ss-pad) 8px; min-width: 0; margin: 0; }
.station-dialog .ss-group legend { padding: 0 6px; font-size: 14px; font-weight: 600; color: var(--label-slate); }
.station-dialog .ss-loc { position: relative; }
.station-dialog .ss-loc::before { content: ''; position: absolute; left: calc(var(--ss-pad) + var(--ss-label) + var(--ss-fmt) + 2px); top: 14px; bottom: 14px; width: 1px; background: var(--control-border); z-index: 1; }
.station-dialog .form-row { gap: 0; }
.station-dialog .ss-loc .ov-label-group-wide { width: calc(var(--ss-label) + var(--ss-fmt) + 2px); flex: 0 0 auto; gap: 0; justify-content: flex-start; }
.station-dialog .ss-loc .ov-label-group-wide label { width: var(--ss-label) !important; }
.station-dialog .ss-loc .ov-label-group-wide .ov-fmt-sel.fmt-select-trigger { width: var(--ss-fmt); flex: 0 0 var(--ss-fmt); }
.station-dialog .ss-loc .ov-input-group { flex: 0 0 var(--ss-field); width: var(--ss-field); margin-left: 3px; }
.station-dialog .ss-loc .form-row > .dms-box-wrap, .station-dialog .ss-loc .form-row > .num-spin-wrap { flex: 0 0 var(--ss-field); width: var(--ss-field); margin-left: 3px; }
.station-dialog .ss-loc .tz-trigger { flex: 0 0 var(--ss-field) !important; width: var(--ss-field) !important; margin-left: 3px; justify-content: flex-start; }
.station-dialog .ss-label { width: var(--ss-label); flex-shrink: 0; }
.station-dialog .ss-key-wrap { position: relative; display: flex; flex: 0 0 var(--ss-span); width: var(--ss-span); }
.station-dialog .ss-key { flex: 1 1 auto; width: 100%; min-width: 0; box-sizing: border-box; height: var(--field-h); padding: 0 8px; text-align: right; }
.station-dialog .ss-clearkey { position: absolute; left: 8px; top: 50%; transform: translateY(-50%); }
@media (max-width: 820px) { .station-dialog .ss-grid { grid-template-columns: minmax(0, 1fr); } }
.station-dialog .ss-link { background: none; border: 0; padding: 0; color: var(--label-slate); font-size: 12px; text-decoration: underline; cursor: pointer; }
.station-dialog .ss-link:hover { color: var(--foreground); }
.station-dialog .ss-ads { display: flex; align-items: center; justify-content: center; min-height: 100px; }
.station-dialog .ss-ads p { color: var(--muted-foreground); font-size: 14px; }
.station-dialog .ss-err { margin: -4px 0 6px; color: var(--danger); font-size: 12px; }
.station-dialog .ss-warn { margin: 12px 0 0; padding: 8px 10px; border: 1px solid #c9962b; background: rgba(201, 150, 43, 0.12); color: #e6b84f; font-size: 13px; }
.station-dialog .ss-save-err { margin-top: 10px; }
.station-dialog .ss-actions { flex: none; display: flex; justify-content: flex-end; gap: 12px; padding: 16px 24px 16px; border-top: 1px solid var(--panel-border); }
.station-dialog .ss-cctv { grid-column: 1 / -1; padding-bottom: 14px; }
.station-dialog .ss-cctv-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; }
.station-dialog .ss-seg { flex: 0 0 var(--ss-field); width: var(--ss-field); }
.station-dialog .ss-cctv-status { flex: 1 1 auto; min-width: 0; font-size: 13px; font-weight: 600; color: var(--foreground); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 820px) { .station-dialog .ss-cctv-grid { grid-template-columns: minmax(0, 1fr); } }
.station-dialog .ss-btn { height: 34px; min-width: 130px; border-radius: 0; font-size: 14px; font-weight: 700; letter-spacing: 0.02em; border: 1px solid var(--control-border); }
.station-dialog .ss-save { background: linear-gradient(to bottom right, var(--primary-from), var(--primary-to)); border-color: var(--accent-green-border); color: #fff; }
.station-dialog .ss-save:disabled { opacity: 0.45; }
</style>
