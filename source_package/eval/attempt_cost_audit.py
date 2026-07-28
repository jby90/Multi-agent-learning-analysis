"""Audit actual model-call cost across succeeded and failed P7 attempts."""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path, PurePosixPath
from typing import Any

from eval.trace_dataset import (
    AttemptRecord,
    canonical_json,
    load_attempt_records,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "eval" / "results" / "live_50_postfix" / "run_ledger.jsonl"
DEFAULT_PRICING = ROOT / "eval" / "pricing_qwen3_cn.json"
DEFAULT_OUTPUT = ROOT / "eval" / "results" / "formal_run_cost_audit.json"
MILLION = Decimal(1_000_000)


def _decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be decimal-compatible") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return parsed


def _non_negative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _repository_path(repository_root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe cache path: {relative}")
    root = Path(repository_root).resolve()
    path = root.joinpath(*pure.parts).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"cache path escapes repository: {relative}")
    return path


def _prices(pricing: Mapping[str, Any]) -> dict[str, tuple[Decimal, Decimal]]:
    entries = pricing.get("prices")
    if not isinstance(entries, Mapping) or not entries:
        raise ValueError("pricing prices must be a non-empty object")
    output: dict[str, tuple[Decimal, Decimal]] = {}
    for model, value in entries.items():
        if not isinstance(model, str) or not isinstance(value, Mapping):
            raise ValueError("pricing model entry is invalid")
        output[model] = (
            _decimal(value.get("input_cny_per_million"), f"{model} input price"),
            _decimal(value.get("output_cny_per_million"), f"{model} output price"),
        )
    return output


def _empty_counter() -> dict[str, Any]:
    return {
        "attempts": 0,
        "api_results": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_prompt_tokens": 0,
        "cost": Decimal(0),
    }


def _add_attempt(counter: dict[str, Any]) -> None:
    counter["attempts"] += 1


def _add_result(
    counter: dict[str, Any],
    *,
    prompt: int,
    completion: int,
    cached: int,
    input_price: Decimal,
    output_price: Decimal,
) -> None:
    counter["api_results"] += 1
    counter["prompt_tokens"] += prompt
    counter["completion_tokens"] += completion
    counter["cached_prompt_tokens"] += cached
    counter["cost"] += (
        Decimal(prompt) * input_price + Decimal(completion) * output_price
    ) / MILLION


def _render(counter: Mapping[str, Any]) -> dict[str, Any]:
    prompt = int(counter["prompt_tokens"])
    completion = int(counter["completion_tokens"])
    return {
        "attempts": int(counter["attempts"]),
        "api_results": int(counter["api_results"]),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cached_prompt_tokens_observed": int(counter["cached_prompt_tokens"]),
        "cost_cny": f"{Decimal(counter['cost']):.6f}",
    }


def _cache_rows(path: Path) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid cache JSON at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"cache row at {path}:{line_number} must be an object")
        rows.append(value)
    return tuple(rows)


def audit_attempt_cost(
    records: Sequence[AttemptRecord],
    *,
    repository_root: Path,
    pricing: Mapping[str, Any],
) -> dict[str, Any]:
    """Count each persisted model cache result once across the immutable ledger."""

    if not records:
        raise ValueError("attempt cost audit requires at least one ledger record")
    price_by_model = _prices(pricing)
    status_counts = {"succeeded": _empty_counter(), "failed": _empty_counter()}
    all_counts = _empty_counter()
    model_counts: dict[str, dict[str, Any]] = defaultdict(_empty_counter)
    seen_trace_ids: set[str] = set()
    for record in records:
        if record.trace_id in seen_trace_ids:
            raise ValueError(f"duplicate trace_id in ledger: {record.trace_id}")
        seen_trace_ids.add(record.trace_id)
        status_counter = status_counts[record.status]
        _add_attempt(status_counter)
        _add_attempt(all_counts)
        cache_path = _repository_path(repository_root, record.cache_path)
        for cache_index, row in enumerate(_cache_rows(cache_path), start=1):
            result = row.get("result")
            if not isinstance(result, Mapping):
                raise ValueError(f"{record.trace_id} cache result {cache_index} is missing")
            model = result.get("model")
            usage = result.get("token_usage")
            if not isinstance(model, str) or model not in price_by_model:
                raise ValueError(f"{record.trace_id} cache result has unpriced model {model}")
            if not isinstance(usage, Mapping):
                raise ValueError(f"{record.trace_id} cache result token_usage is missing")
            prompt = _non_negative_int(usage.get("prompt_tokens"), "prompt_tokens")
            completion = _non_negative_int(
                usage.get("completion_tokens"), "completion_tokens"
            )
            total = _non_negative_int(usage.get("total_tokens"), "total_tokens")
            cached = _non_negative_int(usage.get("cached_tokens", 0), "cached_tokens")
            if prompt + completion != total:
                raise ValueError("cache prompt plus completion does not equal total")
            if cached > prompt:
                raise ValueError("cache cached_tokens cannot exceed prompt_tokens")
            input_price, output_price = price_by_model[model]
            for counter in (status_counter, all_counts, model_counts[model]):
                _add_result(
                    counter,
                    prompt=prompt,
                    completion=completion,
                    cached=cached,
                    input_price=input_price,
                    output_price=output_price,
                )
    model_rows: dict[str, dict[str, Any]] = {}
    for model in sorted(model_counts):
        row = _render(model_counts[model])
        row.pop("attempts")
        model_rows[model] = row
    return {
        "scope": "persisted model cache results across every immutable formal-run attempt",
        "counting_policy": "each cache JSONL result is counted once; standard input list price is used without cache discount",
        "all_attempts": _render(all_counts),
        "succeeded_attempts": _render(status_counts["succeeded"]),
        "failed_attempts": _render(status_counts["failed"]),
        "models": model_rows,
    }


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    content = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审计P7正式跑批全部attempt的真实模型调用成本")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--pricing", type=Path, default=DEFAULT_PRICING)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    pricing = json.loads(args.pricing.read_text(encoding="utf-8"))
    report = audit_attempt_cost(
        load_attempt_records(args.ledger),
        repository_root=args.repository_root,
        pricing=pricing,
    )
    report["ledger_path"] = args.ledger.as_posix()
    report["ledger_sha256"] = sha256_file(args.ledger)
    report["pricing_path"] = args.pricing.as_posix()
    _atomic_write(args.output, report)
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
