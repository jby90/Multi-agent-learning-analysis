<script setup lang="ts">
import { BookOpen, CircleAlert, ListChecks } from '@lucide/vue'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import {
  dataFieldLabel,
  learnerText,
  learnerTextOr,
  misconceptionLabel,
} from '../lib/tracePresentation'
import type { TraceMessage, TraceView } from '../types/trace'
import EvidenceClaim from './EvidenceClaim.vue'
import WaveText from './WaveText.vue'
import SqlResultTable from './SqlResultTable.vue'


const props = withDefaults(defineProps<{
  view: TraceView
  lessonPager?: boolean
  guidanceFeedback?: string
  guidanceNextStepReason?: string
  autoJumpToTask?: boolean
  sqlResultStale?: boolean
  liveOperation?: boolean
  /** 闭环四后继：实操任务须在学员点击"领取实操任务"后才展示详情 */
  taskClaimed?: boolean
  /** 需求⑨：提问环节查阅讲义时展示完整微课（不受"练习态只留任务卡"限制）。 */
  peekLecture?: boolean
  chainRunning?: boolean
  practiceChainActive?: boolean
  /** 闭环五：画像三 data_present（系统代执行查询）——不展示 SQL 练习素材（查询题目卡/提示）。 */
  dataPresentMode?: boolean
}>(), {
  lessonPager: false,
  autoJumpToTask: true,
  sqlResultStale: false,
  liveOperation: false,
  taskClaimed: true,
  peekLecture: false,
  chainRunning: false,
  practiceChainActive: false,
  dataPresentMode: false,
})
const emit = defineEmits<{
  startPractice: []
  pageState: [value: {
    index: number
    total: number
    kind?: 'metrics' | 'section' | 'evidence' | 'task'
    isLast: boolean
  }]
}>()
/* 数据是否属于当前任务：sql_result 的步号早于最新任务消息时，
   说明数据是上一轮练习查的——新题已出、新数据未到，隐藏旧数据。 */
const staleTaskResult = computed(() => {
  const sqlResult = props.view.sqlResult
  const task = props.view.task
  if (!sqlResult || !task) return false
  return sqlResult.step < task.step
})
const focusMode = ref(false)
const guidanceHintLevel = ref(0)


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

const lessonPages = computed<LessonPage[]>(() => {
  const pages: LessonPage[] = []
  // 需求⑥：实训叠页只保留帮助学员掌握知识的小节内容，
  // "本节关键数据""依据要点/数据出处"不展示（回放模式不受影响）。
  if (!props.lessonPager && lectureMetrics.value.length) {
    pages.push({ key: 'metrics', title: '本节关键数据', kind: 'metrics' })
  }
  lectureSections.value.forEach((section, index) => {
    const title = section.title || `知识卡片 ${index + 1}`
    // 需求⑥：面向学员只保留掌握知识点所需内容，"依据要点/数据出处"类溯源小节不展示
    if (props.lessonPager && /依据要点|数据出处/.test(title)) return
    pages.push({
      key: `section-${index}`,
      title,
      kind: 'section',
      section,
    })
  })
  // 优化12：数据出处页删除（回放与 live 同口径）
  // 闭环五：画像三 data_present 系统代执行——学员不写 SQL，任务卡（查询题目/提示）不进入叠页
  if (props.view.task && !props.dataPresentMode) {
    pages.push({
      key: `task-${props.view.task.msgId}`,
      title: props.view.task.payloadType === 'practice_guide' ? '实操指南' : '练习题',
      kind: 'task',
    })
  }
  return pages
})
// 叠页模式：微课整页连续呈现，进入练习环节后才切换为"末页（任务页）"语义，
// 供 App 布局在微课全宽与实操双栏之间切换。
const practiceEntered = ref(false)
// 需求③：进入练习后左栏只保留任务卡（讲义内容退场，双栏聚焦题目+查询）；
// 画像三 data_present 无任务卡——保持整页微课（追问态由 is-followup-focus 让位整宽提问）
const displayLessonPages = computed(() => (
  practiceEntered.value && !props.peekLecture && !props.dataPresentMode
    ? lessonPages.value.filter((page) => page.kind === 'task')
    : lessonPages.value
))

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
  () => {
    practiceEntered.value = false
  },
)
// ①③：前测全对（讲义延期）→ 自动切布局为练习态（practiceEntered+pageState）
// 但不触发 startPractice——推进由 submitPretest 内部链处理（避免双 advance 竞争）
watch(
  [
    () => props.view.lecture?.content.lecture_deferred,
    () => Boolean(props.view.task),
    () => props.liveOperation,
  ],
  ([deferred, hasTask, live]) => {
    if (live && deferred === true && hasTask && !practiceEntered.value) {
      practiceEntered.value = true
      emit('pageState', {
        index: Math.max(0, lessonPages.value.length - 1),
        total: lessonPages.value.length,
        kind: 'task',
        isLast: true,
      })
    }
  },
  { immediate: true },
)
watch(
  [() => lessonPages.value.length, practiceEntered],
  () => emit('pageState', {
    index: practiceEntered.value ? Math.max(0, lessonPages.value.length - 1) : 0,
    total: lessonPages.value.length,
    kind: practiceEntered.value
      ? 'task'
      : (lessonPages.value[0]?.kind ?? 'metrics'),
    isLast: practiceEntered.value,
  }),
  { immediate: true },
)

