<script setup lang="ts">
import { BookOpen, CircleAlert, ListChecks, Maximize2, Minimize2 } from '@lucide/vue'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import {
  dataFieldLabel,
  learnerText,
  learnerTextOr,
  misconceptionLabel,
} from '../lib/tracePresentation'
import type { TraceMessage, TraceView } from '../types/trace'
import EvidenceClaim from './EvidenceClaim.vue'
import SqlResultTable from './SqlResultTable.vue'


const props = withDefaults(defineProps<{
  view: TraceView
  lessonPager?: boolean
  guidanceFeedback?: string
  guidanceNextStepReason?: string
}>(), {
  lessonPager: false,
})
const emit = defineEmits<{
  pageState: [value: {
    index: number
    total: number
    kind?: 'metrics' | 'section' | 'evidence' | 'task'
    isLast: boolean
  }]
}>()
const focusMode = ref(false)
const guidanceHintLevel = ref(0)

function toggleFocusMode(): void {
  focusMode.value = !focusMode.value
}

function leaveFocusMode(event: KeyboardEvent): void {
  if (event.key === 'Escape') focusMode.value = false
}

onMounted(() => window.addEventListener('keydown', leaveFocusMode))
onBeforeUnmount(() => window.removeEventListener('keydown', leaveFocusMode))

interface MarkdownBlock {
  kind: 'heading' | 'paragraph' | 'list'
  text: string
}

interface LectureSection {
  title?: string
  blocks: MarkdownBlock[]
}

interface KeyMetric {
  label: string
  value: string
}

interface LessonPage {
  key: string
  title: string
  kind: 'metrics' | 'section' | 'evidence' | 'task'
  section?: LectureSection
}

interface GuideMarkdownContent {
  objective?: string
  intro?: string
  steps: string[]
  criteria: string[]
  hasRequiredSections: boolean
}

interface GuideContentSource {
  question: string
  intro: string
  steps: string[]
  criteria: string[]
}

type GuideMarkdownSection = 'objective' | 'intro' | 'steps' | 'criteria'

const GUIDE_SECRET_TEXT = [
  /\b(?:sql|standard_sql|generated_sql|executed_sql)\b/iu,
  /\b(?:select|insert|update|delete|drop|alter|with|from|where|join)\b/iu,
  /\b(?:quiz_answer_key|answer[_ -]?key|expected_rows|expected_points)\b/iu,
  /(?:标准\s*SQL|答案键)/iu,
]

function plainInlineMarkdown(text: string): string {
  return text
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
}

function stripInlineMarkdown(text: string): string {
  return learnerText(plainInlineMarkdown(text))
}

function containsGuideSecretText(text: string): boolean {
  return GUIDE_SECRET_TEXT.some((pattern) => pattern.test(text))
}

function safeGuideText(value: unknown, fallback: string): string {
  if (typeof value !== 'string') return fallback
  const normalized = plainInlineMarkdown(value).trim()
  if (!normalized || containsGuideSecretText(value) || containsGuideSecretText(normalized)) {
    return fallback
  }
  const safeText = learnerTextOr(normalized, fallback).trim()
  return safeText && !containsGuideSecretText(safeText) ? safeText : fallback
}

function appendGuideLine(current: string | undefined, line: string): string {
  return current ? `${current} ${line}` : line
}

function parseGuideMarkdown(value: unknown): GuideMarkdownContent {
  const content: GuideMarkdownContent = {
    steps: [],
    criteria: [],
    hasRequiredSections: false,
  }
  if (typeof value !== 'string') return content

  let section: GuideMarkdownSection | undefined
  const seenSections = new Set<GuideMarkdownSection>()
  for (const rawLine of value.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line) continue

    const heading = line.match(/^#{1,6}\s+(.+)$/)
    if (heading?.[1]) {
      const title = plainInlineMarkdown(heading[1]).trim()
      if (/实操目标|练习目标|任务目标|题面/u.test(title)) section = 'objective'
      else if (/开始前|引导|提示/u.test(title)) section = 'intro'
      else if (/操作步骤|实操步骤|练习步骤/u.test(title)) section = 'steps'
      else if (/完成标准|验收标准|检查标准/u.test(title)) section = 'criteria'
      else section = undefined
      if (section) seenSections.add(section)
      continue
    }

    const item = line
      .replace(/^\d+[.)、]\s*/, '')
      .replace(/^[-*+]\s+/, '')
      .trim()
    if (!item || !section) continue
    if (section === 'steps') content.steps.push(item)
    else if (section === 'criteria') content.criteria.push(item)
    else if (section === 'objective') content.objective = appendGuideLine(content.objective, item)
    else content.intro = appendGuideLine(content.intro, item)
  }
  content.hasRequiredSections = (
    ['objective', 'intro', 'steps', 'criteria'] as const
  ).every((required) => seenSections.has(required))
  return content
}

