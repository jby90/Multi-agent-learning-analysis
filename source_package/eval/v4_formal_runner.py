"""Production-faithful formal runner for the flexible v4 50-case library.

Unlike the historical v3 harness, a v4 target may sit behind prerequisite
knowledge units.  This runner therefore follows ``continue_learning`` across
real production sessions, solves every preceding unit from the task currently
shown by the system, and applies the frozen learner script only when the
independent gold target is actually active.  Neither a knowledge point nor a
template id is ever passed into the production manager.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
from typing import Any, Mapping

from eval.v3_formal_runner import (
    GoldLearnerActor,
    _atomic_json,
    _content,
    _git_version,
    _has_path_action,
    _runtime_task_contracts,
    _sha256,
)
from eval.v4_cases import (
    GOLD_PATH,
    INPUT_PATH,
    load_formal_cases,
    load_gold_standard,
)
from orchestrator.interactive_session import InteractiveSessionManager


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / "eval" / "results" / "v4_formal"
MAX_CASE_ACTIONS = 1200
MAX_SESSION_ACTIONS = 180


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_knowledge_point(state: Mapping[str, Any]) -> str:
    contract = state.get("learning_contract")
    if isinstance(contract, Mapping):
        points = contract.get("target_knowledge_points")
        if isinstance(points, list) and points and isinstance(points[0], str):
            return points[0].strip()
    bundle = state.get("evidence_bundle")
    if isinstance(bundle, Mapping) and isinstance(bundle.get("knowledge_point"), str):
        return str(bundle["knowledge_point"]).strip()
    report = state.get("training_report")
    if isinstance(report, Mapping) and isinstance(report.get("knowledge_point"), str):
        return str(report["knowledge_point"]).strip()
    for message in reversed(list(state.get("messages") or [])):
        if not isinstance(message, Mapping):
            continue
        value = _content(message).get("knowledge_point")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _latest_runtime_contract(state: Mapping[str, Any]) -> Mapping[str, Any]:
    for message in reversed(list(state.get("messages") or [])):
        if not isinstance(message, Mapping):
            continue
        content = _content(message)
        if content.get("event") not in {"product_ready", "assessment_ready"}:
            continue
        template_id = str(content.get("template_id") or "").strip()
        contract = _runtime_task_contracts().get(template_id)
        if contract is not None:
            return contract
    return {}


class V4LearnerActor(GoldLearnerActor):
    """External actor that only diverges from correctness at the gold target."""

    def __init__(self, gold: Mapping[str, Any]) -> None:
        super().__init__(
            {
                "case_id": gold.get("case_id"),
                "标准SQL": gold.get("initial_standard_sql"),
                "预期要点": "；".join(
                    str(item) for item in gold.get("initial_expected_points", [])
                ),
            }
        )
        self.gold = dict(gold)
        self.target_attempts = 0

    @property
    def target(self) -> str:
        return str(self.gold["target_knowledge_point"])

    @staticmethod
    def _latest_question(state: Mapping[str, Any]) -> str:
        for message in reversed(list(state.get("messages") or [])):
            if not isinstance(message, Mapping):
                continue
            content = _content(message)
            if content.get("event") != "follow_up_question_ready":
                continue
            question = content.get("question")
            if isinstance(question, str) and question.strip():
                return question.strip()
        return ""

    @staticmethod
    def _latest_query_rows(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        for message in reversed(list(state.get("messages") or [])):
            if not isinstance(message, Mapping):
                continue
            content = _content(message)
            rows = content.get("rows")
            if content.get("event") == "query_completed" and isinstance(rows, list):
                return [item for item in rows if isinstance(item, Mapping)]
        return []

    def _question_aware_answer(self, state: Mapping[str, Any]) -> str | None:
        """Answer the visible question from visible query rows only.

        Formal cases exercise dynamically generated follow-up questions.  A
        fixed task-level expected point (for example ``3行月序列``) is not an
        answer to a later question about direction or persistence.  This
        external actor may use exactly what a learner can see — the current
        question and reviewed result rows — but never hidden gold labels.
        """

        question = self._latest_question(state)
        rows = self._latest_query_rows(state)
        if not question or not rows:
            return None
        month_rows = [
            item
            for item in rows
            if item.get("month_label") is not None
            and item.get("complete_rate") is not None
        ]
        if len(month_rows) < 2 or not any(
            token in question
            for token in ("月份", "月度", "相邻", "趋势", "序列", "变化")
        ):
            return None
        ordered = sorted(month_rows, key=lambda item: str(item["month_label"]))
        bindings = "；".join(
            f"月份字段{item['month_label']}的完成率字段为{item['complete_rate']}"
            for item in ordered
        )
        try:
            rates = [float(item["complete_rate"]) for item in ordered]
        except (TypeError, ValueError):
            return f"根据查询结果：{bindings}。"
        if "最明显" in question or "变化最大" in question:
            deltas = [
                abs(right - left) for left, right in zip(rates, rates[1:])
            ]
            index = max(range(len(deltas)), key=deltas.__getitem__)
            previous = ordered[index]
            current = ordered[index + 1]
            direction = "下降" if rates[index + 1] < rates[index] else "上升"
            return (
                f"根据月份字段和完成率字段，{current['month_label']}相对"
                f"{previous['month_label']}的变化最明显：完成率由"
                f"{previous['complete_rate']}{direction}到"
                f"{current['complete_rate']}。"
            )
        if all(left > right for left, right in zip(rates, rates[1:])):
            conclusion = "相邻月份完成率连续下降，属于持续下降趋势，不是单期孤立波动"
        elif all(left < right for left, right in zip(rates, rates[1:])):
            conclusion = "相邻月份完成率连续上升，属于持续上升趋势，不是单期孤立波动"
        else:
            conclusion = "相邻月份有升有降，属于阶段性波动，不能认定为单一持续趋势"
        return f"根据查询结果：{bindings}。因此，{conclusion}。"

    def follow_up_answer_for_state(
        self,
        state: Mapping[str, Any],
        script_id: str,
    ) -> str:
        current = _current_knowledge_point(state)
        contract = _latest_runtime_contract(state)
        points = [str(item) for item in contract.get("expected_points", [])]
        if not points and current == self.target:
            difficulty = str(state.get("current_difficulty") or "")
            key = (
                "final_expected_points"
                if difficulty == str(self.gold.get("expected_final_difficulty"))
                else "initial_expected_points"
            )
            points = [str(item) for item in self.gold.get(key, [])]
        conclusion = "；".join(points) or "查询结果中的字段和值支持该判断"
        evidence = self._query_evidence(state)

        if current != self.target:
            return self._bounded_answer(evidence, conclusion)

        self.target_attempts += 1
        if script_id == "S-REBUTTAL":
            # A rejected artefact can return to ``follow_up`` without creating
            # a learner assessment.  Count the production assessment event,
            # not actor invocations, otherwise a review retry consumes the
            # one frozen wrong turn and the rebuttal scenario is never tested.
            target_assessments = [
                str(_content(message).get("assessment") or "")
                for message in state.get("messages", [])
                if isinstance(message, Mapping)
                and _content(message).get("event")
                == "learner_follow_up_assessed"
            ]
            if not target_assessments:
                return self._evidence_bearing_wrong_answer(evidence)
        if script_id == "S-DOWNSTEP" and not _has_path_action(state, "step_down"):
            return self._evidence_bearing_wrong_answer(evidence)
        if script_id == "S-REFRESH" and not _has_path_action(state, "refresh"):
            return self._evidence_bearing_wrong_answer(evidence)
        question_aware = self._question_aware_answer(state)
        if question_aware is not None:
            return question_aware
        return self._bounded_answer(evidence, conclusion)


def _target_scenario_completed(
    state: Mapping[str, Any],
    gold: Mapping[str, Any],
    script_id: str,
) -> bool:
    """Require a completed target unit, not merely a generated next task."""

    if state.get("awaiting") != "done" or state.get("outcome") != "completed":
        return False
    report = state.get("training_report")
    if not isinstance(report, Mapping):
        return False
    if str(report.get("knowledge_point") or "") != str(gold["target_knowledge_point"]):
        return False
    if str(report.get("final_difficulty") or "") != str(
        gold["expected_final_difficulty"]
    ):
        return False
    required_action = {
        "S-STEP-UP-B": "step_up",
        "S-STEP-UP-A": "step_up",
        "S-REBUTTAL": "step_up",
        "S-DOWNSTEP": "step_down",
        "S-REFRESH": "refresh",
    }[script_id]
    if not _has_path_action(state, required_action):
        return False
    if script_id in {"S-REBUTTAL", "S-DOWNSTEP", "S-REFRESH"}:
        assessments = [
            str(_content(message).get("assessment") or "")
            for message in state.get("messages", [])
            if isinstance(message, Mapping)
            and _content(message).get("event") == "learner_follow_up_assessed"
        ]
        if not assessments or all(item == "mastered" for item in assessments):
            return False
        if "mastered" not in assessments:
            return False
    return True


def validate_v4_gold(
    cases: list[Mapping[str, Any]],
    gold: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Bind both declared target stages to current production contracts."""

    conflicts: list[dict[str, Any]] = []
    contracts = _runtime_task_contracts()
    for case in cases:
        case_id = str(case.get("case_id") or "")
        expected = gold.get(case_id)
        if not isinstance(expected, Mapping):
            conflicts.append({"case_id": case_id, "reason": "missing_gold"})
            continue
        for stage in ("initial", "final"):
            template_id = str(expected.get(f"expected_{stage}_template") or "")
            contract = contracts.get(template_id)
            mismatches: list[str] = []
            if not isinstance(contract, Mapping):
                mismatches.append("template_missing")
            else:
                checks = {
                    "knowledge_point": (
                        str(contract.get("knowledge_point") or ""),
                        str(expected.get("target_knowledge_point") or ""),
                    ),
                    "difficulty": (
                        str(contract.get("difficulty") or ""),
                        str(expected.get(f"expected_{stage}_difficulty") or ""),
                    ),
                    "standard_sql": (
                        " ".join(str(contract.get("standard_sql") or "").split()),
                        " ".join(str(expected.get(f"{stage}_standard_sql") or "").split()),
                    ),
                    "expected_rows": (
                        contract.get("expected_rows"),
                        expected.get(f"{stage}_expected_rows"),
                    ),
                    "expected_points": (
                        [str(item) for item in contract.get("expected_points", [])],
                        [str(item) for item in expected.get(f"{stage}_expected_points", [])],
                    ),
                }
                mismatches.extend(
                    field for field, (actual, wanted) in checks.items() if actual != wanted
                )
            if mismatches:
                conflicts.append(
                    {
                        "case_id": case_id,
                        "stage": stage,
                        "template_id": template_id,
                        "mismatches": mismatches,
                    }
                )
    return conflicts


