"""v3.3 acceptance checks — every bullet in PLAN-v3.3.md §10.

Three blocks map 1:1 to the plan's acceptance sections:
  - Ranking and eligibility  (§10, first block)
  - Responsible Pick         (§10, second block)
  - Consolidation            (§10, third block, involves store/complex logic)

All run fully offline against fixtures; the acceptance numbers fall directly out
of the frozen v3.3 decision constants in config.py (the curves are piecewise
linear over those anchors, so "7,500 = 35" is true by construction).
"""
import datetime
import json

import pytest

import config
import filters
import geo
import notify
import run
import tiers
from store import format_bw_id
from conftest import with_detail_evidence


TODAY = datetime.date(2026, 9, 6)


def _rec(**over):
    rec = {
        "site": "test",
        "listing_id": "L1",
        "title": "1-værelses lejlighed",
        "address": "Viola Nørløvs Gade 3, 2450 København",
        "listing_type": "lejlighed",
        "rooms": 1,
        "size_m2": 31.0,
        "rent_total": 8000,
        "deposit": 26850,
        "prepaid_rent": 8950,
        # v3.10: on the target date, so no early-start cost bends the rent curve.
        "available_from": "2027-02-01",
        "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "description": "Eget køkken og eget badeværelse. Ubegrænset lejeperiode.",
        "lat": 55.6670,
        "lon": 12.5450,
    }
    rec.update(over)
    return with_detail_evidence(rec)


def _evaluate(rec, stations, monkeypatch, metro_walk=400.0, s_walk=None,
              norreport=2000.0, city=3000.0, commute=12, water=600.0,
              water_kind="harbour", buzz=0.5):
    """Evaluate with every geo/feature input pinned (offline + deterministic)."""
    if metro_walk is None:
        monkeypatch.setattr(geo, "nearest_metro", lambda *a: (None, None, None, None))
    else:
        monkeypatch.setattr(geo, "nearest_metro",
                            lambda *a: ("MetroX", metro_walk * 0.7, float(metro_walk), 5))
    if s_walk is None:
        monkeypatch.setattr(geo, "nearest_transit",
                            lambda *a: (None, None, None, None, "s-tog"))
    else:
        monkeypatch.setattr(geo, "nearest_transit",
                            lambda *a: ("STogY", s_walk * 0.7, float(s_walk), 5, "s-tog"))
    monkeypatch.setattr(geo, "distance_to_norreport", lambda *a: norreport)
    # v3.4 moved the centrality gate from the straight line to the driving road
    # route; the straight line is now an audit diagnostic only.
    monkeypatch.setattr(geo, "road_route_to_norreport", lambda *a: norreport)
    monkeypatch.setattr(geo, "distance_to_radhuspladsen", lambda *a: city)
    import features
    monkeypatch.setattr(
        features, "get_place_features",
        lambda lat, lon, store=None, refresh=False: {
            "commute_min": commute, "buzz_score": buzz,
            "buzz_detail": "Fronts Testgade",
            "water_dist_m": water, "water_kind": water_kind,
            "water_name": "Inderhavnen", "features_pending": 0,
        })
    return filters.evaluate_listing(rec, stations, today=TODAY)


# ==============================================================================
# Ranking and eligibility
# ==============================================================================
def test_affordability_curve_acceptance(mock_stations, monkeypatch):
    # v3.12 (spec 11): the ramp runs 7.500 (35 pts) down to 9.700 (0 pts).
    for rent, want in ((7500, 35.0), (8500, 19.1), (9700, 0.0)):
        res = _evaluate(_rec(rent_total=rent), mock_stations, monkeypatch)
        assert res.passed, res.rejection_reason
        assert abs(res.score_breakdown["affordability"] - want) < 1e-6, \
            f"rent {rent} -> affordability {want}"


def test_affordability_rejects_above_9700(mock_stations, monkeypatch):
    res = _evaluate(_rec(rent_total=9701), mock_stations, monkeypatch)
    assert not res.passed