const lectureBlocks = computed<MarkdownBlock[]>(() => {
  const source = props.view.lecture?.content.lecture_md
  if (typeof source !== 'string') return []
  const blocks: MarkdownBlock[] = []
  for (const line of source.split(/\r?\n/)) {
    const text = line.trim()
    if (!text) continue
    if (/^#{1,6}\s+/.test(text)) {
      blocks.push({
        kind: 'heading',
        text: stripInlineMarkdown(text.replace(/^#{1,6}\s+/, '')),
      })
      continue
    }
    if (/^[-*]\s+/.test(text)) {
      blocks.push({
        kind: 'list',
        text: stripInlineMarkdown(text.replace(/^[-*]\s+/, '')),
      })
      continue
    }
    blocks.push({ kind: 'paragraph', text: stripInlineMarkdown(text) })
  }
  return blocks
})

const lectureSections = computed<LectureSection[]>(() => {
  const sections: LectureSection[] = []
  let current: LectureSection | undefined
  for (const block of lectureBlocks.value) {
    if (block.kind === 'heading') {
      current = { title: block.text, blocks: [] }
      sections.push(current)
      continue
    }
    if (!current) {
      current = { blocks: [] }
      sections.push(current)
    }
    current.blocks.push(block)
  }
  return sections.filter((section) => section.blocks.length > 0)
})

const lessonPageIndex = ref(0)
const lessonPages = computed<LessonPage[]>(() => {
  const pages: LessonPage[] = []
  if (lectureMetrics.value.length) {
    pages.push({ key: 'metrics', title: '本节关键数据', kind: 'metrics' })
  }
  lectureSections.value.forEach((section, index) => {
    pages.push({
      key: `section-${index}`,
      title: section.title || `知识卡片 ${index + 1}`,
      kind: 'section',
      section,
    })
  })
  if (unmatchedClaims.value.length) {
    pages.push({ key: 'evidence', title: '数据出处', kind: 'evidence' })
  }
  if (props.view.task) {
    pages.push({
      key: `task-${props.view.task.msgId}`,
      title: props.view.task.payloadType === 'practice_guide' ? '实操指南' : '练习题',
      kind: 'task',
    })
  }
  return pages
})
const currentLessonPage = computed(() => lessonPages.value[lessonPageIndex.value])
const lessonPanelKicker = computed(() => currentLessonPage.value?.kind === 'task'
  ? '实操准备'
  : '分页微课')
const lessonPanelTitle = computed(() => currentLessonPage.value?.kind === 'task'
  ? '实操指南'
  : '知识卡片')
const lessonFocusLabel = computed(() => currentLessonPage.value?.kind === 'task'
  ? '专注查看指南'
  : '专注学习')

const lectureMetrics = computed<KeyMetric[]>(() => {
  const metrics: KeyMetric[] = []
  const seen = new Set<string>()
  const add = (label: string, value: unknown) => {
    if (value === undefined || value === null || metrics.length >= 3) return
    const normalizedValue = String(value)
    if (seen.has(label)) return
    seen.add(label)
    metrics.push({ label, value: normalizedValue })
  }

  const source = props.view.lecture?.content.lecture_md
  if (typeof source === 'string') {
    const plain = stripInlineMarkdown(source)
    for (const line of plain.split(/\r?\n/)) {
      const labelMatch = line.match(/((?:YCL|ZZTP|AZTP)?(?:计划量|实际完成量|实际量|完成率|偏差率))/)
      if (!labelMatch?.[1]) continue
      const rawLabel = labelMatch[1]
      const label = rawLabel === '实际量' ? '实际完成量' : rawLabel
      const tail = line.slice((labelMatch.index ?? 0) + rawLabel.length)
      const percentages = [...tail.matchAll(/\d+(?:\.\d+)?%/g)]
      const number = tail.match(/\d+(?:\.\d+)?%?/)
      const value = rawLabel.includes('率') && percentages.length
        ? percentages[0]?.[0]
        : number?.[0]
      add(label, value)
    }
  }

  const columns = props.view.sqlResult?.content.columns
  const rows = props.view.sqlResult?.content.rows
  const row = Array.isArray(rows) && typeof rows[0] === 'object'
    && rows[0] !== null && !Array.isArray(rows[0])
    ? rows[0] as Record<string, unknown>
    : undefined
  if (row && Array.isArray(columns)) {
    for (const column of columns) {
      if (typeof column === 'string') add(dataFieldLabel(column), row[column])
    }
  }
  const rank = (label: string) => {
    if (label.includes('计划量')) return 0
    if (label.includes('实际')) return 1
    if (label.includes('率')) return 2
    return 3
  }
  return metrics.sort((left, right) => rank(left.label) - rank(right.label))
})

const unmatchedClaims = computed(() => {
  const lecture = props.view.lecture
  if (!lecture) return []
  return lecture.claims.filter(
    (claim) => !lectureBlocks.value.some((block) => block.text.includes(claim.text)),
  )
})

watch(
  () => props.view.lecture?.msgId,
  () => { lessonPageIndex.value = 0 },
)
watch(
  () => props.view.task?.msgId,
  (taskMessageId) => {
    if (props.lessonPager && taskMessageId) {
      lessonPageIndex.value = Math.max(0, lessonPages.value.length - 1)
    }
  },
)
watch(
  () => lessonPages.value.length,
  (length) => {
    lessonPageIndex.value = Math.min(lessonPageIndex.value, Math.max(0, length - 1))
  },
)
watch(
  [lessonPageIndex, () => lessonPages.value.length, () => currentLessonPage.value?.kind],
  () => emit('pageState', {
    index: lessonPageIndex.value,
    total: lessonPages.value.length,
    kind: currentLessonPage.value?.kind,
    isLast: lessonPageIndex.value === Math.max(0, lessonPages.value.length - 1),
  }),
  { immediate: true },
)

function previousLessonPage(): void {
  lessonPageIndex.value = Math.max(0, lessonPageIndex.value - 1)
}

function nextLessonPage(): void {
  lessonPageIndex.value = Math.min(lessonPages.value.length - 1, lessonPageIndex.value + 1)
}

function lessonSectionPreview(section: LectureSection): string {
  return section.blocks
    .slice(0, 2)
    .map((block) => learnerText(block.text))
    .join(' ')
    .slice(0, 120)
}

function openLessonSection(sectionIndex: number): void {
  const pageIndex = lessonPages.value.findIndex((page) => page.key === `section-${sectionIndex}`)
  if (pageIndex >= 0) lessonPageIndex.value = pageIndex
}

const lectureKnowledgePoint = computed(() => {
  const value = props.view.lecture?.content.knowledge_point
  return typeof value === 'string' ? learnerText(value) : '本节内容'
})

const lectureMatchesBlindSpot = computed(() => {
  const value = props.view.diagnosis?.content.blind_spots
  return Array.isArray(value) && value.includes(props.view.lecture?.content.knowledge_point)
})

const ratedTask = computed(() => [...props.view.visibleMessages].reverse().find(
  (message) => (message.payloadType === 'quiz_set' || message.payloadType === 'practice_guide')
    && ['basic', 'applied', 'advanced'].includes(String(message.content.difficulty)),
))

const taskDifficulty = computed(() => {
  const value = props.view.task?.payloadType === 'practice_guide'
    ? props.view.task.content.difficulty
    : ratedTask.value?.content.difficulty ?? props.view.task?.content.difficulty
  if (value === 'basic') return { value, label: '基础', index: 0, ordinal: '一' }
  if (value === 'applied') return { value, label: '应用', index: 1, ordinal: '二' }
  if (value === 'advanced') return { value, label: '进阶', index: 2, ordinal: '三' }
  return undefined
})

const difficultySteps = ['基础', '应用', '进阶']

const isPracticeGuide = computed(() => props.view.task?.payloadType === 'practice_guide')

const guideMarkdown = computed(
  () => parseGuideMarkdown(props.view.task?.content.guide_md),
)

function isNonEmptyGuideItem(value: unknown): value is string {
  return typeof value === 'string' && Boolean(value.trim())
}

function isValidGuideList(value: unknown, minimum: number): value is string[] {
  return Array.isArray(value)
    && value.length >= minimum
    && value.every(isNonEmptyGuideItem)
}

function hasOwnGuideField(content: Record<string, unknown>, field: string): boolean {
  return Object.prototype.hasOwnProperty.call(content, field)
}

const guideContent = computed<GuideContentSource | undefined>(() => {
  const task = props.view.task
  if (!task || task.payloadType !== 'practice_guide') return undefined
  const content = task.content
  if (!['basic', 'applied', 'advanced'].includes(String(content.difficulty))) {
    return undefined
  }

  const hasStructuredField = ['guide_intro', 'guide_steps', 'completion_criteria']
    .some((field) => hasOwnGuideField(content, field))
  if (hasStructuredField) {
    if (
      !isNonEmptyGuideItem(content.question)
      || !isNonEmptyGuideItem(content.guide_intro)
      || !isValidGuideList(content.guide_steps, 3)
      || !isValidGuideList(content.completion_criteria, 1)
    ) {
      return undefined
    }
    return {
      question: content.question,
      intro: content.guide_intro,
      steps: content.guide_steps,
      criteria: content.completion_criteria,
    }
  }

  const markdown = guideMarkdown.value
  if (
    !markdown.hasRequiredSections
    || !isNonEmptyGuideItem(markdown.objective)
    || !isNonEmptyGuideItem(markdown.intro)
    || !isValidGuideList(markdown.steps, 3)
    || !isValidGuideList(markdown.criteria, 1)
  ) {
    return undefined
  }
  return {
    question: markdown.objective,
    intro: markdown.intro,
    steps: markdown.steps,
    criteria: markdown.criteria,
  }
})

const taskQuestion = computed(() => {
  const value = isPracticeGuide.value
    ? guideContent.value?.question
    : props.view.task?.content.question ?? props.view.task?.content.contextualized_stem
  return safeGuideText(
    value,
    isPracticeGuide.value
      ? '实操题面暂不可用，请结合本节知识完成练习。'
      : '练习题面暂不可用，请结合本节知识完成作答。',
  )
})

const guideIntro = computed(() => {
  return safeGuideText(
    guideContent.value?.intro,
    '请根据题面逐步完成操作，并在提交前核对结果。',
  )
})

const hasGuideStructure = computed(() => Boolean(guideContent.value))

const guideSteps = computed(() => {
  return (guideContent.value?.steps ?? []).map((step) => safeGuideText(
    step,
    '本步骤包含内部信息，已隐藏，请按题面要求继续练习。',
  ))
})

const guideCriteria = computed(() => {
  return (guideContent.value?.criteria ?? []).map((criterion) => safeGuideText(
    criterion,
    '请按题面要求核对完成情况。',
  ))
})

function guideStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && Boolean(item.trim()))
    : []
}

