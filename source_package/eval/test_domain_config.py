from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.domain_config import load_domain_config


ASSET_NAMES = {
    "schema": "schema.json",
    "intents": "intents.json",
    "task_manifest": "task_manifest.json",
    "task_templates": "task_templates.json",
    "counter_evidence": "counter_evidence_map.json",
    "semantic_invariants": "semantic_invariants.json",
    "gateway_scope": "gateway_scope.json",
}


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "domain"
    package.mkdir(parents=True)
    _write_json(
        package / "manifest.json",
        {
            "schema_version": 1,
            "domain_id": "fixture_domain",
            "database": "fixture_db",
            "assets": ASSET_NAMES,
        },
    )
    _write_json(
        package / "schema.json",
        {
            "tables": {"fact_fixture": ["id", "value"]},
            "prompt_ddl": "CREATE TABLE fact_fixture (id INT, value DECIMAL(10,2));",
            "dictionary_rows": "| fact_fixture | value | fixture value |",
        },
    )
    _write_json(package / "intents.json", {"families": ["F1"]})
    _write_json(package / "task_manifest.json", {"template_ids": ["T-1"]})
    _write_json(package / "task_templates.json", {"templates": []})
    _write_json(package / "counter_evidence_map.json", {"mappings": []})
    _write_json(
        package / "semantic_invariants.json",
        {
            "schema_version": 1,
            "invariants": [
                {
                    "invariant_id": "FIXTURE-RATIO-001",
                    "metric": "fixture_rate",
                    "mode": "ratio",
                    "knowledge_points": ["fixture point"],
                    "trigger_terms": ["fixture rate"],
                    "canonical_expression": "actual_value/plan_value",
                    "display_expression": "actual value / plan value",
                    "canonical_claim": "Fixture rate equals actual value divided by plan value.",
                    "evidence_ref": "FIXTURE-001",
                    "evidence_quote": "Fixture rate equals actual value divided by plan value.",
                    "numerator_terms": ["actual value", "actual_value"],
                    "denominator_terms": ["plan value", "plan_value"],
                }
            ],
        },
    )
    _write_json(
        package / "gateway_scope.json",
        {"unsupported_literals": ["库存"]},
    )
    return package


def test_loads_and_deep_freezes_a_complete_package(tmp_path: Path) -> None:
    package = load_domain_config(_package(tmp_path))

    assert package.domain_id == "fixture_domain"
    assert package.schema_version == 1
    assert package.database == "fixture_db"
    assert package.table_columns == {"fact_fixture": frozenset({"id", "value"})}
    assert len(package.package_sha256) == 64
    assert package.intents["families"] == ("F1",)
    assert package.semantic_invariants[0].invariant_id == "FIXTURE-RATIO-001"
    assert (
        package.semantic_invariants[0].canonical_expression
        == "actual_value/plan_value"
    )

    with pytest.raises(TypeError):
        package.intents["families"] = ("F2",)  # type: ignore[index]


@pytest.mark.parametrize("extra_key", ["unknown", "query_contracts"])
def test_rejects_extra_manifest_fields(tmp_path: Path, extra_key: str) -> None:
    path = _package(tmp_path)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    manifest[extra_key] = True
    _write_json(path / "manifest.json", manifest)

    with pytest.raises(ValueError, match="manifest fields"):
        load_domain_config(path)


def test_rejects_missing_asset_and_asset_path_escape(tmp_path: Path) -> None:
    missing = _package(tmp_path / "missing")
    (missing / "intents.json").unlink()
    with pytest.raises(ValueError, match="cannot read domain asset"):
        load_domain_config(missing)

    escaped = _package(tmp_path / "escaped")
    manifest = json.loads((escaped / "manifest.json").read_text(encoding="utf-8"))
    manifest["assets"]["intents"] = "../intents.json"
    _write_json(escaped / "manifest.json", manifest)
    with pytest.raises(ValueError, match="asset path"):
        load_domain_config(escaped)


@pytest.mark.parametrize(
    "tables",
    [
        {"Fact": ["id"], "fact": ["value"]},
        {"fact": ["ID", "id"]},
        {"fact": []},
    ],
)
def test_rejects_ambiguous_or_empty_table_definitions(
    tmp_path: Path, tables: dict[str, list[str]]
) -> None:
    path = _package(tmp_path)
    schema = json.loads((path / "schema.json").read_text(encoding="utf-8"))
    schema["tables"] = tables
    _write_json(path / "schema.json", schema)

    with pytest.raises(ValueError, match="table|column"):
        load_domain_config(path)


def test_package_hash_is_derived_from_asset_bytes(tmp_path: Path) -> None:
    path = _package(tmp_path)
    first = load_domain_config(path).package_sha256
    intents = json.loads((path / "intents.json").read_text(encoding="utf-8"))
    intents["families"].append("F2")
    _write_json(path / "intents.json", intents)

    second = load_domain_config(path).package_sha256

    assert second != first


def test_rejects_a_ratio_invariant_without_ratio_terms(tmp_path: Path) -> None:
    path = _package(tmp_path)
    asset = json.loads(
        (path / "semantic_invariants.json").read_text(encoding="utf-8")
    )
    asset["invariants"][0]["numerator_terms"] = []
    _write_json(path / "semantic_invariants.json", asset)

    with pytest.raises(ValueError, match="semantic_invariants"):
        load_domain_config(path)
