<script setup lang="ts">
// Dev-only harness (unlinked): proves the /api proxy reaches both backends.
type Result = { ok: boolean, detail: string }

const targets = [
  { name: 'gateway', url: '/api/mount/health' },
  { name: 'analysis', url: '/api/health' },
]
const results = ref<Record<string, Result>>({})

async function check(name: string, url: string) {
  try {
    const res = await fetch(url)
    const body = await res.json()
    results.value[name] = { ok: res.ok && body?.ok === true, detail: JSON.stringify(body) }
  }
  catch (e) {
    results.value[name] = { ok: false, detail: String(e) }
  }
}

onMounted(() => {
  for (const t of targets) check(t.name, t.url)
})
</script>

<template>
  <main class="min-h-screen bg-page p-8">
    <h1 class="mb-4 text-xl font-semibold">Backend health</h1>
    <ul class="space-y-2">
      <li v-for="t in targets" :key="t.name" :data-testid="`health-${t.name}`">
        <span class="inline-block w-24">{{ t.name }}</span>
        <span :class="results[t.name]?.ok ? 'text-accent-green-text' : 'text-danger'">
          {{ results[t.name] ? (results[t.name].ok ? 'OK' : 'FAIL') : '...' }}
        </span>
        <span class="ml-3 text-muted-foreground">{{ results[t.name]?.detail }}</span>
      </li>
    </ul>
  </main>
</template>