def test_metro_curve_acceptance(mock_stations, monkeypatch):
    for walk, want in ((500.0, 20.0), (800.0, 16.0), (1500.0, 0.0)):
        res = _evaluate(_rec(), mock_stations, monkeypatch, metro_walk=walk)
        assert res.passed, res.rejection_reason
        assert abs(res.score_breakdown["transit"] - want) < 1e-6, f"metro {walk} -> {want}"


def test_metro_above_1500_requires_responsible_pick(mock_stations, monkeypatch):
    # Metro beyond 1,500 m with no usable S-train: v3.11 ranks it with no-transit
    # points, no rejection (spec 10 section 4.1).
    no_rp = _evaluate(_rec(), mock_stations, monkeypatch, metro_walk=1600, s_walk=None)
    # Metro beyond 1,500 m but an S-train within 800 m + rent <= 8,500 -> RP.
    with_rp = _evaluate(_rec(rent_total=8500), mock_stations, monkeypatch,
                        metro_walk=1600, s_walk=600)
    assert no_rp.rejected is False and no_rp.passed is True
    assert any(f.code == "no_transit" for f in no_rp.soft_flags)
    assert with_rp.passed, with_rp.rejection_reason
    assert with_rp.is_responsible_pick is True


def test_s_train_fallback_curve_acceptance(mock_stations, monkeypatch):
    for walk, want in ((400.0, 16.0), (800.0, 12.0)):
        res = _evaluate(_rec(rent_total=8500), mock_stations, monkeypatch,
                        metro_walk=None, s_walk=walk)
        assert res.passed, res.rejection_reason
        assert abs(res.score_breakdown["transit"] - want) < 1e-6
    over = _evaluate(_rec(rent_total=8500), mock_stations, monkeypatch,
                     metro_walk=None, s_walk=801)
    assert over.rejected is False, "v3.11 no longer rejects beyond 800 m"
    assert over.score_breakdown["transit"] == 0.0
    assert any(f.code == "no_transit" for f in over.soft_flags)


def test_norreport_radius_acceptance(mock_stations, monkeypatch):
    at_limit = _evaluate(_rec(), mock_stations, monkeypatch, norreport=10000.0)
    over = _evaluate(_rec(), mock_stations, monkeypatch, norreport=10000.1)
    assert at_limit.passed, at_limit.rejection_reason
    assert over.rejected is False
    assert any(f.code == "norreport_distance" for f in over.soft_flags)


def test_commute_curve_acceptance(mock_stations, monkeypatch):
    fifteen = _evaluate(_rec(), mock_stations, monkeypatch, commute=15)
    assert abs(fifteen.score_breakdown["commute"] - 16.0) < 1e-6
    for c in (45, 50):
        res = _evaluate(_rec(), mock_stations, monkeypatch, commute=c)
        assert res.passed, res.rejection_reason
        assert res.score_breakdown["commute"] == 0.0, f"commute {c} scores 0 but is eligible"
    # v3.12 (spec 11): a commute over 50 is a hard rejection again.
    over = _evaluate(_rec(), mock_stations, monkeypatch, commute=51)
    assert over.rejected is True
    assert "commute" in over.rejection_reason


def test_value_date_confidence_do_not_change_score(mock_stations, monkeypatch):
    """§3.5: market value, availability fit and confidence carry zero rank weight.

    v3.10 adds one deliberate exception, tested separately: a lease starting
    BEFORE the target date costs overlap rent. A later date costs nothing.
    """
    base = _evaluate(_rec(), mock_stations, monkeypatch)

    segs = {"A": {"median_kr_m2": 300.0, "n": 40}}
    res_market = filters.evaluate_listing(_rec(), mock_stations, segments=segs, today=TODAY)
    assert res_market.score == base.score
    assert res_market.score_breakdown["value"] >= 0  # computed, but no rank weight

    res_date = _evaluate(_rec(available_from="2028-12-01"), mock_stations, monkeypatch)
    assert res_date.score == base.score


