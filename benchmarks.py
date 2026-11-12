"""Market benchmarks and the value model for BoligWatch v3.0.

The user is thrifty: what matters is the lowest DKK per m2 relative to the
market, not the lowest absolute rent. Benchmarks are built from everything we
have observed in the last BENCHMARK_WINDOW_DAYS days, not only from matches,
so market intel comes from the whole scan history.

Segment key chain, first segment with at least BENCHMARK_MIN_N samples wins:
  postcode|tier, postcode, tier, all
"""
import datetime
import re
import statistics
from typing import Any, Dict, List, Optional, Tuple

import config


def clamp(x: float) -> float:
    return min(1.0, max(0.0, float(x)))


def tier_for_commute(commute_min: Optional[int]) -> Optional[str]:
    """Map door-to-door commute minutes onto a commute tier letter."""
    if commute_min is None:
        return None
    if commute_min <= config.TIER_A_MAX_MIN:
        return "A"
    if commute_min <= config.TIER_B_MAX_MIN:
        return "B"
    if commute_min <= config.TIER_C_MAX_MIN:
        return "C"
    return None


def postcode_from_address(address: Optional[str]) -> Optional[str]:
    """Pull the four-digit Danish postcode out of a normalised DAWA address."""
    if not address:
        return None
    m = re.search(r"\b(\d{4})\b", str(address))
    return m.group(1) if m else None


# ==============================================================================
# Benchmark computation
# ==============================================================================
def _quartiles(values: List[float]) -> Tuple[float, float, float]:
    """(p25, median, p75) tolerant of very small samples."""
    vals = sorted(values)
    median = statistics.median(vals)
    if len(vals) < 2:
        return median, median, median
    q = statistics.quantiles(vals, n=4, method="inclusive")
    return q[0], median, q[2]


def collect_population(store: Any, now: Optional[datetime.datetime] = None) -> List[Dict[str, Any]]:
    """Latest observation per source listing inside the benchmark window.

    Only rows with a usable rent, a size of at least BENCHMARK_MIN_SIZE_M2 and a
    room count of 1, 2 or unknown take part, regardless of whether the listing
    ever matched.
    """
    now = now or datetime.datetime.now()
    since = (now - datetime.timedelta(days=config.BENCHMARK_WINDOW_DAYS)).isoformat(timespec="seconds")

    cur = store.conn.execute("""
        SELECT o.rent_total, o.observed_at, p.id AS property_id, p.size_m2, p.rooms,
               p.normalized_address, p.postcode, p.lat, p.lon
        FROM observations o
        JOIN (SELECT source_listing_id, MAX(id) AS mx
              FROM observations WHERE observed_at >= ?
              GROUP BY source_listing_id) latest
          ON o.id = latest.mx
        JOIN properties p ON p.id = o.property_id
        WHERE o.rent_total > 0
          AND p.size_m2 >= ?
          AND (p.rooms IS NULL OR p.rooms IN (1, 2))
    """, (since, config.BENCHMARK_MIN_SIZE_M2))

    rows = []
    for r in cur.fetchall():
        d = dict(r)
        d["kr_per_m2"] = d["rent_total"] / d["size_m2"]
        d["postcode"] = d.get("postcode") or postcode_from_address(d.get("normalized_address"))
        rows.append(d)
    return rows


def _tier_lookup(store: Any) -> Dict[str, str]:
    """coord_key -> tier letter, from the cached place_features commute times."""
    cur = store.conn.execute(
        "SELECT coord_key, commute_min FROM place_features WHERE commute_min IS NOT NULL")
    return {r["coord_key"]: tier_for_commute(r["commute_min"]) for r in cur.fetchall()}


