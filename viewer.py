"""Local web dashboard and viewer for BoligWatch v3.3.

Four dataset views over the same data (spec 07 section 7): Trusted blocks, the
grouped ranked matches and the default view; Needs verification, Recently gone
and Excluded, which are one row per source with a "Why this is not ranked"
disclosure and a Leaflet map of every match, both station networks and the
waterfront. Serves on http://127.0.0.1:58240 and exports a standalone report
with the map data inline. STRICT RULES: Zero emojis.
"""
import argparse
import datetime
import gzip
import hashlib
import html
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from profile import prescan_profile
prescan_profile()

import config
import features
import geo
import notify
import tiers
from store import Store

# Coastline, harbour and canal edges only: the lakes add 183 polylines that
# clutter the map and bloat the static export without saying anything the
# water column does not already say.
MAP_WATER_KINDS = ("coast", "harbour", "canal")
MAP_WATER_MAX_POINTS = 6000


# ==============================================================================
# Payload builders (shared by the live API and the static export)
# ==============================================================================
def next_scheduled(now: datetime.datetime, times) -> datetime.datetime:
    """Return the next configured wall-clock time strictly after ``now``."""
    parsed = sorted(datetime.time.fromisoformat(value) for value in times)
    for value in parsed:
        candidate = datetime.datetime.combine(now.date(), value, tzinfo=now.tzinfo)
        if candidate > now:
            return candidate
    return datetime.datetime.combine(now.date() + datetime.timedelta(days=1), parsed[0], tzinfo=now.tzinfo)


