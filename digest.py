"""Daily digest generator for BoligWatch v3.2.

Grouped by rank (S Exceptional, A Strong, B Solid, up to 3 each; actionable
first, best intel fills), with the shortlist on top, market benchmarks, and a
tail count of C and D ranks on the dashboard only. Dismissed properties never appear.
STRICT RULES: Text only, NO photos, NO emojis.
"""
import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from profile import prescan_profile
prescan_profile()

import benchmarks
import config
import features
import notify
import tiers
from store import Store, format_bw_id

# Telegram carries one message a day, so each tier gets its three best and the
# dashboard carries the rest (alert policy).
MAX_PER_TIER = 3
DIGEST_RANK_TIERS = ("S", "A", "B")


def _short_addr(m: Dict[str, Any]) -> str:
    addr = m.get("address") or m.get("title") or "København"
    return str(addr).split(",")[0].strip()


def _short_date(iso: Optional[str]) -> str:
    if not iso:
        return "?"
    try:
        d = datetime.date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)[:10]
    return f"{d.day} {notify._MONTHS_SHORT[d.month - 1]}"


def _match_line(m: Dict[str, Any]) -> str:
    """'Jorisvej 1a · 78/100 · 8.300 kr · 259 kr/m2 -11% · Nørreport 18 min'."""
    parts = [m.get("group_name") or _short_addr(m), f"{round(m.get('score') or 0)}/100",
             f"{notify.format_dkk(m.get('rent_total'))} kr"]
    if notify.is_responsible_pick(m):
        parts.append("Responsible Pick")
    kr_m2 = m.get("kr_per_m2")
    label = m.get("value_label")
    if kr_m2:
        piece = f"{round(float(kr_m2))} kr/m2"
        ratio = m.get("value_ratio")
        if ratio:
            piece += f" {round((float(ratio) - 1) * 100):+d}%"
        elif label and label != "size unknown":
            piece += f" {label}"
        parts.append(piece)
    else:
        parts.append("size unknown")
    cm = m.get("commute_min")
    if cm is not None:
        parts.append(f"{config.OFFICE_NAME} {round(cm)} min")
    if m.get("eligible_unit_count", 0) > 1:
        parts.append(f"{m['eligible_unit_count']} eligible adverts; "
                     f"{m.get('actionable_unit_count', 0)} actionable")
    return " · ".join(parts)


def _shortlist_line(m: Dict[str, Any]) -> str:
    bw = m.get("bw_id") or format_bw_id(m.get("property_id") or 0)
    parts = [f"{bw} {_short_addr(m)}",
             f"{m.get('shortlist_action', 'saved')} {_short_date(m.get('shortlist_at'))}"]
    if m.get("rent_total") is not None:
        parts.append(f"{notify.format_dkk(m.get('rent_total'))} kr")
    parts.append(m.get("_change_note") or "no change")
    return " · ".join(parts)


def _ungeocodable_count(store: Store) -> int:
    cur = store.conn.execute("""
        SELECT COUNT(*) FROM quarantine
        WHERE resolution_state='quarantined' AND reasons LIKE '%ungeocodable address%'
    """)
    return cur.fetchone()[0]


def _price_drops_since(store: Store, since_iso: str) -> List[Dict[str, Any]]:
    # Mirror the observation writer's evidence-quality comparison. Joining
    # every older observation counted unchanged historical drops repeatedly.
    cur = store.conn.execute("""
        WITH priced AS (
            SELECT *, CASE WHEN detail_state IS NULL OR detail_state IN ('', 'fetched')
                           THEN 2 ELSE 1 END AS quality
            FROM observations
        )
        SELECT current.property_id, previous.rent_total AS old_rent,
               current.rent_total AS new_rent
        FROM priced current JOIN priced previous ON previous.id = (
            SELECT p.id FROM priced p
            WHERE p.source_listing_id=current.source_listing_id AND p.id<current.id
              AND p.quality>=current.quality
            ORDER BY p.id DESC LIMIT 1
        )
        WHERE current.observed_at>=? AND current.quality=previous.quality
          AND (previous.rent_total-current.rent_total)>=?
        ORDER BY current.id
    """, (since_iso, config.PRICE_DROP_SAVED_DKK))
    return [dict(r) for r in cur.fetchall()]


