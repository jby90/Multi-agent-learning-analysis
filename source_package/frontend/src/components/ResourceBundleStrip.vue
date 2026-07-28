<script setup lang="ts">
import { BookOpen, Check, ClipboardCheck, Wrench } from '@lucide/vue'
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

function difficultyLabel(value: string | undefined): string {
  return ({ basic: '基础', applied: '应用', advanced: '进阶' })[value ?? ''] ?? '待定'
}
</script>

<template>
  <section class="resource-bundle-strip" aria-label="本轮三路教学资源包">
    <header>
      <span>
        <b>RESOURCE BUNDLE</b>
        <strong>三路教学资源已汇聚</strong>
      </span>
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
          <small>{{ branch.status === 'ready' ? branch.detail : '等待单分支重试' }}</small>
        </span>
        <em>{{ difficultyLabel(branch.difficulty) }}</em>
        <Check v-if="branch.status === 'ready'" :size="13" aria-label="已生成" />
      </article>
    </div>
    <footer>
      <span>同一学习契约</span>
      <i aria-hidden="true"></i>
      <span>同一证据包</span>
      <i aria-hidden="true"></i>
      <strong>独立生成 · 确定性汇聚</strong>
    </footer>
  </section>
</template>

<style scoped>
.resource-bundle-strip { display: grid; gap: 10px; margin-bottom: 16px; padding: 12px 14px; color: #17364a; background: linear-gradient(135deg,#f5fbff,#f3faf6); border: 1px solid #c8dce6; border-top: 2px solid #2796e7; box-shadow: 0 8px 24px rgba(37,78,102,.08); }
header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
header span { display: grid; gap: 2px; }
header b { color: #167dcc; font: 700 9px/1 ui-monospace,monospace; letter-spacing: .12em; }
header strong { font-size: 13px; }
header code { padding: 4px 7px; color: #27775b; background: #e6f5ec; border: 1px solid #bfdfcc; border-radius: 999px; font: 700 9px/1 ui-monospace,monospace; }
.resource-branches { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 8px; }
article { position: relative; display: grid; grid-template-columns: 24px minmax(0,1fr) auto 14px; gap: 5px; align-items: center; min-height: 48px; padding: 7px 8px; background: rgba(255,255,255,.75); border: 1px solid #d9e5eb; }
article > svg:first-child { color: #5590ad; }
article.is-ready > svg:first-child { color: #1685ce; }
article span { display: grid; gap: 2px; min-width: 0; }
article b { font-size: 11px; }
article small { overflow: hidden; color: #6c8290; font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
article em { padding: 2px 4px; color: #936116; background: #fff4df; border-radius: 3px; font-size: 8px; font-style: normal; }
article > svg:last-child { color: #20a069; }
footer { display: flex; align-items: center; justify-content: center; gap: 8px; color: #748a96; font-size: 9px; }
footer i { width: 20px; height: 1px; background: #b9ccd5; }
footer strong { color: #287a5d; }
@media (max-width: 760px) { .resource-branches { grid-template-columns: 1fr; } footer { flex-wrap: wrap; } }
</style>