def dashboard_stats(store: Store, exported_at: Optional[str] = None,
                    trusted_rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Stats plus dashboard-only freshness metadata."""
    now = datetime.datetime.now().astimezone()
    out = store.get_stats()
    rows = listing_rows(store) if trusted_rows is None else trusted_rows
    groups = [row.get("units") or [row] for row in rows]
    # These are distinct housing options, not independent property rows from
    # the same campus. A complex can contain several ranks/date categories.
    out["total_matches"] = len(rows)
    out["rank_counts"] = {letter: sum(any(notify.display_tier_of(unit) == letter
                                        for unit in units) for units in groups)
                          for letter in config.RANK_TIER_LETTERS}
    out["actionable_count"] = sum(any(u.get("relevance") == "actionable" for u in units)
                                  for units in groups)
    out["intel_count"] = sum(any(u.get("relevance") == "intel" for u in units) for units in groups)
    for category, band in ((1, "A"), (2, "B"), (3, "C")):
        out[f"cat{category}_count"] = sum(any(notify.tier_of(u) == band for u in units)
                                            for units in groups)
    out["summary_scope"] = "buildings_or_complexes"
    out["last_scans"] = store.get_last_scan_runs()
    out["next_scan_at"] = next_scheduled(now, config.SCAN_TIMES).isoformat(timespec="seconds")
    # Three different facts, never collapsed into one "updated" time (spec 06
    # section 3.3): when each portal was last looked at and what that look
    # proved, when the current rules were last applied, and how old the newest
    # stored observation is.
    out["portal_coverage"] = store.portal_coverage_summary()
    local_run = store.last_local_run() or {}
    out["local_rules"] = {
        "applied_at": local_run.get("completed_at"),
        "rules_version": local_run.get("rules_version"),
        "parser_version": local_run.get("parser_version"),
        "listings_evaluated": local_run.get("listings_evaluated"),
        "enriched": bool(local_run.get("enrich")),
    }
    # Cards the scan stored but deliberately did not open a detail page for.
    # They are never nothing: the reason is recorded and shown (spec 06 s10).
    out["card_only"] = [
        {"detail_state": row[0], "count": row[1],
         "label": _CARD_ONLY_REASONS.get(row[0], row[0])}
        for row in store.conn.execute(
            "SELECT detail_state, COUNT(*) FROM source_listings "
            "WHERE detail_state IS NOT NULL AND detail_state != 'fetched' "
            "GROUP BY 1 ORDER BY 2 DESC")]
    newest = store.conn.execute(
        "SELECT MAX(observed_at) FROM observations").fetchone()
    out["snapshot_observed_at"] = newest[0] if newest else None
    if exported_at:
        out["exported_at"] = exported_at
    return out


_CARD_ONLY_REASONS = {
    "skipped:rent": f"card only, detail not fetched (rent above "
                    f"{config.DISCOVERY_RENT_MAX:,})".replace(",", "."),
    "skipped:rooms": "card only, detail not fetched (3 or more rooms)",
    "skipped:type": "card only, detail not fetched (room, house or kollegie)",
    "skipped:card_class": "card only, detail not fetched (classified as a room)",
    "skipped:fresh": "detail page stored earlier and still fresh",
    "failed": "detail fetch failed",
    "failed:gone": "detail page reports the listing is gone",
}


def _days_since(timestamp: Optional[str]) -> Optional[int]:
    if not timestamp:
        return None
    try:
        seen = datetime.datetime.fromisoformat(str(timestamp))
    except ValueError:
        return None
    return max(0, (datetime.datetime.now() - seen.replace(tzinfo=None)).days)


def listing_rows(store: Store, limit: int = 500) -> List[Dict[str, Any]]:
    """One representative per property/building/complex, §3.6 order, dismissed included.

    Props like RP badge and the 'More in building' tag ride on the representative
    rows produced by store.grouped_matches, so the static export and the local
    server render the identical grouped view.
    """
    rows = store.grouped_matches(limit=limit, include_dismissed=True, order="score")
    states = store.get_action_states()
    props = {r["id"]: dict(r) for r in store.conn.execute(
        "SELECT id, lat, lon, postcode, geo_precision FROM properties").fetchall()}

    for r in rows:
        st = states.get(r.get("property_id"))
        r["shortlist_state"] = st["action"] if st else ""
        r["shortlist_note"] = st["note"] if st else ""
        p = props.get(r.get("property_id")) or {}
        r["lat"], r["lon"] = p.get("lat"), p.get("lon")
        r["postcode"] = p.get("postcode")
        r["lifecycle_state"] = (r.get("lifecycle_state")
                                or store.lifecycle_state_for(r.get("property_id")))
        # A card the scan deliberately did not open a detail page for is shown
        # as exactly that, with the reason, rather than as a row with no data.
        detail_state = str(r.get("detail_state") or "")
        r["card_only"] = detail_state.startswith(("skipped:", "failed"))
        r["card_only_reason"] = _CARD_ONLY_REASONS.get(detail_state) if r["card_only"] else None
        r["expired_recently"] = bool(r.get("lifecycle_state") == "expired"
                                     and r.get("expired_at"))
        if r["expired_recently"] and r.get("last_seen_days_ago") is None:
            r["last_seen_days_ago"] = _days_since(r.get("observed_at"))
        if not r.get("tier"):
            r["tier"] = config.TIER_LETTERS.get(r.get("category") or 1, "A")
        # The letter a human reads: rank from 0-100 score.
        r["display_tier"] = notify.display_tier_of(r)
        r["commute_band"] = notify.tier_of(r)
        r["commute_band_name"] = tiers.commute_band_name(r["commute_band"])
        r["over_budget_kr"] = None  # the stretch band was removed in v3.3
        # A block of flats is one property with many live listings. The row shows
        # the representative unit, so it says what else is in the building/complex.
        member_ids = r.get("group_members") or [r.get("property_id")]
        r["units"] = store.eligible_unit_rows(member_ids)
        for unit in r["units"]:
            unit["comparison"] = _comparison_snapshot(store, unit)
            unit["commute_band"] = notify.tier_of(unit)
            unit["commute_band_name"] = tiers.commute_band_name(unit["commute_band"])
            state = states.get(unit.get("property_id")) or {}
            unit["shortlist_state"] = state.get("action", "")
        r["unit_count"] = len(r["units"])
        r["source_count"] = store.conn.execute(
            "SELECT COUNT(*) FROM source_listings WHERE property_id IN (" +
            ",".join("?" for _ in member_ids) + ") AND lifecycle_state IN ('active','missing','relisted')",
            member_ids).fetchone()[0]
        r["unit_rent_min"] = r.get("group_price_min")
        r["unit_rent_max"] = r.get("group_price_max")
        r["comparison"] = _comparison_snapshot(store, r)

    # A browser-local comparison is only restored when it belongs to this exact
    # snapshot.  That avoids a rebuilt local database turning an old selection
    # into an unrelated home just because its numeric id was reused.
    identity = "|".join(
        f"{r['comparison']['subject_id']}:{r['comparison'].get('observation_id') or ''}"
        for r in sorted([item for row in rows for item in [row, *row["units"]]],
                        key=lambda item: item["comparison"]["subject_id"])
    )
    dataset_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    for r in rows:
        r["comparison"]["dataset_id"] = dataset_id
        for unit in r["units"]:
            unit["comparison"]["dataset_id"] = dataset_id
    return rows


def _comparison_snapshot(store: Store, row: Dict[str, Any]) -> Dict[str, Any]:
    """Return facts for the exact source listing represented by a match row.

    Grouping is useful in the main list, but it is unsafe for a decision desk:
    sibling units can have different dates, costs and outlook.  This small read
    model deliberately uses the match's site/listing id and leaves missing
    fields unknown rather than borrowing values from the building.
    """
    source = store.conn.execute("""
        SELECT sl.id AS source_listing_id, sl.site, sl.listing_id, sl.url,
               sl.lifecycle_state, sl.raw_data,
               sl.eligibility_evidence_state, sl.eligibility_checked_at,
               o.id AS observation_id, o.observed_at, o.rent_base,
               o.utilities_amount, o.mandatory_monthly_fees, o.rent_total,
               o.deposit, o.prepaid_rent, o.first_month_charge,
               o.mandatory_onetime_fees, o.move_in_cost,
               o.move_in_cost_verified, o.available_from,
               o.available_from_parsed
        FROM source_listings sl
        LEFT JOIN properties p ON p.id = sl.property_id
        LEFT JOIN observations o ON o.id = (
            SELECT latest.id FROM observations latest
            WHERE latest.source_listing_id = sl.id
            ORDER BY latest.observed_at DESC, latest.id DESC LIMIT 1
        )
        WHERE sl.site = ? AND sl.listing_id = ?
        ORDER BY sl.id DESC LIMIT 1
    """, (row.get("site"), str(row.get("listing_id")))).fetchone()
    source_data = dict(source) if source else {}
    try:
        raw = json.loads(source_data.get("raw_data") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = {}

    evaluation = store.current_evaluation(source_data.get("source_listing_id")) if source else None
    facts = json.loads((evaluation or {}).get("unit_facts") or "{}")
    for field in ("rent_total", "move_in_cost", "move_in_cost_verified"):
        if facts.get(field) is not None:
            source_data[field] = facts[field]
    if facts.get("available_date_str") is not None:
        source_data["available_from_parsed"] = facts["available_date_str"]

    def known(name: str, fallback=None):
        value = source_data.get(name)
        return fallback if value is None else value

    source_id = source_data.get("source_listing_id")
    return {
        "subject_id": f"source:{source_id}" if source_id else f"property:{row.get('property_id')}",
        "subject_scope": "source_listing" if source_id else "property",
        "source_listing_id": source_id,
        "observation_id": source_data.get("observation_id"),
        "snapshot_at": source_data.get("observed_at") or row.get("first_seen_at"),
        "source": source_data.get("site") or row.get("site"),
        "source_url": source_data.get("url") or row.get("url"),
        "lifecycle_state": source_data.get("lifecycle_state") or row.get("lifecycle_state"),
        "eligibility_evidence_state": source_data.get("eligibility_evidence_state"),
        "eligibility_checked_at": source_data.get("eligibility_checked_at"),
        "exact_unit": raw.get("title") or row.get("title"),
        "rooms": raw.get("rooms"),
        "rent_base": known("rent_base", row.get("rent_base")),
        "utilities_amount": known("utilities_amount"),
        "mandatory_monthly_fees": known("mandatory_monthly_fees"),
        "rent_total": known("rent_total", row.get("rent_total")),
        "move_in_cost": known("move_in_cost", row.get("move_in_cost")),
        "move_in_cost_verified": known("move_in_cost_verified", row.get("move_in_cost_verified")),
        "deposit": known("deposit"),
        "prepaid_rent": known("prepaid_rent"),
        "first_month_charge": known("first_month_charge"),
        "mandatory_onetime_fees": known("mandatory_onetime_fees"),
        "available_from": known("available_from", row.get("available_from")),
        "available_from_parsed": known("available_from_parsed", row.get("available_from")),
        "photo_url": raw.get("photo_url") or row.get("photo_url"),
        # Street proximity is building-level context only.  Never present it as
        # proof of the outlook from this particular studio.
        "major_street": row.get("major_street"),
        "window_view": "unknown",
        "window_facing": "unknown",
    }


def _station_features() -> List[Dict[str, Any]]:
    out = []
    for path, network in ((config.METRO_JSON, "Metro"), (config.S_TOG_JSON, "s-tog")):
        try:
            with open(path, encoding="utf-8") as f:
                stations = json.load(f)["stations"]
        except Exception:
            continue
        for s in stations:
            out.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                "properties": {"name": s["name"], "network": network,
                               "office": s["name"] == config.OFFICE_NAME},
            })
    return out


def _water_features() -> List[Dict[str, Any]]:
    """Coast, harbour and canal polylines, budgeted so the export stays small."""
    out, budget = [], MAP_WATER_MAX_POINTS
    edges = [e for e in features.load_water_edges() if e.get("kind") in MAP_WATER_KINDS]
    edges.sort(key=lambda e: -len(e.get("points") or []))
    for e in edges:
        pts = e.get("points") or []
        if len(pts) < 2:
            continue
        # Thin long polylines rather than dropping them outright.
        step = max(1, len(pts) // 200)
        thinned = pts[::step]
        if len(thinned) > budget:
            break
        budget -= len(thinned)
        out.append({
            "type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [[p[1], p[0]] for p in thinned]},
            "properties": {"name": e.get("name"), "kind": e.get("kind")},
        })
    return out


def map_payload(store: Store, rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """GeoJSON for matches, both station networks and the waterfront.

    `rows` is the Trusted blocks dataset: map pins are built from trusted rows
    only, so a review or excluded source can never be pinned (spec 07 section 7).
    """
    rows = listing_rows(store) if rows is None else rows
    matches = []
    for r in rows:
        if r.get("lat") is None or r.get("lon") is None:
            continue
        matches.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {k: r.get(k) for k in (
                "bw_id", "property_id", "tier", "display_tier", "commute_band",
                "commute_band_name", "over_budget_kr", "features_pending",
                "budget_band", "score", "address", "rent_total",
                "score_breakdown",
                "size_m2", "kr_per_m2", "value_label", "value_ratio", "commute_min",
                "commute_route", "buzz_detail", "water_dist_m", "water_name",
                "relevance", "shortlist_state", "url", "available_from",
                "unit_count", "unit_rent_min", "unit_rent_max",
                "is_responsible_pick", "more_in_building",
                "group_price_min", "group_price_max", "norreport_distance_m")},
        })
    return {
        "matches": {"type": "FeatureCollection", "features": matches},
        "stations": {"type": "FeatureCollection", "features": _station_features()},
        "water": {"type": "FeatureCollection", "features": _water_features()},
        "office": {"name": config.OFFICE_NAME, "lat": config.OFFICE_COORDS[0],
                   "lon": config.OFFICE_COORDS[1]},
    }


# Spec 07 section 7: the dashboard's four datasets. Only Trusted blocks is
# grouped (one row per property/building); the other three are source-level and
# come from v36q1's read models, never from `matches`.
DASHBOARD_DATASET_KEYS = ("trusted", "review", "gone", "excluded")
# The view the dashboard opens on (section 7 item 1).
DEFAULT_DASHBOARD_VIEW = "trusted"

# Spec 07 section 7: what each view control is called, which count key it labels
# and whether that count is grouped properties/buildings (trusted) or sources.
# The static shell renders one control per entry and `tests/test_viewer.py` pins
# that the shell, the payload counts and these constants agree.
DASHBOARD_VIEW_LABELS = (
    ("trusted", "Ranked", "grouped"),
    ("review", "Needs verification", "source"),
    ("gone", "Recently gone", "source"),
    ("excluded", "Excluded", "source"),
)
DASHBOARD_COUNT_KEYS = {
    "trusted": "trusted_blocks",
    "review": "needs_verification",
    "gone": "recently_gone",
    "excluded": "excluded",
}
# The word each control shows beside its count, so a source-level count is never
# read as a grouped one (section 7: "counts are source-level in review/excluded
# views and clearly labelled").
DASHBOARD_SCOPE_WORDS = {"grouped": "buildings", "source": "sources"}
# The dataset that opens collapsed (section 7 item 4): the reader opens Excluded
# first, then expands its rows.
DASHBOARD_COLLAPSED_VIEWS = ("excluded",)

# Spec 08 section 7: the compact "Evidence problem" filter, one entry per problem:
# (filter value, the exact label the page renders). The `management` and
# `building` arms went with the two unverified labels; `housing` names the
# largest remaining hold - a card-only row whose housing-eligibility text could
# not be read - which is a coverage fact, not a trust fact. The template renders
# these values and `tests/test_viewer.py` pins that the shell and this constant
# agree.
DASHBOARD_EVIDENCE_PROBLEMS = (
    ("kitchen", "Private kitchen unknown"),
    ("lease", "Lease unknown"),
    ("identity", "Property identity unresolved"),
    ("housing", "Housing eligibility unverified"),
)

# Section 4.4's positive-evidence states: the tenancy verdict that may be shown
# as "Professionally managed" and the building verdict that may be shown as
# "Multi-unit block". Declared here and asserted against `config` at import, the
# same pattern as `store.FORBIDDEN_UNIT_TYPE_STATES`, so a vocabulary rename
# fails loudly instead of leaving the label predicate silently matching nothing.
# They are substituted into the page script as its own constants.
TENANCY_PROFESSIONAL_STATE = "professional_direct"
BUILDING_MANAGED_STATE = "managed_multi_unit_block"

# Section 7.1's rank for "private kitchen, bathroom or unlimited lease
# unverified". The Evidence problem filter's kitchen and lease arms read a
# source-level row's own reasons at this rank (v36q1's `store.REASON_PRECEDENCE`),
# so the filter names the fact the row's own disclosure already names.
EVIDENCE_PROBLEM_REASON_RANK = 5

# Spec 08 section 7's two evidence badges, verbatim. A badge is shown only with
# positive evidence, and an absent badge is the whole statement: the release
# deletes the "unverified" twins and every place that rendered them. The page
# script carries these.
MANAGEMENT_VERIFIED_LABEL = "Professionally managed"
BUILDING_VERIFIED_LABEL = "Multi-unit block"

# Spec 09 sections 5.1 and 5.3: the one preference kind v3.8 stores (the only
# value `Store.set_rank_priority` accepts for priority 1) and the badge label the
# ranked card shows for it. The page script carries both, so a rename moves with
# the page. There is no "unpreferred" twin: an absent badge is the whole
# statement for a property with no explicit preference.
PREFERRED_LOCALITY_KIND = "preferred_locality"
PREFERRED_LOCALITY_LABEL = "Preferred locality"

# Section 7.3's four summary keys, verbatim from the pinned migration-summary
# contract (`ORCH_v32/unblock/operator-review-20260915.md`, v36i1 duty 3). The
# page validates the banner's summary against exactly these, as integers.
MIGRATION_SUMMARY_KEYS = ("relinked_sources", "trusted_blocks",
                          "needs_verification", "excluded")

assert tuple(view for view, _label, _scope in DASHBOARD_VIEW_LABELS) == DASHBOARD_DATASET_KEYS
assert set(DASHBOARD_COUNT_KEYS) == set(DASHBOARD_DATASET_KEYS)
assert TENANCY_PROFESSIONAL_STATE in config.TRUST_TENANCY_CHANNEL_STATES, \
    "the professional-advertiser state is not in config's tenancy vocabulary"
assert BUILDING_MANAGED_STATE in config.TRUST_BUILDING_FORM_STATES, \
    "the managed-block state is not in config's building-form vocabulary"


def _dataset_source_key(row: Dict[str, Any]) -> str:
    """The one identity a dataset row is placed by: `site#listing_id`."""
    return f"{row.get('site')}#{row.get('listing_id')}"


def _assert_one_dataset_per_source(datasets: List[Any]) -> None:
    """Fail on one source in two datasets, naming the `site#listing_id`.

    Section 7 lets a source appear in exactly one current view and forbids
    inserting quarantined or rejected rows into `matches` merely to make them
    visible - which is exactly the defect that would place one source in two
    datasets. This is the invariant that fails on it.
    """
    placed: Dict[str, str] = {}
    for name, rows in datasets:
        for row in rows:
            key = _dataset_source_key(row)
            other = placed.get(key)
            if other is not None and other != name:
                raise ValueError(
                    f"source {key} appears in two datasets: {other} and {name}")
            placed[key] = name


def trusted_property_ids(rows: List[Dict[str, Any]]) -> List[int]:
    """Every property id the Trusted blocks dataset holds (its own and its group).

    `rows` is the trusted dataset (`viewer.listing_rows`). Shortlist actions are
    bound to it, so an action may only name a property the trusted view holds: a
    property merely visible in Needs verification, Recently gone or Excluded is
    not actionable (spec 07 section 7).
    """
    ids: List[int] = []
    for row in rows or []:
        candidates = [row.get("property_id")] + list(row.get("group_members") or [])
        for candidate in candidates:
            if candidate is not None and candidate not in ids:
                ids.append(candidate)
    return ids


def dashboard_payload(store: Store, exported_at: Optional[str] = None) -> Dict[str, Any]:
    """The one dashboard payload, served by both dashboard modes (spec 07 section 7).

    Four datasets - `trusted` (the grouped match path, the default view),
    `review`, `gone` and `excluded` (one row per source) - plus clearly scoped
    counts, the integrity state and the map, which is built from the trusted
    rows only. `rows` stays an alias of `trusted` so the current page script
    keeps working until v36u2 rewires it.
    """
    trusted = listing_rows(store)
    review = store.needs_verification_rows()
    gone = store.recently_gone_rows()
    excluded = store.excluded_rows()
    _assert_one_dataset_per_source([("trusted", trusted), ("review", review),
                                    ("gone", gone), ("excluded", excluded)])

    # Section 7: the trusted count is grouped properties/buildings, the other
    # three count sources, and the payload says which is which.
    counts: Dict[str, Any] = dict(store.view_counts())
    counts["trusted_scope"] = "grouped"
    counts["source_scope"] = ["review", "gone", "excluded"]

    # Section 7's operator warning: any current `match` evaluation with a null
    # source/property link, and any forbidden trust state in `matches`.
    integrity: Dict[str, Any] = dict(store.integrity_counts())
    integrity["warning"] = bool(integrity["null_linked_current_matches"]
                                or integrity["forbidden_trust_in_matches"]
                                or integrity.get("explicit_gone_in_matches"))

    return {
        "stats": dashboard_stats(store, exported_at=exported_at, trusted_rows=trusted),
        "trusted": trusted,
        "review": review,
        "gone": gone,
        "excluded": excluded,
        "counts": counts,
        "integrity": integrity,
        # Section 7.3: the first-v3.6 migration summary, from the latest
        # completed local run only, with that run's own stamp. The page never
        # parses `local_runs.detail` itself.
        "migration": store.last_local_run_migration(),
        "default_view": DEFAULT_DASHBOARD_VIEW,
        "map": map_payload(store, trusted),
        # Alias, not a copy: one dataset object, two names.
        "rows": trusted,
    }


# ==============================================================================
# Page
# ==============================================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="da" class="dark" data-default-view="trusted">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>BoligWatch — Copenhagen homes</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0A0E17; --panel: #10161F; --raised: #161F2C; --overlay: #1B2534;
      --line: rgba(148,163,184,.18); --line-strong: #65748B;
      --text: #EDF1F7; --muted: #9AA7BC; --faint: #919EB2;
      --action: #515DC7; --action-hover: #606DD5; --action-subtle: rgba(91,103,216,.16);
      --link: #7DD3FC; --focus: #B7BEFF; --danger: #FB7185; --success: #34D399; --warn: #FCD34D;
      --on-action: #FFFFFF; --danger-bg: #3B1418; --danger-text: #FEE2E2;
      --warn-bg: #332B19; --warn-line: #A98C36;
      --band-a-bg: #1E3A5F; --band-a-text: #93C5FD;
      --band-b-bg: #1E293B; --band-b-text: #CBD5E1;
      --band-c-bg: #27272A; --band-c-text: #B7B7C1;
      --stretch-bg: #3B1D54; --stretch-text: #E9D5FF;
      --rank-s: #F5C542; --rank-a: #34D399; --rank-b: #38BDF8; --rank-c: #94A3B8; --rank-d: #64748B;
      --rank-ink: #0B1220; --rank-d-ink: #FFFFFF;
      --score-affordability: #F97316; --score-transit: var(--rank-b); --score-commute: var(--rank-a);
      --score-water: #60A5FA; --score-centre: #A78BFA; --score-buzz: var(--rank-s);
      --score-area: #2DD4BF; --score-floor: #E879F9;
      --score-value: #FB7185; --score-date: var(--rank-c);
      --map-water: #2563EB; --map-metro: #60A5FA; --map-stog: #F87171;
      --pin-ink: #0B1220; --pin-ring: #FFFFFF;
      --clear: transparent; --shadow-pin: 0 2px 3px rgba(0,0,0,.55); --shadow-pin-big: 0 3px 5px rgba(0,0,0,.7);
      --r-sm: 6px; --r-md: 10px; --r-lg: 14px;
      --shadow-raised: 0 1px 2px rgba(0,0,0,.4); --shadow-overlay: 0 8px 24px rgba(0,0,0,.45);
      --motion: 140ms ease-out;
    }
    :root[data-theme="light"] {
      color-scheme: light;
      --bg: #F5F6F9; --panel: #FFFFFF; --raised: #ECEFF5; --overlay: #FFFFFF;
      --line: #DADEE7; --line-strong: #78859A;
      --text: #182235; --muted: #526078; --faint: #59667C;
      --action: #4955BC; --action-hover: #3F49A5; --action-subtle: #E9EBFA;
      --link: #175B91; --focus: #4955BC; --danger: #B42342; --success: #13754F; --warn: #815B09;
      --danger-bg: #FFF0F2; --danger-text: #8E1830; --warn-bg: #FFF7DF; --warn-line: #A27B23;
      --band-a-bg: #E1EDFF; --band-a-text: #245487;
      --band-b-bg: #E8EDF5; --band-b-text: #47566C;
      --band-c-bg: #EEEEF2; --band-c-text: #565661;
      --stretch-bg: #F0E6FA; --stretch-text: #6E388E;
      --shadow-raised: 0 1px 2px rgba(24,34,53,.08); --shadow-overlay: 0 8px 24px rgba(24,34,53,.14);
    }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    html { background: var(--bg); }
    body { margin: 0; background: var(--bg); color: var(--text); font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; }
    button, select, input { font: inherit; }
    button, select, input[type=search] { min-height: 40px; }
    button { cursor: pointer; }
    .shell { width: min(1440px, calc(100% - 48px)); margin: 0 auto; }
    .site-header { border-bottom: 1px solid var(--line); padding: 20px 0 18px; }
    .header-row { display: flex; gap: 16px; align-items: end; justify-content: space-between; }
    h1 { font-size: 26px; line-height: 1.15; margin: 0 0 4px; letter-spacing: -.02em; }
    .sub, .freshness { color: var(--muted); font-size: .8125rem; }
    .freshblock { margin: 10px 0 4px; padding: 10px 12px; border: 1px solid var(--line);
                  border-radius: 10px; font-size: .8125rem; line-height: 1.6; }
    .freshblock .fl { display: flex; gap: 8px; flex-wrap: wrap; }
    .freshblock .fk { color: var(--muted); min-width: 104px; }
    .cov { padding: 1px 6px; border-radius: 999px; border: 1px solid var(--line); }
    .cov-complete { color: var(--success); border-color: var(--success); }
    .cov-partial, .cov-empty-unconfirmed { color: var(--warn); border-color: var(--warn); }
    .cov-failed { color: var(--danger); border-color: var(--danger); }
    .pill-cardonly { color: var(--warn); border: 1px solid var(--warn); border-radius: 999px;
                     padding: 1px 6px; font-size: .75rem; }
    .pill-expired { color: var(--danger); border: 1px solid var(--danger); border-radius: 999px;
                    padding: 1px 6px; font-size: .75rem; }
    .freshness { text-align: right; max-width: 400px; }
    main { padding: 20px 0 64px; }
    .stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
    .stat { appearance: none; text-align: left; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; color: var(--text); min-height: 88px; }
    .stat[data-filter] { cursor: pointer; }
    .stat.on { border-color: var(--focus); background: var(--action-subtle); box-shadow: inset 0 0 0 1px var(--focus); }
    .stat b { display: block; font-size: 1.75rem; line-height: 1.05; font-variant-numeric: tabular-nums; }
    .stat span { color: var(--muted); font-size: .8125rem; }
    .summary-bottom { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; margin: 12px 0 18px; }
    .summary-label { color: var(--muted); font-size: .8125rem; margin-right: 2px; }
    .rank-chip, .filter-chip, .pill { border-radius: 999px; border: 1px solid var(--line); background: transparent; color: var(--muted); font-size: .8125rem; line-height: 1.2; padding: 6px 10px; }
    button.rank-chip, button.filter-chip { color: var(--text); }
    .rank-chip.on, .filter-chip { border-color: var(--focus); background: var(--action-subtle); }
    .scan-details { margin-left: auto; color: var(--muted); font-size: .8125rem; }
    .scan-details summary { cursor: pointer; color: var(--text); }
    .scan-grid { display: flex; flex-wrap: wrap; gap: 6px 16px; padding: 10px 0 2px; max-width: 680px; }
    .scan-grid span { white-space: nowrap; }
    .toolbar { position: sticky; top: 0; z-index: 10; padding: 10px 0; background: var(--bg); border-bottom: 1px solid var(--line); }
    .toolbar-top { display: flex; gap: 10px; align-items: center; }
    .search-wrap { position: relative; flex: 1 1 300px; }
    .search-wrap label { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
    input[type=search], select { width: 100%; background: var(--panel); color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 8px 11px; }
    input[type=search]::placeholder { color: var(--faint); }
    .view-tabs { display: flex; gap: 6px; }
    .tab-button, .filter-toggle, .secondary-button, .details-button { border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--text); padding: 8px 13px; white-space: nowrap; }
    .tab-button.on { background: var(--action); border-color: var(--action); color: var(--on-action); }
    .filter-toggle[aria-expanded="true"] { border-color: var(--focus); background: var(--action-subtle); }
    .filter-panel { display: grid; grid-template-columns: repeat(3, minmax(160px, 1fr)); gap: 12px; margin-top: 10px; padding: 16px; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; }
    .filter-panel label { display: grid; gap: 5px; color: var(--muted); font-size: .8125rem; }
    .filter-actions { display: flex; flex-wrap: wrap; align-items: end; gap: 8px; }
    .filter-actions button { min-height: 40px; }
    .active-filters { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin: 10px 0 12px; min-height: 26px; }
    .active-filters:empty { display: none; }
    .filter-chip { display: inline-flex; align-items: center; gap: 7px; cursor: pointer; }
    .filter-chip span { font-size: 1rem; line-height: .8; }
    .shown-count { color: var(--muted); font-size: .875rem; margin-left: auto; white-space: nowrap; }
    .notice { margin: 0 0 12px; padding: 10px 12px; background: var(--action-subtle); border: 1px solid var(--line-strong); border-radius: 8px; }
    /* Spec 07 section 7 item 1: the four view controls, and the source-level rows
       the three non-trusted views render. One control per dataset, each carrying
       its own labelled count and scope word. */
    .view-controls { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 6px; }
    .view-control { display: inline-flex; align-items: baseline; gap: 7px; border: 1px solid var(--line); border-radius: 999px; background: var(--panel); color: var(--text); padding: 8px 14px; min-height: 40px; white-space: nowrap; }
    .view-control.on { background: var(--action); border-color: var(--action); color: var(--on-action); }
    .view-control .view-scope { color: var(--muted); font-size: .75rem; }
    .view-control.on .view-scope { color: var(--on-action); }
    .view-control .view-count { font-weight: 700; font-variant-numeric: tabular-nums; }
    .view-note { color: var(--muted); font-size: .8125rem; margin: 8px 0 10px; }
    .view-expand { margin: 0 0 12px; }
    .source-rows { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
    .source-row { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 15px; min-width: 0; }
    .source-address { font-size: 1.0625rem; font-weight: 700; line-height: 1.3; overflow-wrap: anywhere; }
    .source-title { color: var(--muted); font-size: .8125rem; margin-top: 3px; overflow-wrap: anywhere; }
    .source-facts { list-style: none; margin: 10px 0 0; padding: 0; font-size: .8125rem; }
    .source-facts li { margin: 4px 0; color: var(--muted); overflow-wrap: anywhere; }
    .source-facts .source-rent, .source-facts .source-missing-fact, .source-facts .source-reason, .source-facts .source-last-seen, .source-facts .source-site, .source-facts .source-decision { color: var(--text); }
    .source-facts .source-rent { font-size: 1rem; font-weight: 700; font-variant-numeric: tabular-nums; }
    .source-link { display: inline-block; margin-top: 8px; }
    .source-warning { color: var(--warn); }
    /* Spec 08 section 7: a management/building badge is rendered only with its
       own positive evidence - there is no unverified twin - and section 7.2's
       stale flag is never quiet. */
    .source-facts .source-management, .source-facts .source-building { color: var(--text); }
    .source-facts .source-verified::before { content: '\\2713  '; color: var(--success); }
    .source-facts .source-evidence-date { color: var(--text); }
    .source-facts .source-stale { color: var(--warn); font-weight: 700; }
    /* Spec 07 sections 7 and 7.3, the local operator's two surfaces: the
       integrity warning, and the first-v3.6 migration summary. The warning is
       loud on purpose; the banner never renders while it is active. */
    .integrity { margin: 12px 0 0; padding: 12px 14px; border-radius: 10px;
                 background: var(--danger-bg); border: 1px solid var(--danger);
                 color: var(--danger-text); font-size: .875rem; line-height: 1.5; }
    .integrity b { display: block; margin-bottom: 3px; }
    .migration-banner { display: flex; align-items: start; justify-content: space-between;
                        gap: 12px; margin: 12px 0 0; padding: 12px 14px; border-radius: 10px;
                        background: var(--action-subtle); border: 1px solid var(--action);
                        font-size: .875rem; line-height: 1.5; }
    .migration-banner .secondary-button { min-height: 32px; padding: 5px 10px; }
    .why-not-ranked { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--line); }
    .why-not-ranked > summary { cursor: pointer; color: var(--link); font-size: .8125rem; }
    .reason-list { margin: 9px 0 0; padding-left: 18px; font-size: .8125rem; }
    .reason-primary { font-weight: 700; }
    .reason-secondary { color: var(--muted); }
    .reason-meta { color: var(--muted); font-size: .75rem; margin-top: 6px; overflow-wrap: anywhere; }
    .reason-score { display: inline-block; margin-top: 8px; border: 1px solid var(--line); border-radius: 999px; padding: 2px 9px; font-size: .75rem; }
    .reason-link { margin-top: 8px; }
    .table-wrap { overflow: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; }
    table { width: 100%; min-width: 900px; border-collapse: collapse; font-size: .875rem; }
    th, td { padding: 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: .75rem; text-transform: uppercase; letter-spacing: .06em; cursor: pointer; position: sticky; top: 0; background: var(--panel); z-index: 2; }
    th.sorted::after { content: ''; display: inline-block; margin-left: 5px; border-left: 4px solid transparent; border-right: 4px solid transparent; border-bottom: 5px solid var(--text); }
    th.sorted.desc::after { border-bottom: 0; border-top: 5px solid var(--text); }
    td.numeric { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
    td.home { min-width: 235px; }
    .home-address { color: var(--text); font-weight: 650; line-height: 1.3; }
    .home-meta { color: var(--muted); font-size: .8125rem; margin-top: 4px; }
    .cost-amount { font-size: 1rem; font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .cell-meta { color: var(--muted); font-size: .8125rem; margin-top: 3px; }
    tr.dismissed { opacity: .52; }
    tr.flash { animation: flash 1.5s ease-out; }
    @keyframes flash { from { background: var(--action-subtle); } to { background: var(--clear); } }
    .detail-row td { padding: 0 12px 14px; background: var(--raised); }
    .table-extra { display: none; }
    .all-columns .table-extra { display: table-cell; }
    .tier { display: inline-grid; place-items: center; flex: 0 0 auto; width: 26px; height: 26px; border-radius: var(--r-sm); font-weight: 800; font-size: .8125rem; color: var(--rank-ink); }
    .tier.S { background: var(--rank-s); }
    .tier.A { background: var(--rank-a); }
    .tier.B { background: var(--rank-b); }
    .tier.C { background: var(--rank-c); }
    .tier.D { background: var(--rank-d); color: var(--rank-d-ink); }
    .band { display: inline-block; font-size: .75rem; padding: 3px 7px; border-radius: 999px; margin-top: 5px; font-weight: 600; }
    .band.A { background: var(--band-a-bg); color: var(--band-a-text); }
    .band.B { background: var(--band-b-bg); color: var(--band-b-text); }
    .band.C { background: var(--band-c-bg); color: var(--band-c-text); }
    .over { display: inline-block; font-size: 10px; padding: 1px 5px; border-radius: 4px; margin-left: 5px; background: var(--stretch-bg); color: var(--stretch-text); font-weight: 600; cursor: help; }
    .pill { display: inline-block; }
    /* v3.11 (spec 10 section 5.3): soft-flag badges, one class per severity. */
    .pill.flag-info { background: var(--raised); color: var(--muted); border-color: var(--line); }
    .pill.flag-check { background: var(--warn-bg); color: var(--warn); border-color: var(--warn-line); }
    .pill.flag-over { background: var(--danger-bg); color: var(--danger-text); border-color: var(--danger); font-weight: 600; }
    .scorebar { display: inline-flex; width: 72px; height: 7px; margin-top: 6px;
                vertical-align: middle; background: var(--line); border-radius: 3px; overflow: hidden; }
    .scorebar i { display: block; height: 100%; }
    .pending { margin-left: 5px; background: var(--warn-bg); color: var(--warn); border-color: var(--warn-line); }
    .action-btn { margin: 2px 4px 2px 0; padding: 6px 9px; background: var(--action); color: var(--on-action); border: 0; border-radius: 7px; cursor: pointer; font-size: .8125rem; }
    .good { color: var(--success); } .bad { color: var(--danger); }
    a { color: var(--link); }
    .listing-cards { display: none; }
    .listing-card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; }
    .card-top, .card-bottom { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .card-rank { display: flex; align-items: center; gap: 8px; min-width: 0; }
    .card-rank b { font-size: .875rem; white-space: nowrap; }
    .card-address { font-size: 1.125rem; line-height: 1.25; font-weight: 700; margin: 16px 0 2px; overflow-wrap: anywhere; }
    .card-meta { color: var(--muted); font-size: .8125rem; }
    .card-rent { font-size: 1.5rem; line-height: 1.15; font-weight: 800; margin: 18px 0 2px; font-variant-numeric: tabular-nums; }
    .card-journey { margin: 16px 0; padding: 12px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
    .card-journey b { display: block; }
    .card-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; margin-top: 14px; }
    .card-actions .details-button { padding: 6px 0; border: 0; background: transparent; text-decoration: underline; color: var(--link); min-height: 30px; }
    .details-content { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-top: 12px; padding: 13px; background: var(--raised); border: 1px solid var(--line); border-radius: 8px; }
    .detail-group { min-width: 0; }
    .detail-group b { display: block; color: var(--muted); font-size: .75rem; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 3px; }
    .detail-group div { overflow-wrap: anywhere; font-size: .8125rem; }
    .compare-tray { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin: 4px 0 14px; padding: 10px 12px; background: var(--action-subtle); border: 1px solid var(--line-strong); border-radius: 10px; }
    .compare-tray b { margin-right: 2px; }
    .compare-tray .secondary-button { min-height: 34px; padding: 5px 9px; }
    .compare-control { margin-top: 7px; }
    .compare-control.on { background: var(--action); border-color: var(--action); color: var(--on-action); }
    .comparison-desk { margin: 0 0 20px; padding: 16px; background: var(--panel); border: 1px solid var(--line-strong); border-radius: 12px; }
    .desk-head { display: flex; justify-content: space-between; align-items: start; gap: 12px; margin-bottom: 12px; }
    .desk-head h2 { margin: 0; font-size: 1.25rem; }
    .desk-head p { color: var(--muted); margin: 3px 0 0; font-size: .8125rem; }
    .desk-tools { display: flex; align-items: center; flex-wrap: wrap; gap: 9px; }
    .desk-tools label { color: var(--muted); font-size: .8125rem; }
    .compare-columns { display: grid; grid-template-columns: 160px repeat(var(--compare-count, 2), minmax(210px, 1fr)); overflow-x: auto; border: 1px solid var(--line); border-radius: 9px; }
    .compare-label, .compare-value { min-width: 0; padding: 12px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
    .compare-label { color: var(--muted); font-size: .75rem; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; background: var(--raised); }
    .compare-value { font-size: .8125rem; }
    .compare-value strong { display: block; font-size: .9375rem; margin-bottom: 4px; }
    .compare-value:last-child { border-right: 0; }
    .compare-columns > :nth-last-child(-n + var(--compare-count)) { border-bottom: 0; }
    .compare-note { display: block; margin-top: 5px; color: var(--muted); font-size: .75rem; }
    .compare-warning { color: var(--warn); }
    .compare-good { color: var(--success); }
    .compare-remove { float: right; min-height: 28px; padding: 3px 7px; border: 1px solid var(--line); background: transparent; color: var(--muted); border-radius: 6px; }
    .compare-mobile-picker { display: none; }
    .question-list { margin: 7px 0 0; padding-left: 18px; }
    .question-list li { margin: 4px 0; }
    .map-shell { position: relative; }
    #map { height: min(74vh, 780px); min-height: 440px; border-radius: 12px; border: 1px solid var(--line); }
    .map-message { position: absolute; z-index: 500; left: 12px; bottom: 12px; max-width: 280px; padding: 8px 10px; border-radius: 8px; background: var(--overlay); border: 1px solid var(--line); color: var(--muted); font-size: .8125rem; pointer-events: none; }
    .legend { background: var(--overlay); color: var(--text); padding: 10px 12px;
              border-radius: 10px; font-size: 12px; line-height: 1.5; max-width: 320px;
              border: 1px solid var(--line); box-shadow: var(--shadow-overlay); }
    .lg-head { display: flex; justify-content: space-between; align-items: center; font-weight: 700; margin-bottom: 6px; cursor: pointer; }
    .lg-hide { color: var(--muted); font-size: 11px; font-weight: normal; cursor: pointer; }
    .lg-hide:hover { color: var(--text); }
    .lg-sec { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin: 8px 0 4px; font-weight: 600; }
    .lg-row { display: flex; align-items: center; justify-content: space-between; padding: 2px 4px; border-radius: 4px; cursor: pointer; }
    .lg-row:hover { background: var(--raised); }
    .lg-row.on { background: var(--action); color: var(--on-action); }
    .lg-row-left { display: flex; align-items: center; gap: 6px; }
    .lg-row i { display: inline-block; width: 10px; height: 10px; border-radius: 50%; }
    .lg-blurb { padding: 4px 6px 6px 16px; font-size: 11px; color: var(--muted); background: var(--raised); border-radius: 4px; margin: 2px 0 4px; line-height: 1.4; }
    .lg-only { color: var(--link); margin-left: 6px; text-decoration: underline; cursor: pointer; }
    .lg-row.on .lg-only { color: var(--on-action); }
    .lg-marks { font-size: 11px; color: var(--muted); line-height: 1.4; margin-top: 4px; }
    .lg-marks div { margin-bottom: 3px; }
    /* Leaflet gives a divIcon a white box by default, which would frame every
       pin in a card. The marker is the SVG and nothing else. */
    .leaflet-div-icon.pin { background: none; border: none; }
    .pin svg { display: block; filter: drop-shadow(var(--shadow-pin)); }
    .pin-big svg { filter: drop-shadow(var(--shadow-pin-big)); }
    .office-label { background: var(--overlay); color: var(--text); border: none;
                    box-shadow: none; font-size: 11px; font-weight: 600;
                    padding: 2px 6px; }
    .office-label::before { display: none; }
    .leaflet-popup-content-wrapper { background: var(--panel); color: var(--text); border-radius: 10px; }
    .leaflet-popup-tip { background: var(--panel); }
    .pop b { font-size: 14px; } .pop div { margin-top: 4px; font-size: 12px; }
    .empty { padding: 40px; text-align: center; color: var(--muted); }
    @media (max-width: 767px) {
      .shell { width: min(100% - 32px, 640px); }
      .site-header { padding: 16px 0; }
      .header-row { align-items: start; flex-direction: column; gap: 6px; }
      .freshness { text-align: left; }
      main { padding-top: 16px; }
      .stats { gap: 8px; }
      .stat { min-height: 76px; padding: 12px; }
      .stat b { font-size: 1.35rem; }
      .stat span { font-size: .75rem; }
      .scan-details { margin-left: 0; width: 100%; }
      .toolbar { padding-top: 8px; }
      .toolbar-top { flex-wrap: wrap; }
      .search-wrap { flex-basis: 100%; }
      .view-tabs { flex: 1; }
      .tab-button, .filter-toggle { flex: 1; }
      .filter-panel { grid-template-columns: 1fr; padding: 14px; }
      .shown-count { margin-left: 0; }
      .view-controls { gap: 6px; }
      .view-control { flex: 1 1 44%; justify-content: center; }
      .source-rows { grid-template-columns: 1fr; }
      .table-wrap { display: none; }
      .listing-cards { display: grid; gap: 12px; }
      .details-content { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .comparison-desk { padding: 13px; }
      .desk-head { display: block; }
      .desk-tools { margin-top: 12px; }
      .compare-columns { display: block; overflow: visible; }
      .compare-columns .compare-label { display: none; }
      .compare-columns .compare-value { display: none; border-right: 0; }
      .compare-columns .compare-value.mobile-current { display: block; }
      .compare-columns .compare-value.compare-home { display: none; }
      .compare-columns .compare-value.compare-home.mobile-current { display: block; }
      .compare-mobile-picker { display: block; width: 100%; margin: 0 0 10px; }
      #map { height: calc(100vh - 235px); min-height: 420px; border-radius: 10px; }
      .leaflet-control-layers { max-width: 45vw; font-size: 12px; }
    }
    body { font-size: 14px; line-height: 1.5; font-variant-numeric: tabular-nums; }
    h1 { font-size: 26px; }
    h2, h3, .source-address, .card-address { letter-spacing: -.01em; }
    button, a, select, summary { touch-action: manipulation; }
    button, select, input, a { transition: background-color var(--motion), border-color var(--motion), color var(--motion); }
    button:hover:not(:disabled), summary:hover { border-color: var(--line-strong); }
    .action-btn:hover, .tab-button.on:hover, .view-control.on:hover { background: var(--action-hover); }
    button:disabled { cursor: not-allowed; opacity: .6; }
    :focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; }
    input[type=search], select, .secondary-button, .details-button, .filter-toggle { border-color: var(--line-strong); }
    .header-row { align-items: center; }
    .header-tools { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
    .appearance-control { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; }
    .appearance-control select { width: auto; min-height: 34px; padding: 5px 8px; }
    .version { color: var(--muted); font-size: 12px; font-weight: 500; letter-spacing: 0; }
    .freshness { padding: 5px 9px; border: 1px solid var(--line); border-radius: 999px; font-size: 12px; }
    .fresh-summary { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 16px; margin-bottom: 16px; }
    .fresh-status { color: var(--muted); font-size: 12px; flex: 1 1 240px; }
    .scan-details { margin-left: 0; }
    .scan-details[open] { flex-basis: 100%; }
    .scan-details summary { padding: 6px 0; }
    .scan-grid { max-width: none; padding: 8px 12px; }
    .scan-grid span { white-space: normal; }
    .freshblock { margin-bottom: 0; background: var(--panel); }
    .stat { min-height: 68px; padding: 12px 16px; border-radius: var(--r-md); }
    .stat b { font-size: 24px; margin-bottom: 4px; }
    .summary-bottom { margin: 10px 0 16px; }
    .rank-chip { min-height: 32px; padding: 5px 9px; transition-duration: 80ms; }
    .rank-dot { width: 7px; height: 7px; display: inline-block; border-radius: 50%; margin-right: 6px; }
    .search-icon { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); width: 16px; height: 16px; color: var(--muted); pointer-events: none; }
    .search-wrap input { padding-left: 36px; padding-right: 42px; }
    .search-wrap kbd { position: absolute; right: 12px; top: 50%; transform: translateY(-50%); border: 1px solid var(--line-strong); color: var(--muted); border-radius: 4px; padding: 0 5px; font: 12px/20px inherit; pointer-events: none; }
    .view-tabs, .reference-segments { background: var(--raised); border: 1px solid var(--line); border-radius: var(--r-md); padding: 3px; gap: 3px; }
    .view-tabs .tab-button { border: 0; min-height: 34px; padding: 5px 14px; background: var(--clear); }
    .view-tabs .tab-button.on { background: var(--panel); color: var(--text); box-shadow: var(--shadow-raised); }
    .filter-count { display: inline-grid; place-items: center; min-width: 20px; padding: 1px 5px; margin-left: 5px; background: var(--action-subtle); border-radius: 5px; font-size: 12px; }
    .view-control { border-radius: var(--r-sm); font-size: 13px; }
    .view-control.on { background: var(--action-subtle); border-color: var(--focus); color: var(--text); }
    .view-control.on .view-scope { color: var(--muted); }
    .view-control.on:hover { background: var(--raised); }
    th, td { padding: 10px 12px; }
    th { text-transform: none; letter-spacing: 0; backdrop-filter: blur(12px); }
    th[data-sort="rent"], th[data-sort="m2"], th[data-sort="kr_m2"], th[data-sort="water"] { text-align: right; }
    th:focus-visible { outline-offset: -3px; }
    tr[data-bw]:hover { background: var(--raised); }
    td.home { max-width: 320px; }
    .home-address, .home-meta, .pill, .pending { overflow-wrap: anywhere; }
    .table-rank { display: grid; gap: 6px; min-width: 72px; }
    .table-rank .score-number { font-size: 12px; color: var(--muted); }
    .scorebar { height: 5px; margin-top: 0; }
    .table-rank .pending { margin: 0; font-size: 11px; }
    .cost-amount { font-size: 18px; }
    .listing-card, .source-row { border-radius: var(--r-lg); box-shadow: var(--shadow-raised); }
    .listing-card.dismissed { opacity: .65; }
    .listing-card.flash { animation: flash 1.5s ease-out; }
    .card-address { margin-top: 12px; }
    .card-rent { margin-top: 16px; }
    .card-journey { margin: 12px 0; }
    .card-availability { margin-top: 8px; }
    .details-content { gap: 16px; padding: 16px; border-radius: var(--r-md); }
    .detail-group > b { color: var(--text); margin-bottom: 8px; }
    .detail-group > div + div { margin-top: 6px; }
    .detail-group .detail-label { display: block; color: var(--muted); font-weight: 600; margin: 12px 0 4px; }
    .detail-group .preferred-locality-note b { text-transform: none; letter-spacing: 0; }
    .source-facts { display: flex; flex-direction: column; gap: 4px; }
    .source-facts .source-rent { order: -2; font-size: 22px; line-height: 1.3; letter-spacing: -.01em; margin: 4px 0 8px; }
    .source-facts .source-availability { order: -1; }
    .source-facts .source-missing-fact, .source-facts .source-reason { border-left: 2px solid var(--warn); padding-left: 10px; }
    .source-facts .source-management, .source-facts .source-building { color: var(--muted); }
    .why-not-ranked > summary { color: var(--text); padding: 6px 0; }
    .why-not-ranked[open] { background: var(--raised); border-radius: var(--r-sm); padding: 10px; }
    .compare-tray { background: var(--overlay); border-color: var(--line-strong); box-shadow: var(--shadow-overlay); }
    .compare-tray .pill { display: flex; align-items: center; gap: 8px; max-width: 100%; color: var(--text); border-radius: var(--r-sm); }
    .compare-remove { float: none; flex-shrink: 0; }
    .comparison-desk { background: var(--overlay); box-shadow: var(--shadow-overlay); scroll-margin-top: 160px; }
    .desk-head { flex-wrap: wrap; }
    .desk-tools { width: 100%; }
    .reference-segments { display: flex; flex-wrap: wrap; min-width: 0; }
    .reference-segments button { min-width: 0; max-width: 240px; white-space: normal; overflow-wrap: anywhere; background: var(--clear); }
    .reference-segments button[aria-pressed="true"] { background: var(--action); color: var(--on-action); border-color: var(--action); }
    .reference-label { color: var(--muted); font-size: 12px; margin-bottom: 5px; }
    .compare-label { position: sticky; left: 0; z-index: 1; background: var(--raised); }
    .compare-value { background: var(--panel); }
    .compare-value.reference { background: var(--action-subtle); }
    .compare-value[data-section="cost"] > strong { font-size: 22px; }
    .compare-section-title { display: none; }
    .reference-badge { display: inline-block; font-size: 12px; color: var(--muted); margin-bottom: 6px; }
    .map-tools { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 10px; color: var(--muted); font-size: 12px; }
    #map.dark-tiles .leaflet-tile-pane { filter: invert(1) hue-rotate(180deg) brightness(.78) saturate(.55); }
    .leaflet-container { background: var(--raised); font: inherit; }
    .leaflet-control-layers, .leaflet-bar a, .leaflet-control-attribution { background: var(--overlay); color: var(--text); border-color: var(--line); }
    .leaflet-container a { color: var(--link); }
    .leaflet-bar a:hover { background: var(--raised); }
    .leaflet-container a.leaflet-popup-close-button { color: var(--muted); }
    .leaflet-control-layers label { display: flex; align-items: center; }
    .legend { max-height: 55vh; max-width: min(320px, calc(100vw - 72px)); overflow-y: auto; }
    .lg-head, .lg-row { width: 100%; color: inherit; background: var(--clear); border: 0; text-align: left; font: inherit; }
    .lg-row { min-height: 32px; gap: 8px; }
    .lg-hide { color: var(--muted); }
    .leaflet-popup-content { margin: 16px; max-width: calc(100vw - 96px); }
    .pop { min-width: 0; overflow-wrap: anywhere; }
    .pop .card-top { margin: 0 16px 10px 0; }
    .pop .card-address { font-size: 16px; margin: 10px 0 4px; }
    .pop .card-rent { font-size: 24px; margin-top: 14px; }
    .pop .card-journey { margin: 12px 0; }
    .pop .popup-support { color: var(--muted); }
    .pop .card-actions { gap: 8px 12px; }
    .empty { padding: 32px 16px; grid-column: 1 / -1; }
    .empty strong { display: block; font-size: 16px; color: var(--text); margin-bottom: 6px; }
    .empty p { max-width: 480px; margin: 0 auto 16px; }
    .snapshot-warning { background: var(--warn-bg); color: var(--warn); border: 1px solid var(--warn-line); border-radius: var(--r-md); padding: 10px 12px; margin-bottom: 14px; }
    .zero-actionable { color: var(--muted); font-size: 13px; margin: 10px 0; }
    [data-density="compact"] th, [data-density="compact"] td { padding: 6px 10px; }
    [data-density="compact"] .table-wrap button { min-height: 30px; padding: 4px 8px; }
    [data-density="compact"] .home-meta, [data-density="compact"] .cell-meta { margin-top: 2px; }
    [data-density="compact"] .detail-row td { padding: 0 10px 10px; }
    @media (max-width: 1000px) {
      .header-row { align-items: start; }
      .header-tools { justify-content: flex-end; }
      .filter-panel { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 767px) {
      .shell { width: calc(100% - 24px); }
      .header-tools { justify-content: flex-start; width: 100%; margin-top: 8px; }
      .freshness { text-align: left; max-width: 100%; }
      .appearance-control { flex: 1 1 auto; }
      .appearance-control select { flex: 1; min-width: 0; }
      .fresh-summary { gap: 2px; }
      .fresh-status { flex-basis: 100%; }
      .scan-details { width: 100%; }
      .stat { padding: 12px 8px; }
      .stat span { display: block; line-height: 1.3; }
      .view-control { flex: 1 1 100%; flex-wrap: wrap; justify-content: flex-start; white-space: normal; }
      .view-control .view-count { margin-left: auto; }
      .filter-panel { grid-template-columns: minmax(0, 1fr); }
      .toolbar { position: relative; }
      .listing-card { padding: 14px; min-width: 0; }
      .card-top { flex-wrap: wrap; gap: 8px; }
      .pill, .pending { max-width: 100%; }
      .details-content { gap: 16px 12px; padding: 12px; }
      .detail-group button { max-width: 100%; white-space: normal; }
      .comparison-desk { scroll-margin-top: 12px; }
      .desk-tools > div, .reference-segments { width: 100%; }
      .reference-segments button { flex: 1 1 100%; max-width: none; }
      .compare-section-title { display: block; color: var(--muted); font-size: 12px; margin-bottom: 8px; }
      .compare-mobile-picker { max-width: 100%; }
      .compare-tray .pill { flex-wrap: wrap; border-radius: var(--r-sm); }
      .compare-tray { padding: 12px; }
      .leaflet-control-layers { max-width: calc(100vw - 100px); }
      .legend { max-height: 42vh; }
      .leaflet-container .leaflet-bar a { width: 44px; height: 44px; line-height: 44px; }
      .leaflet-container a.leaflet-popup-close-button { width: 44px; height: 44px; line-height: 44px; }
      button, select, input[type=search], summary, .card-actions .details-button, .appearance-control select, .compare-tray .secondary-button, .migration-banner .secondary-button, .view-tabs .tab-button, .lg-row { min-height: 44px; }
      button, summary { min-width: 44px; }
      .card-actions a, .source-link, .reason-link a, .compare-value a, .notice a, .lg-only { display: inline-flex; align-items: center; min-height: 44px; min-width: 44px; }
      .desk-tools label, .leaflet-control-layers label { min-height: 44px; }
      input[type=checkbox] { width: 20px; height: 20px; accent-color: var(--action); }
      .migration-banner { flex-wrap: wrap; }
      .search-wrap kbd { display: none; }
    }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; } }
  </style>
</head>
<body>
<header class="site-header"><div class="shell header-row">
  <div><h1>BoligWatch <span class="version">v3.12</span></h1><div class="sub">Copenhagen homes with a __OFFICE_NAME_HTML__ commute</div></div>
  <div class="header-tools"><div class="freshness" id="freshness" aria-live="polite"></div>
    <label class="appearance-control" for="appearance">Appearance <select id="appearance" onchange="setAppearance(this.value)"><option value="dark">Dark</option><option value="light">Light</option><option value="system">System</option></select></label>
    <label class="appearance-control" for="density">Density <select id="density" onchange="setDensity(this.value)"><option value="comfortable">Comfortable</option><option value="compact">Compact</option></select></label>
  </div>
</div></header>
<main class="shell">
  <section class="fresh-summary" aria-label="Data freshness">
    <div class="fresh-status" id="freshStatus" aria-live="polite"></div>
    <details class="scan-details"><summary>Scan details</summary><div class="freshblock" id="freshBlock" aria-live="polite"></div><div id="scanStats" class="scan-grid"></div></details>
  </section>
  <div id="snapshotWarning" class="snapshot-warning" role="status" hidden></div>
  <section aria-label="Operator status">
    <div class="integrity" id="integrity" role="alert" aria-live="assertive" data-integrity-warning="false" hidden></div>
    <div class="migration-banner" id="migrationBanner" role="status" data-migration-state="suppressed" hidden></div>
  </section>
  <section aria-label="Snapshot summary">
    <div class="stats" id="stats"></div>
    <p class="view-note">Summary counts are buildings or complexes. Units in one complex can have different ranks and availability categories.</p>
    <div class="summary-bottom"><span class="summary-label">Ranks</span><div id="rankChips" style="display:flex;flex-wrap:wrap;gap:6px"></div>
    </div>
    <p id="zeroActionable" class="zero-actionable" hidden>No actionable homes in this snapshot. Intel listings are context, not confirmed application opportunities; review source availability before deciding.</p>
  </section>
  <section aria-label="Listing views">
    <div class="view-controls" role="group" aria-label="Choose listing view">
      <button class="view-control on" data-view="trusted" data-count-key="trusted_blocks" data-scope="grouped" aria-controls="view-trusted" aria-pressed="true" onclick="showDataset('trusted')">Ranked <span class="view-scope">buildings</span> <span class="view-count">0</span></button>
      <button class="view-control" data-view="review" data-count-key="needs_verification" data-scope="source" aria-controls="view-review" aria-pressed="false" onclick="showDataset('review')">Needs verification <span class="view-scope">sources</span> <span class="view-count">0</span></button>
      <button class="view-control" data-view="gone" data-count-key="recently_gone" data-scope="source" aria-controls="view-gone" aria-pressed="false" onclick="showDataset('gone')">Recently gone <span class="view-scope">sources</span> <span class="view-count">0</span></button>
      <button class="view-control" data-view="excluded" data-count-key="excluded" data-scope="source" aria-controls="view-excluded" aria-pressed="false" onclick="showDataset('excluded')">Excluded <span class="view-scope">sources</span> <span class="view-count">0</span></button>
    </div>
  </section>
  <section class="toolbar" aria-label="Listing controls">
    <div class="toolbar-top">
      <div class="search-wrap"><label for="fSearch">Search listings</label><svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/></svg><input type="search" id="fSearch" placeholder="Search address or title in every view" aria-keyshortcuts="/" oninput="render()"><kbd aria-hidden="true">/</kbd></div>
      <div class="view-tabs" role="group" aria-label="Choose view">
        <button id="tabTable" class="tab-button on" aria-pressed="true" onclick="showView('table')">List</button>
        <button id="tabMap" class="tab-button" aria-pressed="false" onclick="showView('map')">Map</button>
      </div>
      <button id="filterToggle" class="filter-toggle" aria-expanded="false" aria-controls="filterPanel" onclick="toggleFilters()">Filters <span id="filterCount" class="filter-count">0</span></button>
    </div>
    <div class="active-filters" id="activeFilters" aria-live="polite"></div>
    <button class="secondary-button" id="candidateCompare" hidden onclick="compareCandidates()">Compare Jorisvej &amp; C.F. Richs Vej 105C</button>
    <div id="filterPanel" class="filter-panel" hidden>
      <label>Rank
      <select id="fTier" onchange="render()">
        <option value="">All</option><option>S</option><option>A</option><option>B</option><option>C</option><option>D</option>
      </select></label>
    <label>Commute
      <select id="fBand" onchange="render()">
        <option value="">All</option><option value="A">Close</option><option value="B">Commutable</option><option value="C">Far</option>
      </select></label>
    <label>Pick
      <select id="fBudget" onchange="render()">
        <option value="">All</option><option value="rp">Responsible Picks only</option>
      </select></label>
    <label>Relevance
      <select id="fRel" onchange="render()">
        <option value="">All</option><option value="actionable">Actionable</option>
        <option value="intel">Intel</option>
      </select></label>
    <label>Shortlist
      <select id="fShort" onchange="render()">
        <option value="hide_dismissed">Hide dismissed</option>
        <option value="">Show all</option>
        <option value="save">Saved</option>
        <option value="applied">Applied</option>
        <option value="dismiss">Dismissed only</option>
      </select></label>
    <label>Evidence problem
      <select id="fProblem" onchange="render()">
        <option value="">All</option>
        <option value="kitchen" data-problem="kitchen">Private kitchen unknown</option>
        <option value="lease" data-problem="lease">Lease unknown</option>
        <option value="identity" data-problem="identity">Property identity unresolved</option>
        <option value="housing" data-problem="housing">Housing eligibility unverified</option>
      </select></label>
    <label>Sort
      <select id="fSort" onchange="sortFromSelect()">
        <option value="rank">Rank, then score</option>
        <option value="band">Commute band, then score</option>
        <option value="score">Score</option>
        <option value="commute">Commute</option>
        <option value="value">Value vs market</option>
        <option value="rent">Rent</option>
      </select></label>
      <div class="filter-actions"><button id="allColumns" class="secondary-button" aria-pressed="false" onclick="toggleColumns()">All columns</button><button class="secondary-button" onclick="resetFilters()">Reset filters</button></div>
    </div>
  </section>

  <div id="notice" class="notice" hidden></div>
  <section id="compareTray" class="compare-tray" aria-label="Comparison tray" hidden></section>
  <section id="comparisonDesk" class="comparison-desk" aria-labelledby="comparisonHeading" hidden></section>
  <div id="listResults">
  <div id="view-trusted" class="view" data-collapsed="false">
  <div id="viewTable" class="table-wrap">
    <table>
      <thead><tr>
        <th data-sort="rank" onclick="sortBy('rank')">Rank</th><th data-sort="address" onclick="sortBy('address')">Home</th><th data-sort="rent" onclick="sortBy('rent')">Monthly cost</th><th data-sort="commute" onclick="sortBy('commute')">Commute</th><th data-sort="available" onclick="sortBy('available')">Availability</th><th data-sort="shortlist" onclick="sortBy('shortlist')">Links & state</th>
        <th class="table-extra" data-sort="score" onclick="sortBy('score')">Score</th><th class="table-extra" data-sort="bw_id" onclick="sortBy('bw_id')">BW-ID</th><th class="table-extra" data-sort="m2" onclick="sortBy('m2')">m²</th><th class="table-extra" data-sort="kr_m2" onclick="sortBy('kr_m2')">kr/m²</th><th class="table-extra" data-sort="value" onclick="sortBy('value')">Market</th><th class="table-extra" data-sort="street" onclick="sortBy('street')">Street</th><th class="table-extra" data-sort="water" onclick="sortBy('water')">Water</th><th class="table-extra" data-sort="relevance" onclick="sortBy('relevance')">Relevance</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
  <div id="viewCards" class="listing-cards"></div>
  </div>
  <div id="view-review" class="view" data-collapsed="false" hidden>
    <div class="source-rows" id="rows-review"></div>
  </div>
  <div id="view-gone" class="view" data-collapsed="false" hidden>
    <div class="source-rows" id="rows-gone"></div>
  </div>
  <div id="view-excluded" class="view" data-collapsed="true" hidden>
    <p class="view-note">Excluded listings are hard rejections: room rentals, sublets, private landlords, student and shared housing, over-budget or failed gates. They are never ranked, mapped, digested or alerted.</p>
    <button class="secondary-button view-expand" id="expandExcluded" data-expand="excluded" aria-controls="rows-excluded" aria-expanded="false" onclick="toggleDatasetRows('excluded')">Show excluded listings</button>
    <div class="source-rows" id="rows-excluded" hidden></div>
  </div>
  </div>
  <div id="viewMap" hidden><div class="map-tools"><button id="darkTiles" class="secondary-button" aria-pressed="true" onclick="toggleDarkTiles()">Dark tiles on · use plain OSM</button><span>Listing colours and map data stay unchanged.</span></div><div class="map-shell"><div id="map" class="dark-tiles"></div><div id="mapMessage" class="map-message" role="status" hidden>Map background unavailable. Listing pins and details are still available.</div></div></div>
</main>

<script>
const STATIC = false;
// The one dashboard payload (spec 07 section 7): four datasets, their scoped
// counts, the integrity state and the trusted-only map. The static export
// replaces this initial value with the inlined payload.
let DATA = { stats: {}, trusted: [], review: [], gone: [], excluded: [], counts: {}, integrity: {}, migration: {}, default_view: 'trusted', map: null };
let map = null, layers = {}, mapBuilt = false, markersByBw = {};
let SORT = { key: 'rank', dir: 'desc' }, ONLY_PENDING = false, SHOW_ALL_COLUMNS = false, ACTIVE_VIEW = 'table';
// Spec 07 section 7: the four datasets, the view the dashboard opens on (read
// from the root's `data-default-view`) and the one dataset that opens collapsed.
const DATASETS = ['trusted', 'review', 'gone', 'excluded'];
const DEFAULT_DATASET = document.documentElement.dataset.defaultView || 'trusted';
let ACTIVE_DATASET = DEFAULT_DATASET;
// Spec 07 sections 7, 7.2 and 7.3: the page's evidence vocabulary. Every value
// comes from `config` through the template - the two verdict states, the
// evidence-age threshold, the `verification stale` label, the rank-5 reason rank
// and the label texts - and `viewer.py` asserts at import that they are config's
// own values, so a rename moves the page with it instead of leaving a predicate
// matching nothing.
const TENANCY_PROFESSIONAL_STATE = __TENANCY_PROFESSIONAL_STATE__;
const BUILDING_MANAGED_STATE = __BUILDING_MANAGED_STATE__;
const MANAGEMENT_VERIFIED_LABEL = __MANAGEMENT_VERIFIED_LABEL__;
const BUILDING_VERIFIED_LABEL = __BUILDING_VERIFIED_LABEL__;
// Spec 09 sections 5.1 and 5.3: the preference kind and the badge label, both
// substituted from the module constants so a rename moves the page with them.
const PREFERRED_LOCALITY_KIND = __PREFERRED_LOCALITY_KIND__;
const PREFERRED_LOCALITY_LABEL = __PREFERRED_LOCALITY_LABEL__;
const EVIDENCE_MAX_DAYS = __EVIDENCE_MAX_DAYS__;
const STALE_REASON = __STALE_REASON__;
const EVIDENCE_PROBLEM_RANK = __EVIDENCE_PROBLEM_RANK__;
const MIGRATION_SUMMARY_KEYS = __MIGRATION_SUMMARY_KEYS__;
// Section 7.3: dismissal is keyed by the run that carried the summary.
const MIGRATION_STORAGE_PREFIX = 'boligwatch.migration.';
// Expired listings stay in local processing for 14 days so a home that vanished
// yesterday is still inspectable, but they are off the board by default: the
// shortlist is for homes you can still apply for.
let ONLY_EXPIRED = false;
const OPEN_DETAILS = new Set();
const OPEN_UNITS = new Set();
const COMPARE = new Set();
let COMPARISON_OPEN = false, DIFFERENCES_ONLY = false, MOBILE_COMPARE_SUBJECT = '', REFERENCE_SUBJECT = '';
const COMPARE_STORAGE_KEY = 'boligwatch.compare.v1';

const token = name => getComputedStyle(document.documentElement).getPropertyValue('--' + name).trim();
const TIER_COLOR = Object.fromEntries(['S', 'A', 'B', 'C', 'D'].map(key => [key, token('rank-' + key.toLowerCase())]));
const LEGEND = __TIER_LEGEND__;
const SCORE_PARTS = __SCORE_PARTS__;
const OFFICE_NAME = __OFFICE_NAME_JSON__;
const TARGET_MOVE_DATE = __TARGET_MOVE_DATE_JSON__;
const PREFERRED_WINDOW_START = __PREFERRED_WINDOW_START_JSON__;
const PART_COLOR = Object.fromEntries(SCORE_PARTS.map(([key]) => [key, token('score-' + key)]));
const APPEARANCE_KEY = 'boligwatch.appearance.v1', DENSITY_KEY = 'boligwatch.density.v1';
const SYSTEM_THEME = window.matchMedia('(prefers-color-scheme: dark)');
let APPEARANCE = 'dark', DARK_TILES = true;
function readPreference(key, fallback) { try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; } }
function savePreference(key, value) { try { localStorage.setItem(key, value); } catch (e) {} }
function applyAppearance() {
  const theme = APPEARANCE === 'system' ? (SYSTEM_THEME.matches ? 'dark' : 'light') : APPEARANCE;
  document.documentElement.dataset.theme = theme;
  document.documentElement.classList.toggle('dark', theme === 'dark');
  document.getElementById('appearance').value = APPEARANCE;
}
function setAppearance(value) {
  APPEARANCE = ['light', 'dark', 'system'].includes(value) ? value : 'dark';
  savePreference(APPEARANCE_KEY, APPEARANCE); applyAppearance();
}
function setDensity(value) {
  const density = value === 'compact' ? 'compact' : 'comfortable';
  document.documentElement.dataset.density = density;
  document.getElementById('density').value = density;
  savePreference(DENSITY_KEY, density);
}
function paintDarkTiles() {
  document.getElementById('map').classList.toggle('dark-tiles', DARK_TILES);
  const button = document.getElementById('darkTiles');
  button.setAttribute('aria-pressed', String(DARK_TILES));
  button.textContent = DARK_TILES ? 'Dark tiles on · use plain OSM' : 'Plain OSM · use dark tiles';
}
function toggleDarkTiles() { DARK_TILES = !DARK_TILES; savePreference('boligwatch.darkTiles.v1', String(DARK_TILES)); paintDarkTiles(); }
function initPresentation() {
  const saved = readPreference(APPEARANCE_KEY, 'dark');
  APPEARANCE = ['light', 'dark', 'system'].includes(saved) ? saved : 'dark';
  applyAppearance(); setDensity(readPreference(DENSITY_KEY, 'comfortable'));
  DARK_TILES = readPreference('boligwatch.darkTiles.v1', 'true') !== 'false'; paintDarkTiles();
  if (SYSTEM_THEME.addEventListener) SYSTEM_THEME.addEventListener('change', () => { if (APPEARANCE === 'system') applyAppearance(); });
  document.querySelectorAll('th[data-sort]').forEach(th => {
    th.tabIndex = 0; th.scope = 'col';
    th.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); th.click(); }
    });
  });
  document.addEventListener('keydown', event => {
    if (event.key !== '/' || event.ctrlKey || event.altKey || event.metaKey || event.isComposing) return;
    if (event.target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"]')) return;
    event.preventDefault(); document.getElementById('fSearch').focus();
  });
}

// Listing pins are the point of the map, so they are drawn as full teardrop
// markers carrying their tier letter. Stations are reference furniture and stay
// as small dots - the office anchor included, since its location is not news.
function pinIcon(letter, color, big, stretch) {
  const w = big ? 34 : 28, h = big ? 46 : 38;
  const fill = big ? color : color + 'cc';
  const stroke = token(stretch ? 'pin-ring' : 'pin-ink');
  const strokeDash = stretch ? ' stroke-dasharray="4 2"' : '';
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" width="' + w + '" height="' + h + '" viewBox="0 0 34 46">' +
    '<path d="M17 45.5C17 45.5 32.5 26.5 32.5 16.5A15.5 15.5 0 0 0 1.5 16.5C1.5 26.5 17 45.5 17 45.5Z" ' +
      'fill="' + fill + '" stroke="' + stroke + '" stroke-width="2.5"' + strokeDash + '/>' +
    '<circle cx="17" cy="16.5" r="9.5" fill="#0b1220" fill-opacity="0.82"/>' +
    '<text x="17" y="21" text-anchor="middle" font-family="system-ui,sans-serif" ' +
      'font-size="13" font-weight="700" fill="#ffffff">' + letter + '</text></svg>';
  return L.divIcon({
    html: svg, className: 'pin' + (big ? ' pin-big' : ''),
    iconSize: [w, h], iconAnchor: [w / 2, h], popupAnchor: [0, -h + 6],
  });
}
const kr = v => (v === null || v === undefined) ? '-' : Number(v).toLocaleString('da-DK');
const esc = s => String(s === null || s === undefined ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function commuteText(m) {
  if (m.commute_min === null || m.commute_min === undefined) return '-';
  let r = m.commute_route;
  if (typeof r === 'string') { try { r = JSON.parse(r); } catch (e) { r = null; } }
  let tail = '';
  if (r && r.mode === 'walk') tail = ' walk';
  else if (r && r.via) tail = ' via ' + r.via;
  return Math.round(m.commute_min) + ' min' + tail;
}

function vsMarket(m) {
  if (m.value_ratio === null || m.value_ratio === undefined) return '<span class="pill">no benchmark</span>';
  const pct = Math.round((m.value_ratio - 1) * 100);
  const cls = pct <= 0 ? 'good' : 'bad';
  return '<span class="' + cls + '">' + Math.abs(pct) + '% ' + (pct <= 0 ? 'below market' : 'above market') + '</span>';
}

function scoreCell(m) {
  const breakdown = m.score_breakdown || {};
  const title = SCORE_PARTS.map(([key, max]) => key[0].toUpperCase() + key.slice(1) + ' ' + (Number(breakdown[key]) || 0) + '/' + max).join(' · ');
  const segments = SCORE_PARTS.map(([key]) => {
    const width = Math.max(0, Math.min(100, Number(breakdown[key]) || 0));
    return '<i style="width:' + width + '%;background:' + PART_COLOR[key] + '"></i>';
  }).join('');
  const pending = m.features_pending ? '<span class="pill pending" title="Street data is pending; the score is provisional">street data pending</span>' : '';
  return '<b>' + esc(m.display_tier || '?') + ' · ' + Math.round(m.score || 0) + '/100</b><span class="scorebar" title="' + esc(title) + '" aria-label="' + esc(title) + '">' + segments + '</span>' + pending;
}

function shortlistCell(m) {
  const state = m.shortlist_state || '';
  let out = state ? '<span class="pill">' + esc(state) + '</span>' : '';
  if (STATIC) return out;
  const button = (label, action) => '<button class="action-btn" onclick="act(' + Number(m.property_id) + ',\\'' + action + '\\')">' + label + '</button>';
  if (!state || state === 'unsave') out += button('Save', 'save') + button('Dismiss', 'dismiss');
  else if (state === 'save') out += button('Applied', 'applied') + button('Unsave', 'unsave') + button('Dismiss', 'dismiss');
  else if (state === 'applied') out += button('Unsave', 'unsave') + button('Dismiss', 'dismiss');
  else if (state === 'dismiss') out += button('Restore', 'unsave');
  return out;
}

function noticeDismissed(bw, pid) {
  const notice = document.getElementById('notice');
  notice.innerHTML = 'Dismissed ' + esc(bw) + ' · <a href="#" onclick="act(' + Number(pid) + ',\\'unsave\\'); return false;">undo</a>';
  notice.removeAttribute('hidden');
  setTimeout(() => notice.setAttribute('hidden', ''), 6000);
}

async function act(pid, action) {
  if (STATIC) return;
  const scrollY = window.scrollY;
  try {
    const response = await fetch('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ property_id: pid, action: action, note: '' }) });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || 'action failed');
    const row = DATA.trusted.find(m => Number(m.property_id) === Number(pid));
    if (!row) throw new Error('property is not in this view');
    row.shortlist_state = result.shortlist_state || '';
    row.shortlist_note = result.shortlist_note || '';
    DATA.map.matches.features.forEach(feature => {
      if (Number(feature.properties.property_id) === Number(pid)) {
        feature.properties.shortlist_state = row.shortlist_state;
        feature.properties.shortlist_note = row.shortlist_note;
      }
    });
    render();
    window.scrollTo(0, scrollY);
    if (action === 'dismiss') noticeDismissed(row.bw_id, pid);
  } catch (err) {
    sayCount(err.message || 'action failed', 4000);
  }
}

// One search box over the address and title of every dataset (spec 07 section
// 7). Trusted blocks keep their own filters; a source-level row has no rank,
// band, shortlist or relevance, so those controls apply to the trusted view.
function searchableText(row, trustedView) {
  if (trustedView) return [row.group_name, row.address, row.title, row.nearest_station, row.bw_id, row.major_street].filter(Boolean).join(' ').toLowerCase();
  return [row.address, row.title].filter(Boolean).join(' ').toLowerCase();
}

function passes(m, trustedScope, exactUnit) {
  const trustedView = trustedScope === undefined ? ACTIVE_DATASET === 'trusted' : Boolean(trustedScope);
  // Keep a complex when any eligible unit meets the dashboard's
  // filters, even when the cheapest representative has a different date/rank.
  if (trustedView && !exactUnit && (m.units || []).length) {
    return m.units.some(u => passes({ ...u, group_name: m.group_name }, true, true));
  }
  const q = document.getElementById('fSearch').value.toLowerCase().trim();

  if (trustedView) {
    const t = document.getElementById('fTier').value;
    const band = document.getElementById('fBand').value;
    const budget = document.getElementById('fBudget').value;
    const rel = document.getElementById('fRel').value;
    const sl = document.getElementById('fShort').value;
    if (t && (m.display_tier || '') !== t) return false;
    if (band && (m.commute_band || m.tier || '') !== band) return false;
    if (budget === 'rp' && !m.is_responsible_pick) return false;
    if (ONLY_PENDING && !m.features_pending) return false;
    if (ONLY_EXPIRED) { if (!m.expired_recently) return false; }
    else if (m.expired_recently) return false;
    if (rel && (m.relevance || '') !== rel) return false;
    if (sl === 'hide_dismissed' && m.shortlist_state === 'dismiss') return false;
    if (['save', 'applied', 'dismiss'].includes(sl) && m.shortlist_state !== sl) return false;
  } else {
    // Section 7.2: the Evidence problem filter narrows the views that render
    // evidence - Needs verification and Excluded. Recently gone shows only its
    // last-seen line (section 7 item 3), so it carries no evidence facts to
    // narrow; Trusted blocks is evidence-positive by construction.
    const problem = document.getElementById('fProblem').value;
    if (problem && (ACTIVE_DATASET === 'review' || ACTIVE_DATASET === 'excluded')
        && !hasEvidenceProblem(m, problem)) return false;
  }
  if (q && !searchableText(m, trustedView).includes(q)) return false;
  return true;
}

const SORT_VALUE = {
  rank: m => m.score, score: m => m.score, bw_id: m => m.bw_id,
  address: m => m.address, rent: m => m.rent_total, m2: m => m.size_m2,
  kr_m2: m => m.kr_per_m2, value: m => m.value_ratio, commute: m => m.commute_min,
  street: m => m.major_street || m.buzz_detail, water: m => m.water_dist_m,
  available: m => m.available_from, relevance: m => m.relevance, shortlist: m => m.shortlist_state,
  band: m => m.category,
};
const DEFAULT_SORT_DIR = { rank: 'desc', score: 'desc', band: 'asc', commute: 'asc', value: 'asc', rent: 'asc' };
function sorted(rows) {
  const get = SORT_VALUE[SORT.key] || SORT_VALUE.rank;
  const dir = SORT.dir === 'asc' ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const av = get(a), bv = get(b);
    const an = av === null || av === undefined || av === '';
    const bn = bv === null || bv === undefined || bv === '';
    if (an || bn) return an === bn ? 0 : (an ? 1 : -1);
    const cmp = typeof av === 'string' || typeof bv === 'string'
      ? String(av).localeCompare(String(bv), 'da') : Number(av) - Number(bv);
    return cmp * dir || (a.rent_total ?? 1e9) - (b.rent_total ?? 1e9);
  });
}

function syncSortUi() {
  document.querySelectorAll('th[data-sort]').forEach(th => {
    const isSorted = th.dataset.sort === SORT.key;
    th.classList.toggle('sorted', isSorted);
    th.classList.toggle('desc', isSorted && SORT.dir === 'desc');
  });
  const select = document.getElementById('fSort');
  if ([...select.options].some(o => o.value === SORT.key)) {
    select.value = SORT.key;
  } else {
    let option = select.querySelector('option[value="column"]');
    if (!option) { option = new Option('Column', 'column'); option.disabled = true; select.add(option); }
    select.value = 'column';
  }
}
function sortBy(key) {
  SORT = { key, dir: SORT.key === key && SORT.dir === 'asc' ? 'desc' : (SORT.key === key ? 'asc' : (DEFAULT_SORT_DIR[key] || 'asc')) };
  render();
}
function sortFromSelect() {
  const key = document.getElementById('fSort').value;
  if (key !== 'column') SORT = { key, dir: DEFAULT_SORT_DIR[key] || 'asc' };
  render();
}

function updateLegendActiveState(container) {
  const root = container || document.querySelector('.legend');
  if (!root) return;
  const currentTier = document.getElementById('fTier').value;
  const currentBand = document.getElementById('fBand').value;
  root.querySelectorAll('.lg-row').forEach(row => {
    const kind = row.dataset.kind;
    const key = row.dataset.key;
    if ((kind === 'rank' && currentTier === key) || (kind === 'band' && currentBand === key)) {
      row.classList.add('on');
    } else {
      row.classList.remove('on');
    }
  });
}

function availabilityText(m) {
  const raw = m.available_from;
  if (!raw) return 'Availability unknown';
  if (/^\\d{4}-\\d{2}-\\d{2}$/.test(raw)) {
    const parsed = new Date(raw + 'T12:00:00');
    if (!Number.isNaN(parsed)) return parsed.toLocaleDateString('da-DK', { day: 'numeric', month: 'short', year: 'numeric' });
  }
  return esc(raw);
}
function relevanceText(m) { return m.relevance === 'actionable' ? 'Actionable' : (m.relevance === 'intel' ? 'Intel' : 'Unknown'); }
// Spec 09 sections 5.1-5.3: the personal priority is property-scoped local input.
// It is never a second score and never a claim about quality, so the badge is
// rendered only for the one kind v3.8 stores, and a property with no preference
// renders nothing at all (no "unpreferred" twin).
function preferredLocality(row) {
  return !!row && row.rank_priority_kind === PREFERRED_LOCALITY_KIND;
}

function preferredLocalityTitle(row) {
  // Spec 09 section 5.3: the accessible title states the narrow effect - the
  // order inside the row's own tier, which is the tier this row holds; it never
  // says the score, tier or eligibility moved.
  const tier = (row && row.display_tier) ? String(row.display_tier) : '';
  return tier
    ? 'Personally preferred locality; affects order within Tier ' + tier + ' only'
    : 'Personally preferred locality; affects order within the current tier only';
}
function softFlagsHtml(m) {
  const flags = m.soft_flags || [];
  if (!flags.length) return '';
  let out = '';
  for (const f of flags) {
    if (!f || !f.label) continue;
    const sev = (f.severity === 'check' || f.severity === 'over-limit') ? f.severity : 'info';
    const title = f.evidence ? esc(f.evidence) : esc(f.label);
    out += ' <span class="pill flag-' + sev + '" title="' + title + '">' + esc(f.label) + '</span>';
  }
  return out;
}
function badgeLine(m) {
  let out = '';
  // Spec 09 section 5.3: the ranked card shows the preference badge beside the
  // evidence badges, and its title states the within-tier ordering effect.
  if (preferredLocality(m)) out += ' <span class="pill preferred-locality" title="' + esc(preferredLocalityTitle(m)) + '">' + esc(PREFERRED_LOCALITY_LABEL) + '</span>';
  if (m.is_responsible_pick) out += ' <span class="pill rp" title="Responsible Pick under the current rules">Responsible Pick</span>';
  if (m.more_in_building) out += ' <span class="pill" title="More units live in this building">More in building · ' + kr(m.group_price_min) + '–' + kr(m.group_price_max) + ' kr</span>';
  if (m.group_name) out += ' <span class="pill">' + esc(m.group_name) + '</span>';
  if ((m.units || []).length > 1) out += ' <button class="secondary-button" onclick="browseUnits(\\'' + esc(m.bw_id) + '\\')">Browse ' + m.units.length + ' eligible adverts</button>';
  // A card whose detail page was never opened says so, with the reason. A blank
  // row would read as "no information available", which is not what happened.
  if (m.card_only) out += ' <span class="pill-cardonly" title="Discovery stored this card; the detail page was not opened in this scan">' + esc(m.card_only_reason || 'card only, detail not fetched') + '</span>';
  if (m.expired_recently) {
    const days = (m.last_seen_days_ago === null || m.last_seen_days_ago === undefined) ? null : m.last_seen_days_ago;
    out += ' <span class="pill-expired" title="Expired; kept visible for ' + __EXPIRED_VISIBLE_DAYS__ + ' days">' + (days === null ? 'expired' : 'last seen ' + days + (days === 1 ? ' day ago' : ' days ago')) + '</span>';
  }
  if (m.kitchen_state === 'assumed') out += ' <span class="pill" title="No kitchen wording on the page; assumed from an apartment with a verified room count">kitchen: assumed private (apartment)</span>';
  if (m.kitchen_state === 'verified' && m.shared_kitchen_present === 'true') out += ' <span class="pill" title="Private kitchen verified; the building also has a shared one">extra shared kitchen</span>';
  out += softFlagsHtml(m);
  return out;
}
function commuteHtml(m) {
  let out = '<b>' + esc(commuteText(m)) + ' to ' + esc(OFFICE_NAME) + '</b>';
  if (m.commute_band_name) out += '<div><span class="band ' + esc(m.commute_band || '') + '">' + esc(m.commute_band_name) + '</span></div>';
  return out;
}
function detailsHtml(m) {
  const score = (m.score_breakdown || {});
  const scoreBits = SCORE_PARTS.map(([key, max]) => key[0].toUpperCase() + key.slice(1) + ' ' + (Number(score[key]) || 0) + '/' + max).join(' · ');
  const journey = [commuteText(m), m.commute_route ? 'route available' : '', m.nearest_station ? 'Nearest: ' + m.nearest_station : ''].filter(Boolean).join(' · ') || 'Unknown';
  const location = [m.major_street || (m.buzz_detail ? m.buzz_detail.split(',')[0] : ''), m.water_dist_m !== null && m.water_dist_m !== undefined ? 'Water ' + Math.round(m.water_dist_m) + ' m' : ''].filter(Boolean).join(' · ') || 'Unknown';
  const cost = [kr(m.rent_total) + ' kr all-inclusive / month', m.size_m2 ? Math.round(m.size_m2) + ' m²' : '', m.kr_per_m2 ? kr(m.kr_per_m2) + ' kr/m²' : ''].filter(Boolean).join(' · ');
  const c = comparisonSnapshot(m), fit = comparisonDateFit(c);
  const windowDetail = c.major_street ? 'Building fronts or is near ' + c.major_street + '. This does not confirm this studio’s outlook.' : 'Building road context is unknown.';
  // Spec 09 section 5.3: the optional note is local user input shown in the
  // expanded details, and only where the row actually carries a preference.
  const preferredNote = (preferredLocality(m) && m.rank_priority_note)
    ? '<div class="detail-group preferred-locality-note"><b>' + esc(PREFERRED_LOCALITY_LABEL) + '</b><div>' + esc(m.rank_priority_note) + '</div><div>' + esc(preferredLocalityTitle(m)) + '</div></div>'
    : '';
  return '<div class="details-content"><div class="detail-group"><b>Cost</b><div>' + cost + '</div><div>' + vsMarket(m) + '</div></div>' +
    '<div class="detail-group"><b>Journey</b><div>' + esc(journey) + '</div></div>' +
    '<div class="detail-group"><b>Location</b><div>' + esc(location) + '</div></div>' +
    '<div class="detail-group"><b>Window view</b><div><strong>Unknown</strong> · ' + esc(windowDetail) + '</div><div><button class="secondary-button" onclick="copyQuestions(\\'' + esc(comparisonId(m)) + '\\')">Copy questions</button></div></div>' +
    '<div class="detail-group"><b>Dates & cash</b><div>' + esc(fit.label) + ' · ' + esc(c.move_in_cost === null || c.move_in_cost === undefined ? 'move-in cash unknown' : kr(c.move_in_cost) + ' kr before keys') + '</div><div>Initial charge and next bill need confirmation.</div></div>' +
    '<div class="detail-group"><b>Score</b><div>' + esc(scoreBits) + '</div><div>' + (m.features_pending ? 'Provisional: street data pending' : 'Value and availability are unweighted') + '</div></div>' +
    preferredNote + unitBrowserHtml(m) + '</div>';
}
function unitBrowserHtml(m) {
  const units = m.units || [];
  if (!units.length) return '';
  return '<div class="detail-group" style="grid-column:1/-1"><details class="unit-browser"' + (OPEN_UNITS.has(m.bw_id) ? ' open' : '') + ' ontoggle="rememberUnitBrowser(\\'' + esc(m.bw_id) + '\\', this.open)"><summary>Browse all ' + units.length + ' eligible adverts · ' + esc(m.group_name || m.address || m.bw_id) + '</summary>' +
    '<p>Each advert keeps its own facts. Equal prices and sizes do not prove duplicate units. Representative: ' + esc(m.bw_id) + ' · ' + esc(m.listing_id) + '. ' + Number(m.source_count || units.length) + ' live or recently missing sources in this group; other decisions remain in the verification and excluded views.</p>' +
    units.map(u => '<article class="listing-card" style="margin:8px 0"><b>' + esc(u.bw_id) + ' · ' + esc(u.site) + ' #' + esc(u.listing_id) + '</b><div>' + esc(u.address || 'Exact address unknown') + '</div><div>' + esc(u.title || '') + '</div>' +
      '<div><strong>' + kr(u.rent_total) + ' kr / month</strong> · ' + (u.size_m2 == null ? 'Size unknown' : esc(u.size_m2) + ' m²') + ' · ' + esc(u.rooms == null ? 'Rooms unknown' : u.rooms + ' rooms') + '</div>' +
      '<div>Available: ' + availabilityText(u) + ' · ' + esc(comparisonDateFit(comparisonSnapshot(u)).label) + '</div>' +
      '<div>Move-in: ' + (u.move_in_cost == null ? 'Unknown' : kr(u.move_in_cost) + ' kr') + ' · ' + esc(u.lifecycle_state) + ' · ' + esc(u.shortlist_state || 'No shortlist state') + '</div>' +
      '<div>Kitchen: ' + esc(u.kitchen_state || 'Unknown') + ' · Bathroom: ' + esc(u.bathroom_state || 'Unknown') + ' · Eligibility: ' + esc(u.eligibility_evidence_state || 'Unknown') + ' · checked ' + esc(u.eligibility_checked_at || 'Unknown') + '</div>' +
      sourceLink(u) + ' ' + comparisonButton(u) + '</article>').join('') + '</details></div>';
}
function comparisonHomes() {
  const homes = new Map();
  (DATA.trusted || []).forEach(m => { homes.set(comparisonId(m), m); (m.units || []).forEach(u => { if (!homes.has(comparisonId(u))) homes.set(comparisonId(u), u); }); });
  return Array.from(homes.values());
}
function comparisonName(m) {
  const name = m.address || m.bw_id || 'Address unknown';
  return m.listing_id ? name + ' · ' + (m.site || 'source') + ' #' + m.listing_id : name;
}
function comparisonId(m) { return (m.comparison || {}).subject_id || ('property:' + m.property_id); }
function comparisonFor(id) { return comparisonHomes().find(m => comparisonId(m) === id); }
function comparisonSnapshot(m) { return m.comparison || {}; }
function comparisonDate(value) {
  if (typeof value !== 'string' || !/^\\d{4}-\\d{2}-\\d{2}$/.test(value)) return null;
  const date = new Date(value + 'T12:00:00');
  return Number.isNaN(date) || date.toISOString().slice(0, 10) !== value ? null : date;
}
function comparisonDateFit(c) {
  const date = comparisonDate(c.available_from_parsed || c.available_from);
  if (!date) return { label: 'Date unknown', known: false, detail: 'Ask for the offered tenancy start.' };
  const start = comparisonDate(PREFERRED_WINDOW_START), target = comparisonDate(TARGET_MOVE_DATE);
  if (!start || !target) return { label: 'Date unknown', known: false, detail: 'Ask for the offered tenancy start.' };
  if (date < start) return { label: 'Earlier than the preferred window', known: true, detail: 'Ask whether paid start can be ' + PREFERRED_WINDOW_START + ' or later.' };
  if (date > target) return { label: 'Later than target', known: true, detail: 'Arrival after ' + TARGET_MOVE_DATE + ' needs confirmation.' };
  return { label: 'Listed within the target window', known: true, detail: 'Confirm the actual tenancy start.' };
}
function comparisonQuestions(m) {
  const c = comparisonSnapshot(m), questions = [];
  const road = c.major_street ? 'the ' + c.major_street + ' or the courtyard' : 'the road or the courtyard';
  questions.push('Does this exact studio’s main window face ' + road + '?');
  if (!c.photo_url) questions.push('Can you share a photo from that window or confirm its floor and orientation?');
  const fit = comparisonDateFit(c);
  if (!fit.known || fit.label !== 'Listed within the target window') questions.push('Can the tenancy start on ' + PREFERRED_WINDOW_START + ', and what date does the first rent charge cover?');
  if (c.move_in_cost === null || c.move_in_cost === undefined) questions.push('What cash is due before keys, including deposit, prepaid rent, first rent and any one-time fees?');
  else questions.push('Can you confirm the move-in payment breakdown, covered period and next regular due date?');
  return questions.slice(0, 3);
}
function comparisonCell(m, key) {
  const c = comparisonSnapshot(m);
  const amount = (v, label) => v === null || v === undefined ? '' : esc(label + ' ' + kr(v) + ' kr');
  if (key === 'home') {
    const observed = c.snapshot_at && !Number.isNaN(new Date(c.snapshot_at)) ? new Date(c.snapshot_at).toLocaleDateString('da-DK', { dateStyle: 'medium' }) : 'Unknown date';
    return { known: true, value: [m.address, m.listing_id, c.exact_unit || 'Exact unit not named', c.source || 'Unknown source', observed].join('|'), html:
      '<strong>' + esc(m.address || 'Address unknown') + '</strong>' +
      '<span class="compare-note">' + esc(c.exact_unit || 'Exact unit not named') + ' · ' + esc(c.source || 'Unknown source') + (m.listing_id ? ' #' + esc(m.listing_id) : '') + ' · observed ' + esc(observed) + '</span>' +
      (c.source_url ? '<a href="' + esc(c.source_url) + '" target="_blank" rel="noopener">Open exact listing</a>' : '<span class="compare-note compare-warning">Source link unavailable</span>') };
  }
  if (key === 'window') {
    const street = c.major_street ? 'Building fronts or is near ' + c.major_street + '.' : 'Building road context is unknown.';
    return { known: false, value: 'unknown', html: '<strong>Window view unknown</strong><div>' + esc(street) + ' This does not confirm this studio’s outlook.</div><span class="compare-note">Ask whether the main window faces the road or courtyard.</span>' };
  }
  if (key === 'cost') {
    const bits = [amount(c.rent_base, 'Base rent'), amount(c.utilities_amount, 'Utilities'), amount(c.mandatory_monthly_fees, 'Mandatory monthly fees')].filter(Boolean);
    const upfrontParts = [amount(c.deposit, 'Deposit'), amount(c.prepaid_rent, 'Prepaid rent'), amount(c.first_month_charge, 'Initial rent'), amount(c.mandatory_onetime_fees, 'One-time fees')].filter(Boolean);
    const upfrontKnown = [c.deposit, c.prepaid_rent, c.first_month_charge, c.mandatory_onetime_fees].filter(v => v !== null && v !== undefined);
    const cash = c.move_in_cost === null || c.move_in_cost === undefined
      ? (upfrontParts.length ? '<div>Known itemised subtotal: <b>' + kr(upfrontKnown.reduce((total, value) => total + Number(value), 0)) + ' kr</b></div><span class="compare-warning">Other move-in items may be unknown.</span><span class="compare-note">' + upfrontParts.join(' · ') + '</span>' : '<span class="compare-warning">Move-in cash unknown</span>')
      : '<div>Before keys: <b>' + kr(c.move_in_cost) + ' kr</b></div><span class="compare-note">' + (c.move_in_cost_verified ? 'Quoted aggregate; check its breakdown.' : 'Recorded amount; re-confirm terms and breakdown.') + '</span>' + (upfrontParts.length ? '<span class="compare-note">' + upfrontParts.join(' · ') + '</span>' : '');
    return { known: c.rent_total !== null && c.rent_total !== undefined && c.move_in_cost !== null && c.move_in_cost !== undefined,
      value: [c.rent_total, c.move_in_cost, c.rent_base, c.utilities_amount, c.mandatory_monthly_fees].join('|'), html:
      '<strong>' + (c.rent_total === null || c.rent_total === undefined ? 'Monthly cost unknown' : kr(c.rent_total) + ' kr all-inclusive / month') + '</strong>' +
      (bits.length ? '<span class="compare-note">' + bits.join(' · ') + '</span>' : '<span class="compare-note">Monthly breakdown unavailable</span>') + cash };
  }
  if (key === 'journey') {
    const known = m.commute_min !== null && m.commute_min !== undefined;
    return { known: known, value: known ? [m.commute_min, m.nearest_station, m.commute_band_name].join('|') : 'unknown', html:
      '<strong>' + esc(known ? commuteText(m) + ' to ' + OFFICE_NAME : 'Commute unknown') + '</strong><div>' + esc(m.nearest_station ? 'Nearest transit: ' + m.nearest_station : 'Transit station unknown') + '</div><span class="compare-note">Modelled journey' + (m.commute_band_name ? ' · ' + esc(m.commute_band_name) : '') + '</span>' };
  }
  if (key === 'space') {
    const hasSize = m.size_m2 !== null && m.size_m2 !== undefined;
    const hasRooms = c.rooms !== null && c.rooms !== undefined;
    return { known: hasSize && hasRooms, value: [m.size_m2, c.rooms].join('|'), html:
      '<strong>' + (hasSize ? Math.round(m.size_m2) + ' m² listed' : 'Size unknown') + '</strong><div>' + (hasRooms ? esc(c.rooms + ' room' + (Number(c.rooms) === 1 ? '' : 's')) : 'Rooms unknown') + '</div><span class="compare-note">No layout or furniture fit is inferred.</span>' };
  }
  if (key === 'dates') {
    const fit = comparisonDateFit(c), listed = comparisonDate(c.available_from_parsed || c.available_from);
    const initial = c.first_month_charge === null || c.first_month_charge === undefined ? 'Initial rent charge unknown' : 'Initial rent charge: ' + kr(c.first_month_charge) + ' kr';
    return { known: fit.known && c.first_month_charge !== null && c.first_month_charge !== undefined, value: [c.available_from_parsed || c.available_from, c.first_month_charge].join('|'), html:
      '<strong>' + esc(fit.label) + '</strong><div>' + esc(listed ? 'Advertised: ' + listed.toLocaleDateString('da-DK', { dateStyle: 'medium' }) : 'Advertised availability unavailable') + '</div><span class="compare-note">' + esc(fit.detail) + '</span><div class="compare-note">' + esc(initial) + ' · covered period and next regular bill need confirmation.</div><details><summary>January / February timeline</summary><div class="compare-note">Payment due: unknown → tenancy start: ' + esc(listed ? listed.toLocaleDateString('da-DK', { dateStyle: 'medium' }) : 'unknown') + ' → physical move: 18 Jan–1 Feb → next regular payment: unknown.</div><div class="compare-note">Old home: February rent covered by existing prepaid rent (reported); other closing charges unknown.</div></details>' };
  }
  if (key === 'assessment') {
    const known = m.score !== null && m.score !== undefined;
    return { known: known, value: [m.display_tier, m.score, m.is_responsible_pick].join('|'), html:
      '<strong>' + esc(known ? (m.display_tier || '?') + ' · ' + Math.round(m.score) + '/100' : 'Score unknown') + '</strong><div>' + (m.is_responsible_pick ? '<span class="compare-good">Responsible Pick</span>' : 'Not a Responsible Pick') + '</div><span class="compare-note">Ranking is unchanged by this desk.</span>' };
  }
  const questions = comparisonQuestions(m);
  return { known: false, value: 'questions:' + questions.join('|'), html:
    '<strong>Clarify before deciding</strong><ol class="question-list">' + questions.map(q => '<li>' + esc(q) + '</li>').join('') + '</ol>' +
    '<button class="secondary-button" onclick="copyQuestions(\\'' + esc(comparisonId(m)) + '\\')">Copy questions</button>' };
}
function tradeoffCell(m, reference) {
  if (!reference || comparisonId(m) === comparisonId(reference)) return { known: true, value: 'reference', html: '<strong>Reference home</strong><span class="compare-note">Other columns are compared with this exact listing.</span>' };
  const currentCost = comparisonSnapshot(m).rent_total, referenceCost = comparisonSnapshot(reference).rent_total;
  const parts = [];
  if (currentCost !== null && currentCost !== undefined && referenceCost !== null && referenceCost !== undefined) {
    const diff = Number(currentCost) - Number(referenceCost);
    parts.push(diff === 0 ? 'has the same known monthly cost' : 'costs ' + kr(Math.abs(diff)) + ' kr ' + (diff > 0 ? 'more' : 'less') + ' per month');
  }
  if (m.commute_min !== null && m.commute_min !== undefined && reference.commute_min !== null && reference.commute_min !== undefined) {
    const saved = Number(reference.commute_min) - Number(m.commute_min);
    parts.push(saved === 0 ? 'has the same known commute' : (saved > 0 ? 'saves ' + Math.abs(saved) + ' minutes each way' : 'takes ' + Math.abs(saved) + ' more minutes each way'));
  }
  if (m.size_m2 !== null && m.size_m2 !== undefined && reference.size_m2 !== null && reference.size_m2 !== undefined) {
    const diff = Number(m.size_m2) - Number(reference.size_m2);
    parts.push(diff === 0 ? 'has the same listed space' : 'has ' + Math.abs(diff) + ' m² ' + (diff > 0 ? 'more' : 'less') + ' listed space');
  }
  return { known: parts.length === 3, value: parts.join('|') || 'unknown', html: '<strong>Compared with reference</strong><div>' + esc(parts.length ? 'This home ' + parts.join(', ') + '.' : 'Known rent, commute and space are needed for a factual trade-off.') + '</div><span class="compare-note">At unchanged rent; one-time costs excluded.</span>' };
}
function sameKnown(cells) { return cells.length > 1 && cells.every(c => c.known) && cells.every(c => c.value === cells[0].value); }
function comparisonSelected() { return Array.from(COMPARE).map(comparisonFor).filter(Boolean); }
function persistComparison() {
  const selected = comparisonSelected(), datasetId = selected[0] && comparisonSnapshot(selected[0]).dataset_id;
  try { localStorage.setItem(COMPARE_STORAGE_KEY, JSON.stringify({ dataset_id: datasetId || '', subjects: Array.from(COMPARE) })); } catch (e) {}
}
function restoreComparison() {
  try {
    const saved = JSON.parse(localStorage.getItem(COMPARE_STORAGE_KEY) || '{}');
    const datasetId = (DATA.trusted[0] && comparisonSnapshot(DATA.trusted[0]).dataset_id) || '';
    if (saved.dataset_id !== datasetId || !Array.isArray(saved.subjects)) return;
    saved.subjects.slice(0, 4).forEach(id => { if (comparisonFor(id)) COMPARE.add(id); });
  } catch (e) {}
}
function toggleCompare(bw) {
  const m = comparisonFor(bw) || DATA.trusted.find(row => row.bw_id === bw); if (!m) return;
  const id = comparisonId(m);
  if (COMPARE.has(id)) COMPARE.delete(id);
  else if (COMPARE.size >= 4) return sayCount('Compare holds up to four homes', 2500);
  else COMPARE.add(id);
  if (COMPARE.size < 2) COMPARISON_OPEN = false;
  if (!MOBILE_COMPARE_SUBJECT || !COMPARE.has(MOBILE_COMPARE_SUBJECT)) MOBILE_COMPARE_SUBJECT = Array.from(COMPARE)[0] || '';
  persistComparison(); render();
}
function compareCandidates() {
  const richs = comparisonHomes().find(m => m.bw_id === 'BW-0000108');
  const joris = DATA.trusted.find(m => m.complex_key === 'jorisvej-complex');
  if (!richs || !joris) return;
  COMPARE.clear(); COMPARE.add(comparisonId(joris)); COMPARE.add(comparisonId(richs));
  COMPARISON_OPEN = true; REFERENCE_SUBJECT = comparisonId(richs);
  MOBILE_COMPARE_SUBJECT = comparisonId(richs); persistComparison(); render();
  document.getElementById('comparisonDesk').scrollIntoView({ block: 'start', behavior: 'smooth' });
}
function clearComparison() { COMPARE.clear(); COMPARISON_OPEN = false; MOBILE_COMPARE_SUBJECT = ''; persistComparison(); render(); }
function toggleComparisonDesk() { if (COMPARE.size < 2) return sayCount('Choose one more home to compare', 2200); COMPARISON_OPEN = !COMPARISON_OPEN; render(); if (COMPARISON_OPEN) document.getElementById('comparisonDesk').scrollIntoView({ block: 'start', behavior: 'smooth' }); }
function setMobileComparison(id) { MOBILE_COMPARE_SUBJECT = id; paintComparisonDesk(); }
function setComparisonReference(id) { REFERENCE_SUBJECT = id; paintComparisonDesk(); }
function copyQuestions(id) {
  const m = comparisonFor(id); if (!m) return;
  const text = comparisonQuestions(m).map((q, i) => (i + 1) + '. ' + q).join('\\n');
  const done = () => sayCount('Questions copied', 1800);
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done).catch(() => window.prompt('Copy these questions', text));
  else window.prompt('Copy these questions', text);
}
function comparisonButton(m) {
  const picked = COMPARE.has(comparisonId(m));
  return '<button class="secondary-button compare-control' + (picked ? ' on' : '') + '" aria-pressed="' + picked + '" onclick="toggleCompare(\\'' + esc(comparisonId(m)) + '\\')">' + (picked ? 'In compare' : 'Compare') + '</button>';
}
function paintCompareTray() {
  const tray = document.getElementById('compareTray'), selected = comparisonSelected();
  // The comparison desk compares trusted homes; the source-level views hide it.
  if (ACTIVE_DATASET !== 'trusted' || !selected.length) { tray.setAttribute('hidden', ''); return; }
  tray.removeAttribute('hidden');
  tray.innerHTML = '<b>Compare ' + selected.length + '/4</b>' + selected.map(m => '<span class="pill">' + esc(comparisonName(m)) + ' <button class="compare-remove" aria-label="Remove ' + esc(comparisonName(m)) + '" onclick="toggleCompare(\\'' + esc(comparisonId(m)) + '\\')">Remove</button></span>').join('') +
    '<button class="secondary-button" onclick="toggleComparisonDesk()"' + (selected.length < 2 ? ' disabled' : '') + '>' + (COMPARISON_OPEN ? 'Hide comparison' : 'Compare selected') + '</button><button class="secondary-button" onclick="clearComparison()">Clear</button>';
}
function paintComparisonDesk() {
  const desk = document.getElementById('comparisonDesk'), selected = comparisonSelected();
  if (ACTIVE_DATASET !== 'trusted' || !COMPARISON_OPEN || selected.length < 2) { desk.setAttribute('hidden', ''); return; }
  desk.removeAttribute('hidden');
  if (!MOBILE_COMPARE_SUBJECT || !COMPARE.has(MOBILE_COMPARE_SUBJECT)) MOBILE_COMPARE_SUBJECT = comparisonId(selected[0]);
  if (!REFERENCE_SUBJECT || !COMPARE.has(REFERENCE_SUBJECT)) REFERENCE_SUBJECT = comparisonId(selected[0]);
  const reference = comparisonFor(REFERENCE_SUBJECT);
  const sections = [['home', 'Home'], ['tradeoff', 'Trade-off'], ['window', 'Window & locality'], ['cost', 'Cost'], ['journey', 'Journey'], ['space', 'Space'], ['dates', 'Dates & cash'], ['assessment', 'Assessment'], ['next', 'Next step']];
  const referencePicker = '<div class="reference-picker"><div class="reference-label" id="referenceLabel">Reference</div><div class="reference-segments" role="group" aria-labelledby="referenceLabel">' +
    selected.map(m => '<button class="secondary-button" aria-pressed="' + (comparisonId(m) === REFERENCE_SUBJECT) + '" onclick="setComparisonReference(\\'' + esc(comparisonId(m)) + '\\')">' + esc(comparisonName(m)) + '</button>').join('') + '</div></div>';
  let grid = '<div class="compare-columns" style="--compare-count:' + selected.length + '">';
  sections.forEach(([key, label]) => {
    const cells = selected.map(m => key === 'tradeoff' ? tradeoffCell(m, reference) : comparisonCell(m, key));
    if (DIFFERENCES_ONLY && sameKnown(cells)) return;
    grid += '<div class="compare-label">' + esc(label) + '</div><div class="compare-section-title">' + esc(label) + '</div>';
    cells.forEach((cell, index) => grid += '<div class="compare-value' + (comparisonId(selected[index]) === REFERENCE_SUBJECT ? ' reference' : '') + (comparisonId(selected[index]) === MOBILE_COMPARE_SUBJECT ? ' mobile-current' : '') + (key === 'home' ? ' compare-home' : '') + '">' + (comparisonId(selected[index]) === REFERENCE_SUBJECT ? '<span class="reference-badge">Reference home</span>' : '') + cell.html + '</div>');
  });
  grid += '</div>';
  const picker = '<label class="compare-mobile-picker" for="mobileCompare">Viewing</label><select id="mobileCompare" class="compare-mobile-picker" onchange="setMobileComparison(this.value)">' + selected.map(m => '<option value="' + esc(comparisonId(m)) + '"' + (comparisonId(m) === MOBILE_COMPARE_SUBJECT ? ' selected' : '') + '>' + esc(comparisonName(m)) + '</option>').join('') + '</select>';
  desk.innerHTML = '<div class="desk-head"><div><h2 id="comparisonHeading">Decision desk</h2><p>Exact-listing facts only. Unknown outlook, payment coverage and dates stay unresolved.</p></div><div class="desk-tools">' + referencePicker + '<label><input type="checkbox"' + (DIFFERENCES_ONLY ? ' checked' : '') + ' onchange="DIFFERENCES_ONLY=this.checked;paintComparisonDesk()"> Differences only</label><button class="secondary-button" onclick="toggleComparisonDesk()">Close</button></div></div>' + picker + grid;
}
function detailButton(m, card) {
  const open = OPEN_DETAILS.has(m.bw_id);
  const target = (card ? 'card-detail-' : 'detail-') + esc(m.bw_id);
  return '<button class="details-button" onclick="toggleTableDetails(\\'' + esc(m.bw_id) + '\\')" aria-controls="' + target + '" aria-expanded="' + open + '">' + (open ? 'Hide details' : 'Details') + '</button>';
}
function tableRow(m) {
  const rankBlurb = (LEGEND.ranks.find(r => r.letter === m.display_tier) || {}).blurb || '';
  const detailOpen = OPEN_DETAILS.has(m.bw_id);
  return '<tr data-bw="' + esc(m.bw_id) + '" data-pid="' + esc(m.property_id) + '" class="' + (m.shortlist_state === 'dismiss' ? 'dismissed' : '') + '">' +
    '<td><span class="tier ' + esc(m.display_tier) + '" title="' + esc(rankBlurb) + '">' + esc(m.display_tier) + '</span></td>' +
    '<td class="home"><div class="home-address">' + esc(m.address) + '</div><div class="home-meta">' + esc(m.postcode || '') + ' · <a href="#' + esc(m.bw_id) + '" onclick="focusOnMap(\\'' + esc(m.bw_id) + '\\'); return false;">' + esc(m.bw_id) + '</a></div>' + badgeLine(m) + '</td>' +
    '<td><div class="cost-amount">' + kr(m.rent_total) + ' kr</div><div class="cell-meta">All-inclusive / month' + (m.size_m2 ? ' · ' + Math.round(m.size_m2) + ' m²' : '') + '</div></td>' +
    '<td>' + commuteHtml(m) + '</td><td><div>' + availabilityText(m) + '</div><div class="cell-meta">' + relevanceText(m) + '</div></td>' +
    '<td>' + (m.url ? '<a href="' + esc(m.url) + '" target="_blank" rel="noopener">Open listing</a><br>' : '') + comparisonButton(m) + '<br>' + shortlistCell(m) + '<br>' + detailButton(m, false) + '</td>' +
    '<td class="table-extra">' + scoreCell(m) + '</td><td class="table-extra"><a href="#' + esc(m.bw_id) + '" onclick="focusOnMap(\\'' + esc(m.bw_id) + '\\'); return false;">' + esc(m.bw_id) + '</a></td><td class="table-extra numeric">' + (m.size_m2 ? Math.round(m.size_m2) : 'Unknown') + '</td><td class="table-extra numeric">' + (m.kr_per_m2 ? kr(m.kr_per_m2) : 'Unknown') + '</td><td class="table-extra">' + vsMarket(m) + '</td><td class="table-extra">' + esc(m.major_street || 'Unknown') + '</td><td class="table-extra">' + (m.water_dist_m !== null && m.water_dist_m !== undefined ? Math.round(m.water_dist_m) + ' m' : 'Unknown') + '</td><td class="table-extra">' + relevanceText(m) + '</td></tr>' +
    '<tr id="detail-' + esc(m.bw_id) + '" class="detail-row"' + (detailOpen ? '' : ' hidden') + '><td colspan="14">' + detailsHtml(m) + '</td></tr>';
}
function cardRow(m) {
  const open = OPEN_DETAILS.has(m.bw_id);
  return '<article class="listing-card' + (m.shortlist_state === 'dismiss' ? ' dismissed' : '') + '" data-bw="' + esc(m.bw_id) + '"><div class="card-top"><div class="card-rank"><span class="tier ' + esc(m.display_tier) + '">' + esc(m.display_tier) + '</span><b>' + Math.round(m.score || 0) + '/100</b></div><span class="pill">' + relevanceText(m) + '</span></div>' +
    '<div class="card-address">' + esc(m.address) + '</div><div class="card-meta">' + esc(m.postcode || '') + ' · ' + esc(m.bw_id) + '</div>' + badgeLine(m) +
    '<div class="card-rent">' + kr(m.rent_total) + ' kr</div><div class="card-meta">All-inclusive / month' + (m.size_m2 ? ' · ' + Math.round(m.size_m2) + ' m²' : '') + '</div>' +
    '<div class="card-journey">' + commuteHtml(m) + '<div class="card-meta" style="margin-top:4px">' + availabilityText(m) + '</div></div>' +
    '<div class="card-actions">' + comparisonButton(m) + detailButton(m, true) + '<a href="#' + esc(m.bw_id) + '" onclick="focusOnMap(\\'' + esc(m.bw_id) + '\\'); return false;">Show on map</a>' + (m.url ? '<a href="' + esc(m.url) + '" target="_blank" rel="noopener">Open listing</a>' : '') + '</div>' +
    '<div id="card-detail-' + esc(m.bw_id) + '"' + (open ? '' : ' hidden') + '>' + detailsHtml(m) + '</div></article>';
}
function filterChip(label, clear) { return '<button class="filter-chip" onclick="' + clear + '">' + esc(label) + '<span aria-hidden="true">×</span></button>'; }
function paintActiveFilters(count) {
  const chips = [];
  const labels = { fTier: 'Rank ', fBand: 'Commute ', fBudget: 'Pick ', fRel: 'Relevance ', fShort: 'Shortlist ' };
  // The rank, band, pick, relevance and shortlist controls filter the trusted
  // dataset only; a source-level view shows just its search.
  if (ACTIVE_DATASET === 'trusted') {
    ['fTier', 'fBand', 'fBudget', 'fRel', 'fShort'].forEach(id => {
      const el = document.getElementById(id); if (!el || !el.value || (id === 'fShort' && el.value === 'hide_dismissed')) return;
      chips.push(filterChip(labels[id] + el.options[el.selectedIndex].text, "clearFilter('" + id + "')"));
    });
    if (ONLY_PENDING) chips.push(filterChip('Street data pending', 'togglePending()'));
    if (ONLY_EXPIRED) chips.push(filterChip('Expired recently', 'toggleExpired()'));
  } else {
    // Section 7.2's Evidence problem filter applies to the two source-level views
    // that render the evidence facts, and says so where the other filters do.
    const problem = document.getElementById('fProblem');
    if (problem.value && (ACTIVE_DATASET === 'review' || ACTIVE_DATASET === 'excluded')) {
      chips.push(filterChip('Evidence: ' + problem.options[problem.selectedIndex].text, "clearFilter('fProblem')"));
    }
  }
  const q = document.getElementById('fSearch').value.trim(); if (q) chips.push(filterChip('Search: ' + q, "clearFilter('fSearch')"));
  chips.push('<span class="shown-count">' + count + ' shown</span>');
  document.getElementById('activeFilters').innerHTML = chips.join('');
}

// ============================================================================
// Spec 07 section 7 - the four dataset views
// ============================================================================
// One control per dataset switches the list between Trusted blocks and the three
// source-level views. The controls, their containers and the root carry the
// pinned hooks (`data-view`, `data-count-key`, `data-scope`, `data-default-view`
// on the root, `data-collapsed` on Excluded), so the static DOM and the live
// page read the same attributes.
function datasetRows(name) {
  if (!DATA) return [];
  if (name === 'review') return DATA.review || [];
  if (name === 'gone') return DATA.gone || [];
  if (name === 'excluded') return DATA.excluded || [];
  return DATA.trusted || [];
}

function lastSeenText(row) {
  // Section 7 item 3: a recently gone row says how long ago the source was last
  // seen and nothing about a rental date it cannot prove.
  const days = row.last_seen_days_ago;
  if (days === null || days === undefined) return 'last seen unknown';
  return 'last seen ' + days + (Number(days) === 1 ? ' day ago' : ' days ago');
}

function evidenceDateText(row) {
  const value = row.building_profile_verified_at || row.observed_at || row.last_seen_at;
  if (!value) return 'unknown';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString('da-DK', { dateStyle: 'medium' });
}

const EVIDENCE_SOURCE_WORDS = { structured: 'structured portal data', detail: 'detail-page text', manual: 'reviewed building profile' };

function provenanceText(row) {
  // Section 7.1: where the reason came from - structured portal data,
  // detail-page text, a reviewed building profile, or a missing fact.
  const sources = [row.unit_type_evidence_source, row.landlord_evidence_source];
  for (let i = 0; i < sources.length; i++) {
    const word = EVIDENCE_SOURCE_WORDS[sources[i]];
    if (word) return word;
  }
  if (row.building_profile_id) return 'reviewed building profile';
  return 'missing fact';
}

function safeExcerpt(row) {
  const text = row.unit_type_evidence_excerpt || row.landlord_evidence_excerpt;
  return text ? String(text).slice(0, 200) : '';
}

function sourceLink(row, extraClass) {
  const cls = 'source-link' + (extraClass ? ' ' + extraClass : '');
  return row.url
    ? '<a class="' + cls + '" href="' + esc(row.url) + '" target="_blank" rel="noopener">Open exact listing</a>'
    : '<span class="' + cls + ' source-warning">Source link unavailable</span>';
}

function whyNotRanked(row, view) {
  // Section 7.1: one primary reason, then the remaining reasons in the payload's
  // own 7.1 order. That order is v36q1's (`reasons`/`reason_ranks`) and is never
  // re-sorted here. A retained score is labelled previous/provisional and no
  // rank badge is rendered in these views.
  const reasons = (Array.isArray(row.reasons) && row.reasons.length) ? row.reasons : (row.primary_reason ? [row.primary_reason] : []);
  const ranks = Array.isArray(row.reason_ranks) ? row.reason_ranks : [];
  const items = reasons.map((text, index) => '<li class="' + (index === 0 ? 'reason-primary' : 'reason-secondary') + '" data-rank="' + esc(ranks[index] === undefined ? '' : ranks[index]) + '">' + esc(text) + '</li>');
  const excerpt = safeExcerpt(row);
  const score = (row.provisional_score === null || row.provisional_score === undefined)
    ? '' : '<div class="reason-score">Previous score ' + Math.round(row.provisional_score) + '/100 · provisional</div>';
  return '<details class="why-not-ranked" data-view="' + esc(view) + '"><summary>Why this is not ranked</summary>' +
    '<ol class="reason-list">' + (items.join('') || '<li class="reason-primary">reason not recorded on the source</li>') + '</ol>' +
    '<div class="reason-meta">Evidence: ' + esc(provenanceText(row)) + '</div>' +
    (excerpt ? '<div class="reason-meta reason-excerpt">' + esc(excerpt) + '</div>' : '') +
    '<div class="reason-meta reason-date">Evidence date: ' + esc(evidenceDateText(row)) + '</div>' +
    score +
    '<div class="reason-link">' + sourceLink(row, 'reason-source-link') + '</div></details>';
}

// ============================================================================
// Spec 08 sections 7 and 7.2 (and 7.3) - evidence badges, freshness, problems
// ============================================================================
// Spec 08 section 7: a management or building badge is shown with positive
// evidence only, and an absent badge is the whole statement - the unverified
// twins and every place that rendered them are deleted. Section 7.2 adds the
// badge's evidence date, the `verification stale` suffix on a stale block badge
// and the compact Evidence problem filter. Every predicate reads the read
// model's own fields - the row's tenancy and building verdicts (section 4.4),
// its section 7.1 reasons and its property identity state - never a re-derived
// guess.
function managementVerified(row) {
  return !!row && row.tenancy_channel_state === TENANCY_PROFESSIONAL_STATE;
}

function buildingVerified(row) {
  return !!row && row.building_form_state === BUILDING_MANAGED_STATE;
}

function evidenceVerifiedAt(row) {
  // Section 7.2: when the landlord/building evidence was last verified. A
  // reviewed building profile carries the date (section 5.2 `verified_at`); the
  // landlord triple carries none.
  return (row && row.building_profile_verified_at) || null;
}

function verifiedDateText(row) {
  const value = evidenceVerifiedAt(row);
  if (!value) return 'unknown';
  const date = new Date(value);
  return isNaN(date.getTime()) ? String(value) : date.toLocaleDateString('da-DK', { dateStyle: 'medium' });
}

function evidenceAgeDays(row) {
  const value = evidenceVerifiedAt(row);
  if (!value || isNaN(new Date(value).getTime())) return null;
  return Math.floor((Date.now() - new Date(value).getTime()) / 86400000);
}

function evidenceStale(row) {
  // Section 7.2: professional-management or building evidence older than the
  // threshold is `verification stale` and stays in Needs verification.
  const days = evidenceAgeDays(row);
  return days !== null && days > EVIDENCE_MAX_DAYS;
}

function badgeTitle(row, kind) {
  // Spec 08 section 7's badge provenance: the row's own evidence excerpt, plus
  // the date a reviewed profile supplied. The read model projects the landlord
  // pair only, so a block badge's title carries its verified date (and the stale
  // state, section 7.2); the portal footer is never a source.
  const parts = [];
  if (kind === 'management') {
    const excerpt = row.landlord_evidence_excerpt || row.landlord_name
      || row.landlord_evidence_source;
    if (excerpt) parts.push(String(excerpt));
  } else {
    const date = verifiedDateText(row);
    if (date !== 'unknown') parts.push('Evidence verified: ' + date);
  }
  if (evidenceStale(row)) parts.push(STALE_REASON);
  return parts.join(' \\u00b7 ');
}

function evidenceBadge(label, className, title) {
  return '<li class="' + className + ' source-verified" title="' + esc(title) + '">'
    + esc(label) + '</li>';
}

function evidenceBadges(row) {
  // Spec 08 section 7: one badge per positive verdict - `Professionally managed`
  // for a professional_direct tenancy, `Multi-unit block` for a managed block -
  // and nothing at all otherwise. Section 7.2 renders a block badge whose
  // profile evidence is older than the threshold with the stale suffix.
  const badges = [];
  if (managementVerified(row)) {
    badges.push(evidenceBadge(MANAGEMENT_VERIFIED_LABEL, 'source-management',
                              badgeTitle(row, 'management')));
  }
  if (buildingVerified(row)) {
    const label = evidenceStale(row)
      ? BUILDING_VERIFIED_LABEL + ' (' + STALE_REASON + ')' : BUILDING_VERIFIED_LABEL;
    badges.push(evidenceBadge(label, 'source-building', badgeTitle(row, 'building')));
  }
  return badges.join('');
}

function hasEvidenceProblem(row, problem) {
  // Spec 08 section 7's compact filter, one arm per problem: `kitchen`, `lease`,
  // `identity` and `housing`. `identity` reads the row's own identity state.
  // `kitchen` and `lease` read the row's own section 7.1 reasons at rank 5 -
  // "private kitchen, bathroom or unlimited lease unverified" - and only on a
  // Needs verification row: a hard rejection states a fact that IS known (a
  // shared kitchen, a time-limited lease), so it is not an unknown one. `housing`
  // names the largest remaining hold - a card-only row whose housing-eligibility
  // text could not be read - and matches the wording such a row already carries.
  if (!row) return false;
  if (problem === 'identity') return row.property_identity_state !== 'resolved';
  if (problem === 'kitchen' || problem === 'lease') {
    if (row.view !== 'needs_verification') return false;
    const reasons = Array.isArray(row.reasons) ? row.reasons : [];
    const ranks = Array.isArray(row.reason_ranks) ? row.reason_ranks : [];
    const word = problem === 'kitchen' ? 'kitchen' : 'lease';
    return reasons.some((text, index) => Number(ranks[index]) === EVIDENCE_PROBLEM_RANK
      && String(text).toLowerCase().indexOf(word) >= 0);
  }
  if (problem === 'housing') {
    const reasons = Array.isArray(row.reasons) ? row.reasons : [];
    return reasons.some(text => String(text).toLowerCase()
      .indexOf('housing eligibility') >= 0);
  }
  return false;
}

function paintIntegrity() {
  // Section 7: the local operator's integrity warning. It is the loud state and
  // the only one: with no violation the element stays empty rather than
  // reporting a clean status, so a clean reading cannot appear beside a real
  // violation (section 7.3 suppresses the migration banner at the same time).
  const box = document.getElementById('integrity');
  if (!box) return;
  const state = (DATA && DATA.integrity) || {};
  const warning = Boolean(state.warning);
  box.dataset.integrityWarning = String(warning);
  if (!warning) { box.hidden = true; box.innerHTML = ''; return; }
  const nullLinked = Number(state.null_linked_current_matches || 0);
  const forbidden = Number(state.forbidden_trust_in_matches || 0);
  box.innerHTML = '<b>Integrity violation (local operator)</b>' +
    'Current match evaluations with no property link: ' + nullLinked + '. ' +
    'Rows in matches with a forbidden trust state: ' + forbidden + '. ' +
    'Repair the links before trusting these views.';
  box.hidden = false;
}

function migrationStorageKey(completedAt) {
  return MIGRATION_STORAGE_PREFIX + String(completedAt === null || completedAt === undefined ? '' : completedAt);
}

function migrationDismissed(completedAt) {
  try { return localStorage.getItem(migrationStorageKey(completedAt)) === 'dismissed'; }
  catch (err) { return false; }
}

function migrationSummaryShown(payload) {
  // Section 7.3, pinned: the banner is shown only when the migration summary is
  // a dict carrying the four named counts as integers, the integrity warning is
  // false, and the export was built by the very run that carried the summary
  // (`stats.local_rules.applied_at` equals that run's `completed_at`). Any other
  // state renders nothing at all - never a placeholder.
  const data = payload || {};
  if (!data.integrity || data.integrity.warning) return false;
  const run = data.migration || {};
  const summary = run.migration;
  if (!summary || typeof summary !== 'object' || Array.isArray(summary)) return false;
  for (let i = 0; i < MIGRATION_SUMMARY_KEYS.length; i++) {
    if (!Number.isInteger(summary[MIGRATION_SUMMARY_KEYS[i]])) return false;
  }
  const completedAt = run.completed_at;
  const appliedAt = ((data.stats || {}).local_rules || {}).applied_at;
  if (!completedAt || !appliedAt || String(completedAt) !== String(appliedAt)) return false;
  return !migrationDismissed(completedAt);
}

function dismissMigration() {
  // Section 7.3: the dismissal belongs to the run, so a later migration shows
  // its own banner.
  const run = (DATA && DATA.migration) || {};
  try { localStorage.setItem(migrationStorageKey(run.completed_at), 'dismissed'); }
  catch (err) { /* a browser without storage simply shows the banner again */ }
  paintMigration();
}

function paintMigration() {
  const box = document.getElementById('migrationBanner');
  if (!box) return;
  if (!migrationSummaryShown(DATA)) {
    box.dataset.migrationState = 'suppressed';
    box.hidden = true; box.innerHTML = '';
    return;
  }
  const summary = DATA.migration.migration;
  // Spec 08 section 7.3: the first v3.7 export - the migration that relinked
  // nothing - shows the new ranking sentence and the two clauses that follow it.
  // A summary that did relink sources keeps the existing v3.6 wording.
  const counts = Number(summary.trusted_blocks) + ' ranked &middot; ' +
    Number(summary.needs_verification) + ' need verification &middot; ' +
    Number(summary.excluded) + ' excluded.';
  const lines = Number(summary.relinked_sources) === 0
    ? '<div>Ranking rules updated: ' + counts + '</div>' +
      '<div>Missing landlord or building evidence no longer holds a listing; ' +
      'room rentals, sublets and private landlords are still excluded.</div>'
    : '<div>Inventory repaired: ' + Number(summary.relinked_sources) + ' source listings relinked.</div>' +
      '<div>' + Number(summary.trusted_blocks) + ' trusted blocks &middot; ' +
      Number(summary.needs_verification) + ' need verification &middot; ' +
      Number(summary.excluded) + ' excluded.</div>' +
      '<div>Room rentals and private/sublet listings are no longer ranked.</div>';
  box.innerHTML = '<div class="migration-lines">' + lines + '</div>' +
    '<button class="secondary-button" id="migrationDismiss" onclick="dismissMigration()">Dismiss</button>';
  box.dataset.migrationState = 'shown';
  box.hidden = false;
}

function missingScanText(row) {
  // Spec 09 section 9.1: a row absent from one or two complete scans says exactly
  // how many have not seen it. The read model computes the label from the row's
  // own miss count, so the renderer never re-derives a bucket, and it never calls
  // a missing row expired - the third complete miss owns that transition.
  return (row && row.missing_label) ? String(row.missing_label) : '';
}

function explicitGoneText(row) {
  // Spec 09 section 8.2 item 5: the advert's own detail page said it is gone, so
  // the row says exactly that, with when it was checked. Absence-based expiry is
  // different evidence and never borrows this wording.
  if (!row || !row.explicit_gone_at) return '';
  return 'Advert says no longer available · checked ' + shortWhen(row.explicit_gone_at);
}

function ageBoundsText(row) {
  // Spec 09 section 6.2/6.3: the advertiser's own age range, written the way the
  // decision's own reason writes it (both ends, `min N` or `max N`).
  const min = (row.age_min === null || row.age_min === undefined) ? null : row.age_min;
  const max = (row.age_max === null || row.age_max === undefined) ? null : row.age_max;
  if (min !== null && max !== null) return String(min) + '–' + String(max);
  if (min !== null) return 'min ' + min;
  if (max !== null) return 'max ' + max;
  return '';
}

function ageRequirementLines(row) {
  // Spec 09 sections 6.2-6.3 and 10.5: the row discloses the age rule's outcome
  // for the local profile - the advert's own bounds, and whether the profile
  // satisfies them. The profile's age itself and its stored record are never
  // rendered or exported, so nothing here reads a profile field. A row with no
  // readable bounds discloses nothing rather than guessing one.
  const out = [];
  if (!row || row.age_requirement_state !== 'bounded') return out;
  const bounds = ageBoundsText(row);
  if (!bounds) return out;
  out.push('Age requirement ' + bounds);
  if (row.profile_age_fit === 'eligible') out.push('Age requirement satisfied');
  return out;
}

function sourceFactRow(row, view) {
  // Section 7 items 2-4: the facts a source-level row shows. A review row shows
  // the exact source link, its missing fact, rent, availability and source; a
  // gone row says only how long ago it was last seen plus its last decision.
  const facts = [];
  if (view === 'gone') {
    // Spec 09 section 8.2 item 5: an explicitly gone advert says so, with when
    // its own detail page was checked. Only that row carries the wording.
    const gone = explicitGoneText(row);
    if (gone) facts.push('<li class="source-explicit-gone">' + esc(gone) + '</li>');
    facts.push('<li class="source-last-seen">' + esc(lastSeenText(row)) + '</li>');
    facts.push('<li class="source-decision">Last decision: ' + esc(row.outcome || 'unknown') + '</li>');
  } else {
    facts.push('<li class="' + (view === 'excluded' ? 'source-reason' : 'source-missing-fact') + '">' + (view === 'excluded' ? 'Reason: ' : 'Missing fact: ') + esc(row.primary_reason || 'not recorded') + '</li>');
    // Spec 09 section 9.1: one or two complete misses read as a count of complete
    // scans that have not seen the row, and the third miss is what expires it.
    const missing = missingScanText(row);
    if (missing) facts.push('<li class="source-missing-scan">' + esc(missing) + '</li>');
    const ageLines = ageRequirementLines(row);
    for (let i = 0; i < ageLines.length; i++) {
      facts.push('<li class="source-age-requirement">' + esc(ageLines[i]) + '</li>');
    }
    facts.push('<li class="source-rent">Rent: ' + (row.rent_total === null || row.rent_total === undefined ? 'unknown' : kr(row.rent_total) + ' kr all-inclusive / month') + '</li>');
    facts.push('<li class="source-availability">Availability: ' + availabilityText(row) + '</li>');
    for (const f of (row.soft_flags || [])) {
      if (!f || !f.label) continue;
      const sev = (f.severity === 'check' || f.severity === 'over-limit') ? f.severity : 'info';
      facts.push('<li class="source-soft-flag"><span class="pill flag-' + sev + '">' + esc(f.label) + '</span></li>');
    }
    // Spec 08 section 7: a badge only where the row's own verdict carries
    // evidence - an absent badge is the whole statement - and section 7.2 puts
    // the evidence date and the stale state on the badge's title.
    facts.push(evidenceBadges(row));
    if (view === 'review') {
      facts.push('<li class="source-evidence-date">Evidence verified: ' + esc(verifiedDateText(row)) + '</li>');
      if (evidenceStale(row)) facts.push('<li class="source-stale">' + esc(STALE_REASON) + '</li>');
    }
  }
  facts.push('<li class="source-site">Source: ' + esc(row.site || 'unknown') + (row.listing_id === null || row.listing_id === undefined ? '' : ' #' + esc(row.listing_id)) + '</li>');
  return facts.join('');
}

const EMOJI_RE = /[\\u{1F300}-\\u{1FAFF}\\u2600-\\u27BF\\u2B00-\\u2BFF\\uFE0F\\u200D]/gu;

function plainTitle(row) {
  // A portal advert title is the search key and, when a source has no address,
  // the only thing that names the home. Portal titles carry characters of their
  // own (v36u1 found three production adverts with emoji); the dashboard's own
  // text stays emoji-free, so the displayed title is stripped of them.
  // `row.title` itself is never changed.
  const text = (row.title === null || row.title === undefined) ? '' : String(row.title);
  return text.replace(EMOJI_RE, '').replace(/\\s+/g, ' ').trim();
}

function sourceRow(row) {
  // The dataset decides how a row reads: the payload's own `view` name
  // (`needs_verification`, `recently_gone`, `excluded`) is the store's read
  // model's vocabulary, while the controls and containers use the dashboard's
  // dataset keys. The row is rendered for the dataset it was handed with.
  const view = ACTIVE_DATASET;
  const title = plainTitle(row);
  const address = row.address || title || 'Address unknown';
  const subtitle = (row.address && title) ? '<div class="source-title">' + esc(title) + '</div>' : '';
  const source = (row.site || '') + '#' + (row.listing_id === null || row.listing_id === undefined ? '' : row.listing_id);
  return '<article class="source-row" data-view="' + esc(view) + '" data-source="' + esc(source) + '">' +
    '<div class="source-address">' + esc(address) + '</div>' + subtitle +
    '<ul class="source-facts">' + sourceFactRow(row, view) + '</ul>' +
    sourceLink(row) + whyNotRanked(row, view) + '</article>';
}

function paintViewControls() {
  document.getElementById('candidateCompare').hidden = ACTIVE_DATASET !== 'trusted' ||
    !comparisonHomes().some(m => m.bw_id === 'BW-0000108') ||
    !DATA.trusted.some(m => m.complex_key === 'jorisvej-complex');
  const counts = (DATA && DATA.counts) || {};
  document.querySelectorAll('[data-view][data-count-key]').forEach(control => {
    const value = counts[control.dataset.countKey];
    const target = control.querySelector('.view-count');
    if (target) target.textContent = (value === null || value === undefined) ? '0' : String(value);
    const active = control.dataset.view === ACTIVE_DATASET;
    control.classList.toggle('on', active);
    control.setAttribute('aria-pressed', String(active));
  });
  DATASETS.forEach(name => {
    const container = document.getElementById('view-' + name);
    if (container) container.hidden = name !== ACTIVE_DATASET;
  });
  // Excluded opens collapsed: the reader chooses the view, then expands it.
  const excluded = document.getElementById('view-excluded');
  const excludedRows = document.getElementById('rows-excluded');
  if (excluded && excludedRows) excludedRows.hidden = excluded.dataset.collapsed === 'true';
  const expander = document.getElementById('expandExcluded');
  if (excluded && expander) {
    const collapsed = excluded.dataset.collapsed === 'true';
    expander.setAttribute('aria-expanded', String(!collapsed));
    expander.textContent = collapsed ? 'Show excluded listings' : 'Hide excluded listings';
  }
}

function showDataset(name) {
  if (DATASETS.indexOf(name) < 0) return;
  ACTIVE_DATASET = name;
  // The map is the trusted surface: pins are trusted rows only (section 7).
  if (ACTIVE_VIEW !== 'table') showView('table');
  render();
}

function toggleDatasetRows(name) {
  const container = document.getElementById('view-' + name);
  if (!container) return;
  container.dataset.collapsed = container.dataset.collapsed === 'true' ? 'false' : 'true';
  render();
}

function emptyState(rowsStored, filtered) {
  const title = rowsStored ? 'No listings match these filters' : 'Nothing stored in this view yet';
  const explanation = rowsStored
    ? 'The snapshot holds rows here, but none pass the current search and filters. Loosen them to widen the view.'
    : (ACTIVE_DATASET === 'trusted'
      ? 'No trusted blocks in this snapshot. Listings appear here after a scan applies the ranking rules.'
      : 'No rows in this dataset yet. The next scan fills each view from the sources it covers.');
  const action = rowsStored ? ' <button class="secondary-button" onclick="resetFilters()">Reset filters</button>' : '';
  return '<div class="empty"><strong>' + title + '</strong><p>' + explanation + '</p>' + action + '</div>';
}

function render() {
  syncSortUi(); updateLegendActiveState(); paintSummarySelection(); paintFilterCount(); paintCompareTray(); paintComparisonDesk();
  paintViewControls();
  const stored = datasetRows(ACTIVE_DATASET).length;
  const warning = document.getElementById('snapshotWarning');
  const stale = stored > 0 && (DATA.stats || {}).snapshot_observed_at === null;
  warning.hidden = !stale;
  if (stale) warning.textContent = 'This snapshot has no stored observations yet; counts and details may be empty until the next scan.';
  const zeroActionable = document.getElementById('zeroActionable');
  const actionableCount = (DATA.trusted || []).filter(r => r.relevance === 'actionable' && !r.expired_recently).length;
  zeroActionable.hidden = !(ACTIVE_DATASET === 'trusted' && actionableCount === 0 && stored > 0);
  if (ACTIVE_DATASET !== 'trusted') {
    // Needs verification, Recently gone and Excluded: one row per source, no
    // rank treatment, no map pins.
    const rows = sorted(datasetRows(ACTIVE_DATASET).filter(row => passes(row, false)));
    paintActiveFilters(rows.length);
    const body = document.getElementById('rows-' + ACTIVE_DATASET);
    if (body) body.innerHTML = rows.length ? rows.map(sourceRow).join('') : emptyState(stored, true);
    return;
  }
  const rows = sorted(DATA.trusted.filter(row => passes(row, true)));
  paintActiveFilters(rows.length);
  document.getElementById('rows').innerHTML = rows.length ? rows.map(tableRow).join('') : '<tr><td colspan="14" class="empty">' + (stored ? 'No listings match these filters. ' : 'Nothing stored yet. ') + '<button class="secondary-button" onclick="resetFilters()">Reset filters</button></td></tr>';
  document.getElementById('viewCards').innerHTML = rows.length ? rows.map(cardRow).join('') : emptyState(stored, true);
  if (mapBuilt) drawMatches(rows);
}

function statCard(label, value, filter) {
  const on = filter && cardFilterActive(filter) ? ' on' : '';
  const attr = filter ? ' data-filter="' + filter + '" onclick="applyCardFilter(\\'' + filter + '\\')"' : '';
  return '<button class="stat' + on + '"' + attr + ' aria-pressed="' + Boolean(filter && cardFilterActive(filter)) + '"><b>' + value + '</b><span>' + label + '</span></button>';
}

function cardFilterActive(filter) {
  if (filter === 'pending=1') return ONLY_PENDING;
  const [key, value] = filter.split('=');
  const id = { tier: 'fTier', rel: 'fRel', budget: 'fBudget' }[key];
  return id && document.getElementById(id).value === value;
}
function applyCardFilter(filter) {
  if (filter === 'pending=1') { ONLY_PENDING = !ONLY_PENDING; return render(); }
  const [key, value] = filter.split('=');
  const id = { tier: 'fTier', rel: 'fRel', budget: 'fBudget' }[key];
  if (id) {
    const control = document.getElementById(id);
    control.value = control.value === value ? '' : value;
  }
  render();
}

function toggleFilters() {
  const panel = document.getElementById('filterPanel');
  const isHidden = panel.hasAttribute('hidden');
  if (isHidden) panel.removeAttribute('hidden'); else panel.setAttribute('hidden', '');
  document.getElementById('filterToggle').setAttribute('aria-expanded', String(isHidden));
}
function clearFilter(id) {
  const el = document.getElementById(id);
  if (!el) return;
  if (id === 'fSearch') el.value = '';
  else if (id === 'fShort') el.value = 'hide_dismissed';
  else el.value = '';
  render();
}
function togglePending() { ONLY_PENDING = !ONLY_PENDING; render(); }
function toggleExpired() { ONLY_EXPIRED = !ONLY_EXPIRED; render(); }
function resetFilters() {
  document.getElementById('fTier').value = '';
  document.getElementById('fBand').value = '';
  document.getElementById('fBudget').value = '';
  document.getElementById('fRel').value = '';
  document.getElementById('fShort').value = 'hide_dismissed';
  document.getElementById('fProblem').value = '';
  document.getElementById('fSearch').value = '';
  document.getElementById('fSort').value = 'rank';
  SORT = { key: 'rank', dir: 'desc' }; ONLY_PENDING = false; ONLY_EXPIRED = false;
  render();
}
function toggleColumns() {
  SHOW_ALL_COLUMNS = !SHOW_ALL_COLUMNS;
  document.getElementById('viewTable').classList.toggle('all-columns', SHOW_ALL_COLUMNS);
  const button = document.getElementById('allColumns');
  button.textContent = SHOW_ALL_COLUMNS ? 'Compact columns' : 'All columns';
  button.setAttribute('aria-pressed', String(SHOW_ALL_COLUMNS));
}
function rememberUnitBrowser(bw, open) { if (open) OPEN_UNITS.add(bw); else OPEN_UNITS.delete(bw); }
function browseUnits(bw) {
  OPEN_UNITS.add(bw);
  if (!OPEN_DETAILS.has(bw)) toggleTableDetails(bw);
  ['detail-' + bw, 'card-detail-' + bw].forEach(id => {
    const detail = document.getElementById(id);
    if (detail) detail.querySelectorAll('.unit-browser').forEach(browser => { browser.open = true; });
  });
}
function toggleTableDetails(bw) {
  if (OPEN_DETAILS.has(bw)) OPEN_DETAILS.delete(bw); else OPEN_DETAILS.add(bw);
  const tableDetail = document.getElementById('detail-' + bw);
  const cardDetail = document.getElementById('card-detail-' + bw);
  [tableDetail, cardDetail].filter(Boolean).forEach(el => { if (OPEN_DETAILS.has(bw)) el.removeAttribute('hidden'); else el.setAttribute('hidden', ''); });
  document.querySelectorAll('button[aria-controls="detail-' + CSS.escape(bw) + '"], button[aria-controls="card-detail-' + CSS.escape(bw) + '"]').forEach(button => {
    button.setAttribute('aria-expanded', String(OPEN_DETAILS.has(bw)));
    button.textContent = OPEN_DETAILS.has(bw) ? 'Hide details' : 'Details';
  });
}

function shortWhen(value) {
  if (!value || Number.isNaN(new Date(value))) return 'never';
  const d = new Date(value), now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString('da-DK', { hour: '2-digit', minute: '2-digit' });
  return sameDay ? 'today ' + time
                 : d.toLocaleDateString('da-DK', { day: '2-digit', month: 'short' }) + ' ' + time;
}

function ageText(value) {
  if (!value || Number.isNaN(new Date(value))) return 'unknown';
  const hours = Math.floor((Date.now() - new Date(value).getTime()) / 3600000);
  if (hours < 1) return 'under an hour';
  if (hours < 48) return hours + ' h';
  return Math.floor(hours / 24) + ' days';
}

// Spec 06 section 10. Three facts, always visible and never merged: what each
// portal last proved about its own coverage, when the rules were last applied
// and which version they were, and how old the newest observation is. A local
// rescore is never allowed to read as "portal checked just now".
function paintFreshnessBlock() {
  const s = DATA.stats || {};
  const cov = s.portal_coverage || [];
  const parts = cov.map(c => {
    const status = c.coverage_status || 'never scanned';
    const cls = 'cov cov-' + String(status).replace(/[^a-z-]/g, '');
    let extra = '';
    if (c.cards_seen !== null && c.cards_seen !== undefined) extra += ' (' + c.cards_seen + ' cards)';
    if (c.areas_failed) extra += ' (' + c.areas_failed + ' of ' + (c.areas_total || 0) + ' areas failed)';
    return '<span>' + esc(c.site) + ' <span class="' + cls + '">' + esc(status) + '</span> '
      + esc(shortWhen(c.completed_at)) + esc(extra) + '</span>';
  });
  const lr = s.local_rules || {};
  const rules = lr.applied_at
    ? 'applied ' + esc(shortWhen(lr.applied_at)) + ' (rules ' + esc(lr.rules_version || 'unknown') + ')'
    : 'never applied';
  document.getElementById('freshBlock').innerHTML =
    '<div class="fl"><span class="fk">Portal data:</span><span>' + (parts.join(' · ') || 'no scan recorded') + '</span></div>' +
    '<div class="fl"><span class="fk">Local rules:</span><span>' + rules + '</span></div>' +
    '<div class="fl"><span class="fk">Snapshot age:</span><span>' + esc(ageText(s.snapshot_observed_at)) + '</span></div>';
}

function paintFreshness() {
  paintFreshnessBlock();
  const s = DATA.stats || {};
  const scans = s.last_scans || [];
  const dated = s.exported_at || (scans.find(scan => scan.completed_at) || {}).completed_at;
  const stamp = dated && !Number.isNaN(new Date(dated)) ? new Date(dated).toLocaleString('da-DK', { dateStyle: 'medium', timeStyle: 'short' }) : 'Unknown';
  const updateSource = s.exported_at ? 'Exported' : 'Live';
  document.getElementById('freshness').textContent = 'Snapshot updated ' + stamp;
  document.getElementById('freshStatus').textContent = 'Data ' + updateSource.toLowerCase() + ' · snapshot ' + stamp + ' · rules ' + (s.local_rules && s.local_rules.applied_at ? 'applied ' + shortWhen(s.local_rules.applied_at) : 'never applied');
  const entries = [
    '<span>Properties: ' + (s.total_properties || 0) + '</span>',
    '<span>Quarantined: ' + (s.quarantine_count || 0) + '</span>',
    '<span>Ungeocodable: ' + (s.ungeocodable_count || 0) + '</span>',
    '<button class="rank-chip' + (ONLY_PENDING ? ' on' : '') + '" onclick="togglePending()" aria-pressed="' + ONLY_PENDING + '">Street data pending: ' + (s.pending_count || 0) + '</button>'
  ].concat(scans.map(scan => {
    const when = scan.completed_at && !Number.isNaN(new Date(scan.completed_at)) ? new Date(scan.completed_at).toLocaleString('da-DK', { dateStyle: 'short', timeStyle: 'short' }) : 'Unknown time';
    return '<span>' + esc(scan.site) + ': ' + esc(scan.coverage_status || scan.outcome || 'unknown') + ' · ' + esc(scan.cards_seen || 0) + ' cards · ' + esc(scan.listings_observed || 0) + ' observed · ' + esc(when) + '</span>';
  }));
  (s.card_only || []).forEach(c => entries.push(
    '<span title="Stored by discovery; the detail page was not opened">' +
    esc(c.label) + ': ' + esc(c.count) + '</span>'));
  const expiredCount = (DATA.trusted || []).filter(r => r.expired_recently).length;
  entries.push('<button class="rank-chip' + (ONLY_EXPIRED ? ' on' : '') + '" onclick="toggleExpired()" aria-pressed="' + ONLY_EXPIRED + '">Expired recently: ' + expiredCount + '</button>');
  const next = s.next_scan_at && !Number.isNaN(new Date(s.next_scan_at)) ? new Date(s.next_scan_at).toLocaleString('da-DK', { dateStyle: 'short', timeStyle: 'short' }) : 'Unknown';
  entries.push('<span>Next scheduled scan: ' + esc(next) + ' · Digest ' + esc('__DIGEST_TIME__') + '</span>');
  document.getElementById('scanStats').innerHTML = entries.join('');
}

function paintStats() {
  const s = DATA.stats || {};
  const rc = s.rank_counts || {};
  const cards = [
    statCard('Actionable', s.actionable_count || 0, 'rel=actionable'),
    statCard('Intel', s.intel_count || 0, 'rel=intel'),
    statCard('Responsible Picks', (DATA.trusted || []).filter(r => r.is_responsible_pick).length, 'budget=rp')
  ];
  document.getElementById('stats').innerHTML = cards.join('');
  paintSummarySelection();
  paintFreshness();
  // Sections 7 and 7.3: the operator surfaces. The integrity warning is painted
  // first and the migration banner consults it, so the banner can never render
  // beside a live violation.
  paintIntegrity();
  paintMigration();
}

function paintSummarySelection() {
  const s = DATA.stats || {}, rc = s.rank_counts || {};
  document.querySelectorAll('.stat[data-filter]').forEach(card => {
    const active = cardFilterActive(card.dataset.filter);
    card.classList.toggle('on', active); card.setAttribute('aria-pressed', String(active));
  });
  document.getElementById('rankChips').innerHTML = LEGEND.ranks.map(r => '<button class="rank-chip' + (cardFilterActive('tier=' + r.letter) ? ' on' : '') + '" aria-pressed="' + cardFilterActive('tier=' + r.letter) + '" onclick="applyCardFilter(\\'tier=' + r.letter + '\\')"><span class="rank-dot" style="background:' + (TIER_COLOR[r.letter] || token('rank-c')) + '"></span>' + esc(r.letter) + ' · ' + (rc[r.letter] || 0) + '</button>').join('');
}

function activeFilterCount() {
  const ids = ['fTier', 'fBand', 'fBudget', 'fRel', 'fShort', 'fProblem'];
  let count = 0;
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    if (id === 'fShort') { if (el.value && el.value !== 'hide_dismissed') count++; }
    else if (el.value) count++;
  });
  if (ONLY_PENDING) count++;
  if (ONLY_EXPIRED) count++;
  return count;
}

function paintFilterCount() {
  document.getElementById('filterCount').textContent = String(activeFilterCount());
}

function showView(which) {
  ACTIVE_VIEW = which;
  document.getElementById('listResults').hidden = which !== 'table';
  document.getElementById('viewMap').hidden = which !== 'map';
  document.getElementById('tabTable').className = 'tab-button' + (which === 'table' ? ' on' : '');
  document.getElementById('tabMap').className = 'tab-button' + (which === 'map' ? ' on' : '');
  document.getElementById('tabTable').setAttribute('aria-pressed', String(which === 'table'));
  document.getElementById('tabMap').setAttribute('aria-pressed', String(which === 'map'));
  if (which === 'map') { buildMap(); setTimeout(() => map.invalidateSize(), 50); }
}

function sayCount(message, ms) {
  const area = document.getElementById('activeFilters');
  const old = area.innerHTML;
  area.innerHTML = '<span class="shown-count">' + esc(message) + '</span>';
  setTimeout(() => { if (area.textContent === message) { area.innerHTML = old; render(); } }, ms || 2000);
}

function focusOnTable(bw) {
  showView('table');
  const row = document.querySelector('tr[data-bw="' + bw + '"]') || document.querySelector('.listing-card[data-bw="' + bw + '"]');
  if (!row) return sayCount('listing is not shown by the current filters', 2500);
  row.scrollIntoView({ block: 'center' }); row.classList.add('flash');
  setTimeout(() => row.classList.remove('flash'), 1500);
}

function focusOnMap(bw, retried) {
  const listing = DATA.trusted.find(r => r.bw_id === bw);
  if (!listing || listing.lat === null || listing.lat === undefined || listing.lon === null || listing.lon === undefined) {
    sayCount('no coordinates for ' + bw, 2000);
    return;
  }
  showView('map');
  setTimeout(() => {
    const marker = markersByBw[bw];
    if (!marker) return sayCount('listing is not shown by the current filters', 2500);
    map.once('moveend', () => marker.openPopup());
    map.flyTo(marker.getLatLng(), 15);
  }, 80);
}

function openHash() {
  const match = /^#(BW-\\d{7})$/.exec(location.hash || '');
  if (match) focusOnMap(match[1]);
}

function unitsLine(p) {
  // One pin per building, showing its best unit. Say what else is in there, or a
  // block that lists a dozen flats at once looks like a single expensive flat.
  const n = p.unit_count || 0;
  if (n < 2) return '';
  const others = n - 1;
  let line = others + (others === 1 ? ' more unit' : ' more units') + ' at this address';
  if (p.unit_rent_min && p.unit_rent_max && p.unit_rent_max !== p.unit_rent_min) {
    line += ', ' + kr(p.unit_rent_min) + ' to ' + kr(p.unit_rent_max) + ' kr';
  }
  return '<div>' + line + '</div>';
}

function popupHtml(p) {
  const rank = p.display_tier || '?';
  const scoreVal = Math.round(p.score || 0);
  const bw = esc(p.bw_id || '');
  const badges = [];
  if (p.is_responsible_pick) badges.push('<span class="pill rp" title="Responsible Pick under the current rules">Responsible Pick</span>');
  if (p.more_in_building) badges.push('<span class="pill" title="More units live in this building">More in building · ' + kr(p.group_price_min) + '–' + kr(p.group_price_max) + ' kr</span>');
  if (p.features_pending) badges.push('<span class="pill pending" title="Street data is pending; the score is provisional">street data pending</span>');
  const support = [];
  if (p.kr_per_m2) support.push(Math.round(p.kr_per_m2) + ' kr/m²');
  if (p.value_label) support.push(esc(p.value_label));
  support.push(esc(p.buzz_detail || 'Street: unknown'));
  if (p.water_dist_m !== null && p.water_dist_m !== undefined) {
    support.push('Water ' + Math.round(p.water_dist_m) + ' m' + (p.water_name ? ' (' + esc(p.water_name) + ')' : ''));
  }
  const bits = [
    '<div class="pop"><div class="card-top"><div class="card-rank"><span class="tier ' + esc(rank) + '">' + esc(rank) + '</span><b>' + scoreVal + '/100</b></div><span class="pill">' + esc(p.relevance || '') + (p.shortlist_state ? ' · ' + esc(p.shortlist_state) : '') + '</span></div>',
    '<div class="card-address">' + esc(p.address) + ' · ' + bw + '</div>',
    '<div class="card-rent">' + kr(p.rent_total) + ' kr' + (p.size_m2 ? '<span class="card-meta" style="font-weight:400;font-size:.8125rem"> · ' + Math.round(p.size_m2) + ' m²</span>' : '') + '</div>',
    '<div class="card-journey">' + commuteHtml(p) + '</div>',
  ];
  if (badges.length) bits.push('<div>' + badges.join(' ') + '</div>');
  const u = unitsLine(p);
  if (u) bits.push(u);
  bits.push('<div class="popup-support">' + support.join(' · ') + '</div>');
  if (!STATIC) bits.push('<div>' + shortlistCell(p) + '</div>');
  const actions = ['<div class="card-actions">'];
  if (p.url) actions.push('<a href="' + esc(p.url) + '" target="_blank" rel="noopener">portal</a>');
  actions.push('<a href="#" onclick="focusOnTable(\\'' + bw + '\\'); return false;">Show in list</a></div>');
  bits.push(actions.join(''));
  bits.push('</div>');
  return bits.join('');
}

function drawMatches(rows) {
  const wanted = new Set(rows.map(r => r.bw_id));
  markersByBw = {};
  ['actionable', 'intel', 'saved'].forEach(k => layers[k].clearLayers());

  DATA.map.matches.features.forEach(f => {
    const p = f.properties;
    if (!wanted.has(p.bw_id)) return;
    const ll = [f.geometry.coordinates[1], f.geometry.coordinates[0]];
    const letter = p.display_tier || p.tier || '?';
    const color = TIER_COLOR[letter] || token('rank-c');
    const actionable = p.relevance === 'actionable';
    const isStretch = p.budget_band === 'stretch';
    const marker = L.marker(ll, {
      icon: pinIcon(letter, color, actionable, isStretch),
      zIndexOffset: actionable ? 1000 : 0,
      riseOnHover: true,
    }).bindPopup(popupHtml(p));
    markersByBw[p.bw_id] = marker;
    marker.addTo(actionable ? layers.actionable : layers.intel);

    if (p.shortlist_state === 'save' || p.shortlist_state === 'applied') {
      L.circleMarker(ll, { radius: 20, color: token('pin-ring'), weight: 2.5, fill: false })
        .addTo(layers.saved);
    }
  });
}

function buildMap() {
  if (mapBuilt) return;
  const o = DATA.map.office;
  map = L.map('map', { center: [o.lat, o.lon], zoom: 12 });
  // Tiles are the only part that needs the network; markers, stations and the
  // coastline are embedded, so the static export still draws without them.
  const tiles = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);
  tiles.on('tileerror', () => document.getElementById('mapMessage').removeAttribute('hidden'));

  ['water', 'metro', 'stog', 'intel', 'actionable', 'saved'].forEach(k => {
    layers[k] = L.layerGroup().addTo(map);
  });

  DATA.map.water.features.forEach(f => {
    L.polyline(f.geometry.coordinates.map(c => [c[1], c[0]]),
               { color: token('map-water'), weight: 1.5, opacity: 0.55 }).addTo(layers.water);
  });

  DATA.map.stations.features.forEach(f => {
    const p = f.properties;
    const ll = [f.geometry.coordinates[1], f.geometry.coordinates[0]];
    if (p.office) {
      // Sharan knows where Norreport is. A small ringed dot with a standing
      // label is enough; the big markers are reserved for listings.
      L.circleMarker(ll, {
        radius: 6, weight: 2.5, color: token('pin-ring'), fillColor: token('pin-ink'), fillOpacity: 1,
      }).bindTooltip(esc(p.name) + ' (office anchor)', {
        permanent: true, direction: 'right', className: 'office-label', offset: [8, 0],
      }).addTo(map);
      return;
    }
    const metro = p.network === 'Metro';
    const dot = metro ? token('map-metro') : token('map-stog');
    L.circleMarker(ll, {
      radius: 3, weight: 1, color: dot,
      fillColor: dot, fillOpacity: 0.9,
    }).bindTooltip(p.name + ' (' + p.network + ')').addTo(layers[metro ? 'metro' : 'stog']);
  });

  L.control.layers(null, {
    'Actionable matches': layers.actionable,
    'Intel matches': layers.intel,
    'Saved': layers.saved,
    'Metro stations': layers.metro,
    'S-tog stations': layers.stog,
    'Waterfront': layers.water,
  }, { collapsed: false }).addTo(map);

  const legend = L.control({ position: 'bottomright' });
  legend.onAdd = function () {
    const d = L.DomUtil.create('div', 'legend');
    L.DomEvent.disableClickPropagation(d);
    L.DomEvent.disableScrollPropagation(d);

    let collapsed = window.matchMedia && window.matchMedia('(max-width: 767px)').matches;
    try {
      const saved = localStorage.getItem('bw.legend.collapsed');
      if (saved !== null) collapsed = saved === 'true';
    } catch (e) {}

    let html = '<div class="lg-head"><span class="lg-title">Legend</span>' +
               '<span class="lg-hide">' + (collapsed ? '[show]' : '[hide]') + '</span></div>' +
               '<div class="lg-body"' + (collapsed ? ' hidden' : '') + '>';

    // Ranks
    LEGEND.ranks.forEach(r => {
      const color = TIER_COLOR[r.letter] || token('rank-c');
      html += '<div class="lg-row" data-kind="rank" data-key="' + esc(r.letter) + '" role="button" tabindex="0" aria-expanded="false">' +
              '<div class="lg-row-left"><i style="background:' + color + '"></i><b>' + esc(r.letter) + '</b> ' + esc(r.name) + '</div>' +
              '<span>' + r.min + '-' + r.max + '</span></div>' +
              '<div class="lg-blurb" id="blurb-rank-' + esc(r.letter) + '" hidden>' + esc(r.blurb) +
              ' <a href="#" class="lg-only" data-kind="rank" data-key="' + esc(r.letter) + '">only ' + esc(r.letter) + '</a></div>';
    });

    // Commute bands
    html += '<div class="lg-sec">Commute band</div>';
    LEGEND.bands.forEach(b => {
      html += '<div class="lg-row" data-kind="band" data-key="' + esc(b.letter) + '" role="button" tabindex="0" aria-expanded="false">' +
              '<div class="lg-row-left"><b>' + esc(b.name) + '</b></div>' +
              '</div>' +
              '<div class="lg-blurb" id="blurb-band-' + esc(b.letter) + '" hidden>' + esc(b.blurb) +
              ' <a href="#" class="lg-only" data-kind="band" data-key="' + esc(b.letter) + '">only ' + esc(b.name) + '</a></div>';
    });

    // Marks
    html += '<div class="lg-sec">Marks</div><div class="lg-marks">' +
            '<div>Capped at ' + kr(LEGEND.cap.ceiling) + ' kr all-inclusive (hard ceiling).</div>' +
            '<div>Large solid pin: actionable. Faded pin: intel. White ring: saved.</div>' +
            '<div>Small dot: station (blue metro, red S-tog). Ringed dot: ' + esc(OFFICE_NAME) + '.</div>' +
            '</div></div>';

    d.innerHTML = html;

    d.addEventListener('click', function (ev) {
      const hideBtn = ev.target.closest('.lg-hide') || (ev.target.closest('.lg-head') && !ev.target.closest('.lg-row'));
      if (hideBtn) {
        ev.preventDefault();
        const body = d.querySelector('.lg-body');
        const hideSpan = d.querySelector('.lg-hide');
        const isHidden = body.hasAttribute('hidden');
        if (isHidden) {
          body.removeAttribute('hidden');
          hideSpan.textContent = '[hide]';
        } else {
          body.setAttribute('hidden', '');
          hideSpan.textContent = '[show]';
        }
        try {
          localStorage.setItem('bw.legend.collapsed', isHidden ? 'false' : 'true');
        } catch (e) {}
        return;
      }

      const onlyLink = ev.target.closest('.lg-only');
      if (onlyLink) {
        ev.preventDefault();
        ev.stopPropagation();
        const kind = onlyLink.dataset.kind;
        const key = onlyLink.dataset.key;
        if (kind === 'rank') {
          const sel = document.getElementById('fTier');
          sel.value = sel.value === key ? '' : key;
        } else if (kind === 'band') {
          const sel = document.getElementById('fBand');
          sel.value = sel.value === key ? '' : key;
        }
        updateLegendActiveState(d);
        render();
        return;
      }

      const row = ev.target.closest('.lg-row');
      if (row) {
        ev.preventDefault();
        const kind = row.dataset.kind;
        const key = row.dataset.key;
        const blurb = d.querySelector('#blurb-' + kind + '-' + key);
        if (blurb) {
          if (blurb.hasAttribute('hidden')) {
            blurb.removeAttribute('hidden');
          } else {
            blurb.setAttribute('hidden', '');
          }
          row.setAttribute('aria-expanded', String(!blurb.hasAttribute('hidden')));
        }
      }
    });

    d.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      const row = ev.target.closest('.lg-row');
      if (!row) return;
      ev.preventDefault();
      row.click();
    });

    return d;
  };
  legend.addTo(map);
  mapBuilt = true;
  drawMatches(sorted(DATA.trusted.filter(row => passes(row, true))));
}

async function loadData() {
  // One payload, one fetch: the four datasets, their scoped counts and the map
  // (spec 07 section 7). The static export replaces this function with one that
  // paints the inlined payload instead of fetching it.
  DATA = await fetch('/api/dashboard').then(r => r.json());
  ACTIVE_DATASET = DEFAULT_DATASET;
  restoreComparison();
  paintStats();
  render();
  openHash();
  if (location.hash) showView('table');
}

function boot() { initPresentation(); paintStats(); render(); }
initPresentation();
loadData();
</script>
</body>
</html>
"""

# Placeholders in HTML_TEMPLATE replaced at import time
HTML_TEMPLATE = HTML_TEMPLATE.replace("__TIER_LEGEND__", json.dumps(tiers.legend_entries(), ensure_ascii=False))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__OFFICE_NAME_JSON__", json.dumps(config.OFFICE_NAME, ensure_ascii=False))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__TARGET_MOVE_DATE_JSON__", json.dumps(config.TARGET_MOVE_DATE))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__PREFERRED_WINDOW_START_JSON__", json.dumps(config.PREFERRED_WINDOW_START))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__OFFICE_NAME_HTML__", html.escape(config.OFFICE_NAME))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__DIGEST_TIME__", config.DIGEST_TIME)
HTML_TEMPLATE = HTML_TEMPLATE.replace("__EXPIRED_VISIBLE_DAYS__",
                                      str(config.EXPIRED_VISIBLE_DAYS))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__SCORE_PARTS__", json.dumps([
    ["affordability", config.SCORE_AFFORDABILITY_PTS],
    ["transit", config.SCORE_TRANSIT_PTS],
    ["commute", config.SCORE_COMMUTE_PTS],
    ["water", config.SCORE_WATER_PTS],
    ["centre", config.SCORE_CENTRE_PTS],
    ["buzz", config.SCORE_BUZZ_PTS],
    ["area", config.SCORE_AREA_PTS],
    ["floor", config.SCORE_FLOOR_PTS],
    ["value", config.SCORE_VALUE_PTS],
    ["date", config.SCORE_DATE_PTS],
]))
# Spec 07 sections 7, 7.2 and 7.3: the page's evidence vocabulary, substituted
# from `config` and the module constants above so a rename moves the page script
# with it. Every one is asserted present, so a forgotten placeholder fails at
# import rather than rendering as literal text.
HTML_TEMPLATE = HTML_TEMPLATE.replace("__TENANCY_PROFESSIONAL_STATE__",
                                      json.dumps(TENANCY_PROFESSIONAL_STATE))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__BUILDING_MANAGED_STATE__",
                                      json.dumps(BUILDING_MANAGED_STATE))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__MANAGEMENT_VERIFIED_LABEL__",
                                      json.dumps(MANAGEMENT_VERIFIED_LABEL))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__PREFERRED_LOCALITY_KIND__",
                                      json.dumps(PREFERRED_LOCALITY_KIND))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__PREFERRED_LOCALITY_LABEL__",
                                      json.dumps(PREFERRED_LOCALITY_LABEL))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__BUILDING_VERIFIED_LABEL__",
                                      json.dumps(BUILDING_VERIFIED_LABEL))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__EVIDENCE_MAX_DAYS__",
                                      str(int(config.BUILDING_EVIDENCE_MAX_DAYS)))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__STALE_REASON__",
                                      json.dumps(config.TRUST_REASON_STALE))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__EVIDENCE_PROBLEM_RANK__",
                                      str(int(EVIDENCE_PROBLEM_REASON_RANK)))
