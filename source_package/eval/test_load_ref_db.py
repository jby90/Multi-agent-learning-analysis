from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from data.load_ref_db import (
    LoaderError,
    PACKAGED_SELF_CHECK_SQL,
    PACKAGED_VALIDATION_MARKDOWN,
    TARGET_DATABASE,
    _resolve_mysql_binary,
    compare_results,
    main as load_ref_db_main,
    parse_expected_validation,
    rewrite_database,
    split_self_check_queries,
    validate_d_drive_datadir,
)


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "data" / "比赛数据包"
IMPORT_SQL = PACKAGE / "import_mysql.sql"


def test_rewrite_database_targets_only_ref_database() -> None:
    rewritten = rewrite_database(IMPORT_SQL.read_text(encoding="utf-8"))

    assert TARGET_DATABASE == "ref_contest_db"
    assert "CREATE DATABASE IF NOT EXISTS ref_contest_db" in rewritten
    assert "USE ref_contest_db" in rewritten
    assert re.search(r"(?<!ref_)\bcontest_db\b", rewritten) is None


def test_packaged_expectations_are_parsed() -> None:
    expected = parse_expected_validation(PACKAGED_VALIDATION_MARKDOWN)

    assert expected.fact_rows == 6516
    assert expected.ship_count == 6
    assert expected.min_date.isoformat() == "2025-02-01"
    assert expected.max_date.isoformat() == "2025-07-31"
    assert expected.min_normal_rate == Decimal("0.8800")
    assert expected.max_normal_rate == Decimal("1.0300")
    assert expected.over_complete_combinations == 18
    assert expected.anomaly_rates["源头异常"].rate == Decimal("0.6236")
    assert expected.anomaly_rates["滞后传导"].process_code == "ZZTP"
    assert expected.other_ship_min_rate == Decimal("0.8924")
    assert expected.other_ship_max_rate == Decimal("0.9779")


def test_self_check_is_split_into_seven_queries_for_ref_database() -> None:
    queries = split_self_check_queries(
        rewrite_database(PACKAGED_SELF_CHECK_SQL)
    )

    assert len(queries) == 7
    assert all("USE " not in query.upper() for query in queries)


def _matching_result_sets() -> list[list[dict[str, str]]]:
    return [
        [
            {"month_label": "2025-05", "process_code": "YCL", "complete_rate": "0.6236"},
            {"month_label": "2025-06", "process_code": "ZZTP", "complete_rate": "0.7545"},
            {"month_label": "2025-07", "process_code": "AZTP", "complete_rate": "0.8501"},
        ],
        [
            {"ship_no": "H2601", "complete_rate": "0.6236"},
            {"ship_no": "H2602", "complete_rate": "0.8924"},
            {"ship_no": "H2603", "complete_rate": "0.9100"},
            {"ship_no": "H2604", "complete_rate": "0.9400"},
            {"ship_no": "H2605", "complete_rate": "0.9600"},
            {"ship_no": "H2606", "complete_rate": "0.9779"},
        ],
        [{"unexpected_high_risk_rows": "0"}],
        [
            {
                "fact_rows": "6516",
                "ship_count": "6",
                "min_date": "2025-02-01",
                "max_date": "2025-07-31",
            }
        ],
        [{"bad_complete_rate": "0", "bad_deviation_rate": "0"}],
        [{"bad_quality_split": "0"}],
        [
            {
                "min_normal_rate": "0.8800",
                "max_normal_rate": "1.0300",
                "over_complete_combinations": "18",
            }
        ],
    ]


def test_matching_self_check_results_pass_every_documented_expectation() -> None:
    expected = parse_expected_validation(PACKAGED_VALIDATION_MARKDOWN)

    assert compare_results(expected, _matching_result_sets()) == []


def test_mismatch_reports_the_specific_check_name() -> None:
    expected = parse_expected_validation(PACKAGED_VALIDATION_MARKDOWN)
    result_sets = _matching_result_sets()
    result_sets[3][0]["fact_rows"] = "1"

    errors = compare_results(expected, result_sets)

    assert any("fact_rows" in error and "expected=6516" in error for error in errors)


def test_missing_intermediate_ship_in_ranking_is_rejected() -> None:
    expected = parse_expected_validation(PACKAGED_VALIDATION_MARKDOWN)
    result_sets = _matching_result_sets()
    del result_sets[1][2]

    errors = compare_results(expected, result_sets)

    assert any("May YCL ranking row_count" in error for error in errors)


def test_mysql_datadir_is_not_restricted_to_a_drive() -> None:
    for datadir in (
        "C:\\MySQL\\Data\\",
        "D:\\MySQL\\Data\\",
        "E:\\MySQL\\Data\\",
        "/var/lib/mysql",
    ):
        assert validate_d_drive_datadir(datadir) == []


def test_mysql_client_accepts_resolved_path_on_any_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("data.load_ref_db.shutil.which", lambda _: "E:\\MySQL\\bin\\mysql.exe")
    assert _resolve_mysql_binary("mysql") == "E:\\MySQL\\bin\\mysql.exe"

    monkeypatch.setattr("data.load_ref_db.shutil.which", lambda _: "C:\\MySQL\\bin\\mysql.exe")
    assert _resolve_mysql_binary("mysql") == "C:\\MySQL\\bin\\mysql.exe"


def test_mysql_client_still_errors_when_binary_cannot_be_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("data.load_ref_db.shutil.which", lambda _: None)
    monkeypatch.setattr("data.load_ref_db.Path.is_file", lambda _: False)

    with pytest.raises(LoaderError, match="MYSQL_BIN"):
        _resolve_mysql_binary("missing-mysql")


def test_loader_success_reports_packaged_self_check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("data.load_ref_db.load_and_self_check", lambda _: [])

    assert load_ref_db_main() == 0
    assert "内置自检" in capsys.readouterr().out
