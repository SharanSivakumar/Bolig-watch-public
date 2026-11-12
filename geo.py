"""Geocoding (Nominatim/Photon), walking routes, transit ride times and commute.

v3.11 (spec 10 section 3) replaces the DAWA address step, which returned HTTP 410
Gone from 8 October 2026, with OpenStreetMap-based providers: Nominatim primary,
Photon secondary, then a postcode centroid built from the cache. The cache, the
`addr_key` scheme and the "a cached failure is retried on every scan" behaviour
are unchanged.
"""
import json
import math
import os
import re
import datetime
import urllib.parse
import urllib.error
import urllib.request

import config
import nethttp

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_PHOTON = "https://photon.komoot.io/api/"
_GOOGLE_DIST_MATRIX = "https://maps.googleapis.com/maps/api/distancematrix/json"
_GEO_USER_AGENT = "BoligWatch/3.12 (open-source rental research tool)"

# Systemic-outage bookkeeping. Every geocode attempt that fails with an HTTP
# error (as opposed to a clean "no result") is counted, so a scan whose provider
# has shut down can raise a portal-style incident after the fact. A successful
# lookup of any kind proves the service is alive and clears the counters.
_geo_stats = {"attempts": 0, "http_failures": 0}
_GEO_SYSTEMIC_MIN_CALLS = 5
_ROAD_PROFILE = config.ROAD_ROUTE_PROFILE
_road_metadata = {}
_road_failures = 0
_road_disabled = False


def load_stations():
    """Return the combined set of metro + S-tog stations for Greater Copenhagen."""
    metro = []
    if os.path.exists(config.METRO_JSON):
        with open(config.METRO_JSON, encoding="utf-8") as f:
            metro = json.load(f)["stations"]
    s_tog = load_s_tog()
    return metro + s_tog


def load_s_tog():
    """Return the S-tog (S-train) station table."""
    if not os.path.exists(config.S_TOG_JSON):
        return []
    with open(config.S_TOG_JSON, encoding="utf-8") as f:
        return json.load(f)["stations"]