def test_reference_snapshot_bw35_tops_normal_metro(mock_stations, monkeypatch):
    """§8 directional result, re-weighted in v3.10 (transit 30 -> 20).

    With commute tied, BW-0000105's 750 kr saving now outweighs its 1.1 km metro
    walk, so it edges past BW-0000035. Both still beat the dear, fast BW-0000218.
    """
    # (bw, rent, metro_walk, commute)
    refs = [
        ("BW-0000035", 8250, 467, 25),
        ("BW-0000218", 9350, 373, 14),
        ("BW-0000105", 7500, 1109, 25),
    ]
    scored = []
    for bw, rent, walk, commute in refs:
        res = _evaluate(_rec(rent_total=rent), mock_stations, monkeypatch,
                        metro_walk=walk, commute=commute)
        assert res.passed, res.rejection_reason
        scored.append((bw, res.score, tiers.rank_tier_of(res.score)))
    scored.sort(key=lambda t: -t[1])
    assert [bw for bw, _, _ in scored] == ["BW-0000105", "BW-0000035", "BW-0000218"]


def test_rank_letters_match_existing_thresholds():
    assert dict(config.RANK_TIER_BANDS) == {"S": 75, "A": 60, "B": 45, "C": 30, "D": 0}
    assert tiers.rank_tier_of(100) == "S"
    assert tiers.rank_tier_of(75) == "S"
    assert tiers.rank_tier_of(74) == "A"
    assert tiers.rank_tier_of(60) == "A"
    assert tiers.rank_tier_of(0) == "D"


def test_decision_constants_are_self_contained():
    """The acceptance numbers, not prose, drive the engine (no magic literals)."""
    assert config.SCORE_AFFORDABILITY_PTS == 35.0
    assert config.SCORE_TRANSIT_PTS == 20.0
    assert config.SCORE_COMMUTE_PTS == 16.0
    assert config.SCORE_WATER_PTS == 3.0
    assert config.SCORE_CENTRE_PTS == 4.0
    assert config.SCORE_BUZZ_PTS == 3.0
    assert config.SCORE_AREA_PTS == 15.0
    assert config.SCORE_FLOOR_PTS == 4.0
    assert (config.SCORE_AFFORDABILITY_PTS + config.SCORE_TRANSIT_PTS
            + config.SCORE_COMMUTE_PTS + config.SCORE_WATER_PTS
            + config.SCORE_CENTRE_PTS + config.SCORE_BUZZ_PTS
            + config.SCORE_AREA_PTS + config.SCORE_FLOOR_PTS) == 100.0
    assert config.AFFORDABILITY_CURVE == [(7500, 35), (9700, 0)]
    assert config.TRANSIT_METRO_CURVE == [(500, 20), (800, 16), (1500, 0)]
    assert config.RP_S_TRAIN_CURVE == [(400, 16), (800, 12)]
    assert config.COMMUTE_CURVE == [(15, 16), (45, 0)]
    assert config.RENT_CAP_MAX == 9700
    assert config.NORREPORT_RADIUS_MAX_M == 10000
    assert config.COMMUTE_MAX_MIN == 50
    assert config.NORMAL_METRO_WALK_MAX_M == 1500
    assert config.RP_S_TRAIN_MAX_M == 800
    assert config.RP_RENT_MAX == 8500


# ==============================================================================
# Responsible Pick (plan §4 / §10 second block)
# ==============================================================================
def test_rp_full_acceptance_example(mock_stations, monkeypatch):
    """8,500 kr, Metro beyond 1.5 km, S-train at 800 m, Nørreport 10 km, 50 min:
    passes and carries the badge."""
    res = _evaluate(_rec(rent_total=8500), mock_stations, monkeypatch,
                    metro_walk=1600, s_walk=800, norreport=10000.0, commute=50)
    assert res.passed, res.rejection_reason
    assert res.is_responsible_pick is True
    row = res.to_dict()
    assert notify.is_responsible_pick(row) is True


