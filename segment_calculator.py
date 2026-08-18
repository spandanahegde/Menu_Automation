"""ZCTA customer-segment calculator.

The calculator works from raw ACS household-income counts for the selected
ZCTA and derives the report-facing Value / Premium / Premium Edge shares
from those underlying counts. The displayed percentages are normalized to
100.00% after rounding, with the final segment absorbing any tiny rounding
residual.
"""

from typing import Any, Mapping


def _get_number(data: Mapping[str, Any], key: str) -> float:
    value = data.get(key, 0)
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def calculate_customer_segments(zcta_data: Mapping[str, Any]) -> dict:
    """Calculate household segment counts and shares for one ZCTA.

    The function expects ACS B19001-derived raw household counts. It does
    not rely on already-rolled percentages as its source of truth.
    """
    family_value_raw = _get_number(zcta_data, "income_lt_25k_count") + _get_number(zcta_data, "income_25k_49k_count")
    premium_raw = _get_number(zcta_data, "income_50k_99k_count") + _get_number(zcta_data, "income_100k_149k_count")
    premium_edge_raw = _get_number(zcta_data, "income_150k_plus_count")
    total_raw = family_value_raw + premium_raw + premium_edge_raw

    total_households = int(round(_get_number(zcta_data, "total_households") or total_raw))
    if total_raw <= 0:
        return {
            "family_value_raw": 0,
            "premium_raw": 0,
            "premium_edge_raw": 0,
            "total_raw": 0,
            "total_households": total_households,
            "family_value_pct": None,
            "premium_pct": None,
            "premium_edge_pct": None,
            "available": False,
            "source": "ACS B19001 raw household counts",
        }

    family_value_pct = round(100.0 * family_value_raw / total_raw, 2)
    premium_pct = round(100.0 * premium_raw / total_raw, 2)
    premium_edge_pct = round(100.0 * premium_edge_raw / total_raw, 2)

    # Fix only the rounding residue, not the underlying ratios.
    rounding_delta = round(100.0 - (family_value_pct + premium_pct + premium_edge_pct), 2)
    if rounding_delta:
        premium_edge_pct = round(premium_edge_pct + rounding_delta, 2)

    return {
        "family_value_raw": int(round(family_value_raw)),
        "premium_raw": int(round(premium_raw)),
        "premium_edge_raw": int(round(premium_edge_raw)),
        "total_raw": int(round(total_raw)),
        "total_households": total_households,
        "family_value_pct": family_value_pct,
        "premium_pct": premium_pct,
        "premium_edge_pct": premium_edge_pct,
        "available": True,
        "source": "ACS B19001 raw household counts",
    }
