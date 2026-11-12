"""Unified v3.0 score, tier-first ranking and the cross-tier regression guard."""
import datetime

import pytest

import config
import filters
import geo
from store import Store, format_bw_id
from conftest import with_detail_evidence


def _rec(**over):
    rec = {
        "site": "boligportal",
        "listing_id": "test-score",
        "title": "1-værelses studio",
        "address": "Vesterbrogade 10, 1620 København V",
        "listing_type": "lejlighed",
        "rooms": 1,
        "size_m2": 35.0,
        "rent_total": 5000,
        "deposit": 15000,
        "prepaid_rent": 0,
        "available_from": "2027-02-01",
        "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "description": "Eget køkken og badeværelse. Ubegrænset lejeperiode.",
        "lat": 55.6764406,
        "lon": 12.5682006,
    }
    rec.update(over)
    return with_detail_evidence(rec)


def _at_centre(monkeypatch, walk_m=400.0):
    monkeypatch.setattr(geo, "nearest_metro",
                        lambda lat, lon, st: ("Rådhuspladsen", walk_m * 0.7, walk_m, 5))
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda lat, lon, st: ("Rådhuspladsen", walk_m * 0.7, walk_m, 5, "metro"))
    monkeypatch.setattr(geo, "distance_to_radhuspladsen", lambda lat, lon: 0.0)


def test_perfect_score_is_100(mock_stations, monkeypatch, place_features):
    """Full marks on every ranking component sums to exactly the 100-point budget."""
    _at_centre(monkeypatch)
    place_features.update(commute_min=10, buzz_score=1.0, water_dist_m=100.0,
                          water_kind="harbour")
    monkeypatch.setattr(config, "AREA_STATION_RATINGS", {"Rådhuspladsen": 1.0})

    segments = {"all": {"n": 50, "median_kr_m2": 400.0, "p25_kr_m2": 300.0, "p75_kr_m2": 500.0}}
    res = filters.evaluate_listing(
        _rec(rent_total=5000, address="Vesterbrogade 10, 5. th, 1620 København V"),
        mock_stations, segments=segments)

    b = res.score_breakdown
    assert b["affordability"] == config.SCORE_AFFORDABILITY_PTS
    assert b["transit"] == config.SCORE_TRANSIT_PTS
    assert b["commute"] == config.SCORE_COMMUTE_PTS
    assert b["water"] == config.SCORE_WATER_PTS
    assert b["centre"] == config.SCORE_CENTRE_PTS
    assert b["buzz"] == config.SCORE_BUZZ_PTS
    assert b["area"] == config.SCORE_AREA_PTS
    assert b["floor"] == config.SCORE_FLOOR_PTS
    # §3.5: value and date are computed and shown but carry zero ranking points.
    assert b["value"] == config.SCORE_VALUE_PTS
    assert b["date"] == config.SCORE_DATE_PTS
    assert res.score == 100
    assert set(b) == {"affordability", "transit", "commute", "water", "centre",
                      "buzz", "area", "floor", "value", "date"}


def test_score_never_exceeds_100(mock_stations, monkeypatch, place_features):
    _at_centre(monkeypatch)
    for commute in (1, 10, 20, 30, 45, 50):
        for buzz in (0.0, 0.5, 1.0):
            place_features.update(commute_min=commute, buzz_score=buzz, water_dist_m=0.0)
            rent = 5000 if commute > 35 else 9000
            res = filters.evaluate_listing(_rec(rent_total=rent), mock_stations)
            assert 0 <= res.score <= 100


def test_component_formulas(mock_stations, monkeypatch, place_features):
    """Affordability, commute, water weighting and centrality follow the ramps."""
    _at_centre(monkeypatch)
    place_features.update(commute_min=30, buzz_score=0.4,
                          water_dist_m=1150.0, water_kind="lake")
    monkeypatch.setattr(geo, "distance_to_radhuspladsen", lambda lat, lon: 4750.0)

    res = filters.evaluate_listing(_rec(rent_total=9000), mock_stations)
    b = res.score_breakdown
    # affordability = 35 * (9700-9000)/2200 = 11.14 (v3.12 cap 9.700)
    assert b["affordability"] == 11.1
    # commute 30 -> linear 16 at 15 .. 0 at 45 => 8.0
    assert b["commute"] == 8.0
    # transit: metro walk 400 m -> full 20
    assert b["transit"] == 20.0
    # urban = 3 * 0.4 = 1.2
    assert b["buzz"] == 1.2
    # water = 3 * 0.6 (lake) * (2000-1150)/1700 = 0.9
    assert b["water"] == 0.9
    # centre = 4 * (8000-4750)/6500 = 2.0
    assert b["centre"] == 2.0
    # area: unrated station -> default 0.5 * 15; floor unknown -> 2
    assert b["area"] == 7.5
    assert b["floor"] == config.FLOOR_UNKNOWN_PTS


