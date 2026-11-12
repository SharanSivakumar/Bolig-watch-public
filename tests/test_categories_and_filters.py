"""Tier assignment, studio verification, move-in ceilings and quarantine (v3.0)."""
import pytest

import config
import filters
import geo
from conftest import set_detail_text, with_detail_evidence


def _base_candidate(rent_total=8000, move_in_cost=30000, rooms=1, size_m2=35.0):
    return with_detail_evidence({
        "site": "boligportal",
        "listing_id": "test-123",
        "title": "1-værelses studio på Vesterbro",
        "address": "Vesterbrogade 10, 1620 København V",
        "listing_type": "lejlighed",
        "rooms": rooms,
        "size_m2": size_m2,
        "rent_base": rent_total - 500,
        "utilities_amount": 500,
        "rent_total": rent_total,
        "deposit": move_in_cost - rent_total if move_in_cost else 15000,
        "prepaid_rent": 0,
        "move_in_total_explicit": move_in_cost,
        "available_from": "2027-02-01",
        "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "description": ("Dejlig 1-værelses lejlighed med eget køkken og eget "
                        "badeværelse. Ubegrænset leje."),
        "lat": 55.6764,
        "lon": 12.5682,
    })


def _near_metro(monkeypatch, walk_m=400.0):
    monkeypatch.setattr(geo, "nearest_metro",
                        lambda lat, lon, st: ("Rådhuspladsen", walk_m * 0.7, walk_m, 5))
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda lat, lon, st: ("Rådhuspladsen", walk_m * 0.7, walk_m, 5, "metro"))


# --- Tier boundaries -------------------------------------------------------

@pytest.mark.parametrize("commute,tier,name", [
    (1, "A", "Close"),
    (20, "A", "Close"),
    (21, "B", "Commutable"),
    (35, "B", "Commutable"),
    (36, "C", "Far"),
    (50, "C", "Far"),
])
def test_tier_boundaries(mock_stations, monkeypatch, place_features, commute, tier, name):
    _near_metro(monkeypatch)
    place_features["commute_min"] = commute
    rent = 8000 if tier == "C" else 9500
    res = filters.evaluate_listing(_base_candidate(rent_total=rent, move_in_cost=40000),
                                   mock_stations)
    assert res.rejected is False
    assert res.tier == tier
    assert res.category_name == name
    assert res.category == config.TIER_NUMBERS[tier]
    assert res.passed is True


def test_commute_above_ceiling_is_ranked_not_rejected(mock_stations, monkeypatch, place_features):
    _near_metro(monkeypatch)
    # v3.12 (spec 11): 51 min is again a hard rejection (audit decision 4B).
    place_features["commute_min"] = 51
    res = filters.evaluate_listing(_base_candidate(rent_total=8000), mock_stations)
    assert res.rejected is True
    assert "commute" in res.rejection_reason
    # At the ceiling (50 min) it is still eligible and ranked.
    place_features["commute_min"] = 50
    at = filters.evaluate_listing(_base_candidate(rent_total=8000), mock_stations)
    assert at.rejected is False
    assert at.passed is True
    assert at.tier == "C"


def test_hard_rent_cap_is_9700(mock_stations, monkeypatch, place_features):
    """v3.12 (spec 11): 9,700 kr is the hard all-inclusive cap, above it there is
    no path (audit decision 1A)."""
    _near_metro(monkeypatch)
    place_features["commute_min"] = 40

    ok = filters.evaluate_listing(_base_candidate(rent_total=9700, move_in_cost=35000),
                                  mock_stations)
    assert ok.rejected is False

    res = filters.evaluate_listing(_base_candidate(rent_total=9701, move_in_cost=35000),
                                   mock_stations)
    assert res.rejected is True
    assert res.rejection_reason == "rent 9701 > 9700"


def test_tier_c_has_no_separate_rent_ceiling_in_v33(mock_stations, monkeypatch, place_features):
    """v3.3 removed the tier-C 8,500 ceiling: only the single 9,500 cap applies,
    and the 8,500 figure now matters only for Responsible Picks."""
    _near_metro(monkeypatch)  # a normal metro candidate
    place_features["commute_min"] = 40

    res = filters.evaluate_listing(_base_candidate(rent_total=9000, move_in_cost=35000),
                                   mock_stations)
    assert res.rejected is False
    assert res.tier == "C"
    assert res.is_responsible_pick is False


