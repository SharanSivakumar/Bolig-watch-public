"""Transit reference data, the Dijkstra ride-time model and door-to-door commute."""
import json

import pytest

import config
import generate_transit as gt
import geo


@pytest.fixture(scope="module")
def lines():
    with open(config.TRANSIT_LINES_JSON, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def commute_table():
    with open(config.STATION_COMMUTE_JSON, encoding="utf-8") as f:
        return json.load(f)


# --- Line file integrity ---------------------------------------------------

def test_every_wanted_line_is_present(lines):
    refs = {ln["ref"] for ln in lines["lines"]}
    missing = set(gt._WANTED_REFS) - refs
    assert not missing, f"line file is missing {sorted(missing)}"
    for ln in lines["lines"]:
        seq = ln["stations"]
        assert len(seq) >= 2
        # M3 is the Cityringen loop, so its terminus repeats its origin. Any
        # other repeat means the sequence is scrambled.
        body = seq[:-1] if seq[0] == seq[-1] else seq
        assert len(body) == len(set(body)), f"{ln['ref']} repeats a station"


def test_every_station_is_on_a_line_or_listed_as_an_orphan(lines):
    on_lines = set()
    for ln in lines["lines"]:
        on_lines.update(ln["stations"])
    orphan_names = {o["name"] for o in lines.get("orphans", [])}

    for path in (config.METRO_JSON, config.S_TOG_JSON):
        with open(path, encoding="utf-8") as f:
            for s in json.load(f)["stations"]:
                assert s["name"] in on_lines or s["name"] in orphan_names, \
                    f"{s['name']} is on no line and not listed as an orphan"


# Dense inner-city metro stops sit close together; the outer S-tog legs
# (Hillerod to Allerod is 6.8 km) are genuinely long. One threshold each.
MAX_HOP_M = {"Metro": gt.SANITY_MAX_HOP_M, "s-tog": 12000.0}


def test_consecutive_stations_are_geographically_plausible(lines):
    """Catches a scrambled member order: no line may jump across the region."""
    index = gt._build_station_index()
    coords = {}
    for stations in index.values():
        for s in stations:
            coords.setdefault(s["name"], (s["lat"], s["lon"]))
    for ln in lines["lines"]:
        limit = MAX_HOP_M[ln["network"]]
        for a, b in zip(ln["stations"], ln["stations"][1:]):
            if a in coords and b in coords:
                hop = geo.hav(*coords[a], *coords[b])
                assert hop <= limit, f"{ln['ref']}: {a} -> {b} is {hop:.0f} m"


# --- Ride-time model -------------------------------------------------------

def test_calibration_table_within_tolerance(commute_table):
    rows, failures = gt.check_calibration(commute_table["stations"])
    detail = "; ".join(f"{n}: expected {e}, got {g}" for n, e, g, _v, st in rows if st != "ok")
    assert not failures, f"calibration off for {failures} ({detail})"


def test_office_station_is_the_zero_point(commute_table):
    assert commute_table["stations"][config.OFFICE_NAME]["ride_min"] == 0


def test_transfer_penalty_applies_only_across_lines():
    """A shared trunk costs nothing extra; a genuine line change costs TRANSFER_MIN."""
    lines = [
        {"ref": "M3", "network": "Metro", "stations": ["Alpha", "Shared", "Nørreport"]},
        {"ref": "M4", "network": "Metro", "stations": ["Beta", "Shared", "Nørreport"]},
    ]
    adj = gt.build_graph(lines)
    commute = gt.dijkstra_to_office(adj)

    # Alpha rides M3 the whole way: two hops, no transfer.
    assert commute["Alpha"]["ride_min"] == pytest.approx(2 * config.METRO_HOP_MIN, abs=0.6)
    # Beta rides M4 to Shared then continues on the M3/M4 trunk: still no transfer.
    assert commute["Beta"]["ride_min"] == pytest.approx(2 * config.METRO_HOP_MIN, abs=0.6)


def test_cross_network_interchange_costs_a_transfer():
    lines = [
        {"ref": "M1", "network": "Metro", "stations": ["Vanløse", "Nørreport"]},
        {"ref": "C", "network": "s-tog", "stations": ["Outpost", "Vanløse"]},
    ]
    adj = gt.build_graph(lines)
    commute = gt.dijkstra_to_office(adj)
    # Outpost: S-tog hop to Vanløse, transfer to the metro, then one metro hop.
    expected = config.STOG_HOP_MIN + config.TRANSFER_MIN + config.METRO_HOP_MIN
    assert commute["Outpost"]["ride_min"] == pytest.approx(expected, abs=0.6)


# --- Door-to-door commute --------------------------------------------------

def _fixed_walk(monkeypatch, metres):
    monkeypatch.setattr(geo, "walk_route",
                        lambda *a: (metres, max(1, round(metres / config.WALK_SPEED_M_PER_MIN)),
                                    "test"))


def test_commute_picks_the_cheapest_walk_plus_ride(monkeypatch):
    """A 200 m walk to a slow station must lose to a 900 m walk to a fast one."""
    monkeypatch.setattr(geo, "load_metro", lambda: [
        {"name": "Slow", "lat": 55.6500, "lon": 12.6000, "network": "Metro"},
        {"name": "Fast", "lat": 55.6600, "lon": 12.6000, "network": "Metro"},
    ])
    monkeypatch.setattr(geo, "load_s_tog", lambda: [])
    monkeypatch.setattr(geo, "load_station_commute", lambda: {
        "Slow": {"ride_min": 30, "via": "M2"},
        "Fast": {"ride_min": 6, "via": "M2"},
    })
    walks = {"Slow": (200, 3), "Fast": (900, 11)}
    monkeypatch.setattr(geo, "walk_route",
                        lambda o_lat, o_lon, d_lat, d_lon:
                        walks["Slow" if d_lat == 55.6500 else "Fast"] + ("test",))

    minutes, route = geo.commute_to_office(55.6500, 12.6000)
    assert route["station"] == "Fast"
    # 11 walk + 2 wait + 6 ride + 2 egress
    assert minutes == 21


def test_direct_walk_wins_next_to_the_office(monkeypatch):
    """A listing 800 m from Nørreport walks; it does not ride one stop."""
    o_lat, o_lon = config.OFFICE_COORDS
    monkeypatch.setattr(geo, "load_metro", lambda: [
        {"name": "Nørreport", "lat": o_lat, "lon": o_lon, "network": "Metro"}])
    monkeypatch.setattr(geo, "load_s_tog", lambda: [])
    monkeypatch.setattr(geo, "load_station_commute",
                        lambda: {"Nørreport": {"ride_min": 0, "via": "walk"}})
    _fixed_walk(monkeypatch, 800)

    minutes, route = geo.commute_to_office(o_lat + 0.006, o_lon)
    assert route["mode"] == "walk"
    assert minutes == 10


def test_commute_is_none_without_reachable_stations(monkeypatch):
    monkeypatch.setattr(geo, "load_metro", lambda: [])
    monkeypatch.setattr(geo, "load_s_tog", lambda: [])
    monkeypatch.setattr(geo, "load_station_commute", lambda: {})
    assert geo.commute_to_office(56.5, 10.0) == (None, None)
