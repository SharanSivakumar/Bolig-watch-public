"""Telegram notification layer for BoligWatch v3.0.

Strict rules:
1. Only text messages, NEVER photos or previews.
2. NO emojis.
3. One line per fact; unknown facts are omitted rather than printed as
   "unknown", except street and water which say "Street: unknown".
4. Valid escaped HTML formatting.
"""
import datetime
import html
import json
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import config
import tiers

_MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTHS_LONG = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]
_ASAP = ("snarest", "straks", "nu", "omgående", "omgaaende", "asap")


def _api(method: str, **fields: Any) -> Optional[int]:
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return None
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except Exception as e:
        if config.VERBOSE:
            print(f"  [tg] {method} failed: {e}")
        return None


def send_text(text: str, parse_html: bool = False) -> Optional[int]:
    """Send one plain text message to the configured chat."""
    kwargs = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text,
              "disable_web_page_preview": "true"}
    if parse_html:
        kwargs["parse_mode"] = "HTML"
    return _api("sendMessage", **kwargs)


def format_dkk(val: Optional[float]) -> str:
    """Format integer/float as Danish currency string (e.g. 9.300)."""
    if val is None:
        return "Ukendt"
    try:
        return f"{int(round(val)):,}".replace(",", ".")
    except (ValueError, TypeError):
        return "Ukendt"


def _fmt_date(val: Optional[str], months: List[str]) -> str:
    if not val:
        return "Ukendt"
    v = str(val).strip()
    if v.lower() in _ASAP:
        return "Snarest"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        yyyy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mm <= 12:
            return f"{dd} {months[mm - 1]} {yyyy}"
    return html.escape(v)


def format_date_human(val: Optional[str]) -> str:
    """Long form, e.g. '1 February 2027'."""
    return _fmt_date(val, _MONTHS_LONG)


def format_date_short(val: Optional[str]) -> str:
    """Short form used in alerts, e.g. '15 Feb 2027'."""
    return _fmt_date(val, _MONTHS_SHORT)


def tier_of(data: Dict[str, Any]) -> str:
    """Tier letter for a match row, falling back to the legacy category int."""
    t = data.get("tier")
    if t in ("A", "B", "C"):
        return t
    return config.TIER_LETTERS.get(data.get("category") or 1, "A")


def is_responsible_pick(data: Dict[str, Any]) -> bool:
    """True when a match qualified as a v3.3 Responsible Pick (metro-unreachable,
    within 800 m of an S-train, rent <= 8,500 kr)."""
    return bool(data.get("is_responsible_pick"))


def display_tier_of(data: Dict[str, Any]) -> str:
    """The displayed rank letter (S, A, B, C, D) derived from the 0-100 score.

    The commute tier is available from tier_of(data).
    """
    return tiers.rank_tier_of(data.get("score"))