def test_rp_boundaries_are_badges_not_rejections(mock_stations, monkeypatch):
    # v3.12 (spec 11): the commute ceiling is a hard rejection again, so it
    # leaves the badge list and rejects on its own below.
    cases = [
        dict(metro_walk=1600, s_walk=801, norreport=10000.0, commute=50),   # S-train
        dict(metro_walk=1600, s_walk=800, norreport=10001.0, commute=50),   # Nørreport
    ]
    for over in cases:
        res = _evaluate(_rec(rent_total=8500), mock_stations, monkeypatch, **over)
        assert res.rejected is False, f"v3.11 no longer rejects for {over}"
        assert res.soft_flags, f"expected a badge for {over}"
    over_commute = _evaluate(_rec(rent_total=8500), mock_stations, monkeypatch,
                             metro_walk=1600, s_walk=800, norreport=10000.0, commute=51)
    assert over_commute.rejected is True
    assert "commute" in over_commute.rejection_reason


def test_rp_8501_s_train_only_ranks(mock_stations, monkeypatch):
    res = _evaluate(_rec(rent_total=8501), mock_stations, monkeypatch,
                    metro_walk=None, s_walk=500)
    assert res.rejected is False
    assert res.passed is True


def test_within_metro_range_never_rp(mock_stations, monkeypatch):
    res = _evaluate(_rec(rent_total=7000), mock_stations, monkeypatch,
                    metro_walk=1200, s_walk=300)
    assert res.passed, res.rejection_reason
    assert res.is_responsible_pick is False


def test_rp_appears_in_score_order_and_responds_to_filter(temp_db):
    """Responsible Picks are ordinary rows: score-ordered and flagged (§10)."""
    store = temp_db
    pid_rp = store._find_or_create_property_direct(
        "Sydhavnsgade 1, 2450", 55.64, 12.54, 1, 40.0, "new",
        datetime.datetime.now().isoformat())
    pid_nm = store._find_or_create_property_direct(
        "Vesterbrogade 10, 1620", 55.6764, 12.5682, 1, 35.0, "new",
        datetime.datetime.now().isoformat())
    now = datetime.datetime.now().isoformat()
    for pid, addr, score, rent, rp in (
            (pid_rp, "Sydhavnsgade 1, 2450", 78.0, 8500, 1),
            (pid_nm, "Vesterbrogade 10, 1620", 65.0, 8000, 0)):
        store.conn.execute(
            "INSERT INTO matches(property_id, bw_id, tier, category, score, "
            "rent_total, commute_min, relevance, address, quarantined, "
            "is_responsible_pick) "
            "VALUES(?,?, 'B',2,?,?,25,'actionable',?,0,?)",
            (pid, format_bw_id(pid), score, rent, addr, rp))
    store.conn.commit()

    reps = store.grouped_matches(include_dismissed=True)
    assert [r["bw_id"] for r in reps] == \
        [format_bw_id(pid_rp), format_bw_id(pid_nm)], "score desc, already §3.6 order"
    rp_row = next(r for r in reps if r["bw_id"] == format_bw_id(pid_rp))
    assert bool(rp_row["is_responsible_pick"]) is True
    assert bool(next(r for r in reps if r["bw_id"] == format_bw_id(pid_nm))["is_responsible_pick"]) is False


