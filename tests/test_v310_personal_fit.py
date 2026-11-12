"""v3.10 personal fit (1 Oct 2026): neighbourhood rating, never zones, floor
height and the early-start overlap cost."""
import datetime

import config
import filters
from test_v33_acceptance import TODAY, _evaluate, _rec


# ==============================================================================
# Never zones — hard rejections
# ==============================================================================
def test_never_postcode_rejects_bronshoj(monkeypatch):
    monkeypatch.setattr(config, "NEVER_POSTCODES", {"2700": "Brønshøj"})
    reason = filters.never_zone_reason(
        {"address": "Frederikssundsvej 200, 2700 Brønshøj"}, 55.70, 12.49)
    assert reason == "never zone: Brønshøj (2700)"


def test_never_zone_radius_around_norrebro_station(monkeypatch):
    monkeypatch.setattr(config, "NEVER_ZONES",
                        (("Nørrebro station", 55.7000485, 12.5379713, 800.0),))
    name, z_lat, z_lon, radius = config.NEVER_ZONES[0]
    assert name == "Nørrebro station" and radius == 800.0
    # ~550 m north of the station: inside.
    assert filters.never_zone_reason({}, z_lat + 0.005, z_lon) is not None
    # ~1.1 km north: outside.
    assert filters.never_zone_reason({}, z_lat + 0.010, z_lon) is None
    assert filters.never_zone_reason({}, None, None) is None


def test_never_zone_is_a_score_penalty_not_a_rejection(mock_stations, monkeypatch):
    # v3.11 (spec 10 call 3): a never-zone flat is down-ranked and badged, never
    # rejected.
    monkeypatch.setattr(config, "NEVER_POSTCODES", {"2700": "Brønshøj"})
    res = _evaluate(_rec(address="Testvej 1, 2700 Brønshøj"), mock_stations, monkeypatch)
    assert res.rejected is False
    assert res.passed is True
    assert any(f.code == "never_zone" for f in res.soft_flags)


# ==============================================================================
# Neighbourhood — 15 points by nearest station
# ==============================================================================
def test_area_rating_uses_the_profile_station_ratings(monkeypatch):
    monkeypatch.setattr(config, "AREA_STATION_RATINGS", {
        "Flintholm": 1.0, "Sluseholmen": 1.0, "Nuuks Plads": 0.75,
        "Vanløse": 0.5, "Femøren": 0.0, "Østerport": 1.0, "Trianglen": 1.0,
        "Nørrebros Runddel": 0.25, "Peter Bangs Vej": 0.4,
    })
    assert filters.area_rating("Flintholm") == 1.0
    assert filters.area_rating("Sluseholmen") == 1.0
    assert filters.area_rating("Nuuks Plads") == 0.75
    assert filters.area_rating("Vanløse") == 0.5
    assert filters.area_rating("Femøren") == 0.0
    assert filters.area_rating("Østerport") == 1.0
    assert filters.area_rating("Trianglen") == 1.0
    assert filters.area_rating("Nørrebros Runddel") == 0.25
    assert filters.area_rating("Peter Bangs Vej") < filters.area_rating("Vanløse")
    assert filters.area_rating("Somewhere Unrated") == config.AREA_DEFAULT_RATING
    assert filters.area_rating(None) == config.AREA_DEFAULT_RATING


def test_area_points_follow_the_nearest_transit_station(mock_stations, monkeypatch):
    # With s_walk given, _evaluate's nearest_transit returns the station "STogY".
    def area(rating):
        monkeypatch.setattr(config, "AREA_STATION_RATINGS", {"STogY": rating})
        return _evaluate(_rec(), mock_stations, monkeypatch,
                         s_walk=300.0).score_breakdown["area"]
    assert area(1.0) == config.SCORE_AREA_PTS
    assert area(0.0) == 0.0
    monkeypatch.setattr(config, "AREA_STATION_RATINGS", {})
    assert _evaluate(_rec(), mock_stations, monkeypatch, s_walk=300.0
                     ).score_breakdown["area"] == config.SCORE_AREA_PTS * config.AREA_DEFAULT_RATING