def compute_benchmarks(store: Any, now: Optional[datetime.datetime] = None,
                       persist: bool = True) -> Dict[str, Dict[str, Any]]:
    """Recompute every market segment and (by default) store it."""
    import features  # imported here so benchmarks stays importable without geo data

    now = now or datetime.datetime.now()
    rows = collect_population(store, now)
    tiers = _tier_lookup(store)

    buckets: Dict[str, List[float]] = {}

    def add(key: str, value: float) -> None:
        buckets.setdefault(key, []).append(value)

    for r in rows:
        kr = r["kr_per_m2"]
        postcode = r.get("postcode")
        tier = None
        if r.get("lat") is not None and r.get("lon") is not None:
            tier = tiers.get(features.coord_key(r["lat"], r["lon"]))
        add("all", kr)
        if postcode:
            add(postcode, kr)
        if tier:
            add(tier, kr)
        if postcode and tier:
            add(f"{postcode}|{tier}", kr)

    segments = {}
    for key, values in buckets.items():
        p25, median, p75 = _quartiles(values)
        segments[key] = {
            "n": len(values),
            "median_kr_m2": round(median, 2),
            "p25_kr_m2": round(p25, 2),
            "p75_kr_m2": round(p75, 2),
        }

    if persist and segments:
        store.save_benchmarks(segments, now.isoformat(timespec="seconds"))
    return segments


def lookup_segment(segments: Dict[str, Dict[str, Any]], postcode: Optional[str],
                   tier: Optional[str]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """First segment in the chain with at least BENCHMARK_MIN_N samples."""
    chain = []
    if postcode and tier:
        chain.append(f"{postcode}|{tier}")
    if postcode:
        chain.append(postcode)
    if tier:
        chain.append(tier)
    chain.append("all")

    for key in chain:
        seg = segments.get(key)
        if seg and seg.get("n", 0) >= config.BENCHMARK_MIN_N and seg.get("median_kr_m2"):
            return key, seg
    return None, None


# ==============================================================================
# Per-listing value
# ==============================================================================
def value_fields(rent_total: Optional[float], size_m2: Optional[float],
                 segments: Dict[str, Dict[str, Any]], postcode: Optional[str],
                 tier: Optional[str]) -> Dict[str, Any]:
    """kr per m2, ratio against the market median, points and display label."""
    out = {
        "kr_per_m2": None, "value_ratio": None, "value_pts": 0.0,
        "value_label": "size unknown", "benchmark_median": None, "benchmark_segment": None,
    }
    if not rent_total or rent_total <= 0:
        return out

    seg_key, seg = lookup_segment(segments, postcode, tier)
    if seg:
        out["benchmark_median"] = seg["median_kr_m2"]
        out["benchmark_segment"] = seg_key

    if not size_m2 or size_m2 < config.BENCHMARK_MIN_SIZE_M2:
        # No size: fall back to a damped absolute-rent proxy.
        out["value_pts"] = (config.SCORE_VALUE_NO_SIZE_FACTOR * config.SCORE_VALUE_PTS
                            * clamp((config.SCORE_VALUE_NO_SIZE_RENT_MAX - rent_total)
                                    / config.SCORE_VALUE_NO_SIZE_DENOM))
        return out

    kr_per_m2 = rent_total / size_m2
    out["kr_per_m2"] = round(kr_per_m2, 1)

    if not seg:
        out["value_label"] = "no market benchmark"
        out["value_pts"] = (config.SCORE_VALUE_NO_SIZE_FACTOR * config.SCORE_VALUE_PTS
                            * clamp((config.SCORE_VALUE_NO_SIZE_RENT_MAX - rent_total)
                                    / config.SCORE_VALUE_NO_SIZE_DENOM))
        return out

    ratio = kr_per_m2 / seg["median_kr_m2"]
    out["value_ratio"] = round(ratio, 4)
    span = config.SCORE_VALUE_ZERO_RATIO - config.SCORE_VALUE_FULL_RATIO
    out["value_pts"] = config.SCORE_VALUE_PTS * clamp((config.SCORE_VALUE_ZERO_RATIO - ratio) / span)
    out["value_label"] = f"{round((ratio - 1) * 100):+d}% vs market"
    return out


def summary_line(segments: Dict[str, Dict[str, Any]]) -> str:
    """One-line market summary for the digest."""
    parts = []
    for tier in ("A", "B", "C"):
        seg = segments.get(tier)
        if seg:
            parts.append(f"{tier} {round(seg['median_kr_m2'])} kr/m2")
    total = segments.get("all", {}).get("n", 0)
    if not parts:
        return f"Market ({config.BENCHMARK_WINDOW_DAYS} d): no segment data · n={total}"
    return f"Market ({config.BENCHMARK_WINDOW_DAYS} d): " + " · ".join(parts) + f" · n={total}"
