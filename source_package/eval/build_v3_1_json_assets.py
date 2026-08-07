"""Build the v3.1 formal input/gold JSON assets from immutable v3 sources.

The added experience tag is ordinary profile evidence.  It only selects a
frozen diagnostic probe family; the router may mark a knowledge point as a
blind spot only after a wrong pretest or probe answer.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
V3_INPUT = ROOT / "eval" / "cases" / "v3" / "formal_50_inputs_v3.json"
V3_GOLD = ROOT / "eval" / "gold" / "v3" / "formal_50_gold_v3.json"
V31_INPUT = ROOT / "eval" / "cases" / "v3_1" / "formal_50_inputs_v3_1.json"
V31_GOLD = ROOT / "eval" / "gold" / "v3_1" / "formal_50_gold_v3_1.json"

PROBE_TAGS = {
    "DP-01": "process_flow_coordination",
    "AP-01": "plan_actual_reconciliation",
    "AP-02": "completion_rate_reporting",
    "DP-02": "variance_risk_monitoring",
    "AP-05": "monthly_rollup_reporting",
    "AP-04": "anomaly_threshold_review",
    "AP-03": "cross_month_lag_review",
    "DP-03": "decay_pattern_review",
    "DP-04": "handled_responsibility_handoffs",
    "DP-05": "cross_process_root_cause_review",
}


def _tag_for_probe(probe_id: str) -> str:
    prefix = probe_id.rsplit("-", 1)[0] if probe_id.startswith("DP-") else probe_id
    try:
        return PROBE_TAGS[prefix]
    except KeyError as exc:
        raise ValueError(f"no v3.1 experience tag for frozen probe {probe_id}") from exc


def main() -> int:
    rows = json.loads(V3_INPUT.read_text(encoding="utf-8"))
    for row in rows:
        probes = row.get("diagnostic_probe_answers") or []
        tags = [] if not probes else [_tag_for_probe(str(probes[0]["probe_id"]))]
        if any(_tag_for_probe(str(item["probe_id"])) != tags[0] for item in probes):
            raise ValueError(f"{row['case_id']} mixes diagnostic probe families")
        row["experience_tags"] = tags

    V31_INPUT.parent.mkdir(parents=True, exist_ok=True)
    V31_INPUT.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    V31_GOLD.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(V3_GOLD, V31_GOLD)
    print(V31_INPUT)
    print(V31_GOLD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
