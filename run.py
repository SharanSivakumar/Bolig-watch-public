"""BoligWatch runner — Copenhagen rental scanner and local processor.

Usage:
  python run.py [--dry-run] [--sites lejebolig,boligportal] [--limit N]
  python run.py --rescore [--refresh-features]
  python run.py --backfill-evidence [--sites boligportal]
  python run.py --audit-integrity [--json] [--db PATH]
  python run.py --serve [--port 58240]
"""
import argparse
import datetime
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from profile import prescan_profile
prescan_profile()

import benchmarks
import config
import features
import filters
import geo
import notify
import tiers
from store import Store, append_log, format_bw_id


# ==============================================================================
# Geocoding and candidate processing
# ==============================================================================
def geocode_record(rec: Dict[str, Any]) -> None:
    """Resolve a record's coordinates through the v3.0 DAWA fallback chain.

    Never raises and never drops: a total failure leaves lat/lon None, which
    evaluate_listing turns into an 'ungeocodable address' quarantine.
    """
    geo_addr = rec.get("_geo_address") or rec.get("address") or ""
    addr_key = f"{rec.get('site')}|{geo_addr or rec.get('listing_id')}"

    if rec.get("lat") is not None and rec.get("lon") is not None:
        rec.setdefault("geo_precision", "exact")
        rec["dawa_normalized"] = True
    else:
        lat, lon, norm_addr, precision = geo.geocode_chain(addr_key, geo_addr)
        rec["lat"], rec["lon"] = lat, lon
        rec["geo_precision"] = precision
        if norm_addr:
            rec["normalized_address"] = norm_addr
            rec["dawa_normalized"] = True

    if not rec.get("postcode"):
        rec["postcode"] = geo.postcode_of(
            rec.get("normalized_address") or rec.get("address") or geo_addr)


def _alert_payload(rec: Dict[str, Any], eval_res: Any, bw_id: str,
                   prop_id: int) -> Dict[str, Any]:
    """Everything the v3.0 formatters need, in one flat dict."""
    return {
        "tier": eval_res.tier,
        # The alert gate reads this so a Responsible Pick is flagged correctly.
        "is_responsible_pick": bool(getattr(eval_res, "is_responsible_pick", False)),
        "budget_band": getattr(eval_res, "budget_band", "within"),
        "category": eval_res.category,
        "badge": eval_res.category_name,
        "score": eval_res.score,
        "address": rec.get("address") or rec.get("normalized_address"),
        "rent_total": eval_res.rent_total,
        "size_m2": eval_res.size_m2,
        "kr_per_m2": eval_res.kr_per_m2,
        "value_ratio": eval_res.value_ratio,
        "value_label": eval_res.value_label,
        "commute_min": eval_res.commute_min,
        "commute_route": eval_res.commute_route,
        "buzz_detail": eval_res.buzz_detail,
        "water_dist_m": eval_res.water_dist_m,
        "water_name": eval_res.water_name,
        "available_from": eval_res.available_date_str,
        "relevance": eval_res.relevance,
        "nearest_station": eval_res.nearest_station,
        "walk_min": eval_res.walk_min,
        "bw_id": bw_id,
        "property_id": prop_id,
        # v3.11 (spec 10 section 8): one alert line per check/over-limit badge.
        "soft_flags": getattr(eval_res, "soft_flags", None) or [],
    }


def handle_price_change(store: Store, prop_id: int, bw_id: str, pc: Dict[str, Any],
                        relevance: Optional[str], dry_run: bool) -> None:
    """Instant price-drop alert per spec 8.2, saved listings first.

    Spec 06 section 7.3: a price-drop alert requires two observations of the
    same listing from two DIFFERENT scan runs. Two readings inside one run are
    the same look at the market, not a change in it.
    """
    if pc.get("type") != "drop":
        return
    old_run, new_run = pc.get("old_scan_run_id"), pc.get("new_scan_run_id")
    if old_run is not None and new_run is not None and old_run == new_run:
        return
    drop = abs(pc["diff"])
    state = store.get_action_state(prop_id)
    action = state["action"] if state else None
    if action == "dismiss":
        return

    saved = action in ("save", "applied")
    if saved:
        threshold = config.PRICE_DROP_SAVED_DKK
    elif getattr(config, "ALERT_PRICE_DROP_SHORTLIST_ONLY", False):
        # A drop on a flat you have not shortlisted is dashboard material, not a
        # reason to buzz a phone (alert policy).
        threshold = None
    else:
        threshold = (config.PRICE_DROP_ACTIONABLE_DKK
                     if relevance == "actionable" else None)
    if threshold is None or drop < threshold:
        return

    text = notify.fmt_price_drop(bw_id, pc["old_rent"], pc["new_rent"])
    if dry_run or not config.BOT_CONFIRMED:
        print(f"  [alert, dry-run] {text}")
    else:
        notify.send_text(text)


def _trust_eligible(eval_res: Any) -> bool:
    """Is this result allowed to hold a property's representative match row?

    v3.11 (spec 10): a passed evaluation may hold a match row. The trust verdict
    no longer gates visibility - an unanswered unit type (`review`) and the
    softened trust rejections (private landlord, small private building) rank
    with badges. Hard rejections and the Q1/Q2 holds are still excluded.
    """
    return bool(getattr(eval_res, "passed", False))


def _check_geocode_health(store: "Store", dry_run: bool = True) -> None:
    """Raise a portal-style incident when every geocode call failed (spec 10 §3.2)."""
    if not geo.geocode_outage_suspected():
        return
    health = geo.geocode_health()
    detail = (f"all {health['attempts']} geocode calls this run failed with an "
              "HTTP error (Nominatim/Photon outage?)")
    print(f"  [geo] systemic failure: {detail}")
    try:
        should_notify, incident = store.record_portal_failure("geocode", detail)
        if should_notify and not dry_run:
            notify.notify_incident(incident)
    except Exception as exc:  # never let the health check break a scan
        if config.VERBOSE:
            print(f"  [geo] could not record the geocode incident: {exc}")


def cutover_summary(store: "Store") -> str:
    """Spec 10 section 8: how many listings are visible and each badge's tally."""
    stats = store.get_stats()
    lines = [f"BoligWatch v3.11 cut-over: {stats['total_matches']} listings visible"]
    for label, count in store.soft_flag_counts().items():
        lines.append(f"  {label}: {count}")
    return "\n".join(lines)


