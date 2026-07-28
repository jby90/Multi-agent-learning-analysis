from __future__ import annotations

from decimal import Decimal
import importlib
import inspect
import json
from pathlib import Path
import re
import shutil
from typing import Any

import pytest

from agents.domain_config import load_domain_config
from agents.review_agent import evaluate_hard_rules
from agents.sandbox import DatabaseSettings, ReadOnlyExecutor, validate_and_rewrite
from agents.verification_agent import FAMILY_COLUMN_SETS, _output_aliases
from orchestrator.bus import MessageBus
from orchestrator.llm import LLMResult, TokenUsage, call_llm


EXPECTED_PARAMETERS = {
    "ship": "H2601",
    "month": "2025-05",
    "month_down": "2025-06",
    "month2": "2025-07",
    "process": "YCL",
    "process_down": "ZZTP",
    "process_last": "AZTP",
}
EXPECTED_TEMPLATES = {
    "T-01": ("计划量与实际量口径", "basic", "quiz_set", "Q2"),
    "T-02": ("完成率计算", "basic", "quiz_set", "Q3"),
    "T-03": ("三道工序与传导关系", "basic", "practice_guide", "Q6"),
    "T-04": ("月度聚合方法", "basic", "quiz_set", "Q4"),
    "T-05": ("异常识别标准", "basic", "quiz_set", "Q5"),
    "T-06": ("责任单元定位", "basic", "quiz_set", "Q7"),
    "T-07": ("传导时滞分析", "basic", "practice_guide", "Q4"),
    "T-08": ("跨工序归因方法", "applied", "practice_guide", "Q6"),
    "T-09": ("跨工序归因方法", "advanced", "quiz_set", "Q6"),
    "T-10": ("偏差率与风险等级", "basic", "quiz_set", "Q3"),
    "T-02-A": ("完成率计算", "applied", "quiz_set", "Q6"),
    "T-02-B": ("完成率计算", "advanced", "quiz_set", "Q7"),
    "T-01-A": ("计划量与实际量口径", "applied", "quiz_set", "Q5"),
    "T-01-B": ("计划量与实际量口径", "advanced", "quiz_set", "Q7"),
    "T-03-A": ("三道工序与传导关系", "applied", "practice_guide", "Q6"),
    "T-03-B": ("三道工序与传导关系", "advanced", "quiz_set", "Q6"),
    "T-10-A": ("偏差率与风险等级", "applied", "quiz_set", "Q7"),
    "T-10-B": ("偏差率与风险等级", "advanced", "quiz_set", "Q4"),
    "T-04-A": ("月度聚合方法", "applied", "quiz_set", "Q6"),
    "T-04-B": ("月度聚合方法", "advanced", "quiz_set", "Q6"),
    "T-05-A": ("异常识别标准", "applied", "quiz_set", "Q4"),
    "T-05-B": ("异常识别标准", "advanced", "quiz_set", "Q6"),
    "T-07-A": ("传导时滞分析", "applied", "practice_guide", "Q6"),
    "T-07-B": ("传导时滞分析", "advanced", "quiz_set", "Q6"),
    "T-06-A": ("责任单元定位", "applied", "quiz_set", "Q7"),
    "T-06-B": ("责任单元定位", "advanced", "quiz_set", "Q7"),
    "T-08-DECAY": ("异常衰减规律", "basic", "quiz_set", "Q6"),
    "T-08-DECAY-A": ("异常衰减规律", "applied", "practice_guide", "Q6"),
    "T-08-DECAY-B": ("异常衰减规律", "advanced", "quiz_set", "Q6"),
    "T-08-ATTR": ("跨工序归因方法", "basic", "practice_guide", "Q6"),
}
DOMAIN_PACKAGE_IDS = ("production_progress", "first_segment")
LEARNER_GUIDE_FIELDS = (
    "question",
    "contextualized_stem",
    "guide_intro",
    "guide_steps",
    "completion_criteria",
    "guide_md",
)
ROOT = Path(__file__).resolve().parents[1]
TASK_PROMPT_PATH = ROOT / "agents" / "prompts" / "task_contextualize.md"
EXPECTED_TASK_PROMPT = (
    '你是船厂实训导师。把标准任务改写为贴合学员岗位的情境化题面，并写一句实训引导语。'
    '只输出JSON{"contextualized_stem":"...","guide_intro":"..."}。规则：'
    '1.不得改变任务的查询目标、参数与考核点；2.{ship}{month}{process}等参数值原样保留；'
    '日期类参数（如2025-05）必须保持YYYY-MM原格式出现在题面中，不得改写为自然语言日期；'
    '3.语言风格按画像lecture_style；4.引导语不得直接给出答案或SQL。'
    '[标准题面][画像JSON][当前学情摘要]\n'
)


class SpyLLM:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> LLMResult:
        self.calls.append(kwargs)
        return LLMResult(
            data=dict(self.data),
            model="qwen3-235b-a22b",
            latency_ms=53,
            token_usage=TokenUsage(90, 30, 120),
            attempts=1,
        )


def _task_module() -> Any:
    try:
        return importlib.import_module("agents.task_agent")
    except ModuleNotFoundError:
        pytest.fail("agents.task_agent is not implemented")


def test_assessment_branch_normalizes_practice_anchor_to_quiz_contract() -> None:
    message = _task_module().TaskAgent("trace-assessment-branch").generate_assessment(
        "T-03"
    )
    content = message["payload"]["content"]

    assert message["payload"]["type"] == "quiz_set"
    assert content["event"] == "assessment_ready"
    assert content["resource_kind"] == "graded_assessment"
    assert content["questions"] == [
        {
            "id": "T-03",
            "prompt": content["question"],
            "difficulty": "basic",
        }
    ]
    assert "guide_md" not in content
    assert message["evidence"][0]["kind"] == "quiz_answer_key"


def _planner_profile() -> dict[str, Any]:
    return {
        "profile_id": "planner_new",
        "title": "新入职生产计划员",
        "lecture_style": "重讲工序与口径、少讲SQL",
    }


def test_task_prompt_matches_instruction_card_verbatim() -> None:
    assert TASK_PROMPT_PATH.read_text(encoding="utf-8") == EXPECTED_TASK_PROMPT


def _normalized_rows(rows: Any) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for row in rows:
        normalized.append(
            {
                key: format(value, "f") if isinstance(value, Decimal) else str(value)
                for key, value in row.items()
            }
        )
    return normalized


def test_catalog_is_exactly_the_approved_thirty_tasks_and_five_mappings() -> None:
    catalog = _task_module().load_task_catalog()

    assert catalog.demo_parameters == EXPECTED_PARAMETERS
    assert list(catalog.templates) == list(EXPECTED_TEMPLATES)
    assert list(catalog.counter_evidence) == [
        "M-01",
        "M-02",
        "M-03",
        "M-04",
        "M-05",
    ]
    assert {
        template_id: (
            entry.knowledge_point,
            entry.difficulty,
            entry.payload_type,
            entry.family,
        )
        for template_id, entry in catalog.templates.items()
    } == EXPECTED_TEMPLATES
    assert {
        template_id
        for template_id, entry in catalog.templates.items()
        if entry.knowledge_point == "偏差率与风险等级"
    } == {"T-10", "T-10-A", "T-10-B"}
    assert {
        template_id
        for template_id, entry in catalog.templates.items()
        if entry.knowledge_point == "月度聚合方法"
    } == {"T-04", "T-04-A", "T-04-B"}
    assert {
        template_id
        for template_id, entry in catalog.templates.items()
        if entry.knowledge_point == "异常识别标准"
    } == {"T-05", "T-05-A", "T-05-B"}
    assert {
        template_id
        for template_id, entry in catalog.templates.items()
        if entry.knowledge_point == "传导时滞分析"
    } == {"T-07", "T-07-A", "T-07-B"}
    assert {
        template_id
        for template_id, entry in catalog.templates.items()
        if entry.knowledge_point == "异常衰减规律"
    } == {"T-08-DECAY", "T-08-DECAY-A", "T-08-DECAY-B"}
    assert {
        template_id
        for template_id, entry in catalog.templates.items()
        if entry.knowledge_point == "责任单元定位"
    } == {"T-06", "T-06-A", "T-06-B"}
    assert {
        template_id
        for template_id, entry in catalog.templates.items()
        if entry.knowledge_point == "跨工序归因方法"
    } == {"T-08-ATTR", "T-08", "T-09"}


def _copied_domain_package(tmp_path: Path) -> Path:
    source = ROOT / "config" / "domains" / "production_progress"
    destination = tmp_path / "copied_domain"
    shutil.copytree(source, destination)
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    manifest["domain_id"] = "copied_domain"
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return destination


