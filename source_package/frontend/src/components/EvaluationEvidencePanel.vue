<script setup lang="ts">
import { Activity, Check, Gauge, TimerReset, Zap } from '@lucide/vue'
import { computed } from 'vue'

import type { InteractiveCoordinationEvidence } from '../lib/interactiveApi'

const props = defineProps<{ evidence?: InteractiveCoordinationEvidence | null }>()
const summary = computed(() => props.evidence?.summary)
const stages = computed(() => props.evidence?.stages ?? [])

function duration(milliseconds = 0): string {
  if (milliseconds < 1000) return `${Math.max(milliseconds, 0)} ms`
  return `${(milliseconds / 1000).toFixed(milliseconds < 10000 ? 2 : 1)} s`
}

function percent(value = 0): string {
  return `${Math.round(Math.max(0, Math.min(value, 1)) * 100)}%`
}

function parallelWidth(serial: number, parallel: number): string {
  if (serial <= 0) return '100%'
  return `${Math.max(8, Math.min(100, (parallel / serial) * 100))}%`
}
</script>

<template>
  <section class="evaluation-evidence" aria-label="协同效能评测证据">
    <header class="evaluation-heading">
      <div>
        <span>COORDINATION EVIDENCE</span>
        <h3>协同效能证据</h3>
      </div>
      <strong><Activity :size="12" aria-hidden="true" /> LIVE · SAME RUN</strong>
    </header>

    <div v-if="summary?.completed_parallel_stages" class="evaluation-live">
      <div class="evaluation-score">
        <small>本轮并行加速</small>
        <b>{{ summary.speedup.toFixed(2) }}<em>×</em></b>
        <span>{{ summary.completed_parallel_stages }} 个并行阶段 · 最大 {{ summary.max_fan_out }} 路</span>
      </div>
      <dl class="evaluation-metrics">
        <div>
          <dt><TimerReset :size="12" aria-hidden="true" /> 实际墙钟</dt>
          <dd>{{ duration(summary.parallel_elapsed_ms) }}</dd>
        </div>
        <div>
          <dt><Gauge :size="12" aria-hidden="true" /> 配对串行估算</dt>
          <dd>{{ duration(summary.paired_serial_estimate_ms) }}</dd>
        </div>
        <div class="is-saving">
          <dt><Zap :size="12" aria-hidden="true" /> 本轮节省</dt>
          <dd>{{ duration(summary.saved_ms) }}</dd>
        </div>
        <div>
          <dt><Check :size="12" aria-hidden="true" /> 分支成功</dt>
          <dd>{{ percent(summary.branch_success_rate) }}</dd>
        </div>
      </dl>
      <ol class="evaluation-stages" aria-label="并行阶段实测">
        <li v-for="stage in stages" :key="`${stage.stage_id}-${stage.correlation_id}`">
          <div>
            <strong>{{ stage.label }}</strong>
            <span>{{ stage.fan_out }} 路 · {{ stage.speedup.toFixed(2) }}×</span>
          </div>
          <div class="stage-bars" aria-hidden="true">
            <i class="serial-bar"></i>
            <i class="parallel-bar" :style="{ width: parallelWidth(stage.paired_serial_estimate_ms, stage.parallel_elapsed_ms) }"></i>
          </div>
          <small>{{ duration(stage.parallel_elapsed_ms) }} / 串行估算 {{ duration(stage.paired_serial_estimate_ms) }}</small>
        </li>
      </ol>
    </div>
    <div v-else class="evaluation-waiting">
      <Gauge :size="22" aria-hidden="true" />
      <span><b>等待并行阶段汇聚</b>完成岗前测评并打开微课后生成本轮实测。</span>
    </div>

    <footer class="official-baseline">
      <div>
        <span>正式评测集 50×2</span>
        <strong>正式质量基线</strong>
      </div>
      <dl>
        <div><dt>幻觉率</dt><dd>0%</dd></div>
        <div><dt>覆盖率</dt><dd>100%</dd></div>
        <div><dt>适配率</dt><dd>100%</dd></div>
        <div><dt>终点到达</dt><dd>98%</dd></div>
      </dl>
    </footer>
    <p class="evaluation-note">串行值为同一次执行中各分支墙钟耗时之和；输入、证据和模型条件完全配对。基线为正式评测集两轮（96% 与 100%）的综合结果，与本轮实测分列展示。</p>
  </section>
</template>