# ==============================================================================
# Consolidation (plan §5 / §10 third block)
# ==============================================================================
def _seed_property(store, addr, rent, score=60.0, complex_key="viola-noerloevs-gade-complex",
                   rp=0, category=1, expire=False, rent_override=None):
    """Create one property + one active source listing + one match row.

    Properties are created via exact-address dedup, so "Viola Nørløvs Gade 1"
    and "Viola Nørløvs Gade 3" are distinct property rows — exactly what the
    plan's cross-property complex consolidation is for.
    """
    now = datetime.datetime.now().isoformat()
    pid = store._find_or_create_property_direct(addr, 55.65, 12.54, 1, 35.0, "new", now)
    sid = store.record_source_listing(
        pid, "test", f"L{pid}", f"https://example/{pid}",
        {"site": "test", "address": addr, "size_m2": 35.0,
         "rent_total": rent_override if rent_override is not None else rent})
    if expire:
        store.conn.execute("UPDATE source_listings SET lifecycle_state='expired' WHERE id=?",
                           (sid,))
    store.conn.execute(
        "INSERT INTO matches(property_id, bw_id, tier, category, score, "
        "rent_total, commute_min, relevance, address, quarantined, "
        "is_responsible_pick, complex_key) "
        "VALUES(?,?, 'A',?,?,?,15,'actionable',?,0,?,?)",
        (pid, format_bw_id(pid), category, score, rent, addr, rp, complex_key))
    store.conn.commit()
    return pid


def test_viola_complex_one_row_bw1_represents(temp_db):
    store = temp_db
    # Property 1 (BW-0000001) at 9,350 kr is the cheapest -> represents the group.
    pid1 = _seed_property(store, "Viola Nørløvs Gade 1, 2450 København SV", 9350)
    pid3 = _seed_property(store, "Viola Nørløvs Gade 3, 2450 København SV", 9400)

    assert store.resolve_complex_key("Viola Nørløvs Gade 1, 2450", "2450") == \
        "viola-noerloevs-gade-complex"
    assert store.resolve_complex_key("Viola Noerloevs Gade 3, 2450", "2450") == \
        "viola-noerloevs-gade-complex", "Unicode-insensitive (oe/ø) matching"

    reps = store.grouped_matches(include_dismissed=True)
    complex_reps = [r for r in reps if r.get("complex_key")]
    assert len(complex_reps) == 1, "one row/pin for the whole complex"
    rep = complex_reps[0]
    assert rep["rent_total"] == 9350, "the 9,350 kr unit represents the group"
    assert rep["more_in_building"] is True
    assert rep["group_price_min"] == 9350 and rep["group_price_max"] == 9400
    assert set(rep["group_members"]) == {pid1, pid3}, "both BW-IDs preserved"


def test_a_costlier_sibling_cannot_displace_a_cheaper_live_unit(temp_db):
    """§5.2 / §10: a higher score or better date cannot displace the cheapest
    eligible live unit; the representative must stay 9,350 kr."""
    store = temp_db
    _seed_property(store, "Viola Nørløvs Gade 1, 2450 København SV", 9350, score=50.0)
    _seed_property(store, "Viola Nørløvs Gade 3, 2450 København SV", 9400, score=99.0)
    rep = next(r for r in store.grouped_matches(include_dismissed=True)
               if r.get("complex_key"))
    assert rep["rent_total"] == 9350, "cheaper live unit beats the 99/100 costlier one"
    assert rep["score"] == 50.0


def test_representative_changes_when_the_cheapest_expires(temp_db):
    store = temp_db
    _seed_property(store, "Viola Nørløvs Gade 1, 2450 København SV", 9350)
    _seed_property(store, "Viola Nørløvs Gade 3, 2450 København SV", 9400)
    # The cheapest unit expires -> the remaining eligible live unit takes over.
    pid1 = store.get_match_row(1)["property_id"]
    store.conn.execute("UPDATE source_listings SET lifecycle_state='expired' "
                       "WHERE property_id=?", (pid1,))
    store.conn.commit()
    rep = next(r for r in store.grouped_matches(include_dismissed=True)
               if r.get("complex_key"))
    assert rep["rent_total"] == 9400
    assert rep["more_in_building"] is False, "only one eligible live price left"