class V4FormalCaseRunner:
    def __init__(
        self,
        manager: Any,
        *,
        actor: V4LearnerActor,
        run_id: str,
        seed_id: str,
        output_dir: Path,
        code_version: str,
        model_config: Mapping[str, Any] | None = None,
    ) -> None:
        self.manager = manager
        self.actor = actor
        self.run_id = run_id
        self.seed_id = seed_id
        self.output_dir = Path(output_dir)
        self.code_version = code_version
        self.model_config = dict(model_config or {})

    def run_case(self, case: Mapping[str, Any]) -> dict[str, Any]:
        if case.get("route_mode") != "production":
            raise ValueError("formal v4 cases must use production routing")
        forbidden = {
            "knowledge_point", "template_id", "forced_knowledge_point",
            "forced_template_id", "target_knowledge_point",
        }
        leaked = sorted(forbidden.intersection(case))
        if leaked:
            raise ValueError(f"formal case contains forbidden route injection: {leaked}")

        case_id = str(case["case_id"])
        script_id = str(case["learner_script_id"])
        state = self.manager.create_session(
            str(case["profile_id"]),
            experience_tags=tuple(str(item) for item in case.get("experience_tags", [])),
        )
        session_ids = [str(state["session_id"])]
        session_records: list[dict[str, Any]] = []
        encountered_points: list[str] = []
        encountered_session_ids: set[str] = set()
        remaining_probe_answers = {
            str(item["probe_id"]): str(item["answer"])
            for item in case.get("diagnostic_probe_answers", [])
        }
        started = _now()
        actions = 0
        session_actions = 0

        while actions < MAX_CASE_ACTIONS:
            actions += 1
            session_actions += 1
            if session_actions > MAX_SESSION_ACTIONS:
                raise RuntimeError(
                    f"{case_id} exceeded {MAX_SESSION_ACTIONS} actions in one session"
                )
            session_id = str(state["session_id"])
            awaiting = str(state.get("awaiting") or "")
            current = _current_knowledge_point(state)
            if current and session_id not in encountered_session_ids:
                encountered_points.append(current)
                encountered_session_ids.add(session_id)

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
                        f"{case_id} probe mismatch: displayed={sorted(displayed_ids)} "
                        f"remaining={sorted(remaining_probe_answers)}"
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
                    session_id, self.actor.sql_for_state(state)
                )
                continue
            if awaiting == "follow_up":
                state = self.manager.submit_follow_up(
                    session_id,
                    self.actor.follow_up_answer_for_state(state, script_id),
                    f"{case_id}-{session_id}-turn-{actions:03d}",
                )
                continue
            if awaiting == "done":
                record = {
                    "session_id": session_id,
                    "trace_id": str(state.get("trace_id") or ""),
                    "knowledge_point": _current_knowledge_point(state),
                    "outcome": state.get("outcome"),
                    "training_report": state.get("training_report"),
                    "messages": list(state.get("messages") or []),
                }
                session_records.append(record)
                # Persist partial progress before any target assertion.  A
                # failed formal case must still reveal which real curriculum
                # units were reached and why the next hop was rejected.
                _atomic_json(
                    self.output_dir / "cases" / case_id / "progress.json",
                    {
                        "case_id": case_id,
                        "target_knowledge_point": self.actor.target,
                        "encountered_knowledge_points": encountered_points,
                        "session_ids": session_ids,
                        "last_session": record,
                    },
                )
                if _target_scenario_completed(state, self.actor.gold, script_id):
                    break
                if current == self.actor.target:
                    raise RuntimeError(
                        f"{case_id} target unit ended without its frozen scenario"
                    )
                state = self.manager.continue_learning(session_id)
                session_ids.append(str(state["session_id"]))
                session_actions = 0
                continue
            raise RuntimeError(f"{case_id} unsupported awaiting state: {awaiting}")
        else:
            raise RuntimeError(f"{case_id} exceeded {MAX_CASE_ACTIONS} actions")

        if remaining_probe_answers:
            raise RuntimeError(
                f"{case_id} did not consume probes: {sorted(remaining_probe_answers)}"
            )
        target_occurrences = [
            index + 1
            for index, point in enumerate(encountered_points)
            if point == self.actor.target
        ]
        expected_position = int(self.actor.gold["expected_target_plan_position"])
        if expected_position not in target_occurrences:
            raise RuntimeError(
                f"{case_id} target route position mismatch: "
                f"expected={expected_position}, encountered={encountered_points}"
            )

        envelope = {
            "run_id": self.run_id,
            "seed_id": self.seed_id,
            "case_id": case_id,
            "route_mode": "production",
            "code_version": self.code_version,
            "model_config": self.model_config,
            "profile_id": str(case["profile_id"]),
            "experience_tags": list(case.get("experience_tags", [])),
            "learner_script_id": script_id,
            "target_knowledge_point": self.actor.target,
            "encountered_knowledge_points": encountered_points,
            "session_ids": session_ids,
            "started_at": started,
            "finished_at": _now(),
            "status": "completed_target_scenario",
            "actions": actions,
            "sessions": session_records,
        }
        case_dir = self.output_dir / "cases" / case_id
        _atomic_json(case_dir / "case_result.json", envelope)
        trace_path = case_dir / "trace_v4.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        with trace_path.open("w", encoding="utf-8", newline="") as target:
            for session_index, record in enumerate(session_records, start=1):
                for message in record["messages"]:
                    target.write(
                        json.dumps(
                            {
                                "run_id": self.run_id,
                                "seed_id": self.seed_id,
                                "case_id": case_id,
                                "session_index": session_index,
                                "session_id": record["session_id"],
                                "target_knowledge_point": self.actor.target,
                                **dict(message),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n"
                    )
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
    random.seed(20260819 if seed_id == "seed_A" else 20260820)
    cases = [asdict(item) for item in load_formal_cases()]
    gold = load_gold_standard()
    if case_id:
        cases = [item for item in cases if item["case_id"] == case_id]
        if not cases:
            raise ValueError(f"unknown v4 case: {case_id}")
    conflicts = validate_v4_gold(cases, gold)
    if conflicts:
        _atomic_json(Path(output_dir) / "specification_conflicts.json", conflicts)
        raise ValueError(f"v4 gold conflicts with production contracts: {len(conflicts)}")

    output_dir = Path(output_dir)
    manager = InteractiveSessionManager(
        trace_dir=output_dir / "raw_traces",
        cache_dir=output_dir / "llm_cache",
        mode=mode,
        persona_routing=True,
    )
    run_id = f"V4-{seed_id.upper()}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    code_version = _git_version()
    model_config = {
        "mode": mode,
        "model": os.environ.get("REF_LLM_MODEL", "environment_default"),
        "temperature": os.environ.get(
            "REF_LLM_TEMPERATURE", "configured_by_runtime"
        ),
    }
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    def manifest() -> dict[str, Any]:
        value = {
            "run_id": run_id,
            "seed_id": seed_id,
            "route_mode": "production",
            "case_count": len(cases),
            "completed_count": len(results),
            "failed_count": len(failures),
            "code_version": code_version,
            "formal_input_sha256": _sha256(INPUT_PATH),
            "gold_sha256": _sha256(GOLD_PATH),
            "cases": [
                {
                    "case_id": item["case_id"],
                    "status": item["status"],
                    "session_count": len(item["session_ids"]),
                }
                for item in results
            ],
            "failures": failures,
        }
        _atomic_json(output_dir / "run_manifest.json", value)
        return value

    for case in cases:
        try:
            results.append(
                V4FormalCaseRunner(
                    manager,
                    actor=V4LearnerActor(gold[str(case["case_id"])]),
                    run_id=run_id,
                    seed_id=seed_id,
                    output_dir=output_dir,
                    code_version=code_version,
                    model_config=model_config,
                ).run_case(case)
            )
        except Exception as exc:
            failures.append(
                {
                    "case_id": str(case["case_id"]),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        manifest()
    return manifest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", required=True, choices=("seed_A", "seed_B"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--mode", choices=("live", "cached"), default="live")
    parser.add_argument("--case-id")
    args = parser.parse_args()
    report = run_seed(
        args.seed,
        args.output_dir / args.seed,
        mode=args.mode,
        case_id=args.case_id,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["completed_count"] == report["case_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