const practiceHints = computed(() => {
  const authority = props.view.task?.content.query_authority
  const record = typeof authority === 'object' && authority !== null && !Array.isArray(authority)
    ? authority as Record<string, unknown>
    : undefined
  const outputs = guideStringList(record?.output_columns).map(dataFieldLabel)
  const filters = guideStringList(record?.filter_columns).map(dataFieldLabel)
  const groups = guideStringList(record?.group_by_columns).map(dataFieldLabel)
  const timeValues = guideStringList(record?.time_values)
  return [
    '先确认题目对象、时间范围和需要比较的指标，再开始查询。',
    outputs.length
      ? `结果至少应包含：${outputs.join('、')}。`
      : '查询结果中的字段应能直接回答题目。',
    [
      filters.length ? `筛选条件要覆盖${filters.join('、')}` : '',
      timeValues.length ? `时间范围要包含${timeValues.join('、')}` : '',
      groups.length ? `结果需要按${groups.join('、')}进行比较` : '',
    ].filter(Boolean).join('；') || '提交前核对结果行数、单位和统计口径。',
  ]
})

const taskMisconception = computed(() => {
  const value = props.view.task?.content.misconception
  return typeof value === 'string' ? misconceptionLabel(value) : undefined
})

const hasApprovedResource = computed(() => {
  const resourceIds = new Set([
    props.view.lecture?.msgId,
    props.view.task?.msgId,
    props.view.sqlResult?.msgId,
  ].filter((value): value is string => typeof value === 'string'))
  return props.view.visibleMessages.some((message) => (
    message.payloadType === 'review_verdict'
    && ['approve', 'approve_with_fix'].includes(message.verdict?.decision ?? '')
    && typeof message.content.reviewed_msg_id === 'string'
    && resourceIds.has(message.content.reviewed_msg_id)
  ))
})