def test_confidence_is_a_badge_not_points(mock_stations, monkeypatch, place_features):
    _at_centre(monkeypatch)
    res = filters.evaluate_listing(_rec(), mock_stations)
    assert "confidence" not in res.score_breakdown
    assert res.verified_badge == f"Verified {res.confidence_points}/5"


def test_buzz_unknown_scores_zero(mock_stations, monkeypatch, place_features):
    _at_centre(monkeypatch)
    place_features.update(buzz_score=None, buzz_detail=None, features_pending=1)
    res = filters.evaluate_listing(_rec(), mock_stations)
    assert res.score_breakdown["buzz"] == 0.0
    assert res.features_pending == 1


def _insert(store, addr, category, score, rent, commute, value_ratio=None, relevance="actionable"):
    now = datetime.datetime.now().isoformat()
    pid = store._find_or_create_property_direct(addr, 55.6, 12.5, 1, 30, "new", now)
    bw = format_bw_id(pid)
    store.conn.execute("""
        INSERT INTO matches(property_id, bw_id, tier, category, score, rent_total,
                            commute_min, value_ratio, relevance, address, quarantined)
        VALUES(?,?,?,?,?,?,?,?,?,?,0)
    """, (pid, bw, config.TIER_LETTERS[category], category, score, rent, commute,
          value_ratio, relevance, addr))
    store.conn.commit()
    return pid, bw


def test_tier_first_ranking_and_tie_breaks(temp_db):
    store = temp_db
    _, bw_b = _insert(store, "Addr B tier 2", 2, 95.0, 8000, 25)
    _, bw_a_high_rent = _insert(store, "Addr A rent 9000", 1, 80.0, 9000, 15)
    _, bw_a_low_rent = _insert(store, "Addr A rent 8500", 1, 80.0, 8500, 15)
    _, bw_c = _insert(store, "Addr C tier 3", 3, 99.0, 7000, 40)

    order = [m["bw_id"] for m in store.get_matches()]
    assert order == [bw_a_low_rent, bw_a_high_rent, bw_b, bw_c]


def test_hellerup_never_outranks_jorisvej(temp_db):
    """Regression: a high-scoring tier C flat must not top the default view."""
    store = temp_db
    _, hellerup = _insert(store, "Gammel Vartov Vej 25C, Hellerup", 3, 99.0, 5800, 31,
                          value_ratio=0.77)
    _, jorisvej = _insert(store, "Jorisvej 1a, 2300 København", 1, 62.0, 8300, 18,
                          value_ratio=0.89)

    default_view = store.get_matches()
    assert default_view[0]["bw_id"] == jorisvej
    assert [m["bw_id"] for m in default_view].index(hellerup) > 0

    # Cross-tier sorting is only available as an explicit toggle.
    by_score = store.get_matches(order="score")
    assert by_score[0]["bw_id"] == hellerup


def test_alternate_sort_orders(temp_db):
    store = temp_db
    _, slow_cheap = _insert(store, "Slow but cheap", 3, 70.0, 6000, 44, value_ratio=0.70)
    _, fast_dear = _insert(store, "Fast but dear", 1, 90.0, 9400, 12, value_ratio=1.10)

    assert [m["bw_id"] for m in store.get_matches(order="commute")][0] == fast_dear
    assert [m["bw_id"] for m in store.get_matches(order="value")][0] == slow_cheap


def test_dismissed_and_relevance_filters(temp_db):
    store = temp_db
    keep_pid, keep = _insert(store, "Keep me", 1, 80.0, 8000, 15)
    drop_pid, drop = _insert(store, "Dismiss me", 1, 90.0, 8000, 15)
    _, intel = _insert(store, "Intel only", 1, 85.0, 8000, 15, relevance="intel")

    store.record_user_action(drop_pid, "dismiss", "too dark")
    ids = [m["bw_id"] for m in store.get_matches()]
    assert drop not in ids
    assert keep in ids
    assert drop in [m["bw_id"] for m in store.get_matches(include_dismissed=True)]
    assert [m["bw_id"] for m in store.get_matches(relevance="intel")] == [intel]


def test_a_rejected_sibling_listing_does_not_delete_a_valid_match(temp_db):
    """One property, two portals: a rejection on one must not clear the other's match."""
    store = temp_db
    pid = store._find_or_create_property_direct("Jorisvej 1a", 55.65, 12.60, 1, 32.0,
                                                "new", "2026-09-01T10:00:00")
    store.conn.execute("""
        INSERT INTO matches(property_id, bw_id, site, listing_id, tier, category,
                            score, rent_total, quarantined)
        VALUES(?,?,'boligportal','A1','A',1,78,8300,0)
    """, (pid, format_bw_id(pid)))
    store.conn.commit()

    store.clear_match_for_listing(pid, "lejebolig", "B2")
    assert store.get_match_row(pid) is not None, "a sibling listing must not clear the match"

    store.clear_match_for_listing(pid, "boligportal", "A1")
    assert store.get_match_row(pid) is None
