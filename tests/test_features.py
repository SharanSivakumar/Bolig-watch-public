"""Urban buzz derivation, waterfront distance and the place_features cache."""
import json

import pytest

import config
import features


# --- Overpass fixtures -----------------------------------------------------
def _way(highway, name, points, wid=1):
    return {"type": "way", "id": wid, "tags": {"highway": highway, "name": name},
            "geometry": [{"lat": p[0], "lon": p[1]} for p in points]}


def _bus(ref, rid=1):
    return {"type": "relation", "id": rid, "tags": {"route": "bus", "ref": ref}}


LAT, LON = 55.66000, 12.60000
# A north-south polyline roughly 25 m east of (LAT, LON).
NEAR_LINE = [[LAT - 0.002, LON + 0.0004], [LAT + 0.002, LON + 0.0004]]
# The same shape roughly 200 m east.
FAR_LINE = [[LAT - 0.002, LON + 0.0032], [LAT + 0.002, LON + 0.0032]]


def test_primary_road_at_25m_with_five_routes_scores_full():
    data = {"elements": [_way("primary", "Amagerbrogade", NEAR_LINE)]
                        + [_bus(r, i) for i, r in enumerate(["1A", "2A ", "5C", "18", "33"])]}
    out = features.parse_buzz(data, LAT, LON)

    assert out["buzz_street"] == 1.0
    assert out["buzz_bus"] == 1.0
    assert out["buzz_score"] == 1.0
    assert out["bus_routes"] == 5, "trailing whitespace must not create a second route"
    assert out["major_street"] == "Amagerbrogade"
    assert out["buzz_detail"] == "Fronts Amagerbrogade (primary), 5 bus routes"


def test_quiet_tertiary_scores_partial_credit(monkeypatch):
    monkeypatch.setattr(config, "BUZZ_ARTERIAL_MIN_BUSES", 2)
    data = {"elements": [_way("tertiary", "Stille Vej", NEAR_LINE), _bus("6A")]}
    out = features.parse_buzz(data, LAT, LON)

    assert out["buzz_street"] == 0.4
    assert out["bus_routes"] == 1
    assert out["buzz_detail"] == "Fronts Stille Vej (tertiary), 1 bus route"


def test_busy_named_tertiary_counts_as_a_high_street(monkeypatch):
    """OSM tags Amagerbrogade tertiary; bus service is what makes it a high street."""
    monkeypatch.setattr(config, "BUZZ_ARTERIAL_MIN_BUSES", 2)
    data = {"elements": [_way("tertiary", "Torvegade", NEAR_LINE)]
                        + [_bus(r, i) for i, r in enumerate(["2A", "9A", "37"])]}
    out = features.parse_buzz(data, LAT, LON)
    assert out["buzz_street"] == 1.0


def test_major_road_between_40_and_60m_is_partial():
    mid_line = [[LAT - 0.002, LON + 0.00082], [LAT + 0.002, LON + 0.00082]]
    out = features.parse_buzz({"elements": [_way("secondary", "Midtvej", mid_line)]}, LAT, LON)
    assert 40.0 < out["major_street_dist_m"] <= 60.0
    assert out["buzz_street"] == 0.6
    assert out["buzz_detail"].startswith("Near Midtvej (secondary)")


def test_no_road_nearby_scores_zero_street():
    out = features.parse_buzz({"elements": [_way("primary", "Fjernvej", FAR_LINE)]}, LAT, LON)
    assert out["buzz_street"] == 0.0
    assert out["major_street"] is None
    assert out["buzz_detail"] == "Side street, no bus routes"


def test_empty_response_is_a_valid_zero():
    out = features.parse_buzz({"elements": []}, LAT, LON)
    assert out["buzz_score"] == 0.0
    assert out["bus_routes"] == 0


@pytest.mark.real_features
def test_overpass_failure_marks_features_pending(monkeypatch, temp_db):
    monkeypatch.setattr(features, "_overpass_buzz", lambda lat, lon: None)
    monkeypatch.setattr(features, "water_distance", lambda lat, lon: (500.0, "Havnen", "harbour"))
    monkeypatch.setattr(features.geo, "commute_to_office", lambda lat, lon: (18, {"mode": "walk"}))

    out = features.get_place_features(LAT, LON, store=temp_db)
    assert out["features_pending"] == 1
    assert out["buzz_score"] is None
    assert out["commute_min"] == 18

    # The pending state survives the cache round-trip, so the next scan retries.
    cached = features.get_place_features(LAT, LON, store=temp_db)
    assert cached["features_pending"] == 1


