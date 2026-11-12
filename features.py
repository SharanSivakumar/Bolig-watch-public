"""Place features for BoligWatch v3.0: urban buzz and waterfront distance.

Urban buzz rewards what a renter actually wants: a flat that fronts a major
street with buses passing the window. Street frontage and bus traffic are
positives here, never noise penalties.

Waterfront distance measures how close the sea, the harbour, a canal or a lake
is. Lakes count at WATER_LAKE_WEIGHT; coast, harbour and canal at full weight.

Both are computed once per coordinate (rounded to 5 decimals) and cached in the
place_features table. Overpass failures are non-fatal: buzz becomes None and
the caller marks the listing features_pending so the next scan retries.

The refresh rule (6 September 2026): a refresh writes the row only when the buzz
lookup actually succeeded, or when there is no cached row yet. A failed lookup
against an existing row leaves that row untouched and returns the cached values.
Before this rule, one `--rescore --refresh-features` run during an Overpass
outage took place_features from 52 rows with buzz down to 4, and the matches
carrying features_pending from 13 up to 44. It also protects the commute figure,
which would otherwise be replaced by the calibrated fallback during an OSRM
outage. Each coordinate is re-queried at most once per process; see
reset_overpass_state.
"""
import json
import math
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import config
import geo
import nethttp

_USER_AGENT = "BoligWatch/3.12 (open-source rental research tool)"

# Equirectangular projection constants at the latitude of Greater Copenhagen.
_M_PER_DEG_LAT = 111320.0
_M_PER_DEG_LON = 111320.0 * math.cos(math.radians(55.7))

MAJOR_CLASSES = ("trunk", "primary", "secondary")
_last_overpass_at = 0.0

# Same discipline as the OSRM breaker in geo.py, and for the same reason: a
# blackholed Overpass costs the full socket timeout on EVERY new coordinate.
# With both endpoints down that is ~3 s refused plus a 40 s read timeout each
# time, which turned a 30-minute cold scan into a 2-hour one on 6 Sep 2026.
# Buzz is 15 of the 100 points and degrades to features_pending, so giving up is
# always safe: the scan completes and --rescore --refresh-features fills it in
# once Overpass is back.
_OVERPASS_FAILURE_LIMIT = 3
_overpass_failures = 0
_overpass_disabled = False

# Coordinates already queried against Overpass in this process. A refresh of a
# key in here is served from the cache instead of costing another rate-limited
# call: `--rescore --refresh-features` walks listings, and a block of flats such
# as Viola Norlovs Gade has 10 to 12 listings sharing one coordinate.
_refreshed_keys = set()


def reset_overpass_state():
    """Re-arm Overpass and forget which coordinates this process has queried.

    Clears the failure counter, the circuit breaker and _refreshed_keys, so a
    later refresh queries Overpass again. For tests and long-lived processes.
    """
    global _overpass_failures, _overpass_disabled
    _overpass_failures = 0
    _overpass_disabled = False
    _refreshed_keys.clear()


def clamp(x: float) -> float:
    return min(1.0, max(0.0, float(x)))


def coord_key(lat: float, lon: float) -> str:
    """Cache key for a coordinate, rounded to 5 decimals (about 1 m)."""
    return f"{lat:.5f},{lon:.5f}"


# ==============================================================================
# Geometry
# ==============================================================================
def _project(lat: float, lon: float) -> Tuple[float, float]:
    return lon * _M_PER_DEG_LON, lat * _M_PER_DEG_LAT