def _route_dict(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    route = data.get("commute_route")
    if isinstance(route, str):
        try:
            route = json.loads(route)
        except Exception:
            return None
    return route if isinstance(route, dict) else None


def fmt_commute_line(data: Dict[str, Any]) -> Optional[str]:
    """'Nørreport 18 min: walk 4 + M2 13' or 'Nørreport 12 min: direct walk'."""
    cm = data.get("commute_min")
    if cm is None:
        return None
    head = f"{config.OFFICE_NAME} {round(cm)} min"
    route = _route_dict(data)
    if not route:
        return head
    if route.get("mode") == "walk":
        return f"{head}: direct walk"
    parts = []
    if route.get("walk_min") is not None:
        parts.append(f"walk {round(route['walk_min'])}")
    via = route.get("via") or route.get("network") or ""
    if route.get("ride_min") is not None:
        parts.append(html.escape(f"{via} {round(route['ride_min'])}".strip()))
    tail = " + ".join(parts)
    return f"{head}: {tail}" if tail else head


def value_label_of(data: Dict[str, Any]) -> str:
    """The value label, rebuilt from value_ratio when it was not persisted.

    matches stores value_ratio but not the rendered label, so a row read back
    from the database has to derive it or the alert loses the comparison.
    """
    if data.get("value_label"):
        return str(data["value_label"])
    ratio = data.get("value_ratio")
    if ratio is None:
        return "size unknown" if not data.get("kr_per_m2") else "no market benchmark"
    return f"{round((float(ratio) - 1) * 100):+d}% vs market"


def fmt_value_line(data: Dict[str, Any]) -> str:
    """'8.300 kr/md · 32 m2 · 259 kr/m2 (-11% vs market)'."""
    parts = [f"{format_dkk(data.get('rent_total'))} kr/md"]
    size = data.get("size_m2")
    if size:
        parts.append(f"{round(float(size))} m2")
    kr_m2 = data.get("kr_per_m2")
    label = value_label_of(data)
    if kr_m2:
        piece = f"{round(float(kr_m2))} kr/m2"
        if label and label not in ("size unknown",):
            piece += f" ({html.escape(str(label))})"
        parts.append(piece)
    elif label:
        parts.append(html.escape(str(label)))
    return " · ".join(parts)


def fmt_place_line(data: Dict[str, Any]) -> str:
    """'Fronts Amagerbrogade, 5 bus routes · Water 650 m (Amager Strand)'."""
    buzz = data.get("buzz_detail")
    left = html.escape(str(buzz)) if buzz else "Street: unknown"
    wd = data.get("water_dist_m")
    if wd is None:
        return left
    name = data.get("water_name")
    right = f"Water {round(float(wd))} m"
    if name:
        right += f" ({html.escape(str(name))})"
    return f"{left} · {right}"


def fmt_brief_alert(match_data: Dict[str, Any], prefix: str = "") -> str:
    """The v3.0 brief alert, one line per fact (spec section 8.1)."""
    tier = display_tier_of(match_data)
    score_val = round(match_data.get("score") or 0)
    prop_id = match_data.get("property_id") or 0
    bw_id = match_data.get("bw_id") or f"BW-{prop_id:07d}"

    head = f"Tier {tier} · {score_val}/100 · {html.escape(str(bw_id))}"
    if prefix:
        head = f"{html.escape(prefix)} {head}"
    lines = [f"<b>{head}</b>"]

    addr = match_data.get("address") or match_data.get("normalized_address") or "København"
    lines.append(html.escape(str(addr)))
    lines.append(fmt_value_line(match_data))

    if is_responsible_pick(match_data):
        lines.append("Responsible Pick: >1.5 km from the Metro but within "
                     "800 m of an S-train, at most 8,500 kr")

    commute = fmt_commute_line(match_data)
    if commute:
        lines.append(commute)
    lines.append(fmt_place_line(match_data))

    # v3.11 (spec 10 section 8): one line per check / over-limit badge.
    for flag in (match_data.get("soft_flags") or []):
        if isinstance(flag, dict):
            severity = flag.get("severity")
            label = flag.get("label")
        else:
            severity = getattr(flag, "severity", None)
            label = getattr(flag, "label", None)
        if severity in ("check", "over-limit") and label:
            lines.append(f"Check: {html.escape(str(label))}")

    avail = match_data.get("available_from") or match_data.get("available_date_str")
    rel = (match_data.get("relevance") or "").capitalize()
    if avail:
        tail = f"Available {format_date_short(avail)}"
        if rel:
            tail += f" · {rel}"
        lines.append(tail)
    elif rel:
        lines.append(rel)

    lines.append(f"/details {prop_id or html.escape(str(bw_id))}")
    return "\n".join(lines)


def fmt_details_message(data: Dict[str, Any]) -> str:
    """Full record for /details <id>."""
    rank = display_tier_of(data)
    rank_name = config.RANK_TIER_NAMES.get(rank, "")
    score_val = round(data.get("score") or 0)

    header = f"<b>Tier {rank} {html.escape(str(rank_name))} · {score_val}/100</b>"
    addr = html.escape(data.get("address") or data.get("normalized_address") or "København")

    aconto = data.get("utilities_amount")
    rent_tot = data.get("rent_total")
    if rent_tot is not None:
        if aconto:
            rent_line = f"Rent: {format_dkk(rent_tot)} kr/md including {format_dkk(aconto)} kr aconto"
        else:
            rent_line = f"Rent: {format_dkk(rent_tot)} kr/md all-inclusive"
    else:
        rent_line = "Rent: Ukendt"

    c_tier = tier_of(data)
    c_name = config.COMMUTE_BAND_NAMES.get(c_tier, "")
    c_blurb = config.COMMUTE_BAND_BLURBS.get(c_tier, "")
    if c_blurb:
        commute_band_line = f"Commute band: {html.escape(c_name)} ({html.escape(c_blurb.rstrip('.'))})"
    elif c_name:
        commute_band_line = f"Commute band: {html.escape(c_name)}"
    else:
        commute_band_line = "Commute band: Ukendt"

    if is_responsible_pick(data):
        budget_line = ("Budget: Responsible Pick, within the "
                       f"{format_dkk(config.RENT_CAP_MAX)} kr ceiling, "
                       f"at most {format_dkk(config.RP_RENT_MAX)} kr, "
                       "within 800 m of an S-train")
    else:
        budget_line = f"Budget: within the {format_dkk(config.RENT_CAP_MAX)} kr ceiling"

    body = [rent_line, commute_band_line, budget_line]

    size = data.get("size_m2")
    kr_m2 = data.get("kr_per_m2")
    label = value_label_of(data)
    if size and kr_m2:
        body.append(f"Value: {round(float(size))} m2 · {round(float(kr_m2))} kr/m2 · "
                    f"{html.escape(str(label))}")
    else:
        body.append(f"Value: {html.escape(str(label))}")
    seg = data.get("benchmark_segment")
    if seg and data.get("benchmark_median"):
        body.append(f"Benchmark: {round(float(data['benchmark_median']))} kr/m2 "
                    f"(segment {html.escape(str(seg))})")

    move_in = data.get("move_in_cost") or data.get("move_in_price")
    body.append(f"Move-in cost: {format_dkk(move_in)} kr" if move_in
                else "Move-in cost: Ukendt (quarantined)")

    avail = format_date_human(data.get("available_from") or data.get("available_date_str"))
    target_note = " [TARGET DATE]" if data.get("is_target_window") else ""
    body.append(f"Available: {avail}{target_note}")

    rel = data.get("relevance")
    if rel:
        body.append(f"Relevance: {html.escape(str(rel).capitalize())}")

    lease = "Unlimited" if (data.get("lease_verified")
                            or "ubegrænset" in str(data.get("rental_period", "")).lower()) \
        else (data.get("rental_period") or "Unverified")
    body.append(f"Lease: {html.escape(str(lease))}")
    body.append("Kitchen: " + ("Private, verified" if data.get("kitchen_verified")
                               or data.get("kitchen_status") == "own" else "Unverified"))
    body.append("Bathroom: " + ("Private, verified" if data.get("bathroom_verified")
                                else "Unverified"))

    commute = fmt_commute_line(data)
    if commute:
        body.append(f"Commute: {commute}")
    if data.get("geo_precision") == "street":
        body.append("Address resolved to street level")

    st = data.get("nearest_station")
    if st:
        dist_m = round(data.get("distance_m") or data.get("metro_walk_m") or 0)
        walk_min = data.get("walk_min") or max(1, round(dist_m / 80))
        body.append(f"Nearest station: {html.escape(str(st))} · {dist_m} m · {walk_min} min walk")

    body.append(f"Street: {html.escape(str(data.get('buzz_detail')))}"
                if data.get("buzz_detail") else "Street: unknown")

    wd = data.get("water_dist_m")
    if wd is not None:
        wname = data.get("water_name")
        body.append(f"Water: {round(float(wd))} m"
                    + (f" ({html.escape(str(wname))})" if wname else ""))

    city_dist = data.get("city_distance_m")
    if city_dist is not None:
        city_km = round(city_dist / 1000.0, 1)
        body.append(f"City centre: {str(city_km).replace('.', ',')} km")

    state = data.get("shortlist_action")
    if state:
        body.append(f"Shortlist: {html.escape(str(state))}")

    sources = data.get("sources")
    if sources:
        src_names = ", ".join(sorted({s.get("site", "portal").capitalize() for s in sources}))
        body.append(f"Source: {html.escape(src_names)}")
    else:
        body.append(f"Source: {html.escape(str(data.get('site', 'portal')).capitalize())}")

    risk_state = data.get("risk_state") or "low"
    risk_ev = data.get("risk_evidence") or []
    if isinstance(risk_ev, str):
        try:
            risk_ev = json.loads(risk_ev)
        except Exception:
            risk_ev = [risk_ev]
    if risk_state == "high":
        body.append(f"Risk: HIGH WARNING — {html.escape('; '.join(risk_ev))}")
    elif risk_state == "review":
        body.append(f"Risk: Review note — {html.escape('; '.join(risk_ev))}")
    else:
        body.append("Risk: No obvious warning signals")

    conf = data.get("confidence_points")
    if conf is None:
        conf = 0
    body.append(f"Verified {min(5, int(conf))}/5")

    sbd = data.get("score_breakdown") or {}
    if isinstance(sbd, str):
        try:
            sbd = json.loads(sbd)
        except Exception:
            sbd = {}
    # Ranking points are affordability/transit/commute/location/personal fit; value and date
    # are shown for market intelligence but carry zero ranking points (§3.5).
    score_line = (
        f"Score: Affordable {round(sbd.get('affordability', 0))}/{round(config.SCORE_AFFORDABILITY_PTS)} · "
        f"Transit {round(sbd.get('transit', 0))}/{round(config.SCORE_TRANSIT_PTS)} · "
        f"Commute {round(sbd.get('commute', 0))}/{round(config.SCORE_COMMUTE_PTS)}\n"
        f"       Water {round(sbd.get('water', 0))}/{round(config.SCORE_WATER_PTS)} · "
        f"Centre {round(sbd.get('centre', 0))}/{round(config.SCORE_CENTRE_PTS)} · "
        f"Buzz {round(sbd.get('buzz', 0))}/{round(config.SCORE_BUZZ_PTS)} · "
        f"Area {round(sbd.get('area', 0))}/{round(config.SCORE_AREA_PTS)} · "
        f"Floor {round(sbd.get('floor', 0))}/{round(config.SCORE_FLOOR_PTS)}\n"
        f"       Value {round(sbd.get('value', 0))}/{round(config.SCORE_VALUE_PTS)} · "
        f"Date {round(sbd.get('date', 0))}/{round(config.SCORE_DATE_PTS)}"
    )

    urls = []
    for s in (sources or []):
        u = s.get("url")
        if u and u not in urls:
            urls.append(u)
    if not urls and data.get("url"):
        urls.append(data["url"])
    url_lines = "\n".join(html.escape(u) for u in urls)

    parts = [f"{header}\n{addr}", "\n".join(body), score_line]
    if url_lines:
        parts.append(url_lines)
    return "\n\n".join(parts)


# ==============================================================================
# Alert senders
# ==============================================================================
def notify_match(match_data: Dict[str, Any], prefix: str = "") -> Optional[int]:
    """Send the brief alert. Strictly no photos, no previews, no emojis."""
    return send_text(fmt_brief_alert(match_data, prefix=prefix), parse_html=True)


def fmt_price_drop(bw_id: str, old_rent: Any, new_rent: Any) -> str:
    return f"Price drop {bw_id}: {format_dkk(old_rent)} -> {format_dkk(new_rent)} kr/md"


def fmt_removed(bw_id: str, address: Optional[str]) -> str:
    addr = f" ({address.split(',')[0].strip()})" if address else ""
    return f"Removed from portal: {bw_id}{addr}"


def fmt_relisted(bw_id: str, address: Optional[str]) -> str:
    addr = f" ({address.split(',')[0].strip()})" if address else ""
    return f"Relisted: {bw_id}{addr}"


def notify_price_drop(bw_id: str, old_rent: Any, new_rent: Any) -> Optional[int]:
    return send_text(fmt_price_drop(bw_id, old_rent, new_rent))


def notify_lifecycle(kind: str, bw_id: str, address: Optional[str]) -> Optional[int]:
    text = fmt_relisted(bw_id, address) if kind == "relisted" else fmt_removed(bw_id, address)
    return send_text(text)


def notify_incident(incident_data: Dict[str, Any]) -> Optional[int]:
    site = str(incident_data.get("site", "portal")).capitalize()
    count = incident_data.get("count", 2)
    err = incident_data.get("error", "Unknown error")
    msg = (
        f"<b>Portal Alert · {html.escape(site)}</b>\n"
        f"Repeated failures detected: {count} consecutive runs failed.\n"
        f"Error: {html.escape(str(err))}"
    )
    return send_text(msg, parse_html=True)


def notify_recovery(site: str) -> Optional[int]:
    msg = (f"<b>Portal Recovery</b>\n{html.escape(site.capitalize())} has successfully "
           f"recovered and resumed scanning.")
    return send_text(msg, parse_html=True)