def test_catalog_order_and_routes_come_from_the_domain_manifest(tmp_path: Path) -> None:
    package = _copied_domain_package(tmp_path)
    manifest_path = package / "task_manifest.json"
    templates_path = package / "task_templates.json"
    task_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    task_root = json.loads(templates_path.read_text(encoding="utf-8"))
    task_manifest["template_ids"][:2] = reversed(task_manifest["template_ids"][:2])
    task_root["templates"][:2] = reversed(task_root["templates"][:2])
    manifest_path.write_text(
        json.dumps(task_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    templates_path.write_text(
        json.dumps(task_root, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    catalog = _task_module().load_task_catalog(
        domain_config=load_domain_config(package)
    )

    assert list(catalog.templates)[:2] == ["T-02", "T-01"]
    assert catalog.template_ids[:2] == ("T-02", "T-01")


def test_catalog_rejects_a_manifest_route_to_an_unknown_template(tmp_path: Path) -> None:
    package = _copied_domain_package(tmp_path)
    path = package / "task_manifest.json"
    task_manifest = json.loads(path.read_text(encoding="utf-8"))
    task_manifest["diagnostic_template_ids"]["完成率计算"]["basic"] = "MISSING"
    path.write_text(
        json.dumps(task_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown template"):
        _task_module().load_task_catalog(domain_config=load_domain_config(package))


@pytest.mark.parametrize("missing_field", ("guide_steps", "completion_criteria"))
def test_practice_guide_schema_requires_both_structured_fields(
    tmp_path: Path,
    missing_field: str,
) -> None:
    package = _copied_domain_package(tmp_path)
    path = package / "task_templates.json"
    task_root = json.loads(path.read_text(encoding="utf-8"))
    practice_guide = next(
        template
        for template in task_root["templates"]
        if template["payload_type"] == "practice_guide"
    )
    practice_guide["guide_steps"] = [
        "确认实操范围",
        "按业务口径完成查询",
        "根据结果完成自检",
    ]
    practice_guide["completion_criteria"] = ["已按要求留下可复核的实操结论"]
    practice_guide.pop(missing_field)
    path.write_text(
        json.dumps(task_root, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=missing_field):
        _task_module().load_task_catalog(domain_config=load_domain_config(package))


@pytest.mark.parametrize("extra_field", ("guide_steps", "completion_criteria"))
def test_quiz_set_schema_rejects_practice_guide_only_fields(
    tmp_path: Path,
    extra_field: str,
) -> None:
    package = _copied_domain_package(tmp_path)
    path = package / "task_templates.json"
    task_root = json.loads(path.read_text(encoding="utf-8"))
    quiz = next(
        template
        for template in task_root["templates"]
        if template["payload_type"] == "quiz_set"
    )
    quiz[extra_field] = ["不应进入练习题模板"]
    path.write_text(
        json.dumps(task_root, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=rf"quiz_set.*{extra_field}"):
        _task_module().load_task_catalog(domain_config=load_domain_config(package))


def test_practice_guide_fields_use_the_existing_safe_template_renderer(
    tmp_path: Path,
) -> None:
    package = _copied_domain_package(tmp_path)
    path = package / "task_templates.json"
    task_root = json.loads(path.read_text(encoding="utf-8"))
    practice_guide = next(
        template
        for template in task_root["templates"]
        if template["payload_type"] == "practice_guide"
    )
    practice_guide["guide_steps"] = [
        "确认{ship}的实操范围",
        "按业务口径完成查询",
        "根据{unknown_slot}完成自检",
    ]
    practice_guide["completion_criteria"] = ["已形成可复核的实操结论"]
    path.write_text(
        json.dumps(task_root, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="unsupported question template slot: unknown_slot",
    ):
        _task_module().load_task_catalog(domain_config=load_domain_config(package))


def test_completion_rate_tier_answer_keys_match_the_approved_real_rows() -> None:
    catalog = _task_module().load_task_catalog()

    applied = catalog.templates["T-02-A"]
    assert applied.question_template == "查询{ship}{month}三道工序的完成率"
    assert applied.expected_rows == (
        {"process_code": "AZTP", "complete_rate": "0.9149"},
        {"process_code": "YCL", "complete_rate": "0.6236"},
        {"process_code": "ZZTP", "complete_rate": "1.0249"},
    )
    assert applied.expected_points == (
        "YCL=0.6236，ZZTP=1.0249，AZTP=0.9149；YCL完成率最低",
    )

    advanced = catalog.templates["T-02-B"]
    assert advanced.question_template == (
        "{ship}{month}{process}完成率异常，按责任单元下钻定位短板"
    )
    assert advanced.expected_rows == (
        {"workshop_code": "WSB", "complete_rate": "0.6218"},
        {"workshop_code": "WSA", "complete_rate": "0.6253"},
    )
    assert advanced.expected_points == (
        "WSB=0.6218，WSA=0.6253；WSB完成率最低",
    )


def test_completion_rate_tier_sqls_fit_existing_family_contracts() -> None:
    catalog = _task_module().load_task_catalog()

    for template_id in ("T-02-A", "T-02-B"):
        entry = catalog.templates[template_id]
        assert _output_aliases(entry.standard_sql) in FAMILY_COLUMN_SETS[entry.family]


def test_plan_actual_tier_answer_keys_match_the_real_database_rows() -> None:
    catalog = _task_module().load_task_catalog()

    applied = catalog.templates["T-01-A"]
    assert applied.question_template == (
        "查询{month}各船{process}完成率，按船号比较计划兑现情况"
    )
    assert applied.expected_rows == (
        {"ship_no": "H2601", "complete_rate": "0.6236"},
        {"ship_no": "H2604", "complete_rate": "0.8924"},
        {"ship_no": "H2605", "complete_rate": "0.9472"},
        {"ship_no": "H2606", "complete_rate": "0.9727"},
        {"ship_no": "H2602", "complete_rate": "0.9776"},
        {"ship_no": "H2603", "complete_rate": "0.9779"},
    )
    assert applied.expected_points == (
        "H2601完成率最低（0.6236）；各船仅在同月、同工序、同计量口径下可比较计划兑现情况",
    )

    advanced = catalog.templates["T-01-B"]
    assert advanced.question_template == (
        "核验{ship}{month}{process}各责任单元完成率差异，并判断能否仅凭差异直接归因"
    )
    assert advanced.expected_rows == (
        {"workshop_code": "WSB", "complete_rate": "0.6218"},
        {"workshop_code": "WSA", "complete_rate": "0.6253"},
    )
    assert advanced.expected_points == (
        "WSB计划兑现率略低于WSA；仅凭计划-实际差异不能直接归因于产能、物资或责任",
    )


def test_plan_actual_tier_sqls_fit_existing_family_contracts() -> None:
    catalog = _task_module().load_task_catalog()

    for template_id in ("T-01-A", "T-01-B"):
        entry = catalog.templates[template_id]
        assert _output_aliases(entry.standard_sql) in FAMILY_COLUMN_SETS[entry.family]
        assert "period_date" in entry.standard_sql
        assert "batch_code" not in entry.standard_sql


def test_process_propagation_tier_answer_keys_match_the_real_database_rows() -> None:
    catalog = _task_module().load_task_catalog()
    advanced_rows = (
        {"process_code": "AZTP", "month_label": "2025-04", "complete_rate": "0.9522"},
        {"process_code": "AZTP", "month_label": "2025-05", "complete_rate": "0.9149"},
        {"process_code": "AZTP", "month_label": "2025-06", "complete_rate": "0.9901"},
        {"process_code": "AZTP", "month_label": "2025-07", "complete_rate": "0.8501"},
        {"process_code": "YCL", "month_label": "2025-04", "complete_rate": "0.9061"},
        {"process_code": "YCL", "month_label": "2025-05", "complete_rate": "0.6236"},
        {"process_code": "YCL", "month_label": "2025-06", "complete_rate": "1.0067"},
        {"process_code": "YCL", "month_label": "2025-07", "complete_rate": "0.9698"},
        {"process_code": "ZZTP", "month_label": "2025-04", "complete_rate": "0.9326"},
        {"process_code": "ZZTP", "month_label": "2025-05", "complete_rate": "1.0249"},
        {"process_code": "ZZTP", "month_label": "2025-06", "complete_rate": "0.7545"},
        {"process_code": "ZZTP", "month_label": "2025-07", "complete_rate": "0.9981"},
    )

    applied = catalog.templates["T-03-A"]
    assert applied.question_template == (
        "查询{ship}{month}至{month2}三道工序×月份完成率，筛查疑似传导"
    )
    assert applied.expected_rows == tuple(
        row for row in advanced_rows if row["month_label"] != "2025-04"
    )
    assert applied.expected_points == (
        "YCL 2025-05=0.6236、ZZTP 2025-06=0.7545、AZTP 2025-07=0.8501呈上游领先的时间对应；仅支持疑似传导，不预设固定时滞，需结合齐套与交接证据核验",
    )

    advanced = catalog.templates["T-03-B"]
    assert advanced.question_template == (
        "从{ship}{month2}{process_last}异常出发，回看2025-04至{month2}三道工序序列，定位候选源头并列出核查项"
    )
    assert advanced.expected_rows == advanced_rows
    assert advanced.expected_points == (
        "候选源头为YCL 2025-05（0.6236）；后续ZZTP 2025-06=0.7545、AZTP 2025-07=0.8501呈时序对应；当前仅支持疑似传导，需排查共同原因、齐套/交接证据及下游本地异常，强度不预设衰减",
    )


def test_process_propagation_tier_sqls_fit_existing_q6_contract() -> None:
    catalog = _task_module().load_task_catalog()

    for template_id in ("T-03-A", "T-03-B"):
        entry = catalog.templates[template_id]
        assert entry.family == "Q6"
        assert _output_aliases(entry.standard_sql) in FAMILY_COLUMN_SETS[entry.family]
        assert "period_date" in entry.standard_sql
        assert "batch_code" not in entry.standard_sql


def test_deviation_risk_tier_answer_keys_match_the_real_database_rows() -> None:
    catalog = _task_module().load_task_catalog()

    basic = catalog.templates["T-10"]
    assert basic.question_template == (
        "计算{ship}{month2}{process_last}的月偏差率，并判断欠产或超产"
    )
    assert basic.expected_rows == ({"deviation_rate": "-0.1499"},)
    assert basic.expected_points == ("偏差率-0.1499，负值表示欠产约14.99%",)

    applied = catalog.templates["T-10-A"]
    assert applied.question_template == (
        "按项目高风险偏差阈值（deviation_rate < -0.15）筛选{month}{process}记录，"
        "并比较各责任单元的风险分布"
    )
    assert applied.expected_rows == (
        {"workshop_code": "WSA", "high_risk_rows": "31"},
        {"workshop_code": "WSB", "high_risk_rows": "31"},
    )
    assert applied.expected_points == (
        "WSA与WSB各31条高风险记录；风险等级用于初筛，数量分布不能直接替代偏差排序或根因判断",
    )

    advanced = catalog.templates["T-10-B"]
    assert advanced.question_template == (
        "回看{ship}2025-03至{month2}{process}月完成率序列，判断多期偏差趋势并列出风险复核项"
    )
    assert advanced.expected_rows == (
        {"month_label": "2025-03", "complete_rate": "0.9534"},
        {"month_label": "2025-04", "complete_rate": "0.9061"},
        {"month_label": "2025-05", "complete_rate": "0.6236"},
        {"month_label": "2025-06", "complete_rate": "1.0067"},
        {"month_label": "2025-07", "complete_rate": "0.9698"},
    )
    assert advanced.expected_points == (
        "2025-03至07完成率0.9534→0.9061→0.6236→1.0067→0.9698，对应偏差率-0.0466→-0.0939→-0.3764→0.0067→-0.0302；5月恶化、6月恢复、7月轻微回落，需结合延误、节点/浮时与齐套复核，不自行改写风险标签",
    )


def test_deviation_risk_tier_sqls_fit_existing_contracts_and_period_date() -> None:
    catalog = _task_module().load_task_catalog()

    for template_id in ("T-10", "T-10-A", "T-10-B"):
        entry = catalog.templates[template_id]
        assert _output_aliases(entry.standard_sql) in FAMILY_COLUMN_SETS[entry.family]
        assert "period_date" in entry.standard_sql
        assert "batch_code" not in entry.standard_sql


def test_monthly_aggregation_tier_answer_keys_match_the_real_database_rows() -> None:
    catalog = _task_module().load_task_catalog()
    advanced_rows = (
        {"process_code": "AZTP", "month_label": "2025-04", "complete_rate": "0.9522"},
        {"process_code": "AZTP", "month_label": "2025-05", "complete_rate": "0.9149"},
        {"process_code": "AZTP", "month_label": "2025-06", "complete_rate": "0.9901"},
        {"process_code": "AZTP", "month_label": "2025-07", "complete_rate": "0.8501"},
        {"process_code": "YCL", "month_label": "2025-04", "complete_rate": "0.9061"},
        {"process_code": "YCL", "month_label": "2025-05", "complete_rate": "0.6236"},
        {"process_code": "YCL", "month_label": "2025-06", "complete_rate": "1.0067"},
        {"process_code": "YCL", "month_label": "2025-07", "complete_rate": "0.9698"},
        {"process_code": "ZZTP", "month_label": "2025-04", "complete_rate": "0.9326"},
        {"process_code": "ZZTP", "month_label": "2025-05", "complete_rate": "1.0249"},
        {"process_code": "ZZTP", "month_label": "2025-06", "complete_rate": "0.7545"},
        {"process_code": "ZZTP", "month_label": "2025-07", "complete_rate": "0.9981"},
    )

    applied = catalog.templates["T-04-A"]
    assert applied.question_template == (
        "查询{ship}{month}至{month2}三道工序×月份完成率，并核对比率聚合方法"
    )
    assert applied.expected_rows == tuple(
        row for row in advanced_rows if row["month_label"] != "2025-04"
    )
    assert applied.expected_points == (
        "形成9行工序×月份结果；每个完成率均先按工序、月份汇总actual_qty与plan_qty再相除，不对明细完成率求算术平均",
    )

    advanced = catalog.templates["T-04-B"]
    assert advanced.question_template == (
        "核验{ship}2025-04至{month2}三道工序×月份完成率表的聚合口径，"
        "并判断能否把各工序原始数量直接上卷为整船总体进度"
    )
    assert advanced.expected_rows == advanced_rows
    assert advanced.expected_points == (
        "12行结果按工序×月份同口径聚合，期间使用period_date半开区间，比率先汇总分子分母再相除；"
        "跨工序原始数量需先核量纲与重复计量，整船总体进度应采用项目规定的标准工时、挣值或中间产品权重，"
        "当前结果不能直接上卷",
    )


def test_monthly_aggregation_templates_render_complete_iso_month_boundaries() -> None:
    module = _task_module()
    catalog = module.load_task_catalog()

    assert catalog.templates["T-04"].question_template == (
        "查询{ship}{process}在2025-03至2025-05期间的月完成率走势"
    )
    expected_boundaries = {
        "basic": ("2025-03", "2025-05"),
        "applied": ("2025-05", "2025-07"),
        "advanced": ("2025-04", "2025-07"),
    }
    agent = module.TaskAgent("trace-monthly-aggregation-month-boundaries")

    for difficulty, boundaries in expected_boundaries.items():
        message = agent.generate("T-04", diagnostic_difficulty=difficulty)
        standard_stem = message["payload"]["content"]["standard_stem"]
        assert all(boundary in standard_stem for boundary in boundaries)


def test_monthly_aggregation_basic_prior_contextualization_passes_unchanged_r04() -> None:
    module = _task_module()
    llm = SpyLLM(
        {
            "contextualized_stem": (
                "作为新入职的生产计划员，你需要分析H2601YCL在2025-03至2025-05期间的"
                "月度完成率走势，请使用SQL提取相关数据。"
            ),
            "guide_intro": "先按月提取完成率，再观察走势。",
        }
    )
    message = module.TaskAgent(
        "trace-monthly-aggregation-r04-regression",
        llm_call=llm,
    ).generate(
        "T-04",
        diagnostic_difficulty="basic",
        student_profile=_planner_profile(),
        learning_report_summary="当前需巩固月度聚合方法。",
    )

    assert message["payload"]["content"]["contextualize_fallback"] is False
    assert "R-04" not in {
        hit["rule_id"] for hit in evaluate_hard_rules(message)
    }


@pytest.mark.parametrize(
    ("knowledge_point", "diagnostic_difficulty", "expected_template_id"),
    (
        ("三道工序与传导关系", "basic", "T-03"),
        ("三道工序与传导关系", "applied", "T-03-A"),
        ("完成率计算", "advanced", "T-02-B"),
    ),
)
def test_diagnosis_route_selects_the_exact_production_task(
    knowledge_point: str,
    diagnostic_difficulty: str,
    expected_template_id: str,
) -> None:
    message = _task_module().TaskAgent("trace-diagnosis-route").generate_for_diagnosis(
        knowledge_point,
        diagnostic_difficulty,
    )
    content = message["payload"]["content"]

    assert content["template_id"] == expected_template_id
    assert content["knowledge_point"] == knowledge_point
    assert content["difficulty"] == diagnostic_difficulty
    assert content["query_authority"]["template_id"] == expected_template_id
    assert message["evidence"][0]["ref"] == expected_template_id


def test_diagnosis_route_does_not_open_an_llm_generation_path() -> None:
    calls: list[dict[str, Any]] = []

    def forbidden_route_llm(**request: Any) -> LLMResult:
        calls.append(request)
        raise AssertionError("diagnosis routing must be deterministic")

    message = _task_module().TaskAgent(
        "trace-deterministic-diagnosis-route",
        llm_call=forbidden_route_llm,
    ).generate_for_diagnosis("三道工序与传导关系", "applied")

    assert message["payload"]["content"]["template_id"] == "T-03-A"
    assert message["payload"]["content"]["contextualize_fallback"] is True
    assert message["payload"]["content"]["contextualize_fallback_reason"] == (
        "missing_context"
    )
    assert calls == []


@pytest.mark.parametrize(
    ("current_template_id", "action", "expected_template_id", "expected_difficulty"),
    (
        ("T-01", "keep", "T-01", "basic"),
        ("T-01", "step_up", "T-01-A", "applied"),
        ("T-01-A", "step_up", "T-01-B", "advanced"),
        ("T-01-B", "step_down", "T-01-A", "applied"),
    ),
)
def test_learning_action_selects_a_task_from_the_same_catalog_strategy(
    current_template_id: str,
    action: str,
    expected_template_id: str,
    expected_difficulty: str,
) -> None:
    message = _task_module().TaskAgent(
        "trace-learning-action-route",
    ).generate_for_learning_action(current_template_id, action)

    assert message is not None
    content = message["payload"]["content"]
    assert content["template_id"] == expected_template_id
    assert content["knowledge_point"] == "计划量与实际量口径"
    assert content["difficulty"] == expected_difficulty
    assert content["query_authority"]["template_id"] == expected_template_id
    assert message["evidence"][0]["ref"] == expected_template_id


def test_learning_action_reports_catalog_boundaries_without_fake_tiers() -> None:
    module = _task_module()
    production_agent = module.TaskAgent("trace-production-learning-boundary")
    second_domain_agent = module.TaskAgent(
        "trace-second-domain-learning-boundary",
        catalog=module.load_task_catalog(
            domain_config=load_domain_config("first_segment"),
        ),
    )

    assert production_agent.generate_for_learning_action("T-01-B", "step_up") is None
    assert production_agent.generate_for_learning_action("T-01", "step_down") is None
    assert (
        second_domain_agent.generate_for_learning_action("T-FS02", "step_up")
        is None
    )
    kept = second_domain_agent.generate_for_learning_action("T-FS02", "keep")
    assert kept is not None
    assert kept["payload"]["content"]["template_id"] == "T-FS02"
    assert kept["payload"]["content"]["difficulty"] == "basic"


def test_learning_action_route_is_deterministic_and_fail_closed() -> None:
    calls: list[dict[str, Any]] = []

    def forbidden_learning_action_llm(**request: Any) -> LLMResult:
        calls.append(request)
        raise AssertionError("learning-action routing must be deterministic")

    agent = _task_module().TaskAgent(
        "trace-deterministic-learning-action",
        llm_call=forbidden_learning_action_llm,
    )

    selected = agent.generate_for_learning_action("T-01", "step_up")
    assert selected is not None
    assert selected["payload"]["content"]["template_id"] == "T-01-A"
    assert calls == []
    with pytest.raises(ValueError, match="learning action"):
        agent.generate_for_learning_action("T-01", "skip")
    with pytest.raises(ValueError, match="template_id"):
        agent.generate_for_learning_action("UNCONFIGURED", "keep")


def test_diagnosis_route_uses_the_active_second_domain_catalog() -> None:
    module = _task_module()
    catalog = module.load_task_catalog(
        domain_config=load_domain_config("first_segment"),
    )

    message = module.TaskAgent(
        "trace-first-segment-diagnosis-route",
        catalog=catalog,
    ).generate_for_diagnosis("物量数量口径", "basic")

    content = message["payload"]["content"]
    assert content["template_id"] == "T-FS02"
    assert content["knowledge_point"] == "物量数量口径"
    assert content["difficulty"] == "basic"


@pytest.mark.parametrize(
    ("knowledge_point", "diagnostic_difficulty"),
    (
        ("未配置知识点", "basic"),
        ("完成率计算", "expert"),
    ),
)
def test_diagnosis_route_rejects_unconfigured_results(
    knowledge_point: str,
    diagnostic_difficulty: str,
) -> None:
    with pytest.raises(ValueError, match="diagnostic"):
        _task_module().TaskAgent(
            "trace-invalid-diagnosis-route",
        ).generate_for_diagnosis(knowledge_point, diagnostic_difficulty)


def test_monthly_aggregation_tier_sqls_fit_q6_period_ratio_contract() -> None:
    catalog = _task_module().load_task_catalog()

    for template_id in ("T-04-A", "T-04-B"):
        entry = catalog.templates[template_id]
        assert entry.family == "Q6"
        assert _output_aliases(entry.standard_sql) in FAMILY_COLUMN_SETS[entry.family]
        assert "period_date" in entry.standard_sql
        assert "batch_code" not in entry.standard_sql
        assert "SUM(actual_qty)/SUM(plan_qty)" in entry.standard_sql
        assert "AVG(" not in entry.standard_sql


def test_anomaly_identification_tier_answer_keys_match_the_real_database_rows() -> None:
    catalog = _task_module().load_task_catalog()

    applied = catalog.templates["T-05-A"]
    assert applied.question_template == (
        "查询{ship}{process}在2025-02至{month2}期间的月完成率序列，"
        "综合判断异常信号并列出非生产性差异核查项"
    )
    assert applied.expected_rows == (
        {"month_label": "2025-02", "complete_rate": "0.9435"},
        {"month_label": "2025-03", "complete_rate": "0.9534"},
        {"month_label": "2025-04", "complete_rate": "0.9061"},
        {"month_label": "2025-05", "complete_rate": "0.6236"},
        {"month_label": "2025-06", "complete_rate": "1.0067"},
        {"month_label": "2025-07", "complete_rate": "0.9698"},
    )
    assert applied.expected_points == (
        "2025-05=0.6236且相邻月份均在正常区间，说明它是显著单期偏低信号而非持续趋势；"
        "持续性不是异常成立的必要条件；定性前须排除口径、计划、报工、认定时点差异并补独立佐证，"
        "risk_level与完成率同源，不算独立佐证",
    )

    advanced = catalog.templates["T-05-B"]
    assert advanced.question_template == (
        "核验{ship}2025-04至{month2}三道工序×月份完成率，"
        "对各偏低信号分类处置状态并按顺序列出核查项"
    )
    assert advanced.expected_rows == (
        {"process_code": "AZTP", "month_label": "2025-04", "complete_rate": "0.9522"},
        {"process_code": "AZTP", "month_label": "2025-05", "complete_rate": "0.9149"},
        {"process_code": "AZTP", "month_label": "2025-06", "complete_rate": "0.9901"},
        {"process_code": "AZTP", "month_label": "2025-07", "complete_rate": "0.8501"},
        {"process_code": "YCL", "month_label": "2025-04", "complete_rate": "0.9061"},
        {"process_code": "YCL", "month_label": "2025-05", "complete_rate": "0.6236"},
        {"process_code": "YCL", "month_label": "2025-06", "complete_rate": "1.0067"},
        {"process_code": "YCL", "month_label": "2025-07", "complete_rate": "0.9698"},
        {"process_code": "ZZTP", "month_label": "2025-04", "complete_rate": "0.9326"},
        {"process_code": "ZZTP", "month_label": "2025-05", "complete_rate": "1.0249"},
        {"process_code": "ZZTP", "month_label": "2025-06", "complete_rate": "0.7545"},
        {"process_code": "ZZTP", "month_label": "2025-07", "complete_rate": "0.9981"},
    )
    assert advanced.expected_points == (
        "YCL 2025-05=0.6236、ZZTP 2025-06=0.7545、AZTP 2025-07=0.8501均是偏低信号；"
        "仅凭完成率序列证据不足，处置状态均为待核实信号",
        "依次核验口径→计划→报工→独立佐证：前三项任一命中则归为非生产性差异，"
        "全部通过且偏差显著或持续才可上报；不得据时序直接推断原因、传导或责任",
    )


def test_anomaly_identification_tier_sqls_fit_q4_q6_period_contracts() -> None:
    catalog = _task_module().load_task_catalog()

    assert catalog.templates["T-05-A"].family == "Q4"
    assert catalog.templates["T-05-B"].family == "Q6"
    for template_id in ("T-05-A", "T-05-B"):
        entry = catalog.templates[template_id]
        assert _output_aliases(entry.standard_sql) in FAMILY_COLUMN_SETS[entry.family]
        assert "period_date" in entry.standard_sql
        assert "batch_code" not in entry.standard_sql
        assert "SUM(actual_qty)/SUM(plan_qty)" in entry.standard_sql
        assert "AVG(" not in entry.standard_sql


def test_propagation_lag_tier_answer_keys_match_the_real_database_rows() -> None:
    catalog = _task_module().load_task_catalog()
    advanced_rows = (
        {"process_code": "AZTP", "month_label": "2025-04", "complete_rate": "0.9522"},
        {"process_code": "AZTP", "month_label": "2025-05", "complete_rate": "0.9149"},
        {"process_code": "AZTP", "month_label": "2025-06", "complete_rate": "0.9901"},
        {"process_code": "AZTP", "month_label": "2025-07", "complete_rate": "0.8501"},
        {"process_code": "YCL", "month_label": "2025-04", "complete_rate": "0.9061"},
        {"process_code": "YCL", "month_label": "2025-05", "complete_rate": "0.6236"},
        {"process_code": "YCL", "month_label": "2025-06", "complete_rate": "1.0067"},
        {"process_code": "YCL", "month_label": "2025-07", "complete_rate": "0.9698"},
        {"process_code": "ZZTP", "month_label": "2025-04", "complete_rate": "0.9326"},
        {"process_code": "ZZTP", "month_label": "2025-05", "complete_rate": "1.0249"},
        {"process_code": "ZZTP", "month_label": "2025-06", "complete_rate": "0.7545"},
        {"process_code": "ZZTP", "month_label": "2025-07", "complete_rate": "0.9981"},
    )

    basic = catalog.templates["T-07"]
    assert basic.question_template == (
        "查询{ship}{process_down}在{month}至{month_down}的按月完成率，"
        "只返回月份和完成率，标出候选响应期"
    )
    assert basic.expected_rows == (
        {"month_label": "2025-05", "complete_rate": "1.0249"},
        {"month_label": "2025-06", "complete_rate": "0.7545"},
    )
    assert basic.expected_points == (
        "YCL 2025-05异常仅作为候选窗口的教学背景，不进入本题查询输出；"
        "2025-05 ZZTP=1.0249、2025-06=0.7545；2025-06是候选响应期，"
        "仅说明时序相容，不确认传导",
    )

    applied = catalog.templates["T-07-A"]
    assert applied.question_template == (
        "查询{ship}{month}至{month2}三道工序×月份完成率，"
        "在给定候选窗口内对齐上下游并输出候选响应期"
    )
    assert applied.expected_rows == tuple(
        row for row in advanced_rows if row["month_label"] != "2025-04"
    )
    assert applied.expected_points == (
        "YCL 2025-05=0.6236为候选源头期，ZZTP 2025-06=0.7545、"
        "AZTP 2025-07=0.8501为候选响应期；相邻统计期只作候选查找起点，"
        "窗口须有计划衔接、在制缓冲或历史交接依据；仅说明时序相容，不确认传导",
    )

    advanced = catalog.templates["T-07-B"]
    assert advanced.question_template == (
        "核验{ship}2025-04至{month2}三道工序×月份完成率，"
        "先排查伪时滞，再输出四态候选交由传导关系知识点核验"
    )
    assert advanced.expected_rows == advanced_rows
    assert advanced.expected_points == (
        "YCL 2025-05=0.6236、ZZTP 2025-06=0.7545、AZTP 2025-07=0.8501"
        "在给定窗口内时序相容；先核对同批工作包/托盘/物料，排除对象链错配，"
        "再核对完工、报工、质检、工作包关闭时点，排除统计时点错位",
        "月度表未提供上述对象链与时点证据，当前四态候选为证据不足；"
        "若补证排除两类伪时滞，也只能列为疑似传导候选，最终由三道工序与传导关系"
        "结合依赖链、齐套、交接、共同因素及下游本地异常核验",
    )


def test_propagation_lag_tier_sqls_fit_q4_q6_period_contracts() -> None:
    catalog = _task_module().load_task_catalog()

    assert catalog.templates["T-07"].family == "Q4"
    assert catalog.templates["T-07-A"].family == "Q6"
    assert catalog.templates["T-07-B"].family == "Q6"
    for template_id in ("T-07", "T-07-A", "T-07-B"):
        entry = catalog.templates[template_id]
        assert _output_aliases(entry.standard_sql) in FAMILY_COLUMN_SETS[entry.family]
        assert "period_date" in entry.standard_sql
        assert "batch_code" not in entry.standard_sql
        assert "SUM(actual_qty)/SUM(plan_qty)" in entry.standard_sql
        assert "AVG(" not in entry.standard_sql


def test_decay_tier_answer_keys_match_real_rows_and_preserve_evidence_boundaries() -> None:
    catalog = _task_module().load_task_catalog()
    aligned_rows = (
        {"process_code": "YCL", "month_label": "2025-05", "complete_rate": "0.6236"},
        {"process_code": "ZZTP", "month_label": "2025-06", "complete_rate": "0.7545"},
        {"process_code": "AZTP", "month_label": "2025-07", "complete_rate": "0.8501"},
    )
    chain_rows = (
        {"process_code": "AZTP", "month_label": "2025-05", "complete_rate": "0.9149"},
        {"process_code": "AZTP", "month_label": "2025-06", "complete_rate": "0.9901"},
        {"process_code": "AZTP", "month_label": "2025-07", "complete_rate": "0.8501"},
        {"process_code": "YCL", "month_label": "2025-05", "complete_rate": "0.6236"},
        {"process_code": "YCL", "month_label": "2025-06", "complete_rate": "1.0067"},
        {"process_code": "YCL", "month_label": "2025-07", "complete_rate": "0.9698"},
        {"process_code": "ZZTP", "month_label": "2025-05", "complete_rate": "1.0249"},
        {"process_code": "ZZTP", "month_label": "2025-06", "complete_rate": "0.7545"},
        {"process_code": "ZZTP", "month_label": "2025-07", "complete_rate": "0.9981"},
    )

    basic = catalog.templates["T-08-DECAY"]
    assert basic.question_template == (
        "查询{ship}{month}{process}、{month_down}{process_down}、"
        "{month2}{process_last}的对齐完成率，说明观察到的强度形态且不预设方向"
    )
    assert basic.expected_rows == aligned_rows
    assert basic.expected_points == (
        "对齐节点为YCL 2025-05=0.6236、ZZTP 2025-06=0.7545、"
        "AZTP 2025-07=0.8501，对应欠产缺口为0.3764、0.2455、0.1499；"
        "本样本呈表观收窄，只描述数值形态，不推断传导或机制",
    )

    applied = catalog.templates["T-08-DECAY-A"]
    assert applied.expected_rows == chain_rows
    assert applied.expected_points == (
        "候选对齐链YCL 2025-05=0.6236、ZZTP 2025-06=0.7545、"
        "AZTP 2025-07=0.8501的欠产缺口依次为0.3764、0.2455、0.1499，"
        "数值形态为表观收窄；其他同期节点仅作对照",
        "月度表不能核实同一对象链、统计时点与计划版本；这些条件未对齐时应判不可比，"
        "不能由表观收窄直接解释缓冲吸收",
    )

    advanced = catalog.templates["T-08-DECAY-B"]
    assert advanced.expected_rows == tuple(
        {**row}
        for row in (
            {"process_code": "AZTP", "month_label": "2025-04", "complete_rate": "0.9522"},
            *chain_rows[:3],
            {"process_code": "YCL", "month_label": "2025-04", "complete_rate": "0.9061"},
            *chain_rows[3:6],
            {"process_code": "ZZTP", "month_label": "2025-04", "complete_rate": "0.9326"},
            *chain_rows[6:],
        )
    )
    assert advanced.expected_points == (
        "候选对齐链的强度形态为表观收窄，但须先排除对象链错配、统计期间错位、"
        "聚合/单位/计划版本不一致和计划基数效应",
        "两轴结论：强度形态=表观收窄（待排除假形态）；机制证据=证据不足；"
        "月度表不能判缓冲、补偿、放大、本地叠加或传导成立",
    )


def test_responsibility_tier_answer_keys_match_real_rows_without_turning_location_into_blame() -> None:
    catalog = _task_module().load_task_catalog()

    applied = catalog.templates["T-06-A"]
    assert applied.question_template == (
        "查询{ship}{month}{process}各责任单元完成率，"
        "定位候选落点并说明能否据此判断缺口集中或定责"
    )
    assert applied.expected_rows == (
        {"workshop_code": "WSB", "complete_rate": "0.6218"},
        {"workshop_code": "WSA", "complete_rate": "0.6253"},
    )
    assert applied.expected_points == (
        "WSB=0.6218、WSA=0.6253，WSB完成率略低，只能定位候选异常落点",
        "结果缺少各单元计划量与正向欠量，不能据此计算缺口贡献率、"
        "判断集中或分散，更不能认定责任",
    )

    advanced = catalog.templates["T-06-B"]
    assert advanced.question_template == (
        "查询{ship}{month2}{process_last}各责任单元完成率，"
        "区分五类候选成因并列出补证项"
    )
    assert advanced.expected_rows == (
        {"workshop_code": "WSF", "complete_rate": "0.8500"},
        {"workshop_code": "WSE", "complete_rate": "0.8502"},
    )
    assert advanced.expected_points == (
        "WSF=0.8500、WSE=0.8502，当前只能定位两个偏低执行单元，"
        "不能由细微差异判断源头或责任",
        "本地执行约束、上游缺口暴露、共性条件、报工口径和混合情形均待核；"
        "须补同一对象依赖路径与齐套暴露、共同因素、报工时点及本地设备/质量/物资证据",
    )


def test_cross_process_attribution_has_three_real_database_tiers_and_two_axis_limits() -> None:
    catalog = _task_module().load_task_catalog()
    aligned_rows = (
        {"process_code": "YCL", "month_label": "2025-05", "complete_rate": "0.6236"},
        {"process_code": "ZZTP", "month_label": "2025-06", "complete_rate": "0.7545"},
        {"process_code": "AZTP", "month_label": "2025-07", "complete_rate": "0.8501"},
    )

    basic = catalog.templates["T-08-ATTR"]
    assert basic.expected_rows == aligned_rows
    assert basic.expected_points == (
        "数据可见的时序候选链为YCL 2025-05=0.6236、ZZTP 2025-06=0.7545、"
        "AZTP 2025-07=0.8501；尚未核实同一对象依赖路径、齐套暴露与交接，"
        "不能锁定根因或责任",
    )

    applied = catalog.templates["T-08"]
    assert applied.question_template == (
        "从{ship}{month2}{process_last}完成率偏低出发，核验2025-04至{month2}"
        "三道工序×月份表并构造候选归因证据链"
    )
    assert applied.expected_points == (
        "数据可见范围内，YCL 2025-05=0.6236是最早的时序候选，"
        "ZZTP 2025-06=0.7545、AZTP 2025-07=0.8501构成后续候选节点；"
        "只到候选链，不确认传导",
        "须补同一工作包/托盘/物量依赖路径、下游齐套暴露、源头与落点执行单元、"
        "共同因素及下游本地异常证据，才能收窄候选",
    )

    advanced = catalog.templates["T-09"]
    assert advanced.question_template == (
        "核验{ship}2025-04至{month2}三道工序×月份表，"
        "处理多候选与混合情形并输出传导证据×本地叠加两轴结论"
    )
    assert advanced.expected_points == (
        "月度序列显示YCL 2025-05、ZZTP 2025-06、AZTP 2025-07时序相容，"
        "但缺少对象依赖、齐套暴露、交接及独立本地异常证据，候选源头不能唯一锁定",
        "两轴结论：传导证据=证据不足；本地叠加=待核；"
        "不得把表观收窄当成传导成立或无本地问题的证明",
    )


def test_new_transmission_templates_fit_family_period_and_sandbox_contracts() -> None:
    catalog = _task_module().load_task_catalog()

    for template_id in (
        "T-06-A",
        "T-06-B",
        "T-08-DECAY",
        "T-08-DECAY-A",
        "T-08-DECAY-B",
        "T-08-ATTR",
        "T-08",
        "T-09",
    ):
        entry = catalog.templates[template_id]
        assert _output_aliases(entry.standard_sql) in FAMILY_COLUMN_SETS[entry.family]
        assert "period_date" in entry.standard_sql
        assert "batch_code" not in entry.standard_sql
        assert "SUM(actual_qty)/SUM(plan_qty)" in entry.standard_sql
        assert "AVG(" not in entry.standard_sql
        decision = validate_and_rewrite(entry.standard_sql)
        assert decision.allowed, (template_id, decision.rule_id, decision.reason)
        assert decision.executed_sql is not None

    active_assets = json.dumps(
        {
            template_id: {
                "question": catalog.templates[template_id].question_template,
                "points": catalog.templates[template_id].expected_points,
            }
            for template_id in catalog.templates
        },
        ensure_ascii=False,
    )
    assert "逐级衰减" not in active_assets
    assert "时滞规律" not in active_assets


def test_propagation_lag_templates_render_complete_iso_month_boundaries() -> None:
    agent = _task_module().TaskAgent("trace-propagation-lag-month-boundaries")
    expected_boundaries = {
        "basic": ("2025-05", "2025-06"),
        "applied": ("2025-05", "2025-07"),
        "advanced": ("2025-04", "2025-07"),
    }

    for difficulty, boundaries in expected_boundaries.items():
        message = agent.generate("T-07", diagnostic_difficulty=difficulty)
        standard_stem = message["payload"]["content"]["standard_stem"]
        assert all(boundary in standard_stem for boundary in boundaries)
        assert "3-5月" not in standard_stem


def test_propagation_lag_basic_generation_stem_is_single_process_q4() -> None:
    content = _task_module().TaskAgent(
        "trace-propagation-lag-single-process"
    ).generate("T-07", diagnostic_difficulty="basic")["payload"]["content"]

    assert content["standard_stem"] == (
        "查询H2601ZZTP在2025-05至2025-06的按月完成率，"
        "只返回月份和完成率，标出候选响应期"
    )
    assert "YCL" not in content["standard_stem"]
    assert content["family"] == "Q4"
    assert content["query_authority"]["output_columns"] == [
        "month_label",
        "complete_rate",
    ]


def test_propagation_lag_basic_contextualization_passes_unchanged_r04() -> None:
    llm = SpyLLM(
        {
            "contextualized_stem": (
                "作为新入职生产计划员，请查询H2601的ZZTP在2025-05至2025-06的"
                "按月完成率，只返回月份和完成率，并标出候选响应期。"
            ),
            "guide_intro": (
                "YCL 2025-05异常仅用于说明候选窗口；查询时只按月观察ZZTP，"
                "再区分时序相容与传导判定。"
            ),
        }
    )
    message = _task_module().TaskAgent(
        "trace-propagation-lag-r04-regression",
        llm_call=llm,
    ).generate(
        "T-07",
        diagnostic_difficulty="basic",
        student_profile=_planner_profile(),
        learning_report_summary="当前需巩固传导时滞分析。",
    )

    content = message["payload"]["content"]
    assert content["contextualize_fallback"] is False
    assert "YCL" not in content["contextualized_stem"]
    assert "YCL 2025-05异常" in content["guide_intro"]
    assert "R-04" not in {hit["rule_id"] for hit in evaluate_hard_rules(message)}


@pytest.mark.parametrize(
    ("diagnostic_difficulty", "expected_template_id"),
    (
        ("basic", "T-02"),
        ("applied", "T-02-A"),
        ("advanced", "T-02-B"),
    ),
)
def test_completion_rate_task_is_selected_by_diagnostic_difficulty(
    diagnostic_difficulty: str,
    expected_template_id: str,
) -> None:
    message = _task_module().TaskAgent("trace-tier-selection").generate(
        "T-02",
        diagnostic_difficulty=diagnostic_difficulty,
    )
    content = message["payload"]["content"]

    assert content["template_id"] == expected_template_id
    assert content["difficulty"] == diagnostic_difficulty
    assert message["evidence"][0]["ref"] == expected_template_id


@pytest.mark.parametrize(
    ("diagnostic_difficulty", "expected_template_id"),
    (
        ("basic", "T-01"),
        ("applied", "T-01-A"),
        ("advanced", "T-01-B"),
    ),
)
def test_plan_actual_task_is_selected_by_diagnostic_difficulty(
    diagnostic_difficulty: str,
    expected_template_id: str,
) -> None:
    message = _task_module().TaskAgent("trace-plan-actual-tier-selection").generate(
        "T-01",
        diagnostic_difficulty=diagnostic_difficulty,
    )
    content = message["payload"]["content"]

    assert content["template_id"] == expected_template_id
    assert content["difficulty"] == diagnostic_difficulty
    assert message["evidence"][0]["ref"] == expected_template_id


@pytest.mark.parametrize(
    ("diagnostic_difficulty", "expected_template_id"),
    (
        ("basic", "T-03"),
        ("applied", "T-03-A"),
        ("advanced", "T-03-B"),
    ),
)
def test_process_propagation_task_is_selected_by_diagnostic_difficulty(
    diagnostic_difficulty: str,
    expected_template_id: str,
) -> None:
    message = _task_module().TaskAgent("trace-process-propagation-tier-selection").generate(
        "T-03",
        diagnostic_difficulty=diagnostic_difficulty,
    )
    content = message["payload"]["content"]

    assert content["template_id"] == expected_template_id
    assert content["difficulty"] == diagnostic_difficulty
    assert message["evidence"][0]["ref"] == expected_template_id


@pytest.mark.parametrize(
    ("diagnostic_difficulty", "expected_template_id"),
    (
        ("basic", "T-10"),
        ("applied", "T-10-A"),
        ("advanced", "T-10-B"),
    ),
)
def test_deviation_risk_task_is_selected_by_diagnostic_difficulty(
    diagnostic_difficulty: str,
    expected_template_id: str,
) -> None:
    message = _task_module().TaskAgent("trace-deviation-risk-tier-selection").generate(
        "T-10",
        diagnostic_difficulty=diagnostic_difficulty,
    )
    content = message["payload"]["content"]

    assert content["template_id"] == expected_template_id
    assert content["difficulty"] == diagnostic_difficulty
    assert message["evidence"][0]["ref"] == expected_template_id


@pytest.mark.parametrize(
    ("diagnostic_difficulty", "expected_template_id"),
    (
        ("basic", "T-04"),
        ("applied", "T-04-A"),
        ("advanced", "T-04-B"),
    ),
)
def test_monthly_aggregation_task_is_selected_by_diagnostic_difficulty(
    diagnostic_difficulty: str,
    expected_template_id: str,
) -> None:
    message = _task_module().TaskAgent(
        "trace-monthly-aggregation-tier-selection"
    ).generate(
        "T-04",
        diagnostic_difficulty=diagnostic_difficulty,
    )
    content = message["payload"]["content"]

    assert content["template_id"] == expected_template_id
    assert content["difficulty"] == diagnostic_difficulty
    assert message["evidence"][0]["ref"] == expected_template_id


@pytest.mark.parametrize(
    ("diagnostic_difficulty", "expected_template_id"),
    (
        ("basic", "T-05"),
        ("applied", "T-05-A"),
        ("advanced", "T-05-B"),
    ),
)
def test_anomaly_identification_task_is_selected_by_diagnostic_difficulty(
    diagnostic_difficulty: str,
    expected_template_id: str,
) -> None:
    message = _task_module().TaskAgent(
        "trace-anomaly-identification-tier-selection"
    ).generate(
        "T-05",
        diagnostic_difficulty=diagnostic_difficulty,
    )
    content = message["payload"]["content"]

    assert content["template_id"] == expected_template_id
    assert content["difficulty"] == diagnostic_difficulty
    assert message["evidence"][0]["ref"] == expected_template_id


@pytest.mark.parametrize(
    ("diagnostic_difficulty", "expected_template_id"),
    (
        ("basic", "T-07"),
        ("applied", "T-07-A"),
        ("advanced", "T-07-B"),
    ),
)
def test_propagation_lag_task_is_selected_by_diagnostic_difficulty(
    diagnostic_difficulty: str,
    expected_template_id: str,
) -> None:
    message = _task_module().TaskAgent(
        "trace-propagation-lag-tier-selection"
    ).generate(
        "T-07",
        diagnostic_difficulty=diagnostic_difficulty,
    )
    content = message["payload"]["content"]

    assert content["template_id"] == expected_template_id
    assert content["difficulty"] == diagnostic_difficulty
    assert message["evidence"][0]["ref"] == expected_template_id


@pytest.mark.parametrize(
    ("requested_template_id", "diagnostic_difficulty", "expected_template_id"),
    (
        ("T-08-DECAY", "basic", "T-08-DECAY"),
        ("T-08-DECAY", "applied", "T-08-DECAY-A"),
        ("T-08-DECAY", "advanced", "T-08-DECAY-B"),
        ("T-06", "basic", "T-06"),
        ("T-06", "applied", "T-06-A"),
        ("T-06", "advanced", "T-06-B"),
        ("T-08", "basic", "T-08-ATTR"),
        ("T-08", "applied", "T-08"),
        ("T-08", "advanced", "T-09"),
        ("T-09", "basic", "T-08-ATTR"),
        ("T-09", "applied", "T-08"),
        ("T-09", "advanced", "T-09"),
    ),
)
def test_remaining_transmission_tasks_are_selected_by_diagnostic_difficulty(
    requested_template_id: str,
    diagnostic_difficulty: str,
    expected_template_id: str,
) -> None:
    message = _task_module().TaskAgent(
        "trace-transmission-tier-selection"
    ).generate(
        requested_template_id,
        diagnostic_difficulty=diagnostic_difficulty,
    )
    content = message["payload"]["content"]

    assert content["template_id"] == expected_template_id
    assert content["difficulty"] == diagnostic_difficulty
    assert message["evidence"][0]["ref"] == expected_template_id


def test_transmission_task_stems_render_complete_iso_months() -> None:
    agent = _task_module().TaskAgent("trace-transmission-month-boundaries")

    for requested_template_id in ("T-08-DECAY", "T-06", "T-08"):
        for difficulty in ("basic", "applied", "advanced"):
            content = agent.generate(
                requested_template_id,
                diagnostic_difficulty=difficulty,
            )["payload"]["content"]
            standard_stem = content["standard_stem"]
            assert re.search(r"2025-\d{2}", standard_stem)
            assert not re.search(r"(?<!\d)\d{1,2}月", standard_stem)


def test_completion_rate_tasks_declare_current_and_direct_prerequisite_scope() -> None:
    agent = _task_module().TaskAgent("trace-responsibility-scope")

    basic = agent.generate("T-02", diagnostic_difficulty="basic")
    applied = agent.generate("T-02", diagnostic_difficulty="applied")
    advanced = agent.generate("T-02", diagnostic_difficulty="advanced")

    assert basic["payload"]["content"]["responsibility_scope"] == [
        "完成率计算",
        "计划量与实际量口径",
    ]
    assert applied["payload"]["content"]["responsibility_scope"] == [
        "完成率计算",
        "计划量与实际量口径",
    ]
    assert advanced["payload"]["content"]["responsibility_scope"] == [
        "完成率计算",
        "责任单元定位",
    ]


def test_plan_actual_tasks_limit_responsibility_to_the_current_knowledge_point() -> None:
    agent = _task_module().TaskAgent("trace-plan-actual-responsibility-scope")

    for difficulty in ("basic", "applied", "advanced"):
        message = agent.generate("T-01", diagnostic_difficulty=difficulty)
        assert message["payload"]["content"]["responsibility_scope"] == [
            "计划量与实际量口径"
        ]


def test_process_propagation_tasks_limit_r03_to_the_current_blind_spot() -> None:
    agent = _task_module().TaskAgent("trace-process-propagation-responsibility-scope")

    for difficulty in ("basic", "applied", "advanced"):
        message = agent.generate("T-03", diagnostic_difficulty=difficulty)
        assert message["payload"]["content"]["responsibility_scope"] == [
            "三道工序与传导关系"
        ]


def test_deviation_risk_tasks_declare_only_current_and_direct_prerequisites() -> None:
    agent = _task_module().TaskAgent("trace-deviation-risk-responsibility-scope")

    basic = agent.generate("T-10", diagnostic_difficulty="basic")
    applied = agent.generate("T-10", diagnostic_difficulty="applied")
    advanced = agent.generate("T-10", diagnostic_difficulty="advanced")

    assert basic["payload"]["content"]["responsibility_scope"] == [
        "偏差率与风险等级",
        "完成率计算",
    ]
    assert applied["payload"]["content"]["responsibility_scope"] == [
        "偏差率与风险等级",
        "完成率计算",
    ]
    assert advanced["payload"]["content"]["responsibility_scope"] == [
        "偏差率与风险等级"
    ]


def test_monthly_aggregation_tasks_limit_r03_to_the_current_blind_spot() -> None:
    agent = _task_module().TaskAgent("trace-monthly-aggregation-responsibility-scope")

    for difficulty in ("basic", "applied", "advanced"):
        message = agent.generate("T-04", diagnostic_difficulty=difficulty)
        assert message["payload"]["content"]["responsibility_scope"] == [
            "月度聚合方法"
        ]


def test_anomaly_identification_tasks_limit_r03_to_relevant_blind_spots() -> None:
    agent = _task_module().TaskAgent("trace-anomaly-identification-scope")

    basic = agent.generate("T-05", diagnostic_difficulty="basic")
    applied = agent.generate("T-05", diagnostic_difficulty="applied")
    advanced = agent.generate("T-05", diagnostic_difficulty="advanced")

    assert basic["payload"]["content"]["responsibility_scope"] == [
        "异常识别标准",
        "完成率计算",
    ]
    assert applied["payload"]["content"]["responsibility_scope"] == [
        "异常识别标准"
    ]
    assert advanced["payload"]["content"]["responsibility_scope"] == [
        "异常识别标准"
    ]


def test_propagation_lag_tasks_limit_r03_to_relevant_blind_spots() -> None:
    agent = _task_module().TaskAgent("trace-propagation-lag-scope")

    basic = agent.generate("T-07", diagnostic_difficulty="basic")
    applied = agent.generate("T-07", diagnostic_difficulty="applied")
    advanced = agent.generate("T-07", diagnostic_difficulty="advanced")

    assert basic["payload"]["content"]["responsibility_scope"] == [
        "传导时滞分析",
        "三道工序与传导关系",
        "异常识别标准",
    ]
    assert applied["payload"]["content"]["responsibility_scope"] == [
        "传导时滞分析",
        "三道工序与传导关系",
    ]
    assert advanced["payload"]["content"]["responsibility_scope"] == [
        "传导时滞分析",
        "三道工序与传导关系",
    ]


def test_remaining_transmission_tasks_limit_r03_to_direct_prerequisite_blind_spots() -> None:
    agent = _task_module().TaskAgent("trace-transmission-responsibility-scope")

    expected_scopes = {
        ("T-08-DECAY", "basic"): ["异常衰减规律", "传导时滞分析"],
        ("T-08-DECAY", "applied"): ["异常衰减规律", "传导时滞分析"],
        ("T-08-DECAY", "advanced"): ["异常衰减规律", "三道工序与传导关系"],
        ("T-06", "basic"): ["责任单元定位", "月度聚合方法"],
        ("T-06", "applied"): ["责任单元定位", "月度聚合方法"],
        ("T-06", "advanced"): ["责任单元定位", "三道工序与传导关系"],
        ("T-08", "basic"): [
            "跨工序归因方法",
            "责任单元定位",
            "传导时滞分析",
            "异常衰减规律",
        ],
        ("T-08", "applied"): [
            "跨工序归因方法",
            "传导时滞分析",
            "异常衰减规律",
            "责任单元定位",
        ],
        ("T-08", "advanced"): ["跨工序归因方法", "三道工序与传导关系"],
    }

    for (template_id, difficulty), expected_scope in expected_scopes.items():
        message = agent.generate(template_id, diagnostic_difficulty=difficulty)
        assert message["payload"]["content"]["responsibility_scope"] == expected_scope


def test_task_exposes_template_sql_shape_as_structured_query_authority() -> None:
    message = _task_module().TaskAgent("trace-query-authority").generate(
        "T-02", diagnostic_difficulty="applied"
    )

    assert message["payload"]["content"]["query_authority"] == {
        "source": "task_template",
        "template_id": "T-02-A",
        "family": "Q6",
        "standard_stem": "查询H26012025-05三道工序的完成率",
        "output_columns": ["process_code", "complete_rate"],
        "metric_columns": ["complete_rate"],
        "dimension_columns": ["process_code"],
        "filter_columns": ["period_date", "ship_no"],
        "group_by_columns": ["process_code"],
        "time_column": "period_date",
        "time_values": ["2025-05"],
    }


def test_plan_actual_applied_task_exposes_period_date_grouping_authority() -> None:
    message = _task_module().TaskAgent("trace-plan-actual-authority").generate(
        "T-01", diagnostic_difficulty="applied"
    )

    assert message["payload"]["content"]["query_authority"] == {
        "source": "task_template",
        "template_id": "T-01-A",
        "family": "Q5",
        "standard_stem": "查询2025-05各船YCL完成率，按船号比较计划兑现情况",
        "output_columns": ["ship_no", "complete_rate"],
        "metric_columns": ["complete_rate"],
        "dimension_columns": ["ship_no"],
        "filter_columns": ["period_date", "process_code"],
        "group_by_columns": ["ship_no"],
        "time_column": "period_date",
        "time_values": ["2025-05"],
    }


def test_process_propagation_applied_task_exposes_q6_period_date_authority() -> None:
    message = _task_module().TaskAgent("trace-process-propagation-authority").generate(
        "T-03", diagnostic_difficulty="applied"
    )

    assert message["payload"]["content"]["query_authority"] == {
        "source": "task_template",
        "template_id": "T-03-A",
        "family": "Q6",
        "standard_stem": "查询H26012025-05至2025-07三道工序×月份完成率，筛查疑似传导",
        "output_columns": ["process_code", "month_label", "complete_rate"],
        "metric_columns": ["complete_rate"],
        "dimension_columns": ["process_code", "month_label"],
        "filter_columns": ["period_date", "ship_no"],
        "group_by_columns": ["month_label", "process_code"],
        "time_column": "period_date",
        "time_values": ["2025-05", "2025-07"],
    }


def test_deviation_risk_applied_task_exposes_q7_threshold_authority() -> None:
    message = _task_module().TaskAgent("trace-deviation-risk-authority").generate(
        "T-10", diagnostic_difficulty="applied"
    )

    assert message["payload"]["content"]["query_authority"] == {
        "source": "task_template",
        "template_id": "T-10-A",
        "family": "Q7",
        "standard_stem": "按项目高风险偏差阈值（deviation_rate < -0.15）筛选2025-05YCL记录，"
        "并比较各责任单元的风险分布",
        "output_columns": ["workshop_code", "high_risk_rows"],
        "metric_columns": ["high_risk_rows"],
        "dimension_columns": ["workshop_code"],
        "filter_columns": ["deviation_rate", "period_date", "process_code"],
        "group_by_columns": ["workshop_code"],
        "time_column": "period_date",
        "time_values": ["2025-05"],
    }


def test_monthly_aggregation_applied_task_exposes_q6_period_date_authority() -> None:
    message = _task_module().TaskAgent(
        "trace-monthly-aggregation-authority"
    ).generate("T-04", diagnostic_difficulty="applied")

    assert message["payload"]["content"]["query_authority"] == {
        "source": "task_template",
        "template_id": "T-04-A",
        "family": "Q6",
        "standard_stem": (
            "查询H26012025-05至2025-07三道工序×月份完成率，并核对比率聚合方法"
        ),
        "output_columns": ["process_code", "month_label", "complete_rate"],
        "metric_columns": ["complete_rate"],
        "dimension_columns": ["process_code", "month_label"],
        "filter_columns": ["period_date", "ship_no"],
        "group_by_columns": ["month_label", "process_code"],
        "time_column": "period_date",
        "time_values": ["2025-05", "2025-07"],
    }


def test_anomaly_identification_applied_task_exposes_q4_period_date_authority() -> None:
    message = _task_module().TaskAgent(
        "trace-anomaly-identification-authority"
    ).generate("T-05", diagnostic_difficulty="applied")

    assert message["payload"]["content"]["query_authority"] == {
        "source": "task_template",
        "template_id": "T-05-A",
        "family": "Q4",
        "standard_stem": (
            "查询H2601YCL在2025-02至2025-07期间的月完成率序列，"
            "综合判断异常信号并列出非生产性差异核查项"
        ),
        "output_columns": ["month_label", "complete_rate"],
        "metric_columns": ["complete_rate"],
        "dimension_columns": ["month_label"],
        "filter_columns": ["period_date", "process_code", "ship_no"],
        "group_by_columns": ["month_label"],
        "time_column": "period_date",
        "time_values": ["2025-02", "2025-07"],
    }


def test_propagation_lag_applied_task_exposes_q6_period_date_authority() -> None:
    message = _task_module().TaskAgent(
        "trace-propagation-lag-authority"
    ).generate("T-07", diagnostic_difficulty="applied")

    assert message["payload"]["content"]["query_authority"] == {
        "source": "task_template",
        "template_id": "T-07-A",
        "family": "Q6",
        "standard_stem": (
            "查询H26012025-05至2025-07三道工序×月份完成率，"
            "在给定候选窗口内对齐上下游并输出候选响应期"
        ),
        "output_columns": ["process_code", "month_label", "complete_rate"],
        "metric_columns": ["complete_rate"],
        "dimension_columns": ["process_code", "month_label"],
        "filter_columns": ["period_date", "ship_no"],
        "group_by_columns": ["month_label", "process_code"],
        "time_column": "period_date",
        "time_values": ["2025-05", "2025-07"],
    }


def test_difficulty_selection_never_remaps_across_knowledge_points() -> None:
    catalog = _task_module().load_task_catalog()
    agent = _task_module().TaskAgent("trace-tier-knowledge-boundary", catalog=catalog)

    for requested_template_id in (
        "T-01",
        "T-02",
        "T-03",
        "T-04",
        "T-05",
        "T-06",
        "T-07",
        "T-08-DECAY",
        "T-08",
        "T-10",
    ):
        requested_point = catalog.templates[requested_template_id].knowledge_point
        for difficulty in ("basic", "applied", "advanced"):
            selected = agent.generate(
                requested_template_id,
                diagnostic_difficulty=difficulty,
            )["payload"]["content"]
            assert selected["knowledge_point"] == requested_point
            assert selected["difficulty"] == difficulty


def test_invalid_diagnostic_difficulty_is_rejected() -> None:
    with pytest.raises(ValueError, match="diagnostic_difficulty"):
        _task_module().TaskAgent("trace-tier-invalid").generate(
            "T-02",
            diagnostic_difficulty="expert",
        )


def test_machine_assets_contain_full_sandbox_accepted_sql() -> None:
    catalog = _task_module().load_task_catalog()

    all_sql = [entry.standard_sql for entry in catalog.templates.values()]
    all_sql.extend(
        sql
        for entry in catalog.counter_evidence.values()
        for sql in entry.standard_sqls
    )
    assert all_sql
    for sql in all_sql:
        assert "..." not in sql
        assert "同构" not in sql
        decision = validate_and_rewrite(sql)
        assert decision.allowed, (decision.rule_id, decision.reason, sql)
        assert decision.executed_sql is not None


def test_catalog_preserves_required_result_order_and_t07_values() -> None:
    catalog = _task_module().load_task_catalog()

    assert "ORDER BY month_label" in catalog.templates["T-04"].standard_sql
    assert "ORDER BY complete_rate ASC" in catalog.templates["T-05"].standard_sql
    assert "ORDER BY complete_rate ASC" in catalog.templates["T-06"].standard_sql
    assert catalog.templates["T-07"].expected_rows == (
        {"month_label": "2025-05", "complete_rate": "1.0249"},
        {"month_label": "2025-06", "complete_rate": "0.7545"},
    )


def test_counter_evidence_m04_keeps_q5_then_q3_order() -> None:
    entry = _task_module().load_task_catalog().counter_evidence["M-04"]

    assert entry.family == "Q5+Q3"
    assert len(entry.standard_sqls) == 2
    assert "GROUP BY ship_no" in entry.standard_sqls[0]
    assert "AS complete_rate" in entry.standard_sqls[1]
    assert "GROUP BY ship_no" not in entry.standard_sqls[1]


def test_counter_evidence_m05_uses_completion_rate_gap_percentage_points() -> None:
    entry = _task_module().load_task_catalog().counter_evidence["M-05"]

    assert entry.expected_points == (
        "缺口=1-完成率：37.64%→24.55%→14.99%，本样本呈表观收窄；"
        "强度方向不预设，数值形态不等于传导或机制结论（口径=完成率缺口百分点，非绝对量）",
    )


@pytest.mark.parametrize("template_id", list(EXPECTED_TEMPLATES))
def test_generate_returns_deterministic_protocol_product(
    template_id: str, tmp_path: Path
) -> None:
    module = _task_module()
    draft = module.TaskAgent(f"trace-{template_id.lower()}").generate(template_id)
    content = draft["payload"]["content"]
    expected = EXPECTED_TEMPLATES[template_id]

    assert draft["agent"] == "task"
    assert draft["role"] == "produce"
    assert draft["payload"]["type"] == expected[2]
    assert content["event"] == "product_ready"
    assert (
        content["template_id"],
        content["knowledge_point"],
        content["difficulty"],
        content["family"],
    ) == (template_id, expected[0], expected[1], expected[3])
    assert "{" not in content["question"] and "}" not in content["question"]
    if expected[2] == "quiz_set":
        assert content["questions"] == [
            {"id": template_id, "prompt": content["question"]}
        ]
        assert "guide_md" not in content
        assert "guide_steps" not in content
        assert "completion_criteria" not in content
    else:
        assert content["guide_md"] != content["question"]
        assert len(content["guide_steps"]) >= 3
        assert content["completion_criteria"]
        assert "questions" not in content
    assert draft["claims"] == []
    assert len(draft["evidence"]) == 1
    answer_key = draft["evidence"][0]
    assert answer_key["kind"] == "quiz_answer_key"
    assert answer_key["ref"] == template_id
    decoded = json.loads(answer_key["quote"])
    assert decoded["standard_sql"] == module.load_task_catalog().templates[
        template_id
    ].standard_sql
    assert decoded["expected_rows"] == list(
        module.load_task_catalog().templates[template_id].expected_rows
    )
    for field in ("model", "token_usage", "latency_ms"):
        assert field not in draft
    assert MessageBus(tmp_path / "traces").send(draft).accepted


@pytest.mark.parametrize("domain_id", DOMAIN_PACKAGE_IDS)
def test_every_domain_practice_guide_is_structured_deterministic_and_reviewable(
    domain_id: str,
) -> None:
    module = _task_module()
    catalog = module.load_task_catalog(
        domain_config=load_domain_config(ROOT / "config" / "domains" / domain_id)
    )
    practice_guides = [
        entry
        for entry in catalog.templates.values()
        if entry.payload_type == "practice_guide"
    ]

    assert practice_guides
    agent = module.TaskAgent(f"trace-practice-guide-{domain_id}", catalog=catalog)
    for entry in practice_guides:
        first_message = agent.generate(entry.template_id)
        second_message = agent.generate(entry.template_id)
        content = first_message["payload"]["content"]
        learner_surface = {
            field: content[field]
            for field in LEARNER_GUIDE_FIELDS
        }
        learner_text = "\n".join(
            [
                learner_surface["question"],
                learner_surface["contextualized_stem"],
                learner_surface["guide_intro"],
                *learner_surface["guide_steps"],
                *learner_surface["completion_criteria"],
                learner_surface["guide_md"],
            ]
        )

        assert content == second_message["payload"]["content"]
        assert content["question"] == content["contextualized_stem"]
        assert content["guide_intro"] == (
            "开始前，请确认题目中的对象、范围和统计口径。"
        )
        assert len(content["guide_steps"]) >= 3
        assert all(
            isinstance(step, str) and step.strip()
            for step in content["guide_steps"]
        )
        assert content["completion_criteria"]
        assert all(
            isinstance(criterion, str) and criterion.strip()
            for criterion in content["completion_criteria"]
        )
        assert content["guide_md"] != content["question"]
        for heading in ("实操目标", "开始前", "操作步骤", "完成标准"):
            assert f"## {heading}" in content["guide_md"]
        for index, step in enumerate(content["guide_steps"], start=1):
            assert f"{index}. {step}" in content["guide_md"]
        for criterion in content["completion_criteria"]:
            assert f"- {criterion}" in content["guide_md"]
        assert re.search(r"\{[A-Za-z_]\w*\}", learner_text) is None
        assert re.search(
            r"\b(?:SELECT|FROM|WHERE|GROUP\s+BY|ORDER\s+BY)\b",
            learner_text,
            flags=re.IGNORECASE,
        ) is None
        for engineering_term in (
            "standard_sql",
            "expected_rows",
            "expected_points",
            "answer_key",
            "template_id",
            "family",
            "query_authority",
            entry.template_id,
            entry.family,
            entry.standard_sql,
        ):
            assert engineering_term not in learner_text
        assert (
            json.dumps(list(entry.expected_rows), ensure_ascii=False)
            not in learner_text
        )
        assert (
            json.dumps(list(entry.expected_points), ensure_ascii=False)
            not in learner_text
        )
        for row in entry.expected_rows:
            for answer_value in row.values():
                if answer_value not in content["question"]:
                    assert answer_value not in learner_text
        for expected_point in entry.expected_points:
            assert expected_point not in learner_text
        assert evaluate_hard_rules(first_message) == ()


def test_practice_guide_contextualization_keeps_one_existing_llm_call() -> None:
    module = _task_module()
    llm = SpyLLM(
        {
            "contextualized_stem": (
                "作为生产计划员，请按工序顺序查询H2601在2025-05的三道工序完成率。"
            ),
            "guide_intro": "先确认查询范围，再按步骤完成实操。",
        }
    )

    message = module.TaskAgent(
        "trace-practice-guide-contextualized",
        llm_call=llm,
    ).generate(
        "T-03",
        student_profile=_planner_profile(),
        learning_report_summary="当前需巩固三道工序与传导关系。",
    )

    content = message["payload"]["content"]
    assert len(llm.calls) == 1
    assert llm.calls[0]["json_schema"] == module.TASK_CONTEXT_OUTPUT_SCHEMA
    assert content["contextualize_fallback"] is False
    assert content["guide_intro"] == "先确认查询范围，再按步骤完成实操。"
    assert content["guide_intro"] in content["guide_md"]
    assert len(content["guide_steps"]) >= 3
    assert content["completion_criteria"]


@pytest.mark.parametrize("misconception", ["M-01", "M-02", "M-03", "M-04", "M-05"])
def test_counter_evidence_is_one_approved_probe_question(
    misconception: str, tmp_path: Path
) -> None:
    module = _task_module()
    draft = module.TaskAgent(f"trace-{misconception.lower()}").counter_evidence(
        misconception, wrong_attempts=2
    )
    content = draft["payload"]["content"]

    assert draft["agent"] == "task"
    assert draft["role"] == "probe"
    assert draft["payload"]["type"] == "quiz_set"
    assert content["event"] == "counter_evidence_ready"
    assert content["misconception"] == misconception
    assert len(draft["probe"]["questions"]) == 1
    assert draft["probe"]["questions"] == [content["question"]]
    assert draft["probe"] == {
        "wrong_attempts": 2,
        "questions": [content["question"]],
        "target_misconception": misconception,
    }
    assert len(draft["evidence"]) == len(
        module.load_task_catalog().counter_evidence[misconception].standard_sqls
    )
    assert draft["claims"] == []
    assert MessageBus(tmp_path / "traces").send(draft).accepted


@pytest.mark.parametrize(
    ("method", "identifier", "wrong_attempts", "match"),
    [
        ("generate", "T-00", None, "template_id"),
        ("counter_evidence", "M-00", 1, "misconception"),
        ("counter_evidence", "M-01", 0, "wrong_attempts"),
        ("counter_evidence", "M-01", True, "wrong_attempts"),
    ],
)
def test_invalid_task_inputs_fail_before_message_creation(
    method: str, identifier: str, wrong_attempts: int | None, match: str
) -> None:
    agent = _task_module().TaskAgent("trace-invalid-task")

    with pytest.raises(ValueError, match=match):
        if method == "generate":
            agent.generate(identifier)
        else:
            agent.counter_evidence(identifier, wrong_attempts=wrong_attempts)


def test_missing_parameter_falls_back_to_standard_stem_and_keeps_answer_key() -> None:
    module = _task_module()
    llm = SpyLLM(
        {
            "contextualized_stem": "作为计划员，请核对H2601在2025-05的计划量与实际量。",
            "guide_intro": "先明确查询范围，再开始实训。",
        }
    )
    catalog = module.load_task_catalog()

    message = module.TaskAgent(
        "trace-context-fallback", catalog=catalog, llm_call=llm
    ).generate(
        "T-01",
        student_profile=_planner_profile(),
        learning_report_summary="当前需巩固计划量与实际量口径。",
    )
    content = message["payload"]["content"]
    standard_stem = "查询H26012025-05YCL的计划量与实际量"

    assert content["standard_stem"] == standard_stem
    assert content["contextualized_stem"] == standard_stem
    assert content["question"] == standard_stem
    assert content["questions"] == [{"id": "T-01", "prompt": standard_stem}]
    assert content["guide_intro"] == ""
    assert content["contextualize_fallback"] is True
    answer_key = json.loads(message["evidence"][0]["quote"])
    assert answer_key["standard_sql"] == catalog.templates["T-01"].standard_sql
    assert message["token_usage"] == {
        "prompt_tokens": 90,
        "completion_tokens": 30,
        "total_tokens": 120,
    }


def test_contextualized_task_changes_only_the_display_layer(tmp_path: Path) -> None:
    module = _task_module()
    llm = SpyLLM(
        {
            "contextualized_stem": (
                "作为新入职生产计划员，请查询H2601在2025-05期间YCL的计划量与实际量。"
            ),
            "guide_intro": "先圈定船号、月份和工序，再开始实训。",
        }
    )
    catalog = module.load_task_catalog()

    message = module.TaskAgent(
        "trace-context-success", catalog=catalog, llm_call=llm
    ).generate(
        "T-01",
        student_profile=_planner_profile(),
        learning_report_summary="当前需巩固计划量与实际量口径。",
    )
    content = message["payload"]["content"]

    assert content["standard_stem"] == "查询H26012025-05YCL的计划量与实际量"
    assert content["question"] == content["contextualized_stem"]
    assert content["questions"] == [
        {"id": "T-01", "prompt": content["contextualized_stem"]}
    ]
    assert content["guide_intro"] == "先圈定船号、月份和工序，再开始实训。"
    assert content["contextualize_fallback"] is False
    assert llm.calls[0]["model"] == "qwen3-235b-a22b"
    assert llm.calls[0]["temperature"] == 0.5
    assert llm.calls[0]["system"] == EXPECTED_TASK_PROMPT.replace(
        "{ship}{month}{process}", "H2601、2025-05、YCL"
    )
    assert "重讲工序与口径、少讲SQL" in llm.calls[0]["user"]
    assert json.loads(message["evidence"][0]["quote"])["standard_sql"] == (
        catalog.templates["T-01"].standard_sql
    )
    assert MessageBus(tmp_path).send(message).accepted


@pytest.mark.parametrize(
    ("contextualized_stem", "fallback_reason"),
    (
        (
            "查询H2601在2025-05的YCL计划量与实际量，为后续完成率计算提供支持。",
            "metric_semantic_mismatch",
        ),
        (
            "查询H2601在2025-05批次的YCL计划量与实际量。",
            "time_semantic_mismatch",
        ),
        (
            "查询H2601在2025-05的YCL计划量。",
            "metric_semantic_mismatch",
        ),
    ),
)
def test_contextualization_cannot_add_metrics_or_turn_month_into_batch(
    contextualized_stem: str,
    fallback_reason: str,
) -> None:
    llm = SpyLLM(
        {
            "contextualized_stem": contextualized_stem,
            "guide_intro": "先确认查询边界。",
        }
    )

    message = _task_module().TaskAgent(
        "trace-context-semantic-guard", llm_call=llm
    ).generate(
        "T-01",
        student_profile=_planner_profile(),
        learning_report_summary="当前需巩固计划量与实际量口径。",
    )
    content = message["payload"]["content"]

    assert content["contextualized_stem"] == content["standard_stem"]
    assert content["contextualize_fallback"] is True
    assert content["contextualize_fallback_reason"] == fallback_reason


def test_task_prompt_names_the_required_verbatim_parameter_values() -> None:
    module = _task_module()
    llm = SpyLLM(
        {
            "contextualized_stem": "查询H2601在2025-05的YCL计划量与实际量。",
            "guide_intro": "先确认查询边界。",
        }
    )

    module.TaskAgent("trace-parameter-prompt", llm_call=llm).generate(
        "T-01",
        student_profile=_planner_profile(),
        learning_report_summary="当前需巩固计划量与实际量口径。",
    )

    assert '"standard_stem":"查询H26012025-05YCL的计划量与实际量"' in (
        llm.calls[0]["user"]
    )
    assert '"parameter_values":["H2601","2025-05","YCL"]' in (
        llm.calls[0]["user"]
    )
    assert "{ship}" not in llm.calls[0]["system"]
    assert "H2601、2025-05、YCL等参数值原样保留" in llm.calls[0]["system"]


def test_counter_evidence_freezes_machine_question_but_contextualizes_guide() -> None:
    module = _task_module()
    drifted_stem = (
        "站在计划员复核岗位，分别查H2601在2025-05的YCL计划量与实际完成量，"
        "再对照原结论，以便定位责任单元。"
    )
    llm = SpyLLM(
        {
            "contextualized_stem": drifted_stem,
            "guide_intro": "先用两种数量口径核对你刚才的判断。",
        }
    )
    catalog = module.load_task_catalog()

    message = module.TaskAgent(
        "trace-counter-context", catalog=catalog, llm_call=llm
    ).counter_evidence(
        "M-01",
        wrong_attempts=2,
        student_profile=_planner_profile(),
        learning_report_summary="当前把计划量误当成实际完成量。",
    )
    content = message["payload"]["content"]

    standard_stem = (
        "分别查H26012025-05YCL的计划量与实际完成量，对照你的结论"
    )
    assert content["standard_stem"] == standard_stem
    assert content["contextualized_stem"] == standard_stem
    assert content["question"] == standard_stem
    assert message["probe"]["questions"] == [standard_stem]
    assert drifted_stem not in json.dumps(message, ensure_ascii=False)
    assert content["guide_intro"] == "先用两种数量口径核对你刚才的判断。"
    assert content["contextualize_fallback"] is False
    assert message["probe"]["target_misconception"] == "M-01"
    actual_sqls = [
        json.loads(item["quote"])["standard_sql"] for item in message["evidence"]
    ]
    assert actual_sqls == list(catalog.counter_evidence["M-01"].standard_sqls)


@pytest.mark.parametrize("misconception", ["M-01", "M-02", "M-03", "M-04", "M-05"])
def test_every_counter_mapping_uses_its_standard_stem_as_the_machine_question(
    misconception: str,
) -> None:
    module = _task_module()
    catalog = module.load_task_catalog()
    entry = catalog.counter_evidence[misconception]
    standard_stem = module._render(entry.question_template, catalog.demo_parameters)
    drifted_stem = f"{standard_stem}，以便定位责任单元和车间。"
    llm = SpyLLM(
        {
            "contextualized_stem": drifted_stem,
            "guide_intro": "按你的岗位经验先核对查询边界。",
        }
    )

    message = module.TaskAgent(
        f"trace-counter-freeze-{misconception.lower()}",
        catalog=catalog,
        llm_call=llm,
    ).counter_evidence(
        misconception,
        student_profile=_planner_profile(),
        learning_report_summary="需要用反证题复核当前判断。",
    )
    content = message["payload"]["content"]

    assert (
        content["standard_stem"],
        content["contextualized_stem"],
        content["question"],
        message["probe"]["questions"][0],
    ) == (standard_stem,) * 4
    assert drifted_stem not in json.dumps(message, ensure_ascii=False)
    assert content["guide_intro"] == "按你的岗位经验先核对查询边界。"
    assert content["contextualize_fallback"] is False
    assert message["student_profile_ref"] == "planner_new"
    assert message["model"] == "qwen3-235b-a22b"
    assert message["token_usage"] == {
        "prompt_tokens": 90,
        "completion_tokens": 30,
        "total_tokens": 120,
    }
    assert [json.loads(item["quote"]) for item in message["evidence"]] == [
        {
            "standard_sql": standard_sql,
            "expected_rows": list(entry.expected_results[index]),
            "expected_points": list(entry.expected_points),
        }
        for index, standard_sql in enumerate(entry.standard_sqls)
    ]


@pytest.mark.parametrize(
    ("llm_call", "student_profile", "learning_report_summary", "fallback_reason"),
    (
        (None, _planner_profile(), "需要复核。", "llm_disabled"),
        (
            SpyLLM(
                {
                    "contextualized_stem": "分别查H26012025-05YCL的计划量与实际完成量，对照你的结论",
                    "guide_intro": "先复核。",
                }
            ),
            None,
            None,
            "missing_context",
        ),
        (
            lambda **_: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
            _planner_profile(),
            "需要复核。",
            "llm_failure",
        ),
        (
            SpyLLM({"contextualized_stem": "", "guide_intro": ""}),
            _planner_profile(),
            "需要复核。",
            "invalid_output",
        ),
    ),
)
def test_counter_machine_question_stays_frozen_on_contextualization_fallbacks(
    llm_call: Any,
    student_profile: dict[str, str] | None,
    learning_report_summary: str | None,
    fallback_reason: str,
) -> None:
    message = _task_module().TaskAgent(
        f"trace-counter-{fallback_reason}", llm_call=llm_call
    ).counter_evidence(
        "M-01",
        student_profile=student_profile,
        learning_report_summary=learning_report_summary,
    )
    content = message["payload"]["content"]

    assert (
        content["standard_stem"],
        content["contextualized_stem"],
        content["question"],
        message["probe"]["questions"][0],
    ) == (content["standard_stem"],) * 4
    assert content["contextualize_fallback"] is True
    assert content["contextualize_fallback_reason"] == fallback_reason


def test_task_llm_failure_uses_standard_stem_with_audited_usage() -> None:
    module = _task_module()

    def fail(**_: Any) -> LLMResult:
        raise RuntimeError("provider unavailable")

    message = module.TaskAgent(
        "trace-task-llm-failure", llm_call=fail
    ).generate(
        "T-01",
        student_profile=_planner_profile(),
        learning_report_summary="当前需巩固计划量与实际量口径。",
    )
    content = message["payload"]["content"]

    assert content["contextualized_stem"] == content["standard_stem"]
    assert content["guide_intro"] == ""
    assert content["contextualize_fallback"] is True
    assert content["contextualize_fallback_reason"] == "llm_failure"
    assert message["model"] == "qwen3-235b-a22b"
    assert message["token_usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def test_task_constructor_exposes_optional_llm_dependency() -> None:
    module = _task_module()
    signature = inspect.signature(module.TaskAgent)

    assert "llm_call" in signature.parameters
    assert signature.parameters["llm_call"].default is None


def test_live_p4_5_three_profile_tasks_and_one_counter_preserve_parameters() -> None:
    module = _task_module()
    from agents.diagnosis_agent import load_profiles

    profiles = load_profiles()
    catalog = module.load_task_catalog()
    contextualized: dict[str, str] = {}
    fallback_count = 0
    for profile_id, profile in profiles.items():
        message = module.TaskAgent(
            f"trace-live-p4-5-task-{profile_id}",
            catalog=catalog,
            llm_call=call_llm,
        ).generate(
            "T-01",
            student_profile=profile,
            learning_report_summary="岗前测评3/5，当前优先巩固计划量与实际量口径。",
        )
        content = message["payload"]["content"]

        assert all(
            value in content["contextualized_stem"]
            for value in ("H2601", "2025-05", "YCL")
        )
        if content["contextualize_fallback"]:
            fallback_count += 1
            assert content["contextualized_stem"] == content["standard_stem"]
        contextualized[profile_id] = content["contextualized_stem"]

    counter = module.TaskAgent(
        "trace-live-p4-5-counter-line-leader",
        catalog=catalog,
        llm_call=call_llm,
    ).counter_evidence(
        "M-01",
        student_profile=profiles["line_leader"],
        learning_report_summary="已连续混淆计划量与实际完成量，需要反证追问。",
    )
    counter_content = counter["payload"]["content"]
    assert all(
        value in counter_content["contextualized_stem"]
        for value in ("H2601", "2025-05", "YCL")
    )
    if counter_content["contextualize_fallback"]:
        fallback_count += 1
        assert (
            counter_content["contextualized_stem"]
            == counter_content["standard_stem"]
        )
    assert counter["probe"]["target_misconception"] == "M-01"
    actual_sqls = [
        json.loads(item["quote"])["standard_sql"] for item in counter["evidence"]
    ]
    assert actual_sqls == list(catalog.counter_evidence["M-01"].standard_sqls)
    print(
        "P4.5 task live stems:",
        {
            **contextualized,
            "counter_line_leader": counter_content["contextualized_stem"],
            "fallback_rate": f"{fallback_count}/4",
        },
    )


def test_live_catalog_sqls_match_every_stored_result() -> None:
    catalog = _task_module().load_task_catalog()
    executor = ReadOnlyExecutor(DatabaseSettings.from_environment())

    for template_id, entry in catalog.templates.items():
        decision = validate_and_rewrite(entry.standard_sql)
        assert decision.allowed and decision.executed_sql is not None, template_id
        result = executor.execute(decision.executed_sql)
        assert _normalized_rows(result.rows) == list(entry.expected_rows), template_id

    for misconception, entry in catalog.counter_evidence.items():
        assert len(entry.standard_sqls) == len(entry.expected_results)
        for index, (sql, expected_rows) in enumerate(
            zip(entry.standard_sqls, entry.expected_results, strict=True)
        ):
            decision = validate_and_rewrite(sql)
            assert decision.allowed and decision.executed_sql is not None, (
                misconception,
                index,
            )
            result = executor.execute(decision.executed_sql)
            assert _normalized_rows(result.rows) == list(expected_rows), (
                misconception,
                index,
            )
