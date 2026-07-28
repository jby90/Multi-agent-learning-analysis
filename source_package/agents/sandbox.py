"""Deterministic MySQL SELECT sandbox and read-only execution boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from time import perf_counter
from typing import Any

import pymysql
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.tokens import Token, TokenType, Tokenizer

from agents.domain_config import DomainConfig, active_domain_config


_DEFAULT_DOMAIN = active_domain_config()
TARGET_DATABASE = _DEFAULT_DOMAIN.database
MAX_ROWS = 200

TABLE_COLUMNS: dict[str, frozenset[str]] = dict(_DEFAULT_DOMAIN.table_columns)

DANGEROUS_FUNCTIONS = frozenset(
    {
        "sleep",
        "benchmark",
        "load_file",
        "database",
        "current_schema",
        "schema",
        "user",
        "current_user",
        "session_user",
        "system_user",
        "version",
        "current_version",
        "connection_id",
        "get_lock",
        "release_lock",
        "is_free_lock",
        "is_used_lock",
        "master_pos_wait",
        "source_pos_wait",
        "wait_for_executed_gtid_set",
        "last_insert_id",
        "found_rows",
        "row_count",
        "rand",
        "random_bytes",
        "uuid",
        "uuid_short",
    }
)


@dataclass(frozen=True, slots=True)
class SandboxDecision:
    allowed: bool
    generated_sql: str
    executed_sql: str | None
    rule_id: str | None
    reason: str | None


class QueryExecutionError(RuntimeError):
    """Raised when the read-only database cannot execute a validated query."""


class QueryTimeoutError(QueryExecutionError):
    """Raised for either the server-side or client-side five-second timeout."""


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    host: str
    port: int
    password: str

    @classmethod
    def from_environment(cls) -> "DatabaseSettings":
        password = os.environ.get("REF_READER_PASSWORD", "")
        if not password:
            raise ValueError("REF_READER_PASSWORD environment variable is required")
        port_text = os.environ.get("MYSQL_PORT", "3306")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError(f"MYSQL_PORT must be an integer, got {port_text!r}") from exc
        return cls(
            host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
            port=port,
            password=password,
        )


@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    elapsed_ms: int


class ReadOnlyExecutor:
    """Execute already-sandboxed SQL through the dedicated reader account."""

    def __init__(
        self,
        settings: DatabaseSettings,
        connect: Any = pymysql.connect,
        database: str | None = None,
    ) -> None:
        self._settings = settings
        self._connect = connect
        self._database = database or active_domain_config().database

    def execute(self, executed_sql: str) -> QueryResult:
        started = perf_counter()
        connection: Any | None = None
        try:
            connection = self._connect(
                host=self._settings.host,
                port=self._settings.port,
                user="ref_reader",
                password=self._settings.password,
                database=self._database,
                connect_timeout=5,
                read_timeout=5,
                write_timeout=5,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
            )
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION MAX_EXECUTION_TIME=5000")
                cursor.execute(executed_sql)
                rows = tuple(dict(row) for row in cursor.fetchall())
                columns = tuple(item[0] for item in (cursor.description or ()))
            return QueryResult(
                columns=columns,
                rows=rows,
                elapsed_ms=round((perf_counter() - started) * 1000),
            )
        except pymysql.err.OperationalError as exc:
            error_code = exc.args[0] if exc.args else None
            if error_code in {1205, 2006, 2013, 3024}:
                raise QueryTimeoutError("query exceeded the five-second limit") from exc
            raise QueryExecutionError(f"read-only query failed with MySQL {error_code}") from exc
        except QueryExecutionError:
            raise
        except Exception as exc:
            raise QueryExecutionError("read-only query failed") from exc
        finally:
            if connection is not None:
                connection.close()


def _reject(sql: str, rule_id: str, reason: str) -> SandboxDecision:
    return SandboxDecision(False, sql, None, rule_id, reason)


def _has_into_file_tokens(tokens: list[Token]) -> bool:
    for index, token in enumerate(tokens[:-1]):
        if token.token_type is TokenType.INTO:
            following = tokens[index + 1].text.casefold()
            if following in {"outfile", "dumpfile"}:
                return True
    return False


def _cte_names(statement: exp.Select) -> frozenset[str]:
    return frozenset(cte.alias_or_name.casefold() for cte in statement.find_all(exp.CTE))


def _invalid_table(
    statement: exp.Select,
    table_columns: Mapping[str, frozenset[str]],
    target_database: str,
) -> str | None:
    cte_names = _cte_names(statement)
    for table in statement.find_all(exp.Table):
        name = table.name.casefold()
        database = table.db.casefold() if table.db else ""
        catalog = table.catalog.casefold() if table.catalog else ""
        if name in cte_names and not database and not catalog:
            continue
        if name not in table_columns:
            return f"table {table.sql(dialect='mysql')} is not whitelisted"
        if catalog or database not in {"", target_database.casefold()}:
            return f"database qualifier for {table.sql(dialect='mysql')} is not allowed"
    return None


def _scope_output_names(scope: Scope) -> frozenset[str]:
    return frozenset(
        select.alias_or_name.casefold()
        for select in scope.expression.selects
        if select.alias_or_name
    )


def _source_columns(
    source: exp.Table | Scope, table_columns: Mapping[str, frozenset[str]]
) -> frozenset[str]:
    if isinstance(source, Scope):
        return _scope_output_names(source)
    return table_columns.get(source.name.casefold(), frozenset())


def _is_projection_alias_reference(column: exp.Column, scope: Scope) -> bool:
    parent = column.parent
    while parent is not None and parent is not scope.expression:
        if isinstance(parent, (exp.Group, exp.Order, exp.Having, exp.Qualify)):
            return True
        parent = parent.parent
    return False


def _invalid_column(
    statement: exp.Select, table_columns: Mapping[str, frozenset[str]]
) -> str | None:
    for star in statement.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            return "result-set wildcard is not allowed"

    for scope in traverse_scope(statement):
        sources = {name.casefold(): source for name, source in scope.sources.items()}
        projection_aliases = _scope_output_names(scope)
        for column in scope.columns:
            name = column.name.casefold()
            qualifier = column.table.casefold() if column.table else ""
            if qualifier:
                source = sources.get(qualifier)
                if source is None or name not in _source_columns(source, table_columns):
                    return f"column {column.sql(dialect='mysql')} cannot be resolved"
                continue
            matching_sources = [
                source
                for source in sources.values()
                if name in _source_columns(source, table_columns)
            ]
            if len(matching_sources) == 1:
                continue
            if (
                not matching_sources
                and name in projection_aliases
                and _is_projection_alias_reference(column, scope)
            ):
                continue
            if len(matching_sources) > 1:
                return f"column {column.name} is ambiguous"
            return f"column {column.name} is not whitelisted"
    return None


def _function_name(function: exp.Func) -> str:
    if isinstance(function, exp.Anonymous):
        return function.name.casefold()
    return function.sql_name().casefold()


def _dangerous_reason(statement: exp.Select) -> str | None:
    for node in statement.walk():
        if node.comments:
            return "SQL comments are not allowed"
    if statement.find(exp.Into) is not None:
        return "INTO is not allowed"
    if statement.find(exp.Lock) is not None:
        return "locking reads are not allowed"
    if statement.find(exp.Parameter) is not None:
        return "user variables and parameters are not allowed"
    if statement.find(exp.SessionParameter) is not None:
        return "system variables are not allowed"
    if statement.find(exp.Offset) is not None:
        return "OFFSET is not allowed"
    for function in statement.find_all(exp.Func):
        name = _function_name(function)
        if name in DANGEROUS_FUNCTIONS:
            return f"function {name} is not allowed"
    limit = statement.args.get("limit")
    if isinstance(limit, exp.Limit):
        expression = limit.expression
        if not isinstance(expression, exp.Literal) or not expression.is_int:
            return "LIMIT must be a non-negative integer literal"
        if int(expression.this) < 0:
            return "LIMIT must be a non-negative integer literal"
    return None


def _rewrite_limit(statement: exp.Select) -> str:
    limit = statement.args.get("limit")
    if limit is None:
        statement.set("limit", exp.Limit(expression=exp.Literal.number(MAX_ROWS)))
    elif isinstance(limit, exp.Limit) and int(limit.expression.this) > MAX_ROWS:
        limit.set("expression", exp.Literal.number(MAX_ROWS))
    return statement.sql(dialect="mysql")


def validate_and_rewrite(
    sql: str, domain_config: DomainConfig | None = None
) -> SandboxDecision:
    """Apply S-01~S-07 and return an executable SELECT capped at 200 rows."""

    if not isinstance(sql, str) or not sql.strip():
        return _reject(str(sql), "S-07", "SQL is empty")
    try:
        tokens = Tokenizer(dialect="mysql").tokenize(sql)
    except Exception as exc:
        return _reject(sql, "S-07", f"tokenization failed: {exc}")
    if _has_into_file_tokens(tokens):
        return _reject(sql, "S-05", "INTO OUTFILE/DUMPFILE is not allowed")
    try:
        statements: list[Any] = sqlglot.parse(sql, read="mysql")
    except ParseError as exc:
        return _reject(sql, "S-07", f"parse failed: {exc}")
    if len(statements) != 1:
        return _reject(sql, "S-02", "exactly one SQL statement is required")
    statement = statements[0]
    if not isinstance(statement, exp.Select):
        return _reject(sql, "S-01", "the AST root must be Select")
    domain = domain_config or active_domain_config()
    table_reason = _invalid_table(
        statement, domain.table_columns, domain.database
    )
    if table_reason:
        return _reject(sql, "S-03", table_reason)
    column_reason = _invalid_column(statement, domain.table_columns)
    if column_reason:
        return _reject(sql, "S-04", column_reason)
    dangerous_reason = _dangerous_reason(statement)
    if dangerous_reason:
        return _reject(sql, "S-05", dangerous_reason)
    return SandboxDecision(True, sql, _rewrite_limit(statement), None, None)
