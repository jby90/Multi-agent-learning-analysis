from __future__ import annotations

from collections import Counter
from decimal import Decimal
from pathlib import Path

import sqlglot
import pytest

from agents.domain_config import load_domain_config
from agents.sandbox import DatabaseSettings, ReadOnlyExecutor, validate_and_rewrite
from agents.prompts.build_verification_prompt import (
    DICTIONARY_PATH,
    FULL_15_IDS,
    IMPORT_SQL_PATH,
    PROMPT_PATH,
    ROUTED_FEW_SHOT_IDS,
    ROUTER_OUTPUT_SCHEMA,
    build_prompt,
    build_prompt_catalog,
    extract_few_shot_examples,
    extract_schema_ddl,
)


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ROUTING = {
    "Q1": ("FS-01", "FS-02", "FS-03", "FS-05"),
    "Q2": ("FS-03", "FS-04", "FS-01", "FS-05"),
    "Q3": ("FS-05", "FS-06", "FS-01", "FS-03"),
    "Q4": ("FS-07", "FS-08", "FS-15", "FS-11"),
    "Q5": ("FS-09", "FS-10", "FS-13", "FS-14"),
    "Q6": ("FS-11", "FS-12", "FS-07", "FS-13"),
    "Q7": ("FS-13", "FS-14", "FS-11", "FS-12"),
    "OUT_OF_SCOPE_MINI_7": (
        "FS-02",
        "FS-03",
        "FS-06",
        "FS-07",
        "FS-09",
        "FS-11",
        "FS-14",
    ),
}


def test_checked_in_prompt_is_exactly_reproducible() -> None:
    expected = build_prompt(
        IMPORT_SQL_PATH.read_text(encoding="utf-8"),
        DICTIONARY_PATH.read_text(encoding="utf-8"),
    )

    assert PROMPT_PATH.read_text(encoding="utf-8") == expected


def test_production_package_reproduces_the_checked_in_prompt_exactly() -> None:
    catalog = build_prompt_catalog(
        domain_config=load_domain_config("production_progress")
    )

    assert catalog.render_generation("FULL_15") == PROMPT_PATH.read_text(
        encoding="utf-8"
    )
    assert catalog.router_output_schema == ROUTER_OUTPUT_SCHEMA
    assert catalog.text2sql_output_schema["properties"]["family"]["enum"] == [
        "Q1",
        "Q2",
        "Q3",
        "Q4",
        "Q5",
        "Q6",
        "Q7",
        "OUT_OF_SCOPE",
    ]


def test_prompt_has_fifteen_complete_select_examples() -> None:
    examples = extract_few_shot_examples(PROMPT_PATH.read_text(encoding="utf-8"))

    assert len(examples) == 15
    assert Counter(item.family for item in examples) == Counter(
        {
            "Q1": 2,
            "Q2": 2,
            "Q3": 2,
            "Q4": 3,
            "Q5": 2,
            "Q6": 2,
            "Q7": 2,
        }
    )
    assert all("..." not in item.sql and "同构" not in item.sql for item in examples)
    assert all(len(sqlglot.parse(item.sql, read="mysql")) == 1 for item in examples)


def test_prompt_catalog_has_stable_ids_and_exact_routing_matrix() -> None:
    catalog = build_prompt_catalog(
        IMPORT_SQL_PATH.read_text(encoding="utf-8"),
        DICTIONARY_PATH.read_text(encoding="utf-8"),
    )
    examples = extract_few_shot_examples(catalog.render_generation("FULL_15"))

    assert tuple(item.example_id for item in examples) == FULL_15_IDS
    assert FULL_15_IDS == tuple(f"FS-{index:02d}" for index in range(1, 16))
    assert ROUTED_FEW_SHOT_IDS == EXPECTED_ROUTING
    for profile, expected_ids in EXPECTED_ROUTING.items():
        assert catalog.ids_for(profile) == expected_ids
        rendered = extract_few_shot_examples(catalog.render_generation(profile))
        assert tuple(item.example_id for item in rendered) == expected_ids


def test_decision_18_adds_fs15_and_prioritizes_date_bounds_for_q4() -> None:
    catalog = build_prompt_catalog(
        IMPORT_SQL_PATH.read_text(encoding="utf-8"),
        DICTIONARY_PATH.read_text(encoding="utf-8"),
    )

    full_ids = catalog.ids_for("FULL_15")
    examples = {
        item.example_id: item
        for item in extract_few_shot_examples(
            catalog.render_generation("FULL_15")
        )
    }
    fs15 = examples["FS-15"]

    assert full_ids == tuple(f"FS-{index:02d}" for index in range(1, 16))
    assert fs15.family == "Q4"
    assert fs15.question == "H2601预处理3月到5月的完成率走势"
    assert fs15.sql == (
        "SELECT DATE_FORMAT(period_date,'%Y-%m') AS month_label, "
        "ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
        "FROM fact_production_progress WHERE ship_no='H2601' "
        "AND process_code='YCL' AND period_date>='2025-03-01' "
        "AND period_date<'2025-06-01' GROUP BY month_label "
        "ORDER BY month_label"
    )
    assert fs15.verified_result == (
        "3行：2025-03=0.9534, 2025-04=0.9061, 2025-05=0.6236"
    )
    assert ROUTED_FEW_SHOT_IDS["Q4"] == (
        "FS-07",
        "FS-08",
        "FS-15",
        "FS-11",
    )
    assert ROUTED_FEW_SHOT_IDS["OUT_OF_SCOPE_MINI_7"] == EXPECTED_ROUTING[
        "OUT_OF_SCOPE_MINI_7"
    ]