HTML_TEMPLATE = HTML_TEMPLATE.replace("__MIGRATION_SUMMARY_KEYS__",
                                      json.dumps(list(MIGRATION_SUMMARY_KEYS)))
for _placeholder in ("__TIER_LEGEND__", "__SCORE_PARTS__", "__DIGEST_TIME__",
                     "__EXPIRED_VISIBLE_DAYS__", "__TENANCY_PROFESSIONAL_STATE__",
                     "__BUILDING_MANAGED_STATE__", "__MANAGEMENT_VERIFIED_LABEL__",
                     "__BUILDING_VERIFIED_LABEL__", "__PREFERRED_LOCALITY_KIND__",
                     "__PREFERRED_LOCALITY_LABEL__", "__EVIDENCE_MAX_DAYS__",
                     "__STALE_REASON__", "__EVIDENCE_PROBLEM_RANK__",
                     "__MIGRATION_SUMMARY_KEYS__"):
    assert _placeholder not in HTML_TEMPLATE, \
        f"the page template still carries {_placeholder}"

# The page's initial payload literal, blanked by `export_static_html` before the
# payload is inlined. The two must stay in step, so a drift fails at import.
DATA_PLACEHOLDER = ("let DATA = { stats: {}, trusted: [], review: [], gone: [], "
                    "excluded: [], counts: {}, integrity: {}, migration: {}, "
                    "default_view: 'trusted', map: null };")
