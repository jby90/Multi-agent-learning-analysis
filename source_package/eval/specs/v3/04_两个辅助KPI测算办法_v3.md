# 两个辅助KPI测算办法 v3

这两个指标只用于证明审核与纠偏机制有效，不与三项核心指标并列。

## KPI-1：幻觉拦截率

### 公式

```text
幻觉拦截率
=
首次原生生成中，经独立复核确认错误，且被自动审核发现并阻止原样发布的事实单元数
÷
首次原生生成中，经独立复核确认错误的全部事实单元数
```

### 计数规则

- 计数单位：`content_unit_id`；同一事实单元命中多条规则只计1次。
- 分母条件：`first_generation=1` 且 `human_label=HALLUCINATION`。
- 分子还须同时满足：存在 `review_event_id`，自动审核明确发现问题，并触发拦截、重生成或受控修正，最终未原样发布。
- 人工后来发现并修改、但自动审核未发现的，不计为自动拦截成功。
- 普通SQL语法或权限错误不计幻觉；只有无效/空查询被用于支撑事实结论时才计。
- 同一内容多轮审核按 `lineage_unit_id` 去重。

### TRACE字段

`content_unit_id`、`lineage_unit_id`、`generation_stage`、`first_generation`、`human_label`、`review_event_id`、`rule_hits`、`auto_review_detected`、`auto_intercepted`、`published_final`。

### 对外报告

分别报告两轮的 `分子/分母=百分比`，并同时展示“首次生成错误数 → 自动拦截数 → 最终残留错误数”。

---

## KPI-2：原生教学适配失配率

### 公式

```text
原生教学适配失配率
=
首次生成、首轮被R-03驳回且经独立复核确认适配失配的教学事务数
÷
全部接受首轮R-03审核的首次生成教学事务数
```

### 计数规则

- 计数单位：`first_gen_transaction_id`；同一事务多轮审核只计1次。
- 分母条件：`r03_reviewed_first_gen=1`。
- 分子条件：`r03_rejected_first_gen=1` 且 `human_mismatch_confirmed=1`。
- 该指标**越低越好**，不能把高驳回率包装为系统能力强。
- R-03误杀不进入分子；最终以独立人工复核/仲裁标签为准。
- 同时展示两项辅助数量，但不新增KPI：
  - 初次失配后最终修复并获批的事务数；
  - 最终仍残留适配失配的事务数。

### TRACE字段

`first_gen_transaction_id`、`artifact_id`、`artifact_version_id`、`lineage_id`、`generation_stage`、`review_event_id`、`review_cycle`、`review_rule_hits`、`r03_reviewed_first_gen`、`r03_rejected_first_gen`、`human_mismatch_confirmed`、`r03_repair_approved`、`final_residual_mismatch`。

### 对外报告

分别报告两轮的 `分子/分母=百分比`，并展示“首次生成事务 → R-03确认失配 → 修复获批 → 最终残留”的纠偏链。
