# 数据字典

## 1. fact_production_progress.csv

粒度：脱敏船号 × 工序 × 责任单元 × 日期。共6516行，不含表头。

| 字段 | 类型 | 含义 | 单位/格式 | 来源或规则 |
|---|---|---|---|---|
| progress_id | VARCHAR(16) | 事实记录唯一编号 | P+7位数字 | 构建脚本生成 |
| ship_no | VARCHAR(16) | 脱敏船号 | H2601—H2606 | 顺序泛化编码 |
| process_code | VARCHAR(8) | 工序代码 | YCL/ZZTP/AZTP | 业务工序标准化 |
| workshop_code | VARCHAR(8) | 脱敏责任单元代码 | WSA—WSF | 泛化编码 |
| period_date | DATE | 统计日期 | YYYY-MM-DD | 连续日级建模 |
| plan_qty | DECIMAL(14,2) | 计划数量 | 标准任务量 | 基线值×${SCALE_FACTOR} |
| actual_qty | DECIMAL(14,2) | 实际完成数量 | 标准任务量 | 基线值×${SCALE_FACTOR}，指定组合注入异常 |
| complete_rate | DECIMAL(10,4) | 完成率 | 0—1+ | actual_qty/plan_qty |
| deviation_rate | DECIMAL(10,4) | 偏差率 | -1—1+ | complete_rate-1 |
| delay_days | INT | 延期天数 | 天 | 按完成率和异常阶段派生 |
| risk_level | VARCHAR(4) | 风险等级 | 高/中/低 | 偏差<-15%高；-15%至-5%中；其余低 |
| batch_code | VARCHAR(32) | 泛化批次与月份 | 文本 | 脱敏批次+月份 |
| quality_pass_qty | DECIMAL(14,2) | 质量合格数量 | 标准任务量 | 实际量×96.5%—99.5% |
| rework_qty | DECIMAL(14,2) | 返工数量 | 标准任务量 | 实际量-质量合格量 |
| source_type | VARCHAR(16) | 数据性质 | 衍生模拟 | 固定标识 |
| anomaly_flag | VARCHAR(16) | 异常注入阶段 | 空/源头异常/滞后传导/传导衰减 | 异常规则 |

## 2. dim_ship.csv

| 字段 | 类型 | 含义 |
|---|---|---|
| ship_no | VARCHAR(16) | 脱敏船号，主键 |
| ship_type | VARCHAR(16) | 泛化船型 |
| batch_no | VARCHAR(16) | 泛化项目批次 |

## 3. dim_process.csv

| 字段 | 类型 | 含义 |
|---|---|---|
| process_code | VARCHAR(8) | 工序代码，主键 |
| process_name | VARCHAR(32) | 工序中文名 |
| process_order | INT | 工序顺序，1预处理、2制作托盘、3安装托盘 |

## 4. dim_date.csv

覆盖2025-02-01至2025-07-31每日，共181行。字段包括日期键、日期、年、季度、月、月份标签、ISO周、日、星期和周末标记。

## 5. dim_workshop.csv

包含6个脱敏责任单元。每道工序对应2个责任单元，字段包括责任单元代码、泛化名称、所属工序和泛化职责。

## 数据清洗记录

- 源表只用于字段和分布校准，没有直接导出原始明细，因此不存在从原表剔除记录。
- 生成后的事实表无主键重复、无必填字段缺失、无孤立外键。
- 清洗剔除比例：0%，满足≤10%的要求。
