<script setup lang="ts">
// Dev-only harness (unlinked): live gateway snapshot through the web dev proxy.
import { storeToRefs } from 'pinia'
import { useMountStore } from '~/stores/mount'

const { snapshot, hello, connection, messageCount, lastError } = storeToRefs(useMountStore())
</script>

<template>
  <main class="min-h-screen bg-page p-8">
    <h1 class="mb-4 text-xl font-semibold">Mount socket</h1>
    <p class="mb-2" data-testid="mount-connection">
      connection: {{ connection }}
      <span v-if="lastError" class="ml-2 text-muted-foreground">{{ lastError }}</span>
    </p>
    <p class="mb-2" data-testid="mount-state">state: {{ snapshot?.state ?? '-' }}</p>
    <p class="mb-2" data-testid="mount-count">messages: {{ messageCount }}</p>
    <p class="mb-4" data-testid="mount-seq">seq: {{ snapshot?.seq ?? '-' }}</p>
    <pre class="mb-4 text-xs" data-testid="mount-hello">{{ JSON.stringify(hello) }}</pre>
    <pre class="text-xs" data-testid="mount-json">{{ JSON.stringify(snapshot, null, 2) }}</pre>
  </main>
</template>
