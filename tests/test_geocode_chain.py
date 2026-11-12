"""The v3.11 geocoding fallback chain (spec 10 section 3), network mocked.

Providers: Nominatim primary, Photon secondary, then a postcode centroid built
from the cache. The guard rails (Denmark, 40 km, postcode consistency) and the
"a cached failure is retried on every scan" contract are covered too.
"""
import pytest

import filters
import geo
import run
from conftest import with_detail_evidence


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    """Each test starts with an empty geo cache that is never written to disk."""
    monkeypatch.setattr(geo, "_cache_mem", {})
    monkeypatch.setattr(geo, "_save_geo_cache", lambda: None)
    monkeypatch.setattr(geo, "_throttle", lambda key, interval: None)
    geo.reset_geocode_health()


def _record_providers(monkeypatch, nominatim=None, photon=None):
    calls = []

    def _n(q, postcode):
        calls.append(("nominatim", q))
        return nominatim(q, postcode) if nominatim else None

    def _p(q, postcode):
        calls.append(("photon", q))
        return photon(q, postcode) if photon else None

    monkeypatch.setattr(geo, "_nominatim_lookup", _n)
    monkeypatch.setattr(geo, "_photon_lookup", _p)
    return calls


def _nominatim_json(lat, lon, postcode, type_="house", country="dk"):
    return [{
        "lat": str(lat), "lon": str(lon), "display_name": "Testvej 1, Danmark",
        "type": type_, "addresstype": type_,
        "address": {"postcode": postcode, "country_code": country},
    }]


# --- Step by step ----------------------------------------------------------

def test_step_1_nominatim_resolves_exact(monkeypatch):
    calls = _record_providers(
        monkeypatch,
        nominatim=lambda q, pc: (55.76779, 12.507966, "Gyldendalsvej 2", "exact"))
    lat, lon, _norm, precision = geo.geocode_chain(
        "k1", "Gyldendalsvej 2, 2800 Kongens Lyngby")
    assert (lat, lon) == (55.76779, 12.507966)
    assert precision == "exact"
    assert calls and calls[0][0] == "nominatim"


def test_step_2_strips_floor_and_door(monkeypatch):
    stripped = geo.strip_floor_door("Jorisvej 1a, 2. th, 2300 København S")
    assert "2. th" not in stripped

    def nominatim(q, pc):
        return (55.65, 12.60, q, "exact") if q == stripped else None

    _record_providers(monkeypatch, nominatim=nominatim)
    lat, lon, _n, precision = geo.geocode_chain(
        "k2", "Jorisvej 1a, 2. th, 2300 København S")
    assert (lat, lon) == (55.65, 12.60)
    assert precision == "exact"


def test_step_3_falls_back_to_photon(monkeypatch):
    calls = _record_providers(
        monkeypatch,
        nominatim=lambda q, pc: None,
        photon=lambda q, pc: (55.70, 12.55, q, "exact"))
    lat, lon, _n, precision = geo.geocode_chain("k3", "Ukendtvej 5, 2400 København NV")
    assert (lat, lon) == (55.70, 12.55)
    assert precision == "exact"
    assert any(kind == "photon" for kind, _q in calls)


def test_step_4_strips_a_trailing_house_letter(monkeypatch):
    assert geo.strip_house_letter("Gammel Vartov Vej 25C") == "Gammel Vartov Vej 25"

    def nominatim(q, pc):
        return (55.72, 12.57, q, "exact") if "25C" not in q and "Vartov" in q else None

    _record_providers(monkeypatch, nominatim=nominatim)
    lat, lon, _n, precision = geo.geocode_chain("k4", "Gammel Vartov Vej 25C, 2900 Hellerup")
    assert (lat, lon) == (55.72, 12.57)
    assert precision == "exact"


def test_step_5_street_plus_postcode_yields_street_precision(monkeypatch):
    def nominatim(q, pc):
        # Only a bare street name resolves; nothing with a house number does.
        return (55.66, 12.52, q, "street") if q.startswith("Molestien") else None

    _record_providers(monkeypatch, nominatim=nominatim)
    lat, lon, _n, precision = geo.geocode_chain(
        "k5", "Log ind 30 m2 lejlighed paa Molestien Molestien, 2450 Sydhavnen")
    assert (lat, lon) == (55.66, 12.52)
    assert precision == "street"


