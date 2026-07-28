import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import EvaluationEvidencePanel from './EvaluationEvidencePanel.vue'
import type { InteractiveCoordinationEvidence } from '../lib/interactiveApi'

const evidence: InteractiveCoordinationEvidence = {
  schema_version: 1,
  measurement_basis: 'same_run_branch_wall_time_sum',
  baseline_label: '同批分支串行耗时估算',
  stages: [{
    stage_id: 'resource-generation', label: '三路资源生成', correlation_id: 'eb-1', fan_out: 3,
    parallel_elapsed_ms: 120, paired_serial_estimate_ms: 250, saved_ms: 130,
    speedup: 2.083, succeeded: true,
  }],
  summary: {
    completed_parallel_stages: 1, max_fan_out: 3, parallel_elapsed_ms: 120,
    paired_serial_estimate_ms: 250, saved_ms: 130, speedup: 2.083, branch_success_rate: 1,
  },
  quality_flow: { review_decisions: 1, first_pass_approvals: 1, first_pass_rate: 1, debate_triggers: 0, local_regenerations: 0 },
  token_usage: { prompt_tokens: 100, completion_tokens: 30, total_tokens: 130 },
}

describe('EvaluationEvidencePanel', () => {
  it('renders paired same-run speed evidence and the official quality baseline', () => {
    const wrapper = mount(EvaluationEvidencePanel, { props: { evidence } })
    expect(wrapper.text()).toContain('2.08×')
    expect(wrapper.text()).toContain('三路资源生成')
    expect(wrapper.text()).toContain('250 ms')
    expect(wrapper.text()).toContain('正式质量基线')
    expect(wrapper.text()).toContain('幻觉率0%')
    expect(wrapper.text()).toContain('终点到达98%')
    expect(wrapper.text()).toContain('同一次执行')
  })

  it('does not fabricate live speed before a parallel stage has completed', () => {
    const wrapper = mount(EvaluationEvidencePanel)
    expect(wrapper.text()).toContain('等待并行阶段汇聚')
    expect(wrapper.text()).not.toContain('本轮并行加速')
    expect(wrapper.text()).toContain('正式质量基线')
  })
})