assert DATA_PLACEHOLDER in HTML_TEMPLATE
# The view the shell opens on is stated once (section 7 item 1): the root carries
# it, the page script reads it, and the payload names the same view.
assert f'data-default-view="{DEFAULT_DASHBOARD_VIEW}"' in HTML_TEMPLATE


# ==============================================================================
# Server
# ==============================================================================
class DashboardCache:
    """Serve the dashboard payload from memory instead of rebuilding it per request.

    The page fetches one ~15 MB payload. Building it queries every ranked
    building and each unit behind it, which takes several seconds on the
    production database; the published copy feels fast because the scan
    precomputes it once. This cache does the same locally: the scan's export
    writes `report.html.data.json` next to `report.html`, and the server serves
    those bytes (plus a gzip copy) after re-reading the file when it changes.
    With no export on disk it falls back to building the payload once.

    One lock guards the shared SQLite connection and the cached bytes. Requests
    are serialized with builds; the HTTP server is single-threaded anyway, and a
    rebuild happens at most once per scan.
    """

    def __init__(self, store: Store, watch_path: Optional[str] = None):
        self.store = store
        self.watch_path = watch_path or os.path.join(config.BASE_DIR, "report.html")
        self.data_path = self.watch_path + ".data.json"
        # The export beside report.html describes the production database. A
        # scratch copy (`viewer.py --db`) or a test store must build its own
        # payload, never serve the production one.
        self._use_export = (os.path.abspath(self._db_path())
                            == os.path.abspath(config.DB_PATH))
        self.db_lock = threading.RLock()
        self._raw: Optional[bytes] = None
        self._gzip: Optional[bytes] = None
        self._data_stamp: Any = None
        self._db_stamp: Any = None

    @staticmethod
    def _stamp(path: str) -> Any:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _db_path(self) -> str:
        return getattr(self.store, "db_path", config.DB_PATH)

    def _serialize_locked(self, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._raw = body
        self._gzip = gzip.compress(body, 6)

    def _load_export_locked(self) -> bool:
        """Use the payload the last export wrote, without rebuilding it."""
        if not self._use_export:
            return False
        try:
            with open(self.data_path, "rb") as f:
                body = f.read()
        except OSError:
            return False
        # Atomic replace makes a partial file impossible; this cheap shape
        # check avoids parsing 15 MB just to validate it.
        if len(body) < 2 or body[:1] != b"{" or body[-1:] != b"}":
            return False
        self._raw = body
        self._gzip = gzip.compress(body, 6)
        return True

    def _rebuild_locked(self, prefer_export: bool = True) -> None:
        # Read the stamps before building: a write that lands mid-build must
        # leave a different stamp behind so the watcher builds once more.
        self._data_stamp = self._stamp(self.data_path) if self._use_export else None
        self._db_stamp = self._stamp(self._db_path())
        started = time.monotonic()
        source = "export"
        if not (prefer_export and self._load_export_locked()):
            source = "database"
            self._serialize_locked(dashboard_payload(self.store))
        print(f"[viewer] {datetime.datetime.now():%Y-%m-%dT%H:%M:%S} dashboard cache "
              f"rebuilt from {source} in {time.monotonic() - started:.1f}s "
              f"({len(self._raw or b'') / 1e6:.1f} MB)", flush=True)

    def snapshot(self) -> tuple:
        """(raw JSON bytes, gzipped JSON bytes), loading once on first use."""
        with self.db_lock:
            if self._raw is None:
                self._rebuild_locked()
            return self._raw, self._gzip

    def apply_action(self, property_id: int, state: str, note: str) -> None:
        """Patch a shortlist change into the cached bytes so a reload keeps it."""
        with self.db_lock:
            if self._raw is None:
                return
            try:
                payload = json.loads(self._raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            for row in payload.get("trusted", []):
                if int(row.get("property_id") or 0) == int(property_id):
                    row["shortlist_state"] = state
                    row["shortlist_note"] = note
            features = ((payload.get("map") or {}).get("matches") or {}).get("features") or []
            for feature in features:
                props = feature.get("properties") or {}
                if int(props.get("property_id") or 0) == int(property_id):
                    props["shortlist_state"] = state
                    props["shortlist_note"] = note
            self._serialize_locked(payload)

    def warm(self) -> None:
        with self.db_lock:
            if self._raw is None:
                self._rebuild_locked()

    def watch(self, interval: float = 5.0, settle: float = 20.0) -> None:
        """Reload after an export, or after quiet non-scan database writes.

        The scan's export writes `report.html.data.json` when it finishes, so a
        change there is picked up immediately. Database writes that do not come
        from an export (a bot save, a local dismiss) are rebuilt once they have
        been quiet for `settle` seconds, which keeps a multi-minute scan from
        triggering a rebuild on every commit.
        """
        last_db = self._db_stamp
        quiet_since = time.monotonic()
        while True:
            time.sleep(interval)
            data_stamp = self._stamp(self.data_path) if self._use_export else None
            db_stamp = self._stamp(self._db_path())
            if db_stamp != last_db:
                last_db = db_stamp
                quiet_since = time.monotonic()
            with self.db_lock:
                export_changed = data_stamp != self._data_stamp
                db_changed = db_stamp != self._db_stamp
                quiet = time.monotonic() - quiet_since >= settle
                if export_changed:
                    self._rebuild_locked(prefer_export=True)
                elif db_changed and quiet:
                    self._rebuild_locked(prefer_export=False)


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    store = None
    cache: Optional[DashboardCache] = None

    def log_message(self, fmt, *args):
        pass

    def _json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dashboard(self):
        raw, compressed = self.cache.snapshot()
        use_gzip = "gzip" in self.headers.get("Accept-Encoding", "").lower()
        body = compressed if use_gzip else raw
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Accept-Encoding")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str):
        return self._json({"ok": False, "error": message}, status=status)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path in ("/", "/index.html"):
            body = HTML_TEMPLATE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/dashboard":
            return self._dashboard()
        if path == "/api/stats":
            with self.cache.db_lock:
                return self._json(dashboard_stats(self.store))
        if path == "/api/listings":
            with self.cache.db_lock:
                return self._json(listing_rows(self.store))
        if path == "/api/map":
            with self.cache.db_lock:
                return self._json(map_payload(self.store))
        if path == "/api/action":
            return self._error(405, "method not allowed")

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not _is_loopback(self.client_address[0]):
            return self._error(403, "loopback only")

        if path == "/api/action":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                return self._error(400, "invalid JSON")
            property_id = body.get("property_id") if isinstance(body, dict) else None
            action = body.get("action") if isinstance(body, dict) else None
            note = body.get("note", "") if isinstance(body, dict) else None
            if isinstance(property_id, bool) or not isinstance(property_id, int):
                return self._error(400, "property_id must be an integer")
            if action not in {"save", "unsave", "applied", "dismiss"}:
                return self._error(400, "invalid action")
            if not isinstance(note, str) or len(note) > 200:
                return self._error(400, "note must be a string of at most 200 characters")
            with self.cache.db_lock:
                if self.store.get_property_by_bw_id(property_id) is None:
                    return self._error(404, "unknown property")
                # Section 7: shortlist actions are bound to the trusted rows. A
                # property shown only in Needs verification, Recently gone or
                # Excluded is not actionable.
                if property_id not in trusted_property_ids(listing_rows(self.store)):
                    return self._error(404, "property is not in the Ranked view")
                self.store.record_user_action(property_id, action, note)
                state = self.store.get_action_states().get(property_id) or {}
                self.cache.apply_action(property_id, state.get("action", ""),
                                        state.get("note", ""))
            return self._json({"ok": True, "property_id": property_id,
                               "shortlist_state": state.get("action", ""),
                               "shortlist_note": state.get("note", "")})

        if path == "/api/scan":
            def _async_scan():
                try:
                    import subprocess
                    subprocess.run([sys.executable, "run.py", "--live"], cwd=config.BASE_DIR)
                except Exception as e:
                    print(f"Background scan error: {e}")

            threading.Thread(target=_async_scan, daemon=True).start()
            return self._json({"status": "scan_started"})

        self.send_response(404)
        self.end_headers()


def _is_loopback(addr: str) -> bool:
    """Allow state-changing requests only from the local machine."""
    return addr in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def export_static_html(output_file: str = "report.html", store: Optional[Store] = None,
                       payload_file: Optional[str] = None) -> str:
    """Write a standalone report with every payload inlined, no server needed.

    When the output is the local `report.html`, the same payload is also written
    atomically next to it as `report.html.data.json`. The local dashboard server
    serves those bytes instead of rebuilding the page data on every request.
    Other export targets, such as the hosted `index.html`, skip the sidecar.
    """
    if store is None:
        store = Store()
    if payload_file is None and os.path.basename(output_file) == "report.html":
        payload_file = output_file + ".data.json"
    exported_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    # The same one payload the live server serves (spec 07 section 7).
    payload = dashboard_payload(store, exported_at=exported_at)
    rows = payload["rows"]
    payload_json = json.dumps(payload, ensure_ascii=False)

    inline = ("DATA = " + payload_json + ";\n"
              "async function loadData() { restoreComparison(); paintStats(); render(); openHash(); }\n")
    html = HTML_TEMPLATE.replace("const STATIC = false;", "const STATIC = true;", 1)
    if "const STATIC = true;" not in html:
        raise RuntimeError("static export failed to enable static mode")
    html = html.replace(DATA_PLACEHOLDER, "let DATA = null;")
    if "let DATA = null;" not in html:
        raise RuntimeError("static export failed to blank the page's initial payload")
    load_start = html.find("async function loadData() {")
    load_end = html.find("\n}\n\nfunction boot()", load_start)
    if load_start < 0 or load_end < 0:
        raise RuntimeError("static export could not locate the live data loader")
    html = html[:load_start] + inline + html[load_end + 3:]
    if "DATA = {\"stats\"" not in html and "DATA = {'stats'" not in html:
        raise RuntimeError("static export failed to inline the payload")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html)

    # The local server serves this instead of rebuilding the same payload on
    # every page load. Write-then-replace keeps the watcher from reading half a
    # file while the export is still running.
    if payload_file:
        tmp_path = payload_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload_json)
        os.replace(tmp_path, payload_file)

    size_kb = round(os.path.getsize(output_file) / 1024)
    print(f"Exported standalone dashboard to {output_file} ({size_kb} KB, "
          f"{len(rows)} matches, map data inline)")
    return output_file


