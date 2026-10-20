// Opens the one gateway telemetry socket for the whole app (client only).
import { useMountStore } from '~/stores/mount'
export default defineNuxtPlugin(() => {
  useMountStore().start(useRuntimeConfig().public.gatewayWs as string)
})