def process_candidate(rec: Dict[str, Any], stations: List[Dict[str, Any]],
                      store: Store, scan_run_id: Optional[int], dry_run: bool = True,
                      segments: Optional[Dict[str, Any]] = None,
                      refresh_features: bool = False,
                      card_snapshot_id: Optional[int] = None,
                      detail_snapshot_id: Optional[int] = None,
                      detail_state: Optional[str] = None,
                      card_hash: Optional[str] = None,
                      detail_hash: Optional[str] = None) -> bool:
    """Geocode, deduplicate, evaluate, record observations, alert if S/A rank."""
    # v3.4 ordering: detail evidence and an explicit housing verdict are
    # decided before geocoding. A card with unavailable evidence still gets a
    # property/source row for investigation, but it does not spend geo/routing
    # calls while quarantined.
    housing_evidence = filters.housing_disqualification_evidence(rec)
    evidence_missing = filters._eligibility_evidence_quarantine(rec)
    if not housing_evidence and not evidence_missing:
        geocode_record(rec)

    prop_id, bw_id, merge_status = store.resolve_property(
        addr=rec.get("normalized_address") or rec.get("address") or "",
        lat=rec.get("lat"),
        lon=rec.get("lon"),
        rooms=rec.get("rooms"),
        size_m2=rec.get("size_m2"),
        postcode=rec.get("postcode"),
        geo_precision=rec.get("geo_precision"),
    )
    rec["property_id"] = prop_id
    rec["bw_id"] = bw_id
    non_serializable = frozenset({"eval_result", "_unit_decision", "_tenancy_decision", "_building_decision", "_trust_decision", "_lifecycle_state", "_expired_at", "_last_seen_days_ago", "_observed_at", "_detail_fetched_at", "_portal_coverage_status", "_rules_version"})
    raw_payload = {k: v for k, v in rec.items() if k not in non_serializable}
    source_id = store.record_source_listing(
        property_id=prop_id,
        site=rec.get("site", "portal"),
        listing_id=str(rec.get("listing_id")),
        url=rec.get("url", ""),
        raw_data=raw_payload,
        card_class=rec.get("card_class"),
        detail_state=detail_state,
        detail_fetched_at=(datetime.datetime.now().isoformat(timespec="seconds")
                           if detail_state == DETAIL_FETCHED else None),
        card_hash=card_hash,
        detail_hash=detail_hash,
        card_price=rec.get("card_price"),
        card_text=rec.get("card_text"),
    )
    rec["source_listing_id"] = source_id

    prev_match = store.get_match_row(prop_id)
    if refresh_features and rec.get("lat") is not None:
        features.get_place_features(rec["lat"], rec["lon"], store=store, refresh=True)

    eval_res = filters.evaluate_listing(rec, stations, store=store, segments=segments)
    rec["eval_result"] = eval_res

    obs_id, changes = store.record_observation(
        source_listing_id=source_id,
        property_id=prop_id,
        scan_run_id=scan_run_id,
        rec=rec,
        eval_res=eval_res,
        card_snapshot_id=card_snapshot_id,
        detail_snapshot_id=detail_snapshot_id,
        detail_state=detail_state,
    )
    rec["observation_id"] = obs_id
    store.record_evaluation(
        source_listing_id=source_id, property_id=prop_id, observation_id=obs_id,
        outcome=("rejected" if eval_res.rejected
                 else "quarantined" if eval_res.quarantined else "match"),
        rules_version=config.eligibility_rules_version(), eval_res=eval_res,
        rank=notify.display_tier_of({"score": eval_res.score,
                                     "tier": eval_res.tier}) if not eval_res.rejected else None)

    # Spec 07 section 5.1: the nine trust facts this pass established, written
    # beside the evaluation they belong to and with the same source identity.
    # `None` means "nothing new" (section 5.1's COALESCE), never "erase".
    store.record_source_trust(rec.get("site", "portal"), str(rec.get("listing_id")),
                              **filters.source_trust_values(eval_res))
    store.record_source_eligibility(rec.get("site", "portal"), str(rec.get("listing_id")),
                                    **filters.source_eligibility_values(eval_res))

    if changes.get("price_change"):
        pc = changes["price_change"]
        if config.VERBOSE:
            print(f"  [{rec['site']}#{rec['listing_id']}] Price {pc['type']}: "
                  f"{pc['old_rent']} -> {pc['new_rent']} DKK (diff: {pc['diff']} DKK)")
        handle_price_change(store, prop_id, bw_id, pc,
                            getattr(eval_res, "relevance", None), dry_run)

    if detail_state == DETAIL_GONE or rec.get("explicit_gone_at"):
        store.clear_match_for_listing(prop_id, rec.get("site", "portal"), str(rec.get("listing_id")))
        store.mark_seen(rec["site"], str(rec["listing_id"]))
        return False

    if eval_res.rejected:
        store.record_rejection(
            prop_id, source_id, rec.get("site", "portal"), rec.get("listing_id"),
            eval_res.rejection_reason or "rejected",
            housing_evidence.get("source_url") if housing_evidence else None)
        if housing_evidence:
            store.record_building_disqualification(
                rec.get("normalized_address") or rec.get("address"),
                housing_evidence["reason"], housing_evidence.get("source_url"),
                housing_evidence.get("matched_text"))
            store.clear_match(prop_id)
        else:
            store.clear_match_for_listing(prop_id, rec.get("site", "portal"), rec.get("listing_id"))
        store.resolve_quarantine(rec.get("site", "portal"), rec.get("listing_id"))
        append_log(rec, "filtered", eval_res.rejection_reason or "rejected")
        if config.VERBOSE:
            print(f"  [{rec['site']}#{rec['listing_id']}] REJECTED: {eval_res.rejection_reason}")
        return False

    if eval_res.quarantined:
        store.clear_match_for_listing(prop_id, rec.get("site", "portal"), rec.get("listing_id"))
        store.log_quarantine(
            property_id=prop_id,
            source_listing_id=source_id,
            site=rec.get("site", "portal"),
            listing_id=str(rec.get("listing_id")),
            reasons=eval_res.quarantine_reasons,
        )
        append_log(rec, "quarantined", "; ".join(eval_res.quarantine_reasons))
        if config.VERBOSE:
            print(f"  [{rec['site']}#{rec['listing_id']}] QUARANTINED ({bw_id}): "
                  f"{', '.join(eval_res.quarantine_reasons)}")
        return False

    # A student or elderly verdict on ANY listing of a single-building address
    # condemns the building, whichever advert the scan happened to rank first.
    # This is the one rejection that is deliberately property-wide, so unlike
    # every other it uses clear_match rather than clear_match_for_listing.
    inherited = store.property_housing_disqualification(prop_id)
    if inherited:
        store.clear_match(prop_id)
        durable = store.building_disqualification_for_property(prop_id) or {}
        store.record_rejection(prop_id, source_id, rec.get("site", "portal"),
                               rec.get("listing_id"),
                               f"{inherited} (inherited from exact building)",
                               durable.get("evidence_url"))
        store.mark_seen(rec["site"], str(rec["listing_id"]))
        append_log(rec, "rejected", f"{inherited} (inherited from a sibling listing)")
        if config.VERBOSE:
            print(f"  [{rec['site']}#{rec['listing_id']}] REJECTED ({bw_id}): "
                  f"{inherited}, from another listing at the same address")
        return False

    store.resolve_quarantine(rec.get("site", "portal"), rec.get("listing_id"))
    payload = _alert_payload(rec, eval_res, bw_id, prop_id)
    dismissed = prop_id in store.get_dismissed_ids()

    # Alert policy (spec 7). alerted_actionable is the guard, not already_seen:
    # a match can become actionable while stored (the calendar moves, or a
    # --rescore rewrites relevance), and that must still produce the one alert
    # the flag promises. The flag makes it exactly one, whatever the route in.
    already_alerted = bool(prev_match and prev_match.get("alerted_actionable"))
    # Volume policy (alert policy): any S- or A-rank flat may
    # interrupt, whatever its commute band or timeline relevance. The scan runs
    # hourly, so this is how a just-listed exceptional flat reaches the phone.
    # The one-property cooldown and the one-shot flag still cap the volume: one
    # property may not ping again inside the cooldown however many of its units
    # relist, so a stable S-listing does not ring every hour.
    commute_tier = notify.tier_of(payload)
    rank = notify.display_tier_of(payload)
    rank_allowed = rank in config.ALERT_INSTANT_RANK_TIERS
    cooling = store.alert_cooldown_active(prop_id)
    should_alert = (
        rank_allowed
        and not cooling
        and not dismissed
        and not already_alerted
    )
    # "Now actionable:" distinguishes a listing we already knew about from a
    # genuinely new find.
    prefix = "Now actionable:" if (should_alert and prev_match) else ""

    brief_msg = notify.fmt_brief_alert(payload, prefix=prefix)
    live_alert = should_alert and not dry_run and config.BOT_CONFIRMED
    alert_status = "sent" if live_alert else "dry_run"

    # One property, one match row. A block of flats resolves to a single
    # property with a dozen live source listings, so let the best-scoring live
    # unit hold the row instead of whichever unit the scan reached last.
    outranked = store.better_match_exists(
        prop_id, rec.get("site", "portal"), str(rec.get("listing_id")),
        eval_res.score, getattr(eval_res, "budget_band", None), eval_res.rent_total)
    if not _trust_eligible(eval_res):
        # Spec 07 section 6.4 duty 1: a review/reject sibling may never lend
        # evidence to a property's representative. This result is not eligible,
        # so it is never logged as a match (and, having no row, never alerted):
        # it is reported instead, and `_rescore_core` counts the same defect.
        print(f"  [{rec['site']}#{rec['listing_id']}] trust invariant: "
              f"outcome={getattr(eval_res, 'trust_outcome', None)!r} may not hold "
              f"a match row for {bw_id}")
        store.mark_seen(rec["site"], str(rec["listing_id"]))
        return False
    if not outranked:
        store.log_match(
            property_id=prop_id,
            bw_id=bw_id,
            rec=rec,
            eval_res=eval_res,
            alert_text=brief_msg,
            alert_status=alert_status,
        )
    elif config.VERBOSE:
        print(f"  [{rec['site']}#{rec['listing_id']}] stored as a sibling unit of "
              f"{bw_id}; a better-scoring live unit holds the match row")
    store.mark_seen(rec["site"], str(rec["listing_id"]))

    if outranked:
        # The property is already represented by a better unit; alerting again
        # for a worse one is exactly the rotation spam the cooldown exists for.
        return False

    if not should_alert:
        if config.VERBOSE:
            why = ("dismissed" if dismissed
                   else f"rank {rank} not on the instant list" if not rank_allowed
                   else "property alerted recently, cooling down" if cooling
                   else "already alerted")
            print(f"  [{rec['site']}#{rec['listing_id']}] stored ({bw_id}), {why}")
        return False

    # Only a message that actually went out burns the one-shot flag. A dry run
    # that marked it would silently cost the real alert on the next live scan.
    append_log(rec, f"new_match_rank{rank}",
               f"score={eval_res.score}; bw_id={bw_id}; "
               f"relevance={eval_res.relevance}; band={commute_tier}")

    if not live_alert:
        print("\n=======================================================")
        print(f"  ACTIONABLE MATCH (Dry-run / Safe Local Output) [{bw_id}]")
        print("=======================================================")
        print(brief_msg)
        print("=======================================================")
    else:
        # Report what actually happened: an unchecked return value would log a
        # delivery we never confirmed. A failed send leaves the flag unset, so
        # the next scan retries it.
        status = notify.notify_match(payload, prefix=prefix)
        if status is None:
            store.unmark_actionable_alerted(prop_id)
            print(f"  [{rec['site']}#{rec['listing_id']}] TELEGRAM SEND FAILED for {bw_id}; "
                  f"will retry on the next scan")
        else:
            store.mark_actionable_alerted(prop_id)
            print(f"  [{rec['site']}#{rec['listing_id']}] NOTIFIED ON TELEGRAM: {bw_id} "
                  f"(Rank {rank} · commute band {commute_tier} · {eval_res.rent_total} kr/md)")

    return True


# ==============================================================================
# Discovery: the wide net, the detail envelope and per-area coverage
# ==============================================================================
# Fields the pipeline derives after a card is parsed. They must stay out of the
# card snapshot, or a timestamp or a freshly computed coordinate would change
# the content hash on every scan and defeat the whole point of deduplicating by
# hash: an unchanged page would grow a new snapshot row twice a day.
_DERIVED_CARD_KEYS = frozenset({
    "first_seen_at", "eligibility_checked_at", "eligibility_text",
    "eligibility_evidence_state", "eval_result", "property_id", "bw_id",
    "source_listing_id", "lat", "lon", "geo_precision", "normalized_address",
    "dawa_normalized", "move_in_cost", "move_in_cost_verified", "detail_state",
    "detail_hydration", "description", "observation_id",
    "unit_type_decision", "tenancy_channel_decision", "building_form_decision",
    "_unit_decision", "_tenancy_decision", "_building_decision", "_trust_decision",
    "_lifecycle_state", "_expired_at", "_last_seen_days_ago", "_observed_at",
    "_detail_fetched_at", "_portal_coverage_status", "_rules_version",
})

DETAIL_FETCH = "fetch"
DETAIL_FETCHED = "fetched"
DETAIL_FAILED = "failed"
DETAIL_GONE = "failed:gone"
# A detail page that loads but says the listing no longer exists. This is
# evidence about the page, not about the market: the lifecycle rule acts on it
# only when the site's next complete scan agrees.
GONE_MARKERS = getattr(config, "GONE_MARKERS", (
    "siden findes ikke", "annoncen er udløbet", "boligen er udlejet",
    "annoncen findes ikke", "page not found", "404 not found",
    "markeret som reserveret af udlejeren",
    "lejemålet er blevet markeret som reserveret",
    "markeret som reserveret",
    "lejemålet er reserveret",
    "boligen er reserveret",
))
SKIP_REASON_TEXT = {
    "skipped:rent": f"card rent above {config.DISCOVERY_RENT_MAX:,} kr",
    "skipped:rooms": (f"room count outside {config.ROOMS_MIN}-{config.ROOMS_MAX} "
                       "on the card"),
    "skipped:type": "card is a room, house, villa or kollegie",
    "skipped:card_class": "card classified as a room rental",
    "skipped:fresh": "detail page already stored and still fresh",
}


def card_payload(rec: Dict[str, Any]) -> Dict[str, Any]:
    """The immutable evidence a search card represents."""
    return {k: v for k, v in (rec or {}).items() if k not in _DERIVED_CARD_KEYS}