// 链结束后设 practiceEntered（隐藏讲义内容+"进入练习"按钮）
// 链中不设（画像三讲义全宽可见）；链尾 state 已到 sql/follow_up，布局自然切
watch(
  () => props.practiceChainActive,
  (running, wasRunning) => {
    if (wasRunning && !running && !practiceEntered.value && props.liveOperation) {
      practiceEntered.value = true
    }
  },
)

function enterPractice(): void {
  // 先 startPractice（链头同步锁布局 lesson→讲义全宽），
  // 后 pageState（isLast=true 供链尾释放后布局自然切 practice）
  // 不设 practiceEntered——其 watcher 会立即过滤内容+二次 pageState（闪切）
  emit('startPractice')
  emit('pageState', {
    index: Math.max(0, lessonPages.value.length - 1),
    total: lessonPages.value.length,
    kind: 'task',
    isLast: true,
  })
}

function lessonSectionPreview(section: LectureSection): string {
  return section.blocks
    .slice(0, 2)
    .map((block) => learnerText(block.text))
    .join(' ')
    .slice(0, 120)
}

function openLessonSection(sectionIndex: number): void {
  // 叠页模式：滚动定位到对应知识卡片
  const pageIndex = lessonPages.value.findIndex((page) => page.key === `section-${sectionIndex}`)
  const target = document.getElementById(`lesson-page-${pageIndex >= 0 ? pageIndex : 0}`)
  if (target instanceof HTMLElement) target.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
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

/** 优化16：quiz_set（查询题）任务同样展示"操作步骤/完成标准"——
 * 由题面的 query_authority 确定性推导，与 practice_guide 的呈现结构统一。 */
const quizGuideSteps = computed(() => {
  const authority = props.view.task?.content.query_authority
  const record = typeof authority === 'object' && authority !== null && !Array.isArray(authority)
    ? authority as Record<string, unknown>
    : undefined
  const outputs = guideStringList(record?.output_columns).map(dataFieldLabel)
  const filters = guideStringList(record?.filter_columns).map(dataFieldLabel)
  const groups = guideStringList(record?.group_by_columns).map(dataFieldLabel)
  return [
    filters.length
      ? `确认题目对象与时间范围，明确要查询的${filters.join('、')}。`
      : '确认题目对象与时间范围，明确要查询的内容。',
    outputs.length
      ? `查询并输出题面要求的字段：${outputs.join('、')}。`
      : '查询并输出题面要求的结果字段。',
    groups.length
      ? `结果按${groups.join('、')}对应，逐行核对数值后再作答。`
      : '逐行核对查询结果的数值与口径后再作答。',
  ]
})

const quizGuideCriteria = computed(() => [
  '查询结果包含题面要求的全部字段，且数值与结果表一致。',
  '结论引用了查询结果中的字段名和数值，未混用不同口径。',
])

const showGuideSections = computed(() => (
  (isPracticeGuide.value && hasGuideStructure.value) || !isPracticeGuide.value
))
const unifiedGuideSteps = computed(() => (
  isPracticeGuide.value ? guideSteps.value : quizGuideSteps.value
))
const unifiedGuideCriteria = computed(() => (
  isPracticeGuide.value ? guideCriteria.value : quizGuideCriteria.value
))

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
  const hints = [
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
  if (filters.includes('工序') && timeValues.length > 1) {
    hints.push('题目把多个工序与月份一一对应时，筛选条件也要保留这种对应关系，不能只查询整个总时间范围。')
  }
  return hints
})

