"""Deterministic ablation for the domain semantic-claim preflight gate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.domain_config import load_domain_config
from agents.kb_loader import KnowledgeChunk, require_valid_chunks
from agents.review_agent import _r02_formula_hit
from agents.semantic_claims import build_claim_plan, enforce_claim_plan


ROOT = Path(__file__).resolve().parents[1]
CHUNK_DIR = ROOT / "agents" / "knowledge_base" / "chunks"
FIXED_LECTURE = (
    "## 核心概念\n"
    "完成率是计划量与实际量的比值。\n\n"
    "## 计算方法\n"
    "完成率通过计划量除以实际量计算，用于判断生产状态。"
)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _chunks_by_id() -> dict[str, KnowledgeChunk]:
    return {chunk.chunk_id: chunk for chunk in require_valid_chunks(CHUNK_DIR)}


def _review_product(lecture_md: str) -> dict[str, Any]:
    return {
        "trace_id": "semantic-claim-fixed-stub",
        "msg_id": "semantic-claim-fixed-stub-001",
        "agent": "knowledge",
        "role": "produce",
        "payload": {
            "type": "lecture_note",
            "content": {"lecture_md": lecture_md},
        },
        "evidence": [
            {
                "kind": "kb_chunk",
                "ref": "KB-002",
                "quote": '问"做得怎么样"看完成率，完成率 = 实际量 ÷ 计划量。',
            }
        ],
        "claims": [],
    }


def _group(lecture_md: str, *, checked: int, repaired: int) -> dict[str, Any]:
    hit = _r02_formula_hit(_review_product(lecture_md))
    return {
        "lecture_md": lecture_md,
        "lecture_sha256": _canonical_hash(lecture_md),
        "semantic_checked": checked,
        "semantic_repaired": repaired,
        "r02_formula_hit": hit is not None,
        "r02_rule_id": None if hit is None else hit["rule_id"],
        "contains_reversed_formula": "计划量除以实际量" in lecture_md,
        "contains_ambiguous_formula": "计划量与实际量的比值" in lecture_md,
        "contains_canonical_formula": "实际量÷计划量" in lecture_md,
        "llm_calls": 0,
    }


def run_ablation() -> dict[str, Any]:
    """Compare one fixed artifact with the gate disabled and enabled."""

    chunks = _chunks_by_id()
    plan = build_claim_plan(
        load_domain_config("production_progress"),
        "计划量与实际量口径",
        (chunks["KB-002"],),
    )
    enabled = enforce_claim_plan(FIXED_LECTURE, plan)
    h0 = _group(FIXED_LECTURE, checked=0, repaired=0)
    h1 = _group(
        enabled.text,
        checked=enabled.checked,
        repaired=enabled.repaired,
    )
    same_fixed_input = _canonical_hash(FIXED_LECTURE)
    return {
        "schema_version": "semantic-claim-ablation-v1",
        "design": {
            "only_difference": "semantic_claim_preflight_enabled",
            "fixed_input_sha256": same_fixed_input,
            "same_history_prefix": True,
            "same_retrieved_evidence": True,
            "same_model_output": True,
            "post_split_llm_output_used": False,
        },
        "claim_plan": [item.invariant.invariant_id for item in plan],
        "groups": {
            "H0-disabled": h0,
            "H1-enabled": h1,
        },
        "adoption_gate": {
            "baseline_reproduces_e2e_010": (
                h0["r02_formula_hit"]
                and h0["contains_reversed_formula"]
                and h0["contains_ambiguous_formula"]
            ),
            "enabled_repairs_without_llm": (
                not h1["r02_formula_hit"]
                and not h1["contains_reversed_formula"]
                and not h1["contains_ambiguous_formula"]
                and h1["contains_canonical_formula"]
                and h1["semantic_repaired"] == 2
                and h1["llm_calls"] == 0
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("eval/results/semantic_claim_ablation.json"),
    )
    args = parser.parse_args()
    result = run_ablation()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
