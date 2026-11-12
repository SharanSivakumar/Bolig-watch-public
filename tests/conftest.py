"""Pytest fixtures and test setup for BoligWatch v3.0."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tests run against a fixed profile whose values the assertions were written
# for. It lives under tests/ so a user's own profile.json never changes a test.
os.environ.setdefault(
    "BOLIGWATCH_PROFILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "test_profile.json"))

import config
import features
import geo
from store import Store


def with_detail_evidence(rec, text=None, state="verified",
                         checked_at="2026-09-14T10:00:00", operator=True):
    """Stamp the detail-page evidence a stored listing carries after a fetch.

    A search card is discovery data, never proof that a tenancy is unrestricted
    (spec 06 section 3.1), so evaluation quarantines any listing whose
    authoritative detail evidence is missing. A test record stands for a listing
    whose detail page HAS been read, so it carries what a real detail fetch
    stores: the body text, where it came from, and when it was read.

    It also stands for an advert whose own canonical record carries a
    professional operator/project block, which is what the spec 07 section 4.4
    trust table needs in order to reach `eligible` without a store: section 4.2
    bullet 3 (source-scoped structured data naming a company advertiser) and
    section 4.3 route 2 (the same block naming the operator's project page). A
    test that needs the *unverified* path - the trust gate's `review` route -
    passes ``operator=False``.

    Explicit overrides win: a test that sets ``eligibility_evidence_state`` or
    passes ``eligibility_text=None`` is exercising the missing-evidence path and
    is left exactly as it was written, and a test that sets ``source_record``
    itself keeps its own block.
    """
    rec = dict(rec)
    rec.setdefault("eligibility_text",
                   text if text is not None
                   else " ".join(str(rec.get(k) or "") for k in ("title", "description")).strip())
    rec.setdefault("eligibility_source_url",
                   rec.get("url") or "https://example.test/listing")
    rec.setdefault("eligibility_evidence_state", state)
    rec.setdefault("eligibility_checked_at", checked_at)
    rec.setdefault("detail_state", "fetched")
    if operator:
        rec.setdefault("source_record", {
            "id": f"sb-{rec.get('listing_id') or 'x'}",
            "url": rec.get("url") or "https://example.test/listing",
            "listing_id": rec.get("listing_id"),
            "is_newbuild": True,
            "newbuild_details": {
                "newbuild_name": "Testgade Have",
                "company_name": "Test Ejendomme A/S",
                "company_link": "https://example.test/operator/testgade-have",
            },
        })
    return rec


def set_detail_text(rec, text):
    """Rewrite what a listing's detail page says.

    Tenancy verdicts read the authoritative detail evidence, never the card
    description (pitfall 10: a Lejebolig card names every school within 5 km).
    A test that changes what the advert says has to change the evidence, which
    is what a real detail fetch would have stored.
    """
    rec["description"] = text
    rec["eligibility_text"] = text
    return rec


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = Store(db_path=path)
    yield store
    try:
        store.conn.close()
        os.remove(path)
    except Exception:
        pass


TEST_COMPLEX_GROUPS = {
    "jorisvej-complex": {
        "name": "Jorisvej complex",
        "aliases": ["Jorisvej 1A", "Jorisvej 3", "Jorisvej 5",
                    "Jorisvej 7A", "Jorisvej 9A", "Jorisvej 13"],
        "street_only_aliases": ["Jorisvej"], "postcode": "2300",
    },
    "midgaard-complex": {
        "name": "Midgaard", "aliases": ["Else Alfelts Vej 42", "Else Alfelts Vej 44"],
        "street_only_aliases": [], "postcode": "2300",
    },
    "richsgaarden-complex": {
        "name": "Richsgården",
        "aliases": ["C.F. Richs Vej 105C", "C.F. Richs Vej 105D"],
        "street_only_aliases": [], "postcode": "2000",
    },
    "poppelstykket-complex": {
        "name": "Poppelstykket",
        "aliases": ["Poppelstykket 6", "Poppelstykket 8", "Poppelstykket 6-8"],
        "street_only_aliases": [], "postcode": "2450",
    },
    "blaekhus-frederiksberg-complex": {
        "name": "Blækhus Frederiksberg", "aliases": ["Finsensvej 15"],
        "street_only_aliases": [], "postcode": "2000",
    },
    "basestack-bryggen-complex": {
        "name": "BaseStack Bryggen",
        "aliases": [f"Njalsgade {n}" for n in range(151, 170, 2)] + ["Njalsgade 151-169"],
        "street_only_aliases": [], "postcode": "2300",
    },
    "viola-noerloevs-gade-complex": {
        "name": "Viola Nørløvs Gade complex",
        "aliases": ["Viola Nørløvs Gade", "Viola Nørløvs Gade 1",
                    "Viola Nørløvs Gade 3"],
        "street_only_aliases": [], "postcode": "2450",
    },
}


@pytest.fixture(autouse=True)
def test_complex_groups(monkeypatch):
    """Complex fixtures belong to the tests, not to a user profile."""
    monkeypatch.setattr(config, "COMPLEX_GROUPS", TEST_COMPLEX_GROUPS)


@pytest.fixture
def mock_stations():
    return [
        {"name": "København Syd", "lat": 55.6515, "lon": 12.5125, "network": "metro"},
        {"name": "Enghave Plads", "lat": 55.6677, "lon": 12.5458, "network": "metro"},
        {"name": "Rådhuspladsen", "lat": 55.6764, "lon": 12.5682, "network": "metro"},
        {"name": "Nørreport", "lat": 55.6833, "lon": 12.5717, "network": "metro"},
    ]


@pytest.fixture(autouse=True)
def place_features(monkeypatch, request):
    """Deterministic, offline place features.

    Autouse so no test can reach Overpass, OSRM or DAWA by accident. A test that
    cares about a specific commute, buzz or water value mutates the returned
    dict before calling evaluate_listing.
    """
    state = {
        "commute_min": 12,
        "commute_route": {"station": "Enghave Plads", "network": "Metro",
                          "walk_min": 4, "ride_min": 6, "via": "M3"},
        "buzz_score": 0.5,
        "buzz_detail": "Fronts Testgade (primary), 2 bus routes",
        "major_street": "Testgade",
        "major_street_dist_m": 20.0,
        "bus_routes": 2,
        "water_dist_m": 900.0,
        "water_name": "Inderhavnen",
        "water_kind": "harbour",
        "computed_at": None,
        "provider": "test-stub",
        "features_pending": 0,
        # The v3.4 centrality gate is a DRIVING ROAD ROUTE, not the straight
        # line. It is stubbed here because the real one calls Google, ORS and
        # then OSRM: without this the suite reaches the network for any
        # coordinate that is not already in geo_cache.json.
        "norreport_road_distance_m": 2000.0,
        "norreport_straight_distance_m": 2000.0,
    }

    def _stub(lat, lon, store=None, refresh=False):
        return dict(state)

    # A test marked real_features drives the real geo and features code and is
    # responsible for stubbing whatever would otherwise touch the network.
    if "real_features" in request.keywords:
        return state

    monkeypatch.setattr(features, "get_place_features", _stub)
    monkeypatch.setattr(geo, "road_route_to_norreport",
                        lambda lat, lon: state["norreport_road_distance_m"])
    monkeypatch.setattr(geo, "road_route_metadata",
                        lambda lat, lon: {"provider": "test-stub", "status": "ok",
                                          "profile": "driving",
                                          "calculated_at": "2026-09-14T10:00:00"})
    monkeypatch.setattr(geo, "distance_to_norreport",
                        lambda lat, lon: state["norreport_straight_distance_m"])
    monkeypatch.setattr(geo, "direct_walk_min_to_office", lambda lat, lon: None)
    monkeypatch.setattr(geo, "walk_route",
                        lambda o_lat, o_lon, d_lat, d_lon: (
                            geo.hav(o_lat, o_lon, d_lat, d_lon) * config.WALK_ROUTING_FACTOR
                            + config.WALK_DISTANCE_PENALTY_M,
                            None, "calibrated_model"))
    return state


import re
import notify
import run

# Emoji and pictographic ranges. Nothing output may contain these.
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]"
)


def _candidate(available_from="2027-01-20", **over):
    rec = {
        "site": "test",
        "listing_id": "L1",
        "title": "1-værelses",
        "address": "Jorisvej 1a, 2300 København",
        "listing_type": "lejlighed",
        "rooms": 1,
        "size_m2": 32.0,
        "rent_total": 8300,
        "deposit": 20000,
        "prepaid_rent": 0,
        "available_from": available_from,
        "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "description": "Eget køkken og eget badeværelse. Ubegrænset lejeperiode.",
        "lat": 55.65,
        "lon": 12.60,
    }
    rec.update(over)
    return with_detail_evidence(rec)


@pytest.fixture
def alert_env(temp_db, monkeypatch, mock_stations):
    sent = []
    monkeypatch.setattr(notify, "notify_match",
                        lambda payload, prefix="": sent.append((prefix, payload)) or 200)
    monkeypatch.setattr(notify, "send_text",
                        lambda text, parse_html=False: sent.append(text) or 200)
    monkeypatch.setattr(geo, "load_stations", lambda: mock_stations)
    monkeypatch.setattr(geo, "nearest_metro",
                        lambda lat, lon, st: ("Rådhuspladsen", 300.0, 400.0, 5))
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda lat, lon, st: ("Rådhuspladsen", 300.0, 400.0, 5, "metro"))
    monkeypatch.setattr(run.geo, "geocode_chain",
                        lambda k, a: (55.65, 12.60, "Jorisvej 1a, 2300 København", "exact"))
    monkeypatch.setattr(config, "BOT_CONFIRMED", True)
    return temp_db, sent, mock_stations