const taskMisconception = computed(() => {
  const value = props.view.task?.content.misconception
  return typeof value === 'string' ? misconceptionLabel(value) : undefined
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
    <!-- 优化12：面板头行整体删除——live 学员界面无"内容已就绪✓/专注学习"等字样，
         回放与 live 同口径（2026-08-18 用户口径：回放不得出现 live 中不存在的元素） -->


    <section v-if="lessonPager" class="lesson-pager lesson-stack" aria-label="微课连续阅读">
      <template v-if="view.lecture && lessonPages.length">
        <template v-for="(page, pageIndex) in displayLessonPages" :key="page.key">
          <!-- 需求④：未进入练习（锁定）的任务页整块隐藏，点击底部按钮后再显示 -->
          <article
            v-if="page.kind !== 'task' || taskClaimed"
            :id="`lesson-page-${pageIndex}`"
            class="lesson-page"
          >
        <!-- 需求②：去页码与重复知识点，仅保留小节标题 -->
        <header v-if="page.kind !== 'task'" class="lesson-page-heading">
          <h3>{{ page.title }}</h3>
        </header>

        <div v-if="page.kind === 'metrics'" class="lesson-page-metrics">
          <article v-for="metric in lectureMetrics" :key="`${metric.label}-${metric.value}`">
            <span>{{ metric.label }}</span>
            <strong class="instrument-number">{{ metric.value }}</strong>
          </article>
        </div>

        <section
          v-if="focusMode && page.kind === 'metrics' && lectureSections.length"
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
          v-else-if="page.kind === 'section' && page.section"
          class="lesson-page-copy"
        >
          <template
            v-for="(block, index) in page.section.blocks"
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
          v-else-if="page.kind === 'task' && view.task && !taskClaimed"
          id="task-resource"
          class="lesson-page-task task-card task-locked-card"
          aria-label="本轮练习题（待进入）"
        >
          <div class="task-locked-copy">
            <ListChecks :size="18" aria-hidden="true" />
            <p>练习内容已就绪。阅读完上方微课后，点击页面底部<strong>「进入练习环节」</strong>开始本次练习。</p>
          </div>
        </section>

        <section
          v-else-if="page.kind === 'task' && view.task"
          id="task-resource"
          class="lesson-page-task task-card"
          :class="isPracticeGuide ? 'practice-guide-card' : 'quiz-card'"
          aria-label="本轮练习题"
        >
          <div class="task-card-heading">
            <span class="resource-eyebrow">
              <ListChecks :size="14" aria-hidden="true" />
              查询题目
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
          <!-- 优化16：查询题与实操指南统一展示"操作步骤/完成标准"（quiz 由题面授权确定性推导） -->
          <p v-if="isPracticeGuide && hasGuideStructure" class="guide-intro">{{ guideIntro }}</p>
          <div v-if="showGuideSections" class="lesson-guide-sections">
            <section class="guide-section" aria-label="操作步骤">
              <h4>操作步骤</h4>
              <ol class="guide-steps">
                <li v-for="(step, index) in unifiedGuideSteps" :key="`${index}-${step}`">{{ step }}</li>
              </ol>
            </section>
            <section class="guide-section" aria-label="完成标准">
              <h4>完成标准</h4>
              <ul class="guide-criteria">
                <li v-for="(criterion, index) in unifiedGuideCriteria" :key="`${index}-${criterion}`">
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
              <strong>提示</strong>
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
        </article>
        </template>
      </template>
      <section v-else class="resource-placeholder">
        <BookOpen :size="20" aria-hidden="true" />
        <p>岗位微课将在本轮测评完成后出现。</p>
      </section>

      <!-- 闭环五：任务已下发（taskClaimed）后按钮不再滞留——链中显示"正在进入…"，
           链结束即隐藏（画像三 S7 代执行窗口/画像一二 S7+sql 都无需再点"进入练习"）；
           practiceEntered watcher 兜底失效时由 !taskClaimed 兜住 -->
      <footer
        v-if="liveOperation && view.task && lessonPages.length && !practiceEntered && (!taskClaimed || chainRunning)"
        class="lesson-stack-actions"
      >
        <button
          type="button"
          class="lesson-start-practice"
          aria-label="进入练习环节"
          @click="enterPractice"
        :disabled="props.chainRunning"
        >{{ props.chainRunning ? '' : '进入练习 →' }}<WaveText v-if="props.chainRunning" text="正在进入…" /></button>
      </footer>
    </section>

    <div v-else class="resource-scroll">
      <section v-if="view.lecture" id="lecture-resource" class="lecture-card">
        <!-- 优化12：回放与 live 同口径——删"岗位微课"eyebrow 与知识点角标 -->

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
        <!-- 优化12：删"数据出处"证据登记小节（live 学员界面无此元素） -->

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
        <!-- 优化16：回放分支查询题同样展示操作步骤/完成标准（与 live 同口径） -->
        <div v-if="!isPracticeGuide" class="lesson-guide-sections">
          <section class="guide-section" aria-label="操作步骤">
            <h4>操作步骤</h4>
            <ol class="guide-steps">
              <li v-for="(step, index) in quizGuideSteps" :key="`${index}-${step}`">
                {{ step }}
              </li>
            </ol>
          </section>
          <section class="guide-section" aria-label="完成标准">
            <h4>完成标准</h4>
            <ul class="guide-criteria">
              <li v-for="(criterion, index) in quizGuideCriteria" :key="`${index}-${criterion}`">
                {{ criterion }}
              </li>
            </ul>
          </section>
        </div>
        <p v-if="taskMisconception" class="probe-notice">
          <CircleAlert :size="15" aria-hidden="true" />
          本题针对：{{ taskMisconception }}
        </p>
      </section>

      <SqlResultTable
        v-if="view.sqlResult && !sqlResultStale && !staleTaskResult"
        :message="view.sqlResult"
        :live-operation="liveOperation"
      />
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
  /* 面板头行（原 62px 首行）已删除：叠页独占整行铺满 */
  grid-template-rows: minmax(0, 1fr);
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

/* 需求②：叠页模式整体单滚流——各小节不再独立滚动成"下滑块" */
.lesson-pager.lesson-stack {
  display: block;
  overflow: auto;
  padding: 8px 16px 0;
}

.lesson-stack .lesson-page {
  overflow: visible;
  gap: 12px;
  padding: 6px 2px;
}

.lesson-stack .lesson-page-heading {
  padding-bottom: 6px;
}

.lesson-stack .lesson-page-heading h3 {
  font-size: 15px;
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
  grid-template-columns: repeat(2, minmax(0, 1fr));
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
