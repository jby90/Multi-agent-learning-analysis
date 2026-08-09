"""Deterministic task products from the approved P4/P5 SQL catalog."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from string import Formatter
from time import perf_counter
from typing import Any, Callable, Mapping

from orchestrator.llm import LLMResult

from agents.domain_config import DomainConfig, active_domain_config
from agents.knowledge_scope import prerequisite_scaffolds, responsibility_scope
from agents.query_authority import QueryAuthority, build_query_authority


_DEFAULT_DOMAIN = active_domain_config()
TASK_TEMPLATE_PATH = _DEFAULT_DOMAIN.asset_paths["task_templates"]
COUNTER_EVIDENCE_PATH = _DEFAULT_DOMAIN.asset_paths["counter_evidence"]
MODEL = "qwen3-235b-a22b"
TEMPERATURE = 0.5
PROMPT_PATH = Path(__file__).with_name("prompts") / "task_contextualize.md"
TASK_CONTEXT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["contextualized_stem", "guide_intro"],
    "properties": {
        "contextualized_stem": {"type": "string", "minLength": 1},
        "guide_intro": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}
PARAMETER_ORDER = tuple(_DEFAULT_DOMAIN.task_manifest["parameter_order"])
TEMPLATE_IDS = tuple(_DEFAULT_DOMAIN.task_manifest["template_ids"])
MISCONCEPTION_IDS = tuple(_DEFAULT_DOMAIN.task_manifest["misconception_ids"])
DIFFICULTIES = tuple(_DEFAULT_DOMAIN.task_manifest["difficulties"])
METRIC_TEXT_TERMS = {
    key: tuple(value)
    for key, value in _DEFAULT_DOMAIN.task_manifest["metric_text_terms"].items()
}
_MONTH_AS_BATCH_RE = re.compile(r"20\d{2}-\d{2}\s*批次")
DIAGNOSTIC_TEMPLATE_IDS = {
    point: dict(routes)
    for point, routes in _DEFAULT_DOMAIN.task_manifest[
        "diagnostic_template_ids"
    ].items()
}
TASK_MANIFEST_FIELDS = frozenset(
    {
        "parameter_order",
        "template_ids",
        "misconception_ids",
        "difficulties",
        "metric_text_terms",
        "diagnostic_template_ids",
    }
)
TASK_FIELDS = frozenset(
    {
        "template_id",
        "knowledge_point",
        "difficulty",
        "payload_type",
        "question_template",
        "family",
        "standard_sql",
        "expected_rows",
        "expected_points",
    }
)
PRACTICE_GUIDE_FIELDS = frozenset({"guide_steps", "completion_criteria"})
COUNTER_FIELDS = frozenset(
    {
        "misconception",
        "question_template",
        "family",
        "standard_sqls",
        "expected_results",
        "expected_points",
    }
)


@dataclass(frozen=True, slots=True)
class TaskTemplate:
    template_id: str
    knowledge_point: str
    difficulty: str
    payload_type: str
    question_template: str
    family: str
    standard_sql: str
    expected_rows: tuple[dict[str, str], ...]
    expected_points: tuple[str, ...]
    guide_steps: tuple[str, ...]
    completion_criteria: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CounterEvidence:
    misconception: str
    question_template: str
    family: str
    standard_sqls: tuple[str, ...]
    expected_results: tuple[tuple[dict[str, str], ...], ...]
    expected_points: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskCatalog:
    domain_id: str
    domain_package_sha256: str
    demo_parameters: dict[str, str]
    templates: dict[str, TaskTemplate]
    counter_evidence: dict[str, CounterEvidence]
    parameter_order: tuple[str, ...]
    template_ids: tuple[str, ...]
    misconception_ids: tuple[str, ...]
    difficulties: tuple[str, ...]
    metric_text_terms: dict[str, tuple[str, ...]]
    diagnostic_template_ids: dict[str, dict[str, str]]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON asset {path}: {exc}") from exc


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    return tuple(_string(item, f"{field}[]") for item in value)


def _rows(value: Any, field: str) -> tuple[dict[str, str], ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{field} must be a non-empty row list")
    parsed: list[dict[str, str]] = []
    for row_index, row in enumerate(value):
        if not isinstance(row, Mapping) or not row:
            raise ValueError(f"{field}[{row_index}] must be a non-empty object")
        parsed_row: dict[str, str] = {}
        for column, cell in row.items():
            key = _string(column, f"{field}[{row_index}] column")
            parsed_row[key] = _string(cell, f"{field}[{row_index}].{key}")
        parsed.append(parsed_row)
    return tuple(parsed)


def _render(template: str, parameters: Mapping[str, str]) -> str:
    fields: list[str] = []
    for _, field_name, format_spec, conversion in Formatter().parse(template):
        if field_name is None:
            continue
        if format_spec or conversion or field_name not in parameters:
            raise ValueError(f"unsupported question template slot: {field_name}")
        fields.append(field_name)
    try:
        return template.format_map(parameters)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"invalid question template: {exc}") from exc


def _parameter_values(
    template: str, parameters: Mapping[str, str]
) -> tuple[str, ...]:
    values: list[str] = []
    for _, field_name, _, _ in Formatter().parse(template):
        if field_name is None:
            continue
        value = parameters[field_name]
        if value not in values:
            values.append(value)
    return tuple(values)


def _parse_templates(
    raw: Any,
    parameters: Mapping[str, str],
    template_ids: tuple[str, ...],
    difficulties: tuple[str, ...],
) -> dict[str, TaskTemplate]:
    if not isinstance(raw, (list, tuple)) or len(raw) != len(template_ids):
        raise ValueError(
            f"task catalog must contain exactly {len(template_ids)} templates"
        )
    templates: dict[str, TaskTemplate] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"templates[{index}] must be an object")
        payload_type = _string(item.get("payload_type"), "payload_type")
        if payload_type not in {"quiz_set", "practice_guide"}:
            raise ValueError("payload_type must be quiz_set|practice_guide")
        expected_fields = (
            TASK_FIELDS
            if payload_type == "quiz_set"
            else TASK_FIELDS | PRACTICE_GUIDE_FIELDS
        )
        if set(item) != expected_fields:
            missing = sorted(expected_fields - set(item))
            unexpected = sorted(set(item) - expected_fields)
            raise ValueError(
                f"{payload_type} template fields do not match the approved schema; "
                f"missing={missing}; unexpected={unexpected}"
            )
        template_id = _string(item["template_id"], "template_id")
        if template_id != template_ids[index]:
            raise ValueError(f"task template order must be {template_ids[index]}")
        question_template = _string(item["question_template"], "question_template")
        _render(question_template, parameters)
        difficulty = _string(item["difficulty"], "difficulty")
        if difficulty not in difficulties:
            raise ValueError(f"difficulty must be one of {difficulties}")
        if payload_type == "practice_guide":
            guide_steps = _string_tuple(item["guide_steps"], "guide_steps")
            if len(guide_steps) < 3:
                raise ValueError("guide_steps must contain at least three steps")
            completion_criteria = _string_tuple(
                item["completion_criteria"],
                "completion_criteria",
            )
            for field, values in (
                ("guide_steps", guide_steps),
                ("completion_criteria", completion_criteria),
            ):
                for value_index, value in enumerate(values):
                    try:
                        _render(value, parameters)
                    except ValueError as exc:
                        raise ValueError(f"{field}[{value_index}]: {exc}") from exc
        else:
            guide_steps = ()
            completion_criteria = ()
        templates[template_id] = TaskTemplate(
            template_id=template_id,
            knowledge_point=_string(item["knowledge_point"], "knowledge_point"),
            difficulty=difficulty,
            payload_type=payload_type,
            question_template=question_template,
            family=_string(item["family"], "family"),
            standard_sql=_string(item["standard_sql"], "standard_sql"),
            expected_rows=_rows(item["expected_rows"], "expected_rows"),
            expected_points=_string_tuple(item["expected_points"], "expected_points"),
            guide_steps=guide_steps,
            completion_criteria=completion_criteria,
        )
    return templates


def _parse_counters(
    raw: Any,
    parameters: Mapping[str, str],
    misconception_ids: tuple[str, ...],
) -> dict[str, CounterEvidence]:
    if not isinstance(raw, (list, tuple)) or len(raw) != len(misconception_ids):
        raise ValueError(
            f"counter catalog must contain exactly {len(misconception_ids)} mappings"
        )
    counters: dict[str, CounterEvidence] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping) or set(item) != COUNTER_FIELDS:
            raise ValueError(f"mappings[{index}] fields do not match the approved schema")
        misconception = _string(item["misconception"], "misconception")
        if misconception != misconception_ids[index]:
            raise ValueError(f"counter mapping order must be {misconception_ids[index]}")
        question_template = _string(item["question_template"], "question_template")
        _render(question_template, parameters)
        standard_sqls = _string_tuple(item["standard_sqls"], "standard_sqls")
        raw_results = item["expected_results"]
        if not isinstance(raw_results, (list, tuple)):
            raise ValueError("expected_results must be a list")
        expected_results = tuple(
            _rows(result, f"expected_results[{result_index}]")
            for result_index, result in enumerate(raw_results)
        )
        if len(standard_sqls) != len(expected_results):
            raise ValueError("standard_sqls and expected_results lengths must match")
        counters[misconception] = CounterEvidence(
            misconception=misconception,
            question_template=question_template,
            family=_string(item["family"], "family"),
            standard_sqls=standard_sqls,
            expected_results=expected_results,
            expected_points=_string_tuple(item["expected_points"], "expected_points"),
        )
    return counters


def _unique_string_tuple(value: Any, field: str) -> tuple[str, ...]:
    values = _string_tuple(value, field)
    if len(set(values)) != len(values):
        raise ValueError(f"{field} must not contain duplicates")
    return values


def _parse_task_manifest(raw: Any) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    dict[str, tuple[str, ...]],
    dict[str, dict[str, str]],
]:
    if not isinstance(raw, Mapping) or set(raw) != TASK_MANIFEST_FIELDS:
        raise ValueError("task manifest fields do not match the approved schema")
    parameter_order = _unique_string_tuple(raw["parameter_order"], "parameter_order")
    template_ids = _unique_string_tuple(raw["template_ids"], "template_ids")
    misconception_ids = _unique_string_tuple(
        raw["misconception_ids"], "misconception_ids"
    )
    difficulties = _unique_string_tuple(raw["difficulties"], "difficulties")
    raw_terms = raw["metric_text_terms"]
    if not isinstance(raw_terms, Mapping) or not raw_terms:
        raise ValueError("metric_text_terms must be a non-empty object")
    metric_terms = {
        _string(metric, "metric_text_terms key"): _unique_string_tuple(
            terms, f"metric_text_terms.{metric}"
        )
        for metric, terms in raw_terms.items()
    }
    raw_routes = raw["diagnostic_template_ids"]
    if not isinstance(raw_routes, Mapping):
        raise ValueError("diagnostic_template_ids must be an object")
    routes: dict[str, dict[str, str]] = {}
    for point, raw_mapping in raw_routes.items():
        knowledge_point = _string(point, "diagnostic knowledge point")
        if not isinstance(raw_mapping, Mapping) or set(raw_mapping) != set(difficulties):
            raise ValueError(
                f"diagnostic route {knowledge_point} must map every difficulty"
            )
        routes[knowledge_point] = {
            difficulty: _string(
                raw_mapping[difficulty],
                f"diagnostic_template_ids.{knowledge_point}.{difficulty}",
            )
            for difficulty in difficulties
        }
    return (
        parameter_order,
        template_ids,
        misconception_ids,
        difficulties,
        metric_terms,
        routes,
    )


def load_task_catalog(
    template_path: Path | None = None,
    counter_path: Path | None = None,
    *,
    domain_config: DomainConfig | None = None,
) -> TaskCatalog:
    """Load task structure and content from one validated domain package."""

    if domain_config is not None and (template_path is not None or counter_path is not None):
        raise ValueError("domain_config cannot be combined with explicit task asset paths")
    domain = domain_config or active_domain_config()
    task_root = (
        _read_json(Path(template_path))
        if template_path is not None
        else domain.task_templates
    )
    counter_root = (
        _read_json(Path(counter_path))
        if counter_path is not None
        else domain.counter_evidence
    )
    if not isinstance(task_root, Mapping) or set(task_root) != {
        "demo_parameters",
        "templates",
    }:
        raise ValueError("task template root fields do not match the approved schema")
    if not isinstance(counter_root, Mapping) or set(counter_root) != {"mappings"}:
        raise ValueError("counter evidence root fields do not match the approved schema")
    (
        parameter_order,
        template_ids,
        misconception_ids,
        difficulties,
        metric_terms,
        diagnostic_routes,
    ) = _parse_task_manifest(domain.task_manifest)
    raw_parameters = task_root["demo_parameters"]
    if not isinstance(raw_parameters, Mapping) or tuple(raw_parameters) != parameter_order:
        raise ValueError("demo_parameters do not match the package ordered slots")
    parameters = {
        key: _string(raw_parameters[key], f"demo_parameters.{key}")
        for key in parameter_order
    }
    templates = _parse_templates(
        task_root["templates"], parameters, template_ids, difficulties
    )
    counters = _parse_counters(
        counter_root["mappings"], parameters, misconception_ids
    )
    for point, mapping in diagnostic_routes.items():
        for difficulty, template_id in mapping.items():
            entry = templates.get(template_id)
            if entry is None:
                raise ValueError(
                    f"diagnostic route {point}/{difficulty} references unknown template "
                    f"{template_id}"
                )
            if entry.knowledge_point != point or entry.difficulty != difficulty:
                raise ValueError(
                    f"diagnostic route {point}/{difficulty} does not match {template_id}"
                )
    return TaskCatalog(
        domain_id=domain.domain_id,
        domain_package_sha256=domain.package_sha256,
        demo_parameters=parameters,
        templates=templates,
        counter_evidence=counters,
        parameter_order=parameter_order,
        template_ids=template_ids,
        misconception_ids=misconception_ids,
        difficulties=difficulties,
        metric_text_terms=metric_terms,
        diagnostic_template_ids=diagnostic_routes,
    )


def _answer_key_quote(
    standard_sql: str,
    expected_rows: tuple[dict[str, str], ...],
    expected_points: tuple[str, ...],
) -> str:
    return json.dumps(
        {
            "standard_sql": standard_sql,
            "expected_rows": expected_rows,
            "expected_points": expected_points,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _select_template(
    catalog: TaskCatalog,
    template_id: str,
    diagnostic_difficulty: str | None,
) -> TaskTemplate:
    try:
        requested = catalog.templates[template_id]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unsupported template_id: {template_id}") from exc
    if diagnostic_difficulty is None:
        return requested
    if diagnostic_difficulty not in catalog.difficulties:
        raise ValueError(
            f"diagnostic_difficulty must be one of {catalog.difficulties} or None"
        )
    mapping = catalog.diagnostic_template_ids.get(requested.knowledge_point)
    if mapping is None:
        return requested
    selected_id = mapping[diagnostic_difficulty]
    try:
        selected = catalog.templates[selected_id]
    except KeyError as exc:
        raise ValueError(f"missing diagnostic task template: {selected_id}") from exc
    if (
        selected.knowledge_point != requested.knowledge_point
        or selected.difficulty != diagnostic_difficulty
    ):
        raise ValueError(f"invalid diagnostic task template: {selected_id}")
    return selected


class TaskAgent:
    """Render approved task anchors and optionally contextualize their display."""

    def __init__(
        self,
        trace_id: str,
        catalog: TaskCatalog | None = None,
        llm_call: Callable[..., LLMResult] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._trace_id = _string(trace_id, "trace_id")
        self._catalog = catalog or load_task_catalog()
        self._llm_call = llm_call
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    @property
    def domain_id(self) -> str:
        """Return the immutable domain package identity used by this agent."""

        return self._catalog.domain_id

    @property
    def domain_package_sha256(self) -> str:
        """Bind learning contracts to the exact validated domain package."""

        return self._catalog.domain_package_sha256

    @property
    def misconception_ids(self) -> tuple[str, ...]:
        """Return the immutable misconception vocabulary of the active domain."""

        return tuple(self._catalog.misconception_ids)

    def generate_for_diagnosis(
        self,
        knowledge_point: str,
        diagnostic_difficulty: str,
        *,
        student_profile: Mapping[str, Any] | None = None,
        learning_report_summary: str | None = None,
    ) -> dict[str, Any]:
        anchor = self.diagnosis_evidence(
            knowledge_point,
            diagnostic_difficulty,
        )
        return self.generate(
            str(anchor["template_id"]),
            diagnostic_difficulty=diagnostic_difficulty,
            student_profile=student_profile,
            learning_report_summary=learning_report_summary,
        )

    def diagnosis_evidence(
        self,
        knowledge_point: str,
        diagnostic_difficulty: str,
    ) -> dict[str, Any]:
        """Resolve the deterministic business-data anchor before generation."""
        point = _string(knowledge_point, "diagnostic knowledge point")
        difficulty = _string(
            diagnostic_difficulty,
            "diagnostic difficulty",
        )
        routes = self._catalog.diagnostic_template_ids.get(point)
        if routes is None:
            raise ValueError(f"unsupported diagnostic knowledge point: {point}")
        try:
            template_id = routes[difficulty]
        except KeyError as exc:
            raise ValueError(
                f"unsupported diagnostic difficulty: {difficulty}"
            ) from exc
        entry = self._catalog.templates[template_id]
        expected_columns = tuple(entry.expected_rows[0]) if entry.expected_rows else ()
        return {
            "template_id": entry.template_id,
            "knowledge_point": entry.knowledge_point,
            "difficulty": entry.difficulty,
            "family": entry.family,
            "payload_type": entry.payload_type,
            "expected_columns": list(expected_columns),
            "expected_row_count": len(entry.expected_rows),
        }

    def generate_for_learning_action(
        self,
        current_template_id: str,
        action: str,
        *,
        student_profile: Mapping[str, Any] | None = None,
        learning_report_summary: str | None = None,
    ) -> dict[str, Any] | None:
        template_id = _string(current_template_id, "current template_id")
        learning_action = _string(action, "learning action")
        offsets = {"keep": 0, "step_up": 1, "step_down": -1}
        try:
            offset = offsets[learning_action]
        except KeyError as exc:
            raise ValueError(
                f"unsupported learning action: {learning_action}"
            ) from exc
        try:
            current = self._catalog.templates[template_id]
        except KeyError as exc:
            raise ValueError(f"unsupported template_id: {template_id}") from exc
        routes = self._catalog.diagnostic_template_ids.get(current.knowledge_point)
        if routes is None:
            raise ValueError(
                f"unsupported learning knowledge point: {current.knowledge_point}"
            )
        current_index = self._catalog.difficulties.index(current.difficulty)
        target_index = current_index + offset
        if target_index < 0 or target_index >= len(self._catalog.difficulties):
            return None
        target_difficulty = self._catalog.difficulties[target_index]
        return self.generate_for_diagnosis(
            current.knowledge_point,
            target_difficulty,
            student_profile=student_profile,
            learning_report_summary=learning_report_summary,
        )

    def generate(
        self,
        template_id: str,
        *,
        diagnostic_difficulty: str | None = None,
        student_profile: Mapping[str, Any] | None = None,
        learning_report_summary: str | None = None,
    ) -> dict[str, Any]:
        return self._generate(
            template_id,
            diagnostic_difficulty=diagnostic_difficulty,
            student_profile=student_profile,
            learning_report_summary=learning_report_summary,
        )

    def _generate(
        self,
        template_id: str,
        *,
        diagnostic_difficulty: str | None = None,
        student_profile: Mapping[str, Any] | None = None,
        learning_report_summary: str | None = None,
    ) -> dict[str, Any]:
        entry = _select_template(
            self._catalog,
            template_id,
            diagnostic_difficulty,
        )
        standard_stem = _render(
            entry.question_template, self._catalog.demo_parameters
        )
        query_authority = build_query_authority(
            template_id=entry.template_id,
            family=entry.family,
            standard_stem=standard_stem,
            standard_sql=entry.standard_sql,
        )
        display, metadata = self._contextualize(
            standard_stem=standard_stem,
            parameter_values=_parameter_values(
                entry.question_template, self._catalog.demo_parameters
            ),
            student_profile=student_profile,
            learning_report_summary=learning_report_summary,
            query_authority=query_authority,
        )
        question = display["contextualized_stem"]
        content: dict[str, Any] = {
            "event": "product_ready",
            "template_id": entry.template_id,
            "knowledge_point": entry.knowledge_point,
            "responsibility_scope": list(
                responsibility_scope(entry.knowledge_point, entry.difficulty)
            ),
            "difficulty": entry.difficulty,
            "question": question,
            "family": entry.family,
            "query_authority": query_authority.as_dict(),
            **display,
        }
        if entry.payload_type == "quiz_set":
            content["questions"] = [{"id": entry.template_id, "prompt": question}]
        else:
            guide_steps = [
                _render(step, self._catalog.demo_parameters)
                for step in entry.guide_steps
            ]
            scaffolds = [
                dict(item)
                for item in prerequisite_scaffolds(
                    entry.knowledge_point,
                    entry.difficulty,
                )
            ]
            scaffold_steps = [
                "前置检查："
                + str(item["knowledge_point"])
                + "——"
                + str(item["learning_goal"])
                for item in scaffolds
            ]
            guide_steps = [*scaffold_steps, *guide_steps]
            completion_criteria = [
                _render(criterion, self._catalog.demo_parameters)
                for criterion in entry.completion_criteria
            ]
            guide_intro = display["guide_intro"].strip() or (
                "开始前，请确认题目中的对象、范围和统计口径。"
            )
            content.update(
                {
                    "guide_intro": guide_intro,
                    "prerequisite_scaffolds": scaffolds,
                    "guide_steps": guide_steps,
                    "completion_criteria": completion_criteria,
                    "guide_md": "\n\n".join(
                        (
                            f"## 实操目标\n\n{question}",
                            f"## 开始前\n\n{guide_intro}",
                            "## 操作步骤\n\n"
                            + "\n".join(
                                f"{index}. {step}"
                                for index, step in enumerate(guide_steps, start=1)
                            ),
                            "## 完成标准\n\n"
                            + "\n".join(
                                f"- {criterion}"
                                for criterion in completion_criteria
                            ),
                        )
                    ),
                }
            )
        draft = {
            "trace_id": self._trace_id,
            "agent": "task",
            "role": "produce",
            "payload": {"type": entry.payload_type, "content": content},
            "evidence": [
                {
                    "kind": "quiz_answer_key",
                    "ref": entry.template_id,
                    "quote": _answer_key_quote(
                        entry.standard_sql,
                        entry.expected_rows,
                        entry.expected_points,
                    ),
                }
            ],
            "claims": [],
            "timestamp": self._clock().isoformat(),
        }
        if isinstance(student_profile, Mapping):
            profile_id = student_profile.get("profile_id")
            if isinstance(profile_id, str) and profile_id.strip():
                draft["student_profile_ref"] = profile_id
        draft.update(metadata)
        return draft

    def generate_assessment(
        self,
        template_id: str,
        *,
        diagnostic_difficulty: str | None = None,
        student_profile: Mapping[str, Any] | None = None,
        learning_report_summary: str | None = None,
    ) -> dict[str, Any]:
        """Build an independent graded-check draft from the approved task anchor.

        The catalog may route a knowledge point to a practice guide.  The assessment
        branch deliberately reuses the same query authority and answer-key evidence,
        while exposing only the single graded question contract expected downstream.
        """

        draft = deepcopy(
            self._generate(
                template_id,
                diagnostic_difficulty=diagnostic_difficulty,
                student_profile=student_profile,
                learning_report_summary=learning_report_summary,
            )
        )
        payload = draft["payload"]
        content = payload["content"]
        question = content.get("question") or content.get("contextualized_stem")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("assessment question must be a non-empty string")
        for field in (
            "guide_intro",
            "guide_steps",
            "completion_criteria",
            "guide_md",
        ):
            content.pop(field, None)
        content.update(
            {
                "event": "assessment_ready",
                "resource_kind": "graded_assessment",
                "questions": [
                    {
                        "id": str(content["template_id"]),
                        "prompt": question,
                        "difficulty": str(content["difficulty"]),
                    }
                ],
            }
        )
        payload["type"] = "quiz_set"
        return draft

    def generate_practice_guide(
        self,
        template_id: str,
        *,
        diagnostic_difficulty: str | None = None,
        student_profile: Mapping[str, Any] | None = None,
        learning_report_summary: str | None = None,
    ) -> dict[str, Any]:
        """Return a guide-shaped resource for every deterministic task anchor.

        Some frozen task anchors are graded ``quiz_set`` products.  The strict
        learning loop also requires an answer-safe practice guide.  This
        projection adds method scaffolding only; it never exposes standard SQL
        or expected rows and does not change template routing.
        """

        draft = deepcopy(
            self._generate(
                template_id,
                diagnostic_difficulty=diagnostic_difficulty,
                student_profile=student_profile,
                learning_report_summary=learning_report_summary,
            )
        )
        payload = draft["payload"]
        content = payload["content"]
        scaffolds = [
            dict(item)
            for item in prerequisite_scaffolds(
                str(content["knowledge_point"]),
                str(content["difficulty"]),
            )
        ]
        scaffold_steps = [
            "前置检查："
            + str(item["knowledge_point"])
            + "——"
            + str(item["learning_goal"])
            for item in scaffolds
        ]
        if payload["type"] == "practice_guide":
            content.setdefault("resource_kind", "guided_practice")
            if scaffolds and not content.get("prerequisite_scaffolds"):
                content["prerequisite_scaffolds"] = scaffolds
                content["guide_steps"] = [
                    *scaffold_steps,
                    *list(content.get("guide_steps", ())),
                ]
                content["guide_md"] = "\n\n".join(
                    (
                        str(content.get("guide_md", "")),
                        "## 前置知识检查\n\n"
                        + "\n".join(f"- {step}" for step in scaffold_steps),
                    )
                ).strip()
            return draft
        question = content.get("question") or content.get("contextualized_stem")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("practice guide question must be a non-empty string")
        content.pop("questions", None)
        guide_intro = "先确认题目对象、时间范围、比较维度和统计口径，再开始查询。"
        guide_steps = [
            *scaffold_steps,
            "从题目中识别需要返回的字段、筛选条件和分组维度。",
            "使用只读查询获得结果，并核对行数、单位与统计口径。",
            "依据查询结果中的字段和值形成结论，区分数据事实与业务推断。",
        ]
        completion_criteria = [
            "查询结果与题目对象、范围和统计口径一致。",
            "结论明确引用查询结果中的字段和值，且未把相关性写成因果性。",
        ]
        content.update(
            {
                "resource_kind": "guided_practice",
                "prerequisite_scaffolds": scaffolds,
                "guide_intro": guide_intro,
                "guide_steps": guide_steps,
                "completion_criteria": completion_criteria,
                "guide_md": "\n\n".join(
                    (
                        f"## 实操目标\n\n{question}",
                        f"## 开始前\n\n{guide_intro}",
                        "## 操作步骤\n\n"
                        + "\n".join(
                            f"{index}. {step}"
                            for index, step in enumerate(guide_steps, start=1)
                        ),
                        "## 完成标准\n\n"
                        + "\n".join(
                            f"- {criterion}" for criterion in completion_criteria
                        ),
                    )
                ),
            }
        )
        payload["type"] = "practice_guide"
        return draft

    def counter_evidence(
        self,
        misconception: str,
        wrong_attempts: int = 1,
        *,
        student_profile: Mapping[str, Any] | None = None,
        learning_report_summary: str | None = None,
    ) -> dict[str, Any]:
        try:
            entry = self._catalog.counter_evidence[misconception]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"unsupported misconception: {misconception}") from exc
        if (
            not isinstance(wrong_attempts, int)
            or isinstance(wrong_attempts, bool)
            or wrong_attempts < 1
        ):
            raise ValueError("wrong_attempts must be a positive integer")
        standard_stem = _render(
            entry.question_template, self._catalog.demo_parameters
        )
        display, metadata = self._contextualize(
            standard_stem=standard_stem,
            parameter_values=_parameter_values(
                entry.question_template, self._catalog.demo_parameters
            ),
            student_profile=student_profile,
            learning_report_summary=learning_report_summary,
            query_authority=None,
        )
        display["contextualized_stem"] = standard_stem
        question = standard_stem
        evidence = [
            {
                "kind": "quiz_answer_key",
                "ref": f"{entry.misconception}:{index}",
                "quote": _answer_key_quote(
                    standard_sql,
                    entry.expected_results[index - 1],
                    entry.expected_points,
                ),
            }
            for index, standard_sql in enumerate(entry.standard_sqls, start=1)
        ]
        draft = {
            "trace_id": self._trace_id,
            "agent": "task",
            "role": "probe",
            "payload": {
                "type": "quiz_set",
                "content": {
                    "event": "counter_evidence_ready",
                    "misconception": entry.misconception,
                    "family": entry.family,
                    "question": question,
                    **display,
                },
            },
            "evidence": evidence,
            "claims": [],
            "probe": {
                "wrong_attempts": wrong_attempts,
                "questions": [question],
                "target_misconception": entry.misconception,
            },
            "timestamp": self._clock().isoformat(),
        }
        if isinstance(student_profile, Mapping):
            profile_id = student_profile.get("profile_id")
            if isinstance(profile_id, str) and profile_id.strip():
                draft["student_profile_ref"] = profile_id
        draft.update(metadata)
        return draft

    def _contextualize(
        self,
        *,
        standard_stem: str,
        parameter_values: tuple[str, ...],
        student_profile: Mapping[str, Any] | None,
        learning_report_summary: str | None,
        query_authority: QueryAuthority | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._llm_call is None:
            return self._fallback_display(
                standard_stem, "llm_disabled", llm_latency_ms=0
            ), {}

        valid_context = (
            isinstance(student_profile, Mapping)
            and isinstance(student_profile.get("lecture_style"), str)
            and bool(student_profile["lecture_style"].strip())
            and isinstance(learning_report_summary, str)
            and bool(learning_report_summary.strip())
        )
        if not valid_context:
            display = self._fallback_display(
                standard_stem, "missing_context", llm_latency_ms=0
            )
            return display, self._empty_llm_metadata(0)

        user_message = (
            "[标准题面]"
            + json.dumps(
                {
                    "standard_stem": standard_stem,
                    "parameter_values": list(parameter_values),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "[画像JSON]"
            + json.dumps(
                dict(student_profile),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "[当前学情摘要]"
            + learning_report_summary
        )
        started = perf_counter()
        try:
            result = self._llm_call(
                model=MODEL,
                system=self._system_prompt.replace(
                    "{ship}{month}{process}", "、".join(parameter_values)
                ),
                user=user_message,
                json_schema=TASK_CONTEXT_OUTPUT_SCHEMA,
                temperature=TEMPERATURE,
            )
        except Exception:
            elapsed_ms = round(max(0.0, perf_counter() - started) * 1000)
            return (
                self._fallback_display(
                    standard_stem, "llm_failure", llm_latency_ms=elapsed_ms
                ),
                self._empty_llm_metadata(elapsed_ms),
            )

        contextualized_stem = result.data.get("contextualized_stem")
        guide_intro = result.data.get("guide_intro")
        structurally_valid = (
            isinstance(contextualized_stem, str)
            and bool(contextualized_stem.strip())
            and isinstance(guide_intro, str)
            and bool(guide_intro.strip())
        )
        parameters_preserved = structurally_valid and all(
            value in contextualized_stem for value in parameter_values
        )
        if not parameters_preserved:
            reason = "parameter_mismatch" if structurally_valid else "invalid_output"
            display = self._fallback_display(
                standard_stem, reason, llm_latency_ms=result.latency_ms
            )
        elif (
            semantic_issue := self._contextualization_semantic_issue(
                contextualized_stem, query_authority
            )
        ) is not None:
            display = self._fallback_display(
                standard_stem,
                semantic_issue,
                llm_latency_ms=result.latency_ms,
            )
        else:
            display = {
                "standard_stem": standard_stem,
                "contextualized_stem": contextualized_stem,
                "guide_intro": guide_intro,
                "contextualize_fallback": False,
                "contextualize_fallback_reason": None,
                "llm_latency_ms": result.latency_ms,
            }
        elapsed_ms = round(max(0.0, perf_counter() - started) * 1000)
        return display, {
            "model": result.model,
            "latency_ms": max(elapsed_ms, result.latency_ms),
            "token_usage": result.token_usage.as_dict(),
        }

    def _contextualization_semantic_issue(
        self,
        contextualized_stem: str,
        query_authority: QueryAuthority | None,
    ) -> str | None:
        if query_authority is None:
            return None
        mentioned_metrics = {
            metric
            for metric, terms in self._catalog.metric_text_terms.items()
            if any(term.casefold() in contextualized_stem.casefold() for term in terms)
        }
        standard_metrics = {
            metric
            for metric, terms in self._catalog.metric_text_terms.items()
            if any(
                term.casefold() in query_authority.standard_stem.casefold()
                for term in terms
            )
        }
        allowed_metrics = set(query_authority.metric_columns)
        if (
            not standard_metrics.issubset(mentioned_metrics)
            or not mentioned_metrics.issubset(allowed_metrics)
        ):
            return "metric_semantic_mismatch"
        if (
            query_authority.time_column == "period_date"
            and _MONTH_AS_BATCH_RE.search(contextualized_stem)
        ):
            return "time_semantic_mismatch"
        return None

    @staticmethod
    def _fallback_display(
        standard_stem: str, reason: str, *, llm_latency_ms: int
    ) -> dict[str, Any]:
        return {
            "standard_stem": standard_stem,
            "contextualized_stem": standard_stem,
            "guide_intro": "",
            "contextualize_fallback": True,
            "contextualize_fallback_reason": reason,
            "llm_latency_ms": llm_latency_ms,
        }

    @staticmethod
    def _empty_llm_metadata(latency_ms: int) -> dict[str, Any]:
        return {
            "model": MODEL,
            "latency_ms": latency_ms,
            "token_usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }
