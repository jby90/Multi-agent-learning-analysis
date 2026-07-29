# 创新 A+B 组合门禁与最终冻结证据

- 日期：2026-07-29
- 冻结提交：`6e2042e`
- 冻结标签：`innovation-ab-code-freeze-20260729`
- 创新 A 提交：`eed8a59`
- 创新 B 提交：`6e2042e`

## 一、组合门禁

- A/B/Review/自由追问组合压力集：`51 passed`。
- 后端非 live 全量：`1100 passed, 22 deselected`。
- 前端：`28 files, 278 tests passed`；生产构建成功。
- A 的两次软驳回重生成共用同一份 B 覆盖快照；生产默认辩护模型预算为 1，后续走确定性让步并完整复审。
- 最终 approve 只提交一次关系支持覆盖；`refuse`、`ReviewFlowInterrupted`、关系完整性 `system_error`、准备失败和幂等重放均不新增覆盖。
- 第二域完整固定桩会话 B0/B1 公开行为等价、覆盖为空、无主域误区串入。
- 前端未出现 `covered_relation_points`、`route_support_points`、`marginal_support` 或 `RelationIntegrityError`。
- R-01～R-06、S-01～S-09、21 条转移、ReviewAgent、沙箱、三指标算法和正式 runner 哈希保持创新前基线值。

## 二、正式 50×2

两轮均使用冻结提交 `6e2042e`、固定 50 例矩阵、每例一次、`attempt=1`；没有续跑、重试、换样本或调参。失败 attempt 原样进入账本、数据集、成本和到达率。

| 项目 | Round 1 | Round 2 |
|---|---:|---:|
| 幻觉率 | 2/987 = 0.2026% | 2/973 = 0.2055% |
| 覆盖率 | 10/10 = 100% | 10/10 = 100% |
| 自动适配率 | 163/163 = 100% | 159/159 = 100% |
| 到达终点 | 49/50 = 98% | 47/50 = 94% |
| safe_rejected | 0 | 2 |
| system_error | 1 | 1 |
| tokens | 623,400 | 602,721 |
| 成本 | ¥1.867134 | ¥1.779732 |

三指标在两轮均通过最高档阈值；两轮离线计算各自双遍逐位一致。覆盖率和自动适配率保持 100%。但若把“无回退”解释为必须保持历史正式批次的绝对 0 幻觉分子，则本轮并未满足：两轮各记录 2 条由 Review 捕获的 R-02 不支持草稿事件。该数值必须如实披露，不能仅以低于 3% 阈值掩盖。

Round 1 两条幻觉事件均来自 E2E-010 同一草稿的原审核与复审，均被 Review 标记为 `review_r02_unsupported`；未作为批准教学内容放行。Round 2 同样为 E2E-010 的原审核与复审。正式指标按既定算法仍计入，算法未放宽。

## 三、停止与路由观测

- Round 1 E2E-018：讲义三次命中 R-04 预检失败，系统 fail-closed，trace 只到 T02。
- Round 2 E2E-003、E2E-005：模型 SQL 输出 `process_order`，任务权威要求 `process_code`；查询权限门拒绝，执行 T21，SQL 未进入数据库执行。
- Round 2 E2E-012：讲义三次命中 R-04 预检失败，系统 fail-closed。
- Round 1 E2E-013：路由器预测 Q7、最终模型判定 OUT_OF_SCOPE，系统拒绝越界请求；记录 1 次路由 family mismatch，但没有错误 SQL 放行。Round 2 mismatch 为 0。

这 4 个停止案例与 1 个路由观测都保留，系统性静默放行为 0。到达率必须与三指标分列。

## 四、权威哈希与成本

| 工件 | Round 1 | Round 2 |
|---|---|---|
| ledger SHA-256 | `06ee2d9ae9d8c1c59542e54dbac73c13ef09e230637465e11bc48d88d0514b33` | `fd3a41afbaef6b36d720363641e1347c81729f199297839076f354dab8c2b775` |
| dataset SHA-256 | `d3e7bd2dcf51500be7a1a30aee0b4643105b183c7b9e34f60acf3b6c5c8d0007` | `2d50cc84f9982697d2a9eb7e878b6b91932f4da4e2c9dffd909e86e8ea0324db` |
| 双遍指标摘要 | `b9c1c35cc718d6dc29f1f4e7ff969b1750c914cf645709ff9d3c48467eb4000e` | `e6635d8a0d4b12276781e83eab04321571406187ed9c9f74a91c535ada74f2b8` |

两轮合计 888 个持久化 API 结果、1,226,121 tokens、¥3.646866。机器摘要为 `source_package/eval/results/innovation_ab_final_summary.json`。

## 五、Docker 终验与版本保留

- 18080：旧版 1.1.0，健康。
- 18081：原 `coordination-32d3e49` 协同版，健康。
- 18082：创新 A+B 版，健康。
- 创新版容器内组合测试：`51 passed`；HTTP 首页、创建会话、5 道前测均通过。
- 容器内默认值：`rebuttal_budget=1`、`feedback_mode=mapped`、`routing_mode=marginal_support`。

规范 Dockerfile 的 clean build 因 Docker Hub 匿名 token 接口连续返回 `EOF` 而未完成。为完成本机运行终验，使用已验证的 `coordination-32d3e49` 运行时镜像作为依赖层，覆盖冻结源码和已通过测试的前端 dist；该覆盖构建通过容器测试和 HTTP 烟测，但不冒充 clean build。学生在 Docker Hub 可访问时仍按规范 Dockerfile 构建。

## 六、证据边界

正式 50×2 的 runner 不实例化自由追问组件，因此它只证明最终共享状态机、审核链、证据链与三指标门槛；不直接证明 A 降低真实辩护成本，也不证明 B 提高教学效果。A/B 的直接证据分别来自固定桩消融、Review/证据绑定、确定性约束和组合压力集。

第二域迁移证据继续独立报告，只能证明配置驱动、安全退化和零串域。专业人员抽检只用于题目、答案和证据的专业正确性，不参与 B 采用门，也不证明教学有效性。
