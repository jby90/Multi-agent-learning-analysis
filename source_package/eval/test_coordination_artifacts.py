from __future__ import annotations

from coordination.artifacts import ArtifactEnvelope


def _message() -> dict[str, object]:
    return {
        "msg_id": "msg-artifact-1",
        "trace_id": "trace-artifact",
        "agent": "knowledge",
        "payload": {
            "type": "lecture_note",
            "content": {"title": "计划量与实际完成量", "steps": [1, 2]},
        },
        "evidence": [
            {"kind": "kb_chunk", "ref": "kb-01"},
            {"kind": "review_rule", "ref": "kb-01"},
        ],
    }


def test_artifact_envelope_is_stable_and_deduplicates_evidence_refs() -> None:
    first = ArtifactEnvelope.from_message(_message(), contract_id="lc-test")
    reordered = dict(reversed(list(_message().items())))
    second = ArtifactEnvelope.from_message(reordered, contract_id="lc-test")

    assert first.content_sha256 == second.content_sha256
    assert first.artifact_id == "msg-artifact-1"
    assert first.evidence_refs == ("kb-01",)


def test_each_branch_materializes_an_independent_artifact_copy() -> None:
    envelope = ArtifactEnvelope.from_message(_message(), contract_id="lc-test")
    left = envelope.materialize()
    right = envelope.materialize()

    left["payload"]["content"]["steps"].append(3)

    assert right["payload"]["content"]["steps"] == [1, 2]
    assert envelope.materialize()["payload"]["content"]["steps"] == [1, 2]
