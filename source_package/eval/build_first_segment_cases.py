"""Rebuild the frozen first-segment matrix from read-only raw CSV truth."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Sequence

from agents.domain_config import load_domain_config
from eval.first_segment_oracle import CsvOracle, load_first_segment_cases


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class CaseSeed:
    question: str
    sql: str
    table: str
    selected_columns: tuple[str, ...]
    filters: dict[str, str]
    order_by: tuple[str, ...] = ()
    source_kind: str = "status"


STATUS = "dwr_mps_ttyear_wide"
PROCESS = "dwr_mps_ppdataprocess_wide"
SHIP = "dwr_mps_ppdatalast_wide"
STATUS_FILTERS = {"RECORDDATE": "2025-07-10", "TARTYPE": "物量态势"}
ANNUAL_FILTERS = {"RECORDDATE": "2025-07-10", "TARGETTYPE": "年度统计"}
MONTHLY_FILTERS = {"RECORDDATE": "2025-07-10", "TARGETTYPE": "每月情况"}


SECTIONS: tuple[tuple[str, tuple[CaseSeed, ...]], ...] = (
    (
        "SEC001_ENUM",
        (
            CaseSeed(
                "列出2025-07-10物量态势里的全部工序名称",
                "SELECT PROCESS FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' ORDER BY PROCESS ASC",
                STATUS,
                ("PROCESS",),
                STATUS_FILTERS,
                ("PROCESS ASC",),
            ),
            CaseSeed(
                "2025-07-10年度过程统计实际有哪些部门",
                "SELECT DISTINCT DEPT FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='年度统计' ORDER BY DEPT ASC",
                PROCESS,
                ("DEPT",),
                ANNUAL_FILTERS,
                ("DEPT ASC",),
                "process",
            ),
            CaseSeed(
                "把年度过程统计中出现过的工序去重列出来",
                "SELECT DISTINCT PROCESS FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='年度统计' ORDER BY PROCESS ASC",
                PROCESS,
                ("PROCESS",),
                ANNUAL_FILTERS,
                ("PROCESS ASC",),
                "process",
            ),
            CaseSeed(
                "给我年度统计里真实存在的部门和工序组合清单",
                "SELECT DEPT, PROCESS FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='年度统计' ORDER BY DEPT ASC, PROCESS ASC",
                PROCESS,
                ("DEPT", "PROCESS"),
                ANNUAL_FILTERS,
                ("DEPT ASC", "PROCESS ASC"),
                "process",
            ),
            CaseSeed(
                "按年总量从高到低列出7月10日各先行分段工序",
                "SELECT PROCESS, YEARNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' ORDER BY YEARNUM DESC",
                STATUS,
                ("PROCESS", "YEARNUM"),
                STATUS_FILTERS,
                ("YEARNUM DESC",),
            ),
        ),
    ),
    (
        "SEC002_STATUS_VOLUME",
        (
            CaseSeed(
                "2025-07-10切割工序的年总量是多少",
                "SELECT YEARNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='切割'",
                STATUS,
                ("YEARNUM",),
                {**STATUS_FILTERS, "PROCESS": "切割"},
            ),
            CaseSeed(
                "7月10日上胎工序的月计划数",
                "SELECT MONTHNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='上胎'",
                STATUS,
                ("MONTHNUM",),
                {**STATUS_FILTERS, "PROCESS": "上胎"},
            ),
            CaseSeed(
                "查2025年7月9日下胎的当日实际数",
                "SELECT ACTUALNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-09' AND TARTYPE='物量态势' AND PROCESS='下胎'",
                STATUS,
                ("ACTUALNUM",),
                {"RECORDDATE": "2025-07-09", "TARTYPE": "物量态势", "PROCESS": "下胎"},
            ),
            CaseSeed(
                "小组立在2025-07-10的年总量、月计划数和当日实际数各是多少",
                "SELECT YEARNUM, MONTHNUM, ACTUALNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='小组立'",
                STATUS,
                ("YEARNUM", "MONTHNUM", "ACTUALNUM"),
                {**STATUS_FILTERS, "PROCESS": "小组立"},
            ),
            CaseSeed(
                "帮我看下7月10号结构完当天做了多少",
                "SELECT ACTUALNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='结构完'",
                STATUS,
                ("ACTUALNUM",),
                {**STATUS_FILTERS, "PROCESS": "结构完"},
            ),
        ),
    ),
    (
        "SEC003_STATUS_FINISH",
        (
            CaseSeed(
                "2025-07-10切割工序完成率",
                "SELECT FINISHRATE FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='切割'",
                STATUS,
                ("FINISHRATE",),
                {**STATUS_FILTERS, "PROCESS": "切割"},
            ),
            CaseSeed(
                "上胎7月10日的当日实际数和完成率一起给我",
                "SELECT ACTUALNUM, FINISHRATE FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='上胎'",
                STATUS,
                ("ACTUALNUM", "FINISHRATE"),
                {**STATUS_FILTERS, "PROCESS": "上胎"},
            ),
            CaseSeed(
                "2025-07-09下胎完成情况是多少",
                "SELECT FINISHRATE FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-09' AND TARTYPE='物量态势' AND PROCESS='下胎'",
                STATUS,
                ("FINISHRATE",),
                {"RECORDDATE": "2025-07-09", "TARTYPE": "物量态势", "PROCESS": "下胎"},
            ),
            CaseSeed(
                "查完整性7月10日实际数及完成率",
                "SELECT ACTUALNUM, FINISHRATE FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='完整性'",
                STATUS,
                ("ACTUALNUM", "FINISHRATE"),
                {**STATUS_FILTERS, "PROCESS": "完整性"},
            ),
            CaseSeed(
                "进涂在7月10号完成得怎么样，只看完成率",
                "SELECT FINISHRATE FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='进涂'",
                STATUS,
                ("FINISHRATE",),
                {**STATUS_FILTERS, "PROCESS": "进涂"},
            ),
        ),
    ),
    (
        "SEC004_STATUS_ONTIME",
        (
            CaseSeed(
                "2025-07-10出涂工序的准时率",
                "SELECT ONTIMERATE FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='出涂'",
                STATUS,
                ("ONTIMERATE",),
                {**STATUS_FILTERS, "PROCESS": "出涂"},
            ),
            CaseSeed(
                "下胎7月10日按期率是多少",
                "SELECT ONTIMERATE FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='下胎'",
                STATUS,
                ("ONTIMERATE",),
                {**STATUS_FILTERS, "PROCESS": "下胎"},
            ),
            CaseSeed(
                "查2025-07-09切割工序的准时率",
                "SELECT ONTIMERATE FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-09' AND TARTYPE='物量态势' AND PROCESS='切割'",
                STATUS,
                ("ONTIMERATE",),
                {"RECORDDATE": "2025-07-09", "TARTYPE": "物量态势", "PROCESS": "切割"},
            ),
            CaseSeed(
                "完整性在7月10日的准时情况",
                "SELECT ONTIMERATE FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='完整性'",
                STATUS,
                ("ONTIMERATE",),
                {**STATUS_FILTERS, "PROCESS": "完整性"},
            ),
            CaseSeed(
                "7月10号进涂按时完成的比率",
                "SELECT ONTIMERATE FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='进涂'",
                STATUS,
                ("ONTIMERATE",),
                {**STATUS_FILTERS, "PROCESS": "进涂"},
            ),
        ),
    ),
    (
        "SEC005_STATUS_WARNING",
        (
            CaseSeed(
                "2025-07-10结构完的预警数和严重预警数",
                "SELECT WARNNUM, SERWARNNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='结构完'",
                STATUS,
                ("WARNNUM", "SERWARNNUM"),
                {**STATUS_FILTERS, "PROCESS": "结构完"},
            ),
            CaseSeed(
                "小组立7月10日两类预警字段的原始值",
                "SELECT SERWARNNUM, WARNNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='小组立'",
                STATUS,
                ("SERWARNNUM", "WARNNUM"),
                {**STATUS_FILTERS, "PROCESS": "小组立"},
            ),
            CaseSeed(
                "7月10日切割有多少条预警",
                "SELECT WARNNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='切割'",
                STATUS,
                ("WARNNUM",),
                {**STATUS_FILTERS, "PROCESS": "切割"},
            ),
            CaseSeed(
                "出涂在2025-07-10的严重预警数",
                "SELECT SERWARNNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='出涂'",
                STATUS,
                ("SERWARNNUM",),
                {**STATUS_FILTERS, "PROCESS": "出涂"},
            ),
            CaseSeed(
                "进涂7月10号的预警和严重预警原值是什么",
                "SELECT WARNNUM, SERWARNNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='进涂'",
                STATUS,
                ("WARNNUM", "SERWARNNUM"),
                {**STATUS_FILTERS, "PROCESS": "进涂"},
            ),
        ),
    ),
    (
        "SEC006_STATUS_OVERVIEW",
        (
            CaseSeed(
                "2025-07-10各工序完成率和准时率分布",
                "SELECT PROCESS, FINISHRATE, ONTIMERATE FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' ORDER BY PROCESS ASC",
                STATUS,
                ("PROCESS", "FINISHRATE", "ONTIMERATE"),
                STATUS_FILTERS,
                ("PROCESS ASC",),
            ),
            CaseSeed(
                "切割7月10日物量态势十字段总览",
                "SELECT YEARNUM, MONTHNUM, ACTUALNUM, FINISHRATE, ONTIMERATE, SERWARNNUM, WARNNUM, FINISHNUM, ONTIMENUM, PLANNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='切割'",
                STATUS,
                ("YEARNUM", "MONTHNUM", "ACTUALNUM", "FINISHRATE", "ONTIMERATE", "SERWARNNUM", "WARNNUM", "FINISHNUM", "ONTIMENUM", "PLANNUM"),
                {**STATUS_FILTERS, "PROCESS": "切割"},
            ),
            CaseSeed(
                "按预警数从高到低看7月10日各工序预警分布",
                "SELECT PROCESS, WARNNUM, SERWARNNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' ORDER BY WARNNUM DESC",
                STATUS,
                ("PROCESS", "WARNNUM", "SERWARNNUM"),
                STATUS_FILTERS,
                ("WARNNUM DESC",),
            ),
            CaseSeed(
                "给我进涂2025-07-10的完整物量态势",
                "SELECT YEARNUM, MONTHNUM, ACTUALNUM, FINISHRATE, ONTIMERATE, SERWARNNUM, WARNNUM, FINISHNUM, ONTIMENUM, PLANNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='进涂'",
                STATUS,
                ("YEARNUM", "MONTHNUM", "ACTUALNUM", "FINISHRATE", "ONTIMERATE", "SERWARNNUM", "WARNNUM", "FINISHNUM", "ONTIMENUM", "PLANNUM"),
                {**STATUS_FILTERS, "PROCESS": "进涂"},
            ),
            CaseSeed(
                "列出7月10日各工序的完成数、准时数和计划数",
                "SELECT PROCESS, FINISHNUM, ONTIMENUM, PLANNUM FROM dwr_mps_ttyear_wide WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' ORDER BY PROCESS ASC",
                STATUS,
                ("PROCESS", "FINISHNUM", "ONTIMENUM", "PLANNUM"),
                STATUS_FILTERS,
                ("PROCESS ASC",),
            ),
        ),
    ),
    (
        "SEC007_PROCESS_SCOPE",
        (
            CaseSeed(
                "列出年度统计中真实存在的部门工序适用组合",
                "SELECT DEPT, PROCESS FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='年度统计' ORDER BY DEPT ASC, PROCESS ASC",
                PROCESS,
                ("DEPT", "PROCESS"),
                ANNUAL_FILTERS,
                ("DEPT ASC", "PROCESS ASC"),
                "process",
            ),
            CaseSeed(
                "部门01切割工序年度三项记录",
                "SELECT ndjhs, JHYLJ, NDSJS FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='年度统计' AND DEPT='部门01' AND PROCESS='切割'",
                PROCESS,
                ("ndjhs", "JHYLJ", "NDSJS"),
                {**ANNUAL_FILTERS, "DEPT": "部门01", "PROCESS": "切割"},
                source_kind="process",
            ),
            CaseSeed(
                "核对部门02完整性的年度计划、累计和实际记录",
                "SELECT ndjhs, JHYLJ, NDSJS FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='年度统计' AND DEPT='部门02' AND PROCESS='完整性'",
                PROCESS,
                ("ndjhs", "JHYLJ", "NDSJS"),
                {**ANNUAL_FILTERS, "DEPT": "部门02", "PROCESS": "完整性"},
                source_kind="process",
            ),
            CaseSeed(
                "外协出涂这个组合的年度三项原始值",
                "SELECT ndjhs, JHYLJ, NDSJS FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='年度统计' AND DEPT='外协' AND PROCESS='出涂'",
                PROCESS,
                ("ndjhs", "JHYLJ", "NDSJS"),
                {**ANNUAL_FILTERS, "DEPT": "外协", "PROCESS": "出涂"},
                source_kind="process",
            ),
            CaseSeed(
                "验证外协单位01小组立是否真的有年度统计行，不要补数",
                "SELECT DEPT, PROCESS, ndjhs, JHYLJ, NDSJS FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='年度统计' AND DEPT='外协单位01' AND PROCESS='小组立'",
                PROCESS,
                ("DEPT", "PROCESS", "ndjhs", "JHYLJ", "NDSJS"),
                {**ANNUAL_FILTERS, "DEPT": "外协单位01", "PROCESS": "小组立"},
                source_kind="process",
            ),
        ),
    ),
    (
        "SEC008_PROCESS_ANNUAL",
        (
            CaseSeed(
                "部门01上胎的年度计划数",
                "SELECT ndjhs FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='年度统计' AND DEPT='部门01' AND PROCESS='上胎'",
                PROCESS,
                ("ndjhs",),
                {**ANNUAL_FILTERS, "DEPT": "部门01", "PROCESS": "上胎"},
                source_kind="process",
            ),
            CaseSeed(
                "部门01切割年计划累计数是多少",
                "SELECT JHYLJ FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='年度统计' AND DEPT='部门01' AND PROCESS='切割'",
                PROCESS,
                ("JHYLJ",),
                {**ANNUAL_FILTERS, "DEPT": "部门01", "PROCESS": "切割"},
                source_kind="process",
            ),
            CaseSeed(
                "部门03进涂的年度实际数",
                "SELECT NDSJS FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='年度统计' AND DEPT='部门03' AND PROCESS='进涂'",
                PROCESS,
                ("NDSJS",),
                {**ANNUAL_FILTERS, "DEPT": "部门03", "PROCESS": "进涂"},
                source_kind="process",
            ),
            CaseSeed(
                "查部门02下胎年度实际完成了多少",
                "SELECT NDSJS FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='年度统计' AND DEPT='部门02' AND PROCESS='下胎'",
                PROCESS,
                ("NDSJS",),
                {**ANNUAL_FILTERS, "DEPT": "部门02", "PROCESS": "下胎"},
                source_kind="process",
            ),
            CaseSeed(
                "外协结构完年度计划数给我看一下",
                "SELECT ndjhs FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='年度统计' AND DEPT='外协' AND PROCESS='结构完'",
                PROCESS,
                ("ndjhs",),
                {**ANNUAL_FILTERS, "DEPT": "外协", "PROCESS": "结构完"},
                source_kind="process",
            ),
        ),
    ),
    (
        "SEC009_PROCESS_MONTHLY",
        (
            CaseSeed(
                "部门01上胎7月计划和实际对比",
                "SELECT TRENDYEAR, TRENDMONTH, PLANNUM, ACTUALNUM FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='每月情况' AND DEPT='部门01' AND PROCESS='上胎' AND TRENDMONTH=7",
                PROCESS,
                ("TRENDYEAR", "TRENDMONTH", "PLANNUM", "ACTUALNUM"),
                {**MONTHLY_FILTERS, "DEPT": "部门01", "PROCESS": "上胎", "TRENDMONTH": "7"},
                source_kind="process",
            ),
            CaseSeed(
                "部门01切割每月计划数趋势",
                "SELECT TRENDYEAR, TRENDMONTH, PLANNUM FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='每月情况' AND DEPT='部门01' AND PROCESS='切割' ORDER BY TRENDMONTH ASC",
                PROCESS,
                ("TRENDYEAR", "TRENDMONTH", "PLANNUM"),
                {**MONTHLY_FILTERS, "DEPT": "部门01", "PROCESS": "切割"},
                ("TRENDMONTH ASC",),
                "process",
            ),
            CaseSeed(
                "部门03进涂逐月实际数走势",
                "SELECT TRENDYEAR, TRENDMONTH, ACTUALNUM FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='每月情况' AND DEPT='部门03' AND PROCESS='进涂' ORDER BY TRENDMONTH ASC",
                PROCESS,
                ("TRENDYEAR", "TRENDMONTH", "ACTUALNUM"),
                {**MONTHLY_FILTERS, "DEPT": "部门03", "PROCESS": "进涂"},
                ("TRENDMONTH ASC",),
                "process",
            ),
            CaseSeed(
                "部门01切割5到7月计划与实际分别是多少",
                "SELECT TRENDYEAR, TRENDMONTH, PLANNUM, ACTUALNUM FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='每月情况' AND DEPT='部门01' AND PROCESS='切割' AND TRENDMONTH BETWEEN 5 AND 7 ORDER BY TRENDMONTH ASC",
                PROCESS,
                ("TRENDYEAR", "TRENDMONTH", "PLANNUM", "ACTUALNUM"),
                {**MONTHLY_FILTERS, "DEPT": "部门01", "PROCESS": "切割", "TRENDMONTH": "5..7"},
                ("TRENDMONTH ASC",),
                "process",
            ),
            CaseSeed(
                "外协小组立7月份计划数和实际数",
                "SELECT TRENDYEAR, TRENDMONTH, PLANNUM, ACTUALNUM FROM dwr_mps_ppdataprocess_wide WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='每月情况' AND DEPT='外协' AND PROCESS='小组立' AND TRENDMONTH=7",
                PROCESS,
                ("TRENDYEAR", "TRENDMONTH", "PLANNUM", "ACTUALNUM"),
                {**MONTHLY_FILTERS, "DEPT": "外协", "PROCESS": "小组立", "TRENDMONTH": "7"},
                source_kind="process",
            ),
        ),
    ),
    (
        "SEC010_SHIP_STAGE",
        (
            CaseSeed(
                "先行分段表，H2705号船的切割实际值，2025-7-9",
                "SELECT QG_NUM FROM dwr_mps_ppdatalast_wide WHERE RECORDDATE='2025-07-09' AND SHIPNO='H2705' AND M_YJTYPE='实际'",
                SHIP,
                ("QG_NUM",),
                {"RECORDDATE": "2025-07-09", "SHIPNO": "H2705", "M_YJTYPE": "实际"},
                source_kind="ship",
            ),
            CaseSeed(
                "先行分段表，H2705号船的小组立实际值，2025-7-9",
                "SELECT XZL_NUM FROM dwr_mps_ppdatalast_wide WHERE RECORDDATE='2025-07-09' AND SHIPNO='H2705' AND M_YJTYPE='实际'",
                SHIP,
                ("XZL_NUM",),
                {"RECORDDATE": "2025-07-09", "SHIPNO": "H2705", "M_YJTYPE": "实际"},
                source_kind="ship",
            ),
            CaseSeed(
                "查H2717船7月9日先行分段切割实际值",
                "SELECT QG_NUM FROM dwr_mps_ppdatalast_wide WHERE RECORDDATE='2025-07-09' AND SHIPNO='H2717' AND M_YJTYPE='实际'",
                SHIP,
                ("QG_NUM",),
                {"RECORDDATE": "2025-07-09", "SHIPNO": "H2717", "M_YJTYPE": "实际"},
                source_kind="ship",
            ),
            CaseSeed(
                "H2717在2025-07-09的小组立实际完成值",
                "SELECT XZL_NUM FROM dwr_mps_ppdatalast_wide WHERE RECORDDATE='2025-07-09' AND SHIPNO='H2717' AND M_YJTYPE='实际'",
                SHIP,
                ("XZL_NUM",),
                {"RECORDDATE": "2025-07-09", "SHIPNO": "H2717", "M_YJTYPE": "实际"},
                source_kind="ship",
            ),
            CaseSeed(
                "帮我找H2647号船2025年7月9日的切割实际数",
                "SELECT QG_NUM FROM dwr_mps_ppdatalast_wide WHERE RECORDDATE='2025-07-09' AND SHIPNO='H2647' AND M_YJTYPE='实际'",
                SHIP,
                ("QG_NUM",),
                {"RECORDDATE": "2025-07-09", "SHIPNO": "H2647", "M_YJTYPE": "实际"},
                source_kind="ship",
            ),
        ),
    ),
)


def _source_names(kind: str) -> tuple[tuple[str, str], ...]:
    if kind == "status":
        return (
            ("preprocess_status.md", "field_contract"),
            ("dwr_mps_ttyear_wide.csv", "answer_data"),
        )
    if kind == "process":
        return (
            ("preprocess_process.md", "field_contract"),
            ("dwr_mps_ppdataprocess_wide.csv", "answer_data"),
        )
    if kind == "ship":
        return (
            ("dwr_mps_ppdatalast_wide.csv", "answer_data"),
        )
    raise ValueError(f"unknown source kind: {kind}")


def build_cases(raw_root: Path) -> list[dict[str, object]]:
    domain = load_domain_config("first_segment")
    oracle = CsvOracle(raw_root)
    output: list[dict[str, object]] = []
    try:
        for section_index, (intent, seeds) in enumerate(SECTIONS, start=1):
            if len(seeds) != 5:
                raise ValueError(f"SEC-{section_index:03d} must contain five seeds")
            for ordinal, seed in enumerate(seeds, start=1):
                case_id = f"FS-SEC{section_index:03d}-{ordinal:02d}"
                result = oracle.execute(seed.sql, domain)
                sources = []
                for name, role in _source_names(seed.source_kind):
                    path = raw_root / name
                    sources.append(
                        {
                            "file": name,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "role": role,
                        }
                    )
                output.append(
                    {
                        "case_id": case_id,
                        "section": f"SEC-{section_index:03d}",
                        "split": "calibration" if ordinal <= 2 else "holdout",
                        "question": seed.question,
                        "expected_intent": intent,
                        "answer_key": {
                            "table": seed.table,
                            "selected_columns": list(seed.selected_columns),
                            "filters": seed.filters,
                            "order_by": list(seed.order_by),
                            "expected_sql": seed.sql,
                            "expected_columns": list(result.columns),
                            "expected_rows": [list(row) for row in result.rows],
                        },
                        "sources": sources,
                    }
                )
    finally:
        oracle.close()
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "eval" / "cases" / "first_segment_50.jsonl",
    )
    args = parser.parse_args(argv)
    raw_root = args.raw_root.resolve(strict=True)
    cases = build_cases(raw_root)
    rendered = "".join(
        json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "\n"
        for case in cases
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    load_first_segment_cases(args.output, raw_root=raw_root)
    print(f"WROTE {len(cases)} grounded cases to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
