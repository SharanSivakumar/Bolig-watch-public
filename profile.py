"""Optional JSON search profile.

BoligWatch ships with generic Copenhagen defaults. Everything a person is
likely to change lives in one JSON file instead of in Python:

    cp profile.example.json profile.json
    $EDITOR profile.json
    python run.py --dry-run

Resolution order:

1. ``--profile PATH`` on the command line (picked up before ``config`` is
   imported, see :func:`prescan_profile`)
2. the ``BOLIGWATCH_PROFILE`` environment variable
3. ``profile.json`` next to this file

When no profile exists the defaults in ``config.py`` are used unchanged.

Only the keys listed in :data:`TOP_LEVEL_KEYS` are accepted. An unknown key is
an error rather than a silently ignored typo. Every numeric value is range
checked. The profile is applied once, at config import time, so a profile
change requires restarting the viewer or the bot.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any

ENV_VAR = "BOLIGWATCH_PROFILE"
DEFAULT_FILENAME = "profile.json"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SUPPORTED_SITES = ("lejebolig", "boligportal")

TOP_LEVEL_KEYS = {
    "$schema", "version", "destination", "search", "areas", "fit", "scoring",
    "alerts", "web", "complexes",
}


class ProfileError(ValueError):
    """A profile could not be loaded or failed validation."""


# ==============================================================================
# Discovery and loading
# ==============================================================================
def prescan_profile(argv=None) -> None:
    """Set the profile environment variable from ``--profile`` before import.

    ``config`` is imported at module import time, so a CLI flag has to be read
    from ``sys.argv`` before that import happens. Call this at the top of every
    entry point, before ``import config``.
    """
    if argv is None:
        import sys
        argv = sys.argv[1:]
    for index, arg in enumerate(argv):
        if arg == "--profile" and index + 1 < len(argv):
            _set_env(argv[index + 1])
        elif arg.startswith("--profile="):
            _set_env(arg.split("=", 1)[1])


def _set_env(value: str) -> None:
    if value:
        os.environ[ENV_VAR] = os.path.abspath(os.path.expanduser(value))


def resolve_path(explicit: str | None = None) -> str | None:
    """Return the profile path to use, or ``None`` when no profile exists."""
    requested = explicit or os.environ.get(ENV_VAR)
    if requested:
        path = os.path.abspath(os.path.expanduser(requested))
        if not os.path.isfile(path):
            raise ProfileError(f"profile not found: {path}")
        return path

    default = os.path.join(BASE_DIR, DEFAULT_FILENAME)
    return default if os.path.isfile(default) else None


def load(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ProfileError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{path}: the top level must be a JSON object")
    unknown = {key for key in data if not str(key).startswith("_")} - TOP_LEVEL_KEYS
    if unknown:
        raise ProfileError(
            f"{path}: unknown top-level key(s): {', '.join(sorted(unknown))}")
    return data


# ==============================================================================
# Validation helpers
# ==============================================================================
def _where(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _unknown(where: str, data: dict[str, Any], allowed: set[str]) -> None:
    # Keys beginning with "_" are comments for humans and are ignored.
    unknown = {key for key in data if not str(key).startswith("_")} - allowed
    if unknown:
        prefix = f"{where}." if where else ""
        raise ProfileError(
            f"unknown profile key(s): {', '.join(prefix + k for k in sorted(unknown))}")


def _number(value: Any, key: str, *, minimum: float | None = None,
            maximum: float | None = None, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{key} must be a number")
    if integer and int(value) != value:
        raise ProfileError(f"{key} must be an integer")
    number = int(value) if integer else float(value)
    if minimum is not None and number < minimum:
        raise ProfileError(f"{key} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise ProfileError(f"{key} must be at most {maximum}")
    return number


def _boolean(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise ProfileError(f"{key} must be true or false")
    return value


def _string(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{key} must be a non-empty string")
    return value.strip()


def _list(value: Any, key: str) -> list:
    if not isinstance(value, list):
        raise ProfileError(f"{key} must be a list")
    return value


def _dict(value: Any, key: str) -> dict:
    if not isinstance(value, dict):
        raise ProfileError(f"{key} must be an object")
    return value


def _iso_date(value: Any, key: str) -> str:
    text = _string(value, key)
    try:
        _dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ProfileError(f"{key} must be an ISO date (YYYY-MM-DD), got {text!r}") from exc
    return text


def _section(data: dict[str, Any], key: str, allowed: set[str]) -> dict[str, Any] | None:
    if key not in data:
        return None
    section = _dict(data[key], key)
    _unknown(key, section, allowed)
    return section


# ==============================================================================
# Apply
# ==============================================================================
def apply(namespace: dict[str, Any]) -> str | None:
    """Apply the resolved profile to a config module namespace.

    Returns the profile path that was used, or ``None`` when defaults apply.
    """
    path = resolve_path()
    if path is None:
        return None
    data = load(path)
    _apply_destination(data, namespace)
    _apply_search(data, namespace)
    _apply_areas(data, namespace)
    _apply_fit(data, namespace)
    _apply_scoring(data, namespace)
    _apply_alerts(data, namespace)
    _apply_web(data, namespace)
    _apply_complexes(data, namespace)
    _recompute(namespace)
    namespace["PROFILE_PATH"] = path
    return path


def _apply_destination(data: dict[str, Any], ns: dict[str, Any]) -> None:
    section = _section(data, "destination", {"name", "lat", "lon"})
    if not section:
        return
    if "name" in section:
        ns["OFFICE_NAME"] = _string(section["name"], "destination.name")
    lat = section.get("lat", ns["OFFICE_COORDS"][0])
    lon = section.get("lon", ns["OFFICE_COORDS"][1])
    ns["OFFICE_COORDS"] = (
        _number(lat, "destination.lat", minimum=-90, maximum=90),
        _number(lon, "destination.lon", minimum=-180, maximum=180),
    )


def _apply_search(data: dict[str, Any], ns: dict[str, Any]) -> None:
    section = _section(data, "search", {
        "sites", "target_move_date", "preferred_window_start",
        "preferred_window_end", "date_tolerance_days", "rent_cap_dkk",
        "affordable_rent_dkk", "move_in_cost_max_dkk",
        "discovery_rent_max_dkk", "rooms_min", "rooms_max",
        "max_commute_min", "max_distance_km", "metro_walk_max_m",
        "responsible_pick",
    })
    if not section:
        return

    if "sites" in section:
        sites = _list(section["sites"], "search.sites")
        if not sites:
            raise ProfileError("search.sites must name at least one portal")
        for site in sites:
            if site not in SUPPORTED_SITES:
                raise ProfileError(
                    f"search.sites: unsupported portal {site!r}; "
                    f"choose from {', '.join(SUPPORTED_SITES)}")
        ns["SITES"] = list(dict.fromkeys(sites))

    target_supplied = "target_move_date" in section
    if target_supplied:
        ns["TARGET_MOVE_DATE"] = _iso_date(
            section["target_move_date"], "search.target_move_date")
    if "preferred_window_start" in section:
        ns["PREFERRED_WINDOW_START"] = _iso_date(
            section["preferred_window_start"], "search.preferred_window_start")
    if "preferred_window_end" in section:
        ns["PREFERRED_WINDOW_END"] = _iso_date(
            section["preferred_window_end"], "search.preferred_window_end")
    if (target_supplied and "preferred_window_start" not in section
            and "preferred_window_end" not in section):
        # A profile that moves the target date gets a window around it unless
        # it also supplied the window explicitly.
        target = _dt.date.fromisoformat(ns["TARGET_MOVE_DATE"])
        ns["PREFERRED_WINDOW_START"] = (
            target - _dt.timedelta(days=15)).isoformat()
        ns["PREFERRED_WINDOW_END"] = (
            target + _dt.timedelta(days=15)).isoformat()
    if (_dt.date.fromisoformat(ns["PREFERRED_WINDOW_START"])
            > _dt.date.fromisoformat(ns["PREFERRED_WINDOW_END"])):
        raise ProfileError("search.preferred_window_start must not be after preferred_window_end")
    if "date_tolerance_days" in section:
        ns["DATE_TOLERANCE_DAYS"] = _number(
            section["date_tolerance_days"], "search.date_tolerance_days",
            minimum=0, maximum=3650)

    if "rent_cap_dkk" in section:
        cap = _number(section["rent_cap_dkk"], "search.rent_cap_dkk",
                      minimum=1, maximum=1_000_000, integer=True)
        ns["RENT_CAP_MAX"] = cap
    if "affordable_rent_dkk" in section:
        affordable = _number(section["affordable_rent_dkk"],
                             "search.affordable_rent_dkk",
                             minimum=0, maximum=1_000_000)
        if affordable >= ns["RENT_CAP_MAX"]:
            raise ProfileError(
                "search.affordable_rent_dkk must be below search.rent_cap_dkk")
        ns["AFFORDABILITY_FULL_RENT"] = affordable
    if "move_in_cost_max_dkk" in section:
        ns["MOVE_IN_COST_MAX"] = _number(
            section["move_in_cost_max_dkk"], "search.move_in_cost_max_dkk",
            minimum=0, maximum=10_000_000, integer=True)
    if "discovery_rent_max_dkk" in section:
        discovery = _number(section["discovery_rent_max_dkk"],
                            "search.discovery_rent_max_dkk",
                            minimum=1, maximum=1_000_000, integer=True)
        if discovery < ns["RENT_CAP_MAX"]:
            raise ProfileError(
                "search.discovery_rent_max_dkk must be at least search.rent_cap_dkk")
        ns["DISCOVERY_RENT_MAX"] = discovery

    if "rooms_min" in section:
        ns["ROOMS_MIN"] = _number(section["rooms_min"], "search.rooms_min",
                                  minimum=1, maximum=10, integer=True)
    if "rooms_max" in section:
        ns["ROOMS_MAX"] = _number(section["rooms_max"], "search.rooms_max",
                                  minimum=1, maximum=10, integer=True)
    if ns["ROOMS_MIN"] > ns["ROOMS_MAX"]:
        raise ProfileError("search.rooms_min must not exceed search.rooms_max")
    if "max_commute_min" in section:
        commute = _number(section["max_commute_min"], "search.max_commute_min",
                          minimum=1, maximum=240, integer=True)
        ns["COMMUTE_MAX_MIN"] = commute
        ns["TIER_C_MAX_MIN"] = commute
        ns["SCORE_COMMUTE_ZERO_MIN"] = min(ns["SCORE_COMMUTE_ZERO_MIN"], commute)
    if "max_distance_km" in section:
        metres = _number(section["max_distance_km"], "search.max_distance_km",
                         minimum=0.5, maximum=200) * 1000.0
        ns["NORREPORT_RADIUS_MAX_M"] = metres
        ns["NORREPORT_ROAD_DISTANCE_MAX_M"] = metres
    if "metro_walk_max_m" in section:
        walk = _number(section["metro_walk_max_m"], "search.metro_walk_max_m",
                       minimum=0, maximum=10_000)
        ns["NORMAL_METRO_WALK_MAX_M"] = walk
        ns["TRANSIT_METRO_ZERO_M"] = walk

    rp = section.get("responsible_pick")
    if rp is not None:
        rp = _dict(rp, "search.responsible_pick")
        _unknown("search.responsible_pick", rp, {
            "enabled", "s_train_walk_max_m", "rent_max_dkk"})
        if "enabled" in rp:
            if not _boolean(rp["enabled"], "search.responsible_pick.enabled"):
                ns["RP_S_TRAIN_MAX_M"] = 0.0
        if "s_train_walk_max_m" in rp:
            ns["RP_S_TRAIN_MAX_M"] = _number(
                rp["s_train_walk_max_m"],
                "search.responsible_pick.s_train_walk_max_m",
                minimum=0, maximum=10_000)
        if "rent_max_dkk" in rp:
            ns["RP_RENT_MAX"] = _number(
                rp["rent_max_dkk"], "search.responsible_pick.rent_max_dkk",
                minimum=0, maximum=1_000_000)


def _apply_areas(data: dict[str, Any], ns: dict[str, Any]) -> None:
    section = _section(data, "areas", {"lejebolig", "bbox", "boligportal_max_rooms"})
    if not section:
        return
    if "lejebolig" in section:
        areas = _list(section["lejebolig"], "areas.lejebolig")
        if not areas:
            raise ProfileError("areas.lejebolig must name at least one area")
        ns["LEJEBOLIG_AREAS"] = [_string(a, "areas.lejebolig[]") for a in areas]
    if "bbox" in section:
        bbox = _list(section["bbox"], "areas.bbox")
        if len(bbox) != 4:
            raise ProfileError("areas.bbox must be [south, west, north, east]")
        south, west, north, east = (
            _number(v, "areas.bbox[]", minimum=-180, maximum=180) for v in bbox)
        if south >= north or west >= east:
            raise ProfileError("areas.bbox must be ordered south < north and west < east")
        ns["GREATER_CPH_BBOX"] = (south, west, north, east)
    if "boligportal_max_rooms" in section:
        ns["BP_MAX_ROOMS"] = _number(
            section["boligportal_max_rooms"], "areas.boligportal_max_rooms",
            minimum=1, maximum=10, integer=True)


def _apply_fit(data: dict[str, Any], ns: dict[str, Any]) -> None:
    section = _section(data, "fit", {
        "station_ratings", "default_rating", "never_postcodes", "never_zones"})
    if not section:
        return
    if "station_ratings" in section:
        ratings = _dict(section["station_ratings"], "fit.station_ratings")
        cleaned: dict[str, float] = {}
        for name, value in ratings.items():
            if str(name).startswith("_"):
                continue
            cleaned[str(name)] = _number(
                value, f"fit.station_ratings.{name}", minimum=0.0, maximum=1.0)
        ns["AREA_STATION_RATINGS"] = cleaned
    if "default_rating" in section:
        ns["AREA_DEFAULT_RATING"] = _number(
            section["default_rating"], "fit.default_rating",
            minimum=0.0, maximum=1.0)
    if "never_postcodes" in section:
        postcodes = _dict(section["never_postcodes"], "fit.never_postcodes")
        cleaned_codes: dict[str, str] = {}
        for code, label in postcodes.items():
            if str(code).startswith("_"):
                continue
            code = str(code)
            if not (len(code) == 4 and code.isdigit()):
                raise ProfileError(
                    f"fit.never_postcodes: {code!r} is not a four-digit postcode")
            cleaned_codes[code] = _string(
                label, f"fit.never_postcodes.{code}")
        ns["NEVER_POSTCODES"] = cleaned_codes
    if "never_zones" in section:
        zones = []
        for index, entry in enumerate(_list(section["never_zones"], "fit.never_zones")):
            where = f"fit.never_zones[{index}]"
            entry = _dict(entry, where)
            _unknown(where, entry, {"name", "lat", "lon", "radius_m"})
            zones.append((
                _string(entry.get("name"), f"{where}.name"),
                _number(entry.get("lat"), f"{where}.lat", minimum=-90, maximum=90),
                _number(entry.get("lon"), f"{where}.lon", minimum=-180, maximum=180),
                _number(entry.get("radius_m", 500), f"{where}.radius_m",
                        minimum=1, maximum=50_000),
            ))
        ns["NEVER_ZONES"] = tuple(zones)


def _apply_scoring(data: dict[str, Any], ns: dict[str, Any]) -> None:
    section = _section(data, "scoring", {
        "affordability", "transit", "commute", "water", "centre", "buzz",
        "area", "floor", "affordable_rent_dkk", "commute_full_min",
        "commute_zero_min", "transit_full_m", "transit_mid_m", "transit_zero_m",
        "centre_full_m", "centre_zero_m", "water_full_m", "water_zero_m",
        "floor_unknown_pts",
    })
    if not section:
        return
    weights = {
        "affordability": "SCORE_AFFORDABILITY_PTS",
        "transit": "SCORE_TRANSIT_PTS",
        "commute": "SCORE_COMMUTE_PTS",
        "water": "SCORE_WATER_PTS",
        "centre": "SCORE_CENTRE_PTS",
        "buzz": "SCORE_BUZZ_PTS",
        "area": "SCORE_AREA_PTS",
        "floor": "SCORE_FLOOR_PTS",
    }
    for key, target in weights.items():
        if key in section:
            ns[target] = _number(section[key], f"scoring.{key}",
                                 minimum=0, maximum=100)
    total = sum(ns[target] for target in weights.values())
    if abs(total - 100.0) > 0.001:
        raise ProfileError(
            f"scoring weights must add up to 100, got {total:g}")

    if "affordable_rent_dkk" in section:
        affordable = _number(section["affordable_rent_dkk"],
                             "scoring.affordable_rent_dkk",
                             minimum=0, maximum=1_000_000)
        if affordable >= ns["RENT_CAP_MAX"]:
            raise ProfileError("scoring.affordable_rent_dkk must be below the rent cap")
        ns["AFFORDABILITY_FULL_RENT"] = affordable
    if "commute_full_min" in section:
        ns["SCORE_COMMUTE_FULL_MIN"] = _number(
            section["commute_full_min"], "scoring.commute_full_min", minimum=0)
    if "commute_zero_min" in section:
        ns["SCORE_COMMUTE_ZERO_MIN"] = _number(
            section["commute_zero_min"], "scoring.commute_zero_min", minimum=0)
    if "transit_full_m" in section:
        ns["TRANSIT_METRO_FULL_M"] = _number(
            section["transit_full_m"], "scoring.transit_full_m", minimum=0)
    if "transit_mid_m" in section:
        ns["TRANSIT_METRO_24_M"] = _number(
            section["transit_mid_m"], "scoring.transit_mid_m", minimum=0)
    if "transit_zero_m" in section:
        ns["TRANSIT_METRO_ZERO_M"] = _number(
            section["transit_zero_m"], "scoring.transit_zero_m", minimum=0)
        ns["NORMAL_METRO_WALK_MAX_M"] = ns["TRANSIT_METRO_ZERO_M"]
    if "centre_full_m" in section:
        ns["SCORE_CENTRE_FULL_M"] = _number(
            section["centre_full_m"], "scoring.centre_full_m", minimum=0)
    if "centre_zero_m" in section:
        ns["SCORE_CENTRE_ZERO_M"] = _number(
            section["centre_zero_m"], "scoring.centre_zero_m", minimum=0)
    if "water_full_m" in section:
        ns["SCORE_WATER_FULL_M"] = _number(
            section["water_full_m"], "scoring.water_full_m", minimum=0)
    if "water_zero_m" in section:
        ns["SCORE_WATER_ZERO_M"] = _number(
            section["water_zero_m"], "scoring.water_zero_m", minimum=0)
    if "floor_unknown_pts" in section:
        ns["FLOOR_UNKNOWN_PTS"] = _number(
            section["floor_unknown_pts"], "scoring.floor_unknown_pts",
            minimum=0, maximum=100)


def _apply_alerts(data: dict[str, Any], ns: dict[str, Any]) -> None:
    section = _section(data, "alerts", {
        "instant_rank_tiers", "price_drop_shortlist_only",
        "property_cooldown_days", "price_drop_saved_dkk",
        "price_drop_actionable_dkk"})
    if not section:
        return
    if "instant_rank_tiers" in section:
        tiers = _list(section["instant_rank_tiers"], "alerts.instant_rank_tiers")
        for tier in tiers:
            if tier not in ns["RANK_TIER_LETTERS"]:
                raise ProfileError(
                    f"alerts.instant_rank_tiers: {tier!r} is not a rank letter")
        ns["ALERT_INSTANT_RANK_TIERS"] = tuple(tiers)
    if "price_drop_shortlist_only" in section:
        ns["ALERT_PRICE_DROP_SHORTLIST_ONLY"] = _boolean(
            section["price_drop_shortlist_only"],
            "alerts.price_drop_shortlist_only")
    if "property_cooldown_days" in section:
        ns["ALERT_PROPERTY_COOLDOWN_DAYS"] = _number(
            section["property_cooldown_days"], "alerts.property_cooldown_days",
            minimum=0, maximum=3650, integer=True)
    if "price_drop_saved_dkk" in section:
        ns["PRICE_DROP_SAVED_DKK"] = _number(
            section["price_drop_saved_dkk"], "alerts.price_drop_saved_dkk",
            minimum=0, integer=True)
    if "price_drop_actionable_dkk" in section:
        ns["PRICE_DROP_ACTIONABLE_DKK"] = _number(
            section["price_drop_actionable_dkk"],
            "alerts.price_drop_actionable_dkk", minimum=0, integer=True)


def _apply_web(data: dict[str, Any], ns: dict[str, Any]) -> None:
    section = _section(data, "web", {"host", "port"})
    if not section:
        return
    if "host" in section:
        ns["WEB_HOST"] = _string(section["host"], "web.host")
    if "port" in section:
        ns["WEB_PORT"] = _number(section["port"], "web.port",
                                minimum=1, maximum=65535, integer=True)


def _apply_complexes(data: dict[str, Any], ns: dict[str, Any]) -> None:
    if "complexes" not in data:
        return
    groups = _dict(data["complexes"], "complexes")
    cleaned: dict[str, dict[str, Any]] = {}
    for key, group in groups.items():
        if str(key).startswith("_"):
            continue
        where = f"complexes.{key}"
        group = _dict(group, where)
        _unknown(where, group, {
            "name", "aliases", "street_only_aliases", "postcode", "evidence"})
        if "name" not in group:
            raise ProfileError(f"{where}.name is required")
        aliases = [_string(a, f"{where}.aliases[]")
                   for a in _list(group.get("aliases", []), f"{where}.aliases")]
        street = [_string(a, f"{where}.street_only_aliases[]")
                  for a in _list(group.get("street_only_aliases", []),
                                 f"{where}.street_only_aliases")]
        if not aliases and not street:
            raise ProfileError(f"{where} needs at least one alias")
        cleaned[str(key)] = {
            "name": _string(group["name"], f"{where}.name"),
            "aliases": aliases,
            "street_only_aliases": street,
            "postcode": str(group.get("postcode") or ""),
        }
        if group.get("evidence"):
            cleaned[str(key)]["evidence"] = _string(
                group["evidence"], f"{where}.evidence")
    ns["COMPLEX_GROUPS"] = cleaned


# ==============================================================================
# Derived constants
# ==============================================================================
def _recompute(ns: dict[str, Any]) -> None:
    """Rebuild constants that depend on a profile value."""
    cap = float(ns["RENT_CAP_MAX"])
    ns["AFFORDABILITY_ZERO_RENT"] = cap
    ns["AFFORDABILITY_SPAN"] = max(cap - ns["AFFORDABILITY_FULL_RENT"], 1.0)
    ns["AFFORDABILITY_CURVE"] = [
        (ns["AFFORDABILITY_FULL_RENT"], ns["SCORE_AFFORDABILITY_PTS"]),
        (cap, 0),
    ]

    ns["COMMUTE_CURVE"] = [
        (ns["SCORE_COMMUTE_FULL_MIN"], ns["SCORE_COMMUTE_PTS"]),
        (ns["SCORE_COMMUTE_ZERO_MIN"], 0),
    ]
    mid = ns.get("TRANSIT_METRO_24_M", ns["TRANSIT_METRO_ZERO_M"])
    if ns["TRANSIT_METRO_FULL_M"] < mid < ns["TRANSIT_METRO_ZERO_M"]:
        ns["TRANSIT_METRO_CURVE"] = [
            (ns["TRANSIT_METRO_FULL_M"], ns["SCORE_TRANSIT_PTS"]),
            (mid, ns["SCORE_TRANSIT_PTS"] * 0.8),
            (ns["TRANSIT_METRO_ZERO_M"], 0),
        ]
    else:
        ns["TRANSIT_METRO_CURVE"] = [
            (ns["TRANSIT_METRO_FULL_M"], ns["SCORE_TRANSIT_PTS"]),
            (ns["TRANSIT_METRO_ZERO_M"], 0),
        ]
    if ns["RP_S_TRAIN_MAX_M"] <= 0:
        ns["RP_S_TRAIN_CURVE"] = [(0, 0)]
    else:
        ns["RP_S_TRAIN_CURVE"] = [
            (ns["RP_S_TRAIN_FULL_M"], ns["SCORE_TRANSIT_PTS"] * 0.8),
            (ns["RP_S_TRAIN_MAX_M"], ns["SCORE_TRANSIT_PTS"] * 0.6),
        ]
    ns["FLOOR_CURVE"] = [(0, 0.0), (4, ns["SCORE_FLOOR_PTS"])]

    ns["COMMUTE_BAND_BLURBS"] = {
        "A": f"Door to door to {ns['OFFICE_NAME']} in {ns['TIER_A_MAX_MIN']} min or less.",
        "B": f"{ns['TIER_A_MAX_MIN'] + 1} to {ns['TIER_B_MAX_MIN']} min door to door.",
        "C": (f"{ns['TIER_B_MAX_MIN'] + 1} to {ns['TIER_C_MAX_MIN']} min, rent at most "
              f"{ns['RENT_CAP_MAX']} kr."),
    }

    bbox = tuple(ns["GREATER_CPH_BBOX"])
    ns["_BP_BBOX"] = bbox
    ns["BOLIGPORTAL_SEARCH_URLS"] = [
        f"https://www.boligportal.dk/lejligheder/k%C3%B8benhavn/"
        f"?max_monthly_rent={ns['DISCOVERY_RENT_MAX']}",
        (f"https://www.boligportal.dk/lejligheder/"
         f"?max_monthly_rent={ns['DISCOVERY_RENT_MAX']}"
         f"&min_lat={bbox[0]}&min_lng={bbox[1]}"
         f"&max_lat={bbox[2]}&max_lng={bbox[3]}"),
    ]