def test_step_6_postcode_centroid(monkeypatch):
    """Both providers fail, but neighbours in the cache answer the postcode."""
    _record_providers(monkeypatch, nominatim=lambda q, pc: None, photon=lambda q, pc: None)
    geo._cache_mem["seed|1"] = [55.6500, 12.5100, "Jorisvej 1a, 2300 København S", "exact"]
    geo._cache_mem["seed|2"] = [55.6520, 12.5300, "Jorisvej 5a, 2300 København S", "exact"]
    lat, lon, _n, precision = geo.geocode_chain("k6", "Nonsens 999, 2300 København S")
    assert precision == "postcode"
    assert abs(lat - 55.6510) < 0.001 and abs(lon - 12.5200) < 0.001


def test_total_failure_returns_nothing(monkeypatch):
    _record_providers(monkeypatch, nominatim=lambda q, pc: None, photon=lambda q, pc: None)
    assert geo.geocode_chain("k7", "Nonsens 999, 9999 Ingensteds") == (None, None, None, None)


def test_a_cached_failure_is_retried_next_scan(monkeypatch):
    """A negative cache entry is retried on every scan; a positive one is not."""
    calls = _record_providers(monkeypatch, nominatim=lambda q, pc: None, photon=lambda q, pc: None)
    geo.geocode_chain("k8", "Nonsens 999, 9999 Ingensteds")
    first = len(calls)
    geo.geocode_chain("k8", "Nonsens 999, 9999 Ingensteds")
    assert len(calls) > first

    geo._cache_mem["k9"] = [55.6, 12.5, "Somewhere", "exact"]
    before = len(calls)
    assert geo.geocode_chain("k9", "Somewhere")[3] == "exact"
    assert len(calls) == before


# --- Guard rails and provider parsing --------------------------------------

def test_accept_hit_rejects_a_wrong_postcode():
    # Near Copenhagen, correct country, but the postcode disagrees.
    assert geo._accept_hit(55.68, 12.57, "2200", "2300") is False
    assert geo._accept_hit(55.68, 12.57, "2300", "2300") is True
    # A provider that returns no postcode is not a mismatch.
    assert geo._accept_hit(55.68, 12.57, None, "2300") is True


def test_accept_hit_rejects_outside_the_radius():
    # Aalborg is ~220 km from Rådhuspladsen.
    assert geo._accept_hit(57.048, 9.9187, "9000", "9000") is False


def test_nominatim_parsing_and_postcode_guard(monkeypatch):
    payload = _nominatim_json(55.76779, 12.507966, "2800")
    monkeypatch.setattr(geo, "_http_json", lambda url, params, timeout=15: (payload, False))
    hit = geo._nominatim_lookup("Gyldendalsvej 2, 2800", "2800")
    assert hit and hit[0] == 55.76779 and hit[3] == "exact"
    # A deliberately wrong listing postcode is treated as a miss.
    assert geo._nominatim_lookup("Gyldendalsvej 2, 2800", "2200") is None


def test_nominatim_rejects_non_danish(monkeypatch):
    payload = _nominatim_json(52.52, 13.405, "10115", country="de")
    monkeypatch.setattr(geo, "_http_json", lambda url, params, timeout=15: (payload, False))
    assert geo._nominatim_lookup("Unter den Linden, Berlin", None) is None


def test_photon_parsing(monkeypatch):
    payload = {"features": [{
        "geometry": {"coordinates": [12.507966, 55.76779]},
        "properties": {"postcode": "2800", "countrycode": "dk", "type": "house",
                       "name": "Gyldendalsvej 2", "city": "Kongens Lyngby"},
    }]}
    monkeypatch.setattr(geo, "_http_json", lambda url, params, timeout=15: (payload, False))
    hit = geo._photon_lookup("Gyldendalsvej 2, 2800", "2800")
    assert hit and hit[0] == 55.76779 and hit[3] == "exact"