@pytest.mark.real_features
def test_features_are_cached_and_refreshable(monkeypatch, temp_db):
    calls = []

    def _buzz(lat, lon):
        calls.append(1)
        return {"elements": [_way("primary", "Testvej", NEAR_LINE), _bus("1A")]}

    monkeypatch.setattr(features, "_overpass_buzz", _buzz)
    monkeypatch.setattr(features, "water_distance", lambda lat, lon: (500.0, "Havnen", "harbour"))
    monkeypatch.setattr(features.geo, "commute_to_office", lambda lat, lon: (18, {"mode": "walk"}))

    features.get_place_features(LAT, LON, store=temp_db)
    features.get_place_features(LAT, LON, store=temp_db)
    assert len(calls) == 1, "second lookup must come from place_features"

    # A coordinate is queried at most once per process, so the refresh below
    # would otherwise be served from the cache. Re-arming clears that set.
    features.reset_overpass_state()
    features.get_place_features(LAT, LON, store=temp_db, refresh=True)
    assert len(calls) == 2


# --- The refresh rule (6 September 2026) -----------------------------------
# A refresh once wrote its failures over good rows: place_features went from 52
# rows with buzz to 4, and matches carrying features_pending from 13 to 44.

def _ok_buzz(name="Testvej"):
    """A stubbed _overpass_buzz returning a primary road with one bus route."""
    def _buzz(lat, lon):
        return {"elements": [_way("primary", name, NEAR_LINE), _bus("1A")]}
    return _buzz


def _offline(monkeypatch):
    monkeypatch.setattr(features, "water_distance", lambda lat, lon: (500.0, "Havnen", "harbour"))
    monkeypatch.setattr(features.geo, "commute_to_office", lambda lat, lon: (18, {"mode": "walk"}))


@pytest.mark.real_features
def test_a_failed_refresh_keeps_the_cached_buzz(monkeypatch, temp_db):
    _offline(monkeypatch)
    monkeypatch.setattr(features, "_overpass_buzz", _ok_buzz())
    seeded = features.get_place_features(LAT, LON, store=temp_db)
    assert seeded["buzz_score"] is not None

    # Without this the refresh would be served from the once-per-process set
    # and the failure path would never run.
    features.reset_overpass_state()
    monkeypatch.setattr(features, "_overpass_buzz", lambda lat, lon: None)
    out = features.get_place_features(LAT, LON, store=temp_db, refresh=True)

    assert out["buzz_score"] == seeded["buzz_score"]
    assert out["features_pending"] == 0
    row = temp_db.get_place_features(features.coord_key(LAT, LON))
    assert row["buzz_score"] == seeded["buzz_score"]
    assert row["provider"] == "overpass"
    features.reset_overpass_state()


@pytest.mark.real_features
def test_a_failed_refresh_of_a_pending_row_stays_pending(monkeypatch, temp_db):
    _offline(monkeypatch)
    monkeypatch.setattr(features, "_overpass_buzz", lambda lat, lon: None)
    seeded = features.get_place_features(LAT, LON, store=temp_db)
    assert seeded["features_pending"] == 1

    features.reset_overpass_state()
    out = features.get_place_features(LAT, LON, store=temp_db, refresh=True)

    assert out["buzz_score"] is None
    assert out["features_pending"] == 1
    assert temp_db.get_place_features(features.coord_key(LAT, LON))["buzz_score"] is None
    features.reset_overpass_state()


@pytest.mark.real_features
def test_a_successful_refresh_overwrites(monkeypatch, temp_db):
    _offline(monkeypatch)
    key = features.coord_key(LAT, LON)

    monkeypatch.setattr(features, "_overpass_buzz", _ok_buzz("Gammelvej"))
    features.get_place_features(LAT, LON, store=temp_db)
    before = temp_db.get_place_features(key)
    assert "Gammelvej" in before["buzz_detail"]

    features.reset_overpass_state()
    monkeypatch.setattr(features, "_overpass_buzz", _ok_buzz("Nyvej"))
    out = features.get_place_features(LAT, LON, store=temp_db, refresh=True)

    assert "Nyvej" in out["buzz_detail"]
    after = temp_db.get_place_features(key)
    assert "Nyvej" in after["buzz_detail"]
    assert after["computed_at"] >= before["computed_at"]
    features.reset_overpass_state()


