"""Auditable token attribution and list-price accounting for P7 traces."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any


MILLION = Decimal(1_000_000)
SPLIT_PREFIXES = ("routing", "generation")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _non_negative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _usage(value: Any, label: str) -> tuple[int, int, int]:
    usage = _mapping(value)
    prompt = _non_negative_int(usage.get("prompt_tokens"), f"{label} prompt_tokens")
    completion = _non_negative_int(
        usage.get("completion_tokens"), f"{label} completion_tokens"
    )
    total = _non_negative_int(usage.get("total_tokens"), f"{label} total_tokens")
    if prompt + completion != total:
        raise ValueError(f"{label} prompt plus completion does not equal total")
    return prompt, completion, total


def _split_usage(
    message: Mapping[str, Any], content: Mapping[str, Any]
) -> tuple[tuple[str, int, int, int], ...] | None:
    split_markers = {
        f"{prefix}_{field}"
        for prefix in SPLIT_PREFIXES
        for field in (
            "model",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_tokens",
        )
    }
    if not any(field in content for field in split_markers):
        return None
    missing = sorted(field for field in split_markers if field not in content)
    if missing:
        raise ValueError(
            f"{message.get('msg_id', '<unknown>')} incomplete Text2SQL split fields: "
            + ", ".join(missing)
        )

    rows: list[tuple[str, int, int, int]] = []
    for prefix in SPLIT_PREFIXES:
        model = content[f"{prefix}_model"]
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"{prefix}_model must be a non-empty string")
        prompt = _non_negative_int(
            content[f"{prefix}_prompt_tokens"], f"{prefix}_prompt_tokens"
        )
        completion = _non_negative_int(
            content[f"{prefix}_completion_tokens"],
            f"{prefix}_completion_tokens",
        )
        total = _non_negative_int(
            content[f"{prefix}_total_tokens"], f"{prefix}_total_tokens"
        )
        cached = _non_negative_int(
            content[f"{prefix}_cached_tokens"], f"{prefix}_cached_tokens"
        )
        if prompt + completion != total:
            raise ValueError(f"{prefix} prompt plus completion does not equal total")
        if cached > prompt:
            raise ValueError(f"{prefix}_cached_tokens cannot exceed prompt tokens")
        rows.append((model, prompt, completion, cached))

    aggregate_prompt, aggregate_completion, aggregate_total = _usage(
        message.get("token_usage"),
        str(message.get("msg_id", "Text2SQL message")),
    )
    if (
        sum(row[1] for row in rows) != aggregate_prompt
        or sum(row[2] for row in rows) != aggregate_completion
        or sum(row[1] + row[2] for row in rows) != aggregate_total
    ):
        raise ValueError(
            f"{message.get('msg_id', '<unknown>')} aggregate token usage does not "
            "match routing/generation splits"
        )
    return tuple(rows)


def _decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be decimal-compatible") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} must be a finite non-negative decimal")
    return parsed


def _money(value: Decimal) -> str:
    return f"{value:.6f}"


def _empty_counter() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "cached_prompt_tokens": 0}


def _add(counter: dict[str, int], prompt: int, completion: int, cached: int) -> None:
    counter["prompt_tokens"] += prompt
    counter["completion_tokens"] += completion
    counter["cached_prompt_tokens"] += cached


def _priced_counter(
    counter: Mapping[str, int],
    input_price: Decimal,
    output_price: Decimal,
) -> dict[str, Any]:
    input_cost = Decimal(counter["prompt_tokens"]) * input_price / MILLION
    output_cost = Decimal(counter["completion_tokens"]) * output_price / MILLION
    return {
        "prompt_tokens": counter["prompt_tokens"],
        "completion_tokens": counter["completion_tokens"],
        "total_tokens": counter["prompt_tokens"] + counter["completion_tokens"],
        "cached_prompt_tokens_observed": counter["cached_prompt_tokens"],
        "input_cost_cny": _money(input_cost),
        "output_cost_cny": _money(output_cost),
        "cost_cny": _money(input_cost + output_cost),
    }


def _traditional_comparison(pricing: Mapping[str, Any]) -> dict[str, Any]:
    raw = _mapping(pricing.get("traditional_training_estimate"))
    required = {"development_hours", "hourly_cny", "learner_count", "note"}
    if set(raw) != required:
        raise ValueError("traditional_training_estimate fields do not match schema")
    hours = _decimal(raw["development_hours"], "development_hours")
    hourly = _decimal(raw["hourly_cny"], "hourly_cny")
    learners = _non_negative_int(raw["learner_count"], "learner_count")
    if learners == 0:
        raise ValueError("learner_count must be positive")
    note = raw["note"]
    if not isinstance(note, str) or not note.strip():
        raise ValueError("traditional estimate note must be non-empty")
    total = hours * hourly
    return {
        "measurement": "人工粗估",
        "trace_measured": False,
        "development_hours": str(hours),
        "hourly_cny": _money(hourly),
        "learner_count": learners,
        "estimated_total_cny": _money(total),
        "cost_per_learner_cny": _money(total / Decimal(learners)),
        "note": note,
    }


def _validate_pricing(pricing: Mapping[str, Any]) -> dict[str, tuple[Decimal, Decimal]]:
    expected = {
        "currency": "CNY",
        "unit": "per_million_tokens",
        "region": "中国内地",
        "retrieved_at": "2026-07-17",
        "source_url": "https://help.aliyun.com/zh/model-studio/model-pricing",
        "cache_policy": "standard_input_price_without_cache_discount",
    }
    for field, value in expected.items():
        if pricing.get(field) != value:
            raise ValueError(f"pricing {field} must be {value}")
    prices = _mapping(pricing.get("prices"))
    if not prices:
        raise ValueError("pricing prices must be non-empty")
    parsed: dict[str, tuple[Decimal, Decimal]] = {}
    for model, raw in prices.items():
        if not isinstance(model, str) or not model:
            raise ValueError("pricing model ID must be a non-empty string")
        entry = _mapping(raw)
        if set(entry) != {"input_cny_per_million", "output_cny_per_million"}:
            raise ValueError(f"price fields do not match schema for {model}")
        parsed[model] = (
            _decimal(entry["input_cny_per_million"], f"{model} input price"),
            _decimal(entry["output_cny_per_million"], f"{model} output price"),
        )
    _traditional_comparison(pricing)
    return parsed


def cost_report(
    dataset: Sequence[Mapping[str, Any]], pricing: Mapping[str, Any]
) -> dict[str, Any]:
    """Attribute every non-zero token exactly once and price at published rates."""

    price_by_model = _validate_pricing(pricing)
    model_counts: dict[str, dict[str, int]] = defaultdict(_empty_counter)
    case_counts: dict[str, dict[str, int]] = defaultdict(_empty_counter)
    profile_counts: dict[str, dict[str, int]] = defaultdict(_empty_counter)
    profile_sessions: dict[str, int] = defaultdict(int)

    for row in dataset:
        case = _mapping(row.get("case"))
        case_id = case.get("case_id")
        profile_id = case.get("profile_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("dataset case_id must be a non-empty string")
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError(f"{case_id} profile_id must be a non-empty string")
        if case_id in case_counts:
            raise ValueError(f"duplicate dataset case_id: {case_id}")
        case_counts[case_id] = _empty_counter()
        profile_sessions[profile_id] += 1

        for message in _items(row.get("messages")):
            if "token_usage" not in message:
                continue
            content = _mapping(_mapping(message.get("payload")).get("content"))
            split = _split_usage(message, content)
            if split is None:
                prompt, completion, _ = _usage(
                    message.get("token_usage"), str(message.get("msg_id", "message"))
                )
                model = message.get("model")
                if not isinstance(model, str) or not model.strip():
                    if prompt or completion:
                        raise ValueError("non-zero token usage has no model")
                    continue
                attributions = ((model, prompt, completion, 0),)
            else:
                attributions = split

            for model, prompt, completion, cached in attributions:
                if (prompt or completion) and model not in price_by_model:
                    raise ValueError(f"no price configured for model {model}")
                _add(model_counts[model], prompt, completion, cached)
                _add(case_counts[case_id], prompt, completion, cached)
                _add(profile_counts[profile_id], prompt, completion, cached)

    model_rows: dict[str, dict[str, Any]] = {}
    model_costs: dict[str, Decimal] = {}
    for model in sorted(model_counts):
        if model in price_by_model:
            input_price, output_price = price_by_model[model]
        else:
            input_price = output_price = Decimal(0)
        row = _priced_counter(model_counts[model], input_price, output_price)
        row["input_cny_per_million"] = _money(input_price)
        row["output_cny_per_million"] = _money(output_price)
        model_rows[model] = row
        model_costs[model] = Decimal(row["cost_cny"])

    total_cost = sum(model_costs.values(), Decimal(0))
    total_prompt = sum(row["prompt_tokens"] for row in model_counts.values())
    total_completion = sum(row["completion_tokens"] for row in model_counts.values())
    total_cached = sum(row["cached_prompt_tokens"] for row in model_counts.values())

    def grouped_rows(
        counters: Mapping[str, Mapping[str, int]], *, sessions: bool
    ) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        for key in sorted(counters):
            counter = counters[key]
            # Group cost is reconstructed from the same model prices by allocating
            # each message above. Both approved models share the current rates; keep
            # this formula explicit and fail if that ceases to be true.
            rates = set(price_by_model.values())
            if len(rates) != 1:
                raise ValueError("grouped cost requires per-group model attribution")
            input_price, output_price = next(iter(rates))
            value = _priced_counter(counter, input_price, output_price)
            if sessions:
                count = profile_sessions[key]
                value["sessions"] = count
                value["average_cost_per_session_cny"] = _money(
                    Decimal(value["cost_cny"]) / Decimal(count)
                )
            output[key] = value
        return output

    sessions = len(dataset)
    traditional = _traditional_comparison(pricing)
    average = total_cost / Decimal(sessions) if sessions else Decimal(0)
    traditional_per_learner = Decimal(traditional["cost_per_learner_cny"])
    traditional["ai_cost_per_session_cny"] = _money(average)
    traditional["ai_to_traditional_ratio"] = _money(
        average / traditional_per_learner if traditional_per_learner else Decimal(0)
    )

    return {
        "pricing_basis": {
            field: pricing[field]
            for field in (
                "currency",
                "unit",
                "region",
                "retrieved_at",
                "source_url",
                "cache_policy",
            )
        },
        "models": model_rows,
        "cases": grouped_rows(case_counts, sessions=False),
        "profiles": grouped_rows(profile_counts, sessions=True),
        "sessions": sessions,
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "total_tokens": total_prompt + total_completion,
        "cached_prompt_tokens_observed": total_cached,
        "total_cost_cny": _money(total_cost),
        "average_cost_per_session_cny": _money(average),
        "traditional_training_comparison": traditional,
    }
