from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import pymysql
import pytest

from agents.domain_config import load_domain_config
from agents.sandbox import (
    DatabaseSettings,
    QueryTimeoutError,
    ReadOnlyExecutor,
    validate_and_rewrite,
)


def pilot_domain_config():
    return replace(
        load_domain_config("production_progress"),
        domain_id="pilot",
        database="pilot_db",
        table_columns=MappingProxyType(
            {"fact_pilot": frozenset({"oid", "metric_value"})}
        ),
    )


def test_injected_domain_controls_tables_columns_and_database_qualifier() -> None:
    domain = pilot_domain_config()

    allowed = validate_and_rewrite(
        "SELECT metric_value FROM pilot_db.fact_pilot", domain_config=domain
    )
    production_table = validate_and_rewrite(
        "SELECT ship_no FROM dim_ship", domain_config=domain
    )
    foreign_database = validate_and_rewrite(
        "SELECT metric_value FROM other_db.fact_pilot", domain_config=domain
    )

    assert allowed.allowed
    assert production_table.rule_id == "S-03"
    assert foreign_database.rule_id == "S-03"


@pytest.mark.parametrize(
    ("sql", "rule_id"),
    (
        ("DELETE FROM fact_production_progress", "S-01"),
        ("SELECT 1; SELECT 2", "S-02"),
        ("SELECT user FROM mysql.user", "S-03"),
        ("SELECT ship_no FROM contest_db.fact_production_progress", "S-03"),
        ("SELECT secret FROM fact_production_progress", "S-04"),
        ("SELECT * FROM fact_production_progress", "S-04"),
        (
            "SELECT process_code FROM fact_production_progress f "
            "JOIN dim_process p ON f.process_code=p.process_code",
            "S-04",
        ),
        ("SELECT SLEEP(1)", "S-05"),
        ("SELECT BENCHMARK(1,MD5('x'))", "S-05"),
        ("SELECT LOAD_FILE('D:/x')", "S-05"),
        ("SELECT DATABASE()", "S-05"),
        ("SELECT VERSION()", "S-05"),
        ("SELECT @@version", "S-05"),
        ("SELECT @private_value", "S-05"),
        (
            "SELECT ship_no INTO OUTFILE 'D:/x' FROM dim_ship",
            "S-05",
        ),
        ("SELECT ship_no FROM dim_ship FOR UPDATE", "S-05"),
        ("SELECT ship_no FROM dim_ship LOCK IN SHARE MODE", "S-05"),
        ("SELECT ship_no FROM dim_ship -- hidden", "S-05"),
        ("SELECT RAND()", "S-05"),
        ("SELECT UUID()", "S-05"),
        ("SELECT ship_no FROM dim_ship LIMIT 10 OFFSET 1", "S-05"),
        ("SELECT ship_no FROM dim_ship LIMIT @row_count", "S-05"),
        ("SELECT FROM", "S-07"),
    ),
)
def test_rejections_return_exact_rule(sql: str, rule_id: str) -> None:
    decision = validate_and_rewrite(sql)

    assert not decision.allowed
    assert decision.rule_id == rule_id
    assert decision.executed_sql is None
    assert decision.generated_sql == sql
    assert decision.reason


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT GET_LOCK('p2',1)",
        "SELECT RELEASE_LOCK('p2')",
        "SELECT IS_FREE_LOCK('p2')",
        "SELECT IS_USED_LOCK('p2')",
        "SELECT MASTER_POS_WAIT('binlog.000001',1,1)",
        "SELECT SOURCE_POS_WAIT('binlog.000001',1,1)",
        "SELECT WAIT_FOR_EXECUTED_GTID_SET('uuid:1',1)",
        "SELECT LAST_INSERT_ID(7)",
        "SELECT FOUND_ROWS()",
        "SELECT ROW_COUNT()",
    ),
)
def test_s05_rejects_lock_wait_and_session_side_effect_functions(sql: str) -> None:
    decision = validate_and_rewrite(sql)

    assert not decision.allowed
    assert decision.rule_id == "S-05"
    assert decision.executed_sql is None


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT 1",
        "SELECT ship_no FROM dim_ship",
        "SELECT ref_contest_db.dim_ship.ship_no FROM ref_contest_db.dim_ship",
        "SELECT f.ship_no FROM fact_production_progress AS f",
        (
            "SELECT f.ship_no, p.process_name FROM fact_production_progress f "
            "JOIN dim_process p ON f.process_code=p.process_code"
        ),
        "SELECT COUNT(*) AS row_count FROM dim_ship",
        (
            "SELECT DATE_FORMAT(period_date,'%Y-%m') AS month_label, "
            "ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate "
            "FROM fact_production_progress GROUP BY month_label "
            "ORDER BY complete_rate"
        ),
        (
            "WITH monthly AS ("
            "SELECT ship_no, SUM(plan_qty) AS plan_qty "
            "FROM fact_production_progress GROUP BY ship_no"
            ") SELECT ship_no, plan_qty FROM monthly ORDER BY ship_no"
        ),
        "SELECT ship_no FROM dim_ship LIMIT 20",
        "SELECT ship_no FROM dim_ship LIMIT 200",
    ),
)
def test_allowed_queries_pass_all_rules(sql: str) -> None:
    decision = validate_and_rewrite(sql)

    assert decision.allowed, (decision.rule_id, decision.reason)
    assert decision.rule_id is None
    assert decision.executed_sql


