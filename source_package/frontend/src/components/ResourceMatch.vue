<script setup lang="ts">
import { Check, CircleDotDashed, ExternalLink } from '@lucide/vue'
import { computed } from 'vue'

import { resourceCoverage } from '../lib/learningInsights'
import { learnerText } from '../lib/tracePresentation'
import type { TraceView } from '../types/trace'


const props = defineProps<{ view: TraceView }>()
const coverage = computed(() => resourceCoverage(props.view))

function sourceTarget(source: string): string {
  return source === '岗位微课' ? '#lecture-resource' : '#task-resource'
}

function resourceName(name: string): string {
  return learnerText(name)
}
</script>

<template>
  <section
    v-if="coverage.total"
    class="resource-match"
    data-testid="resource-match"
    :aria-label="`资源匹配：${coverage.total}项盲区，本次资源覆盖${coverage.covered}项`"
  >
    <header>
      <div>
        <span>资源匹配</span>
        <strong>{{ coverage.total }}项盲区，本次资源覆盖{{ coverage.covered }}项</strong>
      </div>
      <span class="coverage-count">{{ coverage.covered }}/{{ coverage.total }}</span>
    </header>

    <ul>
      <li
        v-for="item in coverage.items"
        :key="item.name"
        :class="{ 'is-covered': item.covered }"
      >
        <Check v-if="item.covered" :size="13" aria-hidden="true" />
        <CircleDotDashed v-else :size="13" aria-hidden="true" />
        <span class="coverage-name">{{ resourceName(item.name) }}</span>
        <span v-if="!item.covered" class="coverage-pending">尚未覆盖</span>
        <span v-else class="coverage-links">
          <a
            v-for="source in item.sources"
            :key="source"
            :href="sourceTarget(source)"
            :aria-label="`查看覆盖“${resourceName(item.name)}”的${source}`"
          >
            {{ source }} <ExternalLink :size="10" aria-hidden="true" />
          </a>
        </span>
      </li>
    </ul>
  </section>
</template>
