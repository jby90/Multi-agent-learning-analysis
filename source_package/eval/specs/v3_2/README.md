# v3.2 真实路由评测修复说明

## 1. 本目录交付物

- `02_正式评测50题_v3.2_真实路由_修复版.xlsx`：v3.2 正式 50 例的审核版工作簿。
- `03_TRACE字段与指标计算模板_v3.2_修复版.xlsx`：保留 5 张表、支持自动初算与双人复核最终复算的模板。
- 生产输入：`eval/cases/v3_2/formal_50_inputs_v3_2.json`。
- 独立金标准：`eval/gold/v3_2/formal_50_gold_v3_2.json`。

## 2. 本次修复

1. 将正式运行输入和冻结金标准彻底分离。生产输入仅含岗位画像、经验标签、5 题前测、最多 2 道诊断探针及学员脚本，不含目标知识点、模板或期望答案。
2. 正式运行前同时校验初始任务与最终任务的知识点、难度、载荷类型、模板族、业务任务、标准 SQL、期望行及答案要点；任何冲突均在 live 调用前 fail-closed。
3. 覆盖格按 `seed_id + case_id` 独立编号，Seed A/B 不再互相覆盖；两轮均须分别完成 10×3 覆盖门禁。
4. 有效自动适配率只统计由真实交互触发并实际改变难度、路径、任务复杂度或追问的节点；`keep` 保留审计，但不计入有效适配分子。
5. 最终发布幻觉率仅允许以 R-01、R-02、R-04、R-05、R-06 的人工规则命中作为幻觉标签依据；同一事实单元命中多条仍只计 1 次。
6. 两个辅助指标按首次生成事务或内容谱系去重，避免把多轮审核重复计数。
7. 指标沿用此前批准口径：最终发布幻觉率、有效自动适配率、核心知识点完整闭环覆盖率，以及幻觉拦截率、原生教学适配失配率两个辅助指标。

本次未修改 21 条状态转移、R-01～R-06、A 选择性辩护、B 误区选靶、T17 降阶、Text2SQL 和 SQL 沙箱。

## 3. 已通过门禁

- 真实路由：10/10 核心知识点可达，永久不可达 0，正式案例匹配 50/50，路由证据可追溯 50/50，强制注入知识点/模板 0。
- v3.2 针对性测试：20 项通过。
- 非 live 全量回归：1219 项通过，11 项 live 测试按规范排除。
- A+B 创新与第二域专项回归：44 项通过。
- 两个修复版工作簿可重新导入，公式错误扫描为 0。

## 4. 正式运行与复算

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m eval.v3_formal_runner --seed seed_A --output-dir eval/results/v3_2/formal --mode live
.\.venv\Scripts\python.exe -m eval.v3_formal_runner --seed seed_B --output-dir eval/results/v3_2/formal --mode live
```

代码、Prompt、模型、温度、知识库或配置发生变化后，Seed A/B 必须各自完整重跑 50 例；不得只补跑失败例后合并旧结果。

自动初算：

```powershell
.\.venv\Scripts\python.exe -m eval.v3_recompute `
  --run-dir eval/results/v3_2/formal/seed_A `
  --run-dir eval/results/v3_2/formal/seed_B `
  --output-dir eval/results/v3_2/recomputed `
  --mode AUTO_PRELIMINARY
```

学生完成双人复核与必要仲裁后，一键最终复算：

```powershell
.\.venv\Scripts\python.exe -m eval.v3_recompute `
  --run-dir eval/results/v3_2/formal/seed_A `
  --run-dir eval/results/v3_2/formal/seed_B `
  --output-dir eval/results/v3_2/recomputed `
  --mode FINAL_HUMAN_REVIEWED `
  --human-review eval/results/v3_2/recomputed/human_review_template_v3_2.json
```

缺少人工标签、双人意见冲突未仲裁、缺少规则依据或覆盖门禁不完整时，最终模式会报错并拒绝生成成绩。`AUTO_PRELIMINARY` 只能标记为自动初算，不得作为最终评测结果。

