from __future__ import annotations

from collections import Counter
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import pytest

from agents.kb_loader import (
    KnowledgeBaseValidationError,
    load_chunks,
    require_valid_chunks,
)


ROOT = Path(__file__).resolve().parents[1]
CHUNK_DIR = ROOT / "agents" / "knowledge_base" / "chunks"
SEC_SOURCE_DIR = ROOT / "agents" / "knowledge_base" / "sec_sources"
REQUIRED_FIELDS = (
    "chunk_id",
    "knowledge_point",
    "difficulty",
    "prerequisites",
    "learning_goal",
    "common_mistakes",
    "applicable_processes",
)
APPROVED_COMPLETION_TIER_SHA256 = {
    "KB-003-A": "aa8097e7cabf51b1467596079b00f69b5d10bbce48d0fdea7d93756b44a06a02",
    "KB-003-B": "204264b28d23814aab48108e00529ee6496313defb67007acd0048d8f8378edb",
}
APPROVED_PLAN_ACTUAL_TIER_SHA256 = {
    "KB-002-A": "6a8427ab540f7e8f9e4b1ed73faf4628285ee992e75c0da26d0b5c671b2e019d",
    "KB-002-B": "6a47d9b077de7613180815a38ff0ac3680c6e96a053d8925d26340dd00b5580b",
}
APPROVED_PROCESS_PROPAGATION_TIER_SHA256 = {
    "KB-001-A": "b86acf72c062d72f2340b9536c022a248ad1a9f0c924c40623986be659069253",
    "KB-001-B": "9c0af0bfd040d680c73ed27be6271ff3060f5ef1a3b54a3ae825de280ef5972e",
}
APPROVED_DEVIATION_RISK_TIER_SHA256 = {
    "KB-004": "cbc354cb5d81867997b2a9aef248ad282d00ad1bed86d517e43372ad8ac39730",
    "KB-004-A": "ca05b7d5ed266e62fdedec731ef9ce173b966e0fa747ec447fb5bb0225f71706",
    "KB-004-B": "bd05d00e4ec02ecf96735fe3fc8559beee2cfd006945dfc2c3922c4892cb4fbe",
}
APPROVED_MONTHLY_AGGREGATION_TIER_SHA256 = {
    "KB-005-A": "a9744c6f3a847da91724ace2f9edb8aca02524d1533a75e6aa9fd3854e7b0ece",
    "KB-005-B": "076d567d89a7ad16b57d15def4627c9a4e45259e9eab6f265eede0efe919f72b",
}
APPROVED_ANOMALY_IDENTIFICATION_TIER_SHA256 = {
    "KB-006-A": "1820e6ac6675a702fc7a8ca2b5cb6abaade6cf9cd271037e82442be4f5c33b5e",
    "KB-006-B": "f9519dc791b5330d7b5e7e873360856608f16b8be36628fb5795939901e3fc12",
}
APPROVED_PROPAGATION_LAG_TIER_SHA256 = {
    "KB-007-A": "b90b47aa544256caf878d0af677b400312d8b1c0401717f12493537971abb782",
    "KB-007-B": "b0e427b293e744f915505da21e6c0558cadbbbadcf5a3dceb8d0033a9e776602",
}
APPROVED_TRANSMISSION_GROUP_SHA256 = {
    "KB-007": (
        "KB-007_传导时滞分析.md",
        "24f2bd271f70b220a6c45b5d78231a91325d268c737dfad4a052863a5716122a",
    ),
    "KB-007-A": (
        "KB-007-A_传导时滞分析_applied.md",
        "b90b47aa544256caf878d0af677b400312d8b1c0401717f12493537971abb782",
    ),
    "KB-007-B": (
        "KB-007-B_传导时滞分析_advanced.md",
        "b0e427b293e744f915505da21e6c0558cadbbbadcf5a3dceb8d0033a9e776602",
    ),
    "KB-008": (
        "KB-008_异常衰减规律.md",
        "f86f2a2f76e845096bfe6d88c2ba67ab7fb21ea6015e437368d2c2df1ac02ecd",
    ),
    "KB-008-A": (
        "KB-008-A_异常衰减规律_applied.md",
        "9117c44e04172be394ffdef638e6ae3eb6bcadccfadddab69b9b3f6e6bb364a6",
    ),
    "KB-008-B": (
        "KB-008-B_异常衰减规律_advanced.md",
        "f3edf3f6ca26f306a159751847017f2fe20cec4e29d2a87065b5acd12ddabd56",
    ),
    "KB-009": (
        "KB-009_责任单元定位.md",
        "2322dd275436bacd4fcc110c22eab4f49480c869c84accc61c331e61f9fa058f",
    ),
    "KB-009-A": (
        "KB-009-A_责任单元定位_applied.md",
        "690432b991bef02f1e7fd45bd51d9aa45779339c74e95b58047e32db5d18b38e",
    ),
    "KB-009-B": (
        "KB-009-B_责任单元定位_advanced.md",
        "fdabee82fb145399617ad7acffd8a4e01144b64d68d3ee008aceb75d47995c6a",
    ),
    "KB-010": (
        "KB-010_跨工序归因方法.md",
        "0e824ff1cb0443609110212b748d76c120191d12d2f7d15e151af28696efe0b9",
    ),
    "KB-010-A": (
        "KB-010-A_跨工序归因方法_applied.md",
        "1d308c1edf54e1b1445d31616c5e4aa3b6a988c2c3506ea7ce4d094cdfd865ee",
    ),
    "KB-010-B": (
        "KB-010-B_跨工序归因方法_advanced.md",
        "b4abb0536beb30c472a60f5d6031ed78ef520c0dadf2a24131dbcc94f5c9f91a",
    ),
}
TRANSMISSION_GROUP_METADATA = {
    "KB-007": ("basic", ("KB-001", "KB-006")),
    "KB-007-A": ("applied", ("KB-007", "KB-001-A")),
    "KB-007-B": ("advanced", ("KB-007", "KB-007-A", "KB-001-B")),
    "KB-008": ("basic", ("KB-007",)),
    "KB-008-A": ("applied", ("KB-008", "KB-007-A")),
    "KB-008-B": ("advanced", ("KB-008", "KB-008-A", "KB-001-B")),
    "KB-009": ("basic", ("KB-005",)),
    "KB-009-A": ("applied", ("KB-009", "KB-005-A")),
    "KB-009-B": ("advanced", ("KB-009", "KB-009-A", "KB-001-B")),
    "KB-010": ("basic", ("KB-009", "KB-007", "KB-008")),
    "KB-010-A": (
        "applied",
        ("KB-010", "KB-007-A", "KB-008-A", "KB-009-A"),
    ),
    "KB-010-B": ("advanced", ("KB-010", "KB-010-A", "KB-001-B")),
}
TEACHING_FACT_1 = "本项目岗位培训与评测采用的月完成率正常波动区间为88%—103%。"
TEACHING_FACT_2 = (
    "月完成率低于88%时，先作为偏低信号观察，不能仅凭单月数值直接定性；"
    "还须结合持续性或伴随的风险记录。"
)
TEACHING_FACT_SOURCE = {
    "type": "project_business_asset",
    "path": "项目业务资产",
    "locator": "异常识别阈值",
    "version": "交付版",
    "git_commit": "不适用",
    "git_blob": "不适用",
    "effective_scope": "岗位培训与评测",
}