def test_normal_candidate_needs_metro_within_1500m(mock_stations, monkeypatch, place_features):
    """v3.3: a normal candidate must reach a Metro on foot within 1,500 m."""
    place_features["commute_min"] = 15
    _near_metro(monkeypatch, walk_m=1500.0)
    assert filters.evaluate_listing(_base_candidate(rent_total=9000),
                                    mock_stations).rejected is False

    # Above 1,500 m from the metro the home no longer rejects: v3.11 (spec 10
    # section 4.1) zeroes the transit points and badges it.
    _near_metro(monkeypatch, walk_m=1501.0)
    res = filters.evaluate_listing(_base_candidate(rent_total=9000), mock_stations)
    assert res.rejected is False
    assert res.passed is True
    assert any(f.code == "no_transit" for f in res.soft_flags)


def test_responsible_pick_classification(mock_stations, monkeypatch, place_features):
    """RP: metro >1,500 m, S-train <=800 m, rent <=8,500 kr -> admitted as RP."""
    place_features["commute_min"] = 20
    _near_metro(monkeypatch, walk_m=1600.0)  # metro too far; s-train walk 1600 too...

    # S-train walk within the 800 m RP cap with rent at/under 8,500 -> RP.
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda lat, lon, st: ("Sydhavn", 600.0, 400.0, 5, "s-tog"))
    rp = filters.evaluate_listing(_base_candidate(rent_total=8500), mock_stations)
    assert rp.rejected is False
    assert rp.is_responsible_pick is True

    # Over the RP rent cap but under the global 9,500 cap -> now ranked, RP
    # information only (spec 10 call 8 / section 4.1).
    over = filters.evaluate_listing(_base_candidate(rent_total=8501), mock_stations)
    assert over.rejected is False
    assert over.passed is True

    # S-train beyond 800 m -> ranked with the no-transit badge, not rejected.
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda lat, lon, st: ("Sydhavn", 900.0, 801.0, 10, "s-tog"))
    walk_fail = filters.evaluate_listing(_base_candidate(rent_total=8000), mock_stations)
    assert walk_fail.rejected is False
    assert any(f.code == "no_transit" for f in walk_fail.soft_flags)


def test_norreport_radius_10000m_passes(mock_stations, monkeypatch, place_features):
    """v3.4: a driving road route of exactly 10,000 m to Nørreport passes."""
    _near_metro(monkeypatch)
    place_features["commute_min"] = 30
    monkeypatch.setattr(geo, "road_route_to_norreport", lambda lat, lon: 10000.0)
    ok = filters.evaluate_listing(_base_candidate(rent_total=8000), mock_stations)
    assert ok.rejected is False

    monkeypatch.setattr(geo, "road_route_to_norreport", lambda lat, lon: 10001.0)
    res = filters.evaluate_listing(_base_candidate(rent_total=8000), mock_stations)
    assert res.rejected is False
    assert any(f.code == "norreport_distance" for f in res.soft_flags)


def test_geo_precision_penalty(mock_stations, monkeypatch, place_features):
    """A street-level address pays three minutes, which can move it down a tier."""
    _near_metro(monkeypatch)
    place_features["commute_min"] = 19

    rec = _base_candidate(rent_total=9000, move_in_cost=40000)
    rec["geo_precision"] = "street"
    res = filters.evaluate_listing(rec, mock_stations)
    assert res.commute_min == 22
    assert res.tier == "B"
    assert res.geo_precision == "street"


def test_ungeocodable_ranks_with_location_unknown(mock_stations, monkeypatch, place_features):
    _near_metro(monkeypatch)
    rec = _base_candidate(rent_total=8000)
    rec["lat"] = rec["lon"] = None
    res = filters.evaluate_listing(rec, mock_stations)
    assert res.rejected is False
    assert res.quarantined is False
    assert res.passed is True
    assert any(f.code == "location_unknown" for f in res.soft_flags)


# --- Unchanged v2.1 non-negotiables ---------------------------------------

def test_move_in_cost_inclusive_ceiling(mock_stations, monkeypatch):
    _near_metro(monkeypatch)

    res_pass = filters.evaluate_listing(
        _base_candidate(rent_total=8000, move_in_cost=config.MOVE_IN_COST_MAX), mock_stations)
    assert res_pass.passed is True
    assert res_pass.rejected is False

    res_fail = filters.evaluate_listing(
        _base_candidate(rent_total=8000, move_in_cost=config.MOVE_IN_COST_MAX + 1), mock_stations)
    assert res_fail.rejected is True
    assert "move-in cost" in res_fail.rejection_reason