def test_systemic_http_failure_raises_the_outage_flag(monkeypatch):
    geo.reset_geocode_health()

    def boom(*args, **kwargs):
        raise geo.urllib.error.HTTPError("http://x", 410, "Gone", {}, None)

    monkeypatch.setattr(geo.nethttp, "urlopen", boom)
    for i in range(6):
        geo._nominatim_lookup(f"Nonsens {i}, 9999", None)
    assert geo.geocode_outage_suspected() is True


# --- Helper parsing --------------------------------------------------------

def test_street_candidates_from_a_noisy_scrape():
    cands = geo.street_candidates(
        "Log ind 30 m2 lejlighed paa Molestien Molestien, 2450 Sydhavnen")
    assert "Molestien" in cands


def test_postcode_extraction():
    assert geo.postcode_of("Jorisvej 1a, 2300 København S") == "2300"
    assert geo.postcode_of("no digits") is None


# --- Integration: a failure ranks, it does not hold ------------------------

def test_failure_ranks_with_location_unknown(mock_stations, monkeypatch):
    monkeypatch.setattr(geo, "geocode_chain",
                        lambda key, address: (None, None, None, None))

    rec = with_detail_evidence({
        "site": "test", "listing_id": "X1",
        "address": "Nonsens 999, 9999 Ingensteds",
        "listing_type": "lejlighed", "rooms": 1, "size_m2": 30.0,
        "rent_total": 8000, "deposit": 20000, "prepaid_rent": 0,
        "available_from": "2027-02-01", "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "description": "Eget køkken og eget badeværelse. Ubegrænset lejeperiode.",
    })
    run.geocode_record(rec)
    assert rec["lat"] is None

    res = filters.evaluate_listing(rec, mock_stations)
    assert res.rejected is False
    assert res.quarantined is False
    assert res.passed is True
    assert any(f.code == "location_unknown" for f in res.soft_flags)


def test_geocode_record_keeps_precision_and_postcode(monkeypatch):
    monkeypatch.setattr(geo, "geocode_chain",
                        lambda k, a: (55.65, 12.60, "Jorisvej 1a, 2300 København S", "street"))
    rec = {"site": "test", "listing_id": "X2", "address": "Jorisvej 1a, 2300 København S"}
    run.geocode_record(rec)
    assert rec["geo_precision"] == "street"
    assert rec["postcode"] == "2300"
    assert rec["normalized_address"] == "Jorisvej 1a, 2300 København S"


def test_strips_the_housing_association_apartment_suffix():
    """The '41, 6. Lejl. 4' form is stripped before any provider call."""
    assert geo.strip_floor_door("Stjernepladsen 41, 6. Lejl. 4, 9000 Aalborg") \
        == "Stjernepladsen 41, 9000 Aalborg"
    assert geo.strip_floor_door("Østre Havnegade 13, 3. lejl. 23, 9000 Aalborg") \
        == "Østre Havnegade 13, 9000 Aalborg"
    assert geo.strip_floor_door("Jorisvej 1a, 2. th, 2300 København S") \
        == "Jorisvej 1a, 2300 København S"
    assert geo.strip_floor_door("Jorisvej 1a, 2300 København S") \
        == "Jorisvej 1a, 2300 København S"


def test_collapses_a_house_number_range():
    assert geo.collapse_house_range("Poppelstykket 6-8, 2450 København") \
        == "Poppelstykket 6, 2450 København"
    assert geo.collapse_house_range("Jorisvej 1a, 2300 København") \
        == "Jorisvej 1a, 2300 København"


def test_house_range_resolves_exact(monkeypatch):
    """A range names a real building, so the reduced number resolves it."""
    def nominatim(q, pc):
        if q == "Poppelstykket 6, 2450 København":
            return (55.6538, 12.5127, q, "exact")
        return None

    _record_providers(monkeypatch, nominatim=nominatim)
    lat, lon, _n, precision = geo.geocode_chain("k10", "Poppelstykket 6-8, 2450 København")
    assert (lat, lon) == (55.6538, 12.5127)
    assert precision == "exact"