def test_out_of_scope_mini_set_covers_all_nine_output_contract_aliases() -> None:
    catalog = build_prompt_catalog(
        IMPORT_SQL_PATH.read_text(encoding="utf-8"),
        DICTIONARY_PATH.read_text(encoding="utf-8"),
    )
    examples = extract_few_shot_examples(
        catalog.render_generation("OUT_OF_SCOPE_MINI_7")
    )
    aliases = {
        alias.casefold()
        for example in examples
        for alias in sqlglot.parse_one(example.sql, read="mysql").named_selects
    }

    assert tuple(item.example_id for item in examples) == EXPECTED_ROUTING[
        "OUT_OF_SCOPE_MINI_7"
    ]
    assert aliases == {
        "plan_qty",
        "actual_qty",
        "complete_rate",
        "deviation_rate",
        "month_label",
        "ship_no",
        "process_code",
        "workshop_code",
        "high_risk_rows",
    }


def test_router_prompt_only_classifies_family() -> None:
    catalog = build_prompt_catalog(
        IMPORT_SQL_PATH.read_text(encoding="utf-8"),
        DICTIONARY_PATH.read_text(encoding="utf-8"),
    )

    assert ROUTER_OUTPUT_SCHEMA["required"] == ["family"]
    assert set(ROUTER_OUTPUT_SCHEMA["properties"]) == {"family"}
    assert "SELECT" not in catalog.router_prompt.upper()
    assert '"sql"' not in catalog.router_prompt.casefold()
    assert "Schema DDL" not in catalog.router_prompt
    assert "Q5" in catalog.router_prompt and "Q7" in catalog.router_prompt
    assert "单向" in catalog.router_prompt


def test_router_prompt_prioritizes_cross_process_dimension_over_rate_words() -> None:
    catalog = build_prompt_catalog(
        IMPORT_SQL_PATH.read_text(encoding="utf-8"),
        DICTIONARY_PATH.read_text(encoding="utf-8"),
    )

    assert "判别优先级" in catalog.router_prompt
    assert "硬规则（按顺序命中即停止）" in catalog.router_prompt
    assert "两个及以上工序" in catalog.router_prompt
    assert "即使只查询完成率也归Q6" in catalog.router_prompt
    assert "三道工序 → 必须Q6，不能Q3" in catalog.router_prompt
    assert (
        "按工序顺序查询H2601在2025-05三道工序完成率 → Q6"
        in catalog.router_prompt
    )


def test_router_prompt_separates_single_process_time_series_from_q6() -> None:
    catalog = build_prompt_catalog(
        IMPORT_SQL_PATH.read_text(encoding="utf-8"),
        DICTIONARY_PATH.read_text(encoding="utf-8"),
    )

    assert "同一船号、单一工序、两个及以上月份" in catalog.router_prompt
    assert "背景提到YCL、实际只查询ZZTP跨月 → Q4，不是Q6" in catalog.router_prompt
    assert (
        "H2601的ZZTP在2025-05与2025-06的完成率变化 → Q4"
        in catalog.router_prompt
    )
    assert "工序×月份对照表 → Q6" in catalog.router_prompt


def test_router_prompt_separates_cross_ship_ranking_and_scalar_metrics() -> None:
    catalog = build_prompt_catalog(
        IMPORT_SQL_PATH.read_text(encoding="utf-8"),
        DICTIONARY_PATH.read_text(encoding="utf-8"),
    )

    assert "2025-05各船YCL完成率排名 → Q5" in catalog.router_prompt
    assert "各船 → 必须Q5，不能Q3" in catalog.router_prompt
    assert "责任单元 → 必须Q7，不能Q3" in catalog.router_prompt
    assert "Q1只查询计划量或实际量" in catalog.router_prompt
    assert "Q3只查询完成率或偏差率" in catalog.router_prompt
    assert '输出：{"family":"Q2"}' in catalog.router_prompt


def test_all_generation_profiles_share_the_complete_fixed_prefix() -> None:
    catalog = build_prompt_catalog(
        IMPORT_SQL_PATH.read_text(encoding="utf-8"),
        DICTIONARY_PATH.read_text(encoding="utf-8"),
    )

    assert "JSON Schema" in catalog.fixed_prefix
    assert "Schema DDL" in catalog.fixed_prefix
    assert "字段字典关键行" in catalog.fixed_prefix
    assert "Q1：" in catalog.fixed_prefix and "Q7：" in catalog.fixed_prefix
    for profile in (*EXPECTED_ROUTING, "FULL_15"):
        assert catalog.render_generation(profile).startswith(catalog.fixed_prefix)


