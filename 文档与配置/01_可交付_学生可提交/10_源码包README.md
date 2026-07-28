# 多智能体协同决策系统源码包说明

源码包用于学生学习、作品提交、评委审阅和本地复现。包内只保留运行、构建、测试与演示所需的脱敏资产；本目录中的其他文档与源码包同版。

## 包内目录

| 目录 | 内容 |
|---|---|
| `agents/` | 岗位诊断、知识生成、任务生成、数据验证与专业检查 |
| `orchestrator/` | 学习流程、审核闭环、消息记录与交互会话 |
| `frontend/` | 学员实操、回放、三画像对比与协同视图 |
| `config/domains/` | 主域与第二域配置 |
| `data/比赛数据包/` | 5 个 CSV、数据字典和 MySQL 导入脚本，共 7 个建库文件 |
| `traces/` | 5 条脱敏冻结回放，其中 4 条供前端演示，1 条供第二域契约测试 |
| `evidence/replays/` | 自然审核驳回的脱敏回放素材 |
| `eval/` | 非在线测试、固定评测用例与指标计算代码 |

源码包不携带文档副本、正式评测结果、依赖目录或内部发布审计文件。冻结回放直接从 `traces/` 读取。

## 数据库准备

在 `data/比赛数据包/` 中启用 MySQL `local_infile` 后执行：

```powershell
mysql --local-infile=1 -u root -p < import_mysql.sql
```

Docker 交付物会在首次启动数据库容器时自动完成同一批数据导入。

## 后端非在线测试

```powershell
python -m pip install -r requirements.txt
$env:FIRST_SEGMENT_RAW_ROOT = "<获授权的第一域和第二域测试数据根目录>"
python -m pytest eval -k "not live" -q
```

不得把未授权原始数据复制进源码包，也不得用临时数据替代后声称完成全量门禁。

## 前端测试与构建

```powershell
Set-Location frontend
npm ci
npm test -- --run
npm run build
```

源码包保留已构建的 `frontend/dist/`。依赖目录与临时构建产物不随包交付。

## 安全边界

- 只使用包内脱敏数据，不接入真实组织信息或未授权数据。
- 查询仅在只读安全范围内执行；未通过检查的查询不会运行。
- 未通过专业检查的内容不会向学员展示。
- 学员界面不显示内部状态、协议字段、技术枚举或错误堆栈。
- 不在提交包中写入账号、密钥、访问令牌、本机路径或内部审计信息。