@pytest.mark.real_features
def test_a_tripped_breaker_does_not_overwrite_cached_rows(monkeypatch, temp_db):
    """The exact shape of the 6 Sep 2026 damage: one failure, then all of them."""
    _offline(monkeypatch)
    coords = [(LAT, LON), (LAT + 0.01, LON), (LAT + 0.02, LON)]
    real_buzz = features._overpass_buzz
    try:
        monkeypatch.setattr(features, "_overpass_buzz", _ok_buzz())
        seeded = {}
        for lat, lon in coords:
            seeded[features.coord_key(lat, lon)] = \
                features.get_place_features(lat, lon, store=temp_db)["buzz_score"]
        assert all(v is not None for v in seeded.values())

        # The real _overpass_buzz with the breaker tripped: it returns None
        # without touching the network, which is what an outage looks like.
        features.reset_overpass_state()
        monkeypatch.setattr(features, "_overpass_buzz", real_buzz)
        features._overpass_disabled = True

        for lat, lon in coords:
            out = features.get_place_features(lat, lon, store=temp_db, refresh=True)
            key = features.coord_key(lat, lon)
            assert out["buzz_score"] == seeded[key]
            assert temp_db.get_place_features(key)["buzz_score"] == seeded[key]
    finally:
        features.reset_overpass_state()


@pytest.mark.real_features
def test_refresh_queries_each_coordinate_once_per_process(monkeypatch, temp_db):
    """A block of flats shares one coordinate across a dozen listings."""
    _offline(monkeypatch)
    calls = []

    def _buzz(lat, lon):
        calls.append(1)
        return {"elements": [_way("primary", "Testvej", NEAR_LINE), _bus("1A")]}

    monkeypatch.setattr(features, "_overpass_buzz", _buzz)
    features.reset_overpass_state()
    try:
        for _ in range(3):
            out = features.get_place_features(LAT, LON, store=temp_db, refresh=True)
            assert out["buzz_score"] is not None
        assert len(calls) == 1, "one Overpass call per coordinate per process"
    finally:
        features.reset_overpass_state()


def test_coord_key_rounds_to_five_decimals():
    assert features.coord_key(55.650981234, 12.509876543) == "55.65098,12.50988"


# --- Water -----------------------------------------------------------------

def test_water_distance_for_a_point_200m_from_a_coastline(monkeypatch):
    coast = [{"name": "Øresund", "kind": "coast",
              "points": [[LAT - 0.01, LON + 0.0032], [LAT + 0.01, LON + 0.0032]]}]
    monkeypatch.setattr(features, "load_water_edges", lambda force=False: coast)

    dist, name, kind = features.water_distance(LAT, LON)
    assert 180 <= dist <= 220
    assert name == "Øresund"
    assert kind == "coast"


def test_nearest_edge_wins_across_kinds(monkeypatch):
    edges = [
        {"name": "Øresund", "kind": "coast",
         "points": [[LAT - 0.01, LON + 0.0160], [LAT + 0.01, LON + 0.0160]]},
        {"name": "Søerne", "kind": "lake",
         "points": [[LAT - 0.01, LON + 0.0016], [LAT + 0.01, LON + 0.0016]]},
    ]
    monkeypatch.setattr(features, "load_water_edges", lambda force=False: edges)
    dist, name, kind = features.water_distance(LAT, LON)
    assert name == "Søerne"
    assert kind == "lake"
    assert dist < 150


def test_lake_weight_is_sixty_percent():
    assert features.water_weight("lake") == config.WATER_LAKE_WEIGHT
    for kind in ("coast", "harbour", "canal"):
        assert features.water_weight(kind) == 1.0


def test_missing_water_table_scores_zero(monkeypatch):
    monkeypatch.setattr(features, "load_water_edges", lambda force=False: [])
    assert features.water_distance(LAT, LON) == (None, None, None)


def test_real_water_table_is_present_and_usable():
    """The generated table must actually cover the harbour and the lakes."""
    assert features.water_available()

    # Islands Brygge waterfront: on the harbour edge.
    dist, name, kind = features.water_distance(55.6690, 12.5790)
    assert dist is not None and dist < 200
    assert kind in ("coast", "harbour", "canal")

    # A Vanlose side street is far from anything, and its nearest water is a lake.
    dist_inland, _n, kind_inland = features.water_distance(55.6870, 12.4880)
    assert dist_inland > 500
    assert kind_inland == "lake"


# --- Offline behaviour (spec acceptance criterion 8) -----------------------

UNROUTABLE = "http://10.255.255.1:9/api/interpreter"