def write_chunk(
    path: Path,
    *,
    omit: str | None = None,
    body: str = "这是用于结构校验的正文。",
    **overrides: Any,
) -> None:
    metadata: dict[str, Any] = {
        "chunk_id": "KB-900",
        "knowledge_point": "测试知识点",
        "difficulty": "basic",
        "prerequisites": [],
        "learning_goal": "能够完成结构测试",
        "common_mistakes": [],
        "applicable_processes": ["YCL"],
    }
    metadata.update(overrides)
    if omit is not None:
        metadata.pop(omit)
    lines = ["---"]
    lines.extend(
        f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in metadata.items()
    )
    lines.extend(("---", "", body, ""))
    path.write_text("\n".join(lines), encoding="utf-8")


def teaching_fact_card() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "card_id": "KB-006-BASIC-ANOMALY-RANGE",
        "card_version": "1.0.0",
        "title": "本项目异常识别口径卡",
        "source": dict(TEACHING_FACT_SOURCE),
        "facts": [
            {"fact_id": "TF-006-001", "text": TEACHING_FACT_1},
            {"fact_id": "TF-006-002", "text": TEACHING_FACT_2},
        ],
    }


def teaching_fact_body(*, duplicate_first: bool = False) -> str:
    sentences = ["先讲不带阈值的判断方法。", TEACHING_FACT_1, TEACHING_FACT_2]
    if duplicate_first:
        sentences.append(TEACHING_FACT_1)
    return "\n\n".join(sentences)