def _by_score(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(rows, key=lambda m: -(m.get("score") or 0))


def _top_for_rank(actionable: List[Dict[str, Any]], intel: List[Dict[str, Any]],
                  letter: str, limit: int) -> List[Dict[str, Any]]:
    """The rank's best rows: actionable first, then the best intel to fill up.

    Availability sits months ahead of the February 2027 move date, so most of the
    market is intel and an actionable-only digest would some days list nothing at
    all. Intel rows fill the remaining slots and are labelled as such, so the one
    message a day is still worth opening.
    """
    picked = _by_score([m for m in actionable if notify.display_tier_of(m) == letter])
    if len(picked) < limit:
        picked += _by_score([m for m in intel if notify.display_tier_of(m) == letter])
    return picked[:limit]


def generate_daily_digest(store: Optional[Store] = None,
                          segments: Optional[Dict[str, Any]] = None) -> str:
    if store is None:
        store = Store()

    now = datetime.datetime.now()
    date_str = f"{now.day} {notify._MONTHS_SHORT[now.month - 1]}"
    since_iso = (now - datetime.timedelta(hours=24)).isoformat()

    if segments is None:
        try:
            segments = benchmarks.compute_benchmarks(store, now)
        except Exception:
            segments = store.get_latest_benchmarks()

    # v3.3: the digest surfaces one representative per property/building/complex.
    grouped = store.grouped_matches(limit=200)
    for group in grouped:
        units = store.eligible_unit_rows(group.get("group_members") or [group["property_id"]])
        group["eligible_unit_count"] = len(units)
        group["actionable_unit_count"] = sum(u.get("relevance") == "actionable" for u in units)
    actionable = [m for m in grouped if m.get("relevance") == "actionable"]
    intel = [m for m in grouped if m.get("relevance") == "intel"]
    new_24h = [m for m in actionable if (m.get("first_seen_at") or "") >= since_iso]

    drops = _price_drops_since(store, since_iso)
    dismissed = store.get_dismissed_ids()
    drops = [d for d in drops if d.get("property_id") not in dismissed]

    cur = store.conn.execute("SELECT COUNT(*) FROM source_listings WHERE lifecycle_state='expired'")
    removed_count = cur.fetchone()[0]

    quarantine_count = len(store.get_quarantined_listings(limit=500))
    ungeocodable = _ungeocodable_count(store)

    health = store.get_portal_health()
    failing = [s for s, h in health.items()
               if h.get("consecutive_failures", 0) >= config.CONSECUTIVE_FAILURE_THRESHOLD]
    portal_status = ", ".join(failing) if failing else "none"

    lines = [
        f"BoligWatch · Daily · {date_str}",
        f"Actionable {len(actionable)} · Intel {len(intel)} · New 24h {len(new_24h)} · "
        f"Price drops {len(drops)} · Removed {removed_count}",
    ]

    shortlist = store.get_shortlist()
    if shortlist:
        drop_by_prop = {d["property_id"]: d for d in drops}
        lines.append("")
        lines.append("Shortlist")
        for m in shortlist:
            d = drop_by_prop.get(m.get("property_id"))
            if d:
                m["_change_note"] = (f"down {notify.format_dkk(d['old_rent'] - d['new_rent'])} kr")
            lines.append(_shortlist_line(m))

    for letter in DIGEST_RANK_TIERS:
        rows = _top_for_rank(actionable, intel, letter, MAX_PER_TIER)
        if not rows:
            continue
        lines.append("")
        lines.append(f"{letter} {config.RANK_TIER_NAMES[letter]}")
        for m in rows:
            suffix = "" if m.get("relevance") == "actionable" else " · intel"
            lines.append(_match_line(m) + suffix)

    all_matches = actionable + intel
    tail_counts = []
    for letter in ("C", "D"):
        cnt = sum(1 for m in all_matches if notify.display_tier_of(m) == letter)
        if cnt > 0:
            tail_counts.append(f"{letter} {cnt}")
    if tail_counts:
        lines.append("")
        lines.append(" · ".join(tail_counts) + " on the dashboard only")

    lines.append("")
    lines.append(benchmarks.summary_line(segments or {}))

    best_intel = None
    for m in intel:
        if m.get("value_ratio") is None:
            continue
        if best_intel is None or m["value_ratio"] < best_intel["value_ratio"]:
            best_intel = m
    if best_intel:
        cm = best_intel.get("commute_min")
        parts = [_short_addr(best_intel),
                 f"{notify.format_dkk(best_intel.get('rent_total'))} kr",
                 notify.value_label_of(best_intel)]
        if cm is not None:
            parts.append(f"{round(cm)} min")
        lines.append("Best value intel: " + " · ".join(parts))

    # Spec 06 section 7.3: the digest states which scan produced the snapshot it
    # is reading and when the rules were last applied, so a re-ranked list can
    # never be mistaken for a fresh look at the market.
    coverage_bits = []
    for row in store.portal_coverage_summary():
        status = row.get("coverage_status") or "never scanned"
        when = _short_date(row.get("completed_at")) if row.get("completed_at") else "never"
        cards = row.get("cards_seen")
        bit = f"{row['site']} {status} {when}"
        if cards:
            bit += f" ({cards} cards)"
        if row.get("areas_failed"):
            bit += f" ({row['areas_failed']} of {row.get('areas_total') or 0} areas failed)"
        coverage_bits.append(bit)
    if coverage_bits:
        lines.append("Portal data: " + " · ".join(coverage_bits))
    local_run = store.last_local_run() or {}
    if local_run.get("completed_at"):
        lines.append(f"Local rules: applied {_short_date(local_run['completed_at'])} "
                     f"(rules {local_run.get('rules_version') or 'unknown'})")

    health_bits = [f"Needs verification: {quarantine_count}",
                   f"Ungeocodable: {ungeocodable}",
                   f"Portal issues: {portal_status}"]
    if not features.water_available():
        health_bits.insert(0, "water table missing")
    lines.append(" · ".join(health_bits))
    # v3.11 (spec 10 section 8): a compact tally of the soft-flag badges.
    flag_counts = store.soft_flag_counts()
    if flag_counts:
        badge_bits = [f"{label}: {n}" for label, n in list(flag_counts.items())[:10]]
        lines.append("Badges: " + " · ".join(badge_bits))

    return "\n".join(lines)


def send_daily_digest(store: Optional[Store] = None, live: bool = False):
    """Send text digest to Telegram if live, or print local preview."""
    if store is None:
        store = Store()
    text = generate_daily_digest(store)
    if live and config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        notify.send_text(text)
    else:
        print("\n=== BOLIGWATCH DAILY DIGEST (LOCAL PREVIEW) ===")
        print(text)
        print("================================================\n")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="BoligWatch Daily Digest Generator")
    ap.add_argument("--live", action="store_true", help="Send live digest to Telegram")
    ap.add_argument("--dry-run", action="store_true", help="Print local digest preview")
    args = ap.parse_args()
    send_daily_digest(live=args.live and not args.dry_run)
