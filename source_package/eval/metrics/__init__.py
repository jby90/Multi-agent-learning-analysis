"""Deterministic P7 metrics."""

from __future__ import annotations

from decimal import Decimal


def fixed_rate(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0.000000"
    return format(Decimal(numerator) / Decimal(denominator), ".6f")


__all__ = ["fixed_rate"]
