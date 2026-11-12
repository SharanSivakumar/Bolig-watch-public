"""Acceptance tests for spec 06 - scheduled portal scan and local processing.

Numbered against `specs/housing-search/06-DAILY-SCAN-AND-LOCAL-PROCESSING.md`
section 11. Everything here is offline: the scan path is driven with fake
search and detail callables, and the tests that assert "no portal request" make
the real ones raise.
"""
import ast
import datetime
import json
import os
import pathlib

import pytest

import config
import fetchers
import filters
import geo
import notify
import run
import viewer
from conftest import with_detail_evidence
from store import Store

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


# ==============================================================================
# Harness
# ==============================================================================
def _card(site="boligportal", listing_id="1", **over):
    """A discovered search card, before any detail page is opened."""
    rec = {
        "site": site,
        "listing_id": listing_id,
        "url": f"https://example.test/{site}/{listing_id}",
        "title": "1-værelses lejlighed",
        "address": "Jorisvej 1a, 2300 København",
        "_geo_address": "Jorisvej 1a, 2300 København",
        "postcode": "2300",
        "listing_type": "lejlighed",
        "card_class": "apartment",
        "rooms": 1,
        "size_m2": 32.0,
        "rent_total": 8300,
        "card_price": 8300,
        "card_text": "Lejlighed i København, 8.300,-",
        "available_from": "2027-01-20",
        "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "deposit": 20000,
        "prepaid_rent": 0,
        "lat": 55.65,
        "lon": 12.60,
    }
    rec.update(over)
    # The v36i1 plan's duty 1a: a shared fixture stands for an advert whose own
    # canonical record carries a professional operator/project block, which is
    # what the spec 07 section 4.4 trust table needs in order to reach
    # `eligible` with no store (section 4.2 bullet 3 and section 4.3 route 2).
    # `setdefault`, so a test that sets `source_record` keeps its own.
    rec.setdefault("source_record", {
        "id": f"sb-{rec['listing_id']}",
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


def _detail_fetcher(text="Eget køkken og eget badeværelse. Ubegrænset lejeperiode.",
                    calls=None, state="verified"):
    """A stand-in for fetchers.*_detail that records what it was asked to open."""
    def fetch(page, rec):
        if calls is not None:
            calls.append((rec["site"], str(rec["listing_id"])))
        rec = dict(rec)
        rec["description"] = text
        rec["eligibility_text"] = f"{rec.get('title', '')} {text}"
        rec["eligibility_source_url"] = rec.get("url")
        rec["eligibility_evidence_state"] = state
        rec["eligibility_checked_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        return rec
    return fetch


def _never_fetch(page, rec):
    raise AssertionError(f"a detail page was opened for {rec['site']}#{rec['listing_id']}")


@pytest.fixture
def scan_env(temp_db, monkeypatch, mock_stations):
    """A store plus the geo stubs a scan needs, with alerts captured not sent."""
    sent = []
    monkeypatch.setattr(notify, "notify_match",
                        lambda payload, prefix="": sent.append(payload) or 200)
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


def _scan(store, stations, cards, scan_run_id=None, fetcher=None, counters=None):
    """Run the discovery+envelope+detail path over a list of cards."""
    counters = counters if counters is not None else run.ScanCounters()
    out = []
    for card in cards:
        out.append(run.scan_card(card, stations, store, scan_run_id, True, {}, False,
                                 page=None, detail_fetcher=fetcher or _detail_fetcher(),
                                 counters=counters))
    return out, counters


# ==============================================================================
# Net width and storage (acceptance 1-6)
# ==============================================================================
def test_01_a_card_above_the_eligibility_cap_is_stored_whatever_its_rent(scan_env):
    """Spec 06 acceptance 1, reconciled with section 4.3 and section 10.

    Section 11 uses a 10,400 kr card as its example, but section 4.3's table and
    section 10's UI copy both put the detail-fetch threshold at
    DISCOVERY_RENT_MAX (11,000 kr). Two normative statements beat one worked
    example, so 10,400 kr is inside the envelope and IS fetched; a card above
    11,000 kr is the one stored with skipped:rent. Both halves of the point the
    acceptance test is making still hold: the card is stored whatever its rent,
    and a card outside the envelope records why it was not opened.
    """
    store, _sent, stations = scan_env
    calls = []
    _scan(store, stations,
          [_card(listing_id="10400", rent_total=10400, card_price=10400),
           _card(listing_id="11500", rent_total=11500, card_price=11500)],
          fetcher=_detail_fetcher(calls=calls))

    inside = store.get_source_listing("boligportal", "10400")
    assert inside is not None, "a card above the 9,500 kr cap must still be stored"
    assert inside["detail_state"] == "fetched"
    assert inside["card_price"] == 10400

    outside = store.get_source_listing("boligportal", "11500")
    assert outside is not None
    assert outside["detail_state"] == "skipped:rent"
    assert store.latest_snapshot(outside["id"], "card") is not None
    assert store.latest_snapshot(outside["id"], "detail") is None
    assert calls == [("boligportal", "10400")]


def test_02_three_room_card_is_stored_skipped_unknown_rooms_is_fetched(scan_env):
    store, _sent, stations = scan_env
    calls = []
    _scan(store, stations,
          [_card(listing_id="three", rooms=3), _card(listing_id="unknown", rooms=None)],
          fetcher=_detail_fetcher(calls=calls))

    assert store.get_source_listing("boligportal", "three")["detail_state"] == "skipped:rooms"
    assert store.get_source_listing("boligportal", "unknown")["detail_state"] == "fetched"
    assert calls == [("boligportal", "unknown")]


def test_03_lejebolig_card_without_the_old_keywords_is_stored_and_fetched(scan_env):
    store, _sent, stations = scan_env
    # "Hyggelig bolig tæt på Metro" matched none of the four phrases the old
    # gate required, so it was discarded before anything was stored.
    assert fetchers.classify_lejebolig_card("Hyggelig bolig tæt på Metro") == "unknown"
    assert fetchers.classify_lejebolig_card("Værelse i delt lejlighed") == "room"

    calls = []
    _scan(store, stations, [
        _card(site="lejebolig", listing_id="odd", card_class="unknown"),
        _card(site="lejebolig", listing_id="room", card_class="room"),
    ], fetcher=_detail_fetcher(calls=calls))

    assert store.get_source_listing("lejebolig", "odd")["detail_state"] == "fetched"
    assert store.get_source_listing("lejebolig", "odd")["card_class"] == "unknown"
    room = store.get_source_listing("lejebolig", "room")
    assert room["detail_state"] == "skipped:card_class"
    assert room["card_class"] == "room"
    assert calls == [("lejebolig", "odd")]


def test_04_raising_the_rent_cap_recovers_listings_with_no_fetch(scan_env, monkeypatch):
    store, _sent, stations = scan_env
    _scan(store, stations, [_card(listing_id="9750", rent_total=9750, card_price=9750)])

    run.refresh_local(store, export=False, quiet=True)
    ev = store.current_evaluation(store.get_source_listing("boligportal", "9750")["id"])
    assert ev["outcome"] == "rejected"
    assert "9750" in (ev["reason"] or "")

    # The rule changes; nothing is fetched; the listing comes back.
    monkeypatch.setattr(config, "RENT_CAP_MAX", 9800)
    monkeypatch.setattr(config, "AFFORDABILITY_CURVE", [(7500, 35), (9800, 0)])
    monkeypatch.setattr(fetchers, "boligportal_search", _never_fetch)
    monkeypatch.setattr(fetchers, "boligportal_detail", _never_fetch)
    run.refresh_local(store, export=False, quiet=True)

    ev = store.current_evaluation(store.get_source_listing("boligportal", "9750")["id"])
    assert ev["outcome"] == "match", ev["reason"]
    assert ev["rules_version"] != config.RULES_VERSION_TAG
    assert store.get_match_row(ev["property_id"]) is not None


def test_05_an_unchanged_page_makes_one_snapshot_and_two_observations(scan_env):
    store, _sent, stations = scan_env
    fetcher = _detail_fetcher()
    _scan(store, stations, [_card(listing_id="same")], scan_run_id=1, fetcher=fetcher)
    source_id = store.get_source_listing("boligportal", "same")["id"]
    # The refresh window would normally skip the second read; force the fetch so
    # the question under test is snapshot dedup, not the envelope.
    run.scan_card(_card(listing_id="same"), stations, store, 2, True, {}, False,
                  page=None, detail_fetcher=fetcher, force_detail=True)

    assert store.snapshot_count(source_id, "detail") == 1
    observations = store.conn.execute(
        "SELECT detail_snapshot_id, scan_run_id FROM observations "
        "WHERE source_listing_id=? ORDER BY id", (source_id,)).fetchall()
    assert len(observations) == 2
    assert observations[0]["detail_snapshot_id"] == observations[1]["detail_snapshot_id"]

    # A changed page adds a second snapshot and leaves the first untouched.
    first = store.latest_snapshot(source_id, "detail")
    run.scan_card(_card(listing_id="same"), stations, store, 3, True, {}, False,
                  page=None, force_detail=True,
                  detail_fetcher=_detail_fetcher(text="Eget køkken, nu med opvaskemaskine."))
    assert store.snapshot_count(source_id, "detail") == 2
    kept = store.conn.execute("SELECT payload FROM source_snapshots WHERE id=?",
                              (first["id"],)).fetchone()
    assert kept["payload"] == first["payload"], "stored payloads are immutable"


def test_06_a_changed_card_is_refetched_inside_the_refresh_window(scan_env):
    store, _sent, stations = scan_env
    _scan(store, stations, [_card(listing_id="moved", rent_total=8300, card_price=8300)])
    stored = store.get_source_listing("boligportal", "moved")
    assert stored["detail_state"] == "fetched"

    unchanged = _card(listing_id="moved", rent_total=8300, card_price=8300)
    assert run.card_envelope_state(
        unchanged, stored, run.Store.snapshot_hash(run.card_payload(unchanged))
    ) == "skipped:fresh"

    cheaper = _card(listing_id="moved", rent_total=7900, card_price=7900)
    assert run.card_envelope_state(
        cheaper, stored, run.Store.snapshot_hash(run.card_payload(cheaper))
    ) == "fetch"


# ==============================================================================
# Coverage and lifecycle (acceptance 7-13)
# ==============================================================================
def _finish(store, run_id, site, coverage, cards_seen=0):
    totals = store.scan_area_totals(run_id)
    store.finish_scan_run(run_id, "success", coverage_status=coverage,
                          cards_seen=cards_seen, areas_total=totals["areas_total"],
                          areas_ok=totals["areas_ok"], areas_failed=totals["areas_failed"])


def test_07_a_failed_area_makes_the_run_partial_and_costs_no_misses(scan_env):
    store, _sent, stations = scan_env
    _scan(store, stations, [_card(site="lejebolig", listing_id="live")])
    source_id = store.get_source_listing("lejebolig", "live")["id"]

    run_id = store.start_scan_run("lejebolig")
    store.record_scan_area(run_id, "lejebolig", "valby", "ok", cards_seen=4)
    store.record_scan_area(run_id, "lejebolig", "amager", "failed",
                           error="Timeout 60000ms exceeded")
    totals = store.scan_area_totals(run_id)
    coverage = store.compute_coverage_status(
        "lejebolig", run_id, cards_seen=4, areas_total=totals["areas_total"],
        areas_ok=totals["areas_ok"], areas_failed=totals["areas_failed"])
    assert coverage == "partial"

    areas = store.scan_areas_for_run(run_id)
    failed = [a for a in areas if a["status"] == "failed"]
    assert len(failed) == 1 and "Timeout" in failed[0]["error"]

    store.update_lifecycle_after_scan("lejebolig", [], coverage_status=coverage,
                                      scan_run_id=run_id)
    row = store.conn.execute(
        "SELECT lifecycle_state, consecutive_misses FROM source_listings WHERE id=?",
        (source_id,)).fetchone()
    assert row["lifecycle_state"] == "active"
    assert row["consecutive_misses"] == 0


def test_08_zero_cards_only_counts_when_the_page_said_so(scan_env):
    store, _sent, stations = scan_env
    _scan(store, stations, [_card(site="lejebolig", listing_id="live")])
    source_id = store.get_source_listing("lejebolig", "live")["id"]

    unconfirmed = store.start_scan_run("lejebolig")
    store.record_scan_area(unconfirmed, "lejebolig", "valby", "empty-unconfirmed")
    totals = store.scan_area_totals(unconfirmed)
    status = store.compute_coverage_status(
        "lejebolig", unconfirmed, 0, totals["areas_total"], totals["areas_ok"],
        totals["areas_failed"], empty_confirmed=False)
    assert status == "empty-unconfirmed"
    store.update_lifecycle_after_scan("lejebolig", [], coverage_status=status)
    assert store.conn.execute(
        "SELECT consecutive_misses FROM source_listings WHERE id=?",
        (source_id,)).fetchone()[0] == 0

    confirmed = store.start_scan_run("lejebolig")
    store.record_scan_area(confirmed, "lejebolig", "valby", "empty-confirmed")
    totals = store.scan_area_totals(confirmed)
    status = store.compute_coverage_status(
        "lejebolig", confirmed, 0, totals["areas_total"], totals["areas_ok"],
        totals["areas_failed"], empty_confirmed=True)
    assert status == "empty-confirmed"
    store.update_lifecycle_after_scan("lejebolig", [], coverage_status=status)
    assert store.conn.execute(
        "SELECT consecutive_misses FROM source_listings WHERE id=?",
        (source_id,)).fetchone()[0] == 1


def test_09_a_collapsed_card_count_is_partial_not_complete(temp_db):
    store = temp_db
    first = store.start_scan_run("boligportal")
    store.record_scan_area(first, "boligportal", "url", "ok", cards_seen=280)
    _finish(store, first, "boligportal", "complete", cards_seen=280)

    second = store.start_scan_run("boligportal")
    store.record_scan_area(second, "boligportal", "url", "ok", cards_seen=100)
    totals = store.scan_area_totals(second)
    assert store.compute_coverage_status(
        "boligportal", second, 100, totals["areas_total"], totals["areas_ok"],
        totals["areas_failed"]) == "partial"

    third = store.start_scan_run("boligportal")
    store.record_scan_area(third, "boligportal", "url", "ok", cards_seen=200)
    totals = store.scan_area_totals(third)
    assert store.compute_coverage_status(
        "boligportal", third, 200, totals["areas_total"], totals["areas_ok"],
        totals["areas_failed"]) == "complete"


def test_10_three_complete_misses_expire_three_partial_ones_do_not(scan_env):
    store, _sent, stations = scan_env
    _scan(store, stations, [_card(site="lejebolig", listing_id="a")])
    _scan(store, stations, [_card(site="boligportal", listing_id="b")])
    a = store.get_source_listing("lejebolig", "a")["id"]
    b = store.get_source_listing("boligportal", "b")["id"]

    for _ in range(3):
        store.update_lifecycle_after_scan("lejebolig", [], coverage_status="complete")
    for _ in range(3):
        store.update_lifecycle_after_scan("boligportal", [], coverage_status="partial")

    expired = store.conn.execute(
        "SELECT lifecycle_state, consecutive_misses, expired_at FROM source_listings "
        "WHERE id=?", (a,)).fetchone()
    assert expired["lifecycle_state"] == "expired"
    assert expired["consecutive_misses"] == 3
    assert expired["expired_at"] is not None

    untouched = store.conn.execute(
        "SELECT lifecycle_state, consecutive_misses FROM source_listings WHERE id=?",
        (b,)).fetchone()
    assert untouched["lifecycle_state"] == "active"
    assert untouched["consecutive_misses"] == 0


def test_11_listings_observed_is_counted_from_the_observations(scan_env):
    store, _sent, stations = scan_env
    run_id = store.start_scan_run("boligportal")
    _scan(store, stations,
          [_card(listing_id="a"), _card(listing_id="b"), _card(listing_id="c")],
          scan_run_id=run_id)
    # The caller passes a number that is simply wrong, exactly as run 52 did.
    store.finish_scan_run(run_id, "success", listings_observed=0, duration_sec=1.0,
                          coverage_status="complete")
    stored = store.get_scan_run(run_id)
    assert stored["listings_observed"] == 3
    assert len(store.observed_source_ids_for_run(run_id)) == 3


def test_12_an_expired_listing_stays_visible_for_fourteen_days(scan_env):
    store, _sent, stations = scan_env
    _scan(store, stations, [_card(site="lejebolig", listing_id="recent"),
                            _card(site="lejebolig", listing_id="ancient",
                                  address="Testvej 9, 2300 København",
                                  _geo_address="Testvej 9, 2300 København")])
    recent = store.get_source_listing("lejebolig", "recent")["id"]
    ancient = store.get_source_listing("lejebolig", "ancient")["id"]
    now = datetime.datetime.now()
    store.conn.execute(
        "UPDATE source_listings SET lifecycle_state='expired', expired_at=? WHERE id=?",
        ((now - datetime.timedelta(days=3)).isoformat(timespec="seconds"), recent))
    store.conn.execute(
        "UPDATE source_listings SET lifecycle_state='expired', expired_at=? WHERE id=?",
        ((now - datetime.timedelta(days=30)).isoformat(timespec="seconds"), ancient))
    store.conn.commit()

    in_scope = {r["source_listing_id"] for r in store.local_scope_candidates()}
    assert recent in in_scope
    assert ancient not in in_scope

    run.refresh_local(store, export=False, quiet=True)
    ev = store.current_evaluation(recent)
    assert ev is not None and ev["outcome"] in ("match", "quarantined", "rejected")
    rows = viewer.listing_rows(store)
    recent_rows = [r for r in rows if r.get("expired_recently")]
    for row in recent_rows:
        assert row["last_seen_days_ago"] is not None


def test_12b_an_expired_unit_never_outranks_a_live_one(scan_env):
    store, _sent, stations = scan_env
    # Two listings of the same property: the expired one is cheaper, so without
    # the lifecycle rank it would take the representative row.
    _scan(store, stations, [_card(site="lejebolig", listing_id="live", rent_total=8300,
                                  card_price=8300),
                            _card(site="boligportal", listing_id="dead", rent_total=7900,
                                  card_price=7900)])
    dead = store.get_source_listing("boligportal", "dead")["id"]
    store.conn.execute(
        "UPDATE source_listings SET lifecycle_state='expired', expired_at=? WHERE id=?",
        (datetime.datetime.now().isoformat(timespec="seconds"), dead))
    store.conn.commit()

    run.refresh_local(store, export=False, quiet=True)
    prop_id = store.get_source_listing("lejebolig", "live")["property_id"]
    match = store.get_match_row(prop_id)
    assert match is not None
    assert match["listing_id"] == "live", "an expired unit must never hold the row"


def test_13_the_one_off_repair_restores_the_zero_card_expiries(scan_env):
    store, _sent, stations = scan_env
    good = store.start_scan_run("lejebolig")
    _scan(store, stations, [_card(site="lejebolig", listing_id="x"),
                            _card(site="lejebolig", listing_id="y")],
          scan_run_id=good)
    _finish(store, good, "lejebolig", "complete", cards_seen=2)

    # Four scans that saw nothing and called themselves a success.
    broken = []
    for _ in range(4):
        run_id = store.start_scan_run("lejebolig")
        store.finish_scan_run(run_id, "success", coverage_status="complete", cards_seen=0)
        store.update_lifecycle_after_scan("lejebolig", [], coverage_status="complete")
        broken.append(run_id)

    states = store.conn.execute(
        "SELECT lifecycle_state, consecutive_misses FROM source_listings "
        "WHERE site='lejebolig'").fetchall()
    assert all(s["lifecycle_state"] == "expired" for s in states)

    preview = store.repair_zero_card_expiries("lejebolig", tuple(broken), dry_run=True)
    assert preview["repaired"] == 2
    assert preview["reference_scan_id"] == good
    assert store.conn.execute(
        "SELECT COUNT(*) FROM source_listings WHERE lifecycle_state='expired'"
    ).fetchone()[0] == 2, "a dry run must change nothing"

    result = store.repair_zero_card_expiries("lejebolig", tuple(broken))
    assert result["repaired"] == 2
    restored = store.conn.execute(
        "SELECT lifecycle_state, consecutive_misses, expired_at FROM source_listings "
        "WHERE site='lejebolig'").fetchall()
    assert all(r["lifecycle_state"] == "active" and r["consecutive_misses"] == 0
               and r["expired_at"] is None for r in restored)
    # Idempotent: a second run has nothing left to find.
    assert store.repair_zero_card_expiries("lejebolig", tuple(broken))["repaired"] == 0


def test_13b_the_repair_leaves_genuinely_stale_listings_expired(scan_env):
    store, _sent, stations = scan_env
    old_run = store.start_scan_run("lejebolig")
    _scan(store, stations, [_card(site="lejebolig", listing_id="gone")],
          scan_run_id=old_run)
    _finish(store, old_run, "lejebolig", "complete", cards_seen=1)

    good = store.start_scan_run("lejebolig")
    _scan(store, stations, [_card(site="lejebolig", listing_id="fresh")], scan_run_id=good)
    _finish(store, good, "lejebolig", "complete", cards_seen=1)

    broken = []
    for _ in range(4):
        run_id = store.start_scan_run("lejebolig")
        store.finish_scan_run(run_id, "success", coverage_status="complete", cards_seen=0)
        store.update_lifecycle_after_scan("lejebolig", [], coverage_status="complete")
        broken.append(run_id)

    result = store.repair_zero_card_expiries("lejebolig", tuple(broken))
    assert result["repaired"] == 1
    assert store.get_source_listing("lejebolig", "fresh")["lifecycle_state"] == "active"
    # "gone" was already missing before the broken scans; it stays expired.
    assert store.get_source_listing("lejebolig", "gone")["lifecycle_state"] == "expired"


# ==============================================================================
# Fetch boundary (acceptance 14-19)
# ==============================================================================
LOCAL_MODULES = ("filters", "store", "viewer", "digest", "bot", "benchmarks", "tiers")


def test_14a_no_local_module_imports_a_portal_client():
    """A static check, so a new import is caught before it can ever run."""
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = {}
    for name in LOCAL_MODULES:
        tree = ast.parse((root / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for module in names:
                if module in ("fetchers", "playwright"):
                    offenders.setdefault(name, []).append(module)
    assert not offenders, f"local modules must not import a portal client: {offenders}"


def test_14b_refresh_local_succeeds_with_every_portal_client_raising(scan_env, monkeypatch):
    store, _sent, stations = scan_env
    _scan(store, stations, [_card(listing_id="a"), _card(listing_id="b")])

    def boom(*a, **kw):
        raise AssertionError("local processing contacted a portal")

    for name in ("lejebolig_search", "lejebolig_detail", "boligportal_search",
                 "boligportal_detail", "_goto"):
        monkeypatch.setattr(fetchers, name, boom)
    monkeypatch.setitem(__import__("sys").modules, "playwright", None)

    result = run.refresh_local(store, export=False, quiet=True)
    assert result["counts"]["evaluated"] == 2


def test_15_repeated_local_refreshes_change_no_lifecycle_state(scan_env):
    store, _sent, stations = scan_env
    _scan(store, stations, [_card(listing_id="a")])
    before_runs = store.conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
    before = dict(store.conn.execute(
        "SELECT last_seen_at, consecutive_misses, lifecycle_state, last_complete_scan_id "
        "FROM source_listings WHERE site='boligportal' AND listing_id='a'").fetchone())

    for _ in range(3):
        run.refresh_local(store, export=False, quiet=True)

    after = dict(store.conn.execute(
        "SELECT last_seen_at, consecutive_misses, lifecycle_state, last_complete_scan_id "
        "FROM source_listings WHERE site='boligportal' AND listing_id='a'").fetchone())
    assert before == after
    assert store.conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == before_runs
    assert store.conn.execute("SELECT COUNT(*) FROM local_runs").fetchone()[0] == 3


@pytest.mark.real_features
def test_16_an_uncached_coordinate_stays_pending_without_enrich(temp_db, monkeypatch):
    import features
    calls = []
    monkeypatch.setattr(features, "urban_buzz",
                        lambda lat, lon: calls.append("overpass"))
    monkeypatch.setattr(geo, "commute_to_office",
                        lambda lat, lon: calls.append("osrm") or (None, None))
    monkeypatch.setattr(features, "water_distance",
                        lambda lat, lon: calls.append("water") or (None, None, None))

    monkeypatch.setattr(features, "CACHE_ONLY", True)
    feats = features.get_place_features(55.71, 12.41, store=temp_db)
    assert feats["features_pending"] == 1
    assert feats["commute_min"] is None
    assert calls == [], "no external lookup may happen without --enrich"

    monkeypatch.setattr(geo, "CACHE_ONLY", True)
    assert geo.geocode_chain("k|Nonsens 1, 9999", "Nonsens 1, 9999") == (None, None, None, None)
    assert geo.road_route_to_norreport(55.71, 12.41) is None


def test_17_a_targeted_fetch_touches_only_the_listings_it_selected(scan_env, monkeypatch):
    store, _sent, stations = scan_env
    _scan(store, stations, [_card(listing_id="wanted", rent_total=10400, card_price=10400),
                            _card(listing_id="other")])
    other_before = dict(store.get_source_listing("boligportal", "other"))

    picked = run.select_for_detail_fetch(store, "ids", ids=[("boligportal", "wanted")])
    assert [p["listing_id"] for p in picked] == ["wanted"]

    targeted = store.start_scan_run("boligportal", kind="targeted", selection="ids")
    row = picked[0]
    rec = dict(row["raw"])
    rec["site"], rec["listing_id"] = "boligportal", "wanted"
    run.scan_card(rec, stations, store, targeted, True, {}, False, page=None,
                  detail_fetcher=_detail_fetcher(), force_detail=True)
    store.finish_scan_run(targeted, "success", coverage_status="targeted")

    assert store.get_scan_run(targeted)["kind"] == "targeted"
    assert store.get_source_listing("boligportal", "wanted")["detail_state"] == "fetched"
    other_after = dict(store.get_source_listing("boligportal", "other"))
    for field in ("lifecycle_state", "consecutive_misses", "detail_state", "last_seen_at"):
        assert other_after[field] == other_before[field]
    # A targeted run is not coverage, so the portal-health view ignores it.
    assert store.latest_scheduled_scan("boligportal") is None


def test_18_a_local_score_change_sends_nothing(scan_env, monkeypatch):
    store, sent, stations = scan_env
    _scan(store, stations, [_card(listing_id="a")])
    sent.clear()
    monkeypatch.setattr(config, "SCORE_BUZZ_PTS", 12.0)
    run.refresh_local(store, export=False, quiet=True)
    assert sent == [], "local processing is silent"


def test_18b_a_card_only_observation_cannot_announce_a_price_drop(scan_env):
    store, _sent, stations = scan_env
    _scan(store, stations, [_card(listing_id="p", rent_total=9000, card_price=9000)])
    source_id = store.get_source_listing("boligportal", "p")["id"]

    _obs, changes = store.record_observation(
        source_listing_id=source_id, property_id=None, scan_run_id=9,
        rec={"rent_total": 8000}, eval_res=None, detail_state="skipped:rent")
    assert "price_change" not in changes

    _obs, changes = store.record_observation(
        source_listing_id=source_id, property_id=None, scan_run_id=10,
        rec={"rent_total": 8000}, eval_res=None, detail_state="fetched")
    assert changes["price_change"]["type"] == "drop"


def test_19_the_export_works_with_every_portal_unreachable(scan_env, monkeypatch, tmp_path):
    store, _sent, stations = scan_env
    _scan(store, stations, [_card(listing_id="a")])
    run.refresh_local(store, export=False, quiet=True)

    def boom(*a, **kw):
        raise AssertionError("the export contacted a portal")
    for name in ("lejebolig_search", "boligportal_search", "_goto"):
        monkeypatch.setattr(fetchers, name, boom)

    out = tmp_path / "report.html"
    viewer.export_static_html(str(out), store=store)
    html = out.read_text(encoding="utf-8")
    assert "Portal data:" in html and "Local rules:" in html and "Snapshot age:" in html


# ==============================================================================
# Kitchen (acceptance 20-26)
# ==============================================================================
@pytest.fixture
def near_metro(monkeypatch):
    monkeypatch.setattr(geo, "nearest_metro",
                        lambda lat, lon, st: ("Rådhuspladsen", 300.0, 400.0, 5))
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda lat, lon, st: ("Rådhuspladsen", 300.0, 400.0, 5, "metro"))


def _kitchen(text, **over):
    rec = {"site": "lejebolig", "listing_id": "k", "listing_type": "lejlighed",
           "card_class": "apartment", "eligibility_text": text,
           "eligibility_evidence_state": "verified",
           "eligibility_source_url": "https://example.test/k",
           "eligibility_checked_at": "2026-09-14T10:00:00"}
    rec.update(over)
    return filters.analyse_kitchen(rec, rooms_verified=over.pop("rooms_verified", True))


def test_20_private_plus_shared_kitchen_passes_with_the_caveat():
    k = _kitchen("Studio with private kitchen and shared kitchen in the building.")
    assert k.decision == "pass"
    assert k.private_kitchen_present is True
    assert k.shared_kitchen_present is True
    assert k.kitchen_state == "verified"
    assert "shared kitchen" in (k.caveat or "")
    assert k.evidence_source == "detail"
    assert k.evidence_excerpt and len(k.evidence_excerpt) <= 200


def test_21_shared_kitchen_only_is_rejected_as_a_shared_kitchen():
    k = _kitchen("Shared kitchen only.")
    assert k.decision == "reject"
    assert k.reason == "shared kitchen"
    assert k.kitchen_state == "rejected"
    assert _kitchen("You share the kitchen with other tenants.").decision == "reject"


def test_22_a_shared_bathroom_is_rejected_as_a_shared_bathroom(mock_stations, near_metro):
    rec = with_detail_evidence({
        "site": "lejebolig", "listing_id": "b", "listing_type": "lejlighed",
        "card_class": "apartment", "rooms": 1, "size_m2": 32.0, "rent_total": 8000,
        "deposit": 20000, "prepaid_rent": 0, "available_from": "2027-01-20",
        "rental_period": "ubegrænset", "rental_period_raw": 0,
        "lat": 55.65, "lon": 12.60,
        "description": "Eget køkken, men fælles bad på gangen.",
    })
    res = filters.evaluate_listing(rec, mock_stations)
    assert res.rejected is True
    assert res.rejection_reason == "shared bathroom"
    assert "kitchen" not in res.rejection_reason


def test_23_a_flatshare_stays_rejected_despite_a_private_kitchenette(mock_stations, near_metro):
    rec = with_detail_evidence({
        "site": "lejebolig", "listing_id": "s", "listing_type": "lejlighed",
        "card_class": "room", "rooms": 1, "size_m2": 20.0, "rent_total": 6000,
        "deposit": 12000, "prepaid_rent": 0, "available_from": "2027-01-20",
        "rental_period": "ubegrænset", "rental_period_raw": 0,
        "lat": 55.65, "lon": 12.60,
        "description": "Værelse i bofællesskab med eget tekøkken.",
    })
    res = filters.evaluate_listing(rec, mock_stations)
    assert res.rejected is True
    # Spec 07 section 4.4 row 1 rejects a room/share advert before the v3.5
    # housing classifier runs, and section 6.2 fixes the reason wording, which
    # section 7.1 ranks first. The v3.5 wording is still admitted for a record
    # whose evidence reaches the housing gate first.
    assert res.rejection_reason in ("shared housing",
                                    "room rental / not self-contained")


def test_24_poppelstykket_1902885(mock_stations, near_metro):
    fixture = json.loads(
        (FIXTURES / "lejebolig_1902885_poppelstykket.json").read_text(encoding="utf-8"))

    def rec_for(text, rent):
        rec = {
            "site": "lejebolig", "listing_id": fixture["listing_id"],
            "url": fixture["url"], "title": "Lejlighed på Poppelstykket",
            "listing_type": fixture["listing_type"], "card_class": fixture["card_class"],
            "rooms": fixture["rooms"], "size_m2": fixture["size_m2"],
            "rent_total": rent, "deposit": fixture["deposit"],
            "prepaid_rent": fixture["prepaid_rent"],
            "available_from": "2027-01-20", "rental_period": "ubegrænset",
            "rental_period_raw": 0, "lat": 55.65, "lon": 12.60,
            "address": fixture["address"], "description": text,
            "eligibility_text": text, "eligibility_evidence_state": "verified",
            "eligibility_source_url": fixture["url"],
            "eligibility_checked_at": "2026-09-14T10:00:00",
        }
        # Duty 1a of the v36i1 plan, as on `_card`: this record is built here
        # rather than by `with_detail_evidence`, so it carries the advert's own
        # operator/project block itself (spec 07 sections 4.2 bullet 3, 4.3
        # route 2), keyed on the fixture's listing id.
        rec.setdefault("source_record", {
            "id": f"sb-{rec['listing_id']}",
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

    # The page as Lejebolig actually publishes it names the building's shared
    # kitchens and never states a private one. v3.12 (spec 11 section 4A) holds
    # it as Kitchen-quarantined instead of ranking it with an unconfirmed badge.
    as_published = filters.analyse_kitchen(rec_for(fixture["detail_text_as_published"], 9350),
                                           rooms_verified=True)
    assert as_published.decision == "quarantine"
    assert as_published.kitchen_state == "quarantined"
    assert as_published.shared_kitchen_present is True
    assert as_published.private_kitchen_present is None

    # With the private kitchen confirmed, spec 06 section 8.1 requires it to
    # pass the kitchen gate, and the whole evaluation to pass with it.
    confirmed = rec_for(fixture["detail_text_with_private_kitchen"], 9350)
    kitchen = filters.analyse_kitchen(confirmed, rooms_verified=True)
    assert kitchen.decision == "pass"
    assert kitchen.private_kitchen_present is True
    assert kitchen.shared_kitchen_present is True
    assert kitchen.kitchen_state == "verified"

    res = filters.evaluate_listing(confirmed, mock_stations)
    assert res.rejected is False, res.rejection_reason
    assert res.kitchen_state == "verified"
    assert res.private_kitchen_present is True

    # The rent the page actually quotes is over the cap, and that is the reason
    # it is out, not the kitchen.
    over_cap = filters.evaluate_listing(
        rec_for(fixture["detail_text_with_private_kitchen"], fixture["rent_total"]),
        mock_stations)
    assert over_cap.rejected is True
    assert "rent" in over_cap.rejection_reason


def test_25_a_sibling_listings_evidence_cannot_rescue_this_one():
    sibling_text = "Eget køkken og eget badeværelse i lejligheden."
    subject = {"site": "lejebolig", "listing_id": "subject",
               "listing_type": "ukendt", "card_class": "unknown",
               "eligibility_text": "Adgang til fælleskøkken.",
               "eligibility_evidence_state": "verified",
               "eligibility_source_url": "https://example.test/subject",
               "eligibility_checked_at": "2026-09-14T10:00:00",
               # A neighbour's copy, deliberately hung on the record.
               "sibling_description": sibling_text}
    k = filters.analyse_kitchen(subject, rooms_verified=True)
    assert k.decision == "reject"
    sources = [source for source, _text in filters.evidence_texts(subject)]
    assert sources == ["detail"], "only this listing's own snapshot is evidence"


def test_26_the_apartment_fallback_is_labelled_and_never_used_for_a_room():
    apartment = _kitchen("Lys bolig tæt på metroen.")
    assert apartment.decision == "pass"
    assert apartment.kitchen_state == "assumed"
    assert "assumed private" in (apartment.caveat or "")

    # v3.11 (spec 10 section 5.2): a missing kitchen verdict ranks with the
    # Kitchen-unconfirmed badge. It is not assumed for a room.
    as_room = _kitchen("Lys bolig tæt på metroen.", card_class="room")
    assert as_room.decision == "pass"
    assert as_room.kitchen_state == "unverified"

    unknown_type = _kitchen("Lys bolig tæt på metroen.", listing_type="ukendt",
                            card_class="unknown")
    assert unknown_type.decision == "pass"

    unverified_rooms = filters.analyse_kitchen(
        {"site": "t", "listing_id": "1", "listing_type": "lejlighed",
         "card_class": "apartment", "eligibility_text": "Lys bolig.",
         "eligibility_evidence_state": "verified"}, rooms_verified=False)
    assert unverified_rooms.decision == "pass"
    assert unverified_rooms.kitchen_state == "unverified"


# ==============================================================================
# Envelope and freshness plumbing
# ==============================================================================
def test_envelope_reads_only_the_discovery_constants():
    assert config.DISCOVERY_RENT_MAX == 11000
    assert config.DISCOVERY_RENT_MAX > config.RENT_CAP_MAX
    at_ceiling = _card(rent_total=config.DISCOVERY_RENT_MAX,
                       card_price=config.DISCOVERY_RENT_MAX)
    assert run.card_envelope_state(at_ceiling) == "fetch"
    above = _card(rent_total=config.DISCOVERY_RENT_MAX + 1,
                  card_price=config.DISCOVERY_RENT_MAX + 1)
    assert run.card_envelope_state(above) == "skipped:rent"
    assert run.card_envelope_state(_card(listing_type="værelse")) == "skipped:type"
    assert run.card_envelope_state(_card(card_price=None, rent_total=None)) == "fetch"


def test_portal_search_urls_use_the_discovery_ceiling():
    for url in config.BOLIGPORTAL_SEARCH_URLS:
        assert f"max_monthly_rent={config.DISCOVERY_RENT_MAX}" in url
        assert "max_monthly_rent=9500" not in url


def test_the_dashboard_states_coverage_rules_version_and_snapshot_age(scan_env):
    store, _sent, stations = scan_env
    run_id = store.start_scan_run("boligportal")
    _scan(store, stations, [_card(listing_id="a")], scan_run_id=run_id)
    store.record_scan_area(run_id, "boligportal", "url", "ok", cards_seen=1)
    _finish(store, run_id, "boligportal", "complete", cards_seen=1)
    run.refresh_local(store, export=False, quiet=True)

    stats = viewer.dashboard_stats(store)
    coverage = {row["site"]: row for row in stats["portal_coverage"]}
    assert coverage["boligportal"]["coverage_status"] == "complete"
    assert coverage["boligportal"]["cards_seen"] == 1
    assert stats["local_rules"]["rules_version"] == config.eligibility_rules_version()
    assert stats["local_rules"]["applied_at"] is not None
    assert stats["snapshot_observed_at"] is not None


def test_a_card_only_row_says_so_on_the_dashboard(scan_env):
    store, _sent, stations = scan_env
    _scan(store, stations, [_card(listing_id="rent", rent_total=11500, card_price=11500)],
          fetcher=_never_fetch)
    run.refresh_local(store, export=False, quiet=True)
    source = store.get_source_listing("boligportal", "rent")
    assert source["detail_state"] == "skipped:rent"
    reason = viewer._CARD_ONLY_REASONS["skipped:rent"]
    assert "card only" in reason and "11.000" in reason


def test_raw_data_is_merged_not_overwritten(scan_env):
    """A discovery-only pass must not wipe what a detail fetch established."""
    store, _sent, stations = scan_env
    _scan(store, stations, [_card(listing_id="m")])
    source_id = store.get_source_listing("boligportal", "m")["id"]
    before = json.loads(store.get_source_listing("boligportal", "m")["raw_data"])
    assert before.get("rent_total") == 8300

    card_only = _card(listing_id="m", rent_total=12000, card_price=12000)
    card_only.pop("deposit")
    card_only.pop("rent_total")
    run.scan_card(card_only, stations, store, None, True, {}, False, page=None,
                  detail_fetcher=_never_fetch)
    after = json.loads(store.get_source_listing("boligportal", "m")["raw_data"])
    assert store.get_source_listing("boligportal", "m")["detail_state"] == "skipped:rent"
    assert after["rent_total"] == 8300, "a card-only pass must not blank the rent"
    assert after["deposit"] == 20000
    assert store.snapshot_count(source_id, "detail") == 1


# ==============================================================================
# Housing evidence scope (pitfall 10 / spec 06 section 8.4)
# ==============================================================================
def test_a_sibling_advert_in_the_page_tail_cannot_condemn_this_listing():
    """Spec 06 section 8.4 works in both directions.

    A BoligPortal detail page ends with adverts for other homes. One flat was
    rejected as student housing because "Trorod Kollegiet" appeared in that
    tail, which is no more this listing's evidence than a neighbour's private
    kitchen would be.
    """
    page = ("Lys 1-vaerelses lejlighed paa Testvej. Eget koekken og eget bad. "
            "Ubegraenset lejeperiode. " + "Beskrivelse af boligen. " * 12 +
            "Andre boliger 2 vaer. lejlighed paa 57 m2 vedbaek, troeroed kollegiet 7.631 kr.")
    rec = {"site": "boligportal", "listing_id": "t", "title": "1-vaerelses lejlighed",
           "url": "https://example.test/t", "eligibility_text": page,
           "eligibility_evidence_state": "verified"}
    assert "kollegie" not in filters.own_listing_text(page)
    assert filters.housing_disqualification_evidence(rec) is None

    # The same phrase inside the listing's own copy still rejects it.
    rec["eligibility_text"] = "Moderne kollegie paa Frederiksberg. " + page
    assert filters.housing_disqualification_evidence(rec)["reason"] == "student housing"


def test_a_fact_table_answering_no_is_not_a_restriction():
    """"Kun for studerende: Nej" says the opposite of what it contains.

    BoligPortal renders this label on essentially every page. Matching the label
    alone rejected 222 of 491 stored listings as student housing.
    """
    def verdict(text):
        return filters.housing_disqualification_evidence(
            {"site": "boligportal", "listing_id": "f", "title": "1-vaerelses",
             "url": "https://example.test/f", "eligibility_text": text,
             "eligibility_evidence_state": "verified"})

    no = ("Boligtype lejlighed stoerrelse 37 m2 vaerelser 1 moebleret ja "
          "delevenlig nej seniorvenlig nej kun for studerende nej altan nej")
    assert verdict(no) is None
    yes = no.replace("kun for studerende nej", "kun for studerende ja")
    assert verdict(yes)["reason"] == "student housing"
    assert verdict("NB: ingen krav om studiekort. Husdyr er ikke tilladt.") is None
    assert verdict("Der er krav om studiekort ved indflytning.")["reason"] == "student housing"


def test_an_age_limit_needs_an_age_unit():
    """"from over 60 countries" is a co-living brag, not an age restriction."""
    def verdict(text):
        return filters.housing_disqualification_evidence(
            {"site": "boligportal", "listing_id": "a", "title": "1-vaerelses",
             "url": "https://example.test/a", "eligibility_text": text,
             "eligibility_evidence_state": "verified"})

    assert verdict("The community has grown to over 200 professionals from "
                   "over 60 countries.") is None
    assert verdict("Boligen udlejes kun til beboere over 60 aar.")["reason"] \
        == "elderly / senior housing"
    assert verdict("Udlejes for 55+ beboere.")["reason"] == "elderly / senior housing"


def test_a_limited_run_can_never_count_as_complete_coverage(temp_db):
    """--limit stops looking on purpose, so it proves nothing about absence."""
    run_id = temp_db.start_scan_run("boligportal")
    temp_db.record_scan_area(run_id, "boligportal", "url", "ok", cards_seen=15)
    totals = temp_db.scan_area_totals(run_id)
    assert temp_db.compute_coverage_status(
        "boligportal", run_id, 15, totals["areas_total"], totals["areas_ok"],
        totals["areas_failed"], limited=True) == "partial"
    assert temp_db.compute_coverage_status(
        "boligportal", run_id, 15, totals["areas_total"], totals["areas_ok"],
        totals["areas_failed"], limited=False) == "complete"


def test_a_query_whose_cards_were_all_seen_already_is_covered_not_empty():
    """Coverage is about what the page showed, not about how much was new.

    BoligPortal's bbox URL overlaps the city URL almost completely. Counting
    only new ids made a query that loaded 21 pages of 374 results report itself
    as empty-unconfirmed, which is the exact confusion this spec exists to end.
    """
    from unittest.mock import MagicMock

    def result(rid):
        return {"rentable_id": rid, "title": f"Studio {rid}", "street_name": "Testvej",
                "street_number": str(rid), "postal_code": 2200,
                "category": "rental_apartment", "rooms": 1, "size_m2": 32.0,
                "monthly_rent": 7500, "monthly_rent_extra_costs": 500,
                "rental_period": 0, "url": f"/lejlighed/{rid}", "description": "x"}

    shared = [result(1), result(2)]
    page = MagicMock()
    page.locator.return_value.count.return_value = 0
    page.inner_text.return_value = ""
    body = {"props": {"page_props": {"results": shared, "limit": 18, "result_count": 2}}}
    page.evaluate.side_effect = [body, body] + [
        {"props": {"page_props": {"results": []}}}] * 10

    cards = fetchers.boligportal_search(page, search_urls=["https://x/?a=1", "https://x/?b=2"])
    assert [c["listing_id"] for c in cards] == ["1", "2"], "still deduplicated"
    assert [a["status"] for a in cards.areas] == ["ok", "ok"]
    assert [a["cards_seen"] for a in cards.areas] == [2, 2]


def test_a_rule_change_supersedes_a_building_verdict_it_no_longer_supports(scan_env):
    """Spec 06 section 7.2: superseded, never deleted.

    99 Copenhagen buildings were condemned as student housing because
    BoligPortal renders "Kun for studerende  Nej" on every page. The evidence is
    still a real portal page, so the record survives, marked as no longer
    supported by the rules.
    """
    store, _sent, stations = scan_env
    fact_table = ("Boligtype lejlighed vaerelser 1 moebleret ja delevenlig nej "
                  "kun for studerende nej altan nej")
    _scan(store, stations, [_card(site="lejebolig", listing_id="v")],
          fetcher=_detail_fetcher(text=fact_table))
    address = store.conn.execute(
        "SELECT normalized_address FROM properties LIMIT 1").fetchone()[0]

    # A verdict recorded by the old, over-eager classifier.
    store.record_building_disqualification(address, "student housing",
                                           "https://example.test/v",
                                           "kun for studerende")
    normalized = store.normalize_exact_address(address)
    assert store.conn.execute(
        "SELECT COUNT(*) FROM building_disqualifications WHERE normalized_address=? "
        "AND (state IS NULL OR state='active')", (normalized,)).fetchone()[0] == 1

    counts = store.supersede_unsupported_building_disqualifications()
    assert counts["superseded"] == 1
    record = store.conn.execute(
        "SELECT * FROM building_disqualifications WHERE normalized_address=?",
        (normalized,)).fetchone()
    assert record["state"] == "superseded"
    assert record["superseded_at"] is not None
    assert record["matched_text"] == "kun for studerende", "the evidence is kept"

    # And a building the rules still condemn is held.
    store.conn.execute(
        "UPDATE source_listings SET eligibility_text=? WHERE site='lejebolig'",
        ("Moderne ungdomsbolig med krav om studieaktivitet.",))
    store.conn.execute("UPDATE building_disqualifications SET state='active' "
                       "WHERE normalized_address=?", (normalized,))
    store.conn.commit()
    assert store.supersede_unsupported_building_disqualifications()["held"] == 1


def test_legacy_building_verdicts_are_backfilled_active(tmp_path):
    path = tmp_path / "legacy-building-state.db"
    store = Store(db_path=str(path))
    now = datetime.datetime.now().isoformat(timespec="seconds")
    store.conn.execute("""
        INSERT INTO building_disqualifications(
            normalized_address, reason, first_seen_at, last_seen_at, state)
        VALUES(?,?,?,?,NULL)
    """, ("testvej 1", "student housing", now, now))
    store.conn.commit()
    store.conn.close()

    reopened = Store(db_path=str(path))
    row = reopened.conn.execute(
        "SELECT state FROM building_disqualifications WHERE normalized_address=?",
        ("testvej 1",)).fetchone()
    assert row["state"] == "active"
    reopened.conn.close()


def test_rules_version_covers_every_evaluation_constant(monkeypatch):
    expected = {
        "RENT_CAP_MAX", "MOVE_IN_COST_MAX", "TIER_A_MAX_MIN",
        "TIER_B_MAX_MIN", "TIER_C_MAX_MIN", "TIER_C_RENT_MAX",
        "AFFORDABILITY_CURVE", "TRANSIT_METRO_CURVE", "RP_S_TRAIN_CURVE",
        "COMMUTE_CURVE", "SCORE_BUZZ_PTS", "SCORE_WATER_PTS",
        "SCORE_WATER_FULL_M", "SCORE_WATER_ZERO_M", "WATER_LAKE_WEIGHT",
        "SCORE_CENTRE_PTS", "SCORE_CENTRE_FULL_M", "SCORE_CENTRE_ZERO_M",
        "SCORE_DATE_PTS", "DATE_TOLERANCE_DAYS", "GEO_PRECISION_PENALTY_MIN",
        "GEO_PRECISION_POSTCODE_PENALTY_MIN", "COMMUTE_FALLBACK_BASE_MIN",
        "COMMUTE_FALLBACK_MIN_PER_KM", "NEVER_ZONE_SCORE_PENALTY",
        "GEO_MAX_DISTANCE_M",
        "RANK_TIER_BANDS", "TARGET_MOVE_DATE", "PREFERRED_WINDOW_START",
        "PREFERRED_WINDOW_END", "ACTIONABLE_BEFORE_DAYS",
        "ACTIONABLE_AFTER_DAYS", "UNKNOWN_LIVE_BEFORE_DAYS",
        "POST_TARGET_HORIZON_DAYS", "EXPIRED_VISIBLE_DAYS",
    }
    assert expected <= set(config.ELIGIBILITY_CONSTANTS)
    # v3.11 (spec 10 section 4.1): the retired hard-rule constants must have
    # left the hashed set so every listing re-evaluates.
    retired = {"COMMUTE_MAX_MIN", "NORREPORT_ROAD_DISTANCE_MAX_M",
               "NORMAL_METRO_WALK_MAX_M", "RP_S_TRAIN_MAX_M", "RP_RENT_MAX"}
    assert not (retired & set(config.ELIGIBILITY_CONSTANTS))

    before = config.eligibility_rules_version()
    monkeypatch.setattr(config, "WATER_LAKE_WEIGHT", config.WATER_LAKE_WEIGHT + 0.01)
    assert config.eligibility_rules_version() != before


def test_a_price_drop_needs_two_scan_runs(scan_env, monkeypatch):
    store, sent, _stations = scan_env
    monkeypatch.setattr(config, "ALERT_PRICE_DROP_SHORTLIST_ONLY", False)
    captured = []
    monkeypatch.setattr(notify, "fmt_price_drop",
                        lambda bw, old, new: captured.append((old, new)) or "drop")

    same_run = {"type": "drop", "diff": -900, "old_rent": 9000, "new_rent": 8100,
                "old_scan_run_id": 7, "new_scan_run_id": 7}
    run.handle_price_change(store, 1, "BW-0000001", same_run, "actionable", True)
    assert captured == [], "two readings inside one run are one look at the market"

    across_runs = dict(same_run, old_scan_run_id=7, new_scan_run_id=8)
    run.handle_price_change(store, 1, "BW-0000001", across_runs, "actionable", True)
    assert captured == [(9000, 8100)]