def test_s06_appends_preserves_and_caps_limit() -> None:
    added = validate_and_rewrite("SELECT ship_no FROM dim_ship")
    preserved = validate_and_rewrite("SELECT ship_no FROM dim_ship LIMIT 20")
    capped = validate_and_rewrite("SELECT ship_no FROM dim_ship LIMIT 999")

    assert added.allowed and added.executed_sql.endswith("LIMIT 200")
    assert preserved.allowed and preserved.executed_sql.endswith("LIMIT 20")
    assert capped.allowed and capped.executed_sql.endswith("LIMIT 200")


def test_s02_takes_precedence_over_later_statement_content() -> None:
    decision = validate_and_rewrite(
        "SELECT ship_no FROM dim_ship; DELETE FROM fact_production_progress"
    )

    assert decision.rule_id == "S-02"


def test_s07_takes_precedence_when_the_input_cannot_be_parsed() -> None:
    decision = validate_and_rewrite("SELECT 'unterminated")

    assert decision.rule_id == "S-07"


def test_count_star_is_allowed_but_result_star_is_rejected() -> None:
    count = validate_and_rewrite("SELECT COUNT(*) AS row_count FROM dim_ship")
    rows = validate_and_rewrite("SELECT * FROM dim_ship")

    assert count.allowed
    assert rows.rule_id == "S-04"


@dataclass
class FakeCursor:
    fail_query: BaseException | None = None
    executed: list[str] = field(default_factory=list)
    description: tuple[tuple[Any, ...], ...] = (
        ("ship_no", None, None, None, None, None, None),
        ("complete_rate", None, None, None, None, None, None),
    )

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, sql: str) -> int:
        self.executed.append(sql)
        if len(self.executed) > 1 and self.fail_query is not None:
            raise self.fail_query
        return 1

    def fetchall(self) -> list[dict[str, Any]]:
        return [{"ship_no": "H2601", "complete_rate": Decimal("0.6236")}]


@dataclass
class FakeConnection:
    cursor_instance: FakeCursor
    closed: bool = False

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True


class FakeConnect:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.kwargs: dict[str, Any] = {}

    def __call__(self, **kwargs: Any) -> FakeConnection:
        self.kwargs = kwargs
        return self.connection


def reader_settings() -> DatabaseSettings:
    return DatabaseSettings(host="127.0.0.1", port=3306, password="test-password")


def test_executor_sets_database_account_and_dual_timeout() -> None:
    connection = FakeConnection(FakeCursor())
    connect = FakeConnect(connection)
    executor = ReadOnlyExecutor(settings=reader_settings(), connect=connect)

    result = executor.execute("SELECT ship_no, complete_rate FROM dim_ship LIMIT 200")

    assert connect.kwargs["read_timeout"] == 5
    assert connect.kwargs["write_timeout"] == 5
    assert connect.kwargs["connect_timeout"] == 5
    assert connect.kwargs["database"] == "ref_contest_db"
    assert connect.kwargs["user"] == "ref_reader"
    assert connect.connection.cursor_instance.executed == [
        "SET SESSION MAX_EXECUTION_TIME=5000",
        "SELECT ship_no, complete_rate FROM dim_ship LIMIT 200",
    ]
    assert result.columns == ("ship_no", "complete_rate")
    assert result.rows == (
        {"ship_no": "H2601", "complete_rate": Decimal("0.6236")},
    )
    assert result.elapsed_ms >= 0
    assert connection.closed


def test_executor_accepts_an_explicit_package_database() -> None:
    connection = FakeConnection(FakeCursor())
    connect = FakeConnect(connection)
    executor = ReadOnlyExecutor(
        settings=reader_settings(), connect=connect, database="pilot_db"
    )

    executor.execute("SELECT metric_value FROM fact_pilot LIMIT 200")

    assert connect.kwargs["database"] == "pilot_db"


@pytest.mark.parametrize("error_code", (3024, 2013))
def test_executor_maps_server_and_client_timeouts(error_code: int) -> None:
    connection = FakeConnection(
        FakeCursor(fail_query=pymysql.err.OperationalError(error_code, "timeout"))
    )
    executor = ReadOnlyExecutor(
        settings=reader_settings(), connect=FakeConnect(connection)
    )

    with pytest.raises(QueryTimeoutError):
        executor.execute("SELECT ship_no FROM dim_ship LIMIT 200")

    assert connection.closed


def test_database_settings_requires_reader_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REF_READER_PASSWORD", raising=False)

    with pytest.raises(ValueError, match="REF_READER_PASSWORD"):
        DatabaseSettings.from_environment()


def test_live_ref_reader_has_only_ref_select_permission() -> None:
    settings = DatabaseSettings.from_environment()
    connection = pymysql.connect(
        host=settings.host,
        port=settings.port,
        user="ref_reader",
        password=settings.password,
        database="ref_contest_db",
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW GRANTS FOR CURRENT_USER")
            grants = [str(row[0]) for row in cursor.fetchall()]
            assert any(
                "GRANT SELECT ON `ref_contest_db`.* TO `ref_reader`@`127.0.0.1`"
                in grant
                for grant in grants
            )
            assert not any("ALL PRIVILEGES" in grant for grant in grants)
            cursor.execute("SELECT COUNT(*) FROM fact_production_progress")
            assert cursor.fetchone()[0] == 6516
            with pytest.raises(pymysql.err.OperationalError):
                cursor.execute("DELETE FROM dim_ship WHERE 1=0")
            with pytest.raises(pymysql.err.OperationalError):
                cursor.execute("SELECT COUNT(*) FROM mysql.user")
    finally:
        connection.close()