def test_real_corpus_loads_exact_kb_and_sec_assets() -> None:
    result = load_chunks(CHUNK_DIR)

    assert result.issues == ()
    assert {chunk.chunk_id for chunk in result.chunks} == {
        *(f"KB-{index:03d}" for index in range(1, 11)),
        "KB-001-A",
        "KB-001-B",
        "KB-002-A",
        "KB-002-B",
        "KB-003-A",
        "KB-003-B",
        "KB-004-A",
        "KB-004-B",
        "KB-005-A",
        "KB-005-B",
        "KB-006-A",
        "KB-006-B",
        "KB-007-A",
        "KB-007-B",
        "KB-008-A",
        "KB-008-B",
        "KB-009-A",
        "KB-009-B",
        "KB-010-A",
        "KB-010-B",
        *(f"SEC-{index:03d}" for index in range(1, 11)),
    }
    assert Counter(chunk.difficulty for chunk in result.chunks) == {
        "basic": 16,
        "applied": 14,
        "advanced": 10,
    }


def test_ingested_sec_assets_are_byte_identical_to_approved_sources() -> None:
    sources = sorted(SEC_SOURCE_DIR.glob("SEC-*.md"))

    assert [path.name.split("_")[0] for path in sources] == [
        f"SEC-{index:03d}" for index in range(1, 11)
    ]
    for source in sources:
        ingested = CHUNK_DIR / source.name
        assert ingested.is_file(), f"missing ingested SEC chunk: {source.name}"
        assert hashlib.sha256(ingested.read_bytes()).digest() == hashlib.sha256(
            source.read_bytes()
        ).digest()


def test_completion_rate_tier_assets_are_byte_identical_to_frozen_sources() -> None:
    paths = {
        "KB-003-A": CHUNK_DIR / "KB-003-A_完成率计算_applied.md",
        "KB-003-B": CHUNK_DIR / "KB-003-B_完成率计算_advanced.md",
    }

    for chunk_id, path in paths.items():
        assert path.is_file(), f"missing frozen completion-rate chunk: {path.name}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == (
            APPROVED_COMPLETION_TIER_SHA256[chunk_id]
        )


def test_plan_actual_tier_assets_are_byte_identical_to_frozen_sources() -> None:
    paths = {
        "KB-002-A": CHUNK_DIR / "KB-002-A_计划量与实际量口径_applied.md",
        "KB-002-B": CHUNK_DIR / "KB-002-B_计划量与实际量口径_advanced.md",
    }

    for chunk_id, path in paths.items():
        assert path.is_file(), f"missing frozen plan-actual chunk: {path.name}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == (
            APPROVED_PLAN_ACTUAL_TIER_SHA256[chunk_id]
        )


def test_process_propagation_tier_assets_are_byte_identical_to_frozen_sources() -> None:
    paths = {
        "KB-001-A": CHUNK_DIR / "KB-001-A_三道工序与传导关系_applied.md",
        "KB-001-B": CHUNK_DIR / "KB-001-B_三道工序与传导关系_advanced.md",
    }

    for chunk_id, path in paths.items():
        assert path.is_file(), f"missing frozen process-propagation chunk: {path.name}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == (
            APPROVED_PROCESS_PROPAGATION_TIER_SHA256[chunk_id]
        )


def test_deviation_risk_tier_assets_are_byte_identical_to_frozen_sources() -> None:
    paths = {
        "KB-004": CHUNK_DIR / "KB-004_偏差率与风险等级.md",
        "KB-004-A": CHUNK_DIR / "KB-004-A_偏差率与风险等级_applied.md",
        "KB-004-B": CHUNK_DIR / "KB-004-B_偏差率与风险等级_advanced.md",
    }

    for chunk_id, path in paths.items():
        assert path.is_file(), f"missing frozen deviation-risk chunk: {path.name}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == (
            APPROVED_DEVIATION_RISK_TIER_SHA256[chunk_id]
        )


def test_monthly_aggregation_tier_assets_are_byte_identical_to_frozen_sources() -> None:
    paths = {
        "KB-005-A": CHUNK_DIR / "KB-005-A_月度聚合方法_applied.md",
        "KB-005-B": CHUNK_DIR / "KB-005-B_月度聚合方法_advanced.md",
    }

    for chunk_id, path in paths.items():
        assert path.is_file(), f"missing frozen monthly-aggregation chunk: {path.name}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == (
            APPROVED_MONTHLY_AGGREGATION_TIER_SHA256[chunk_id]
        )


def test_anomaly_identification_tier_assets_are_byte_identical_to_frozen_sources() -> None:
    paths = {
        "KB-006-A": CHUNK_DIR / "KB-006-A_异常识别标准_applied.md",
        "KB-006-B": CHUNK_DIR / "KB-006-B_异常识别标准_advanced.md",
    }

    for chunk_id, path in paths.items():
        assert path.is_file(), f"missing frozen anomaly-identification chunk: {path.name}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == (
            APPROVED_ANOMALY_IDENTIFICATION_TIER_SHA256[chunk_id]
        )