function claimsFor(message: TraceMessage, text: string) {
  return message.claims.filter((claim) => text.includes(claim.text))
}
</script>

<template>
  <main
    class="panel resource-panel"
    :class="{ 'is-lesson-pager': lessonPager, 'is-focus-mode': focusMode }"
  >
    <header class="panel-heading resource-heading">
      <div>
        <span v-if="lessonPager" class="section-kicker">{{ lessonPanelKicker }}</span>
        <h2>{{ lessonPager ? lessonPanelTitle : '微课 / 实操' }}</h2>
      </div>
      <div class="resource-heading-status">
        <span v-if="hasApprovedResource" class="professional-review-badge">
          已通过专业审核 ✓
        </span>
        <BookOpen :size="19" aria-hidden="true" />
        <button
          type="button"
          class="content-focus-toggle"
          :aria-label="focusMode ? currentLessonPage?.kind === 'task' ? '退出实操指南专注模式' : '退出微课专注模式' : currentLessonPage?.kind === 'task' ? '最大化实操指南' : '最大化微课'"
          :title="focusMode ? '退出专注模式（Esc）' : currentLessonPage?.kind === 'task' ? '最大化实操指南' : '最大化微课'"
          @click="toggleFocusMode"
        >
          <Minimize2 v-if="focusMode" :size="16" aria-hidden="true" />
          <Maximize2 v-else :size="16" aria-hidden="true" />
          <span>{{ focusMode ? '还原' : lessonFocusLabel }}</span>
        </button>
      </div>
    </header>

    <section v-if="lessonPager" class="lesson-pager" aria-label="微课分页阅读">
      <div v-if="view.lecture && currentLessonPage" class="lesson-page">
        <header class="lesson-page-heading">
          <div>
            <span>{{ learnerText(String(view.lecture.content.knowledge_point ?? '岗位微课')) }}</span>
            <h3>{{ currentLessonPage.title }}</h3>
          </div>
          <b>{{ lessonPageIndex + 1 }} / {{ lessonPages.length }}</b>
        </header>

        <div v-if="currentLessonPage.kind === 'metrics'" class="lesson-page-metrics">
          <article v-for="metric in lectureMetrics" :key="`${metric.label}-${metric.value}`">
            <span>{{ metric.label }}</span>
            <strong class="instrument-number">{{ metric.value }}</strong>
          </article>
        </div>

        <section
          v-if="focusMode && currentLessonPage.kind === 'metrics' && lectureSections.length"
          class="lesson-page-overview"
          aria-label="本节内容概览"
        >
          <header>
            <div>
              <span>本节内容概览</span>
              <strong>从关键数据进入业务理解</strong>
            </div>
            <small>共 {{ lectureSections.length }} 个知识模块</small>
          </header>
          <div class="lesson-overview-grid">
            <button
              v-for="(section, index) in lectureSections"
              :key="`${index}-${section.title ?? '知识卡片'}`"
              type="button"
              @click="openLessonSection(index)"
            >
              <b>{{ String(index + 1).padStart(2, '0') }}</b>
              <span>
                <strong>{{ section.title || `知识卡片 ${index + 1}` }}</strong>
                <small>{{ lessonSectionPreview(section) }}</small>
              </span>
              <i aria-hidden="true">→</i>
            </button>
          </div>
        </section>

        <div
          v-else-if="currentLessonPage.kind === 'section' && currentLessonPage.section"
          class="lesson-page-copy"
        >
          <template
            v-for="(block, index) in currentLessonPage.section.blocks"
            :key="`${index}-${block.text}`"
          >
            <div v-if="block.kind === 'list'" class="markdown-list-item">
              <span aria-hidden="true"></span>
              <EvidenceClaim
                :text="block.text"
                :claims="claimsFor(view.lecture, block.text)"
                :evidence="view.lecture.evidence"
                :knowledge-point="lectureKnowledgePoint"
                :matches-blind-spot="lectureMatchesBlindSpot"
              />
            </div>
            <p v-else>
              <EvidenceClaim
                :text="block.text"
                :claims="claimsFor(view.lecture, block.text)"
                :evidence="view.lecture.evidence"
                :knowledge-point="lectureKnowledgePoint"
                :matches-blind-spot="lectureMatchesBlindSpot"
              />
            </p>
          </template>
        </div>

        <section
          v-else-if="currentLessonPage.kind === 'task' && view.task"
          id="task-resource"
          class="lesson-page-task task-card"
          :class="isPracticeGuide ? 'practice-guide-card' : 'quiz-card'"
          aria-label="本轮练习题"
        >
          <div class="task-card-heading">
            <span class="resource-eyebrow">
              <ListChecks :size="14" aria-hidden="true" />
              {{ isPracticeGuide ? '实操指南' : '练习题' }}
            </span>
            <div
              v-if="taskDifficulty"
              class="task-difficulty-ladder"
              :aria-label="`题目难度：${taskDifficulty.label}，三级分阶中的第${taskDifficulty.ordinal}级`"
            >
              <span
                v-for="(step, index) in difficultySteps"
                :key="step"
                class="task-difficulty-step"
                :class="{ 'is-active': index === taskDifficulty.index, 'is-reached': index <= taskDifficulty.index }"
              >{{ step }}</span>
            </div>
          </div>
          <h3 :class="{ 'guide-question': isPracticeGuide }">{{ taskQuestion }}</h3>
          <p v-if="isPracticeGuide && hasGuideStructure" class="guide-intro">{{ guideIntro }}</p>
          <div v-if="isPracticeGuide && hasGuideStructure" class="lesson-guide-sections">
            <section class="guide-section" aria-label="操作步骤">
              <h4>操作步骤</h4>
              <ol class="guide-steps">
                <li v-for="(step, index) in guideSteps" :key="`${index}-${step}`">{{ step }}</li>
              </ol>
            </section>
            <section class="guide-section" aria-label="完成标准">
              <h4>完成标准</h4>
              <ul class="guide-criteria">
                <li v-for="(criterion, index) in guideCriteria" :key="`${index}-${criterion}`">
                  {{ criterion }}
                </li>
              </ul>
            </section>
          </div>
          <p v-if="taskMisconception" class="probe-notice">
            <CircleAlert :size="15" aria-hidden="true" />
            本题针对：{{ taskMisconception }}
          </p>
          <section class="practice-coaching" aria-label="实操辅助信息">
            <div class="practice-coaching-heading">
              <strong>实操老师提示</strong>
              <button
                type="button"
                @click="guidanceHintLevel = guidanceHintLevel >= practiceHints.length ? 0 : guidanceHintLevel + 1"
              >{{ guidanceHintLevel >= practiceHints.length ? '收起提示' : `查看提示 ${guidanceHintLevel + 1}/${practiceHints.length}` }}</button>
            </div>
            <ol v-if="guidanceHintLevel">
              <li v-for="hint in practiceHints.slice(0, guidanceHintLevel)" :key="hint">{{ hint }}</li>
            </ol>
            <article v-if="guidanceFeedback || guidanceNextStepReason" class="practice-round-summary">
              <strong>本轮小结</strong>
              <p v-if="guidanceFeedback">{{ learnerText(guidanceFeedback) }}</p>
              <small v-if="guidanceNextStepReason">
                进入下一步的理由：{{ learnerText(guidanceNextStepReason) }}
              </small>
            </article>
          </section>
        </section>

        <ul v-else class="lesson-page-evidence">
          <li v-for="claim in unmatchedClaims" :key="claim.text">
            <EvidenceClaim
              :text="claim.text"
              :claims="[claim]"
              :evidence="view.lecture.evidence"
              :knowledge-point="lectureKnowledgePoint"
              :matches-blind-spot="lectureMatchesBlindSpot"
            />
          </li>
        </ul>
      </div>
      <section v-else class="resource-placeholder">
        <BookOpen :size="20" aria-hidden="true" />
        <p>岗位微课将在本轮测评完成后出现。</p>
      </section>

      <footer v-if="lessonPages.length > 1" class="lesson-pager-actions">
        <button
          type="button"
          aria-label="上一张微课卡片"
          :disabled="lessonPageIndex === 0"
          @click="previousLessonPage"
        >上一页</button>
        <span aria-label="微课分页进度">
          <i
            v-for="(page, index) in lessonPages"
            :key="page.key"
            :class="{ 'is-current': index === lessonPageIndex }"
          ></i>
        </span>
        <button
          type="button"
          aria-label="下一张微课卡片"
          :disabled="lessonPageIndex === lessonPages.length - 1"
          @click="nextLessonPage"
        >下一页</button>
      </footer>
    </section>

    <div v-else class="resource-scroll">
      <section v-if="view.lecture" id="lecture-resource" class="lecture-card">
        <div class="resource-title-row">
          <span class="resource-eyebrow"><BookOpen :size="14" aria-hidden="true" /> 岗位微课</span>
          <span v-if="typeof view.lecture.content.knowledge_point === 'string'" class="knowledge-tag">
            {{ learnerText(view.lecture.content.knowledge_point) }}
          </span>
        </div>
        <div v-if="lectureMetrics.length" class="lecture-key-numbers" aria-label="本节关键数据">
          <article v-for="metric in lectureMetrics" :key="`${metric.label}-${metric.value}`">
            <span>{{ metric.label }}</span>
            <strong class="instrument-number">{{ metric.value }}</strong>
          </article>
        </div>
        <div class="safe-markdown">
          <section
            v-for="(section, sectionIndex) in lectureSections"
            :key="`${sectionIndex}-${section.title ?? '正文'}`"
            class="lecture-section-card"
          >
            <h3 v-if="section.title">{{ section.title }}</h3>
            <template v-for="(block, index) in section.blocks" :key="`${index}-${block.text}`">
              <div v-if="block.kind === 'list'" class="markdown-list-item">
                <span aria-hidden="true"></span>
                <EvidenceClaim
                  :text="block.text"
                  :claims="claimsFor(view.lecture, block.text)"
                  :evidence="view.lecture.evidence"
                  :knowledge-point="lectureKnowledgePoint"
                  :matches-blind-spot="lectureMatchesBlindSpot"
                />
              </div>
              <p v-else>
                <EvidenceClaim
                  :text="block.text"
                  :claims="claimsFor(view.lecture, block.text)"
                  :evidence="view.lecture.evidence"
                  :knowledge-point="lectureKnowledgePoint"
                  :matches-blind-spot="lectureMatchesBlindSpot"
                />
              </p>
            </template>
          </section>
        </div>
        <section
          v-if="unmatchedClaims.length"
          class="claim-register"
          aria-label="结论证据"
        >
          <h4>数据出处</h4>
          <ul>
            <li v-for="claim in unmatchedClaims" :key="claim.text">
              <EvidenceClaim
                :text="claim.text"
                :claims="[claim]"
                :evidence="view.lecture.evidence"
                :knowledge-point="lectureKnowledgePoint"
                :matches-blind-spot="lectureMatchesBlindSpot"
              />
            </li>
          </ul>
        </section>
      </section>
      <section v-else class="resource-placeholder">
        <BookOpen :size="20" aria-hidden="true" />
        <p>岗位微课将在本轮测评完成后出现。</p>
      </section>

      <section
        v-if="view.task && isPracticeGuide"
        id="task-resource"
        class="task-card practice-guide-card"
        aria-label="实操指南"
      >
        <div class="task-card-heading">
          <span class="resource-eyebrow"><ListChecks :size="14" aria-hidden="true" /> 实操指南</span>
          <div
            v-if="taskDifficulty"
            class="task-difficulty-ladder"
            :aria-label="`题目难度：${taskDifficulty.label}，三级分阶中的第${taskDifficulty.ordinal}级`"
          >
            <span
              v-for="(step, index) in difficultySteps"
              :key="step"
              class="task-difficulty-step"
              :class="{ 'is-active': index === taskDifficulty.index, 'is-reached': index <= taskDifficulty.index }"
            >{{ step }}</span>
          </div>
        </div>
        <template v-if="hasGuideStructure">
          <h3 class="guide-question">{{ taskQuestion }}</h3>
          <p class="guide-intro">{{ guideIntro }}</p>
          <section class="guide-section" aria-label="操作步骤">
            <h4>操作步骤</h4>
            <ol class="guide-steps">
              <li v-for="(step, index) in guideSteps" :key="`${index}-${step}`">
                {{ step }}
              </li>
            </ol>
          </section>
          <section class="guide-section" aria-label="完成标准">
            <h4>完成标准</h4>
            <ul class="guide-criteria">
              <li v-for="(criterion, index) in guideCriteria" :key="`${index}-${criterion}`">
                {{ criterion }}
              </li>
            </ul>
          </section>
        </template>
        <p v-else class="guide-unavailable">
          这份实操指南暂时无法展示，请稍后再试。
        </p>
      </section>

      <section v-else-if="view.task" id="task-resource" class="task-card quiz-card">
        <div class="task-card-heading">
          <span class="resource-eyebrow"><ListChecks :size="14" aria-hidden="true" /> 练习题</span>
          <div
            v-if="taskDifficulty"
            class="task-difficulty-ladder"
            :aria-label="`题目难度：${taskDifficulty.label}，三级分阶中的第${taskDifficulty.ordinal}级`"
          >
            <span
              v-for="(step, index) in difficultySteps"
              :key="step"
              class="task-difficulty-step"
              :class="{ 'is-active': index === taskDifficulty.index, 'is-reached': index <= taskDifficulty.index }"
            >{{ step }}</span>
          </div>
        </div>
        <h3>{{ taskQuestion }}</h3>
        <p v-if="taskMisconception" class="probe-notice">
          <CircleAlert :size="15" aria-hidden="true" />
          本题针对：{{ taskMisconception }}
        </p>
      </section>

      <SqlResultTable v-if="view.sqlResult" :message="view.sqlResult" />
      <section v-else class="resource-placeholder compact-placeholder">
        <p>数据结果将在查询完成并确认可用后出现。</p>
      </section>
    </div>
  </main>
