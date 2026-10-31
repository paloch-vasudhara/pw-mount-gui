import tailwindcss from '@tailwindcss/vite'

const gateway = process.env.NUXT_GATEWAY_URL || 'http://127.0.0.1:8400'
const analysis = process.env.NUXT_ANALYSIS_URL || 'http://127.0.0.1:8410'

export default defineNuxtConfig({
  compatibilityDate: '2026-09-10',

  ssr: false,
  srcDir: 'app/',

  devtools: { enabled: true },
  modules: ['@pinia/nuxt', 'shadcn-nuxt', '@nuxt/eslint'],

  typescript: { strict: false, typeCheck: false },

  app: {
    head: {
      title: 'PW MOUNT CONTROL',
      meta: [{ name: 'viewport', content: 'width=device-width, initial-scale=1.0' }],
    },
  },

  css: ['~/assets/css/fonts.css', '~/assets/css/tailwind.css', '~/assets/css/widgets.css'],

  vite: { plugins: [tailwindcss()] },
  shadcn: { prefix: '', componentDir: './app/components/ui' },

  runtimeConfig: {
    public: {
      // Dev only: Nitro's devProxy does not forward WebSocket upgrades (they fall through to
      // the Nuxt dev worker and get the SPA page back), so in dev the telemetry socket connects
      // straight to the gateway (its Origin allow-list admits the web dev origin; the token is
      // still fetched through the proxy). Empty = same-origin (production: gateway serves the app).
      gatewayWs: process.env.NODE_ENV === 'production' ? '' : gateway.replace(/^http/, 'ws'),
    },
  },

  nitro: {
    preset: 'static',
    // Dev only (not part of `nuxt generate` output). h3's app.use() strips the
    // matched route prefix before the proxy sees the path, and httpxy then
    // prepends the TARGET's own pathname - so each target must carry its own
    // route prefix or the backend would receive "/health" instead of
    // "/api/mount/health". (Same handling as ssa-toolkit's nuxt.config.ts.)
    // Longest prefix first: /api/mount (gateway) before /api (analysis).
    devProxy: {
      '/api/mount': { target: `${gateway}/api/mount`, changeOrigin: true, prependPath: true },
      // H4 (T3.3) is a gateway route outside /api/mount: without this it fell through to the
      // analysis service and the Go To modal's computed pair froze (T3.4d).
      '/api/transform': { target: `${gateway}/api/transform`, changeOrigin: true, prependPath: true },
      '/api': { target: `${analysis}/api`, changeOrigin: true, prependPath: true },
    },
  },
})