def test_propagation_lag_tier_assets_are_byte_identical_to_frozen_sources() -> None:
    paths = {
        "KB-007-A": CHUNK_DIR / "KB-007-A_传导时滞分析_applied.md",
        "KB-007-B": CHUNK_DIR / "KB-007-B_传导时滞分析_advanced.md",
    }

    for chunk_id, path in paths.items():
        assert path.is_file(), f"missing frozen propagation-lag chunk: {path.name}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == (
            APPROVED_PROPAGATION_LAG_TIER_SHA256[chunk_id]
        )


def test_transmission_group_assets_are_byte_identical_to_frozen_sources() -> None:
    assert tuple(APPROVED_TRANSMISSION_GROUP_SHA256) == tuple(
        TRANSMISSION_GROUP_METADATA
    )
    for chunk_id, (filename, expected_hash) in (
        APPROVED_TRANSMISSION_GROUP_SHA256.items()
    ):
        path = CHUNK_DIR / filename
        assert path.is_file(), f"missing frozen transmission chunk: {filename}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash, chunk_id


def test_transmission_group_has_the_exact_atomic_tier_metadata() -> None:
    chunks = {chunk.chunk_id: chunk for chunk in require_valid_chunks(CHUNK_DIR)}

    for chunk_id, (difficulty, prerequisites) in TRANSMISSION_GROUP_METADATA.items():
        chunk = chunks[chunk_id]
        assert chunk.difficulty == difficulty, chunk_id
        assert chunk.prerequisites == prerequisites, chunk_id


def test_kb007_is_basic_and_uses_the_approved_same_month_boundary() -> None:
    chunks = {chunk.chunk_id: chunk for chunk in require_valid_chunks(CHUNK_DIR)}
    chunk = chunks["KB-007"]

    assert chunk.difficulty == "basic"
    assert "不能只用同月数据确认或排除传导" in chunk.body
    assert "同月正常不能排除后续影响" in chunk.body
    assert "同月同时异常也不能仅凭月度数据确认即时传导" in chunk.body
    assert "上游异常影响下游**不是即时的**" not in chunk.body


def test_kb001b_uses_direction_neutral_strength_wording() -> None:
    chunk = {
        item.chunk_id: item for item in require_valid_chunks(CHUNK_DIR)
    }["KB-001-B"]

    assert "强度不是必然逐级减弱" in chunk.body
    assert "强度形态不是传导成立的必要条件" in chunk.body
    assert "缓冲吸收（减弱）、大致保持、放大" not in chunk.body


def test_kb006_basic_declares_teaching_fact_card() -> None:
    chunks = {chunk.chunk_id: chunk for chunk in require_valid_chunks(CHUNK_DIR)}
    chunk = chunks["KB-006"]
    card = chunk.teaching_fact_card

    assert card is not None
    assert card.schema_version == 1
    assert card.card_id == "KB-006-BASIC-ANOMALY-RANGE"
    assert card.card_version == "1.0.0"
    assert card.title == "异常识别口径卡"
    assert dict(card.source) == TEACHING_FACT_SOURCE
    assert [fact.fact_id for fact in card.facts] == ["TF-006-001", "TF-006-002"]
    assert [fact.text for fact in card.facts] == [TEACHING_FACT_1, TEACHING_FACT_2]
    assert all(chunk.sentences.count(fact.text) == 1 for fact in card.facts)
    free_sentences = [
        sentence
        for sentence in chunk.sentences
        if sentence not in {fact.text for fact in card.facts}
    ]
    assert all(not any(character.isdigit() for character in sentence) for sentence in free_sentences)
    assert "62.36%" not in chunk.body
    assert "90.61%" not in chunk.body
    assert chunk.metadata["source_basis"] == (
        "阈值来自项目业务资产定稿；"
        "比赛库数值仅作任务与核验数据"
    )


def test_base_chunks_use_deidentified_source_basis() -> None:
    chunks = {chunk.chunk_id: chunk for chunk in require_valid_chunks(CHUNK_DIR)}
    expected = "船舶生产领域指标口径库翻写；数值示例全部取自比赛脱敏数据包"

    for chunk_id in ("KB-001", "KB-002", "KB-003", "KB-004", "KB-005", "KB-009"):
        assert chunks[chunk_id].metadata["source_basis"] == expected


def test_kb003b_contains_self_contained_gap_formulas() -> None:
    chunk = {
        item.chunk_id: item for item in require_valid_chunks(CHUNK_DIR)
    }["KB-003-B"]

    assert "D_output = max(P - A, 0)" in chunk.body
    assert "D_conversion = max(min(A, P) - min(Q, P), 0)" in chunk.body
    assert "D_total = D_output + D_conversion" in chunk.body


