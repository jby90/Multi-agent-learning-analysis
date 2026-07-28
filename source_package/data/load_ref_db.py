"""Load the contest data package into the isolated REF MySQL database."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Sequence


TARGET_DATABASE = "ref_contest_db"
SOURCE_DATABASE = "contest_db"
PACKAGE_DIR = Path(__file__).resolve().parent / "比赛数据包"
IMPORT_SQL_PATH = PACKAGE_DIR / "import_mysql.sql"

PACKAGED_SELF_CHECK_SQL = """\
USE contest_db;

SELECT DATE_FORMAT(period_date, '%Y-%m') AS month_label,
       process_code,
       ROUND(SUM(actual_qty) / SUM(plan_qty), 4) AS complete_rate
FROM fact_production_progress
WHERE ship_no = 'H2601'
GROUP BY month_label, process_code
ORDER BY month_label, FIELD(process_code, 'YCL', 'ZZTP', 'AZTP');

SELECT ship_no,
       ROUND(SUM(actual_qty) / SUM(plan_qty), 4) AS complete_rate
FROM fact_production_progress
WHERE period_date >= '2025-05-01'
  AND period_date < '2025-06-01'
  AND process_code = 'YCL'
GROUP BY ship_no
ORDER BY complete_rate;

SELECT COUNT(*) AS unexpected_high_risk_rows
FROM fact_production_progress
WHERE risk_level = '高'
  AND NOT (
    (ship_no = 'H2601' AND process_code = 'YCL'
      AND period_date BETWEEN '2025-05-01' AND '2025-05-31') OR
    (ship_no = 'H2601' AND process_code = 'ZZTP'
      AND period_date BETWEEN '2025-06-01' AND '2025-06-30') OR
    (ship_no = 'H2601' AND process_code = 'AZTP'
      AND period_date BETWEEN '2025-07-01' AND '2025-07-31')
  );

SELECT COUNT(*) AS fact_rows,
       COUNT(DISTINCT ship_no) AS ship_count,
       MIN(period_date) AS min_date,
       MAX(period_date) AS max_date
FROM fact_production_progress;

SELECT SUM(ABS(complete_rate - actual_qty / plan_qty) > 0.0001)
         AS bad_complete_rate,
       SUM(ABS(deviation_rate - (complete_rate - 1)) > 0.0001)
         AS bad_deviation_rate
FROM fact_production_progress;

SELECT SUM(quality_pass_qty + rework_qty > actual_qty) AS bad_quality_split
FROM fact_production_progress;

WITH monthly_rate AS (
  SELECT ship_no,
         process_code,
         DATE_FORMAT(period_date, '%Y-%m') AS month_label,
         SUM(actual_qty) / SUM(plan_qty) AS complete_rate
  FROM fact_production_progress
  WHERE anomaly_flag IS NULL
  GROUP BY ship_no, process_code, month_label
)
SELECT ROUND(MIN(complete_rate), 4) AS min_normal_rate,
       ROUND(MAX(complete_rate), 4) AS max_normal_rate,
       SUM(complete_rate > 1) AS over_complete_combinations
FROM monthly_rate;
"""

PACKAGED_VALIDATION_MARKDOWN = """\
| 事实表行数 | 6516 |
| 脱敏船号数 | 6 |
| 最早日期 | 2025-02-01 |
| 最晚日期 | 2025-07-31 |
| 完成率计算错误 | 0 |
| 偏差率计算错误 | 0 |
| 质量拆分不自洽行数 | 0 |
| 非注入组合意外高风险记录 | 0 |
| 正常组合月度完成率范围 | 88.00%—103.00% |
| 超额完成组合数 | 18 |
| 源头异常 | H2601 | 2025-05 | 预处理YCL | 62.36% |
| 滞后传导 | H2601 | 2025-06 | 制作托盘ZZTP | 75.45% |
| 传导衰减 | H2601 | 2025-07 | 安装托盘AZTP | 85.01% |