# ==============================================================================
# Floor height — 4 points
# ==============================================================================
def test_floor_is_read_from_the_listing_then_the_address():
    assert filters.floor_of({"eligibility_text": "Størrelse 23 m² Værelser 1 Etage 1. Møbleret Nej"}) == 1
    assert filters.floor_of({"eligibility_text": "Værelser 1 Etage Stuen Møbleret Nej"}) == 0
    assert filters.floor_of({"eligibility_text": "Etage: 6"}) == 6
    assert filters.floor_of({"normalized_address": "C.F. Richs Vej 105C, 1. 105, 2000 Frederiksberg"}) == 1
    assert filters.floor_of({"address": "Testgade 4, st. tv, 2000 Frederiksberg"}) == 0
    assert filters.floor_of({"address": "Viola Nørløvs Gade 3, 3. 3, 2450 København SV"}) == 3
    # A postcode is not a floor.
    assert filters.floor_of({"address": "Jorisvej 5, 2300 København S"}) is None
    # The listing's own field wins over the address.
    assert filters.floor_of({"eligibility_text": "Etage 4.", "address": "Testgade 1, 1. th"}) == 4


def test_floor_points(mock_stations, monkeypatch):
    def floor_pts(floor):
        return _evaluate(_rec(address=f"Viola Nørløvs Gade 3, {floor} th, 2450 København"),
                         mock_stations, monkeypatch).score_breakdown["floor"]
    assert floor_pts("st.") == 0.0
    assert floor_pts("2.") == 2.0
    assert floor_pts("4.") == config.SCORE_FLOOR_PTS
    assert floor_pts("9.") == config.SCORE_FLOOR_PTS
    assert _evaluate(_rec(), mock_stations, monkeypatch
                     ).score_breakdown["floor"] == config.FLOOR_UNKNOWN_PTS


# ==============================================================================
# Early start — overlap rent with the old lease
# ==============================================================================
def test_early_start_cost_rules():
    jan1 = datetime.date(2027, 1, 1)
    assert filters.early_start_cost(9000, jan1, "2027-01-01", TODAY) == 9000 * 31 / 30.0
    assert filters.early_start_cost(9000, datetime.date(2027, 2, 1), "2027-02-01", TODAY) == 0.0
    assert filters.early_start_cost(9000, datetime.date(2027, 3, 1), "2027-03-01", TODAY) == 0.0
    full = 9000 * config.EARLY_START_MAX_DAYS / 30.0
    # "Snarest" and a date already past mean "start now": the full capped overlap,
    # so an available-now unit never looks cheaper than a January one.
    assert filters.early_start_cost(9000, None, "snarest", TODAY) == full
    assert filters.early_start_cost(9000, datetime.date(2026, 9, 1), "2026-09-01", TODAY) == full
    # A missing date is unknown: no cost.
    assert filters.early_start_cost(9000, None, None, TODAY) == 0.0
    # Capped at EARLY_START_MAX_DAYS.
    capped = filters.early_start_cost(9000, datetime.date(2026, 10, 1), "2026-10-01", TODAY)
    assert capped == 9000 * config.EARLY_START_MAX_DAYS / 30.0


def test_a_january_start_scores_below_the_same_flat_in_february(mock_stations, monkeypatch):
    feb = _evaluate(_rec(rent_total=8650, available_from="2027-02-01"), mock_stations, monkeypatch)
    jan = _evaluate(_rec(rent_total=8650, available_from="2027-01-07"), mock_stations, monkeypatch)
    assert feb.passed and jan.passed
    # 25 days of overlap at 8,650 = ~7,208 kr, /12 = ~601 kr/month more rent.
    effective = 8650 + 8650 * 25 / 30.0 / 12.0
    want = 35.0 * (9700 - effective) / 2200.0
    assert abs(jan.score_breakdown["affordability"] - round(want, 1)) < 0.06
    assert jan.score < feb.score
    # The hard rent cap still uses the real rent.
    assert jan.rent_total == 8650


def test_danish_long_form_dates_parse():
    """lejebolig.dk writes "7. januar 2027"; unparsed it was neither actionable
    nor costed."""
    assert filters.parse_available_date("7. januar 2027") == (datetime.date(2027, 1, 7), "2027-01-07")
    assert filters.parse_available_date("1. november 2026") == (datetime.date(2026, 11, 1), "2026-11-01")
    assert filters.parse_available_date("15. oktober 2026")[0] == datetime.date(2026, 10, 15)
    assert filters.parse_available_date("1 maj 2027")[0] == datetime.date(2027, 5, 1)
    assert filters.parse_available_date("ledig efter aftale") == (None, None)
