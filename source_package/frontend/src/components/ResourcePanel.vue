<script setup lang="ts">
import { BookOpen, CircleAlert, ListChecks } from '@lucide/vue'
import { computed } from 'vue'

import {
  dataFieldLabel,
  learnerText,
  learnerTextOr,
  misconceptionLabel,
} from '../lib/tracePresentation'
import type { TraceMessage, TraceView } from '../types/trace'
import EvidenceClaim from './EvidenceClaim.vue'
import SqlResultTable from './SqlResultTable.vue'


const props = defineProps<{ view: TraceView }>()

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
  return sections
})

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
  <main class="panel resource-panel">
    <header class="panel-heading resource-heading">
      <div>
        <h2>微课 / 实操</h2>
      </div>
      <div class="resource-heading-status">
        <span v-if="hasApprovedResource" class="professional-review-badge">
          已通过专业审核 ✓
        </span>
        <BookOpen :size="19" aria-hidden="true" />
      </div>
    </header>

    <div class="resource-scroll">
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
</style>