def test_frozen_tier_assets_are_marked_byte_preserving() -> None:
    attributes = (CHUNK_DIR / ".gitattributes").read_text(encoding="utf-8").splitlines()

    assert attributes == [
        "KB-003-A_完成率计算_applied.md -text",
        "KB-003-B_完成率计算_advanced.md -text",
        "KB-002-A_计划量与实际量口径_applied.md -text",
        "KB-002-B_计划量与实际量口径_advanced.md -text",
        "KB-001-A_三道工序与传导关系_applied.md -text",
        "KB-001-B_三道工序与传导关系_advanced.md -text",
        "KB-004_偏差率与风险等级.md -text",
        "KB-004-A_偏差率与风险等级_applied.md -text",
        "KB-004-B_偏差率与风险等级_advanced.md -text",
        "KB-005-A_月度聚合方法_applied.md -text",
        "KB-005-B_月度聚合方法_advanced.md -text",
        "KB-006-A_异常识别标准_applied.md -text",
        "KB-006-B_异常识别标准_advanced.md -text",
        "KB-007_传导时滞分析.md -text",
        "KB-007-A_传导时滞分析_applied.md -text",
        "KB-007-B_传导时滞分析_advanced.md -text",
        "KB-008_异常衰减规律.md -text",
        "KB-008-A_异常衰减规律_applied.md -text",
        "KB-008-B_异常衰减规律_advanced.md -text",
        "KB-009_责任单元定位.md -text",
        "KB-009-A_责任单元定位_applied.md -text",
        "KB-009-B_责任单元定位_advanced.md -text",
        "KB-010_跨工序归因方法.md -text",
        "KB-010-A_跨工序归因方法_applied.md -text",
        "KB-010-B_跨工序归因方法_advanced.md -text",
    ]


def test_one_valid_chunk_is_a_valid_corpus(tmp_path: Path) -> None:
    write_chunk(tmp_path / "one.md", chunk_id="KB-901")

    result = load_chunks(tmp_path)

    assert [chunk.chunk_id for chunk in result.chunks] == ["KB-901"]
    assert result.issues == ()


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_missing_required_field_reports_exact_path(
    tmp_path: Path, field: str
) -> None:
    write_chunk(tmp_path / "missing.md", omit=field)

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert len(result.issues) == 1
    assert result.issues[0].path.name == "missing.md"
    assert result.issues[0].field == field
    assert "required" in result.issues[0].message


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("chunk_id", []),
        ("knowledge_point", []),
        ("difficulty", []),
        ("prerequisites", "KB-001"),
        ("learning_goal", []),
        ("common_mistakes", "M-01"),
        ("applicable_processes", "YCL"),
    ),
)
def test_wrong_field_type_reports_exact_path(
    tmp_path: Path, field: str, value: Any
) -> None:
    write_chunk(tmp_path / "wrong-type.md", **{field: value})

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert result.issues[0].field == field
    assert "type" in result.issues[0].message


@pytest.mark.parametrize(
    "field", ("prerequisites", "common_mistakes", "applicable_processes")
)
def test_list_fields_reject_non_string_items(tmp_path: Path, field: str) -> None:
    write_chunk(tmp_path / "wrong-item.md", **{field: [1]})

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert result.issues[0].field == f"{field}[0]"
    assert "string" in result.issues[0].message


def test_invalid_difficulty_reports_enum_values(tmp_path: Path) -> None:
    write_chunk(tmp_path / "difficulty.md", difficulty="expert")

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert result.issues[0].field == "difficulty"
    assert "basic|applied|advanced" in result.issues[0].message


