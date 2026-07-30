<script setup lang="ts">
import { BriefcaseBusiness, CircleDotDashed } from '@lucide/vue'
import { computed } from 'vue'

import { learnerText } from '../lib/tracePresentation'
import type { KnowledgeCatalogEntry, TraceView } from '../types/trace'
import DiagnosisRadar from './DiagnosisRadar.vue'
import DifficultyJourney from './DifficultyJourney.vue'
import ResourceMatch from './ResourceMatch.vue'


defineOptions({ name: 'ProfilePanel' })

const props = withDefaults(defineProps<{
  view: TraceView
  catalog?: KnowledgeCatalogEntry[]
}>(), {
  catalog: () => [],
})

const blindSpots = computed(() => {
  const value = props.view.diagnosis?.content.blind_spots
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string').map(learnerText)
    : []
})

const knowledgeDimensions = computed(
  () => props.view.knowledgeDimensions.map(learnerText),
)

const strengths = computed(() => {
  const value = props.view.profile?.strengths
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string').map(learnerText)
    : []
})

const score = computed(() => {
  const value = props.view.diagnosis?.content.pretest_score
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
})

const difficulty = computed(() => {
  const value = props.view.diagnosis?.content.difficulty
  if (value === 'basic') return '基础档'
  if (value === 'applied') return '应用档'
  if (value === 'advanced') return '进阶档'
  return '待诊断'
})
</script>

<template>
  <aside class="panel profile-panel">
    <header class="panel-heading">
      <div>
        <h2>岗位画像</h2>
      </div>
      <BriefcaseBusiness :size="18" aria-hidden="true" />
    </header>

    <section v-if="view.profile" class="profile-identity">
      <span class="profile-index">当前岗位</span>
      <h3>{{ view.profile.title ?? '岗位画像已载入' }}</h3>
      <p>{{ learnerText(view.profile.background ?? '岗位背景已记录在当前会话。') }}</p>
      <div v-if="strengths.length" class="tag-row">
        <span v-for="strength in strengths" :key="strength">{{ strength }}</span>
      </div>
    </section>
    <section v-else class="panel-empty">
      学情画像将在回放进入画像载入后出现。
    </section>

    <section v-if="view.diagnosis" class="assessment-block">
      <div class="assessment-summary">
        <div>
          <span>岗前测评</span>
          <strong v-if="score">{{ score.correct }}/{{ score.total }}</strong>
          <strong v-else>已完成</strong>
        </div>
        <div>
          <span>当前档位</span>
          <strong>{{ difficulty }}</strong>
        </div>
      </div>

      <details class="profile-detail-disclosure">
        <summary>查看完整学习画像</summary>
        <div
          class="profile-detail-content"
          role="region"
          aria-label="完整学习画像内容"
          tabindex="0"
        >
          <DiagnosisRadar
            :dimensions="knowledgeDimensions"
            :blind-spots="blindSpots"
          />

          <div class="blind-spot-list">
            <h4>知识盲区</h4>
            <p v-if="!blindSpots.length" class="quiet-copy">本轮尚未标记知识盲区。</p>
            <ul v-else>
              <li v-for="spot in blindSpots" :key="spot">
                <CircleDotDashed :size="13" aria-hidden="true" />
                {{ spot }}
              </li>
            </ul>
          </div>

          <ResourceMatch :view="view" />
          <DifficultyJourney :view="view" :catalog="catalog" />
        </div>
      </details>
    </section>
    <section v-else class="panel-empty">
      岗前测评尚未播放到此处。
    </section>
  </aside>
</template>