def test_unknown_move_in_cost_quarantined(mock_stations, monkeypatch):
    _near_metro(monkeypatch)
    rec = _base_candidate(rent_total=8000)
    rec["listing_type"] = "værelse"
    rec["rooms"] = None
    rec["deposit"] = None
    rec["prepaid_rent"] = None
    rec["move_in_total_explicit"] = None
    res = filters.evaluate_listing(rec, mock_stations)
    assert res.rejected is True or res.quarantined is True


def test_unknown_availability_eligible_with_zero_points(mock_stations, monkeypatch):
    _near_metro(monkeypatch)
    rec = _base_candidate(rent_total=8000)
    rec["available_from"] = None
    res = filters.evaluate_listing(rec, mock_stations)
    assert res.passed is True
    assert res.score_breakdown["date"] == 0.0


def test_availability_outside_preferred_window_eligible(mock_stations, monkeypatch):
    _near_metro(monkeypatch)
    rec = _base_candidate(rent_total=8000)
    rec["available_from"] = "2026-09-01"
    res = filters.evaluate_listing(rec, mock_stations)
    assert res.passed is True
    assert res.is_target_window is False


def test_accept_1_and_2_rooms_and_reject_3_plus_rooms(mock_stations, monkeypatch):
    _near_metro(monkeypatch)

    assert filters.evaluate_listing(_base_candidate(rooms=1), mock_stations).passed is True
    assert filters.evaluate_listing(_base_candidate(rooms=2), mock_stations).passed is True
    assert filters.evaluate_listing(_base_candidate(rooms=3), mock_stations).rejected is True

    rec_shared = _base_candidate()
    set_detail_text(rec_shared, "Værelse med fælles køkken og bad i bofællesskab.")
    assert filters.evaluate_listing(rec_shared, mock_stations).rejected is True

    rec_student = _base_candidate()
    rec_student["title"] = "Studiebolig på Amager"
    assert filters.evaluate_listing(rec_student, mock_stations).rejected is True

    rec_elderly = _base_candidate()
    rec_elderly["url"] = ("https://www.lejebolig.dk/lejebolig/1910940/"
                          "aeldrebolig-paa-31-m2-til-8400-kr")
    rec_elderly["title"] = "Ældrebolig på 31 m2"
    res_elderly = filters.evaluate_listing(rec_elderly, mock_stations)
    assert res_elderly.rejected is True
    assert "elderly" in res_elderly.rejection_reason

    rec_senior = _base_candidate()
    set_detail_text(rec_senior, "Seniorbolig for 55+ med eget køkken og bad.")
    assert filters.evaluate_listing(rec_senior, mock_stations).rejected is True


def test_reject_fixed_term_sublet_leases(mock_stations, monkeypatch):
    _near_metro(monkeypatch)
    rec = _base_candidate()
    rec["rental_period"] = "tidsbegrænset i 12 måneder"
    rec["rental_period_raw"] = 12
    res = filters.evaluate_listing(rec, mock_stations)
    assert res.rejected is True
    assert "time-limited" in res.rejection_reason


def test_unknown_lease_duration_ranks_not_held(mock_stations, monkeypatch):
    _near_metro(monkeypatch)
    rec = _base_candidate()
    rec["listing_type"] = "lejlighed"
    rec["rooms"] = 1
    rec["rental_period"] = None
    rec["rental_period_raw"] = None
    set_detail_text(rec, "Lejlighed")
    res = filters.evaluate_listing(rec, mock_stations)
    assert res.quarantined is False
    assert res.passed is True


def test_geo_calls_never_run_for_a_hard_filter_failure(mock_stations, monkeypatch):
    """Spec 3.1: never route a listing that already failed rent, rooms or lease."""
    calls = []
    monkeypatch.setattr(geo, "nearest_metro",
                        lambda *a, **k: calls.append("metro") or ("X", 1.0, 1.0, 1))
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda *a, **k: calls.append("transit") or ("X", 1.0, 1.0, 1, "metro"))

    for rec in (_base_candidate(rooms=3),
                _base_candidate(rent_total=12000),
                _base_candidate(move_in_cost=90000)):
        filters.evaluate_listing(rec, mock_stations)
    assert calls == []