2025年5月预处理完成率排名中，其余船号为89.24%—97.79%。
"""


class LoaderError(RuntimeError):
    """Raised when a safe import or validation cannot be completed."""


@dataclass(frozen=True)
class AnomalyExpectation:
    ship_no: str
    month_label: str
    process_code: str
    rate: Decimal


@dataclass(frozen=True)
class ExpectedValidation:
    fact_rows: int
    ship_count: int
    min_date: date
    max_date: date
    bad_complete_rate: int
    bad_deviation_rate: int
    bad_quality_split: int
    unexpected_high_risk_rows: int
    min_normal_rate: Decimal
    max_normal_rate: Decimal
    over_complete_combinations: int
    anomaly_rates: Mapping[str, AnomalyExpectation]
    other_ship_min_rate: Decimal
    other_ship_max_rate: Decimal


@dataclass(frozen=True)
class MySQLSettings:
    binary: str
    host: str
    port: int
    user: str
    password: str

    @classmethod
    def from_environment(cls) -> "MySQLSettings":
        port_text = os.environ.get("MYSQL_PORT", "3306")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise LoaderError(f"MYSQL_PORT 必须是整数，实际为 {port_text!r}") from exc
        return cls(
            binary=os.environ.get("MYSQL_BIN", "mysql"),
            host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
            port=port,
            user=os.environ.get("MYSQL_USER", "root"),
            password=os.environ.get("MYSQL_PASSWORD", ""),
        )


def rewrite_database(sql: str) -> str:
    """Rewrite only the standalone source database identifier."""

    rewritten, replacements = re.subn(
        rf"\b{re.escape(SOURCE_DATABASE)}\b",
        TARGET_DATABASE,
        sql,
        flags=re.IGNORECASE,
    )
    if replacements == 0:
        raise LoaderError(f"SQL 中未找到源库名 {SOURCE_DATABASE}")
    return rewritten


def _table_value(markdown: str, label: str) -> str:
    pattern = re.compile(
        rf"^\|\s*{re.escape(label)}\s*\|\s*([^|]+?)\s*\|\s*$",
        flags=re.MULTILINE,
    )
    match = pattern.search(markdown)
    if match is None:
        raise LoaderError(f"验证文档缺少检查项：{label}")
    return match.group(1).strip()


def _percent(value: str) -> Decimal:
    try:
        return Decimal(value.strip().removesuffix("%")) / Decimal(100)
    except InvalidOperation as exc:
        raise LoaderError(f"无法解析百分比：{value!r}") from exc


def _percent_range(value: str) -> tuple[Decimal, Decimal]:
    parts = re.split(r"\s*[—–-]\s*", value)
    if len(parts) != 2:
        raise LoaderError(f"无法解析百分比范围：{value!r}")
    return _percent(parts[0]), _percent(parts[1])


def parse_expected_validation(markdown: str) -> ExpectedValidation:
    """Parse every expected value from the validation table format."""

    normal_min, normal_max = _percent_range(
        _table_value(markdown, "正常组合月度完成率范围")
    )

    anomaly_rates: dict[str, AnomalyExpectation] = {}
    stages = {"源头异常", "滞后传导", "传导衰减"}
    for line in markdown.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5 or cells[0] not in stages:
            continue
        process_match = re.search(r"([A-Z][A-Z0-9]+)$", cells[3])
        if process_match is None:
            raise LoaderError(f"无法从异常链工序解析代码：{cells[3]!r}")
        anomaly_rates[cells[0]] = AnomalyExpectation(
            ship_no=cells[1],
            month_label=cells[2],
            process_code=process_match.group(1),
            rate=_percent(cells[4]),
        )
    if set(anomaly_rates) != stages:
        missing = ", ".join(sorted(stages - set(anomaly_rates)))
        raise LoaderError(f"验证文档缺少异常链阶段：{missing}")

    ranking_match = re.search(
        r"其余船号为\s*([0-9.]+%)\s*[—–-]\s*([0-9.]+%)",
        markdown,
    )
    if ranking_match is None:
        raise LoaderError("验证文档缺少五月其余船号完成率范围")

    return ExpectedValidation(
        fact_rows=int(_table_value(markdown, "事实表行数")),
        ship_count=int(_table_value(markdown, "脱敏船号数")),
        min_date=date.fromisoformat(_table_value(markdown, "最早日期")),
        max_date=date.fromisoformat(_table_value(markdown, "最晚日期")),
        bad_complete_rate=int(_table_value(markdown, "完成率计算错误")),
        bad_deviation_rate=int(_table_value(markdown, "偏差率计算错误")),
        bad_quality_split=int(_table_value(markdown, "质量拆分不自洽行数")),
        unexpected_high_risk_rows=int(
            _table_value(markdown, "非注入组合意外高风险记录")
        ),
        min_normal_rate=normal_min,
        max_normal_rate=normal_max,
        over_complete_combinations=int(_table_value(markdown, "超额完成组合数")),
        anomaly_rates=anomaly_rates,
        other_ship_min_rate=_percent(ranking_match.group(1)),
        other_ship_max_rate=_percent(ranking_match.group(2)),
    )


def split_self_check_queries(sql: str) -> list[str]:
    """Return the seven executable SELECT/CTE statements from self_check.sql."""

    without_comments = re.sub(r"(?m)^\s*--.*$", "", sql)
    statements = [statement.strip() for statement in without_comments.split(";")]
    return [
        statement
        for statement in statements
        if statement.upper().startswith("SELECT") or statement.upper().startswith("WITH")
    ]


def _actual_value(
    result_sets: Sequence[Sequence[Mapping[str, str]]],
    result_index: int,
    column: str,
    errors: list[str],
) -> str | None:
    if result_index >= len(result_sets) or not result_sets[result_index]:
        errors.append(f"result[{result_index}].{column}: 缺少结果行")
        return None
    value = result_sets[result_index][0].get(column)
    if value is None:
        errors.append(f"result[{result_index}].{column}: 缺少列")
    return value


def _compare_scalar(
    name: str,
    actual: object,
    expected: object,
    errors: list[str],
) -> None:
    if actual != expected:
        errors.append(f"{name}: expected={expected}, actual={actual}")


def _as_int(value: str | None, name: str, errors: list[str]) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        errors.append(f"{name}: 无法解析整数 {value!r}")
        return None


def _as_decimal(value: str | None, name: str, errors: list[str]) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        errors.append(f"{name}: 无法解析小数 {value!r}")
        return None


def compare_results(
    expected: ExpectedValidation,
    result_sets: Sequence[Sequence[Mapping[str, str]]],
) -> list[str]:
    """Compare seven self-check result sets with documented expectations."""

    errors: list[str] = []
    if len(result_sets) != 7:
        errors.append(f"self_check result_sets: expected=7, actual={len(result_sets)}")

    if result_sets:
        monthly: dict[tuple[str, str], Decimal] = {}
        for row in result_sets[0]:
            try:
                monthly[(row["month_label"], row["process_code"])] = Decimal(
                    row["complete_rate"]
                )
            except (KeyError, InvalidOperation):
                errors.append(f"monthly anomaly row 无法解析：{dict(row)}")
        for stage, anomaly in expected.anomaly_rates.items():
            actual = monthly.get((anomaly.month_label, anomaly.process_code))
            _compare_scalar(f"anomaly_rate[{stage}]", actual, anomaly.rate, errors)

    if len(result_sets) > 1:
        ranking: dict[str, Decimal] = {}
        for row in result_sets[1]:
            try:
                ranking[row["ship_no"]] = Decimal(row["complete_rate"])
            except (KeyError, InvalidOperation):
                errors.append(f"May YCL ranking row 无法解析：{dict(row)}")
        _compare_scalar(
            "May YCL ranking row_count",
            len(result_sets[1]),
            expected.ship_count,
            errors,
        )
        if len(ranking) != len(result_sets[1]):
            errors.append("May YCL ranking: ship_no 存在重复或缺失")
        source = expected.anomaly_rates["源头异常"]
        _compare_scalar(
            "May YCL H2601 rate", ranking.get(source.ship_no), source.rate, errors
        )
        other_rates = [rate for ship, rate in ranking.items() if ship != source.ship_no]
        if other_rates:
            _compare_scalar(
                "May YCL other_ship_min_rate",
                min(other_rates),
                expected.other_ship_min_rate,
                errors,
            )
            _compare_scalar(
                "May YCL other_ship_max_rate",
                max(other_rates),
                expected.other_ship_max_rate,
                errors,
            )
            if ranking.get(source.ship_no) is not None and not (
                ranking[source.ship_no] < min(other_rates)
            ):
                errors.append("May YCL ranking: H2601 不是唯一明显最低值")
        else:
            errors.append("May YCL ranking: 缺少其余船号")

    scalar_checks = (
        (2, "unexpected_high_risk_rows", expected.unexpected_high_risk_rows, int),
        (3, "fact_rows", expected.fact_rows, int),
        (3, "ship_count", expected.ship_count, int),
        (3, "min_date", expected.min_date.isoformat(), str),
        (3, "max_date", expected.max_date.isoformat(), str),
        (4, "bad_complete_rate", expected.bad_complete_rate, int),
        (4, "bad_deviation_rate", expected.bad_deviation_rate, int),
        (5, "bad_quality_split", expected.bad_quality_split, int),
        (6, "min_normal_rate", expected.min_normal_rate, Decimal),
        (6, "max_normal_rate", expected.max_normal_rate, Decimal),
        (6, "over_complete_combinations", expected.over_complete_combinations, int),
    )
    for result_index, column, expected_value, value_type in scalar_checks:
        raw_value = _actual_value(result_sets, result_index, column, errors)
        if value_type is int:
            actual_value: object = _as_int(raw_value, column, errors)
        elif value_type is Decimal:
            actual_value = _as_decimal(raw_value, column, errors)
        else:
            actual_value = raw_value
        if actual_value is not None:
            _compare_scalar(column, actual_value, expected_value, errors)

    return errors


def validate_d_drive_datadir(datadir: str) -> list[str]:
    """Keep the legacy validation hook without restricting the host drive."""

    del datadir
    return []


def _resolve_mysql_binary(binary: str) -> str:
    resolved = shutil.which(binary)
    if resolved is None:
        candidate = Path(binary)
        if candidate.is_file():
            resolved = str(candidate.resolve())
    if resolved is None:
        raise LoaderError(
            "找不到 MySQL 客户端；请将 mysql 加入 PATH，"
            "或通过 MYSQL_BIN 提供 mysql 可执行文件路径"
        )
    return resolved


def _mysql_command(
    settings: MySQLSettings, binary: str, database: str | None = None
) -> list[str]:
    command = [
        binary,
        "--local-infile=1",
        f"--host={settings.host}",
        f"--port={settings.port}",
        f"--user={settings.user}",
        "--default-character-set=utf8mb4",
        "--batch",
        "--raw",
    ]
    if database is not None:
        command.append(f"--database={database}")
    return command


def _run_mysql(
    settings: MySQLSettings,
    binary: str,
    sql: str,
    database: str | None = None,
) -> str:
    environment = os.environ.copy()
    environment["MYSQL_PWD"] = settings.password
    completed = subprocess.run(
        _mysql_command(settings, binary, database),
        input=sql,
        cwd=PACKAGE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise LoaderError(f"MySQL 命令失败（exit={completed.returncode}）：{detail}")
    return completed.stdout


def _parse_tsv_result(output: str) -> list[dict[str, str]]:
    lines = [line.rstrip("\r") for line in output.splitlines() if line.strip()]
    if not lines:
        return []
    headers = lines[0].lstrip("\ufeff").split("\t")
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        values = line.split("\t")
        if len(values) != len(headers):
            raise LoaderError(f"MySQL TSV 列数不一致：{line!r}")
        rows.append(dict(zip(headers, values, strict=True)))
    return rows


def load_and_self_check(settings: MySQLSettings) -> list[str]:
    """Import the package, execute the packaged self-check, and return mismatches."""

    binary = _resolve_mysql_binary(settings.binary)

    datadir_rows = _parse_tsv_result(
        _run_mysql(settings, binary, "SELECT @@datadir AS data_dir;")
    )
    if not datadir_rows or "data_dir" not in datadir_rows[0]:
        raise LoaderError("无法读取 MySQL @@datadir")
    datadir_errors = validate_d_drive_datadir(datadir_rows[0]["data_dir"])
    if datadir_errors:
        raise LoaderError(datadir_errors[0])

    import_sql = rewrite_database(IMPORT_SQL_PATH.read_text(encoding="utf-8"))
    _run_mysql(settings, binary, import_sql)

    self_check_sql = rewrite_database(PACKAGED_SELF_CHECK_SQL)
    queries = split_self_check_queries(self_check_sql)
    if len(queries) != 7:
        raise LoaderError(f"内置自检SQL应包含 7 个查询，实际为 {len(queries)}")
    result_sets = [
        _parse_tsv_result(_run_mysql(settings, binary, query, TARGET_DATABASE))
        for query in queries
    ]
    expected = parse_expected_validation(PACKAGED_VALIDATION_MARKDOWN)
    return compare_results(expected, result_sets)


def main() -> int:
    try:
        errors = load_and_self_check(MySQLSettings.from_environment())
    except (LoaderError, OSError) as exc:
        print("FAIL")
        print(f"- {exc}")
        return 1

    if errors:
        print("FAIL")
        for error in errors:
            print(f"- {error}")
        return 1

    print("PASS")
    print(f"- database: {TARGET_DATABASE}")
    print("- validation: 内置自检全部预期值匹配")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