def point_to_segment_m(lat: float, lon: float,
                       lat1: float, lon1: float,
                       lat2: float, lon2: float) -> float:
    """Distance in metres from a point to a segment, equirectangular at 55.7 N."""
    px, py = _project(lat, lon)
    ax, ay = _project(lat1, lon1)
    bx, by = _project(lat2, lon2)
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def point_to_polyline_m(lat: float, lon: float, points: List[List[float]]) -> Optional[float]:
    """Minimum distance in metres from a point to a polyline [[lat, lon], ...]."""
    if not points:
        return None
    if len(points) == 1:
        return geo.hav(lat, lon, points[0][0], points[0][1])
    best = None
    for (la1, lo1), (la2, lo2) in zip(points, points[1:]):
        d = point_to_segment_m(lat, lon, la1, lo1, la2, lo2)
        if best is None or d < best:
            best = d
    return best


# ==============================================================================
# Waterfront distance
# ==============================================================================
_water_edges: Optional[List[Dict[str, Any]]] = None


def load_water_edges(force: bool = False) -> List[Dict[str, Any]]:
    """Read water_edges.json, adding a bounding box per polyline for prefiltering."""
    global _water_edges
    if _water_edges is not None and not force:
        return _water_edges
    _water_edges = []
    if not os.path.exists(config.WATER_EDGES_JSON):
        return _water_edges
    try:
        with open(config.WATER_EDGES_JSON, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        if config.VERBOSE:
            print(f"  [features] could not read water_edges.json: {e}")
        return _water_edges

    for e in raw.get("edges", []):
        pts = e.get("points") or []
        if len(pts) < 2:
            continue
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        e = dict(e)
        e["_bbox"] = (min(lats), min(lons), max(lats), max(lons))
        _water_edges.append(e)
    return _water_edges


def water_available() -> bool:
    return bool(load_water_edges())


def water_weight(kind: Optional[str]) -> float:
    """Lakes count at 60 percent; coast, harbour and canal at full weight."""
    return config.WATER_LAKE_WEIGHT if kind == "lake" else 1.0


def water_distance(lat: float, lon: float) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """Return (distance_m, name, kind) to the nearest water, or (None, None, None)."""
    edges = load_water_edges()
    if lat is None or lon is None or not edges:
        return None, None, None

    best_d, best = None, None
    for e in edges:
        bbox = e.get("_bbox")
        if bbox and best_d is not None:
            s_lat, s_lon, n_lat, n_lon = bbox
            # Cheap rejection: how far the point is outside this polyline's box.
            dlat = max(s_lat - lat, lat - n_lat, 0.0) * _M_PER_DEG_LAT
            dlon = max(s_lon - lon, lon - n_lon, 0.0) * _M_PER_DEG_LON
            if math.hypot(dlat, dlon) >= best_d:
                continue
        d = point_to_polyline_m(lat, lon, e["points"])
        if d is not None and (best_d is None or d < best_d):
            best_d, best = d, e

    if best is None:
        return None, None, None
    return round(best_d, 1), best.get("name"), best.get("kind")


# ==============================================================================
# Urban buzz
# ==============================================================================
BUZZ_QUERY = """[out:json][timeout:25];
(
  way(around:60,{lat},{lon})["highway"~"^(trunk|primary|secondary|tertiary)$"];
  node(around:200,{lat},{lon})["highway"="bus_stop"];
);
out geom;
(rel(bn)["route"="bus"];);
out tags;
"""


def _overpass_buzz(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    """One Overpass call per coordinate. Returns the raw JSON or None."""
    global _last_overpass_at, _overpass_failures, _overpass_disabled
    if _overpass_disabled:
        return None
    query = BUZZ_QUERY.format(lat=f"{lat:.6f}", lon=f"{lon:.6f}")

    for attempt, url in enumerate(config.OVERPASS_URLS):
        for retry in range(2):
            wait = 1.05 - (time.time() - _last_overpass_at)
            if wait > 0:
                time.sleep(wait)
            try:
                data = urllib.parse.urlencode({"data": query}).encode()
                req = urllib.request.Request(url, data=data)
                req.add_header("User-Agent", _USER_AGENT)
                with nethttp.urlopen(req, timeout=40) as r:
                    _last_overpass_at = time.time()
                    _overpass_failures = 0
                    return json.load(r)
            except urllib.error.HTTPError as e:
                _last_overpass_at = time.time()
                if e.code in (429, 504) and retry == 0:
                    time.sleep(10)
                    continue
                break
            except Exception as e:
                _last_overpass_at = time.time()
                if config.VERBOSE:
                    print(f"  [features] Overpass buzz error at {url}: {e}")
                break
    # Every endpoint failed for this coordinate. Count it once, not once per URL.
    _overpass_failures += 1
    if _overpass_failures >= _OVERPASS_FAILURE_LIMIT:
        _overpass_disabled = True
        print(f"  [features] Overpass unreachable after {_overpass_failures} "
              f"coordinates; skipping buzz for the rest of this run. Re-run "
              f"'run.py --rescore --refresh-features' once it is back.")
    elif config.VERBOSE:
        print(f"  [features] buzz unavailable for {lat:.5f},{lon:.5f}, marking pending")
    return None


def _plural_routes(n: int) -> str:
    if not n:
        return "no bus routes"
    return "1 bus route" if n == 1 else f"{n} bus routes"


def parse_buzz(data: Dict[str, Any], lat: float, lon: float) -> Dict[str, Any]:
    """Derive the buzz fields from an Overpass response. Pure, so tests can use fixtures."""
    ways, bus_refs = [], set()
    for e in (data or {}).get("elements", []):
        etype = e.get("type")
        tags = e.get("tags") or {}
        if etype == "way":
            hw = tags.get("highway")
            pts = [[p["lat"], p["lon"]] for p in (e.get("geometry") or [])]
            if hw in MAJOR_CLASSES + ("tertiary",) and pts:
                d = point_to_polyline_m(lat, lon, pts)
                if d is not None:
                    ways.append({"name": tags.get("name"), "class": hw, "dist_m": d})
        elif etype == "relation" and tags.get("route") == "bus":
            ref = (tags.get("ref") or "").strip()
            if ref:
                bus_refs.add(ref)

    ways.sort(key=lambda w: w["dist_m"])
    major = [w for w in ways if w["class"] in MAJOR_CLASSES]
    tertiary = [w for w in ways if w["class"] == "tertiary"]

    bus_routes = len(bus_refs)

    chosen, buzz_street, prefix = None, 0.0, "Side street"
    near_m = config.BUZZ_MAJOR_CLASS_M
    if major and major[0]["dist_m"] <= near_m:
        chosen, buzz_street, prefix = major[0], 1.0, "Fronts"
    elif major and major[0]["dist_m"] <= config.BUZZ_MAJOR_NEAR_M:
        chosen, buzz_street, prefix = major[0], 0.6, "Near"
    elif tertiary and tertiary[0]["dist_m"] <= near_m:
        # A named tertiary with real bus service is a city high street in all
        # but OSM's classification: Amagerbrogade and Torvegade are tagged
        # tertiary end to end. Credit those in full, quiet tertiaries partially.
        arterial = (config.BUZZ_ARTERIAL_MIN_BUSES
                    and bus_routes >= config.BUZZ_ARTERIAL_MIN_BUSES
                    and tertiary[0].get("name"))
        chosen, prefix = tertiary[0], "Fronts"
        buzz_street = 1.0 if arterial else 0.4

    buzz_bus = clamp(bus_routes / config.BUZZ_BUS_FULL_ROUTES)
    buzz_score = config.BUZZ_STREET_WEIGHT * buzz_street + config.BUZZ_BUS_WEIGHT * buzz_bus

    routes_txt = _plural_routes(bus_routes)
    if chosen:
        name = chosen["name"] or f"a {chosen['class']} road"
        detail = f"{prefix} {name} ({chosen['class']}), {routes_txt}"
    else:
        detail = f"Side street, {routes_txt}"

    return {
        "buzz_score": round(buzz_score, 3),
        "buzz_street": buzz_street,
        "buzz_bus": round(buzz_bus, 3),
        "buzz_detail": detail,
        "major_street": (chosen or {}).get("name"),
        "major_street_class": (chosen or {}).get("class"),
        "major_street_dist_m": round(chosen["dist_m"], 1) if chosen else None,
        "bus_routes": bus_routes,
    }


def urban_buzz(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    """Fetch and derive buzz for a coordinate. None means Overpass was unreachable."""
    data = _overpass_buzz(lat, lon)
    if data is None:
        return None
    return parse_buzz(data, lat, lon)


# ==============================================================================
# Cached place features
# ==============================================================================
FEATURE_FIELDS = (
    "commute_min", "commute_route", "buzz_score", "buzz_detail", "major_street",
    "major_street_dist_m", "bus_routes", "water_dist_m", "water_name", "water_kind",
    "computed_at", "provider",
)


# When true, get_place_features answers only from the place_features cache and
# makes no external request. run.refresh_local turns this on for the whole call
# tree so "no portal, no enrichment" is enforced at the source, not by hoping
# every caller remembers to pass a flag.
CACHE_ONLY = False


def get_place_features(lat: Optional[float], lon: Optional[float],
                       store: Any = None, refresh: bool = False) -> Dict[str, Any]:
    """Commute, buzz and water for a coordinate, cached in place_features.

    Never raises on external failure. When Overpass is unreachable the returned
    dict carries buzz_score None and features_pending 1.

    On refresh the row is written only when the buzz lookup succeeded, or when
    there is no cached row yet; see the refresh rule in the module docstring.
    """
    empty = {f: None for f in FEATURE_FIELDS}
    empty["features_pending"] = 0
    if lat is None or lon is None:
        return empty

    key = coord_key(lat, lon)
    cached = store.get_place_features(key) if store is not None else None
    if CACHE_ONLY:
        # Local processing reads what is stored and never fills a gap over the
        # network (spec 06 section 7.1). A coordinate with no cached row stays
        # pending rather than quietly costing an Overpass and an OSRM call.
        if cached:
            cached["features_pending"] = 1 if cached.get("buzz_score") is None else 0
            return cached
        out = dict(empty)
        out["features_pending"] = 1
        out["provider"] = "pending"
        return out
    if refresh and key in _refreshed_keys:
        # Already re-queried in this process. One Overpass call per coordinate,
        # not one per listing sharing it.
        refresh = False
    if cached and not refresh:
        cached["features_pending"] = 1 if cached.get("buzz_score") is None else 0
        return cached

    out = dict(empty)

    commute_min, route = geo.commute_to_office(lat, lon)
    out["commute_min"] = commute_min
    out["commute_route"] = json.dumps(route, ensure_ascii=False) if route else None

    buzz = urban_buzz(lat, lon)
    if buzz is None:
        out["features_pending"] = 1
    else:
        out["buzz_score"] = buzz["buzz_score"]
        out["buzz_detail"] = buzz["buzz_detail"]
        out["major_street"] = buzz["major_street"]
        out["major_street_dist_m"] = buzz["major_street_dist_m"]
        out["bus_routes"] = buzz["bus_routes"]

    w_dist, w_name, w_kind = water_distance(lat, lon)
    out["water_dist_m"], out["water_name"], out["water_kind"] = w_dist, w_name, w_kind

    out["provider"] = "overpass" if buzz is not None else "pending"

    if buzz is None and cached is not None:
        # Overpass failed. Keep the row we have: a refresh must never trade a
        # good buzz score for NULL (6 Sep 2026: 52 buzz rows became 4). This
        # also keeps a routed commute from being replaced by the fallback model
        # during an OSRM outage.
        cached["features_pending"] = 1 if cached.get("buzz_score") is None else 0
        if config.VERBOSE:
            print(f"  [features] refresh failed for {key}; keeping cached row")
        return cached

    if store is not None:
        store.save_place_features(key, out)
        _refreshed_keys.add(key)
    return out