def test_duplicate_yaml_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-key.md"
    path.write_text(
        "---\n"
        "chunk_id: KB-900\n"
        "chunk_id: KB-901\n"
        "knowledge_point: 测试\n"
        "difficulty: basic\n"
        "prerequisites: []\n"
        "learning_goal: 测试\n"
        "common_mistakes: []\n"
        "applicable_processes: [YCL]\n"
        "---\n\n正文\n",
        encoding="utf-8",
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert result.issues[0].path.name == path.name
    assert result.issues[0].field == "frontmatter"
    assert "duplicate key: chunk_id" in result.issues[0].message


def test_duplicate_ids_reject_every_conflicting_file_regardless_of_order(
    tmp_path: Path,
) -> None:
    write_chunk(tmp_path / "z.md", chunk_id="KB-777")
    write_chunk(tmp_path / "a.md", chunk_id="KB-777")

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert {(issue.path.name, issue.field) for issue in result.issues} == {
        ("a.md", "chunk_id"),
        ("z.md", "chunk_id"),
    }
    assert all("duplicate chunk_id: KB-777" in issue.message for issue in result.issues)


def test_missing_prerequisite_rejects_the_referrer(tmp_path: Path) -> None:
    write_chunk(
        tmp_path / "child.md",
        chunk_id="KB-902",
        prerequisites=["KB-404"],
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert result.issues[0].field == "prerequisites[0]"
    assert "KB-404" in result.issues[0].message


def test_prerequisite_rejection_cascades_from_an_invalid_parent(
    tmp_path: Path,
) -> None:
    write_chunk(tmp_path / "parent.md", chunk_id="KB-910", difficulty="expert")
    write_chunk(
        tmp_path / "child.md",
        chunk_id="KB-911",
        prerequisites=["KB-910"],
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert {(issue.path.name, issue.field) for issue in result.issues} == {
        ("parent.md", "difficulty"),
        ("child.md", "prerequisites[0]"),
    }


def test_empty_body_is_rejected_without_enforcing_length(tmp_path: Path) -> None:
    write_chunk(tmp_path / "empty.md", body="   ")
    write_chunk(tmp_path / "short.md", chunk_id="KB-901", body="短正文")

    result = load_chunks(tmp_path)

    assert [chunk.chunk_id for chunk in result.chunks] == ["KB-901"]
    assert len(result.issues) == 1
    assert result.issues[0].field == "body"


def test_extra_metadata_is_preserved(tmp_path: Path) -> None:
    write_chunk(tmp_path / "extra.md", source_basis="老师供稿")

    result = load_chunks(tmp_path)

    assert result.issues == ()
    assert result.chunks[0].metadata["source_basis"] == "老师供稿"


def test_body_is_split_into_verbatim_anchored_sentences(tmp_path: Path) -> None:
    write_chunk(
        tmp_path / "sentences.md",
        body="第一句。第二问？第三叹！第四段；第五尾\n\n**第六段**真的？！",
    )

    chunk = require_valid_chunks(tmp_path)[0]

    assert chunk.sentences == (
        "第一句。",
        "第二问？",
        "第三叹！",
        "第四段；",
        "第五尾",
        "**第六段**真的？！",
    )


def test_colon_leads_create_structural_evidence_groups_without_resegmenting(
    tmp_path: Path,
) -> None:
    body = (
        "开场说明。判断时要结合：\n\n"
        "- 第一项。补充说明；继续核对。\n"
        "* 第二项？\n\n"
        "普通正文。\n\n"
        "ASCII lead:\n\n"
        "1. 第三项。\n"
        "2) 第四项！\n\n"
        "## 标题：\n\n"
        "+ 标题后的列表不成组。\n\n"
        "普通冒号：\n\n"
        "后面不是列表。\n\n"
        "- 孤立列表。"
    )
    write_chunk(tmp_path / "groups.md", body=body)

    chunk = require_valid_chunks(tmp_path)[0]

    assert chunk.sentences == (
        "开场说明。",
        "判断时要结合：",
        "- 第一项。",
        "补充说明；",
        "继续核对。",
        "* 第二项？",
        "普通正文。",
        "ASCII lead:",
        "1. 第三项。",
        "2) 第四项！",
        "## 标题：",
        "+ 标题后的列表不成组。",
        "普通冒号：",
        "后面不是列表。",
        "- 孤立列表。",
    )
    assert [
        (group.lead_ref, group.member_refs)
        for group in chunk.evidence_context_groups
    ] == [
        (2, (3, 4, 5, 6)),
        (8, (9, 10)),
    ]
    with pytest.raises(FrozenInstanceError):
        chunk.evidence_context_groups[0].lead_ref = 99


def test_real_kb006_applied_has_exact_list_evidence_groups() -> None:
    chunks = {chunk.chunk_id: chunk for chunk in require_valid_chunks(CHUNK_DIR)}
    chunk = chunks["KB-006-A"]
    groups = {
        group.lead_ref: group.member_refs
        for group in chunk.evidence_context_groups
    }

    assert groups[6] == (7, 8, 9, 10, 11, 12, 13)
    assert groups[21] == (22, 23, 24, 25)
    assert hashlib.sha256(chunk.path.read_bytes()).hexdigest() == (
        APPROVED_ANOMALY_IDENTIFICATION_TIER_SHA256["KB-006-A"]
    )


def test_real_corpus_keeps_internal_m_ids_only_in_frontmatter() -> None:
    chunks = {chunk.chunk_id: chunk for chunk in require_valid_chunks(CHUNK_DIR)}
    internal_id = re.compile(r"(?<![A-Za-z0-9])M-\d{2}(?!\d)")

    assert all(internal_id.search(chunk.body) is None for chunk in chunks.values())
    assert chunks["KB-007"].common_mistakes == ("M-02",)
    assert chunks["KB-008"].common_mistakes == ("M-05",)
    assert chunks["KB-010"].common_mistakes == ("M-02", "M-05")


def test_require_valid_chunks_raises_one_aggregate_error(tmp_path: Path) -> None:
    write_chunk(tmp_path / "bad.md", difficulty="expert")
    write_chunk(tmp_path / "good.md", chunk_id="KB-901")

    with pytest.raises(KnowledgeBaseValidationError) as error:
        require_valid_chunks(tmp_path)

    assert "bad.md:difficulty" in str(error.value)
    assert error.value.issues[0].path.name == "bad.md"


def test_teaching_fact_card_is_parsed_into_read_only_values(tmp_path: Path) -> None:
    write_chunk(
        tmp_path / "card.md",
        body=teaching_fact_body(),
        teaching_facts=teaching_fact_card(),
    )

    chunk = require_valid_chunks(tmp_path)[0]
    card = chunk.teaching_fact_card

    assert card is not None
    assert card.schema_version == 1
    assert card.card_id == "KB-006-BASIC-ANOMALY-RANGE"
    assert card.card_version == "1.0.0"
    assert card.title == "本项目异常识别口径卡"
    assert dict(card.source) == TEACHING_FACT_SOURCE
    assert [(fact.fact_id, fact.text) for fact in card.facts] == [
        ("TF-006-001", TEACHING_FACT_1),
        ("TF-006-002", TEACHING_FACT_2),
    ]
    with pytest.raises(FrozenInstanceError):
        card.card_id = "changed"
    with pytest.raises(TypeError):
        card.source["path"] = "changed"


def test_chunk_without_teaching_facts_has_none_card(tmp_path: Path) -> None:
    write_chunk(tmp_path / "plain.md")

    chunk = require_valid_chunks(tmp_path)[0]

    assert chunk.teaching_fact_card is None


@pytest.mark.parametrize(
    "field",
    ("schema_version", "card_id", "card_version", "title", "source", "facts"),
)
def test_teaching_fact_card_rejects_missing_top_level_field(
    tmp_path: Path, field: str
) -> None:
    card = teaching_fact_card()
    card.pop(field)
    write_chunk(
        tmp_path / "missing-card-field.md",
        body=teaching_fact_body(),
        teaching_facts=card,
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert any(
        issue.field == f"teaching_facts.{field}" and "required" in issue.message
        for issue in result.issues
    )


@pytest.mark.parametrize("field", tuple(TEACHING_FACT_SOURCE))
def test_teaching_fact_card_rejects_missing_source_field(
    tmp_path: Path, field: str
) -> None:
    card = teaching_fact_card()
    card["source"].pop(field)
    write_chunk(
        tmp_path / "missing-source-field.md",
        body=teaching_fact_body(),
        teaching_facts=card,
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert any(
        issue.field == f"teaching_facts.source.{field}"
        and "required" in issue.message
        for issue in result.issues
    )


@pytest.mark.parametrize(
    ("field", "mutate"),
    (
        ("teaching_facts.unexpected", lambda card: card.update(unexpected="x")),
        (
            "teaching_facts.source.unexpected",
            lambda card: card["source"].update(unexpected="x"),
        ),
        (
            "teaching_facts.facts[0].unexpected",
            lambda card: card["facts"][0].update(unexpected="x"),
        ),
    ),
)
def test_teaching_fact_card_rejects_unknown_fields(
    tmp_path: Path, field: str, mutate: Any
) -> None:
    card = teaching_fact_card()
    mutate(card)
    write_chunk(
        tmp_path / "unknown-card-field.md",
        body=teaching_fact_body(),
        teaching_facts=card,
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert any(
        issue.field == field and "unknown field" in issue.message
        for issue in result.issues
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", True),
        ("schema_version", "1"),
        ("card_id", []),
        ("card_version", 1),
        ("title", {}),
        ("source", []),
        ("facts", {}),
        ("source.path", []),
        ("facts[0].fact_id", []),
        ("facts[0].text", 88),
    ),
)
def test_teaching_fact_card_rejects_wrong_types(
    tmp_path: Path, field: str, value: Any
) -> None:
    card = teaching_fact_card()
    if field.startswith("source."):
        card["source"][field.removeprefix("source.")] = value
    elif field.startswith("facts[0]."):
        card["facts"][0][field.removeprefix("facts[0].")] = value
    else:
        card[field] = value
    write_chunk(
        tmp_path / "wrong-card-type.md",
        body=teaching_fact_body(),
        teaching_facts=card,
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert any(
        issue.field == f"teaching_facts.{field}" and "type" in issue.message
        for issue in result.issues
    )


@pytest.mark.parametrize(
    "field",
    (
        "card_id",
        "card_version",
        "title",
        *(f"source.{field}" for field in TEACHING_FACT_SOURCE),
        "facts[0].fact_id",
        "facts[0].text",
    ),
)
def test_teaching_fact_card_rejects_blank_strings(
    tmp_path: Path, field: str
) -> None:
    card = teaching_fact_card()
    if field.startswith("source."):
        card["source"][field.removeprefix("source.")] = "   "
    elif field.startswith("facts[0]."):
        card["facts"][0][field.removeprefix("facts[0].")] = "   "
    else:
        card[field] = "   "
    write_chunk(
        tmp_path / "blank-card-string.md",
        body=teaching_fact_body(),
        teaching_facts=card,
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert any(
        issue.field == f"teaching_facts.{field}" and "empty" in issue.message
        for issue in result.issues
    )


def test_teaching_fact_card_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    card = teaching_fact_card()
    card["schema_version"] = 2
    write_chunk(
        tmp_path / "schema-version.md",
        body=teaching_fact_body(),
        teaching_facts=card,
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert any(
        issue.field == "teaching_facts.schema_version"
        and "must equal 1" in issue.message
        for issue in result.issues
    )


def test_teaching_fact_card_rejects_empty_facts(tmp_path: Path) -> None:
    card = teaching_fact_card()
    card["facts"] = []
    write_chunk(
        tmp_path / "empty-facts.md",
        body="只讲判断方法。",
        teaching_facts=card,
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert any(
        issue.field == "teaching_facts.facts" and "at least one" in issue.message
        for issue in result.issues
    )


def test_teaching_fact_card_rejects_duplicate_fact_id(tmp_path: Path) -> None:
    card = teaching_fact_card()
    card["facts"][1]["fact_id"] = card["facts"][0]["fact_id"]
    write_chunk(
        tmp_path / "duplicate-fact-id.md",
        body=teaching_fact_body(),
        teaching_facts=card,
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert any(
        issue.field == "teaching_facts.facts[1].fact_id"
        and "duplicate" in issue.message
        for issue in result.issues
    )


def test_teaching_fact_card_rejects_fact_without_percentage(tmp_path: Path) -> None:
    card = teaching_fact_card()
    card["facts"][0]["text"] = "本项目采用已裁决的正常波动区间。"
    write_chunk(
        tmp_path / "fact-without-percentage.md",
        body="本项目采用已裁决的正常波动区间。\n\n" + TEACHING_FACT_2,
        teaching_facts=card,
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert any(
        issue.field == "teaching_facts.facts[0].text"
        and "percentage" in issue.message
        for issue in result.issues
    )


@pytest.mark.parametrize(
    ("body", "message"),
    (
        ("只讲判断方法。\n\n" + TEACHING_FACT_2, "exactly once"),
        (teaching_fact_body(duplicate_first=True), "exactly once"),
    ),
)
def test_teaching_fact_card_rejects_non_unique_body_sentence_match(
    tmp_path: Path, body: str, message: str
) -> None:
    write_chunk(
        tmp_path / "body-match.md",
        body=body,
        teaching_facts=teaching_fact_card(),
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert any(
        issue.field == "teaching_facts.facts[0].text"
        and message in issue.message
        for issue in result.issues
    )


def test_teaching_fact_card_rejects_two_facts_sharing_one_sentence(
    tmp_path: Path,
) -> None:
    card = teaching_fact_card()
    card["facts"][1]["text"] = TEACHING_FACT_1
    write_chunk(
        tmp_path / "shared-sentence.md",
        body=TEACHING_FACT_1,
        teaching_facts=card,
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert any(
        issue.field == "teaching_facts.facts[1].text"
        and "shared" in issue.message
        for issue in result.issues
    )


def test_duplicate_teaching_fact_card_ids_reject_both_chunks(tmp_path: Path) -> None:
    write_chunk(
        tmp_path / "a.md",
        chunk_id="KB-901",
        body=teaching_fact_body(),
        teaching_facts=teaching_fact_card(),
    )
    write_chunk(
        tmp_path / "b.md",
        chunk_id="KB-902",
        body=teaching_fact_body(),
        teaching_facts=teaching_fact_card(),
    )

    result = load_chunks(tmp_path)

    assert result.chunks == ()
    assert {
        (issue.path.name, issue.field) for issue in result.issues
    } == {
        ("a.md", "teaching_facts.card_id"),
        ("b.md", "teaching_facts.card_id"),
    }
    assert all("duplicate card_id" in issue.message for issue in result.issues)