def test_prompt_schema_is_mechanically_extracted_without_load_statements() -> None:
    ddl = extract_schema_ddl(IMPORT_SQL_PATH.read_text(encoding="utf-8"))

    assert ddl.count("CREATE TABLE") == 5
    assert "fact_production_progress" in ddl
    assert "LOAD DATA" not in ddl
    assert ddl in PROMPT_PATH.read_text(encoding="utf-8")


def test_prompt_contains_metric_rule_and_output_alias_contracts() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "计划量=plan_qty，实际量=actual_qty，完成率=complete_rate" in prompt
    assert (
        "月度/区间聚合的完成率一律用 "
        "ROUND(SUM(actual_qty)/SUM(plan_qty),4)" in prompt
    )
    assert (
        "偏差率一律用 "
        "ROUND((SUM(actual_qty)-SUM(plan_qty))/SUM(plan_qty),4)" in prompt
    )
    assert "禁止对日级rate字段取AVG作为聚合率值" in prompt
    assert "AVG(complete_rate)" not in prompt
    assert "AVG(deviation_rate)" not in prompt
    family_rule = (
        "5. 族边界优先级（2026-07-14裁决回写）："
        "涉及责任单元/车间的下钻、分组一律归Q7；"
        "Q4仅用于单工序月度序列，同时对照两种及以上工序时"
        "（即使含月份、区间或时序）归Q6。"
    )
    sort_rule = (
        "6. 排序契约（2026-07-14裁决回写）："
        "Q7完成率对比/下钻按 complete_rate ASC（异常最重的排前）；"
        "Q7高风险计数按 high_risk_rows DESC；"
        "Q4按 month_label ASC；Q5按指标列 ASC。"
    )
    out_of_scope_rule = (
        "7. 问题不属于Q1-Q7任何模式族时，"
        "family填OUT_OF_SCOPE，sql填null。"
    )
    assert family_rule in prompt
    assert sort_rule in prompt
    assert out_of_scope_rule in prompt
    assert prompt.index(family_rule) < prompt.index(sort_rule) < prompt.index(
        out_of_scope_rule
    )
    assert "6. 问题不属于Q1-Q7" not in prompt
    assert "Q1单值" in prompt
    assert "Q7" in prompt and "high_risk_rows" in prompt
    assert "OUT_OF_SCOPE" in prompt


@pytest.mark.live
def test_all_fifteen_few_shots_pass_sandbox_and_match_live_database() -> None:
    examples = extract_few_shot_examples(PROMPT_PATH.read_text(encoding="utf-8"))
    executor = ReadOnlyExecutor(DatabaseSettings.from_environment())
    results = []
    for example in examples:
        decision = validate_and_rewrite(example.sql)
        assert decision.allowed, (example.family, decision.rule_id, decision.reason)
        assert decision.executed_sql is not None
        results.append(executor.execute(decision.executed_sql).rows)

    assert results[0] == ({"plan_qty": Decimal("1855.06")},)
    assert results[1] == ({"actual_qty": Decimal("1299.69")},)
    assert results[2] == (
        {"plan_qty": Decimal("1855.06"), "actual_qty": Decimal("1156.87")},
    )
    assert results[3] == (
        {"plan_qty": Decimal("1336.12"), "actual_qty": Decimal("1008.15")},
    )
    assert results[4] == ({"complete_rate": Decimal("0.6236")},)
    assert results[5] == ({"deviation_rate": Decimal("-0.1499")},)
    assert len(results[6]) == 6 and results[6][3]["complete_rate"] == Decimal("0.6236")
    assert len(results[7]) == 6
    assert results[8][0] == {"ship_no": "H2601", "complete_rate": Decimal("0.6236")}
    assert results[9][0] == {"ship_no": "H2601", "complete_rate": Decimal("0.7545")}
    q6_chain = {
        (row["process_code"], row["month_label"]): row["complete_rate"]
        for row in results[10]
    }
    assert q6_chain[("YCL", "2025-05")] == Decimal("0.6236")
    assert q6_chain[("ZZTP", "2025-06")] == Decimal("0.7545")
    assert q6_chain[("AZTP", "2025-07")] == Decimal("0.8501")
    assert len(results[11]) == 3
    assert {row["process_code"]: row["complete_rate"] for row in results[11]}[
        "AZTP"
    ] == Decimal("0.8501")
    assert results[12] == (
        {"workshop_code": "WSA", "complete_rate": Decimal("0.9044")},
        {"workshop_code": "WSB", "complete_rate": Decimal("0.9045")},
    )
    assert results[13] == (
        {"workshop_code": "WSA", "high_risk_rows": 31},
        {"workshop_code": "WSB", "high_risk_rows": 31},
    )
    assert results[14] == (
        {"month_label": "2025-03", "complete_rate": Decimal("0.9534")},
        {"month_label": "2025-04", "complete_rate": Decimal("0.9061")},
        {"month_label": "2025-05", "complete_rate": Decimal("0.6236")},
    )
