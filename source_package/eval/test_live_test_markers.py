from __future__ import annotations

from importlib import import_module

import pytest


EXTERNAL_TESTS = (
    ("eval.test_debate_runtime", "test_live_manual_false_r02_runs_real_235b_32b_and_persists_trace"),
    ("eval.test_diagnosis_agent", "test_live_p4_5_three_profile_narratives_are_grounded_and_distinct"),
    ("eval.test_knowledge_agent", "test_live_knowledge_matrix_and_refusal"),
    ("eval.test_review_agent", "test_live_r02_three_non_entailing_rejected_and_three_entailing_accepted"),
    ("eval.test_review_agent", "test_live_r03_three_explicit_cross_level_mismatches_rejected"),
    ("eval.test_sandbox", "test_live_ref_reader_has_only_ref_select_permission"),
    ("eval.test_task_agent", "test_live_p4_5_three_profile_tasks_and_one_counter_preserve_parameters"),
    ("eval.test_task_agent", "test_live_catalog_sqls_match_every_stored_result"),
    ("eval.test_text2sql_regression", "test_all_standard_sql_matches_stored_live_results"),
    ("eval.test_text2sql_regression", "test_live_text2sql_regression_three_runs"),
    ("eval.test_verification_prompt", "test_all_fifteen_few_shots_pass_sandbox_and_match_live_database"),
)


@pytest.mark.parametrize(("module_name", "function_name"), EXTERNAL_TESTS)
def test_external_test_is_marked_live(module_name: str, function_name: str) -> None:
    function = getattr(import_module(module_name), function_name)
    markers = getattr(function, "pytestmark", ())

    assert any(marker.name == "live" for marker in markers), (
        module_name,
        function_name,
    )