<style scoped>
.evaluation-evidence{overflow:hidden;color:#c7e1ed;background:linear-gradient(145deg,#061724,#092232);border:1px solid rgba(74,202,241,.25);border-radius:12px;box-shadow:0 14px 34px rgba(0,0,0,.22)}
.evaluation-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:13px 14px 11px;border-bottom:1px solid rgba(102,196,229,.12)}
.evaluation-heading>div{display:grid;gap:4px}.evaluation-heading span,.official-baseline span{color:#51d5ff;font:800 10px/1.2 ui-monospace,monospace;letter-spacing:.12em}.evaluation-heading h3{margin:0;color:#effbff;font-size:16px}.evaluation-heading>strong{display:inline-flex;align-items:center;gap:5px;padding:7px 9px;color:#61dfa5;font:800 10px/1 ui-monospace,monospace;background:rgba(58,211,146,.07);border:1px solid rgba(71,218,158,.19);border-radius:999px}
.evaluation-live{display:grid;grid-template-columns:142px minmax(0,1fr);gap:14px;padding:14px 16px}.evaluation-score{display:grid;align-content:center;justify-items:center;gap:6px;min-height:124px;background:radial-gradient(circle,rgba(45,205,244,.12),transparent 68%);border:1px solid rgba(77,205,241,.16);border-radius:10px}.evaluation-score small{color:#7397a8;font-size:11px}.evaluation-score b{color:#66e1ff;font:800 30px/1 ui-monospace,monospace;text-shadow:0 0 18px rgba(57,207,247,.24)}.evaluation-score em{margin-left:2px;font-size:13px;font-style:normal}.evaluation-score span{color:#6e91a1;font-size:10px}
.evaluation-metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:0}.evaluation-metrics>div{padding:10px;background:rgba(255,255,255,.025);border:1px solid rgba(112,185,213,.1);border-radius:8px}.evaluation-metrics dt{display:flex;align-items:center;gap:5px;color:#6f93a4;font-size:10px}.evaluation-metrics dd{margin:6px 0 0;color:#d5edf6;font:800 13px/1 ui-monospace,monospace}.evaluation-metrics .is-saving dd{color:#5be09f}
.evaluation-stages{grid-column:1/-1;display:grid;gap:7px;margin:0;padding:0;list-style:none}.evaluation-stages li{display:grid;grid-template-columns:142px minmax(0,1fr) 152px;align-items:center;gap:10px;padding:9px 10px;background:rgba(255,255,255,.018);border-radius:7px}.evaluation-stages li>div:first-child{display:grid;gap:3px}.evaluation-stages strong{color:#b9d7e4;font-size:11px}.evaluation-stages span,.evaluation-stages small{color:#607f8e;font:700 10px/1.3 ui-monospace,monospace}.evaluation-stages small{text-align:right}.stage-bars{position:relative;height:12px}.stage-bars i{position:absolute;left:0;height:3px;border-radius:4px}.serial-bar{top:1px;width:100%;background:rgba(143,164,177,.22)}.parallel-bar{bottom:1px;background:linear-gradient(90deg,#2bc5ef,#5add9d);box-shadow:0 0 8px rgba(47,207,224,.25)}
.evaluation-waiting{display:flex;align-items:center;justify-content:center;gap:12px;min-height:104px;color:#507383}.evaluation-waiting span{display:grid;gap:4px;font-size:11px}.evaluation-waiting b{color:#a9c8d5;font-size:12px}.official-baseline{display:flex;align-items:center;gap:14px;padding:12px 16px;background:rgba(255,255,255,.025);border-top:1px solid rgba(111,190,219,.1)}.official-baseline>div{display:grid;gap:4px;min-width:106px}.official-baseline strong{color:#d4eaf3;font-size:12px}.official-baseline dl{flex:1;display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin:0}.official-baseline dl div{display:flex;align-items:baseline;justify-content:space-between;gap:4px;padding:8px;background:rgba(4,17,27,.34);border-radius:6px}.official-baseline dt{color:#668696;font-size:10px}.official-baseline dd{margin:0;color:#61dfa5;font:800 12px/1 ui-monospace,monospace}.evaluation-note{margin:0;padding:9px 16px 11px;color:#5d7d8c;font-size:10px;line-height:1.5;border-top:1px solid rgba(111,190,219,.07)}
@media(max-width:900px){.evaluation-live{grid-template-columns:1fr}.evaluation-stages{grid-column:auto}.evaluation-stages li{grid-template-columns:1fr}.evaluation-stages small{text-align:left}.official-baseline{align-items:flex-start;flex-direction:column}.official-baseline dl{width:100%}}
</style>
