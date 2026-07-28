from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from agents.domain_config import load_domain_config
from agents.prompts.build_verification_prompt import build_prompt_catalog
from agents.sandbox import validate_and_rewrite
from agents.task_agent import load_task_catalog
from eval.first_segment_oracle import CsvOracle, load_first_segment_cases


ROOT = Path(__file__).resolve().parents[1]
CASE_PATH = ROOT / "eval" / "cases" / "first_segment_50.jsonl"


def test_first_segment_package_assets_form_closed_component_catalogs() -> None:
    domain = load_domain_config("first_segment")

    prompts = build_prompt_catalog(domain_config=domain)
    tasks = load_task_catalog(domain_config=domain)

    assert "JSON" in prompts.router_prompt
    assert len(prompts.few_shots) == 21
    assert len(tasks.templates) == 10
    assert tuple(tasks.templates) == tasks.template_ids


def test_matrix_has_ten_five_case_sections_and_frozen_split() -> None:
    cases = load_first_segment_cases(CASE_PATH)

    assert len(cases) == 50
    assert {case.section for case in cases} == {
        f"SEC-{index:03d}" for index in range(1, 11)
    }
    assert sum(case.split == "calibration" for case in cases) == 20
    assert sum(case.split == "holdout" for case in cases) == 30
    for section in {case.section for case in cases}:
        members = [case for case in cases if case.section == section]
        assert len(members) == 5
        assert sum(case.split == "calibration" for case in members) == 2


def test_matrix_ids_sources_and_answer_keys_are_well_formed() -> None:
    cases = load_first_segment_cases(CASE_PATH)

    assert len({case.case_id for case in cases}) == 50
    for case in cases:
        assert case.answer_key.formula is None
        assert case.sources
        for source in case.sources:
            assert re.fullmatch(r"[0-9a-f]{64}", source.sha256)
            assert "准确率测试集" not in source.file
            assert "泛化性测试集" not in source.file


def test_loader_rejects_duplicate_ids_and_invented_formula(tmp_path: Path) -> None:
    raw = json.loads(CASE_PATH.read_text(encoding="utf-8").splitlines()[0])
    raw["answer_key"]["formula"] = "ACTUALNUM / PLANNUM"
    duplicate_path = tmp_path / "bad.jsonl"
    duplicate_path.write_text(
        "\n".join(json.dumps(raw, ensure_ascii=False) for _ in range(50)),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="answer_key fields"):
        load_first_segment_cases(duplicate_path)


def test_oracle_preserves_null_and_rejects_non_package_tables(
    first_segment_csv_root: Path,
) -> None:
    domain = load_domain_config("first_segment")
    oracle = CsvOracle(first_segment_csv_root)

    result = oracle.execute(
        "SELECT ndjhs FROM dwr_mps_ppdataprocess_wide "
        "WHERE TARGETTYPE='每月情况' ORDER BY oid LIMIT 1",
        domain,
    )
    assert result.columns == ("ndjhs",)
    assert result.rows == ((None,),)

    production = load_domain_config("production_progress")
    with pytest.raises(ValueError, match="CSV source is missing"):
        oracle.execute("SELECT progress_id FROM fact_production_progress", production)


@pytest.mark.parametrize(
    ("sql", "columns", "rows"),
    [
        (
            "SELECT ACTUALNUM, FINISHRATE FROM dwr_mps_ttyear_wide "
            "WHERE RECORDDATE='2025-07-10' AND TARTYPE='物量态势' AND PROCESS='切割'",
            ("ACTUALNUM", "FINISHRATE"),
            (("1369", "0.4231"),),
        ),
        (
            "SELECT ndjhs, JHYLJ, NDSJS FROM dwr_mps_ppdataprocess_wide "
            "WHERE RECORDDATE='2025-07-10' AND TARGETTYPE='年度统计' "
            "AND DEPT='部门01' AND PROCESS='切割'",
            ("ndjhs", "JHYLJ", "NDSJS"),
            (("1717", "850", "686"),),
        ),
        (
            "SELECT QG_NUM FROM dwr_mps_ppdatalast_wide "
            "WHERE RECORDDATE='2025-07-09' AND SHIPNO='H2705' AND M_YJTYPE='实际'",
            ("QG_NUM",),
            (("210",),),
        ),
    ],
)
def test_oracle_known_fixture_anchors(
    first_segment_csv_root: Path,
    sql: str,
    columns: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
) -> None:
    result = CsvOracle(first_segment_csv_root).execute(
        sql, load_domain_config("first_segment")
    )

    assert result.columns == columns
    assert result.rows == rows


def test_all_fifty_answer_keys_are_sandbox_approved_and_deidentified() -> None:
    domain = load_domain_config("first_segment")
    forbidden_departments = (
        "".join(("制造", "一部")),
        "".join(("制造", "二部")),
        "".join(("涂装", "部")),
    )

    for case in load_first_segment_cases(CASE_PATH):
        decision = validate_and_rewrite(case.answer_key.expected_sql, domain_config=domain)
        assert decision.allowed, case.case_id
        assert decision.executed_sql is not None, case.case_id
        assert not any(
            department in case.answer_key.expected_sql
            for department in forbidden_departments
        )
