<script setup lang="ts">
import { CircleDotDashed } from '@lucide/vue'
import { computed, ref, watch } from 'vue'

import profiles from 'virtual:profile-catalog'
import { currentProfileCopy, learnerText } from '../lib/tracePresentation'
import { difficultyJourney, resourceCoverage } from '../lib/learningInsights'
import type { KnowledgeCatalogEntry, TraceView } from '../types/trace'
import DiagnosisRadar from './DiagnosisRadar.vue'
import DifficultyJourney from './DifficultyJourney.vue'


defineOptions({ name: 'ProfilePanel' })

const props = withDefaults(defineProps<{
  view: TraceView
  catalog?: KnowledgeCatalogEntry[]
  currentDifficulty?: string | null
  /** 画像目录编号：展示层统一岗位文案（与岗位选择页/三画像同源）。 */
  profileId?: string
}>(), {
  catalog: () => [],
  currentDifficulty: undefined,
  profileId: undefined,
})

// 岗位标题/背景以现行画像目录为准，旧 trace 内嵌文案仅兜底。
const profileCopy = computed(() => currentProfileCopy(props.profileId, props.view.profile))

/** 需求⑤：各维度已达到档位（盲区 0；其余默认基础 1，按学习状态提升）。 */
const radarMasteryLevels = computed(() => {
  const statusByPoint = new Map<string, string>()
  if (learningPlan.value) {
    for (const item of learningPlan.value) {
      if (item.status) statusByPoint.set(item.point, item.status)
    }
  }
  return knowledgeDimensions.value.map((dimension) => {
    if (blindSpots.value.includes(dimension)) return 0
    const status = statusByPoint.get(dimension) ?? ''
    if (status.includes('advanced')) return 3
    if (status.includes('applied')) return 2
    return 1
  })
})

// 需求⑥：提交岗前测评（诊断生成）后，完整学习画像默认展开，无需手动点开。
const detailOpen = ref(false)
watch(
  () => Boolean(props.view.diagnosis),
  (hasDiagnosis) => {
    if (hasDiagnosis) detailOpen.value = true
  },
  { immediate: true },
)

const coveredBlindSpots = computed(() => resourceCoverage(props.view).items
  .filter((item) => item.covered)
  .map((item) => item.name))

const blindSpots = computed(() => {
  const value = props.view.diagnosis?.content.blind_spots
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string').map(learnerText)
    : []
})

/** 闭环四名实一致：前测全对（无盲区）与“尚未测评”的空态含义不同。 */
const blindSpotsEmptyCopy = computed(() => {
  const score = props.view.diagnosis?.content.pretest_score as
    { correct?: number; total?: number } | undefined
  if (
    score
    && typeof score.correct === 'number'
    && typeof score.total === 'number'
    && score.total > 0
    && score.correct === score.total
  ) {
    return '前测全部通过，本轮无知识盲区。'
  }
  return '本轮尚未标记知识盲区。'
})

const knowledgeDimensions = computed(() => {
  let dimensions = props.view.knowledgeDimensions.map(learnerText)
  // 闭环四后继：雷达只画该画像领域内的知识点（蓝图 2.1~2.3 的
  // knowledge_scope），不再一律十个维度。
  const scope = profiles.find((item) => item.id === props.profileId)?.knowledgeScope
  if (scope && scope.length) {
    const scopeSet = new Set(scope.map(learnerText))
    const scoped = dimensions.filter((item) => scopeSet.has(item))
    if (scoped.length) dimensions = scoped
  }
  if (!props.catalog.length) return dimensions
  // 雷达只画真正的知识点：后端 knowledge_dimensions 混入了 SEC 字段口径
  // 资料源标题，它们永远不进诊断盲区，画出来只是恒满格的装饰噪音。
  const catalogPoints = new Set(
    props.catalog.map((entry) => learnerText(entry.knowledgePoint)),
  )
  const filtered = dimensions.filter((item) => catalogPoints.has(item))
  return filtered.length ? filtered : dimensions
})