</template>

<style scoped>
.resource-panel.is-lesson-pager {
  min-height: 0;
  display: grid;
  grid-template-rows: 62px minmax(0, 1fr);
  overflow: hidden;
  border: 0;
  border-radius: 0;
  box-shadow: none;
}

.is-lesson-pager .resource-heading {
  min-height: 0;
  padding: 12px 16px;
}

.is-lesson-pager .resource-heading > div:first-child {
  display: grid;
  gap: 2px;
}

.is-lesson-pager .resource-heading h2 {
  font-size: 16px;
}

.is-lesson-pager .professional-review-badge {
  padding: 4px 7px;
  font-size: 9px;
}

.lesson-pager {
  min-height: 0;
  display: grid;
  grid-template-rows: minmax(0, 1fr) 52px;
  padding: 0 16px;
  overflow: hidden;
}

.lesson-page {
  min-height: 0;
  display: grid;
  align-content: start;
  gap: 16px;
  padding: 18px 2px 12px;
  overflow: auto;
}

.lesson-page-heading {
  display: flex;
  align-items: start;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 13px;
  border-bottom: 1px solid var(--border);
}

.lesson-page-heading > div {
  min-width: 0;
  display: grid;
  gap: 5px;
}

.lesson-page-heading span {
  overflow: hidden;
  color: var(--blue);
  font-size: 10px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.lesson-page-heading h3 {
  margin: 0;
  color: var(--text);
  font-size: 18px;
  line-height: 1.35;
}

.lesson-page-heading b {
  flex: 0 0 auto;
  padding: 4px 8px;
  color: var(--muted);
  font-size: 10px;
  background: #f3f4f7;
  border-radius: 999px;
}

.lesson-page-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.lesson-page-metrics article {
  min-width: 0;
  display: grid;
  gap: 6px;
  padding: 12px 10px;
  background: #f5f5f7;
  border-radius: 12px;
}

.lesson-page-metrics span {
  overflow: hidden;
  color: var(--muted);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.lesson-page-metrics strong {
  color: var(--text);
  font-size: 18px;
}

.lesson-page-overview {
  display: grid;
  gap: 14px;
  padding-top: 4px;
}

.lesson-page-overview > header {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 16px;
}

.lesson-page-overview > header div {
  display: grid;
  gap: 4px;
}

.lesson-page-overview > header span,
.lesson-page-overview > header small {
  color: var(--muted);
  font-size: 11px;
}

.lesson-page-overview > header strong {
  color: var(--text);
  font-size: 17px;
}

.lesson-overview-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 10px;
}

.lesson-overview-grid button {
  min-width: 0;
  min-height: 92px;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: start;
  gap: 12px;
  padding: 15px;
  color: inherit;
  text-align: left;
  background: #f7f8fa;
  border: 1px solid rgba(22, 43, 74, 0.08);
  border-radius: 14px;
  cursor: pointer;
  transition: border-color 160ms ease, box-shadow 160ms ease, transform 160ms ease;
}

.lesson-overview-grid button:hover {
  border-color: rgba(0, 113, 227, 0.28);
  box-shadow: 0 10px 26px rgba(22, 43, 74, 0.08);
  transform: translateY(-1px);
}

.lesson-overview-grid button > b {
  color: var(--blue);
  font-size: 11px;
}

.lesson-overview-grid button > span {
  min-width: 0;
  display: grid;
  gap: 7px;
}

.lesson-overview-grid button span > strong {
  overflow: hidden;
  color: var(--text);
  font-size: 14px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.lesson-overview-grid button span > small {
  display: -webkit-box;
  overflow: hidden;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.55;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.lesson-overview-grid button > i {
  color: var(--blue);
  font-size: 14px;
  font-style: normal;
}

.lesson-page-copy {
  display: grid;
  gap: 12px;
  color: var(--text);
  font-size: 13px;
  line-height: 1.72;
}

.lesson-page-copy p {
  margin: 0;
}

.lesson-page-evidence {
  display: grid;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.lesson-page-evidence li {
  padding: 11px 12px;
  color: var(--text);
  font-size: 12px;
  line-height: 1.6;
  background: #f5f5f7;
  border-radius: 10px;
}

.lesson-pager-actions {
  display: grid;
  grid-template-columns: 76px minmax(0, 1fr) 76px;
  align-items: center;
  gap: 8px;
  border-top: 1px solid var(--border);
}

.lesson-pager-actions button {
  min-height: 32px;
  color: var(--blue);
  font-size: 11px;
  font-weight: 650;
  background: rgba(0, 113, 227, 0.07);
  border: 0;
  border-radius: 9px;
  cursor: pointer;
}

.lesson-pager-actions button:disabled {
  color: #a6a6ab;
  background: #f5f5f7;
  cursor: default;
}

.lesson-pager-actions > span {
  display: flex;
  justify-content: center;
  gap: 4px;
}

.lesson-pager-actions i {
  width: 5px;
  height: 5px;
  background: #d2d2d7;
  border-radius: 50%;
}

.lesson-pager-actions i.is-current {
  width: 16px;
  background: var(--blue);
  border-radius: 999px;
}

.practice-guide-card {
  display: grid;
}

.guide-question {
  color: var(--text);
}

.guide-unavailable {
  margin: 0;
  color: var(--muted);
  line-height: 1.65;
}

.guide-intro {
  margin: 12px 0 16px;
  padding: 10px 12px;
  color: var(--muted);
  line-height: 1.65;
  background: #f3efe7;
  border-left: 3px solid var(--paper-amber);
}

.guide-section + .guide-section {
  margin-top: 14px;
}

.guide-section h4 {
  margin: 0 0 8px;
  color: var(--text);
  font-size: 13px;
}

.guide-steps,
.guide-criteria {
  display: grid;
  gap: 8px;
  margin: 0;
  padding-left: 24px;
  color: var(--text);
  line-height: 1.6;
}

.guide-steps li::marker {
  color: var(--paper-amber);
  font-weight: 700;
}

.guide-criteria li::marker {
  color: var(--paper-green);
}

.lesson-guide-sections {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(0, 0.85fr);
  gap: 14px;
}

.lesson-guide-sections .guide-section {
  min-width: 0;
  padding: 12px 14px;
  background: #f7f7f9;
  border-radius: 12px;
}

.lesson-guide-sections .guide-section + .guide-section {
  margin-top: 0;
}

.practice-coaching {
  display: grid;
  gap: 10px;
  padding: 12px 14px;
  background: rgba(0, 113, 227, 0.055);
  border: 1px solid rgba(0, 113, 227, 0.13);
  border-radius: 12px;
}

.practice-coaching-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.practice-coaching-heading strong,
.practice-round-summary strong {
  color: var(--text);
  font-size: 12px;
}

.practice-coaching-heading button {
  padding: 0;
  color: var(--blue);
  font-size: 11px;
  font-weight: 650;
  background: transparent;
  border: 0;
  cursor: pointer;
}

.practice-coaching > ol {
  display: grid;
  gap: 6px;
  margin: 0;
  padding-left: 20px;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.55;
}

.practice-round-summary {
  display: grid;
  gap: 5px;
  padding-top: 10px;
  border-top: 1px solid rgba(0, 113, 227, 0.12);
}

.practice-round-summary p,
.practice-round-summary small {
  margin: 0;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.55;
}

@media (max-width: 640px) {
  .lesson-guide-sections {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