@pytest.mark.real_features
def test_every_external_service_down_still_produces_a_stored_record(monkeypatch, temp_db):
    """Overpass and OSRM unreachable: no crash, features pending, commute still modelled."""
    import geo
    monkeypatch.setattr(config, "OVERPASS_URLS", [UNROUTABLE])
    monkeypatch.setattr(config, "OSRM_FOOT_URL", "http://10.255.255.1:9/routed-foot")
    monkeypatch.setattr(config, "ORS_API_KEY", "")
    monkeypatch.setattr(config, "GOOGLE_MAPS_API_KEY", "")
    monkeypatch.setattr(features, "_overpass_buzz", lambda lat, lon: None)
    # A dead socket, not a slow one: the point is the fallback, not the timeout.
    monkeypatch.setattr(geo.nethttp, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("network is down")))
    monkeypatch.setattr(geo, "_cache_mem", {})
    monkeypatch.setattr(geo, "_save_geo_cache", lambda: None)
    geo.reset_router_state()

    out = features.get_place_features(55.6690, 12.5790, store=temp_db)

    assert out["features_pending"] == 1
    assert out["buzz_score"] is None
    # The offline network model and the local water table still work.
    assert out["commute_min"] is not None
    assert out["water_dist_m"] is not None
    assert temp_db.get_place_features(features.coord_key(55.6690, 12.5790)) is not None
    geo.reset_router_state()


@pytest.mark.real_features
def test_osrm_circuit_breaker_stops_retrying_a_dead_router(monkeypatch):
    """Spec 15: an outage must not cost a socket timeout on every single route."""
    import geo
    calls = []

    def _dead(*a, **k):
        calls.append(1)
        raise OSError("connection timed out")

    monkeypatch.setattr(geo.nethttp, "urlopen", _dead)
    monkeypatch.setattr(geo, "_throttle", lambda key, interval: None)
    geo.reset_router_state()

    for i in range(10):
        assert geo._route_osrm(55.66, 12.57, 55.68, 12.58) is None
    assert len(calls) == geo._ROUTER_FAILURE_LIMIT, \
        "the router must be given up on, not retried for every route"
    geo.reset_router_state()


@pytest.mark.real_features
def test_walk_route_falls_back_to_the_calibrated_model(monkeypatch):
    import geo
    geo.reset_router_state()
    monkeypatch.setattr(geo, "_cache_mem", {})
    monkeypatch.setattr(geo, "_save_geo_cache", lambda: None)
    monkeypatch.setattr(geo, "_route_google", lambda *a: None)
    monkeypatch.setattr(geo, "_route_ors", lambda *a: None)
    monkeypatch.setattr(geo, "_route_osrm", lambda *a: None)

    metres, minutes, provider = geo.walk_route(55.6690, 12.5790, 55.6838, 12.5712)
    assert provider == "calibrated_model"
    straight = geo.hav(55.6690, 12.5790, 55.6838, 12.5712)
    assert metres == round(straight * config.WALK_ROUTING_FACTOR + config.WALK_DISTANCE_PENALTY_M)
    assert minutes >= 1


@pytest.mark.real_features
def test_overpass_buzz_recovers_via_ipv4_fallback_on_enetunreach(monkeypatch):
    """Plan 0.5: _overpass_buzz succeeds and leaves failures=0 when IPv6 fails with ENETUNREACH."""
    import errno
    import io
    import urllib.error
    import urllib.request
    from unittest.mock import MagicMock
    import nethttp

    features.reset_overpass_state()
    raw_json = b'{"elements": [{"type": "way", "id": 1, "tags": {"highway": "primary", "name": "Testgade"}}]}'

    def _fail_first_urlopen(url, *a, **k):
        raise urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable"))

    mock_opener = MagicMock()
    mock_opener.open.return_value = io.BytesIO(raw_json)

    monkeypatch.setattr(urllib.request, "urlopen", _fail_first_urlopen)
    monkeypatch.setattr(nethttp, "_v4_opener", lambda: mock_opener)
    monkeypatch.setattr(features.time, "sleep", lambda s: None)

    try:
        res = features._overpass_buzz(55.68, 12.57)
        assert res is not None
        assert len(res.get("elements", [])) == 1
        assert res["elements"][0]["tags"]["name"] == "Testgade"
        assert features._overpass_failures == 0
        assert features._overpass_disabled is False
        assert mock_opener.open.call_count == 1
    finally:
        features.reset_overpass_state()

