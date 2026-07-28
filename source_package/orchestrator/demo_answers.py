"""Approved fixed input script for the P6 demonstration session."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


PRETEST_ANSWERS: Mapping[str, str] = MappingProxyType(
    {
        "PT-1": "B",
        "PT-2": "A",
        "PT-3": "C",
        "PT-4": "A",
        "PT-5": "C",
    }
)
FIRST_TASK_ID = "T-01"
CONCLUSION_TASK_ID = "T-01"
TARGET_MISCONCEPTION = "M-01"
FIRST_CONCLUSION_RESULT = "wrong"
CORRECTED_CONCLUSION_RESULT = "correct"
