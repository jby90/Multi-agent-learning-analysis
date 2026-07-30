<script setup lang="ts">
import { BookOpen, Check, ChevronDown, CircleCheck, ClipboardCheck, Wrench } from '@lucide/vue'
import { computed } from 'vue'

import type {
  InteractiveResourceBranch,
  InteractiveResourceBundle,
} from '../lib/interactiveApi'


const props = defineProps<{ bundle: InteractiveResourceBundle }>()

const branchMeta: Record<InteractiveResourceBranch['branch_id'], {
  label: string
  detail: string
}> = {
  knowledge: { label: '岗位微课', detail: '知识证据已组织' },
  practice: { label: '实操任务', detail: '业务任务已锚定' },
  assessment: { label: '分阶测验', detail: '难度与画像已匹配' },
}

const branches = computed(() => props.bundle.branches.map((branch) => ({
  ...branch,
  ...branchMeta[branch.branch_id],
})))

const readyCount = computed(() => branches.value.filter((branch) => branch.status === 'ready').length)

function difficultyLabel(value: string | undefined): string {
  return ({ basic: '基础', applied: '应用', advanced: '进阶' })[value ?? ''] ?? '待定'
}
</script>

<template>
  <details class="resource-bundle-strip" aria-label="本轮学习资源状态">
    <summary>
      <span class="bundle-summary-copy">
        <CircleCheck :size="18" aria-hidden="true" />
        <span>
          <strong>微课、实操与测验已准备</strong>
          <small>内容围绕同一学习目标组织</small>
        </span>
      </span>
      <span class="bundle-summary-status">
        <em>{{ readyCount }}/{{ branches.length }} 已就绪</em>
        <span>查看说明</span>
        <ChevronDown :size="15" aria-hidden="true" />
      </span>
    </summary>
    <div class="bundle-details">
      <header>
        <strong>本轮学习资源</strong>
        <code :title="bundle.bundle_id">RB · {{ bundle.bundle_id.slice(-6) }}</code>
      </header>
      <div class="resource-branches">
        <article
          v-for="branch in branches"
          :key="branch.branch_id"
          :class="{ 'is-ready': branch.status === 'ready' }"
        >
          <BookOpen v-if="branch.branch_id === 'knowledge'" :size="17" aria-hidden="true" />
          <Wrench v-else-if="branch.branch_id === 'practice'" :size="17" aria-hidden="true" />
          <ClipboardCheck v-else :size="17" aria-hidden="true" />
          <span>
            <b>{{ branch.label }}</b>
            <small>{{ branch.status === 'ready' ? branch.detail : '正在重新准备' }}</small>
          </span>
          <em>{{ difficultyLabel(branch.difficulty) }}</em>
          <Check v-if="branch.status === 'ready'" :size="13" aria-label="已生成" />
        </article>
      </div>
      <footer>三项内容基于同一学习目标，并已完成一致性检查。</footer>
    </div>
  </details>
</template>

<style scoped>
.resource-bundle-strip { margin: 0; color: #17364a; background: #f7fafc; border-bottom: 1px solid #e3eaf0; }
summary { min-height: 48px; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 8px 18px; cursor: pointer; list-style: none; }
summary::-webkit-details-marker { display: none; }
.bundle-summary-copy { min-width: 0; display: flex; align-items: center; gap: 9px; }
.bundle-summary-copy > svg { flex: 0 0 auto; color: #20a069; }
.bundle-summary-copy > span { min-width: 0; display: grid; gap: 1px; }
.bundle-summary-copy strong { font-size: 13px; }
.bundle-summary-copy small { color: #71808c; font-size: 10px; }
.bundle-summary-status { flex: 0 0 auto; display: flex; align-items: center; gap: 9px; color: #6d7d89; font-size: 10px; }
.bundle-summary-status em { padding: 4px 7px; color: #27775b; background: #e6f5ec; border-radius: 999px; font-style: normal; font-weight: 700; }
.bundle-summary-status svg { transition: transform 160ms ease; }
details[open] .bundle-summary-status svg { transform: rotate(180deg); }
.bundle-details { display: grid; gap: 9px; padding: 10px 18px 13px; border-top: 1px solid #e3eaf0; }
header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
header strong { font-size: 12px; }
header code { padding: 4px 7px; color: #687986; background: #edf2f5; border-radius: 999px; font: 700 9px/1 ui-monospace,monospace; }
.resource-branches { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 8px; }
article { position: relative; display: grid; grid-template-columns: 24px minmax(0,1fr) auto 14px; gap: 5px; align-items: center; min-height: 48px; padding: 7px 8px; background: rgba(255,255,255,.75); border: 1px solid #d9e5eb; }
article > svg:first-child { color: #5590ad; }
article.is-ready > svg:first-child { color: #1685ce; }
article span { display: grid; gap: 2px; min-width: 0; }
article b { font-size: 12px; }
article small { overflow: hidden; color: #6c8290; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
article em { padding: 3px 5px; color: #936116; background: #fff4df; border-radius: 4px; font-size: 10px; font-style: normal; }
article > svg:last-child { color: #20a069; }
footer { color: #748a96; font-size: 10px; text-align: center; }
@media (max-width: 760px) { summary { align-items: flex-start; } .bundle-summary-status > span { display: none; } .resource-branches { grid-template-columns: 1fr; } }
</style>
