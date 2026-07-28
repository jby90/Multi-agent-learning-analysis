"""Build Text2SQL prompts from a validated domain configuration package."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from agents.domain_config import DomainConfig, active_domain_config


ROOT = Path(__file__).resolve().parents[2]
IMPORT_SQL_PATH = ROOT / "data" / "比赛数据包" / "import_mysql.sql"
DICTIONARY_PATH = ROOT / "data" / "比赛数据包" / "data_dictionary.md"
PROMPT_PATH = Path(__file__).with_name("verification.md")
INTENT_FIELDS = frozenset(
    {
        "text2sql_output_schema",
        "router_output_schema",
        "generation_preamble",
        "router_prompt",
        "full_profile",
        "out_of_scope_profile",
        "full_examples_heading",
        "routed_examples_heading",
        "few_shots",
        "routing_profiles",
    }
)
FEW_SHOT_FIELDS = frozenset(
    {"example_id", "family", "question", "sql", "verified_result"}
)
_CREATE_TABLE_RE = re.compile(
    r"CREATE TABLE\s+\w+\s*\(.*?\) CHARACTER SET utf8mb4;",
    flags=re.IGNORECASE | re.DOTALL,
)
_EXAMPLE_RE = re.compile(
    r"^### Few-shot (?P<display_id>[A-Za-z0-9_-]+) \| (?P<family>[A-Za-z0-9_-]+)\n"
    r"问题：(?P<question>[^\r\n]+)\n"
    r"SQL：\n```sql\n(?P<sql>[\s\S]+?)\n```\n"
    r"实测结果：(?P<verified>[^\r\n]+)$",
    flags=re.MULTILINE,
)


class PromptBuildError(RuntimeError):
    """Raised when a package cannot form a closed prompt catalog."""


@dataclass(frozen=True, slots=True)
class FewShotExample:
    example_id: str
    family: str
    question: str
    sql: str
    verified_result: str


@dataclass(frozen=True, slots=True)
class PromptCatalog:
    fixed_prefix: str
    router_prompt: str
    few_shots: tuple[FewShotExample, ...]
    routing_profiles: Mapping[str, tuple[str, ...]]
    full_profile: str
    out_of_scope_profile: str
    full_examples_heading: str
    routed_examples_heading: str
    router_output_schema: dict[str, Any]
    text2sql_output_schema: dict[str, Any]

    def ids_for(self, profile: str) -> tuple[str, ...]:
        try:
            return self.routing_profiles[profile]
        except KeyError as exc:
            raise ValueError(f"unknown prompt profile: {profile}") from exc

    def render_examples(self, profile: str) -> str:
        by_id = {item.example_id: item for item in self.few_shots}
        return "\n\n".join(_format_example(by_id[item]) for item in self.ids_for(profile))

    def render_generation(self, profile: str) -> str:
        heading = (
            self.full_examples_heading
            if profile == self.full_profile
            else self.routed_examples_heading
        )
        return f"{self.fixed_prefix}\n\n## {heading}\n\n{self.render_examples(profile)}\n"


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _mapping(value: Any, field: str, fields: frozenset[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PromptBuildError(f"{field} must be an object")
    if fields is not None and set(value) != fields:
        raise PromptBuildError(f"{field} fields do not match the package schema")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PromptBuildError(f"{field} must be a non-empty string")
    return value


def _schema(value: Any, field: str) -> dict[str, Any]:
    schema = _plain(_mapping(value, field))
    try:
        families = schema["properties"]["family"]["enum"]
    except (KeyError, TypeError) as exc:
        raise PromptBuildError(f"{field} must define properties.family.enum") from exc
    if not isinstance(families, list) or not families or not all(
        isinstance(item, str) and item for item in families
    ):
        raise PromptBuildError(f"{field} family enum must be a non-empty string list")
    return schema


def _examples(value: Any, families: frozenset[str]) -> tuple[FewShotExample, ...]:
    if not isinstance(value, tuple) or not value:
        raise PromptBuildError("few_shots must be a non-empty list")
    examples: list[FewShotExample] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        item = _mapping(raw, f"few_shots[{index}]", FEW_SHOT_FIELDS)
        example_id = _string(item["example_id"], f"few_shots[{index}].example_id")
        if example_id in seen:
            raise PromptBuildError(f"duplicate few-shot ID: {example_id}")
        seen.add(example_id)
        family = _string(item["family"], f"few_shots[{index}].family")
        if family not in families:
            raise PromptBuildError(f"few-shot {example_id} has unknown family {family}")
        examples.append(
            FewShotExample(
                example_id=example_id,
                family=family,
                question=_string(item["question"], f"few_shots[{index}].question"),
                sql=_string(item["sql"], f"few_shots[{index}].sql"),
                verified_result=_string(
                    item["verified_result"], f"few_shots[{index}].verified_result"
                ),
            )
        )
    return tuple(examples)


def _profiles(
    value: Any,
    examples: tuple[FewShotExample, ...],
    full_profile: str,
    out_of_scope_profile: str,
) -> Mapping[str, tuple[str, ...]]:
    raw_profiles = _mapping(value, "routing_profiles")
    known_ids = {item.example_id for item in examples}
    profiles: dict[str, tuple[str, ...]] = {}
    for raw_name, raw_ids in raw_profiles.items():
        name = _string(raw_name, "routing profile name")
        if not isinstance(raw_ids, tuple) or not raw_ids:
            raise PromptBuildError(f"routing profile {name} must contain examples")
        ids = tuple(_string(item, f"routing_profiles.{name}[]") for item in raw_ids)
        if len(set(ids)) != len(ids) or not set(ids).issubset(known_ids):
            raise PromptBuildError(f"routing profile {name} has duplicate or unknown IDs")
        profiles[name] = ids
    if full_profile not in profiles or out_of_scope_profile not in profiles:
        raise PromptBuildError("full and out-of-scope profiles must exist")
    if profiles[full_profile] != tuple(item.example_id for item in examples):
        raise PromptBuildError("full profile must list every few-shot in package order")
    return profiles


def extract_schema_ddl(import_sql: str) -> str:
    blocks = _CREATE_TABLE_RE.findall(import_sql)
    if len(blocks) != 5:
        raise PromptBuildError(f"expected 5 CREATE TABLE blocks, got {len(blocks)}")
    return "\n\n".join(block.strip() for block in blocks)


def extract_dictionary_rows(dictionary_markdown: str) -> str:
    lines: list[str] = []
    for line in dictionary_markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            lines.append(stripped)
        elif stripped.startswith("|") and not re.match(r"^\|[- :|]+\|$", stripped):
            lines.append(stripped)
    if not any("plan_qty" in line for line in lines):
        raise PromptBuildError("data dictionary extraction missed plan_qty")
    return "\n".join(lines)


def _format_example(example: FewShotExample) -> str:
    display_id = (
        example.example_id[3:]
        if re.fullmatch(r"FS-\d+", example.example_id)
        else example.example_id
    )
    return (
        f"### Few-shot {display_id} | {example.family}\n"
        f"问题：{example.question}\n"
        "SQL：\n"
        f"```sql\n{example.sql}\n```\n"
        f"实测结果：{example.verified_result}"
    )


def _catalog_from_domain(
    domain: DomainConfig,
    *,
    schema_ddl: str | None = None,
    dictionary_rows: str | None = None,
) -> PromptCatalog:
    intents = _mapping(domain.intents, "intents", INTENT_FIELDS)
    text_schema = _schema(intents["text2sql_output_schema"], "text2sql_output_schema")
    router_schema = _schema(intents["router_output_schema"], "router_output_schema")
    text_families = tuple(text_schema["properties"]["family"]["enum"])
    router_families = tuple(router_schema["properties"]["family"]["enum"])
    if router_families != text_families:
        raise PromptBuildError("router and generator family enums must match")
    examples = _examples(intents["few_shots"], frozenset(text_families))
    full_profile = _string(intents["full_profile"], "full_profile")
    out_profile = _string(intents["out_of_scope_profile"], "out_of_scope_profile")
    profiles = _profiles(
        intents["routing_profiles"], examples, full_profile, out_profile
    )
    ddl = schema_ddl if schema_ddl is not None else domain.schema_ddl
    rows = dictionary_rows if dictionary_rows is not None else domain.dictionary_rows
    fixed_prefix = (
        f"{_string(intents['generation_preamble'], 'generation_preamble')}\n\n"
        "## Schema DDL（从import_mysql.sql机械提取）\n\n"
        f"```sql\n{ddl}\n```\n\n"
        "## 字段字典关键行（从data_dictionary.md机械提取）\n\n"
        f"{rows}"
    )
    return PromptCatalog(
        fixed_prefix=fixed_prefix,
        router_prompt=_string(intents["router_prompt"], "router_prompt"),
        few_shots=examples,
        routing_profiles=profiles,
        full_profile=full_profile,
        out_of_scope_profile=out_profile,
        full_examples_heading=_string(
            intents["full_examples_heading"], "full_examples_heading"
        ),
        routed_examples_heading=_string(
            intents["routed_examples_heading"], "routed_examples_heading"
        ),
        router_output_schema=router_schema,
        text2sql_output_schema=text_schema,
    )


def build_prompt_catalog(
    import_sql: str | None = None,
    dictionary_markdown: str | None = None,
    *,
    domain_config: DomainConfig | None = None,
) -> PromptCatalog:
    if domain_config is not None and (
        import_sql is not None or dictionary_markdown is not None
    ):
        raise PromptBuildError("domain_config cannot be combined with legacy prompt inputs")
    domain = domain_config or active_domain_config()
    if import_sql is None and dictionary_markdown is None:
        return _catalog_from_domain(domain)
    if import_sql is None or dictionary_markdown is None:
        raise PromptBuildError("both legacy prompt inputs are required")
    return _catalog_from_domain(
        domain,
        schema_ddl=extract_schema_ddl(import_sql),
        dictionary_rows=extract_dictionary_rows(dictionary_markdown),
    )


_DEFAULT_CATALOG = build_prompt_catalog(domain_config=active_domain_config())
TEXT2SQL_OUTPUT_SCHEMA = _DEFAULT_CATALOG.text2sql_output_schema
ROUTER_OUTPUT_SCHEMA = _DEFAULT_CATALOG.router_output_schema
FEW_SHOTS = _DEFAULT_CATALOG.few_shots
FULL_15_IDS = _DEFAULT_CATALOG.ids_for(_DEFAULT_CATALOG.full_profile)
ROUTED_FEW_SHOT_IDS = {
    name: ids
    for name, ids in _DEFAULT_CATALOG.routing_profiles.items()
    if name != _DEFAULT_CATALOG.full_profile
}
OUT_OF_SCOPE_MINI_IDS = _DEFAULT_CATALOG.ids_for(
    _DEFAULT_CATALOG.out_of_scope_profile
)
ROUTER_PROMPT = _DEFAULT_CATALOG.router_prompt


def build_prompt(import_sql: str, dictionary_markdown: str) -> str:
    catalog = build_prompt_catalog(import_sql, dictionary_markdown)
    return catalog.render_generation(catalog.full_profile)


def extract_few_shot_examples(prompt: str) -> tuple[FewShotExample, ...]:
    examples: list[FewShotExample] = []
    for match in _EXAMPLE_RE.finditer(prompt):
        display_id = match.group("display_id")
        example_id = f"FS-{display_id}" if display_id.isdigit() else display_id
        examples.append(
            FewShotExample(
                example_id=example_id,
                family=match.group("family"),
                question=match.group("question"),
                sql=match.group("sql"),
                verified_result=match.group("verified"),
            )
        )
    return tuple(examples)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the verification prompt")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    rendered = build_prompt(
        IMPORT_SQL_PATH.read_text(encoding="utf-8"),
        DICTIONARY_PATH.read_text(encoding="utf-8"),
    )
    if args.check:
        if not PROMPT_PATH.exists() or PROMPT_PATH.read_text(encoding="utf-8") != rendered:
            print("FAIL: verification.md is stale")
            return 1
        print("PASS: verification.md is current")
        return 0
    PROMPT_PATH.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"WROTE: {PROMPT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
