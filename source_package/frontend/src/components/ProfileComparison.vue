<script setup lang="ts">
import { Columns3, Radar } from '@lucide/vue'
import { computed } from 'vue'

import { nextLearningPlan } from '../lib/learningInsights'
import {
  contextualizedTaskStem,
  firstLectureGoal,
  learnerText,
} from '../lib/tracePresentation'
import type { KnowledgeCatalogEntry, TraceDocument, TraceView } from '../types/trace'


export interface ComparisonEntry {
  document: TraceDocument
  view: TraceView
}

const props = defineProps<{
  entries: ComparisonEntry[]
  catalog: KnowledgeCatalogEntry[]
}>()

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : []
}

function scoreLabel(entry: ComparisonEntry): string {
  const score = entry.view.diagnosis?.content.pretest_score
  if (typeof score !== 'object' || score === null || Array.isArray(score)) return '尚未播放到测评结果'
  const record = score as Record<string, unknown>
  return `${record.correct}/${record.total}`
}

const ordered = computed(() => {
  const order = ['planner_new', 'craft_engineer', 'line_leader']
  return [...props.entries].sort(
    (left, right) => order.indexOf(left.document.profileId ?? '') - order.indexOf(right.document.profileId ?? ''),
  )
})

function lecturePoint(entry: ComparisonEntry): string {
  const value = entry.view.lecture?.content.knowledge_point
  return typeof value === 'string' ? learnerText(value) : '尚未播放到岗位微课'
}

function lectureSummary(entry: ComparisonEntry): string | undefined {
  const goal = firstLectureGoal(entry.view.lecture?.content.lecture_md)
  return goal ? learnerText(goal) : undefined
}

function taskStem(entry: ComparisonEntry): string {
  const stem = contextualizedTaskStem(entry.view)
  return stem ? learnerText(stem) : '暂无岗位实操任务'
}

function plannedPoint(entry: ComparisonEntry) {
  return nextLearningPlan(entry.view, props.catalog)
}
</script>

<template>
  <section class="panel profile-comparison" data-testid="profile-comparison">
    <header class="panel-heading comparison-heading">
      <div>
        <h2>岗位对比</h2>
      </div>
      <Columns3 :size="19" aria-hidden="true" />
    </header>

    <div class="comparison-grid">
      <article v-for="entry in ordered" :key="entry.document.traceId" class="comparison-card">
        <div class="comparison-card-top">
          <span class="profile-seal"><Radar :size="17" aria-hidden="true" /></span>
          <span>{{ scoreLabel(entry) }} 岗前测评</span>
        </div>
        <h3>{{ entry.view.profile?.title ?? '岗位画像' }}</h3>
        <p class="comparison-background">
          {{ learnerText(entry.view.profile?.background ?? '岗位背景随会话载入。') }}
        </p>

        <div class="comparison-section">
          <span>知识盲区</span>
          <ul>
            <li
              v-for="spot in stringList(entry.view.diagnosis?.content.blind_spots).slice(0, 4)"
              :key="spot"
            >{{ learnerText(spot) }}</li>
          </ul>
        </div>

        <div class="comparison-resource">
          <span>岗位微课</span>
          <strong class="comparison-lecture-point">{{ lecturePoint(entry) }}</strong>
          <p v-if="lectureSummary(entry)" class="comparison-lecture-summary">
            {{ lectureSummary(entry) }}
          </p>
        </div>

        <div class="comparison-resource">
          <span>实操任务</span>
          <strong class="comparison-task-stem">{{ taskStem(entry) }}</strong>
        </div>

        <div v-if="plannedPoint(entry)" class="comparison-next-point">
          <span>下一学习点</span>
          <strong>{{ plannedPoint(entry)?.knowledgePoint }}</strong>
        </div>
      </article>
    </div>
  </section>
</template>