def test_unrelated_adjacent_buildings_never_merge_without_alias(temp_db):
    store = temp_db
    _seed_property(store, "Hvidovrevej 101, 2650 Hvidovre", 8000, complex_key=None)
    _seed_property(store, "Hvidovrevej 103, 2650 Hvidovre", 8200, complex_key=None)
    reps = store.grouped_matches(include_dismissed=True)
    assert len(reps) == 2, "adjacent but unconfigured addresses stay separate"
    assert all(not r.get("complex_key") for r in reps)


def test_price_span_excludes_expired_duplicate_quarantined_overcap(temp_db):
    store = temp_db
    _seed_property(store, "Viola Nørløvs Gade 1, 2450 København SV", 9350)
    _seed_property(store, "Viola Nørløvs Gade 3, 2450 København SV", 9400)
    # An over-cap (9,750) active listing on the same property must not widen
    # the span (raw_data 9,750 > 9,700 cap, v3.12).
    pid3 = store.conn.execute(
        "SELECT p.id FROM properties p JOIN matches m ON m.property_id=p.id "
        "WHERE m.rent_total=9400").fetchone()["id"]
    store.record_source_listing(
        pid3, "test", "L-OVER", "https://example/over",
        {"site": "test", "address": "Viola Nørløvs Gade 3, 2450 København SV",
         "size_m2": 35.0, "rent_total": 9750})
    store.conn.commit()
    rep = next(r for r in store.grouped_matches(include_dismissed=True)
               if r.get("complex_key"))
    # over-cap ignored -> eligible live span is still 9,350-9,400
    assert rep["group_price_min"] == 9350 and rep["group_price_max"] == 9400
    assert rep["more_in_building"] is True


def test_details_resolves_representative_and_sibling_bw_ids(temp_db):
    store = temp_db
    pid1 = _seed_property(store, "Viola Nørløvs Gade 1, 2450 København SV", 9350)
    pid3 = _seed_property(store, "Viola Nørløvs Gade 3, 2450 København SV", 9400)
    reps = store.grouped_matches(include_dismissed=True)
    complex_reps = [r for r in reps if r.get("complex_key")]
    assert len(complex_reps) == 1, "/details has one representative"
    rep = complex_reps[0]
    assert set(rep["group_members"]) == {pid1, pid3}, "both BW-IDs resolved"
    # Direct lookup of the hidden sibling still works (its row exists).
    assert all(store.get_match_row(p) is not None for p in (pid1, pid3))
    # The non-representative sibling is grouped under the representative's BW-ID.
    assert store.get_group_by_property(pid3) == "viola-noerloevs-gade-complex"


def test_fully_expired_property_is_purged_from_matches(temp_db):
    """§2/§10: a property whose listings have all expired must not stay visible.

    The rescore and the scan lifecycle both clear_stale_matches(), so an expired
    (or over-cap) home that was matched under the old stretch band never surfaces
    through the hard 9,500 kr cap. Mirrors the live-data bug where BW-0000118
    (9,640 kr) and BW-0000101 (9,550 kr) persisted because their listings were
    expired and the rescore candidates query only selects active/missing.
    """
    store = temp_db
    # A standalone expired property that once matched (e.g. over-cap under the
    # new hard cap) must be cleared, not served as a representative.
    _seed_property(store, "Hans Knudsens Plads 1A, 2100 København Ø", 9640,
                   complex_key=None, expire=True)
    # A live standalone property stays.
    _seed_property(store, "Englandsvej 51, 2300 København S", 7500, complex_key=None)

    before = len(store.grouped_matches(include_dismissed=True))
    cleared = store.clear_stale_matches()
    assert cleared == 1, "only the fully-expired property is stale"
    after = store.grouped_matches(include_dismissed=True)
    assert len(after) == before - 1, "expired property dropped from the served list"
    assert all(r["rent_total"] <= config.RENT_CAP_MAX for r in after)
    # Property 1 is the expired one, property 2 the live one.
    served_ids = {r["property_id"] for r in after}
    assert 1 not in served_ids, "expired property purged from the served list"
    assert 2 in served_ids, "live property stays"
    assert after[0]["bw_id"] == "BW-0000002"
