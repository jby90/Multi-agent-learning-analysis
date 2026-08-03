# 多智能体协同决策系统源码包

本源码包为学生学习、作品提交和本地复现使用。包内只保留运行、构建、测试与演示所需的脱敏资产；操作说明、评测报告和内部核验材料位于独立的“文档与配置”交付物中。

## 目录说明

| 目录 | 内容 |
|---|---|
| `agents/` | 岗位诊断、知识生成、任务生成、数据验证与专业检查 |
| `orchestrator/` | 学习流程、审核闭环、消息记录与交互会话 |
| `frontend/` | 学员实操、回放、三画像对比与协同视图 |
| `config/domains/` | 主域与第二域配置 |
| `data/比赛数据包/` | 建库所需的 5 个 CSV、数据字典和导入脚本 |
| `traces/` | 5 条脱敏冻结回放，其中 4 条供前端演示，1 条供第二域契约测试 |
| `evidence/replays/` | 自然审核驳回的脱敏回放素材 |
| `eval/` | 非在线测试、固定评测用例与指标计算代码 |

包内不携带依赖目录或内部发布审计文件。冻结回放直接从 `traces/` 读取。

## 数据库准备

`data/比赛数据包/` 固定包含以下 7 个文件：

- `dim_date.csv`
- `dim_process.csv`
- `dim_ship.csv`
- `dim_workshop.csv`
- `fact_production_progress.csv`
- `data_dictionary.md`
- `import_mysql.sql`

在 MySQL 8 环境中启用 `local_infile` 后，于该目录执行：

```powershell
mysql --local-infile=1 -u root -p < import_mysql.sql
```

Docker 交付物会在首次启动数据库容器时自动完成同一批数据的导入。

## 后端安装与非在线测试

```powershell
python -m pip install -r requirements.txt
$env:FIRST_SEGMENT_RAW_ROOT = "<获授权的第一域和第二域测试数据根目录>"
python -m pytest eval -m "not live" -q
```

`FIRST_SEGMENT_RAW_ROOT` 只用于运行需要受控测试数据的用例。不得把未授权原始数据复制进源码包，也不得用临时数据替代后声称完成全量门禁。

`pytest.ini` 默认排除需要真实模型或数据库的 `live` 测试。只有在正式密钥、只读数据库和运行窗口均已准备好时，才显式运行：

```powershell
python -m pytest eval -m live -q
```

## 前端测试与构建

```powershell
Set-Location frontend
npm ci
npm test -- --run
npm run build
```

源码包保留已构建的 `frontend/dist/`，也可根据锁定依赖重新构建。依赖目录与临时构建产物不随包交付。

## 安全边界

- 只使用包内脱敏数据，不接入真实组织信息或未授权数据。
- 查询仅在只读安全范围内执行；未通过检查的查询不会运行。
- 未通过专业检查的内容不会向学员展示。
- 重复误区的下一目标由知识库共标关系、岗位责任范围和已探查历史确定；模型不能自由改写路由结果。
- 产物生成或状态转换异常会安全结束当前会话，并以幂等方式返回公开错误文案。
- 学员界面不显示内部状态、协议字段、技术枚举或错误堆栈。
- 不在提交包中写入账号、密钥、访问令牌、本机路径或内部审计信息。