def detail_payload(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The immutable evidence a detail page represents: body text plus hydration."""
    text = rec.get("eligibility_text") or rec.get("description")
    hydration = rec.get("detail_hydration")
    if not text and hydration is None:
        return None
    return {"text": text, "hydration": hydration}


def card_envelope_state(rec: Dict[str, Any], stored: Optional[Dict[str, Any]] = None,
                        card_hash: Optional[str] = None,
                        now: Optional[datetime.datetime] = None) -> str:
    """Decide whether this card earns a detail page in this scan (spec 06 4.3).

    Evaluated on card data only, and deliberately wider than the eligibility
    rules: a card inside the envelope but outside the rules is fetched and
    stored so that raising the cap, allowing a second room or adding a phrase is
    answered locally. Returns ``fetch`` or ``skipped:<reason>``; the card itself
    is stored either way, with the reason it was not fetched.
    """
    card_rent = rec.get("card_price")
    if card_rent is None:
        card_rent = rec.get("rent_total")
    if card_rent is not None and card_rent > config.DISCOVERY_RENT_MAX:
        return "skipped:rent"

    rooms = rec.get("rooms")
    if (isinstance(rooms, (int, float)) and rooms
            and not (config.ROOMS_MIN <= rooms <= config.ROOMS_MAX)):
        return "skipped:rooms"

    listing_type = (rec.get("listing_type") or "").lower()
    if listing_type in ("værelse", "vaerelse", "room", "hus", "house", "villa",
                        "kollegie", "rental_room", "rental_house", "rental_villa"):
        return "skipped:type"

    if (rec.get("card_class") or "") == "room":
        return "skipped:card_class"

    # Fetch once. A card whose rent, availability or title moved since the last
    # detail snapshot is read again in the same scan even inside the window;
    # an unchanged card is not, and the window bounds how stale a detail page
    # can become while the card stands still.
    if stored and stored.get("detail_hash") and stored.get("detail_fetched_at"):
        if card_hash and stored.get("card_hash") and card_hash != stored.get("card_hash"):
            return DETAIL_FETCH
        try:
            fetched_at = datetime.datetime.fromisoformat(str(stored["detail_fetched_at"]))
        except ValueError:
            return DETAIL_FETCH
        age_days = ((now or datetime.datetime.now()) - fetched_at).days
        if age_days < config.DETAIL_REFRESH_DAYS:
            return "skipped:fresh"
    return DETAIL_FETCH


class ScanCounters:
    """What a site's run did, printed per spec 06 section 10 and stored on the run."""

    def __init__(self):
        self.cards_seen = 0
        self.cards_new = 0
        self.details_attempted = 0
        self.details_succeeded = 0
        self.details_failed = 0
        self.details_skipped = 0
        self.skips: Dict[str, int] = {}
        self.decision_revalidation_selected = 0
        self.decision_revalidation_succeeded = 0
        self.decision_revalidation_failed = 0
        self.decision_revalidation_gone = 0

    def skip(self, reason: str) -> None:
        self.details_skipped += 1
        self.skips[reason] = self.skips.get(reason, 0) + 1

    def summary(self) -> str:
        by_reason = ", ".join(f"{k} {v}" for k, v in sorted(self.skips.items())) or "none"
        reval = ""
        if self.decision_revalidation_selected > 0:
            reval = (f" · revalidation selected {self.decision_revalidation_selected} "
                     f"/ succeeded {self.decision_revalidation_succeeded} "
                     f"/ failed {self.decision_revalidation_failed} "
                     f"/ gone {self.decision_revalidation_gone}")
        return (f"cards {self.cards_seen} ({self.cards_new} new), details "
                f"attempted {self.details_attempted} / succeeded {self.details_succeeded} "
                f"/ failed {self.details_failed} / skipped {self.details_skipped} "
                f"[{by_reason}]{reval}")


def scan_card(rec: Dict[str, Any], stations: List[Dict[str, Any]], store: Store,
              scan_run_id: Optional[int], dry_run: bool, segments, refresh_features,
              page=None, detail_fetcher=None, counters: Optional[ScanCounters] = None,
              force_detail: bool = False) -> Dict[str, Any]:
    """Store one discovered card, then fetch its detail page if it earns one.

    Every card is stored, whatever its rent, rooms or type. Only the decision to
    open a detail page reads the envelope, and the reason a page was not opened
    is recorded on the listing.
    """
    counters = counters or ScanCounters()
    site = rec.get("site", "portal")
    listing_id = str(rec.get("listing_id"))
    stored = store.get_source_listing(site, listing_id)
    counters.cards_seen += 1
    if stored is None:
        counters.cards_new += 1

    payload = card_payload(rec)
    card_hash = Store.snapshot_hash(payload)
    state = DETAIL_FETCH if force_detail else card_envelope_state(rec, stored, card_hash)

    source_id = store.record_source_listing(
        property_id=None, site=site, listing_id=listing_id,
        url=rec.get("url") or "", raw_data=payload,
        card_class=rec.get("card_class"),
        detail_state=None if state == DETAIL_FETCH else state,
        card_hash=card_hash, card_price=rec.get("card_price"),
        card_text=rec.get("card_text"))
    rec["source_listing_id"] = source_id
    card_snapshot_id, _new_card = store.save_snapshot(
        source_id, "card", payload, url=rec.get("url"), scan_run_id=scan_run_id)

    if state != DETAIL_FETCH:
        counters.skip(state)
        if config.VERBOSE:
            print(f"  [{site}#{listing_id}] stored, detail not fetched "
                  f"({SKIP_REASON_TEXT.get(state, state)})")
        store.record_observation(
            source_listing_id=source_id, property_id=stored.get("property_id") if stored else None,
            scan_run_id=scan_run_id, rec=rec, eval_res=None,
            card_snapshot_id=card_snapshot_id, detail_state=state)
        return {"source_listing_id": source_id, "detail_state": state, "evaluated": False}

    counters.details_attempted += 1
    if detail_fetcher is not None:
        if config.VERBOSE:
            print(f"  [{site}#{listing_id}] opening detail page")
        rec = detail_fetcher(page, rec)
        rec["source_listing_id"] = source_id

    evidence_state = str(rec.get("eligibility_evidence_state") or "").lower()
    body = " ".join(str(rec.get("eligibility_text") or rec.get("description") or "").lower().split())
    matched_marker = next((m for m in GONE_MARKERS if m in body), None)
    if not matched_marker and (rec.get("detail_state") == DETAIL_GONE or rec.get("is_gone")):
        matched_marker = rec.get("gone_marker") or rec.get("status") or "gone"

    if matched_marker:
        counters.details_failed += 1
        detail_state = DETAIL_GONE
        store.record_source_explicit_gone(site, listing_id, matched_marker)
    elif evidence_state == "failed":
        counters.details_failed += 1
        detail_state = DETAIL_FAILED
    else:
        counters.details_succeeded += 1
        detail_state = DETAIL_FETCHED
        store.clear_source_explicit_gone(site, listing_id)
    rec["detail_state"] = detail_state

    detail_body = detail_payload(rec)
    detail_snapshot_id = None
    detail_hash = None
    if detail_body is not None and detail_state == DETAIL_FETCHED:
        detail_hash = Store.snapshot_hash(detail_body)
        detail_snapshot_id, _ = store.save_snapshot(
            source_id, "detail", detail_body, url=rec.get("url"),
            scan_run_id=scan_run_id)

    process_candidate(rec, stations, store, scan_run_id, dry_run,
                      segments=segments, refresh_features=refresh_features,
                      card_snapshot_id=card_snapshot_id,
                      detail_snapshot_id=detail_snapshot_id,
                      detail_state=detail_state, card_hash=card_hash,
                      detail_hash=detail_hash)
    return {"source_listing_id": rec.get("source_listing_id", source_id),
            "detail_state": detail_state, "evaluated": True}


# ==============================================================================
# Portal runners
# ==============================================================================
def _record_areas(store: Store, scan_run_id: Optional[int], site: str, result) -> None:
    for report in getattr(result, "areas", []) or []:
        store.record_scan_area(
            scan_run_id, site, report.get("area_or_query") or site,
            report.get("status") or "ok", cards_seen=report.get("cards_seen") or 0,
            expected_cards=report.get("expected_cards"),
            pages_loaded=report.get("pages_loaded") or 0,
            error=report.get("error"), started_at=report.get("started_at"),
            finished_at=report.get("finished_at"))


def revalidate_decision_details(site: str, page, stations, store: Store,
                                scan_run_id: Optional[int], dry_run: bool,
                                segments, refresh_features: bool, detail_fetcher,
                                counters: ScanCounters, seen_this_scan: Set[Tuple[str, str]],
                                limit: int = 0) -> None:
    """Fetch 24-hour decision-relevant detail pages during scheduled scan (Spec 09 section 8.1)."""
    if detail_fetcher is None:
        return
    reval_rows = select_for_decision_revalidation(
        store, sites=[site], already_fetched=seen_this_scan, limit=limit
    )
    for row in reval_rows:
        key = (site, str(row.get("listing_id")))
        seen_this_scan.add(key)
        counters.decision_revalidation_selected += 1
        rec = dict(row.get("raw") or {})
        rec["site"] = site
        rec["listing_id"] = row["listing_id"]
        rec["url"] = rec.get("url") or row.get("url")
        rec["source_listing_id"] = row["source_listing_id"]
        rec["property_id"] = row.get("property_id")
        rec.setdefault("card_class", row.get("card_class"))
        rec.setdefault("card_text", row.get("card_text"))
        rec.setdefault("card_price", row.get("card_price"))
        if config.VERBOSE:
            print(f"  [{site}#{row['listing_id']}] decision revalidation: opening {rec['url']}")
        outcome = scan_card(
            rec, stations, store, scan_run_id, dry_run, segments,
            refresh_features, page=page, detail_fetcher=detail_fetcher,
            counters=counters, force_detail=True
        )
        state = outcome.get("detail_state")
        if state == DETAIL_FETCHED:
            counters.decision_revalidation_succeeded += 1
        elif state == DETAIL_GONE:
            counters.decision_revalidation_gone += 1
        else:
            counters.decision_revalidation_failed += 1


def run_lejebolig(page, stations, store, scan_run_id, dry_run, limit,
                  segments=None, refresh_features=False,
                  counters: Optional[ScanCounters] = None) -> List[int]:
    from fetchers import lejebolig_detail, lejebolig_search
    counters = counters if counters is not None else ScanCounters()
    observed_source_ids: List[int] = []
    seen_this_scan = set()
    fetched_this_scan = set()
    n = 0
    for area in config.LEJEBOLIG_AREAS:
        if config.VERBOSE:
            print(f"[lejebolig] scanning area: {area}")
        started = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            cards = lejebolig_search(page, area)
        except Exception as e:
            # An area that raised is failed coverage, not an empty area. This
            # one line is the whole difference between "the market is empty" and
            # "we could not look", and the confusion expired 68 live listings.
            store.record_scan_area(scan_run_id, "lejebolig", area, "failed",
                                    error=str(e), started_at=started)
            if config.VERBOSE:
                print(f"  [lejebolig] area {area} FAILED: {e}")
            continue
        _record_areas(store, scan_run_id, "lejebolig", cards)

        for cand in cards:
            key = ("lejebolig", str(cand.get("listing_id")))
            if key in seen_this_scan:
                continue          # the same card on two areas costs one fetch
            seen_this_scan.add(key)
            if limit and n >= limit:
                return observed_source_ids
            n += 1
            outcome = scan_card(cand, stations, store, scan_run_id, dry_run,
                                segments, refresh_features, page=page,
                                detail_fetcher=lejebolig_detail, counters=counters)
            if outcome.get("source_listing_id"):
                observed_source_ids.append(outcome["source_listing_id"])
            if outcome.get("evaluated"):
                fetched_this_scan.add(key)

    revalidate_decision_details("lejebolig", page, stations, store, scan_run_id,
                                dry_run, segments, refresh_features, lejebolig_detail,
                                counters, fetched_this_scan, limit)
    return observed_source_ids


def _run_card_portal(site, search, detail, page, stations, store, scan_run_id,
                     dry_run, limit, segments, refresh_features, counters) -> List[int]:
    """One-query portals: BoligPortal uses this shape."""
    if config.VERBOSE:
        print(f"[{site}] fetching Greater Copenhagen listings")
    cards = search(page)
    _record_areas(store, scan_run_id, site, cards)
    if config.VERBOSE:
        print(f"  found {len(cards)} cards from {site}")

    observed_source_ids: List[int] = []
    seen_this_scan = set()
    fetched_this_scan = set()
    n = 0
    for rec in cards:
        key = (site, str(rec.get("listing_id")))
        if key in seen_this_scan:
            continue
        seen_this_scan.add(key)
        if limit and n >= limit:
            break
        n += 1
        outcome = scan_card(rec, stations, store, scan_run_id, dry_run, segments,
                            refresh_features, page=page, detail_fetcher=detail,
                            counters=counters)
        if outcome.get("source_listing_id"):
            observed_source_ids.append(outcome["source_listing_id"])
        if outcome.get("evaluated"):
            fetched_this_scan.add(key)

    revalidate_decision_details(site, page, stations, store, scan_run_id,
                                dry_run, segments, refresh_features, detail,
                                counters, fetched_this_scan, limit)
    return observed_source_ids


def run_boligportal(page, stations, store, scan_run_id, dry_run, limit,
                    segments=None, refresh_features=False,
                    counters: Optional[ScanCounters] = None) -> List[int]:
    from fetchers import boligportal_detail, boligportal_search
    return _run_card_portal(
        "boligportal",
        lambda pg: boligportal_search(pg, max_pages=config.BP_MAX_PAGES),
        boligportal_detail, page, stations, store, scan_run_id, dry_run, limit,
        segments, refresh_features,
        counters if counters is not None else ScanCounters())


def notify_lifecycle_changes(store: Store, transitions: Dict[str, Any], dry_run: bool,
                             coverage_status: str = "complete") -> None:
    """Expired and relisted alerts, saved properties only (spec 8.2).

    A transition only reaches Telegram when a scan that demonstrably covered the
    site produced it; a partial or unconfirmed-empty scan has nothing to say
    about whether a listing is gone.
    """
    if not Store.coverage_counts_as_miss(coverage_status):
        return
    states = store.get_action_states()
    for kind, key in (("expired", "expired_properties"), ("relisted", "relisted_properties")):
        for prop_id in transitions.get(key, []) or []:
            st = states.get(prop_id)
            if not st or st["action"] not in ("save", "applied"):
                continue
            row = store.get_match_row(prop_id) or {}
            text = (notify.fmt_relisted(format_bw_id(prop_id), row.get("address"))
                    if kind == "relisted"
                    else notify.fmt_removed(format_bw_id(prop_id), row.get("address")))
            if dry_run or not config.BOT_CONFIRMED:
                print(f"  [alert, dry-run] {text}")
            else:
                notify.send_text(text)


# ==============================================================================
# Rescore
# ==============================================================================
def _top_table(store: Store, limit_per_tier: int = 10) -> Dict[str, List[Dict[str, Any]]]:
    out = {}
    for tier in ("A", "B", "C"):
        out[tier] = store.get_matches(tier=tier, limit=limit_per_tier, order="tier")
    return out


def _print_table(title: str, table: Dict[str, List[Dict[str, Any]]]) -> None:
    print(f"\n--- {title} ---")
    for tier in ("A", "B", "C"):
        rows = table.get(tier) or []
        print(f"\nCommute band {tier} {config.COMMUTE_BAND_NAMES.get(tier, '')} ({len(rows)})")
        if not rows:
            print("  (none)")
            continue
        print(f"  {'BW-ID':<12} {'rank':<4} {'score':>5} {'rent':>6} {'min':>4} {'kr/m2':>6}  address")
        for m in rows:
            addr = str(m.get("address") or "")[:44]
            kr = m.get("kr_per_m2")
            rank_letter = tiers.rank_tier_of(m.get("score"))
            print(f"  {str(m.get('bw_id') or ''):<12} {rank_letter:<4} {round(m.get('score') or 0):>5} "
                  f"{int(m.get('rent_total') or 0):>6} "
                  f"{('-' if m.get('commute_min') is None else round(m['commute_min'])):>4} "
                  f"{('-' if kr is None else round(kr)):>6}  {addr}")


class _NoNetwork:
    """Make the whole evaluation call tree answer from cache only.

    Local processing must never contact a portal, and without --enrich it must
    never contact DAWA, OSRM or Overpass either. Enforcing that at the source
    rather than by threading a flag through every caller means a new code path
    cannot quietly reintroduce a lookup: spec 06 acceptance 16 asserts it.
    """

    def __init__(self, enrich: bool = False):
        self.enrich = enrich
        self._previous = None

    def __enter__(self):
        self._previous = (geo.CACHE_ONLY, features.CACHE_ONLY)
        geo.CACHE_ONLY = features.CACHE_ONLY = not self.enrich
        return self

    def __exit__(self, *exc):
        geo.CACHE_ONLY, features.CACHE_ONLY = self._previous
        return False


def expired_visible_cutoff(today: Optional[datetime.date] = None) -> datetime.date:
    return (today or datetime.date.today()) - datetime.timedelta(
        days=int(config.EXPIRED_VISIBLE_DAYS))


def refresh_local(store: Store, enrich: bool = False,
                  export: bool = True, limit: int = 0,
                  refresh_features: bool = False, quiet: bool = False,
                  migration_summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Re-apply the current rules to the stored snapshot. No portal, ever.

    Reads source listings, their latest snapshots and observations in scope
    (active, missing, relisted, plus expired inside EXPIRED_VISIBLE_DAYS),
    re-evaluates every one of them from its own latest record, rewrites the
    match, quarantine and rejection rows, stamps when the rules were applied
    and which version they were, and rebuilds report.html.

    It never changes last_seen_at, consecutive_misses, lifecycle_state or
    last_complete_scan_id, never opens a coverage scan run, and never alerts.
    A rejection is not sticky: a listing rejected yesterday for 9,650 kr is back
    on the shortlist the moment the cap admits it, with no fetch.

    `migration_summary`, when given, is the caller's spec 07 section 7.3
    migration summary. It is recorded verbatim in the run's `detail` and read
    back through `Store.last_local_run_detail`; this function computes nothing
    from it, and with the default `None` the `detail` string is exactly what it
    always was.
    """
    started = time.time()
    rules_version = config.eligibility_rules_version()
    local_run_id = store.start_local_run(rules_version, enrich=enrich)
    with _NoNetwork(enrich=enrich):
        result = _rescore_core(store, refresh_features=refresh_features, limit=limit,
                               rules_version=rules_version, local=True, quiet=quiet)
    counts = result["counts"]
    mode = "enrich" if enrich else "no-network"
    detail = mode if migration_summary is None else json.dumps(
        {"mode": mode, "migration": migration_summary}, ensure_ascii=False)
    store.finish_local_run(
        local_run_id, evaluated=counts["evaluated"], skipped=counts["skipped"],
        matched=counts["matched"], rejected=counts["rejected"],
        quarantined=counts["quarantined"], published=False,
        duration_sec=round(time.time() - started, 2),
        detail=detail)

    if export:
        from viewer import export_static_html
        path = export_static_html(store=store)
        print(f"  [refresh-local] wrote {path}")
    network = ("non-portal enrichment was allowed (DAWA, OSRM, Overpass); no "
               "portal search or detail page was opened"
               if enrich else "no portal, DAWA, OSRM or Overpass request was made")
    print(f"  [refresh-local] rules {rules_version} applied to "
          f"{counts['evaluated']} listing(s) in {round(time.time() - started, 1)}s; "
          f"{network}")
    result["rules_version"] = rules_version
    result["local_run_id"] = local_run_id
    return result


def rescore(store: Store, refresh_features: bool = False, limit: int = 0) -> Dict[str, Any]:
    """Re-evaluate every stored listing from raw_data. Idempotent, never alerts.

    Kept as the historical action. Unlike --refresh-local it is still allowed to
    retry an ungeocodable address against DAWA, which is what it has always
    done; --refresh-local moves that behind --enrich.
    """
    return _rescore_core(store, refresh_features=refresh_features, limit=limit,
                         rules_version=config.eligibility_rules_version(), local=False)


def _rescore_core(store: Store, refresh_features: bool = False, limit: int = 0,
                  rules_version: str = "", local: bool = False,
                  quiet: bool = False) -> Dict[str, Any]:
    stations = geo.load_stations()
    before = _top_table(store)
    if not quiet:
        _print_table("BEFORE rescore", before)

    segments = benchmarks.compute_benchmarks(store)
    print(f"\nBenchmarks: {len(segments)} segments · {benchmarks.summary_line(segments)}")

    # A rule change can withdraw support from a durable building verdict. This
    # runs before the listings are judged, so a superseded record cannot condemn
    # a property in the same pass that retired it (spec 06 section 7.2).
    superseded = store.supersede_unsupported_building_disqualifications()
    if superseded["superseded"]:
        print(f"  [rules] {superseded['superseded']} building disqualification(s) "
              f"marked superseded; the current rules no longer support them "
              f"({superseded['held']} held, {superseded['unverifiable']} not re-testable)")

    rows = store.local_scope_candidates()
    if limit:
        rows = rows[:limit]
    counts = {"evaluated": 0, "matched": 0, "quarantined": 0, "rejected": 0, "skipped": 0,
              "properties": 0, "geocoded": 0, "expired_visible": 0, "card_only": 0,
              "trust_invariant": 0}
    coverage = {row["site"]: row.get("coverage_status")
                for row in store.portal_coverage_summary()}
    # One property can carry several source listings. Evaluate them all, then let
    # the best passing one own the match row; a rejection on a sibling listing
    # must not delete a match another listing still supports.
    best: Dict[int, Any] = {}
    quarantines: Dict[int, Any] = {}

    detail_hydrations = store.latest_detail_hydrations()

    for row in rows:
        rec = row.get("raw") or {}
        if not rec:
            counts["skipped"] += 1
            continue
        rec.pop("eval_result", None)
        rec["site"] = row["site"]
        rec["listing_id"] = row["listing_id"]
        rec["url"] = rec.get("url") or row.get("url")
        rec["property_id"] = row["property_id"]
        if rec.get("lat") is None and row.get("lat") is not None:
            rec["lat"], rec["lon"] = row["lat"], row["lon"]
        rec.setdefault("normalized_address", row.get("normalized_address"))
        rec.setdefault("postcode", row.get("postcode")
                       or geo.postcode_of(rec.get("normalized_address") or rec.get("address")))
        rec.setdefault("geo_precision", row.get("geo_precision"))
        if rec.get("detail_hydration") is None and row["source_listing_id"] in detail_hydrations:
            rec["detail_hydration"] = detail_hydrations[row["source_listing_id"]]
        # Older BoligPortal snapshots labelled every postal code København.
        # Correct their display address from the same card's city_area while
        # they await the next scan with the fixed parser.
        if rec["site"] == "boligportal":
            source = rec.get("source_record") or {}
            locality = str(source.get("city_area") or "").strip()
            postcode = str(source.get("postal_code") or rec.get("postcode") or "")
            old_tail = f", {postcode} København"
            if locality and postcode and str(rec.get("address") or "").endswith(old_tail):
                rec["address"] = rec["address"][:-len(old_tail)] + f", {postcode} {locality}"
        for field in ("eligibility_text", "eligibility_source_url",
                      "eligibility_evidence_state", "eligibility_checked_at"):
            if rec.get(field) is None and row.get(field) is not None:
                rec[field] = row[field]
        # Stored discovery facts the record itself may predate.
        for field in ("card_class", "detail_state", "card_text", "card_price"):
            if rec.get(field) is None and row.get(field) is not None:
                rec[field] = row[field]
        # Spec 07 section 5.1 facts the last pass stored, merged the same way, so
        # `classify_tenancy_channel`'s stored-landlord route and
        # `classify_building_form`'s stored-conflict rule read them (a repaired
        # read-back: without this the columns only decorate the read model).
        for field in ("tenancy_channel_state", "landlord_name",
                      "landlord_evidence_source", "landlord_evidence_excerpt",
                      "building_form_state", "building_profile_id"):
            if rec.get(field) is None and row.get(field) is not None:
                rec[field] = row[field]
        # Spec 09 section 6.2 demographic facts the last pass stored.
        for field in ("student_requirement_state", "age_requirement_state",
                      "age_min", "age_max", "profile_age_fit",
                      "eligibility_fact_excerpt", "explicit_gone_at",
                      "explicit_gone_marker"):
            if rec.get(field) is None and row.get(field) is not None:
                rec[field] = row[field]

        # Check for explicit gone / reserved markers in stored text
        body = " ".join(str(rec.get("eligibility_text") or rec.get("card_text") or rec.get("description") or "").lower().split())
        matched_marker = next((m for m in GONE_MARKERS if m in body), None)
        if matched_marker:
            rec["explicit_gone_at"] = rec.get("explicit_gone_at") or datetime.datetime.now().isoformat(timespec="seconds")
            rec["explicit_gone_marker"] = matched_marker
            store.record_source_explicit_gone(row["site"], str(row["listing_id"]), matched_marker)

        lifecycle_state = row.get("lifecycle_state")
        is_expired = lifecycle_state == "expired"
        if is_expired:
            counts["expired_visible"] += 1
        if str(rec.get("detail_state") or "").startswith(("skipped:", "failed")):
            counts["card_only"] += 1

        prop_id = row["property_id"]
        if rec.get("lat") is None:
            # Spec 11.3: an ungeocodable address is retried on every pass. That
            # is a DAWA lookup, not a portal fetch, so --rescore may do it; a
            # local refresh may not, and _NoNetwork makes the call a no-op.
            geocode_record(rec)
            if rec.get("lat") is not None and prop_id is not None:
                store.conn.execute(
                    "UPDATE properties SET lat=?, lon=?, postcode=COALESCE(postcode, ?), "
                    "geo_precision=? WHERE id=?",
                    (rec["lat"], rec["lon"], rec.get("postcode"),
                     rec.get("geo_precision"), prop_id))
                store.conn.commit()
                counts["geocoded"] += 1
        if refresh_features and rec.get("lat") is not None:
            features.get_place_features(rec["lat"], rec["lon"], store=store, refresh=True)

        try:
            res = filters.evaluate_listing(rec, stations, store=store, segments=segments)
        except Exception as e:
            counts["skipped"] += 1
            if config.VERBOSE:
                print(f"  [rescore] {row['site']}#{row['listing_id']} failed: {e}")
            continue

        counts["evaluated"] += 1
        latest_obs = store.latest_observation(row["source_listing_id"]) or {}
        store.record_evaluation(
            source_listing_id=row["source_listing_id"], property_id=prop_id,
            observation_id=latest_obs.get("id"),
            outcome=("rejected" if res.rejected
                     else "quarantined" if res.quarantined else "match"),
            rules_version=rules_version, eval_res=res,
            rank=(None if res.rejected
                  else notify.display_tier_of({"score": res.score})))
        # Spec 07 section 5.1 & Spec 09 section 6.2: the trust and demographic
        # facts this listing's evaluation established.
        store.record_source_trust(row["site"], str(row["listing_id"]),
                                  **filters.source_trust_values(res))
        store.record_source_eligibility(row["site"], str(row["listing_id"]),
                                        **filters.source_eligibility_values(res))
        evidence = filters.housing_disqualification_evidence(rec)
        if evidence:
            store.record_building_disqualification(
                rec.get("normalized_address") or rec.get("address"),
                evidence["reason"], evidence.get("source_url"),
                evidence.get("matched_text"))
        if not res.quarantined:
            store.resolve_quarantine(row["site"], row["listing_id"])
        if res.rejected:
            counts["rejected"] += 1
            evidence = filters.housing_disqualification_evidence(rec)
            store.record_rejection(
                prop_id, row["source_listing_id"], row["site"], row["listing_id"],
                res.rejection_reason or "rejected",
                evidence.get("source_url") if evidence else None)
        elif res.quarantined:
            counts["quarantined"] += 1
            # A card we deliberately did not fetch is not a review backlog item:
            # it is a card we have not looked at yet. It is evaluated and shown,
            # labelled "card only", but it does not inflate the quarantine count
            # that exists for listings we DID read and still could not place.
            if not str(rec.get("detail_state") or "").startswith(("skipped:", "failed")):
                quarantines.setdefault(prop_id, (row, res))
        else:
            # Spec 07 section 6.4 duty 1: only an eligible source may represent a
            # property. A review/reject sibling is quarantined/rejected above and
            # cannot reach this branch, so a non-eligible result that still reads
            # as a match is a defect: counted as `trust_invariant`, reported
            # here and in the summary line, never logged as a match, and never
            # offered to `best` - which therefore holds eligible results only.
            if not _trust_eligible(res):
                counts["trust_invariant"] += 1
                print(f"  [trust-invariant] {row['site']}#{row['listing_id']} "
                      f"outcome={getattr(res, 'trust_outcome', None)!r} is not "
                      f"eligible; property {prop_id} takes no match row from it")
                continue
            if row.get("explicit_gone_at") or rec.get("explicit_gone_at"):
                continue
            counts["matched"] += 1
            rec["_lifecycle_state"] = lifecycle_state
            rec["_expired_at"] = row.get("expired_at")
            rec["_last_seen_days_ago"] = row.get("last_seen_days_ago")
            rec["_observed_at"] = row.get("last_seen_at")
            rec["_detail_fetched_at"] = row.get("detail_fetched_at")
            rec["_portal_coverage_status"] = coverage.get(row["site"])
            rec["_rules_version"] = rules_version
            prev = best.get(prop_id)
            rank = Store.match_rank(res.score, getattr(res, "budget_band", None),
                                    res.rent_total)
            # An expired listing is evaluated and visible, but it never takes a
            # representative row from a unit that is still on the market.
            candidate_key = (0 if is_expired else 1, rank)
            previous_key = ((0 if (prev and prev[2]) else 1),
                            Store.match_rank(prev[1].score,
                                             getattr(prev[1], "budget_band", None),
                                             prev[1].rent_total)) if prev else None
            if previous_key is None or candidate_key > previous_key:
                best[prop_id] = (rec, res, is_expired)

    for prop_id in {r["property_id"] for r in rows if r["property_id"] is not None}:
        counts["properties"] += 1
        inherited = store.property_housing_disqualification(prop_id)
        if inherited:
            store.clear_match(prop_id)
            durable = store.building_disqualification_for_property(prop_id) or {}
            store.record_rejection(
                prop_id, None, None, None,
                f"{inherited} (inherited from exact building)",
                durable.get("evidence_url"))
            counts["rejected"] += 1
            continue
        if prop_id in best:
            rec, res, _expired = best[prop_id]
            if not _trust_eligible(res):
                # Unreachable while the guard above holds (nothing else writes
                # `best`); kept so no future edit can log a non-eligible result
                # as a match row. Same defect count, same reporting.
                counts["trust_invariant"] += 1
                print(f"  [trust-invariant] property {prop_id} lost its only "
                      f"representative candidate: outcome="
                      f"{getattr(res, 'trust_outcome', None)!r} is not eligible")
                store.clear_match(prop_id)
                continue
            bw_id = format_bw_id(prop_id)
            store.log_match(
                property_id=prop_id,
                bw_id=bw_id,
                rec=rec,
                eval_res=res,
                alert_text=notify.fmt_brief_alert(_alert_payload(rec, res, bw_id, prop_id)),
                alert_status="rescored",
            )
            continue
        store.clear_match(prop_id)
        if prop_id in quarantines:
            row, res = quarantines[prop_id]
            store.log_quarantine(prop_id, row["source_listing_id"], row["site"],
                                 row["listing_id"], res.quarantine_reasons)

    # Properties whose listings have all gone expired (excluded from the
    # rescore candidates above) must not keep a stale match row: v3.3 never
    # retains a home above the hard 9,500 kr cap or one that is no longer live.
    stale = store.clear_stale_matches()
    if stale:
        print(f"  [rescore] cleared {stale} stale match(es) for fully-expired "
              f"or listing-less properties")

    after = _top_table(store)
    if not quiet:
        _print_table("AFTER rescore", after)
    print(f"\nRescored {counts['evaluated']} listings across {counts['properties']} "
          f"properties: {counts['matched']} listing evaluations matched, "
          f"{counts['quarantined']} quarantined, {counts['rejected']} rejected, "
          f"{counts['skipped']} skipped (no usable raw_data). "
          f"{counts['geocoded']} previously ungeocodable addresses resolved. "
          f"{counts['expired_visible']} expired within {config.EXPIRED_VISIBLE_DAYS} "
          f"days kept visible, {counts['card_only']} card-only listings "
          f"(detail not fetched). "
          f"{counts['trust_invariant']} trust-invariant defect(s) not logged as "
          f"matches (a match outcome without an eligible trust outcome).")
    return {"before": before, "after": after, "counts": counts, "segments": segments}


# ==============================================================================
# Targeted detail fetch (explicit; never runs by itself)
# ==============================================================================
SELECTIONS = ("newly-in-envelope", "evidence-failed", "stale", "decision-revalidation", "ids")

def parse_ids(text: Optional[str]) -> List[Tuple[str, str]]:
    """'lejebolig#1902885,boligportal#123456' -> [(site, listing_id), ...]"""
    out = []
    for token in (text or "").split(","):
        token = token.strip()
        if not token:
            continue
        if "#" not in token:
            raise SystemExit(f"--ids expects site#listing_id, got {token!r}")
        site, listing_id = token.split("#", 1)
        out.append((site.strip(), listing_id.strip()))
    return out


def select_for_decision_revalidation(
    store: Store,
    sites: Optional[List[str]] = None,
    limit: int = 0,
    now: Optional[datetime.datetime] = None,
    already_fetched: Optional[Set[Tuple[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Select decision-relevant detail pages whose last successful snapshot is >= 24h old.

    Spec 09 section 8.1:
    1. every source currently supporting a Ranked representative;
    2. every source whose property has a current saved/user action;
    3. Needs verification sources that are active/relisted, are not already known
       over the rent cap, and have no explicit hard rejection.
    """
    now = now or datetime.datetime.now()
    rows = store.local_scope_candidates(include_expired=False)
    allowed = set(sites) if sites else None
    if allowed:
        rows = [r for r in rows if r["site"] in allowed]

    already_fetched = already_fetched or set()

    cur = store.conn.execute(
        "SELECT id, property_id, site, listing_id "
        "FROM matches WHERE (quarantined = 0 OR quarantined IS NULL)"
    )
    ranked_pairs = set()
    ranked_prop_ids = set()
    for m in cur.fetchall():
        if m["site"] and m["listing_id"]:
            ranked_pairs.add((m["site"], str(m["listing_id"])))
        if m["property_id"]:
            ranked_prop_ids.add(m["property_id"])

    action_states = store.get_action_states()
    saved_prop_ids = set(action_states.keys())

    # History is an audit trail, not a permanent exclusion. A source once
    # rejected for an old price may now be an eligible or review candidate.
    current_outcomes = {r[0]: r[1] for r in store.conn.execute(
        "SELECT source_listing_id, outcome FROM evaluations WHERE is_current=1")}
    rejected_source_ids = {sid for sid, outcome in current_outcomes.items()
                           if outcome == "rejected"}
    # Older/imported rows may have a rejection but no evaluation yet. Preserve
    # that safeguard; a later current match/review verdict supersedes history.
    rejected_source_ids.update(r[0] for r in store.conn.execute(
        "SELECT DISTINCT source_listing_id FROM rejection_history")
        if r[0] not in current_outcomes)

    cur_disq = store.conn.execute(
        "SELECT normalized_address FROM building_disqualifications WHERE normalized_address IS NOT NULL "
        "AND COALESCE(state, 'active') = 'active'"
    )
    disq_addresses = {row[0] for row in cur_disq.fetchall() if row[0]}

    refresh_hours = getattr(config, "DECISION_DETAIL_REFRESH_HOURS", 24)
    rent_cap = getattr(config, "RENT_CEILING_TOTAL_MAX", 9500)

    picked: List[Dict[str, Any]] = []
    for r in rows:
        site = r["site"]
        listing_id = str(r["listing_id"])
        src_id = r["source_listing_id"]
        prop_id = r.get("property_id")

        if (site, listing_id) in already_fetched:
            continue

        # Missing, expired, out-of-window rows keep 7-day rule, not 24-hour rule
        if r.get("lifecycle_state") not in ("active", "relisted"):
            continue

        # Explicit gone rows or listings with gone/reserved markers are not decision candidates
        if r.get("explicit_gone_at"):
            continue
        body = " ".join(str(r.get("eligibility_text") or (r.get("raw") or {}).get("description") or "").lower().split())
        if any(m in body for m in GONE_MARKERS):
            continue

        # Check 24-hour freshness
        fetched = r.get("detail_fetched_at")
        if fetched:
            try:
                fetched_dt = datetime.datetime.fromisoformat(str(fetched))
                age_hours = (now - fetched_dt).total_seconds() / 3600.0
                if age_hours < refresh_hours:
                    continue
            except (ValueError, TypeError):
                pass

        # 1. Ranked representative
        is_ranked = ((site, listing_id) in ranked_pairs or
                     (prop_id in ranked_prop_ids and
                      current_outcomes.get(src_id) == "match"))

        # 2. Saved / user action property
        is_saved = (prop_id is not None and prop_id in saved_prop_ids)

        # 3. Needs verification, active/relisted, not over cap, no hard rejection
        is_needs_verification = False
        if not is_ranked and not is_saved:
            norm_addr = store.normalize_exact_address(r.get("normalized_address") or (r.get("raw") or {}).get("address"))
            has_hard_rejection = (
                src_id in rejected_source_ids
                or (norm_addr and norm_addr in disq_addresses)
                or r.get("outcome") == "rejected"
            )
            if not has_hard_rejection:
                raw = r.get("raw") or {}
                envelope = dict(raw)
                envelope.setdefault("card_class", r.get("card_class"))
                envelope.setdefault("card_price", r.get("card_price"))
                if card_envelope_state(envelope) != DETAIL_FETCH:
                    continue
                price = r.get("card_price")
                if price is None:
                    price = raw.get("rent_total") or raw.get("price") or raw.get("rent")
                is_over_cap = False
                if price is not None:
                    try:
                        if float(price) > rent_cap:
                            is_over_cap = True
                    except (ValueError, TypeError):
                        pass

                if not is_over_cap:
                    is_needs_verification = True

        if is_ranked or is_saved or is_needs_verification:
            picked.append(r)

    if limit and limit > 0:
        picked = picked[:limit]
    return picked


def select_for_detail_fetch(store: Store, selection: str,
                            ids: Optional[List[Tuple[str, str]]] = None,
                            sites: Optional[List[str]] = None,
                            limit: int = 0) -> List[Dict[str, Any]]:
    """Stored cards a targeted fetch should open a detail page for."""
    rows = store.local_scope_candidates()
    allowed = set(sites) if sites else None
    if allowed:
        rows = [r for r in rows if r["site"] in allowed]
    now = datetime.datetime.now()
    picked: List[Dict[str, Any]] = []

    if selection == "ids":
        wanted = {(site, str(lid)) for site, lid in (ids or [])}
        picked = [r for r in rows if (r["site"], str(r["listing_id"])) in wanted]
    elif selection == "newly-in-envelope":
        for r in rows:
            if r.get("detail_hash"):
                continue        # already has a detail snapshot
            if (str(r.get("eligibility_evidence_state") or "").lower() == "verified"
                    and r.get("eligibility_text")):
                # Detail evidence stored before source_snapshots existed. We
                # already hold this page, so the card is not newly in the
                # envelope; the next scheduled scan re-reads it under the
                # current parser version and gives it a snapshot.
                continue
            rec = dict(r.get("raw") or {})
            rec.setdefault("card_class", r.get("card_class"))
            rec.setdefault("card_price", r.get("card_price"))
            if card_envelope_state(rec, stored=None) == DETAIL_FETCH:
                picked.append(r)
    elif selection == "evidence-failed":
        picked = [r for r in rows
                  if str(r.get("eligibility_evidence_state") or "").lower()
                  in ("failed", "unavailable", "")
                  or r.get("eligibility_evidence_state") is None]
    elif selection == "stale":
        for r in rows:
            fetched = r.get("detail_fetched_at")
            if not fetched:
                continue
            try:
                age = (now - datetime.datetime.fromisoformat(str(fetched))).days
            except ValueError:
                continue
            if age >= config.DETAIL_REFRESH_DAYS:
                picked.append(r)
    elif selection == "decision-revalidation":
        picked = select_for_decision_revalidation(store, sites=sites, limit=limit, now=now)
    else:
        raise SystemExit(f"unknown --select {selection!r}; expected one of "
                         f"{', '.join(SELECTIONS[:-1])}")

    if limit:
        picked = picked[:limit]
    return picked


def fetch_details(store: Store, selection: str = "newly-in-envelope",
                  ids: Optional[List[Tuple[str, str]]] = None,
                  sites: Optional[List[str]] = None, limit: int = 0,
                  dry_run: bool = True, refresh_local_after: bool = True) -> Dict[str, Any]:
    """Open detail pages for stored cards you selected. Never a search page.

    Records its own scan run with kind = 'targeted' and the selection used. It
    is not a coverage scan: it never updates consecutive_misses or lifecycle for
    listings it did not select, and a listing whose page is gone is recorded as
    detail_state 'failed:gone', which is evidence for the lifecycle rule only
    once the site's next complete scan agrees.
    """
    from fetchers import (_dismiss_cookies, _new_context, boligportal_detail,
                          lejebolig_detail)
    from playwright.sync_api import sync_playwright

    detailers = {"lejebolig": lejebolig_detail, "boligportal": boligportal_detail}
    rows = select_for_detail_fetch(store, selection, ids=ids, sites=sites, limit=limit)
    if not rows:
        print(f"Targeted fetch: nothing selected by --select {selection}")
        return {"selected": 0, "fetched": 0, "failed": 0, "gone": 0, "runs": {}}

    by_site: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_site.setdefault(row["site"], []).append(row)

    print(f"Targeted detail fetch: {len(rows)} listing(s) selected by "
          f"--select {selection} across {len(by_site)} site(s). "
          f"This is the only path besides a scheduled scan that contacts a portal.")

    stations = geo.load_stations()
    segments = benchmarks.compute_benchmarks(store)
    totals = {"selected": len(rows), "fetched": 0, "failed": 0, "gone": 0, "runs": {}}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=config.HEADLESS,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        try:
            for site, site_rows in by_site.items():
                detailer = detailers.get(site)
                if detailer is None:
                    print(f"  [{site}] no detail fetcher; skipped")
                    continue
                scan_run_id = store.start_scan_run(site, kind="targeted",
                                                   selection=selection)
                totals["runs"][site] = scan_run_id
                counters = ScanCounters()
                ctx = _new_context(browser)
                page = ctx.new_page()
                _dismiss_cookies(page)
                start_t = time.time()
                try:
                    for row in site_rows:
                        rec = dict(row.get("raw") or {})
                        rec["site"] = site
                        rec["listing_id"] = row["listing_id"]
                        rec["url"] = rec.get("url") or row.get("url")
                        rec.setdefault("card_class", row.get("card_class"))
                        rec.setdefault("card_text", row.get("card_text"))
                        rec.setdefault("card_price", row.get("card_price"))
                        print(f"  [{site}#{row['listing_id']}] opening {rec['url']}")
                        outcome = scan_card(
                            rec, stations, store, scan_run_id, dry_run, segments,
                            False, page=page, detail_fetcher=detailer,
                            counters=counters, force_detail=True)
                        state = outcome.get("detail_state")
                        if state == DETAIL_FETCHED:
                            totals["fetched"] += 1
                        elif state == DETAIL_GONE:
                            totals["gone"] += 1
                        else:
                            totals["failed"] += 1
                finally:
                    store.finish_scan_run(
                        scan_run_id=scan_run_id, outcome="success",
                        duration_sec=round(time.time() - start_t, 2),
                        coverage_status="targeted",
                        cards_seen=counters.cards_seen,
                        details_attempted=counters.details_attempted,
                        details_succeeded=counters.details_succeeded,
                        details_failed=counters.details_failed,
                        details_skipped=counters.details_skipped,
                        areas_total=0, areas_ok=0, areas_failed=0,
                        decision_revalidation_selected=totals["selected"] if selection == "decision-revalidation" else None,
                        decision_revalidation_succeeded=totals["fetched"] if selection == "decision-revalidation" else None,
                        decision_revalidation_failed=totals["failed"] if selection == "decision-revalidation" else None,
                        decision_revalidation_gone=totals["gone"] if selection == "decision-revalidation" else None)
                    ctx.close()
        finally:
            browser.close()

    print(f"Targeted fetch finished: {totals['fetched']} fetched, "
          f"{totals['failed']} failed, {totals['gone']} gone, out of "
          f"{totals['selected']} selected.")
    if refresh_local_after:
        refresh_local(store, enrich=False, export=True, quiet=True)
    return totals


def backfill_eligibility(store: Store, sites: Optional[List[str]] = None,
                         limit: int = 0, dry_run: bool = True) -> Dict[str, Any]:
    """Historical name for the targeted fetch of BoligPortal evidence.

    Spec 06 section 4.6 makes it an alias of
    ``--fetch-details --select evidence-failed --sites boligportal``.
    """
    return fetch_details(store, selection="evidence-failed",
                         sites=sites or ["boligportal"], limit=limit, dry_run=dry_run)


# ==============================================================================
# Integrity audit (spec 07 section 8)
# ==============================================================================
def _link_state_lines(title: str, block: Dict[str, Any],
                      labels: List[str]) -> List[str]:
    """The heading plus one line per cross-tab key for a link-state block.

    Both renderers are fed by the same result object, so this only formats:
    every count it prints comes from `Store.audit_integrity()`, and a key's
    counts always add up to the heading's own total.
    """
    lines = [f"{title}: {block['total']} total - {block['linked']} linked, "
             f"{block['unlinked']} unlinked"]
    for label in labels:
        lines.append(f"  {label.replace('by_', 'by ')}:")
        table = block.get(label) or {}
        if not table:
            lines.append("    (none)")
            continue
        width = max(len(key) for key in table)
        for key, counts in table.items():
            lines.append(f"    {key:<{width}}  {counts['total']:6d} total - "
                         f"{counts['linked']} linked, "
                         f"{counts['unlinked']} unlinked")
    return lines


def _format_integrity_audit(result: Dict[str, Any], db_path: str) -> str:
    """The human rendering of one `Store.audit_integrity()` result.

    Every fact the JSON carries is printed here: both link-state blocks with
    their cross-tabs, the five counts, the review reasons and the verdict. The
    section 8 sentence that normal building consolidation is not an error is
    printed with the grouped count, so that number cannot read as a defect.
    """
    lines = ["BoligWatch integrity audit (spec 07 section 8) - read-only: "
             "no write, no network, no alert",
             f"Database: {db_path}",
             ""]
    lines += _link_state_lines("Sources (source_listings)", result["sources"],
                               ["by_site", "by_lifecycle"])
    lines.append("")
    lines += _link_state_lines("Current evaluations (is_current=1)",
                               result["evaluations"], ["by_outcome"])
    lines.append("")
    lines.append("Integrity")
    lines.append("  current match evaluations with no property link: "
                 f"{result['null_linked_current_matches']}")
    lines.append("  linked current match evaluations folded by normal building "
                 f"consolidation: {result['grouped_omissions']}")
    lines.append("    (spec 07 section 8: normal building consolidation is "
                 "reported, not an error)")
    lines.append("  linked current match evaluations not represented for any "
                 f"other reason: {result['abnormal_omissions']}")
    lines.append("  eligible evaluations with explicit rejection evidence: "
                 f"{result['eligible_invariant_failures']}")
    lines.append("  room / sublet / private rows present in matches: "
                 f"{result['forbidden_in_matches']}")
    lines.append("")
    lines.append("Needs verification rows by section 7.1 primary reason")
    review = result["review_by_reason"]
    if review:
        width = max(len(reason) for reason in review)
        for reason, count in sorted(review.items(),
                                    key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {reason:<{width}}  {count:6d}")
        lines.append(f"  {'total':<{width}}  {sum(review.values()):6d}")
    else:
        lines.append("  (none)")
    lines.append("")
    if result["critical"]:
        lines.append(f"Result: {len(result['critical'])} critical invariant "
                     f"violation(s): {', '.join(result['critical'])}")
    else:
        lines.append("Result: no critical invariant violated.")
    return "\n".join(lines)


# ==============================================================================
# Entrypoint
# ==============================================================================
def main():
    ap = argparse.ArgumentParser(description="BoligWatch — Copenhagen rental scanner")
    ap.add_argument("--dry-run", action="store_true", help="Dry-run mode (suppresses live alerts)")
    ap.add_argument("--live", action="store_true", help="Force live alert mode")
    ap.add_argument("--profile", default=None,
                    help="Path to a JSON search profile (default: profile.json)")
    ap.add_argument("--limit", type=int, default=0, help="Cap number of listings processed per portal")
    ap.add_argument("--sites", default=",".join(config.SITES), help="Comma-separated portals to scan")
    ap.add_argument("--rescore", action="store_true",
                    help="Re-evaluate stored listings from raw_data without fetching")
    ap.add_argument("--refresh-local", action="store_true",
                    help="Re-apply the current rules to the stored snapshot and "
                         "rebuild report.html. Contacts no portal.")
    ap.add_argument("--enrich", action="store_true",
                    help="With --refresh-local: allow the non-portal DAWA, OSRM "
                         "and Overpass lookups that fill missing derived data")
    ap.add_argument("--fetch-details", action="store_true",
                    help="Explicitly open detail pages for stored cards")
    ap.add_argument("--select", default="newly-in-envelope",
                    help="Which stored cards --fetch-details opens: "
                         "newly-in-envelope | evidence-failed | stale")
    ap.add_argument("--ids", default="",
                    help="Explicit listings for --fetch-details, e.g. "
                         "lejebolig#1902885,boligportal#123456")
    ap.add_argument("--backfill-evidence", action="store_true",
                    help="Alias of --fetch-details --select evidence-failed "
                         "--sites boligportal")
    ap.add_argument("--repair-lifecycle", action="store_true",
                    help="One-off: undo the expiries caused by the zero-card "
                         "Lejebolig scans of 12-14 September 2026")
    ap.add_argument("--repair-runs", default="40,43,46,49",
                    help="Scan-run ids the one-off repair treats as broken")
    ap.add_argument("--repair-site", default="lejebolig",
                    help="Site the one-off repair applies to")
    ap.add_argument("--refresh-features", action="store_true",
                    help="Recompute cached commute, buzz and water features")
    ap.add_argument("--repair-source-links", action="store_true",
                    help="Spec 07: relink orphaned source listings to their "
                         "properties from stored data (no network, no alerts). "
                         "Pair with --dry-run to preview without writing.")
    ap.add_argument("--serve", action="store_true", help="Launch local web dashboard viewer")
    ap.add_argument("--port", type=int, default=config.WEB_PORT, help="Port for web dashboard viewer")
    ap.add_argument("--audit-integrity", action="store_true",
                    help="Spec 07 section 8: read-only integrity audit of the "
                         "stored snapshot (no write, no network, no alert). "
                         "Exit status 2 on a critical invariant violation.")
    ap.add_argument("--json", action="store_true",
                    help="With --audit-integrity: print the stable machine JSON "
                         "on stdout and nothing else.")
    ap.add_argument("--db", default=config.DB_PATH,
                    help="Database for this command (default: config.DB_PATH); "
                         "use a copy for validation. --dry-run suppresses alerts, "
                         "but scans still write observations.")
    ap.add_argument("--suppress-alert-backlog", action="store_true",
                    help="Spec 08 section 6.3: mark every existing match row "
                         "already alerted, so the migration creates no instant "
                         "alert backlog. Writes to the production database. "
                         "Cannot be combined with --dry-run.")
    ap.add_argument("--set-profile-age", type=int, default=None,
                    help="Spec 09 section 6.1: set local search profile age (18–120)")
    ap.add_argument("--set-rank-priority", default=None,
                    help="Spec 09 section 5.1: set personal priority for a property by BW-ID")
    ap.add_argument("--kind", default=None,
                    help="With --set-rank-priority: preference kind (v3.8: preferred_locality)")
    ap.add_argument("--note", default=None,
                    help="With --set-rank-priority: optional note text")
    ap.add_argument("--clear-rank-priority", default=None,
                    help="Spec 09 section 5.1: clear personal priority for a property by BW-ID")
    args = ap.parse_args()

    # Scheduled, bot and manual writers must not interleave lifecycle updates
    # or overwrite each other's representatives. The lock is per database.
    import fcntl
    writer_lock = None
    if not args.serve:
        writer_lock = open(args.db + ".writer.lock", "a")
        try:
            fcntl.flock(writer_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another BoligWatch writer is active for this database; try again when it finishes.")
            raise SystemExit(75)

    # The audit answers from its OWN database (plan duty 2), so it runs before
    # the production `store = Store()` below: opening production would apply the
    # schema migration to it, which a read-only audit may not do, and the `--db`
    # flag exists precisely so a copy can be audited instead.
    if args.audit_integrity:
        audit_store = Store(db_path=args.db)
        result = audit_store.audit_integrity()
        if args.json:
            print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=1))
        else:
            print(_format_integrity_audit(result, args.db))
        if result["critical"]:
            raise SystemExit(2)
        return

    # Spec 08 section 10's migration step 4. It writes under the normal
    # production path, so it needs the production store; `--dry-run` is refused
    # because a suppressed count read from a dry run would not be real
    # (spec 08 section 6.3).
    if args.suppress_alert_backlog:
        if args.dry_run:
            print("--suppress-alert-backlog cannot be combined with --dry-run: "
                  "the count has to be real")
            raise SystemExit(2)
        backlog_store = Store(db_path=args.db)
        # Spec 08 section 6.4: `last_alert_at` carries the migration's own
        # completed_at, i.e. the local run whose refresh created this backlog.
        stamp = (backlog_store.last_local_run_migration().get("completed_at")
                 or datetime.datetime.now().isoformat(timespec="seconds"))
        suppressed = backlog_store.suppress_alert_backlog(stamp)
        print(f"suppressed {suppressed} match row(s)")
        return

    # Spec 09 offline commands: local search profile age and personal priority.
    # Operates on the specified database (--db) without scan, export, or alerts.
    if args.set_profile_age is not None:
        target_store = Store(db_path=args.db)
        age = args.set_profile_age
        if isinstance(age, bool) or not (18 <= age <= 120):
            print(f"Error: --set-profile-age must be an integer between 18 and 120, got {age}")
            raise SystemExit(2)
        before = target_store.get_search_profile()
        target_store.set_search_profile_age(age, evidence_source="user")
        after = target_store.get_search_profile()
        before_age = before["age_years"] if before else "none"
        after_age = after["age_years"] if after else "none"
        print("Search profile age updated:")
        print(f"  before: age_years={before_age}")
        print(f"  after:  age_years={after_age}")
        return

    if args.set_rank_priority is not None:
        target_store = Store(db_path=args.db)
        bw_input = args.set_rank_priority
        prop = target_store.resolve_property_by_bw_id(bw_input)
        if not prop:
            print(f"Error: Unknown or ambiguous property identifier '{bw_input}'")
            raise SystemExit(2)
        if args.kind != "preferred_locality":
            print(f"Error: --set-rank-priority requires --kind preferred_locality (got {args.kind!r})")
            raise SystemExit(2)
        prop_id = prop["id"]
        bw_id = prop.get("bw_id") or format_bw_id(prop_id)
        addr = prop.get("normalized_address") or "unknown"
        before_p = prop.get("rank_priority", 0)
        before_k = prop.get("rank_priority_kind")
        before_n = prop.get("rank_priority_note")
        target_store.set_rank_priority(prop_id, 1, kind=args.kind, note=args.note)
        updated = target_store.resolve_property_by_bw_id(prop_id)
        after_p = updated.get("rank_priority", 0) if updated else 1
        after_k = updated.get("rank_priority_kind") if updated else args.kind
        after_n = updated.get("rank_priority_note") if updated else args.note
        print(f"Rank priority set for {bw_id} ({addr}):")
        print(f"  before: priority={before_p}, kind={before_k!r}, note={before_n!r}")
        print(f"  after:  priority={after_p}, kind={after_k!r}, note={after_n!r}")
        return

    if args.clear_rank_priority is not None:
        target_store = Store(db_path=args.db)
        bw_input = args.clear_rank_priority
        prop = target_store.resolve_property_by_bw_id(bw_input)
        if not prop:
            print(f"Error: Unknown or ambiguous property identifier '{bw_input}'")
            raise SystemExit(2)
        prop_id = prop["id"]
        bw_id = prop.get("bw_id") or format_bw_id(prop_id)
        addr = prop.get("normalized_address") or "unknown"
        before_p = prop.get("rank_priority", 0)
        before_k = prop.get("rank_priority_kind")
        before_n = prop.get("rank_priority_note")
        target_store.clear_rank_priority(prop_id)
        updated = target_store.resolve_property_by_bw_id(prop_id)
        after_p = updated.get("rank_priority", 0) if updated else 0
        after_k = updated.get("rank_priority_kind") if updated else None
        after_n = updated.get("rank_priority_note") if updated else None
        print(f"Rank priority cleared for {bw_id} ({addr}):")
        print(f"  before: priority={before_p}, kind={before_k!r}, note={before_n!r}")
        print(f"  after:  priority={after_p}, kind={after_k!r}, note={after_n!r}")
        return

    store = Store(db_path=args.db)
    sites_arg = [x.strip() for x in args.sites.split(",") if x.strip()]

    if args.serve:
        from viewer import start_server
        start_server(port=args.port, host=config.WEB_HOST, store=store)
        return

    if args.repair_lifecycle:
        run_ids = tuple(int(x) for x in args.repair_runs.split(",") if x.strip())
        print(f"BoligWatch lifecycle repair for {args.repair_site}, "
              f"undoing the effect of scan runs {list(run_ids)}")
        preview = store.repair_zero_card_expiries(args.repair_site, run_ids, dry_run=True)
        print(f"  reference scan (last complete before run {min(run_ids)}): "
              f"{preview['reference_scan_id']}")
        print(f"  listings to restore: {preview['repaired']}")
        if not preview["repaired"]:
            print("  nothing to repair")
            return
        result = store.repair_zero_card_expiries(args.repair_site, run_ids)
        print(f"  restored {result['repaired']} listing(s) to active with zero "
              f"misses, across {len(result['properties'])} propert(y/ies)")
        refresh_local(store, enrich=False, export=True, quiet=True)
        return

    if args.repair_source_links:
        print("BoligWatch source-link repair (spec 07 6.1); "
              "no network request, no alerts")
        report = store.repair_source_links(dry_run=args.dry_run)
        print(f"  scanned {report['scanned']} null-linked source(s): "
              f"{report['repaired']} repaired, {report['unchanged']} unchanged "
              f"({'dry-run' if args.dry_run else 'written'}), "
              f"{report['unresolved']} unresolved")
        by_status = {}
        for m in report["mapping"]:
            by_status[m["merge_status"]] = by_status.get(m["merge_status"], 0) + 1
        if by_status:
            print("  merge status: " + ", ".join(
                f"{k} {v}" for k, v in sorted(by_status.items())))
        if report["unresolved_rows"]:
            print(f"  unresolved ({len(report['unresolved_rows'])}): "
                  + ", ".join(f"{r['site']}#{r['listing_id']}"
                              for r in report["unresolved_rows"][:20]))
        if not args.dry_run and report["repaired"]:
            print("  running --refresh-local so new current evaluations carry "
                  "the repaired links")
            refresh_local(store, enrich=False, export=True, quiet=True)
        return

    if args.refresh_local:
        print("BoligWatch local refresh (no portal request, no alerts)")
        refresh_local(store, enrich=args.enrich,
                      limit=args.limit, refresh_features=args.refresh_features)
        return

    if args.rescore:
        print("BoligWatch v3.5 rescore (no portal fetch, no alerts)")
        rescore(store, refresh_features=args.refresh_features, limit=args.limit)
        return

    if args.fetch_details or args.backfill_evidence:
        dry_run = True if args.dry_run else (False if args.live else not config.BOT_CONFIRMED)
        if args.backfill_evidence and not args.fetch_details:
            print("BoligWatch targeted detail fetch (--backfill-evidence alias)")
            backfill_eligibility(store, sites=["boligportal"], limit=args.limit,
                                 dry_run=dry_run)
            return
        selection = "ids" if args.ids else args.select
        print("BoligWatch targeted detail fetch")
        fetch_details(store, selection=selection, ids=parse_ids(args.ids),
                      sites=sites_arg, limit=args.limit, dry_run=dry_run)
        return

    from fetchers import _dismiss_cookies, _new_context
    from playwright.sync_api import sync_playwright

    stations = geo.load_stations()
    dry_run = True if args.dry_run else (False if args.live else not config.BOT_CONFIRMED)
    sites = sites_arg

    segments = benchmarks.compute_benchmarks(store)
    # A cold database has no market table yet: benchmarks are computed FROM the
    # observations this scan is about to create. Every listing would therefore
    # score 0 on value, silently losing 25 of the 100 points, and with value
    # flattened the best-unit tie-break degenerates too (Viola Nørløvs Gade 3
    # showed its 9.700 kr unit instead of its 9.350 kr one). Remember it here and
    # re-score once the data exists.
    cold_start = not segments

    print("BoligWatch v3.2 scan started...")
    print(f"   Office: {config.OFFICE_NAME} · Target move: {config.TARGET_MOVE_DATE}")
    print(f"   Commute bands: A <= {config.TIER_A_MAX_MIN} min, B <= {config.TIER_B_MAX_MIN} min, "
          f"C <= {config.TIER_C_MAX_MIN} min (rent <= {config.TIER_C_RENT_MAX})")
    print(f"   Market segments: {len(segments)} · {benchmarks.summary_line(segments)}")
    print(f"   Portals: {sites}")
    print(f"   Mode: {'DRY-RUN (Safe Local Mode)' if dry_run else 'LIVE BOT'}")

    runners = {"lejebolig": run_lejebolig, "boligportal": run_boligportal}

    geo.reset_geocode_health()
    coverage_by_site: Dict[str, str] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=config.HEADLESS,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        try:
            for site in sites:
                start_t = time.time()
                scan_run_id = store.start_scan_run(site, kind="scheduled")
                ctx = _new_context(browser)
                page = ctx.new_page()
                _dismiss_cookies(page)

                counters = ScanCounters()
                outcome, error_msg = "success", None
                runner_failed = False

                try:
                    runner = runners.get(site)
                    if not runner:
                        print(f"Unknown portal: {site}")
                        runner_failed = True
                    else:
                        runner(page, stations, store, scan_run_id, dry_run,
                               args.limit, segments, args.refresh_features,
                               counters=counters)
                except Exception as e:
                    outcome, error_msg = "failed", str(e)
                    runner_failed = True
                    if config.VERBOSE:
                        print(f"  [{site}] Scan failed with error: {e}")

                # Coverage is decided from what the database can prove, never
                # from a list the runner returned or from whether it raised.
                totals = store.scan_area_totals(scan_run_id)
                observed_ids = store.observed_source_ids_for_run(scan_run_id)
                areas = store.scan_areas_for_run(scan_run_id)
                empty_confirmed = bool(areas) and all(
                    a["status"] == "empty-confirmed" for a in areas)
                coverage = store.compute_coverage_status(
                    site=site, scan_run_id=scan_run_id,
                    cards_seen=counters.cards_seen or totals["cards_seen"],
                    areas_total=totals["areas_total"], areas_ok=totals["areas_ok"],
                    areas_failed=totals["areas_failed"], runner_failed=runner_failed,
                    empty_confirmed=empty_confirmed, limited=bool(args.limit))
                coverage_by_site[site] = coverage

                try:
                    transitions = store.update_lifecycle_after_scan(
                        site, observed_ids, coverage_status=coverage,
                        scan_run_id=scan_run_id)
                    notify_lifecycle_changes(store, transitions, dry_run, coverage)

                    # The portal-broken and recovery incidents key on coverage,
                    # not on whether the runner raised: a scan that returned
                    # nothing because every area failed is a broken portal even
                    # though it completed without an exception.
                    if coverage in ("complete", "empty-confirmed"):
                        was_notified, _prev = store.record_portal_success(site)
                        if was_notified and not dry_run:
                            notify.notify_recovery(site)
                    else:
                        detail = error_msg or (
                            f"coverage {coverage}: {totals['areas_failed']} of "
                            f"{totals['areas_total']} areas failed")
                        should_notify, inc_data = store.record_portal_failure(site, detail)
                        if should_notify and not dry_run:
                            notify.notify_incident(inc_data)
                except Exception as e:
                    error_msg = error_msg or str(e)
                    print(f"  [{site}] lifecycle update failed: {e}")
                finally:
                    store.finish_scan_run(
                        scan_run_id=scan_run_id,
                        outcome=outcome,
                        new_matches=0,
                        duration_sec=round(time.time() - start_t, 2),
                        failure_detail=error_msg,
                        coverage_status=coverage,
                        cards_seen=counters.cards_seen,
                        cards_new=counters.cards_new,
                        details_attempted=counters.details_attempted,
                        details_succeeded=counters.details_succeeded,
                        details_failed=counters.details_failed,
                        details_skipped=counters.details_skipped,
                        areas_total=totals["areas_total"],
                        areas_ok=totals["areas_ok"],
                        areas_failed=totals["areas_failed"],
                        decision_revalidation_selected=counters.decision_revalidation_selected,
                        decision_revalidation_succeeded=counters.decision_revalidation_succeeded,
                        decision_revalidation_failed=counters.decision_revalidation_failed,
                        decision_revalidation_gone=counters.decision_revalidation_gone,
                    )
                    print(f"[{site}] areas ok {totals['areas_ok']} / failed "
                          f"{totals['areas_failed']} of {totals['areas_total']} · "
                          f"{counters.summary()} · coverage {coverage} · "
                          f"listings observed {len(observed_ids)}")
                    ctx.close()
        finally:
            browser.close()

    segments = benchmarks.compute_benchmarks(store)
    if cold_start and segments:
        print("\n  [cold start] The market table was empty when this scan began, so "
              "value scored 0 everywhere.\n  Re-scoring against the benchmarks it "
              "just produced; no fetching, no alerts.")
    # The scan closes by applying the current rules to everything in scope, not
    # only to what it happened to fetch: a card stored without a detail page, a
    # listing that expired yesterday and a relisted one all need a verdict, and
    # the alert decisions have already been made above.
    print("\n  [scan] applying local rules to the new snapshot (no portal request)")
    refresh_local(store, enrich=False, export=True, quiet=True)

    # Spec 10 section 3.2: an upstream geocoder shutdown is seen the day it
    # happens, through the portal-style incident path.
    _check_geocode_health(store, dry_run)

    print("\n  Portal coverage this run:")
    for row in store.portal_coverage_summary():
        if row["site"] not in sites:
            continue
        print(f"    {row['site']}: {row.get('coverage_status')} at "
              f"{row.get('completed_at')} · {row.get('cards_seen')} cards · "
              f"areas {row.get('areas_ok')} ok / {row.get('areas_failed')} failed")

    stats = store.get_stats()
    rank_counts = stats.get("rank_counts") or {}
    rank_str = " / ".join(f"{l} {rank_counts.get(l, 0)}" for l in config.RANK_TIER_LETTERS)
    print("\n=======================================================")
    print("           BOLIGWATCH v3.2 SCAN SUMMARY                ")
    print("=======================================================")
    print(f"  Total Observed:         {stats['total_seen']}")
    print(f"  Unique Properties:      {stats['total_properties']}")
    print(f"  Confirmed Matches:      {stats['total_matches']}")
    print(f"   - Commute A (Close):           {stats['cat1_count']}")
    print(f"   - Commute B (Commutable):      {stats['cat2_count']}")
    print(f"   - Commute C (Far):             {stats['cat3_count']}")
    print(f"  Rank S/A/B/C/D:         {rank_str}")
    print(f"  Actionable / Intel:     {stats['actionable_count']} / {stats['intel_count']}")
    print(f"  Quarantine Backlog:     {stats['quarantine_count']} "
          f"(ungeocodable {stats['ungeocodable_count']})")
    print("=======================================================\n")


if __name__ == "__main__":
    main()
