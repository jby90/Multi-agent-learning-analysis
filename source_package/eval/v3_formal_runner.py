"""Formal v3 production-route runner with an external frozen learner actor.

The manager receives only a profile, five pretest answers, optional frozen
probe answers and ordinary learner submissions.  Gold expectations are owned
by ``GoldLearnerActor`` and are never passed to diagnosis or task routing.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
from typing import Any, Mapping

from eval.v3_cases import load_formal_cases, load_gold_standard
from orchestrator.interactive_session import InteractiveSessionManager


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / "eval" / "results" / "v3_formal"
TASK_TEMPLATE_PATH = (
    ROOT / "config" / "domains" / "production_progress" / "task_templates.json"
)
# A formal case can legitimately retain the same learner turn while the
# production review flow retries or safely interrupts a candidate.  Keep a
# finite ceiling, but leave enough room for four learner rounds plus one T17
# remediation cycle without turning review retries into false case failures.
MAX_ACTIONS = 160


@lru_cache(maxsize=1)
def _runtime_task_sql() -> dict[str, str]:
    """Index production task SQL by the task actually shown to the learner."""

    raw = json.loads(TASK_TEMPLATE_PATH.read_text(encoding="utf-8"))
    templates = raw.get("templates") if isinstance(raw, Mapping) else None
    if not isinstance(templates, list):
        raise ValueError("production task template registry is invalid")
    indexed: dict[str, str] = {}
    for item in templates:
        if not isinstance(item, Mapping):
            continue
        template_id = str(item.get("template_id") or "").strip()
        sql = str(item.get("standard_sql") or "").strip()
        if template_id and sql:
            if template_id in indexed:
                raise ValueError(f"duplicate production task template: {template_id}")
            indexed[template_id] = sql
    return indexed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_version() -> str:
    configured = os.environ.get("V3_CODE_VERSION")
    if configured:
        return configured.strip()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        version_path = ROOT / "VERSION.txt"
        return version_path.read_text(encoding="utf-8").strip() + "+uncommitted-v3"
    return result.stdout.strip()


def _content(message: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = message.get("payload")
    value = payload.get("content") if isinstance(payload, Mapping) else None
    return value if isinstance(value, Mapping) else {}


class GoldLearnerActor:
    """External deterministic learner used only by the formal harness."""

    def __init__(self, gold: Mapping[str, Any]) -> None:
        self._gold = dict(gold)
        self._follow_up_attempt = 0

    @property
    def sql(self) -> str:
        value = self._gold.get("标准SQL")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("frozen learner actor requires 标准SQL")
        return value.strip()

    def sql_for_state(self, state: Mapping[str, Any]) -> str:
        """Solve the runtime task currently visible to the external learner.

        Formal gold rows describe the case-level target, while a production
        route may first display a different difficulty template.  Selecting
        SQL from the observed ``template_id`` follows that real task and does
        not pass a route or template into the production manager.
        """

        for message in reversed(list(state.get("messages", []))):
            if not isinstance(message, Mapping):
                continue
            content = _content(message)
            if content.get("event") not in {"product_ready", "assessment_ready"}:
                continue
            template_id = str(content.get("template_id") or "").strip()
            if not template_id:
                continue
            sql = _runtime_task_sql().get(template_id)
            if sql:
                return sql
        return self.sql

    def follow_up_answer(self, state: Mapping[str, Any], script_id: str) -> str:
        self._follow_up_attempt += 1
        evidence = self._query_evidence(state)
        if script_id == "S-REBUTTAL" and self._follow_up_attempt == 1:
            return self._evidence_bearing_wrong_answer(evidence)
        if script_id == "S-DOWNSTEP" and not _has_path_action(state, "step_down"):
            # T17 remains the production source of truth.  The external learner
            # keeps answering incorrectly until the real session emits the
            # frozen down-step action; the harness never injects a difficulty.
            return self._evidence_bearing_wrong_answer(evidence)

        point = str(self._gold.get("预期要点") or "")
        return f"根据刚才查询结果中的字段和值：{evidence}。据此判断：{point}。"

    @staticmethod
    def _evidence_bearing_wrong_answer(evidence: str) -> str:
        """Return an admissible but factually wrong learner turn.

        Production correctly rejects vacuous answers such as ``不知道`` before
        review.  Formal wrong-answer scenarios must therefore exercise the real
        reviewer with a field/value claim instead of mistaking input validation
        for a failed case.
        """

        return (
            f"根据刚才查询结果中的字段和值：{evidence}。"
            "我判断完成率为9999%，因此所有工序均已正常完成。"
        )

    @staticmethod
    def _query_evidence(state: Mapping[str, Any]) -> str:
        rows: list[Any] = []
        for message in state.get("messages", []):
            if not isinstance(message, Mapping):
                continue
            content = _content(message)
            if content.get("event") == "query_completed" and isinstance(content.get("rows"), list):
                rows = list(content["rows"])
        labels = {
            "process_code": "工序",
            "complete_rate": "完成率",
            "plan_qty": "计划量",
            "actual_qty": "实际完成量",
            "month_label": "月份",
            "variance_rate": "偏差率",
            "risk_level": "风险等级",
            "unit_name": "责任单元",
            "ship_no": "船号",
            "period_date": "日期",
        }
        rendered_rows: list[str] = []
        for row in rows[-12:]:
            if not isinstance(row, Mapping):
                continue
            rendered_rows.append(
                "，".join(
                    f"{labels.get(str(key), str(key))}为{value}"
                    for key, value in row.items()
                )
            )
        return "；".join(rendered_rows)


def _event_contents(state: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any], Mapping[str, Any]]]:
    values: list[tuple[int, Mapping[str, Any], Mapping[str, Any]]] = []
    for message in state.get("messages", []):
        if not isinstance(message, Mapping):
            continue
        values.append((int(message.get("step") or 0), message, _content(message)))
    return values


def _has_path_action(state: Mapping[str, Any], action: str) -> bool:
    return any(
        content.get("difficulty_action") == action
        for _, _, content in _event_contents(state)
    )


def _scenario_reached(state: Mapping[str, Any], script_id: str) -> bool:
    events = _event_contents(state)
    assessments = [
        (step, str(content.get("assessment") or ""))
        for step, _, content in events
        if content.get("event") == "learner_follow_up_assessed"
    ]
    mastered_steps = [step for step, assessment in assessments if assessment == "mastered"]
    wrong_steps = [step for step, assessment in assessments if assessment != "mastered"]
    if script_id in {"S-KEEP-B", "S-KEEP-A"}:
        return bool(mastered_steps)
    if script_id == "S-STEP-UP":
        step_up_steps = [
            step
            for step, _, content in events
            if content.get("difficulty_action") == "step_up"
        ]
        if not step_up_steps:
            return False
        return any(
            step > step_up_steps[-1]
            and content.get("event") == "product_ready"
            and content.get("difficulty") == "advanced"
            for step, _, content in events
        )
    if script_id == "S-REBUTTAL":
        return bool(wrong_steps and mastered_steps and mastered_steps[-1] > wrong_steps[0])
    if script_id == "S-DOWNSTEP":
        step_down_steps = [
            step
            for step, _, content in events
            if content.get("difficulty_action") == "step_down"
        ]
        return bool(
            step_down_steps
            and mastered_steps
            and mastered_steps[-1] > step_down_steps[-1]
        )
    raise ValueError(f"unsupported frozen learner script: {script_id}")


class FormalCaseRunner:
    """Drive one production session without route or template injection."""

    def __init__(
        self,
        manager: Any,
        *,
        actor: GoldLearnerActor,
        run_id: str,
        seed_id: str,
        output_dir: Path,
        code_version: str,
        prompt_version: str = "v3-frozen",
        model_config: Mapping[str, Any] | None = None,
    ) -> None:
        self.manager = manager
        self.actor = actor
        self.run_id = run_id
        self.seed_id = seed_id
        self.output_dir = Path(output_dir)
        self.code_version = code_version
        self.prompt_version = prompt_version
        self.model_config = dict(model_config or {})

    def run_case(self, case: Mapping[str, Any]) -> dict[str, Any]:
        if case.get("route_mode") != "production":
            raise ValueError("formal v3 cases must use production routing")
        forbidden = {"knowledge_point", "template_id", "forced_knowledge_point", "forced_template_id"}
        leaked = sorted(forbidden.intersection(case))
        if leaked:
            raise ValueError(f"formal case contains forbidden route injection: {leaked}")
        case_id = str(case["case_id"])
        started = _now()
        state = self.manager.create_session(
            str(case["profile_id"]),
            experience_tags=tuple(str(value) for value in case.get("experience_tags", [])),
        )
        session_id = str(state["session_id"])
        script_id = str(case["learner_script_id"])
        probe_answers = {
            str(item["probe_id"]): str(item["answer"])
            for item in case.get("diagnostic_probe_answers", [])
        }
        remaining_probe_answers = dict(probe_answers)
        actions = 0
        while actions < MAX_ACTIONS:
            if _scenario_reached(state, script_id):
                if remaining_probe_answers:
                    raise RuntimeError(
                        f"{case_id} reached scenario before consuming frozen probes: "
                        f"{sorted(remaining_probe_answers)}"
                    )
                break
            actions += 1
            awaiting = str(state.get("awaiting") or "")
            if awaiting == "pretest":
                state = self.manager.submit_pretest(
                    session_id, dict(case["pretest_answers"])
                )
                continue
            if awaiting == "diagnostic_probe":
                displayed = self.manager.get_diagnostic_probes(session_id)
                displayed_ids = {str(item["probe_id"]) for item in displayed}
                if not displayed_ids or not displayed_ids.issubset(remaining_probe_answers):
                    raise RuntimeError(
                        f"{case_id} frozen probe mismatch: displayed={sorted(displayed_ids)} "
                        f"remaining_input={sorted(remaining_probe_answers)}"
                    )
                state = self.manager.submit_diagnostic_probes(
                    session_id,
                    {
                        probe_id: remaining_probe_answers.pop(probe_id)
                        for probe_id in displayed_ids
                    },
                )
                continue
            if awaiting == "advance":
                state = self.manager.advance(session_id)
                continue
            if awaiting == "sql":
                state = self.manager.submit_sql(
                    session_id,
                    self.actor.sql_for_state(state),
                )
                continue
            if awaiting == "follow_up":
                answer = self.actor.follow_up_answer(state, script_id)
                state = self.manager.submit_follow_up(
                    session_id,
                    answer,
                    f"{case_id}-turn-{actions:02d}",
                )
                continue
            if awaiting == "done":
                if remaining_probe_answers:
                    raise RuntimeError(
                        f"{case_id} did not consume frozen probes: "
                        f"{sorted(remaining_probe_answers)}"
                    )
                break
            raise RuntimeError(f"{case_id} unsupported awaiting state: {awaiting}")
        else:
            raise RuntimeError(f"{case_id} exceeded {MAX_ACTIONS} formal learner actions")

        scenario_reached = _scenario_reached(state, script_id)
        if not scenario_reached:
            raise RuntimeError(f"{case_id} ended before its frozen scenario was observed")
        status = "completed_scenario"
        envelope = {
            "run_id": self.run_id,
            "seed_id": self.seed_id,
            "case_id": case_id,
            "session_id": session_id,
            "trace_id": str(state.get("trace_id") or ""),
            "route_mode": "production",
            "code_version": self.code_version,
            "prompt_version": self.prompt_version,
            "model_config": self.model_config,
            "profile_id": str(case["profile_id"]),
            "experience_tags": list(case.get("experience_tags", [])),
            "learner_script_id": script_id,
            "started_at": started,
            "finished_at": _now(),
            "status": status,
            "outcome": state.get("outcome"),
            "terminal_state": state.get("state"),
            "actions": actions,
            "scenario_reached": scenario_reached,
            "messages": list(state.get("messages") or []),
        }
        case_dir = self.output_dir / "cases" / case_id
        _atomic_json(case_dir / "case_result.json", envelope)
        normalized_path = case_dir / "trace_v3.jsonl"
        normalized_path.parent.mkdir(parents=True, exist_ok=True)
        with normalized_path.open("w", encoding="utf-8", newline="") as target:
            for message in envelope["messages"]:
                enriched = {
                    "run_id": self.run_id,
                    "seed_id": self.seed_id,
                    "case_id": case_id,
                    "session_id": session_id,
                    "route_mode": "production",
                    "code_version": self.code_version,
                    "prompt_version": self.prompt_version,
                    "model_config": self.model_config,
                    **dict(message),
                }
                target.write(json.dumps(enriched, ensure_ascii=False, sort_keys=True) + "\n")
        return envelope


def run_seed(
    seed_id: str,
    output_dir: Path,
    *,
    mode: str = "live",
    case_id: str | None = None,
) -> dict[str, Any]:
    if seed_id not in {"seed_A", "seed_B"}:
        raise ValueError("seed_id must be seed_A or seed_B")
    seed_value = 20260807 if seed_id == "seed_A" else 20260808
    random.seed(seed_value)
    output_dir = Path(output_dir)
    trace_dir = output_dir / "raw_traces"
    cache_dir = output_dir / "llm_cache"
    manager = InteractiveSessionManager(
        trace_dir=trace_dir,
        cache_dir=cache_dir,
        mode=mode,
    )
    inputs = [asdict(case) for case in load_formal_cases()]
    gold = load_gold_standard()
    if case_id:
        inputs = [row for row in inputs if row["case_id"] == case_id]
        if not inputs:
            raise ValueError(f"unknown formal case_id: {case_id}")
    run_id = f"V3-{seed_id.upper()}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    code_version = _git_version()
    model_config = {
        "mode": mode,
        "model": os.environ.get("REF_LLM_MODEL", "environment_default"),
        "temperature": os.environ.get("REF_LLM_TEMPERATURE", "configured_by_runtime"),
    }
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    def write_manifest() -> dict[str, Any]:
        manifest = {
            "run_id": run_id,
            "seed_id": seed_id,
            "route_mode": "production",
            "case_count": len(inputs),
            "executed_count": len(results) + len(failures),
            "completed_count": sum(
                result["status"].startswith("completed") for result in results
            ),
            "failed_count": len(failures),
            "code_version": code_version,
            "formal_input_sha256": _sha256(
                ROOT / "eval" / "cases" / "v3_1" / "formal_50_inputs_v3_1.json"
            ),
            "gold_sha256": _sha256(
                ROOT / "eval" / "gold" / "v3_1" / "formal_50_gold_v3_1.json"
            ),
            "cases": [
                {
                    "case_id": result["case_id"],
                    "session_id": result["session_id"],
                    "trace_id": result["trace_id"],
                    "status": result["status"],
                }
                for result in results
            ],
            "failures": list(failures),
        }
        _atomic_json(output_dir / "run_manifest.json", manifest)
        return manifest

    for case in inputs:
        runner = FormalCaseRunner(
            manager,
            actor=GoldLearnerActor(gold[str(case["case_id"])]),
            run_id=run_id,
            seed_id=seed_id,
            output_dir=output_dir,
            code_version=code_version,
            model_config=model_config,
        )
        try:
            results.append(runner.run_case(case))
        except Exception as exc:  # keep an auditable full-seed attempt ledger
            failures.append(
                {
                    "case_id": str(case["case_id"]),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        write_manifest()
    return write_manifest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", required=True, choices=("seed_A", "seed_B"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--mode", choices=("live", "cached"), default="live")
    parser.add_argument("--case-id")
    args = parser.parse_args()
    seed_dir = args.output_dir / args.seed
    manifest = run_seed(args.seed, seed_dir, mode=args.mode, case_id=args.case_id)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["completed_count"] == manifest["case_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
