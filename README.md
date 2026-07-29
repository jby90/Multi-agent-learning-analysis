# 多智能体协同决策系统

面向船舶制造数字化岗位培训的、证据约束的多智能体个性化学习系统。系统根据岗位画像和岗前测评生成微课、实操任务与分阶测验，让学员在只读安全边界内查询比赛生产数据，再通过专业审核、反证追问和路径更新完成“诊断—学习—实操—核验—纠偏—再学习”闭环。

当前推荐版本为创新 A+B 版：

- 代码冻结：`6e2042e`，标签 `innovation-ab-code-freeze-20260729`
- 最终评测与部署证据：`9ad5623`，标签 `innovation-ab-final-20260729`
- 当前入口：`http://127.0.0.1:18082/`
- 完整项目说明：[18_当前项目完整说明](文档与配置/01_可交付_学生可提交/18_当前项目完整说明.md)
- 最终评测摘要：[17_创新AB版本说明与最终评测](文档与配置/01_可交付_学生可提交/17_创新AB版本说明与最终评测.md)
- 学生探索测试：[19_学生全流程探索测试清单](文档与配置/01_可交付_学生可提交/19_学生全流程探索测试清单.md)
- 初学者 GitHub 指南：[20_GitHub初学者下载部署与更新指南](文档与配置/01_可交付_学生可提交/20_GitHub初学者下载部署与更新指南.md)

## 初学者先看这里

已经部署旧版的同学不要覆盖原目录。请把学生测试分支克隆到一个新文件夹：

```powershell
git clone `
  --branch agent/poll-recovery-student-test `
  --single-branch `
  https://github.com/jby90/Multi-agent-learning-analysis.git `
  Multi-agent-learning-analysis-student-test
```

进入新目录后，从原项目复制本机 `Docker镜像/.env`，再按照 [20_GitHub初学者下载部署与更新指南](文档与配置/01_可交付_学生可提交/20_GitHub初学者下载部署与更新指南.md) 构建并启动。不要提交 `.env`，不要执行 `docker compose down -v`。

## 核心能力

- 三种岗位画像：新入职生产计划员、转岗数字化工艺工程师、一线班组长。
- 五类 Agent：学情诊断、领域知识、实操任务、数据验证、专业审核。
- 有序主链与三处局部并行：三源证据检索、三路资源生成、四维质量审核。
- Learning Contract 与 Evidence Bundle：冻结同轮画像、目标、难度、质量策略和证据边界。
- R-01～R-06 审核闭环：只有 R-02/R-03 可进入受控反馈，硬规则保持 fail-closed。
- 创新 A：事务级选择性辩护，限制模型辩护调用并隔离审核内部语言。
- 创新 B：审核约束的边际误区支持覆盖路由，抑制零增益跳转与重复关系贡献。
- Text2SQL 与只读沙箱：模板权限、AST 检查、表列白名单、最多 200 行。
- Vue 实操、JSONL 会话回放和实时协同舱，Agent 状态及并行 Join 由 SSE 事件驱动。
- 主域 `production_progress` 与第二域 `first_segment` 配置包及独立回归证据。

本项目不是让多个模型自由群聊。底层协同模式是：

> 有序状态机 + 局部并行 DAG + 多轴独立审核 + 确定性汇聚 + 争议点定向处理。

## 当前验证结果

冻结版本 `6e2042e` 的最终门禁：

- 后端非 live：`1100 passed, 22 deselected`
- A+B/Review/自由追问组合压力：`51 passed`
- A/B 定向消融与第二域固定桩复核：`36 passed`
- 前端：`28 files, 278 tests passed`
- 前端生产构建：成功

本次学生探索热修复只调整前端会话轮询恢复，不修改后端状态机、创新 A/B 或三指标算法；新增 3 项轮询/HTTP 状态测试后，当前前端为 `28 files, 281 tests passed`。

正式固定 50 例执行两轮，每个案例每轮只执行一次：

| 指标 | Round 1 | Round 2 |
|---|---:|---:|
| 幻觉率 | 2/987 = 0.2026% | 2/973 = 0.2055% |
| 知识覆盖率 | 10/10 = 100% | 10/10 = 100% |
| 自动适配率 | 163/163 = 100% | 159/159 = 100% |
| 到达学习终点 | 49/50 = 98% | 47/50 = 94% |

两轮三项比赛指标均达到最高档阈值。四条 R-02 不支持草稿均被 Review 识别并阻断，但仍按既定算法计入，因此不声称“幻觉率绝对为 0”。正式 50×2 与第二域迁移证据采用不同口径，不能表述为“两个领域各完成 50 例”。

## 三个版本并存

| 版本 | 默认入口 | 用途 |
|---|---|---|
| 旧版 1.1.0 | `http://127.0.0.1:18080/` | 保留学生原环境和历史对照 |
| 原协同重构版 | `http://127.0.0.1:18081/` | 并行协同版本对照 |
| 创新 A+B 版 | `http://127.0.0.1:18082/` | 当前推荐演示与开发版本 |

三版拥有独立前后端容器和运行记录，只共享原比赛数据库网络及只读数据库账号。升级不需要重新导入数据库。

## 启动创新 A+B 版

已有旧版数据库的环境，在仓库根目录准备好 `Docker镜像/.env` 后执行：

```powershell
docker build --file .\docker\backend\Dockerfile --tag multiagent-decision-backend:innovation-ab-6e2042e .
docker build --file .\docker\frontend\Dockerfile --tag multiagent-decision-frontend:innovation-ab-6e2042e .

docker compose `
  --env-file .\Docker镜像\.env `
  --file .\Docker镜像\docker-compose.innovation-ab.yml `
  up --detach --wait --wait-timeout 120
```

打开 `http://127.0.0.1:18082/`。详细的三版并存、端口覆盖、停止和历史离线镜像说明见 [Docker 部署说明](Docker镜像/README.md)。

停止创新版时不要附加 `-v`：

```powershell
docker compose `
  --env-file .\Docker镜像\.env `
  --file .\Docker镜像\docker-compose.innovation-ab.yml `
  down
```

不要对任何保留数据的 Compose 执行 `docker compose down -v`。

## 本地开发与测试

后端：

```powershell
Set-Location source_package
python -m pip install -r requirements.txt
python -m pytest eval -k "not live" -q
```

前端：

```powershell
Set-Location source_package\frontend
npm ci
npm test -- --run
npm run build
```

正式模型调用需要在本机环境变量或 `Docker镜像/.env` 中提供 `DASHSCOPE_API_KEY`。测试默认排除 live，不应把正式 Key 写入源码或提交到 Git。

## 仓库导航

| 路径 | 内容 |
|---|---|
| `source_package/agents/` | 五类 Agent、审核分支、Text2SQL、SQL 沙箱和岗位画像 |
| `source_package/orchestrator/` | 21 条状态转移、交互会话、审核闭环和 SSE 事件 |
| `source_package/coordination/` | 学习契约、证据包、有界并行与确定性汇聚 |
| `source_package/config/domains/` | 主域与第二域配置包 |
| `source_package/frontend/` | 实操、回放、协同视图与 Agent 动效 |
| `source_package/eval/` | 回归、正式 runner、三指标和创新 A/B 消融 |
| `Docker镜像/` | 三版并存的 Compose 与部署说明 |
| `文档与配置/01_可交付_学生可提交/` | 学生、评委和现场演示可使用文档 |
| `文档与配置/02_不可交付_内部材料/` | 内部复核、因果审计和答辩材料，不随学生作品提交 |

源码包使用说明见 [source_package/README.md](source_package/README.md)，全部文档见 [文档与配置目录索引](文档与配置/README_目录索引.md)。

## 安全说明

- 发布源码和镜像不包含模型 Key、数据库密码或本机路径。
- 每位使用者保留自己的 `Docker镜像/.env`，不得提交该文件。
- 数据库访问使用只读账号；未通过模板权限、沙箱和 Review 的查询不会执行。
- 未通过专业审核的教学内容不会进入学员界面。
- 学员侧不显示规则号、状态枚举、消息 ID、答案键、覆盖集合或错误堆栈。
- 实时服务当前定位为本地比赛演示系统，不应未经身份认证和网络加固直接暴露到公网。
