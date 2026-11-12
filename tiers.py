"""Pure helpers for BoligWatch v3.2 rank tiers, commute bands, and legend metadata.

Imports config only; no database or external dependencies.
"""
from typing import Any, Dict, Optional, Tuple

import config


def rank_tier_of(score: Any) -> str:
    """Return the rank letter (S, A, B, C, D) corresponding to a numeric score.

    Defaults to 'D' if score is None, negative, or unparseable.
    """
    if score is None:
        return "D"
    try:
        val = float(score)
    except (ValueError, TypeError):
        return "D"

    for letter, threshold in config.RANK_TIER_BANDS:
        if val >= threshold:
            return letter
    return "D"


def rank_range(letter: str) -> Tuple[int, int]:
    """Return the (min_score, max_score) inclusive range for a given rank tier letter."""
    bands = list(config.RANK_TIER_BANDS)
    for idx, (l, min_val) in enumerate(bands):
        if l == letter:
            if idx == 0:
                max_val = 100
            else:
                max_val = bands[idx - 1][1] - 1
            return (min_val, max_val)
    return (0, 0)


def commute_band_name(tier: Optional[str]) -> str:
    """Return human-readable commute band name (Close, Commutable, Far), or empty string."""
    if not tier:
        return ""
    return config.COMMUTE_BAND_NAMES.get(tier, "")


def over_budget_kr(row: Optional[Dict[str, Any]]) -> Optional[int]:
    """Return excess rent above standard ceiling in DKK when budget_band is 'stretch'."""
    if not row or not isinstance(row, dict):
        return None
    if row.get("budget_band") != "stretch":
        return None
    rent = row.get("rent_total")
    if rent is None:
        return None
    try:
        return int(rent) - config.TIER_AB_RENT_MAX
    except (ValueError, TypeError):
        return None


def legend_entries() -> Dict[str, Any]:
    """Return dictionary with metadata for dashboard and static map legend."""
    ranks = []
    for letter, _ in config.RANK_TIER_BANDS:
        r_min, r_max = rank_range(letter)
        ranks.append({
            "letter": letter,
            "name": config.RANK_TIER_NAMES.get(letter, ""),
            "min": r_min,
            "max": r_max,
            "blurb": config.RANK_TIER_BLURBS.get(letter, ""),
        })

    bands = []
    for letter in ("A", "B", "C"):
        bands.append({
            "letter": letter,
            "name": config.COMMUTE_BAND_NAMES.get(letter, ""),
            "blurb": config.COMMUTE_BAND_BLURBS.get(letter, ""),
        })

    cap = {
        "ceiling": config.RENT_CAP_MAX,
        "max": config.RENT_CAP_MAX,
    }

    return {
        "ranks": ranks,
        "bands": bands,
        "cap": cap,
    }
