# 完整学情数据样例

> 交付视图包含画像输入、协同中间数据与最终学习资源。每组同时保留未经改写的完整 trace JSONL，供离线回放与逐消息核验。

- 来源：当前正式 50×2 终验 Round 2
- 样例数：3

| 样例 | case | 画像 | 路径 |
|---|---|---|---|
| `01_planner_new_rebuttal_corrected` | `E2E-008` | `planner_new` | `rebuttal_corrected` |
| `02_craft_engineer_direct_correct` | `E2E-012` | `craft_engineer` | `direct_correct` |
| `03_line_leader_second_wrong_step_down` | `E2E-030` | `line_leader` | `second_wrong_step_down` |

每个目录内：

- `完整trace.jsonl`：从正式成功 attempt 逐字复制，可直接交给 trace 回放器。
- `完整学情数据.json`：同一真实 trace 按“画像输入—协同中间数据—最终资源”建立索引，消息正文不改写。

完整性门禁覆盖画像与前测、诊断、讲义及引用、任务、SQL 提交与执行、审核、可选反证、最终学习路径和完成态。