/**
 * 闭环四后继（用户 2026-08-17 反馈）：右侧画像不只显示前测答错的盲区，
 * 而是显示学习清单——当前进行中的知识点与之后要学的知识点。
 * v4 诊断带 knowledge_point_plan 时启用；v3/回放退回盲区列表。
 */
interface LearningPlanItem {
  point: string
  tier: string
  status: string
}

const learningPlan = computed<LearningPlanItem[] | null>(() => {
  const plan = props.view.diagnosis?.content.knowledge_point_plan
  if (!Array.isArray(plan) || !plan.length) return null
  const items: LearningPlanItem[] = []
  for (const entry of plan) {
    if (typeof entry !== 'object' || entry === null) continue
    const point = entry.knowledge_point
    const tier = entry.tier
    if (typeof point !== 'string' || !point.trim()) continue
    if (typeof tier !== 'string') continue
    items.push({
      point,
      tier,
      status: typeof entry.mastery_status === 'string' ? entry.mastery_status : '',
    })
  }
  return items.length ? items : null
})

const score = computed(() => {
  const value = props.view.diagnosis?.content.pretest_score
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
})

const difficulty = computed(() => {
  // 与培养路径头部、难度轨迹面板同源：轨迹最后一点是当前权威档，
  // 避免画像与路径在同帧显示两个互相矛盾的档位。
  const value = props.currentDifficulty
    ?? difficultyJourney(props.view, props.catalog).points.at(-1)?.level
    ?? props.view.path?.content.difficulty
    ?? props.view.task?.content.difficulty
    ?? props.view.diagnosis?.content.difficulty
  if (value === 'basic') return '基础档'
  if (value === 'applied') return '应用档'
  if (value === 'advanced') return '进阶档'
  return '待诊断'
})
</script>

<template>
  <aside class="panel profile-panel">
    <!-- 需求⑤：删除"岗位画像"标题行 -->
    <section v-if="view.profile" class="profile-identity">
      <span class="profile-index">当前岗位</span>
      <h3>{{ profileCopy.title }}</h3>
      <p>{{ learnerText(profileCopy.background) }}</p>
      <!-- 0819 bug3 补：实训页右侧"当前岗位"的特长小椭圆同步删除 -->
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

      <details class="profile-detail-disclosure" :open="detailOpen">
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
            :covered-points="coveredBlindSpots"
            :mastery-levels="radarMasteryLevels"
          />

          <div v-if="learningPlan" class="blind-spot-list learning-plan-list">
            <h4>学习清单</h4>
            <ol>
              <li
                v-for="(item, index) in learningPlan"
                :key="`${item.point}-${index}`"
                :class="{ 'is-current': index === 0 }"
              >
                <CircleDotDashed :size="13" aria-hidden="true" />
                <span>{{ item.point }}</span>
                <em v-if="index === 0">进行中</em>
                <em v-else-if="item.tier === 'prerequisite_lift'">补前置</em>
                <em v-else>待学习</em>
              </li>
            </ol>
          </div>
          <div v-else class="blind-spot-list">
            <h4>知识盲区</h4>
            <p v-if="!blindSpots.length" class="quiet-copy">{{ blindSpotsEmptyCopy }}</p>
            <ul v-else>
              <li v-for="spot in blindSpots" :key="spot">
                <CircleDotDashed :size="13" aria-hidden="true" />
                {{ spot }}
              </li>
            </ul>
          </div>

          <!-- 需求⑦：资源匹配板块已删除 -->
          <DifficultyJourney
            :view="view"
            :catalog="catalog"
            :current-difficulty="currentDifficulty"
          />
        </div>
      </details>
    </section>
    <section v-else class="panel-empty">
      岗前测评尚未播放到此处。
    </section>
  </aside>
</template>
