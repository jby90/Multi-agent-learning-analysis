from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def first_segment_csv_root(tmp_path: Path) -> Path:
    """Build the smallest deidentified CSV package needed by oracle unit tests."""
    raw_root = tmp_path / "first_segment_csv"
    raw_root.mkdir()
    schema = json.loads(
        (ROOT / "config" / "domains" / "first_segment" / "schema.json").read_text(
            encoding="utf-8"
        )
    )["tables"]

    status_rows = []
    for index, process in enumerate(
        ("上胎", "下胎", "出涂", "切割", "完整性", "小组立", "结构完", "进涂"),
        start=1,
    ):
        status_rows.append(
            {
                "OID": f"S{index:03d}",
                "TARTYPE": "物量态势",
                "PROCESS": process,
                "ACTUALNUM": "1369" if process == "切割" else str(100 + index),
                "FINISHRATE": "0.4231" if process == "切割" else "0.5000",
                "RECORDDATE": "2025-07-10",
            }
        )

    process_rows = [
        {
            "oid": "P000",
            "TARGETTYPE": "每月情况",
            "DEPT": "部门01",
            "PROCESS": "切割",
            "TRENDMONTH": "7",
            "PLANNUM": "10",
            "ACTUALNUM": "8",
            "TRENDYEAR": "2025",
            "RECORDDATE": "2025-07-10",
        }
    ]
    departments = (
        "部门01",
        "部门02",
        "外协",
        "部门01",
        "部门02",
        "外协",
        "部门01",
        "部门03",
    )
    for index, (process, department) in enumerate(
        zip(
            ("上胎", "下胎", "出涂", "切割", "完整性", "小组立", "结构完", "进涂"),
            departments,
            strict=True,
        ),
        start=1,
    ):
        process_rows.append(
            {
                "oid": f"P{index:03d}",
                "TARGETTYPE": "年度统计",
                "DEPT": department,
                "PROCESS": process,
                "ndjhs": "1717" if process == "切割" else str(1000 + index),
                "JHYLJ": "850" if process == "切割" else str(500 + index),
                "NDSJS": "686" if process == "切割" else str(400 + index),
                "RECORDDATE": "2025-07-10",
            }
        )

    ship_rows = [
        {
            "OID": "L001",
            "SHIPNO": "H2705",
            "M_YJTYPE": "实际",
            "QG_NUM": "210",
            "XZL_NUM": "210",
            "RECORDDATE": "2025-07-09",
        }
    ]

    rows_by_table = {
        "dwr_mps_ttyear_wide": status_rows,
        "dwr_mps_ppdataprocess_wide": process_rows,
        "dwr_mps_ppdatalast_wide": ship_rows,
    }
    for table, headers in schema.items():
        path = raw_root / f"{table}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            for partial in rows_by_table[table]:
                writer.writerow({header: partial.get(header, "") for header in headers})

    return raw_root
