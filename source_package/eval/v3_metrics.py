"""Frozen v3 TRACE normalization and five-metric recomputation.

The production runtime never imports this module.  Gold expectations and
human labels are merged only after a production trace has been written.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import json
from typing import Any


FACT_KINDS = frozenset({"fact", "data_conclusion"})
FACT_RULES = frozenset({"R-01", "R-02", "R-04", "R-05", "R-06"})
PUBLISHED_DECISIONS = frozenset({"approve", "approve_with_fix"})
PRODUCT_TYPES = frozenset(
    {"lecture_note", "practice_guide", "quiz_set", "sql_result", "feedback"}
)
VALID_MODES = frozenset({"AUTO_PRELIMINARY", "FINAL_HUMAN_REVIEWED"})


class FinalReviewIncomplete(ValueError):
    """Raised when final scoring would require invented human evidence."""


def _items(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _content(message: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = message.get("payload")
    value = payload.get("content") if isinstance(payload, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _payload_type(message: Mapping[str, Any]) -> str:
    payload = message.get("payload")
    value = payload.get("type") if isinstance(payload, Mapping) else ""
    return str(value or "")


def _stable_id(*parts: object, prefix: str) -> str:
    raw = "\x1f".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _run_value(run: Mapping[str, Any], name: str) -> str:
    return str(run.get(name) or "")


def _reviews(messages: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for message in messages:
        if message.get("role") not in {"verdict", "re_verdict"}:
            continue
        reviewed = _content(message).get("reviewed_msg_id")
        if isinstance(reviewed, str) and reviewed:
            result[reviewed].append(message)
    for values in result.values():
        values.sort(key=lambda item: int(item.get("step") or 0))
    return result


def _rule_hits(review: Mapping[str, Any] | None) -> tuple[str, ...]:
    if review is None:
        return ()
    verdict = review.get("verdict")
    hits = verdict.get("rule_hits") if isinstance(verdict, Mapping) else None
    return tuple(
        str(item["rule_id"])
        for item in _items(hits)
        if isinstance(item.get("rule_id"), str)
    )


def _decision(review: Mapping[str, Any] | None) -> str:
    verdict = review.get("verdict") if isinstance(review, Mapping) else None
    return str(verdict.get("decision") or "") if isinstance(verdict, Mapping) else ""


def _fact_candidate_text(product: Mapping[str, Any]) -> str:
    """Return a bounded factual premise for rejected products lacking claims.

    Question-like products can contain a factual premise that Review correctly
    rejects before the producer has emitted structured ``claims``.  Keeping the
    whole question as one reviewable candidate is conservative: human reviewers
    still decide whether it is a hallucination, while the rejected content no
    longer disappears from the auxiliary-KPI audit queue.
    """

    content = _content(product)
    for key in ("question", "feedback", "summary", "lecture_md"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _implicit_generation_transaction(
    run: Mapping[str, Any],
    product: Mapping[str, Any],
) -> tuple[str, ...] | None:
    """Group retry products from one follow-up turn without reading gold.

    Legacy follow-up messages do not carry ``generation_stage`` or a lineage
    identifier.  Their round, template and event are nevertheless stable across
    regeneration attempts, so they form a deterministic first-generation
    transaction key.  Products outside a numbered follow-up keep the historical
    one-product/one-transaction behaviour.
    """

    content = _content(product)
    follow_up_round = content.get("follow_up_round")
    if follow_up_round is None:
        return None
    return (
        _run_value(run, "session_id"),
        _payload_type(product),
        str(content.get("event") or ""),
        str(follow_up_round),
        str(content.get("template_id") or ""),
    )


def build_fact_units(
    runs: Sequence[Mapping[str, Any]],
    *,
    mode: str = "AUTO_PRELIMINARY",
) -> list[dict[str, Any]]:
    """Split produced artifacts into deduplicated claim-level fact units."""

    if mode not in VALID_MODES:
        raise ValueError(f"unsupported v3 recomputation mode: {mode}")
    rows: list[dict[str, Any]] = []
    for run in runs:
        messages = list(_items(run.get("messages")))
        reviews = _reviews(messages)
        seen_implicit_transactions: set[tuple[str, ...]] = set()
        products = [
            message
            for message in messages
            if message.get("role") in {"produce", "probe"}
            and _payload_type(message) in PRODUCT_TYPES
        ]
        for product in products:
            msg_id = str(product.get("msg_id") or "")
            product_reviews = reviews.get(msg_id, [])
            latest_review = product_reviews[-1] if product_reviews else None
            first_review = product_reviews[0] if product_reviews else None
            latest_decision = _decision(latest_review)
            first_hits = _rule_hits(first_review)
            latest_hits = _rule_hits(latest_review)
            all_hits = tuple(
                sorted({hit for review in product_reviews for hit in _rule_hits(review)})
            )
            artifact_id = str(
                _content(product).get("artifact_id")
                or _stable_id(
                    _run_value(run, "session_id"),
                    _payload_type(product),
                    msg_id,
                    prefix="ART",
                )
            )
            lineage_id = str(
                _content(product).get("lineage_id")
                or _content(product).get("artifact_lineage_id")
                or artifact_id
            )
            declared_generation_stage = str(
                _content(product).get("generation_stage") or ""
            )
            implicit_transaction = _implicit_generation_transaction(run, product)
            if declared_generation_stage:
                generation_stage = declared_generation_stage
            elif implicit_transaction is None:
                generation_stage = "first_generation"
            else:
                generation_stage = (
                    "revision"
                    if implicit_transaction in seen_implicit_transactions
                    else "first_generation"
                )
                seen_implicit_transactions.add(implicit_transaction)
            first_generation = int(generation_stage in {"first_generation", "initial", "native"})
            claims = [
                claim
                for claim in _items(product.get("claims"))
                if claim.get("kind") in FACT_KINDS
                and isinstance(claim.get("text"), str)
                and str(claim.get("text")).strip()
            ]
            candidate_text = _fact_candidate_text(product)
            if (
                not claims
                and candidate_text
                and FACT_RULES.intersection(all_hits)
                and any(_decision(review) == "reject" for review in product_reviews)
            ):
                claims = [{"kind": "fact_candidate", "text": candidate_text}]
            for claim_index, claim in enumerate(claims, start=1):
                text = str(claim["text"]).strip()
                unit_id = _stable_id(msg_id, claim_index, text, prefix="CU")
                lineage_unit_id = _stable_id(lineage_id, text, prefix="LU")
                evidence_ids = sorted({
                    str(item.get("ref"))
                    for item in _items(product.get("evidence"))
                    if item.get("ref")
                    and (
                        item.get("supports_claim") == text
                        or claim.get("kind") == "fact_candidate"
                    )
                })
                native_hallucination = bool(FACT_RULES.intersection(first_hits))
                final_hallucination = bool(FACT_RULES.intersection(latest_hits))
                auto_detected = int(bool(first_review) and native_hallucination)
                auto_intercepted = int(
                    auto_detected == 1
                    and (
                        _decision(first_review) == "reject"
                    )
                )
                published_final = int(latest_decision in PUBLISHED_DECISIONS)
                effective_hallucination = (
                    final_hallucination if published_final else native_hallucination
                )
                auto_label = (
                    "HALLUCINATION" if effective_hallucination else "SUPPORTED"
                )
                rows.append(
                    {
                        "run_id": _run_value(run, "run_id"),
                        "seed_id": _run_value(run, "seed_id"),
                        "case_id": _run_value(run, "case_id"),
                        "session_id": _run_value(run, "session_id"),
                        "artifact_id": artifact_id,
                        "artifact_version_id": msg_id,
                        "lineage_id": lineage_id,
                        "generation_stage": generation_stage,
                        "content_unit_id": unit_id,
                        "lineage_unit_id": lineage_unit_id,
                        "unit_kind": str(claim.get("kind")),
                        "content_text": text,
                        "evidence_ids": evidence_ids,
                        "published_final": published_final,
                        "human_label": "PENDING_HUMAN",
                        "hallucination_reason": "",
                        "first_generation": first_generation,
                        "auto_review_detected": auto_detected,
                        "auto_intercepted": auto_intercepted,
                        "review_event_id": (
                            str(first_review.get("msg_id")) if first_review else ""
                        ),
                        "rule_hits": list(all_hits),
                        "adjudicator": "PENDING_HUMAN",
                        "final_fact_denominator": int(published_final == 1),
                        "final_hallucination_numerator": int(
                            published_final == 1 and final_hallucination
                        ),
                        "native_error_denominator": int(
                            first_generation == 1 and native_hallucination
                        ),
                        "interception_numerator": int(
                            first_generation == 1
                            and native_hallucination
                            and auto_detected == 1
                            and auto_intercepted == 1
                        ),
                        "auto_label": auto_label,
                        "auto_hallucination_reason": ",".join(
                            sorted(
                                FACT_RULES.intersection(
                                    latest_hits if published_final else first_hits
                                )
                            )
                        ),
                    }
                )
    # A repeated review cannot duplicate a content unit.  Keep the latest
    # version per content_unit_id, but preserve separate native revisions.
    deduplicated = {row["content_unit_id"]: row for row in rows}
    return sorted(
        deduplicated.values(),
        key=lambda row: (row["seed_id"], row["case_id"], row["artifact_version_id"], row["content_unit_id"]),
    )


def _gold_by_case(gold_rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result = {str(row.get("case_id") or row.get("案例ID") or ""): row for row in gold_rows}
    result.pop("", None)
    return result


def _diagnosis(messages: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    for message in messages:
        if _payload_type(message) == "profile_assessment":
            return _content(message)
    return {}


def _path_actions(messages: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    values: list[Mapping[str, Any]] = []
    for message in messages:
        content = _content(message)
        action = content.get("difficulty_action")
        if isinstance(action, str) and action in {"keep", "step_up", "step_down", "deferred"}:
            values.append(message)
    return values


def _expected_post_action(value: str) -> str:
    parts = [part.strip() for part in value.split("→")]
    for item in reversed(parts):
        if item in {"keep", "step_up", "step_down", "deferred"}:
            return item
    return "keep"


def _submission_events(messages: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        message
        for message in messages
        if _content(message).get("event") in {"student_answer", "probe_outcome"}
        and _content(message).get("answer_result") in {"correct", "wrong"}
    ]


def _expected_submission_actions(gold: Mapping[str, Any]) -> list[list[str]]:
    sequence = str(gold.get("预期适配序列") or "")
    normalized_sequence = "".join(sequence.split())
    count = int(gold.get("预期适配节点数") or 2) - 1
    if normalized_sequence.count("wrong") >= 2 and "step_down" in normalized_sequence:
        actions = [["targeted_followup"], ["step_down"], ["keep"]]
    elif "wrong→targeted_followup/rebuttal" in normalized_sequence:
        actions = [["targeted_followup", "rebuttal"], ["keep"]]
    else:
        actions = [[_expected_post_action(sequence)]]
    while len(actions) < count:
        actions.append(["keep"])
    return actions[:count]


def _difficulty_from_content(content: Mapping[str, Any]) -> str:
    direct = content.get("difficulty") or content.get("new_difficulty")
    if isinstance(direct, str) and direct:
        return direct
    for key in ("learning_contract", "evidence_bundle"):
        nested = content.get(key)
        if isinstance(nested, Mapping):
            difficulty = nested.get("difficulty")
            if isinstance(difficulty, str) and difficulty:
                return difficulty
    return ""


def _adaptation_candidates(
    messages: Sequence[Mapping[str, Any]],
    initial_difficulty: str,
) -> list[dict[str, Any]]:
    """Collapse raw follow-up turns into auditable adaptation decisions.

    A formal gold row counts semantic nodes rather than every retry.  For
    example, several wrong attempts before T17 still represent one targeted
    follow-up node followed by one real step-down node.  This function derives
    those decisions exclusively from production events; it never reads gold.
    """

    ordered = sorted(messages, key=lambda item: int(item.get("step") or 0))
    assessed = [
        item
        for item in ordered
        if _content(item).get("event") == "learner_follow_up_assessed"
    ]
    candidates: list[dict[str, Any]] = []
    previous_assessment_step = 0
    current_difficulty = initial_difficulty
    for index, assessment_message in enumerate(assessed):
        assessment_step = int(assessment_message.get("step") or 0)
        next_assessment_step = (
            int(assessed[index + 1].get("step") or 0)
            if index + 1 < len(assessed)
            else None
        )
        submissions = [
            item
            for item in ordered
            if previous_assessment_step < int(item.get("step") or 0) <= assessment_step
            and _content(item).get("event") == "learner_follow_up_submitted"
        ]
        trigger = submissions[-1] if submissions else assessment_message
        submission_step = int(trigger.get("step") or 0)
        response_window = [
            item
            for item in ordered
            if submission_step < int(item.get("step") or 0) <= assessment_step
        ]
        consequence_window = [
            item
            for item in ordered
            if assessment_step < int(item.get("step") or 0)
            and (
                next_assessment_step is None
                or int(item.get("step") or 0) < next_assessment_step
            )
        ]
        path_event = next(
            (
                item
                for item in consequence_window
                if _content(item).get("difficulty_action")
                in {"step_up", "step_down", "deferred"}
            ),
            None,
        )
        assessment = str(_content(assessment_message).get("assessment") or "")
        if path_event is not None:
            action = str(_content(path_event).get("difficulty_action"))
            consequence = path_event
        elif assessment == "mastered":
            action = "keep"
            consequence = assessment_message
        else:
            rebuttal = next(
                (
                    item
                    for item in response_window + consequence_window
                    if _content(item).get("event") == "rebuttal_ready"
                ),
                None,
            )
            follow_up = next(
                (
                    item
                    for item in response_window + consequence_window
                    if _content(item).get("event") == "follow_up_question_ready"
                ),
                None,
            )
            action = "rebuttal" if rebuttal is not None else "targeted_followup"
            consequence = rebuttal or follow_up or assessment_message
        consequence_content = _content(consequence)
        downstream = consequence
        difficulty_evidence = consequence
        if path_event is not None:
            path_step = int(path_event.get("step") or 0)
            later_events = [
                item
                for item in consequence_window
                if int(item.get("step") or 0) > path_step
            ]
            downstream = next(
                (
                    item
                    for item in later_events
                    if _content(item).get("event")
                    in {"product_ready", "assessment_ready"}
                ),
                consequence,
            )
            difficulty_evidence = next(
                (
                    item
                    for item in later_events
                    if _difficulty_from_content(_content(item))
                ),
                downstream,
            )
        after_difficulty = (
            _difficulty_from_content(consequence_content)
            or _difficulty_from_content(_content(difficulty_evidence))
            or _difficulty_from_content(_content(downstream))
            or current_difficulty
        )
        candidates.append(
            {
                "action": action,
                "trigger": trigger,
                "consequence": consequence,
                "downstream": downstream,
                "before_difficulty": current_difficulty,
                "after_difficulty": after_difficulty,
                "assessment": assessment,
            }
        )
        current_difficulty = after_difficulty
        previous_assessment_step = assessment_step
    return candidates


def build_adaptation_nodes(
    runs: Sequence[Mapping[str, Any]],
    gold_rows: Sequence[Mapping[str, Any]],
    *,
    mode: str = "AUTO_PRELIMINARY",
) -> list[dict[str, Any]]:
    """Construct every due adaptation node and merge frozen expectations."""

    if mode not in VALID_MODES:
        raise ValueError(f"unsupported v3 recomputation mode: {mode}")
    gold = _gold_by_case(gold_rows)
    rows: list[dict[str, Any]] = []
    for run in runs:
        case_id = _run_value(run, "case_id")
        expected = gold.get(case_id)
        if expected is None:
            raise ValueError(f"missing frozen gold row for {case_id}")
        messages = list(_items(run.get("messages")))
        diagnosis = _diagnosis(messages)
        selected = str(diagnosis.get("selected_knowledge_point") or "")
        actual_initial = str(diagnosis.get("selected_difficulty") or "")
        expected_point = str(expected.get("目标知识点") or "")
        expected_initial = str(expected.get("预期初始难度") or "")
        plan = diagnosis.get("knowledge_point_plan")
        selected_plan = plan[0] if isinstance(plan, list) and plan and isinstance(plan[0], Mapping) else {}
        route_evidence = selected_plan.get("evidence_ids")
        route_reason = str(selected_plan.get("route_reason") or "")
        route_event = next(
            (message for message in messages if _payload_type(message) == "profile_assessment"),
            {},
        )
        first_transaction = _stable_id(
            _run_value(run, "session_id"), "teaching-first-generation", prefix="TX"
        )
        base = {
            "run_id": _run_value(run, "run_id"),
            "seed_id": _run_value(run, "seed_id"),
            "case_id": case_id,
            "session_id": _run_value(run, "session_id"),
            "knowledge_point": selected,
            "first_gen_transaction_id": first_transaction,
            "r03_reviewed_first_gen": 0,
            "r03_rejected_first_gen": 0,
            "human_mismatch_confirmed": "PENDING_HUMAN",
            "r03_repair_approved": 0,
            "final_residual_mismatch": "PENDING_HUMAN",
            "adjudicator": "PENDING_HUMAN",
        }
        reviews = _reviews(messages)
        first_product = next(
            (message for message in messages if _payload_type(message) in {"lecture_note", "practice_guide", "quiz_set"}),
            None,
        )
        if first_product is not None:
            first_product_reviews = reviews.get(str(first_product.get("msg_id") or ""), [])
            r03_hits = [hit for review in first_product_reviews for hit in _rule_hits(review) if hit == "R-03"]
            base["r03_reviewed_first_gen"] = int(bool(first_product_reviews))
            base["r03_rejected_first_gen"] = int(bool(r03_hits) and _decision(first_product_reviews[0]) == "reject")
            base["r03_repair_approved"] = int(
                base["r03_rejected_first_gen"] == 1
                and any(_decision(review) == "approve" for review in first_product_reviews[1:])
            )

        actual_route = f"initial_route({actual_initial})" if actual_initial else ""
        expected_route = f"initial_route({expected_initial})"
        route_ok = (
            _run_value(run, "route_mode") == "production"
            and selected == expected_point
            and actual_initial == expected_initial
            and isinstance(route_evidence, list)
            and bool(route_evidence)
            and bool(route_reason)
        )
        rows.append(
            {
                **base,
                "adaptation_node_id": _stable_id(case_id, "initial_route", prefix="AN"),
                "node_type": "initial_route",
                "trigger_event_id": str(route_event.get("msg_id") or ""),
                "trigger_event_type": "diagnosis_ready",
                "before_state_json": {},
                "expected_action": expected_route,
                "acceptable_actions": [expected_route],
                "actual_action": actual_route,
                "after_state_json": {"knowledge_point": selected, "difficulty": actual_initial},
                "decision_reason": route_reason,
                "downstream_artifact_id": str(first_product.get("msg_id") or "") if first_product else "",
                "expected_downstream_difficulty": expected_initial,
                "actual_downstream_difficulty": actual_initial,
                "due_node": 1,
                "trigger_binding_valid": int(bool(route_event)),
                "action_gold_match": int(actual_route == expected_route and selected == expected_point),
                "after_state_consistent": int(selected == expected_point and actual_initial == expected_initial),
                "downstream_executed": int(first_product is not None),
                "node_success": int(route_ok and first_product is not None),
                "non_keep": 1,
                "effective_adaptation": int(route_ok and first_product is not None),
            }
        )

        expected_actions = _expected_submission_actions(expected)
        expected_final = str(expected.get("预期最终难度") or expected_initial)
        candidates = _adaptation_candidates(messages, actual_initial)
        candidate_cursor = 0
        for index, acceptable in enumerate(expected_actions, start=1):
            candidate: Mapping[str, Any] = {}
            while candidate_cursor < len(candidates):
                current = candidates[candidate_cursor]
                candidate_cursor += 1
                if current.get("action") in acceptable:
                    candidate = current
                    break
            submission = candidate.get("trigger") if candidate else {}
            consequence = candidate.get("consequence") if candidate else None
            downstream = candidate.get("downstream") if candidate else consequence
            consequence_content = _content(consequence or {})
            actual_action = str(candidate.get("action") or "")
            actual_before = str(candidate.get("before_difficulty") or actual_initial)
            actual_after = str(candidate.get("after_difficulty") or actual_before)
            is_last = index == len(expected_actions)
            expected_after = expected_final if is_last else (
                "basic" if "step_down" in acceptable else actual_initial
            )
            action_match = actual_action in acceptable
            after_consistent = (
                actual_after == expected_after
                if actual_action in {"step_up", "step_down", "keep"}
                else True
            )
            node_success = int(
                bool(submission)
                and action_match
                and after_consistent
                and consequence is not None
                and downstream is not None
            )
            non_keep = int(actual_action not in {"", "keep"})
            rows.append(
                {
                    **base,
                    "adaptation_node_id": _stable_id(case_id, f"post_answer_{index}", prefix="AN"),
                    "node_type": "post_answer" if len(expected_actions) == 1 else f"post_answer_{index}",
                    "trigger_event_id": str(submission.get("msg_id") or ""),
                    "trigger_event_type": "learner_submission",
                    "before_state_json": {"difficulty": actual_before},
                    "expected_action": acceptable[0],
                    "acceptable_actions": acceptable,
                    "actual_action": actual_action,
                    "after_state_json": {"difficulty": actual_after},
                    "decision_reason": str(
                        consequence_content.get("decision_reason")
                        or consequence_content.get("assessment")
                        or candidate.get("assessment")
                        or ""
                    ),
                    "downstream_artifact_id": str((downstream or {}).get("msg_id") or ""),
                    "expected_downstream_difficulty": expected_after,
                    "actual_downstream_difficulty": actual_after,
                    "due_node": 1,
                    "trigger_binding_valid": int(bool(submission)),
                    "action_gold_match": int(action_match),
                    "after_state_consistent": int(after_consistent),
                    "downstream_executed": int(downstream is not None),
                    "node_success": node_success,
                    "non_keep": non_keep,
                    # Earlier approved KPI definition: a due node is effective
                    # only when real interaction triggers an actual path,
                    # difficulty, task-complexity or follow-up change. A
                    # correct no-op/keep remains auditable but is not counted
                    # in the numerator.
                    "effective_adaptation": int(node_success == 1 and non_keep == 1),
                }
            )
    return sorted(rows, key=lambda row: (row["seed_id"], row["case_id"], row["node_type"]))


def _approved_types(messages: Sequence[Mapping[str, Any]]) -> set[str]:
    reviews = _reviews(messages)
    approved: set[str] = set()
    for message in messages:
        msg_id = str(message.get("msg_id") or "")
        if any(_decision(review) == "approve" for review in reviews.get(msg_id, [])):
            approved.add(_payload_type(message))
    return approved


def build_coverage_cells(
    runs: Sequence[Mapping[str, Any]],
    gold_rows: Sequence[Mapping[str, Any]],
    *,
    mode: str = "AUTO_PRELIMINARY",
) -> list[dict[str, Any]]:
    """Map the frozen thirty coverage cases to strict closed-loop gates."""

    if mode not in VALID_MODES:
        raise ValueError(f"unsupported v3 recomputation mode: {mode}")
    runs_by_case: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for run in runs:
        runs_by_case[_run_value(run, "case_id")].append(run)
    rows: list[dict[str, Any]] = []
    difficulty_map = {"BASIC": "basic", "APPLIED": "applied", "ADVANCED": "advanced"}
    for gold in gold_rows:
        if str(gold.get("计入覆盖率") or "") != "是":
            continue
        case_id = str(gold.get("case_id") or gold.get("案例ID") or "")
        coverage_key = str(gold.get("覆盖格") or "")
        if not coverage_key:
            continue
        for run in runs_by_case.get(case_id, []):
            messages = list(_items(run.get("messages")))
            diagnosis = _diagnosis(messages)
            plan = diagnosis.get("knowledge_point_plan")
            selected_plan = (
                plan[0]
                if isinstance(plan, list) and plan and isinstance(plan[0], Mapping)
                else {}
            )
            expected_point = str(gold.get("目标知识点") or "")
            difficulty = difficulty_map.get(
                coverage_key.rsplit("-", 1)[-1],
                str(gold.get("预期最终难度") or ""),
            )
            expected_initial = str(gold.get("预期初始难度") or "")
            route_reachable = int(
                _run_value(run, "route_mode") == "production"
                and diagnosis.get("selected_knowledge_point") == expected_point
            )
            route_evidence_valid = int(
                route_reachable == 1
                and isinstance(selected_plan.get("evidence_ids"), list)
                and bool(selected_plan.get("evidence_ids"))
                and bool(selected_plan.get("route_reason"))
            )
            contract_messages = [
                _content(message).get("learning_contract")
                for message in messages
                if _content(message).get("event") == "learning_contract_ready"
                and isinstance(_content(message).get("learning_contract"), Mapping)
            ]
            learning_contract_match = int(
                any(
                    expected_point
                    in (
                        contract.get("target_knowledge_points")
                        if isinstance(contract.get("target_knowledge_points"), list)
                        else []
                    )
                    and str(contract.get("difficulty") or "")
                    in {expected_initial, difficulty}
                    for contract in contract_messages
                )
            )
            profile_id = _run_value(run, "profile_id")
            reviews = _reviews(messages)
            approved_products = [
                message
                for message in messages
                if _payload_type(message)
                in {"lecture_note", "practice_guide", "quiz_set"}
                and any(
                    _decision(review) == "approve"
                    for review in reviews.get(str(message.get("msg_id") or ""), [])
                )
                and str(_content(message).get("knowledge_point") or "")
                == expected_point
                and str(_content(message).get("difficulty") or "") == difficulty
            ]
            approved_types = {_payload_type(message) for message in approved_products}
            profile_resource_match = int(
                bool(profile_id)
                and bool(approved_products)
                and all(
                    str(message.get("student_profile_ref") or "") == profile_id
                    for message in approved_products
                )
            )
            lecture = int("lecture_note" in approved_types)
            practice = int("practice_guide" in approved_types)
            quiz = int("quiz_set" in approved_types)
            feedback = int(
                any(
                    _content(message).get("event")
                    in {"follow_up_feedback", "student_answer_reviewed", "path_updated"}
                    or _payload_type(message) == "feedback"
                    for message in messages
                )
            )
            review_pass = int(lecture == practice == quiz == 1)
            evidence_pass = int(
                any(
                    claim.get("kind") in FACT_KINDS
                    and any(
                        evidence.get("supports_claim") == claim.get("text")
                        for evidence in _items(message.get("evidence"))
                    )
                    for message in approved_products
                    for claim in _items(message.get("claims"))
                )
            )
            gates = [
                route_reachable,
                route_evidence_valid,
                learning_contract_match,
                profile_resource_match,
                lecture,
                practice,
                quiz,
                feedback,
                review_pass,
                evidence_pass,
            ]
            seed_id = _run_value(run, "seed_id")
            rows.append(
                {
                    "coverage_cell_id": _stable_id(seed_id, case_id, prefix="CC"),
                    "seed_id": seed_id,
                    "knowledge_point": expected_point,
                    "difficulty": difficulty,
                    "case_id": case_id,
                    "route_mode": _run_value(run, "route_mode"),
                    "route_reachable": route_reachable,
                    "route_evidence_valid": route_evidence_valid,
                    "learning_contract_match": learning_contract_match,
                    "profile_resource_match": profile_resource_match,
                    "lecture_pass": lecture,
                    "practice_pass": practice,
                    "quiz_pass": quiz,
                    "feedback_pass": feedback,
                    "review_pass": review_pass,
                    "evidence_pass": evidence_pass,
                    "cell_pass": int(all(gates)),
                    "human_gate": "PENDING_HUMAN",
                }
            )
    return rows


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "percentage": round((numerator / denominator * 100) if denominator else 0.0, 4),
        "denominator_zero": denominator == 0,
    }


def _resolve_pair(
    row: Mapping[str, Any],
    left: str,
    right: str,
    adjudicator: str,
    *,
    label: str,
) -> Any:
    a = row.get(left)
    b = row.get(right)
    if a is None or b is None:
        raise FinalReviewIncomplete(f"missing double review for {label}")
    if a == b:
        return a
    value = row.get(adjudicator)
    if value is None or value == "":
        raise FinalReviewIncomplete(f"unadjudicated disagreement for {label}")
    return value


def _apply_final_review(
    fact_rows: list[dict[str, Any]],
    adaptation_rows: list[dict[str, Any]],
    coverage_rows: list[dict[str, Any]],
    human_review: Mapping[str, Any],
) -> None:
    fact_labels = {
        str(row.get("content_unit_id")): row
        for row in _items(human_review.get("fact_units"))
    }
    for row in fact_rows:
        review = fact_labels.get(row["content_unit_id"])
        if review is None:
            raise FinalReviewIncomplete(
                f"missing human label for fact unit {row['content_unit_id']}"
            )
        label = _resolve_pair(
            review,
            "reviewer_a",
            "reviewer_b",
            "adjudicator",
            label=f"fact unit {row['content_unit_id']}",
        )
        if label not in {"SUPPORTED", "HALLUCINATION"}:
            raise FinalReviewIncomplete(f"invalid human label for fact unit {row['content_unit_id']}")
        row["human_label"] = label
        row["adjudicator"] = str(review.get("adjudicator") or "AGREED")
        row["hallucination_reason"] = str(review.get("reason") or "")
        human_rule_hits = {
            str(item) for item in review.get("rule_hits", []) if str(item) in FACT_RULES
        }
        if label == "HALLUCINATION" and not human_rule_hits:
            raise FinalReviewIncomplete(
                f"hallucination fact unit {row['content_unit_id']} requires an R-01/R-02/R-04/R-05/R-06 rule hit"
            )
        if human_rule_hits:
            row["rule_hits"] = sorted(human_rule_hits)
        row["final_fact_denominator"] = int(row["published_final"] == 1)
        row["final_hallucination_numerator"] = int(
            row["published_final"] == 1 and label == "HALLUCINATION"
        )
        row["native_error_denominator"] = int(
            row["first_generation"] == 1 and label == "HALLUCINATION"
        )
        row["interception_numerator"] = int(
            row["native_error_denominator"] == 1
            and row["auto_review_detected"] == 1
            and row["auto_intercepted"] == 1
        )

    tx_labels = {
        str(row.get("first_gen_transaction_id")): row
        for row in _items(human_review.get("adaptation_transactions"))
    }
    for tx_id in sorted({row["first_gen_transaction_id"] for row in adaptation_rows if row["first_gen_transaction_id"]}):
        review = tx_labels.get(tx_id)
        if review is None:
            raise FinalReviewIncomplete(f"missing human label for adaptation transaction {tx_id}")
        value = _resolve_pair(
            review,
            "reviewer_a_mismatch",
            "reviewer_b_mismatch",
            "adjudicator_mismatch",
            label=f"adaptation transaction {tx_id}",
        )
        if value not in {0, 1, False, True}:
            raise FinalReviewIncomplete(f"invalid adaptation label for {tx_id}")
        for row in adaptation_rows:
            if row["first_gen_transaction_id"] == tx_id:
                row["human_mismatch_confirmed"] = int(bool(value))
                row["final_residual_mismatch"] = int(
                    bool(value) and row["r03_repair_approved"] != 1
                )
                row["adjudicator"] = str(review.get("adjudicator_mismatch") or "AGREED")

    cell_labels = {
        str(row.get("coverage_cell_id") or row.get("case_id")): row
        for row in _items(human_review.get("coverage_cells"))
    }
    for row in coverage_rows:
        review = cell_labels.get(row["coverage_cell_id"])
        if review is None:
            raise FinalReviewIncomplete(
                f"missing coverage gate for {row['seed_id']}/{row['case_id']}"
            )
        value = _resolve_pair(
            review,
            "reviewer_a_pass",
            "reviewer_b_pass",
            "adjudicator_pass",
            label=f"coverage cell {row['seed_id']}/{row['case_id']}",
        )
        if value not in {0, 1, False, True}:
            raise FinalReviewIncomplete(
                f"invalid coverage gate for {row['seed_id']}/{row['case_id']}"
            )
        row["human_gate"] = int(bool(value))
        row["cell_pass"] = int(row["cell_pass"] == 1 and bool(value))


def compute_v3_metrics(
    fact_rows: list[dict[str, Any]],
    adaptation_rows: list[dict[str, Any]],
    coverage_rows: list[dict[str, Any]],
    *,
    mode: str,
    human_review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute competition core metrics plus auxiliary diagnostic KPIs."""

    if mode not in VALID_MODES:
        raise ValueError(f"unsupported v3 recomputation mode: {mode}")
    if mode == "FINAL_HUMAN_REVIEWED":
        if not isinstance(human_review, Mapping):
            raise FinalReviewIncomplete("final mode requires a human review file")
        _apply_final_review(fact_rows, adaptation_rows, coverage_rows, human_review)

    final_fact_denominator = sum(int(row["final_fact_denominator"]) for row in fact_rows)
    final_hallucinations = sum(int(row["final_hallucination_numerator"]) for row in fact_rows)
    due_nodes = sum(int(row["due_node"]) for row in adaptation_rows)
    effective_nodes = sum(int(row.get("effective_adaptation", 0)) for row in adaptation_rows)
    initial_route_nodes = [
        row
        for row in adaptation_rows
        if int(row.get("due_node", 0)) == 1
        and str(row.get("node_type") or "") == "initial_route"
    ]
    profile_resource_matches = sum(
        int(
            int(row.get("trigger_binding_valid", 0)) == 1
            and int(row.get("action_gold_match", 0)) == 1
            and int(row.get("after_state_consistent", 0)) == 1
            and int(row.get("downstream_executed", 0)) == 1
            and str(row.get("expected_downstream_difficulty") or "")
            == str(row.get("actual_downstream_difficulty") or "")
        )
        for row in initial_route_nodes
    )
    by_seed_point: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for row in coverage_rows:
        if int(row["cell_pass"]) == 1:
            by_seed_point[str(row.get("seed_id") or "single")][
                str(row["knowledge_point"])
            ].add(str(row["difficulty"]))
    seeds = sorted({str(row.get("seed_id") or "single") for row in coverage_rows})
    covered_points = sum(
        1
        for point in {
            str(row["knowledge_point"]) for row in coverage_rows
        }
        if seeds
        and all(
            by_seed_point[seed][point] == {"basic", "applied", "advanced"}
            for seed in seeds
        )
    )
    native_errors = sum(int(row["native_error_denominator"]) for row in fact_rows)
    intercepted = sum(int(row["interception_numerator"]) for row in fact_rows)
    first_gen_transactions: dict[str, dict[str, Any]] = {}
    for row in adaptation_rows:
        tx_id = str(row.get("first_gen_transaction_id") or "")
        if not tx_id:
            continue
        current = first_gen_transactions.setdefault(
            tx_id,
            {
                "reviewed": 0,
                "rejected": 0,
                "human_mismatch": 0,
                "repair": 0,
                "residual": 0,
            },
        )
        current["reviewed"] = max(current["reviewed"], int(row["r03_reviewed_first_gen"]))
        current["rejected"] = max(current["rejected"], int(row["r03_rejected_first_gen"]))
        human = row["human_mismatch_confirmed"]
        if human != "PENDING_HUMAN":
            current["human_mismatch"] = max(current["human_mismatch"], int(human))
        current["repair"] = max(current["repair"], int(row["r03_repair_approved"]))
        residual = row["final_residual_mismatch"]
        if residual != "PENDING_HUMAN":
            current["residual"] = max(current["residual"], int(residual))
    mismatch_denominator = sum(item["reviewed"] for item in first_gen_transactions.values())
    mismatch_numerator = sum(
        int(item["reviewed"] and item["rejected"] and item["human_mismatch"])
        for item in first_gen_transactions.values()
    )
    return {
        "mode": mode,
        "label_status": (
            "AUTOMATIC_INITIAL_ESTIMATE_NOT_FINAL"
            if mode == "AUTO_PRELIMINARY"
            else "DOUBLE_HUMAN_REVIEWED_AND_ADJUDICATED"
        ),
        "metrics": {
            "final_hallucination_rate": _rate(final_hallucinations, final_fact_denominator),
            "profile_resource_difficulty_adaptation_accuracy": _rate(
                profile_resource_matches, len(initial_route_nodes)
            ),
            "strict_closed_loop_coverage": _rate(covered_points, 10),
            "effective_automatic_adaptation_rate": _rate(effective_nodes, due_nodes),
            "hallucination_interception_rate": _rate(intercepted, native_errors),
            "native_teaching_adaptation_mismatch_rate": _rate(
                mismatch_numerator, mismatch_denominator
            ),
        },
        "auxiliary_counts": {
            "first_generation_errors": native_errors,
            "automatically_intercepted": intercepted,
            "final_residual_hallucinations": final_hallucinations,
            "official_initial_adaptation_nodes": len(initial_route_nodes),
            "official_profile_resource_matches": profile_resource_matches,
            "first_generation_r03_transactions": mismatch_denominator,
            "confirmed_r03_mismatches": mismatch_numerator,
            "r03_repaired_and_approved": sum(item["repair"] for item in first_gen_transactions.values()),
            "final_residual_adaptation_mismatches": sum(item["residual"] for item in first_gen_transactions.values()),
        },
    }


def normalize_for_json(value: Any) -> Any:
    """Convert tuple/set and mappings into deterministic JSON-compatible data."""

    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