def hav(lat1, lon1, lat2, lon2):
    """Haversine straight-line distance in meters between two coordinates."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def distance_to_radhuspladsen(lat, lon):
    """Haversine distance in meters to Radhuspladsen (Copenhagen City Hall)."""
    if lat is None or lon is None:
        return None
    c_lat, c_lon = config.RADHUSPLADSEN_COORDS
    return hav(lat, lon, c_lat, c_lon)


def distance_to_norreport(lat, lon):
    """Haversine straight-line distance in meters to Nørreport (the office).

    Retained as an audited diagnostic for the v3.4 road-route gate. Returns
    None when the coordinate is missing; this value must not determine
    eligibility.
    """
    if lat is None or lon is None:
        return None
    o_lat, o_lon = config.OFFICE_COORDS
    return hav(lat, lon, o_lat, o_lon)


def road_route_metadata(lat, lon):
    """Return the latest cached/attempted Nørreport road-route audit record."""
    return dict(_road_metadata.get(_road_coord_key(lat, lon), {}))


def _road_coord_key(lat, lon):
    # Unlike the walking feature cache, this gate is keyed by the exact source
    # coordinate supplied by the geocoder.  repr() is stable for parsed floats
    # and avoids silently broadening a hard boundary by rounding.
    return f"{lat!r},{lon!r}"


def _road_cache_key(lat, lon, provider):
    return f"road|{_road_coord_key(lat, lon)}|{provider}|{_ROAD_PROFILE}"


def _load_geo_cache():
    if os.path.exists(config.GEO_CACHE):
        try:
            with open(config.GEO_CACHE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


_cache_mem = _load_geo_cache()


def _save_geo_cache():
    try:
        with open(config.GEO_CACHE, "w", encoding="utf-8") as f:
            json.dump(_cache_mem, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def geocode(addr_key, address):
    """Resolve an address string to (lat, lon). addr_key is the cache lookup key.

    Kept as the historical entry point; holds no DAWA call since v3.11. Returns
    (lat, lon, normalized_address) or (None, None, None) on failure.
    """
    if addr_key in _cache_mem:
        v = _cache_mem[addr_key]
        if v:
            if len(v) >= 3:
                return (v[0], v[1], v[2])
            return (v[0], v[1], address)
        return (None, None, None)
    if not address:
        _cache_mem[addr_key] = None
        _save_geo_cache()
        return (None, None, None)
    lat, lon, label, _precision = geocode_chain(addr_key, address)
    return (lat, lon, label)


def get_walking_distance(origin_lat, origin_lon, dest_lat, dest_lon):
    """Walking route (metres, minutes, provider) via the v3.0 routing chain.

    Kept as the historical entry point; walk_route() holds the implementation:
    cache, Google Distance Matrix (only if a key is set), OpenRouteService,
    keyless FOSSGIS OSRM foot, then the calibrated urban model.
    """
    return walk_route(origin_lat, origin_lon, dest_lat, dest_lon)


def _route_google(o_lat, o_lon, d_lat, d_lon):
    """Google Distance Matrix walking distance in metres, or None."""
    if not config.GOOGLE_MAPS_API_KEY:
        return None
    try:
        params = {
            "origins": f"{o_lat},{o_lon}",
            "destinations": f"{d_lat},{d_lon}",
            "mode": "walking",
            "key": config.GOOGLE_MAPS_API_KEY,
        }
        url = f"{_GOOGLE_DIST_MATRIX}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", _GEO_USER_AGENT)
        with nethttp.urlopen(req, timeout=10) as r:
            res = json.load(r)
        if res.get("status") == "OK" and res.get("rows"):
            elem = res["rows"][0]["elements"][0]
            if elem.get("status") == "OK":
                return float(elem["distance"]["value"])
    except Exception as e:
        if config.VERBOSE:
            print(f"  [geo] Google Maps API error: {e}")
    return None


def _route_google_driving(o_lat, o_lon, d_lat, d_lon):
    """Google Distance Matrix driving distance in metres, or None."""
    if not config.GOOGLE_MAPS_API_KEY:
        return None
    try:
        params = {
            "origins": f"{o_lat},{o_lon}",
            "destinations": f"{d_lat},{d_lon}",
            "mode": "driving",
            "key": config.GOOGLE_MAPS_API_KEY,
        }
        url = f"{_GOOGLE_DIST_MATRIX}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", _GEO_USER_AGENT)
        with nethttp.urlopen(req, timeout=10) as r:
            res = json.load(r)
        if res.get("status") == "OK" and res.get("rows"):
            elem = res["rows"][0]["elements"][0]
            if elem.get("status") == "OK":
                return float(elem["distance"]["value"])
    except Exception as e:
        if config.VERBOSE:
            print(f"  [geo] Google driving API error: {e}")
    return None


def _route_ors_driving(o_lat, o_lon, d_lat, d_lon):
    """OpenRouteService driving-car distance in metres, or None."""
    global _ors_disabled
    if _ors_disabled or not config.ORS_API_KEY:
        return None
    try:
        _throttle("ors", _ORS_MIN_INTERVAL_S)
        body = json.dumps({"coordinates": [[o_lon, o_lat], [d_lon, d_lat]]}).encode()
        req = urllib.request.Request(
            "https://api.openrouteservice.org/v2/directions/driving-car", data=body)
        req.add_header("Authorization", config.ORS_API_KEY)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", _GEO_USER_AGENT)
        with nethttp.urlopen(req, timeout=_ROUTER_TIMEOUT_S) as r:
            data = json.load(r)
        return float(data["routes"][0]["summary"]["distance"])
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429):
            _ors_disabled = True
            if config.VERBOSE:
                print(f"  [geo] OpenRouteService driving disabled (HTTP {e.code})")
    except Exception as e:
        if config.VERBOSE:
            print(f"  [geo] OpenRouteService driving error: {e}")
    return None


def _route_osrm_driving(o_lat, o_lon, d_lat, d_lon):
    """OSRM driving distance in metres, or None, with the existing breaker."""
    global _road_failures, _road_disabled
    if _road_disabled:
        return None
    try:
        _throttle("osrm_driving", _OSRM_MIN_INTERVAL_S)
        url = (f"{config.OSRM_DRIVING_URL}/route/v1/driving/"
               f"{o_lon},{o_lat};{d_lon},{d_lat}?overview=false")
        req = urllib.request.Request(url)
        req.add_header("User-Agent", _GEO_USER_AGENT)
        with nethttp.urlopen(req, timeout=_ROUTER_TIMEOUT_S) as r:
            data = json.load(r)
        if data.get("code") != "Ok" or not data.get("routes"):
            _road_failures += 1
            return None
        _road_failures = 0
        return float(data["routes"][0]["distance"])
    except Exception as e:
        _road_failures += 1
        if _road_failures >= _ROUTER_FAILURE_LIMIT:
            _road_disabled = True
            print(f"  [geo] driving OSRM unreachable after {_road_failures} attempts; "
                  "quarantining road-distance checks for the rest of this run")
        elif config.VERBOSE:
            print(f"  [geo] driving OSRM error: {e}")
        return None


# Mirrors features.CACHE_ONLY: while set, every function that would otherwise
# call DAWA, OSRM, ORS or Google answers from cache or returns None.
CACHE_ONLY = False


def road_route_to_norreport(lat, lon):
    """Return driving road distance to Nørreport, or None when unverified.

    There is intentionally no geometric fallback.  A missing route is a hard
    centrality quarantine, because a permissive Haversine fallback would defeat
    the v3.4 gate. Successful results are cached per exact coordinate,
    provider, and driving profile.
    """
    if None in (lat, lon):
        return None

    coord = _road_coord_key(lat, lon)
    providers = (("google_driving", _route_google_driving),
                 ("openrouteservice_driving", _route_ors_driving),
                 ("osrm_driving", _route_osrm_driving))
    # Check every provider cache before making a new request. This matters when
    # the successful provider is the fallback OSRM route: a cached result must
    # not first probe Google and ORS again on every scan.
    for provider, fn in providers:
        key = _road_cache_key(lat, lon, provider)
        cached = _cache_mem.get(key)
        if isinstance(cached, dict) and cached.get("status") == "ok":
            distance = cached.get("distance_m")
            _road_metadata[coord] = cached
            return float(distance) if distance is not None else None
    if CACHE_ONLY:
        # No cached route and no permission to ask for one: the centrality gate
        # stays unverified, which quarantines rather than admitting the home.
        return None
    for provider, fn in providers:
        # Plot the normal route from the home to Nørreport. Driving distances
        # can be directional because of one-way streets.
        distance = fn(lat, lon, config.OFFICE_COORDS[0], config.OFFICE_COORDS[1])
        if distance is None:
            continue
        audit = {
            "distance_m": round(float(distance), 1),
            "provider": provider,
            "profile": _ROAD_PROFILE,
            "calculated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "status": "ok",
        }
        _cache_mem[key] = audit
        _road_metadata[coord] = audit
        _save_geo_cache()
        return audit["distance_m"]

    audit = {
        "distance_m": None,
        "provider": None,
        "profile": _ROAD_PROFILE,
        "calculated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "status": "failed",
    }
    _road_metadata[coord] = audit
    # Failure metadata is persisted for audit, but it is not a positive cache:
    # the next scan must retry after a provider outage recovers.
    _cache_mem[f"road|{coord}|attempt|{_ROAD_PROFILE}"] = audit
    _save_geo_cache()
    return None


def _nearest(lat, lon, stations, metro_only=False):
    """Shared nearest-station search. Returns (name, straight_dist_m, walk_dist_m, walk_min, network)."""
    if lat is None or lon is None or not stations:
        return None, None, None, None, None

    if metro_only:
        candidates = [s for s in stations if "metro" in str(s.get("network", "")).lower()]
    else:
        candidates = stations

    if not candidates:
        return None, None, None, None, None

    scored = []
    for s in candidates:
        d = hav(lat, lon, s["lat"], s["lon"])
        scored.append((d, s))
    scored.sort(key=lambda x: x[0])

    best = None
    for straight_d, s in scored[:3]:
        walk_m, walk_min, _ = get_walking_distance(lat, lon, s["lat"], s["lon"])
        if best is None or (walk_m is not None and walk_m < best[2]):
            best = (s["name"], straight_d, walk_m, walk_min, s.get("network", ""))

    return best if best else (None, None, None, None, None)


def nearest_metro(lat, lon, stations):
    """Return (nearest_metro_name, straight_dist_m, walk_dist_m, walk_min) for a coordinate.

    Restricts to metro-network stations even when S-tog stations are also present.
    """
    name, straight, walk_m, walk_min, _ = _nearest(lat, lon, stations, metro_only=True)
    return name, straight, walk_m, walk_min


def nearest_transit(lat, lon, stations):
    """Return (name, straight_dist_m, walk_dist_m, walk_min, network).

    Considers BOTH metro and S-tog stations (whatever is closest), used for the
    Cheap-Compromise proximity rule.
    """
    return _nearest(lat, lon, stations, metro_only=False)


def calculate_walking_minutes(distance_m):
    """Calculate average walking time in minutes (~80 meters/min or 4.8 km/h)."""
    if distance_m is None:
        return None
    return max(1, round(distance_m / config.WALK_SPEED_M_PER_MIN))


# ==============================================================================
# v3.0 — walking routing chain, transit graph lookup, commute to the office
# ==============================================================================
_OSRM_MIN_INTERVAL_S = 1.05     # FOSSGIS etiquette: at most one request per second
_ORS_MIN_INTERVAL_S = 1.6       # OpenRouteService free tier: 40 requests per minute
_last_call_at = {"osrm": 0.0, "ors": 0.0}
_ors_disabled = False           # set once the key is rejected or the quota is spent

# A blackholed router costs the full socket timeout on every single route. Six
# routes per property would turn an outage into an hours-long scan, so give up
# on a router after this many consecutive failures and let the calibrated model
# carry the rest of the run (spec 15: a scan with every service down must still
# complete).
_ROUTER_FAILURE_LIMIT = 3
_ROUTER_TIMEOUT_S = 20
_osrm_failures = 0
_osrm_disabled = False


def reset_router_state():
    """Re-arm the routing chain. For tests and long-lived processes."""
    global _ors_disabled, _osrm_failures, _osrm_disabled, _road_failures, _road_disabled
    _ors_disabled = False
    _osrm_failures = 0
    _osrm_disabled = False
    _road_failures = 0
    _road_disabled = False


def load_metro():
    """Return the Metro station table only."""
    if not os.path.exists(config.METRO_JSON):
        return []
    with open(config.METRO_JSON, encoding="utf-8") as f:
        return json.load(f)["stations"]


def _throttle(key, min_interval):
    import time
    wait = min_interval - (time.time() - _last_call_at.get(key, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call_at[key] = time.time()


def _route_ors(o_lat, o_lon, d_lat, d_lon):
    """OpenRouteService foot-walking. Returns metres or None."""
    global _ors_disabled
    if _ors_disabled or not config.ORS_API_KEY:
        return None
    try:
        _throttle("ors", _ORS_MIN_INTERVAL_S)
        body = json.dumps({"coordinates": [[o_lon, o_lat], [d_lon, d_lat]]}).encode()
        req = urllib.request.Request(
            "https://api.openrouteservice.org/v2/directions/foot-walking", data=body)
        req.add_header("Authorization", config.ORS_API_KEY)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", _GEO_USER_AGENT)
        with nethttp.urlopen(req, timeout=_ROUTER_TIMEOUT_S) as r:
            data = json.load(r)
        return float(data["routes"][0]["summary"]["distance"])
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429):
            _ors_disabled = True
            if config.VERBOSE:
                print(f"  [geo] OpenRouteService disabled for this run (HTTP {e.code})")
        return None
    except Exception as e:
        if config.VERBOSE:
            print(f"  [geo] OpenRouteService error: {e}")
        return None


def _route_osrm(o_lat, o_lon, d_lat, d_lon):
    """Keyless FOSSGIS OSRM foot router. Returns metres or None."""
    global _osrm_failures, _osrm_disabled
    if _osrm_disabled:
        return None
    try:
        _throttle("osrm", _OSRM_MIN_INTERVAL_S)
        url = (f"{config.OSRM_FOOT_URL}/route/v1/foot/"
               f"{o_lon},{o_lat};{d_lon},{d_lat}?overview=false")
        req = urllib.request.Request(url)
        req.add_header("User-Agent", _GEO_USER_AGENT)
        with nethttp.urlopen(req, timeout=_ROUTER_TIMEOUT_S) as r:
            data = json.load(r)
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        _osrm_failures = 0
        return float(data["routes"][0]["distance"])
    except Exception as e:
        _osrm_failures += 1
        if _osrm_failures >= _ROUTER_FAILURE_LIMIT:
            _osrm_disabled = True
            print(f"  [geo] OSRM unreachable after {_osrm_failures} attempts; "
                  f"using the calibrated model for the rest of this run")
        elif config.VERBOSE:
            print(f"  [geo] OSRM error: {e}")
        return None


def walk_route(o_lat, o_lon, d_lat, d_lon):
    """Walking route (metres, minutes, provider), cached under the walk| key.

    Chain: cache, OpenRouteService (if a key is set), keyless OSRM foot,
    then the calibrated straight * 1.35 + 150 model.
    """
    if None in (o_lat, o_lon, d_lat, d_lon):
        return None, None, None

    cache_key = f"walk|{o_lat:.5f},{o_lon:.5f}->{d_lat:.5f},{d_lon:.5f}"
    if cache_key in _cache_mem:
        v = _cache_mem[cache_key]
        if v and len(v) >= 2:
            return v[0], v[1], (v[2] if len(v) > 2 else "cache")

    metres, provider = None, None
    for fn, name in ((_route_google, "google_maps"),
                     (_route_ors, "openrouteservice"),
                     (_route_osrm, "osrm_foot")):
        metres = fn(o_lat, o_lon, d_lat, d_lon)
        if metres is not None:
            provider = name
            break

    if metres is None:
        straight = hav(o_lat, o_lon, d_lat, d_lon)
        metres = straight * config.WALK_ROUTING_FACTOR + config.WALK_DISTANCE_PENALTY_M
        provider = "calibrated_model"
        if config.VERBOSE:
            print(f"  [geo] routing fell back to calibrated_model for {cache_key}")

    walk_m = round(metres)
    walk_min = max(1, round(walk_m / config.WALK_SPEED_M_PER_MIN))
    _cache_mem[cache_key] = [walk_m, walk_min, provider]
    _save_geo_cache()
    return walk_m, walk_min, provider


# --- Station ride times to the office anchor -----------------------------------
_station_commute = None


def load_station_commute(force=False):
    """Return {station_name: {'ride_min': int, 'via': str, 'network': str}}."""
    global _station_commute
    if _station_commute is not None and not force:
        return _station_commute
    _station_commute = {}
    if os.path.exists(config.STATION_COMMUTE_JSON):
        try:
            with open(config.STATION_COMMUTE_JSON, encoding="utf-8") as f:
                _station_commute = json.load(f).get("stations", {})
        except Exception as e:
            if config.VERBOSE:
                print(f"  [geo] could not read station_commute.json: {e}")
    return _station_commute


def _wait_min(network):
    return (config.METRO_WAIT_MIN if str(network).lower().startswith("metro")
            else config.STOG_WAIT_MIN)


def _n_nearest(lat, lon, stations, n):
    scored = [(hav(lat, lon, s["lat"], s["lon"]), s) for s in stations]
    scored.sort(key=lambda x: x[0])
    return [s for _d, s in scored[:n]]


def commute_to_office(lat, lon, n_per_network=3):
    """Door-to-door minutes from a coordinate to the office anchor.

    Tries the three nearest metro and three nearest S-tog stations plus, when
    the office is within DIRECT_WALK_MAX_STRAIGHT_M straight-line, a direct
    walk. Returns (commute_min, route_dict) or (None, None).
    """
    if lat is None or lon is None:
        return None, None

    ride = load_station_commute()
    options = []

    for stations in (load_metro(), load_s_tog()):
        for s in _n_nearest(lat, lon, stations, n_per_network):
            entry = ride.get(s["name"])
            if entry is None:
                continue
            walk_m, walk_min, _p = walk_route(lat, lon, s["lat"], s["lon"])
            if walk_min is None:
                continue
            ride_min = entry.get("ride_min") or 0
            total = walk_min + _wait_min(s.get("network")) + ride_min + config.EGRESS_MIN
            options.append((total, {
                "mode": "transit",
                "station": s["name"],
                "network": s.get("network"),
                "walk_min": walk_min,
                "walk_m": walk_m,
                "ride_min": ride_min,
                "via": entry.get("via"),
            }))

    o_lat, o_lon = config.OFFICE_COORDS
    if hav(lat, lon, o_lat, o_lon) <= config.DIRECT_WALK_MAX_STRAIGHT_M:
        walk_m, walk_min, _p = walk_route(lat, lon, o_lat, o_lon)
        if walk_min is not None:
            options.append((float(walk_min), {
                "mode": "walk", "walk_min": walk_min, "walk_m": walk_m,
            }))

    if not options:
        return None, None
    options.sort(key=lambda x: x[0])
    best_total, best_route = options[0]
    return int(round(best_total)), best_route


def direct_walk_min_to_office(lat, lon):
    """Walking minutes straight to the office, or None if it is far away."""
    if lat is None or lon is None:
        return None
    o_lat, o_lon = config.OFFICE_COORDS
    if hav(lat, lon, o_lat, o_lon) > config.DIRECT_WALK_MAX_STRAIGHT_M:
        return None
    _m, walk_min, _p = walk_route(lat, lon, o_lat, o_lon)
    return walk_min


# ==============================================================================
# v3.11 — geocoding fallback chain (spec 10 section 3)
# ==============================================================================

# Danish floor and door suffixes: ", 2. th", "st. tv", "1. tv", "kl.", and the
# housing-association form "6. Lejl. 4" / "3. lejl. 23" that FindBolig emitted.
_FLOOR_DOOR_RE = re.compile(
    r"(?:,\s*|\s+)(?:kl|st|\d{1,2})\s*\.\s*"
    r"(?:(?:lejl|lejlighed|vaer|vær)\s*\.?\s*\d{1,3}|tv|th|mf|md|[a-zæøå]{1,4})?\.?"
    r"(?=\s*(?:,|$))",
    re.IGNORECASE)
_TRAILING_HOUSE_LETTER_RE = re.compile(r"\b(\d{1,4})\s*[A-Za-z]\b")
# "Poppelstykket 6-8" is a range of house numbers; DAWA wants one of them.
_HOUSE_RANGE_RE = re.compile(r"\b(\d{1,4})\s*[-/]\s*\d{1,4}\b")
_POSTCODE_RE = re.compile(r"\b(\d{4})\b")

# Portal page chrome that leaks into scraped address strings.
_CHROME_RE = re.compile(
    r"\b(?:søg bolig|udlej bolig gratis|in english|log ind|previous|next|"
    r"lejlighed til|værelser? for|m2 lejlighed|værelses lejlighed|"
    r"kr\.?\s*pr\.?\s*måned|husdyr er tilladt|på|i)\b", re.IGNORECASE)


def _record_http_failure():
    _geo_stats["attempts"] += 1
    _geo_stats["http_failures"] += 1


def _record_geocode_call():
    _geo_stats["attempts"] += 1


def geocode_health() -> dict:
    """Return a copy of the geocode attempt/failure counters for this process."""
    return dict(_geo_stats)


def reset_geocode_health():
    """Re-arm the systemic-outage counters. For tests and long-lived processes."""
    _geo_stats["attempts"] = 0
    _geo_stats["http_failures"] = 0


def geocode_outage_suspected() -> bool:
    """True when every geocode attempt this run failed with an HTTP error.

    A clean "no result" never counts; only a 4xx/5xx (a service that is gone,
    rate-limiting hard or unreachable with a status) does. Requires a minimum
    number of attempts so a single failure early in a scan does not alert.
    """
    attempts = _geo_stats["attempts"]
    return (attempts >= _GEO_SYSTEMIC_MIN_CALLS
            and _geo_stats["http_failures"] == attempts)


def _http_json(url, params, timeout=15):
    """One JSON GET. Returns (data, http_error_bool)."""
    try:
        req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}")
        req.add_header("User-Agent", _GEO_USER_AGENT)
        req.add_header("Accept", "application/json")
        with nethttp.urlopen(req, timeout=timeout) as r:
            return json.load(r), False
    except urllib.error.HTTPError as e:
        _record_http_failure()
        if config.VERBOSE:
            print(f"  [geo] {url} HTTP {e.code} for {params.get('q')!r}")
        return None, True
    except Exception as e:
        # DNS/timeout/connection errors are not an HTTP status; the provider may
        # simply be unreachable from this network. Not counted as systemic.
        _record_geocode_call()
        if config.VERBOSE:
            print(f"  [geo] {url} error for {params.get('q')!r}: {e}")
        return None, False


def _accept_hit(lat, lon, hit_postcode, listing_postcode):
    """Guard rails: in Denmark, near Copenhagen, and postcode-consistent.

    A hit is rejected when it is outside Denmark or more than 40 km from
    Rådhuspladsen, and when the listing gave a postcode and the provider returned
    a different one. A provider that returns no postcode is not a mismatch.
    """
    if lat is None or lon is None:
        return False
    if distance_to_radhuspladsen(lat, lon) > config.GEO_MAX_DISTANCE_M:
        return False
    if (listing_postcode and hit_postcode
            and str(hit_postcode).strip() != str(listing_postcode).strip()):
        return False
    return True


def _nominatim_lookup(query, listing_postcode):
    """Nominatim primary. Returns (lat, lon, label, precision) or None.

    verified 8 Oct 2026: "Gyldendalsvej 2, 2800 Kongens Lyngby" -> type house.
    Usage policy: descriptive User-Agent and at most one request per second.
    """
    _throttle("nominatim", 1.05)
    params = {
        "q": query, "format": "jsonv2", "countrycodes": "dk",
        "limit": "1", "addressdetails": "1", "accept-language": "da,en",
    }
    data, _http_err = _http_json(_NOMINATIM, params)
    if not data:
        return None
    try:
        item = data[0]
        lat, lon = float(item["lat"]), float(item["lon"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    address = item.get("address") or {}
    hit_postcode = address.get("postcode")
    country = str(address.get("country_code") or "dk").lower()
    if country and country != "dk":
        return None
    if not _accept_hit(lat, lon, hit_postcode, listing_postcode):
        return None
    label = item.get("display_name") or query
    type_ = str(item.get("type") or item.get("addresstype") or "").lower()
    precision = "exact" if type_ in ("house", "building", "residential") else "street"
    if type_ == "postcode":
        precision = "postcode"
    return lat, lon, label, precision


def _photon_lookup(query, listing_postcode):
    """Photon secondary, same OpenStreetMap data. Returns (lat, lon, label, precision)."""
    _throttle("photon", 1.05)
    params = {"q": query, "limit": "1"}
    data, _http_err = _http_json(_PHOTON, params)
    if not data:
        return None
    try:
        feature = (data.get("features") or [])[0]
        lon, lat = feature["geometry"]["coordinates"][:2]
        lat, lon = float(lat), float(lon)
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    props = feature.get("properties") or {}
    if str(props.get("countrycode") or "dk").lower() != "dk":
        return None
    hit_postcode = props.get("postcode")
    if not _accept_hit(lat, lon, hit_postcode, listing_postcode):
        return None
    label = props.get("name") or query
    city = props.get("city") or props.get("district") or ""
    label = ", ".join(part for part in (label, city) if part)
    type_ = str(props.get("type") or "").lower()
    precision = "exact" if type_ in ("house", "building") else "street"
    return lat, lon, label, precision


def _postcode_centroid(postcode):
    """Mean coordinate of cached exact/access hits in a postcode, or None.

    The tertiary rung: when both providers fail but the card gives a postcode,
    the cache almost always holds neighbours in the same postcode (2,869 entries
    as of 8 October 2026). Precision is deliberately reported as "postcode".
    """
    if not postcode:
        return None
    key = f"centroid|{postcode}"
    cached = _cache_mem.get(key)
    if cached:
        return cached[0], cached[1], cached[2] if len(cached) >= 3 else postcode
    lats, lons = [], []
    needle = f" {postcode}"
    for value in _cache_mem.values():
        if not isinstance(value, (list, tuple)) or len(value) < 3:
            continue
        label = value[2] if isinstance(value[2], str) else ""
        precision = value[3] if len(value) >= 4 else "exact"
        if precision not in ("exact", "access", "street"):
            continue
        if needle not in f" {label} " and postcode not in label:
            continue
        try:
            lats.append(float(value[0]))
            lons.append(float(value[1]))
        except (TypeError, ValueError):
            continue
    if not lats:
        return None
    lat = sum(lats) / len(lats)
    lon = sum(lons) / len(lons)
    _cache_mem[key] = [lat, lon, f"postcode {postcode}", "postcode"]
    _save_geo_cache()
    return lat, lon, f"postcode {postcode}"


def strip_floor_door(address):
    """Remove Danish floor and door suffixes from an address string."""
    return " ".join(_FLOOR_DOOR_RE.sub("", address or "").split()).strip(" ,")


def strip_house_letter(address):
    """Turn a trailing house letter into the bare number: 77C becomes 77."""
    return _TRAILING_HOUSE_LETTER_RE.sub(r"\1", address or "")


def collapse_house_range(address):
    """Reduce a house-number range to its first number: 6-8 becomes 6."""
    return _HOUSE_RANGE_RE.sub(r"\1", address or "")


def street_candidates(address):
    """Plausible street names at the tail of a noisy scraped address.

    Portal scrapes arrive as "Log ind 30 m2 lejlighed paa Molestien Molestien,
    2450 Sydhavnen": the street is the run of capitalised words just before the
    postcode, sometimes repeated. Longest candidate first.
    """
    if not address:
        return []
    head = address
    m = _POSTCODE_RE.search(address)
    if m:
        head = address[:m.start()]
    head = head.strip().strip(",").strip()
    head = _CHROME_RE.sub(" ", head)
    # Drop a trailing house number so we are left with the street name.
    head = re.sub(r"\s+\d+\s*[A-Za-zæøå]?\.?$", "", head).strip()

    words = [w for w in head.split() if w]
    tail = []
    for w in reversed(words):
        if not w[:1].isupper() or w.lower() in ("kr", "m2"):
            break
        tail.insert(0, w)
        if len(tail) >= 4:
            break
    if not tail:
        return []

    # Collapse an immediately repeated street name ("Molestien Molestien").
    for size in range(1, len(tail) // 2 + 1):
        if tail[-size:] == tail[-2 * size:-size]:
            tail = tail[-size:]
            break

    return [" ".join(tail[i:]) for i in range(len(tail))][:4]


def postcode_of(address):
    m = _POSTCODE_RE.search(address or "")
    return m.group(1) if m else None


def geocode_chain(addr_key, address):
    """Resolve an address to (lat, lon, normalized, precision).

    v3.11 (spec 10 section 3). Steps, each cached under the same addr_key:
      1 Nominatim, full address then floor/door-stripped, then house-letter and
        house-range reduced variants
      2 Photon, the same variants
      3 street name plus postcode only, precision "street"
      4 postcode centroid from the cache, precision "postcode"
    A total failure returns (None, None, None, None); the caller ranks the
    listing with the "Location unknown" badge rather than quarantining it.
    """
    v = _cache_mem.get(addr_key)
    if v:
        lat, lon = v[0], v[1]
        norm = v[2] if len(v) >= 3 else address
        precision = v[3] if len(v) >= 4 else "exact"
        return lat, lon, norm, precision
    # A cached failure is NOT final: an ungeocodable address is retried on every
    # scan, so only a positive hit short-circuits the chain.
    if CACHE_ONLY:
        # Retrying an ungeocodable address is a network call. Local processing
        # may not make it; `--refresh-local --enrich` is the action that may.
        return None, None, None, None

    if not address:
        _cache_mem[addr_key] = None
        _save_geo_cache()
        return None, None, None, None

    postcode = postcode_of(address)
    no_floor = strip_floor_door(address)
    variants = []
    for q in (address, no_floor, strip_house_letter(no_floor),
              collapse_house_range(no_floor)):
        q = (q or "").strip()
        if q and q not in variants:
            variants.append(q)

    seen_queries = set()

    def try_provider(fn, q):
        key = (fn.__name__, q)
        if not q or key in seen_queries:
            return None
        seen_queries.add(key)
        hit = fn(q, postcode)
        if hit:
            lat, lon, label, precision = hit
            _cache_mem[addr_key] = [lat, lon, label or q, precision]
            _save_geo_cache()
            return lat, lon, label or q, precision
        return None

    # Steps 1 and 2: the full-address variants against each provider in turn.
    for fn in (_nominatim_lookup, _photon_lookup):
        for q in variants:
            hit = try_provider(fn, q)
            if hit:
                return hit

    # Step 3: street name plus postcode only, precision "street".
    for cand in street_candidates(address):
        q = f"{cand} {postcode}" if postcode else cand
        hit = try_provider(_nominatim_lookup, q) or try_provider(_photon_lookup, q)
        if hit:
            lat, lon, label, _precision = hit
            _cache_mem[addr_key] = [lat, lon, label or q, "street"]
            _save_geo_cache()
            return lat, lon, label or q, "street"

    # Step 4: postcode centroid, precision "postcode".
    centroid = _postcode_centroid(postcode)
    if centroid:
        lat, lon, label = centroid
        _cache_mem[addr_key] = [lat, lon, label, "postcode"]
        _save_geo_cache()
        return lat, lon, label, "postcode"

    _cache_mem[addr_key] = None
    _save_geo_cache()
    return None, None, None, None
