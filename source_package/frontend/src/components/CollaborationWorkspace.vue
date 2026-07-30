<script setup lang="ts">
import { Activity, ChartNoAxesCombined, Network } from '@lucide/vue'
import { computed, ref } from 'vue'

import type {
  AgentActivityEvent,
  InteractiveCoordinationEvidence,
  InteractiveEvidenceBundle,
  InteractiveLearningContract,
  InteractiveResourceBundle,
} from '../lib/interactiveApi'
import type { TraceView } from '../types/trace'
import AgentStage from './AgentStage.vue'
import EvaluationEvidencePanel from './EvaluationEvidencePanel.vue'
import TracePanel from './TracePanel.vue'


const props = withDefaults(defineProps<{
  view: TraceView
  events?: AgentActivityEvent[]
  contract?: InteractiveLearningContract
  evidenceBundle?: InteractiveEvidenceBundle
  resourceBundle?: InteractiveResourceBundle
  coordinationEvidence?: InteractiveCoordinationEvidence | null
}>(), {
  events: () => [],
  contract: undefined,
  evidenceBundle: undefined,
  resourceBundle: undefined,
  coordinationEvidence: undefined,
})

type WorkspacePage = 'stage' | 'evidence' | 'records'
const page = ref<WorkspacePage>('stage')

const pages = computed(() => [
  {
    id: 'stage' as const,
    label: '实时协同',
    detail: '查看 Agent 分工与接力',
    icon: Network,
  },
  {
    id: 'evidence' as const,
    label: '效能证据',
    detail: props.coordinationEvidence ? '本轮实测已生成' : '等待并行阶段汇聚',
    icon: ChartNoAxesCombined,
  },
  {
    id: 'records' as const,
    label: '协作记录',
    detail: `${props.view.visibleMessages.length} 条可审阅记录`,
    icon: Activity,
  },
])
</script>

<template>
  <section class="collaboration-workbench" aria-label="协同视图工作台">
    <header class="collaboration-workbench-heading">
      <div>
        <span class="section-kicker">协同工作台</span>
        <strong>{{ pages.find((item) => item.id === page)?.label }}</strong>
      </div>
      <nav role="tablist" aria-label="切换协同信息页面">
        <button
          v-for="item in pages"
          :key="item.id"
          type="button"
          role="tab"
          :aria-selected="page === item.id"
          :class="{ 'is-active': page === item.id }"
          @click="page = item.id"
        >
          <component :is="item.icon" :size="15" aria-hidden="true" />
          <span><b>{{ item.label }}</b><small>{{ item.detail }}</small></span>
        </button>
      </nav>
    </header>

    <div class="collaboration-page-stack">
      <AgentStage
        v-show="page === 'stage'"
        class="collaboration-page is-stage"
        :view="view"
        :events="events"
        :contract="contract"
        :evidence-bundle="evidenceBundle"
        :resource-bundle="resourceBundle"
      />
      <EvaluationEvidencePanel
        v-show="page === 'evidence'"
        class="collaboration-page is-evidence"
        :evidence="coordinationEvidence"
      />
      <TracePanel
        v-show="page === 'records'"
        class="collaboration-page is-records"
        :view="view"
      />
    </div>
  </section>
</template>