def make_server(store: Store, host: str, port: int):
    """Build a reusable dashboard server, falling through to a free port."""
    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    class BoundDashboardHandler(DashboardHandler):
        pass

    BoundDashboardHandler.store = store
    cache = DashboardCache(store)
    BoundDashboardHandler.cache = cache
    for candidate in range(port, port + 20):
        try:
            server = ReusableTCPServer((host, candidate), BoundDashboardHandler)
            server.dashboard_cache = cache
            return server, server.server_address[1]
        except OSError:
            continue
    raise OSError("Could not bind to any free port.")


def start_server(port: int = config.WEB_PORT, host: str = config.WEB_HOST,
                 store: Optional[Store] = None):
    if store is None:
        store = Store()
    try:
        server, bound_port = make_server(store, host, port)
    except OSError:
        print("Could not bind to any free port.")
        return

    cache: DashboardCache = server.dashboard_cache
    threading.Thread(target=cache.warm, daemon=True).start()
    threading.Thread(target=cache.watch, daemon=True).start()

    print(f"\n[BoligWatch v3.3 Local Viewer] Running at: http://localhost:{bound_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping BoligWatch Local Viewer...")
        server.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="BoligWatch v3.0 web viewer")
    ap.add_argument("--profile", default=None,
                    help="Path to a JSON search profile (default: profile.json)")
    ap.add_argument("--port", type=int, default=config.WEB_PORT)
    ap.add_argument("--host", default=config.WEB_HOST)
    ap.add_argument("--db", default=config.DB_PATH, help="Database path (use a copy for manual actions)")
    ap.add_argument("--export", type=str, default="", help="Write a standalone HTML report")
    args = ap.parse_args()

    if args.export:
        export_static_html(args.export, store=Store(db_path=args.db))
    else:
        start_server(port=args.port, host=args.host, store=Store(db_path=args.db))
