"""Plan 2 dashboard API, export and scheduling regression tests."""
import datetime
import gzip
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request

import pytest
from bs4 import BeautifulSoup

import config
import digest
import run
import viewer
from conftest import EMOJI_RE, _candidate
from store import format_bw_id, reason_rank


def _seed_match(alert_env):
    store, _, stations = alert_env
    run.process_candidate(_candidate(listing_id="VIEWER-1"), stations, store, None, dry_run=True)
    return store


def _request(url, method="GET", payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _server(store):
    server, port = viewer.make_server(store, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{port}"


def test_listing_rows_include_dashboard_score_fields(alert_env):
    store = _seed_match(alert_env)
    row = viewer.listing_rows(store)[0]
    assert isinstance(row["score_breakdown"], dict)
    assert "features_pending" in row
    assert row["commute_band"] == row["tier"]


def test_listing_rows_keep_an_exact_listing_comparison_snapshot(alert_env):
    store, _, stations = alert_env
    candidate = _candidate(listing_id="COMPARE-1", rent_total=8400,
                           deposit=25200, prepaid_rent=8400,
                           available_from="2027-01-18")
    candidate["first_month_charge"] = 8400
    candidate["move_in_cost"] = 42000
    candidate["move_in_cost_verified"] = True
    run.process_candidate(candidate, stations, store, None, dry_run=True)

    row = viewer.listing_rows(store)[0]
    snapshot = row["comparison"]
    assert snapshot["subject_scope"] == "source_listing"
    assert snapshot["subject_id"].startswith("source:")
    assert snapshot["rent_total"] == 8400
    assert snapshot["move_in_cost"] == 42000
    assert snapshot["available_from_parsed"] == "2027-01-18"
    assert snapshot["window_view"] == "unknown"
    assert snapshot["major_street"] == "Testgade"
    assert snapshot["dataset_id"]


def test_export_static_html_has_static_mode_and_freshness(alert_env, tmp_path):
    store = _seed_match(alert_env)
    path = tmp_path / "report.html"
    viewer.export_static_html(str(path), store=store)
    content = path.read_text(encoding="utf-8")
    assert "const STATIC = true;" in content
    assert "Exported" in content
    assert "__SCORE_PARTS__" not in content
    assert "__TIER_LEGEND__" not in content
    assert not EMOJI_RE.search(content)

    # The local server reads this sidecar instead of rebuilding the payload.
    sidecar = tmp_path / "report.html.data.json"
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["rows"]


def test_next_scheduled_boundaries():
    tz = datetime.timezone.utc
    assert viewer.next_scheduled(datetime.datetime(2026, 9, 7, 9, tzinfo=tz), ("10:00", "16:00")) == datetime.datetime(2026, 9, 7, 10, tzinfo=tz)
    assert viewer.next_scheduled(datetime.datetime(2026, 9, 7, 12, tzinfo=tz), ("10:00", "16:00")) == datetime.datetime(2026, 9, 7, 16, tzinfo=tz)
    assert viewer.next_scheduled(datetime.datetime(2026, 9, 7, 17, tzinfo=tz), ("10:00", "16:00")) == datetime.datetime(2026, 9, 8, 10, tzinfo=tz)
    assert viewer.next_scheduled(datetime.datetime(2026, 9, 7, 10, tzinfo=tz), ("10:00", "16:00")) == datetime.datetime(2026, 9, 7, 16, tzinfo=tz)


def test_is_loopback_accepts_only_local_forms():
    assert viewer._is_loopback("127.0.0.1")
    assert viewer._is_loopback("::1")
    assert viewer._is_loopback("::ffff:127.0.0.1")
    assert not viewer._is_loopback("192.0.2.1")


def test_get_last_scan_runs_returns_latest_for_site(temp_db):
    first = temp_db.start_scan_run("test")
    temp_db.finish_scan_run(first, "success", duration_sec=0.1, coverage_status="complete")
    later = temp_db.start_scan_run("test")
    temp_db.finish_scan_run(later, "success", duration_sec=0.1, coverage_status="partial")
    rows = temp_db.get_last_scan_runs()
    assert len(rows) == 1
    assert rows[0]["site"] == "test"
    assert rows[0]["outcome"] == "success"
    assert rows[0]["coverage_status"] == "partial"
    # listings_observed is counted from the observations carrying the run id,
    # never from a number the caller passed in (spec 06 acceptance 11).
    assert rows[0]["listings_observed"] == 0


def test_dashboard_server_stats_and_actions(alert_env):
    store = _seed_match(alert_env)
    row = viewer.listing_rows(store)[0]
    server, thread, base = _server(store)
    try:
        status, stats = _request(base + "/api/stats")
        assert status == 200
        assert {"rank_counts", "pending_count", "last_scans", "next_scan_at"} <= set(stats)
        with urllib.request.urlopen(base + "/", timeout=3) as response:
            assert "const STATIC = false;" in response.read().decode("utf-8")

        status, action = _request(base + "/api/action", "POST", {"property_id": row["property_id"], "action": "save", "note": ""})
        assert status == 200 and action["shortlist_state"] == "save"
        _, listings = _request(base + "/api/listings")
        assert next(item for item in listings if item["property_id"] == row["property_id"])["shortlist_state"] == "save"

        _, dismiss = _request(base + "/api/action", "POST", {"property_id": row["property_id"], "action": "dismiss", "note": ""})
        assert dismiss["shortlist_state"] == "dismiss"
        _, restore = _request(base + "/api/action", "POST", {"property_id": row["property_id"], "action": "unsave", "note": ""})
        assert restore["shortlist_state"] == ""

        for payload, expected in (({"property_id": row["property_id"], "action": "bad"}, 400),
                                  ({"property_id": 999999, "action": "save"}, 404)):
            try:
                _request(base + "/api/action", "POST", payload)
            except urllib.error.HTTPError as err:
                assert err.code == expected
            else:
                raise AssertionError("expected HTTP error")
        try:
            req = urllib.request.Request(base + "/api/action", data=b"not json", method="POST")
            urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as err:
            assert err.code == 400
        else:
            raise AssertionError("expected invalid JSON rejection")
        try:
            _request(base + "/api/action")
        except urllib.error.HTTPError as err:
            assert err.code == 405
        else:
            raise AssertionError("expected method rejection")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_template_has_no_emoji():
    assert not EMOJI_RE.search(viewer.HTML_TEMPLATE)


# ==============================================================================
# Spec 07 section 7 - the four-dataset dashboard payload (v36u1)
# ==============================================================================
# One stored source per dataset, driven through the real scan/local entry path
# so the views are decided by the trust gate and the section 7 partition, not by
# a fixture that asserts its own conclusion.

DASH_TRUSTED = "DASH-T1"
DASH_REVIEW = "DASH-R1"
DASH_GONE = "DASH-G1"
DASH_EXCLUDED = "DASH-X1"

# The detail label that makes a listing a room rental (spec 07 sections 3.2/4.1).
ROOM_TEXT = "Boligtype Værelse. Værelse på Frederiksberg tæt på byen."


def _review_record(**over):
    """The review dataset's fixture: an unknown UNIT type.

    Spec 08 section 4.3 row 5 makes the unit type the only route to `review`, so
    popping the operator block is no longer enough - `_candidate`'s own title
    carries the `1-værelses` card marker and its detail text the `Boligtype
    Lejlighed` label, which is row 4's `eligible`. The neutral title and
    `listing_type` (no card marker, no room marker) leave the unit type
    unanswered, which is the review route.
    """
    over.setdefault("title", "Lejebolig ved Noerrebro")
    over.setdefault("listing_type", "bolig")
    # v3.11 (spec 10 section 5.1): the only remaining hold is Q1/Q2. Clear the
    # rent so this fixture is the Q1 "rent unknown" case that stays in Needs
    # verification.
    over.setdefault("rent_total", None)
    over.setdefault("card_price", None)
    over.setdefault("rent_base", None)
    over.setdefault("utilities_amount", None)
    rec = _candidate(**over)
    rec.pop("source_record", None)
    return rec


def _room_record(**over):
    """The same fixture as a room advert: detail `Boligtype Værelse`."""
    rec = _candidate(**over)
    rec["description"] = ROOM_TEXT
    rec["eligibility_text"] = ROOM_TEXT
    return rec


def _seed_payload_fixtures(store, stations):
    """One source per dataset, one distinct property each.

    Each fixture gets its own house-numbered address AND its own coordinates:
    four records at one address would be one property (and the geo merge inside
    `resolve_property` collapses coincident coordinates within 25 m), which is
    the wrong shape for a one-view-per-source test.
    """
    def _placed(builder, listing_id, address, offset):
        rec = builder(listing_id=listing_id, address=address)
        rec["lat"] = 55.65 + offset * 0.01
        rec["lon"] = 12.60 + offset * 0.01
        rec["url"] = f"https://example.test/test/{listing_id}"
        return rec

    with run._NoNetwork():
        for builder, listing_id, address, offset in (
                (_candidate, DASH_TRUSTED, "Trustedvej 1, 2300 København", 0),
                (_review_record, DASH_REVIEW, "Reviewvej 2, 2300 København", 1),
                (_review_record, DASH_GONE, "Gonevej 3, 2300 København", 2),
                (_room_record, DASH_EXCLUDED, "Excludedvej 4, 2300 København", 3)):
            run.process_candidate(_placed(builder, listing_id, address, offset),
                                  stations, store, None, dry_run=True)

    # Recently gone is a lifecycle state, not a verdict: the source expired
    # inside `config.EXPIRED_VISIBLE_DAYS`.
    ago = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat(
        timespec="seconds")
    store.conn.execute(
        "UPDATE source_listings SET lifecycle_state='expired', expired_at=?, "
        "last_seen_at=? WHERE site='test' AND listing_id=?",
        (ago, ago, DASH_GONE))
    store.conn.commit()


def _dataset_keys(payload, name):
    return {f"{row.get('site')}#{row.get('listing_id')}" for row in payload[name]}


def test_the_four_datasets_hold_one_view_per_source(alert_env):
    """Duty 4: exact-one partitions (spec 07 section 9.4 item 1)."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    payload = viewer.dashboard_payload(store)

    assert len(store.conn.execute("SELECT id FROM properties").fetchall()) == 4, \
        "the four fixtures are four distinct properties"
    keys = {name: _dataset_keys(payload, name) for name in viewer.DASHBOARD_DATASET_KEYS}
    for name, listing_id in (("trusted", DASH_TRUSTED), ("review", DASH_REVIEW),
                             ("gone", DASH_GONE), ("excluded", DASH_EXCLUDED)):
        key = f"test#{listing_id}"
        assert key in keys[name], f"{key} is missing from the {name} dataset"
        for other, other_keys in keys.items():
            if other != name:
                assert key not in other_keys, f"{key} is also in the {other} dataset"

    # A review row carries no S-D badge anywhere in the payload (section 7 item 2).
    for row in payload["review"]:
        for forbidden in ("rank", "tier", "display_tier", "badge"):
            assert forbidden not in row, "a non-ranked row carries no S-D badge"


def test_dashboard_payload_has_the_pinned_keys_and_default_view(alert_env):
    """Duty 1/2: the pinned key set, Trusted blocks as the explicit default."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    payload = viewer.dashboard_payload(store)

    assert list(payload) == ["stats", "trusted", "review", "gone", "excluded",
                             "counts", "integrity", "migration", "default_view",
                             "map", "rows"], \
        "the pinned key set, plus v36u3's `migration` key (spec 07 section 7.3)"
    assert payload["default_view"] == "trusted"
    assert payload["rows"] is payload["trusted"], \
        "`rows` stays an alias of the trusted dataset until v36u2 rewires the page"
    assert viewer.DEFAULT_DASHBOARD_VIEW == "trusted"
    assert set(payload["counts"]) == {
        "trusted_blocks", "needs_verification", "recently_gone", "excluded",
        "trusted_scope", "source_scope"}


def test_payload_counts_are_clearly_scoped_to_their_datasets(alert_env):
    """Duty 1/4: grouped trusted count, source-level counts for the other three."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    payload = viewer.dashboard_payload(store)
    counts = payload["counts"]

    assert counts["trusted_scope"] == "grouped"
    assert counts["source_scope"] == ["review", "gone", "excluded"]
    assert counts["trusted_blocks"] == len(payload["trusted"])
    assert counts["needs_verification"] == len(payload["review"])
    assert counts["recently_gone"] == len(payload["gone"])
    assert counts["excluded"] == len(payload["excluded"])
    assert counts["trusted_blocks"] == 1, "one grouped block, not one row per source"
    for name in ("needs_verification", "recently_gone", "excluded"):
        assert counts[name] == 1, "one source each"


def test_the_map_carries_the_trusted_rows_only(alert_env):
    """Duty 2: map pins are built from the trusted dataset (section 7).

    The partition is per SOURCE, so the claim is source-level: the trusted
    representative is pinned, and no source that sits in Needs verification,
    Recently gone or Excluded is. A building whose representative is trusted can
    still carry a sibling in Excluded - one property may span two views, one
    source may not.
    """
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    payload = viewer.dashboard_payload(store)

    features = payload["map"]["matches"]["features"]
    pinned_ids = {feature["properties"]["property_id"] for feature in features}
    assert pinned_ids == {row["property_id"] for row in payload["trusted"]}, \
        "every pin is a trusted row and every trusted row is pinned"
    pinned_urls = {feature["properties"]["url"] for feature in features
                   if feature["properties"].get("url")}
    assert pinned_urls == {row["url"] for row in payload["trusted"] if row.get("url")}
    for name in ("review", "gone", "excluded"):
        for row in payload[name]:
            assert row["url"] not in pinned_urls, \
                f"the {name} source {row['url']} must not be pinned"


def test_only_the_trusted_row_reaches_the_digest(alert_env):
    """Duty 2: the digest input stays the trusted path (dismissal included)."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)

    with run._NoNetwork():
        text = digest.generate_daily_digest(store=store)

    counts_line = [line for line in text.splitlines()
                   if line.startswith("Actionable ")][0]
    assert counts_line.startswith("Actionable 1"), \
        "the digest's ranked inputs hold the trusted row only"
    for address in ("Reviewvej", "Gonevej", "Excludedvej"):
        assert address not in text, f"{address} must not reach the digest"


def test_a_room_row_is_excluded_and_never_trusted_or_in_review(alert_env):
    """Spec 07 section 9.4 item 3: a room rejects, visible under Excluded."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    payload = viewer.dashboard_payload(store)

    assert f"test#{DASH_EXCLUDED}" in _dataset_keys(payload, "excluded")
    room = next(row for row in payload["excluded"]
                if row["listing_id"] == DASH_EXCLUDED)
    assert room["primary_reason"] == config.TRUST_REASON_ROOM
    assert room["outcome"] == "rejected"
    assert room["url"] is not None and room["address"]
    assert f"test#{DASH_EXCLUDED}" not in _dataset_keys(payload, "trusted")
    assert f"test#{DASH_EXCLUDED}" not in _dataset_keys(payload, "review")


def test_a_null_linked_current_match_raises_the_integrity_warning(alert_env):
    """Duty 3 / section 9.4 item 4: the operator warning follows the counts."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)

    clean = viewer.dashboard_payload(store)
    assert clean["integrity"]["warning"] is False, \
        "a tree with no null link and no forbidden match row shows no warning"

    # Section 3.4's defect: a current `match` evaluation with no property link.
    store.conn.execute("UPDATE evaluations SET property_id=NULL "
                       "WHERE is_current=1 AND outcome='match'")
    store.conn.commit()

    payload = viewer.dashboard_payload(store)
    assert payload["integrity"]["null_linked_current_matches"] == 1
    assert payload["integrity"]["warning"] is True
    assert set(payload["integrity"]) == {"null_linked_current_matches",
                                         "forbidden_trust_in_matches",
                                         "explicit_gone_in_matches", "warning"}


def test_a_source_in_two_datasets_fails_naming_the_listing(alert_env):
    """Duty 3: the invariant fails on duplicate placement, by `site#listing_id`.

    Section 7 forbids inserting a quarantined or rejected row into `matches`
    merely to make it visible, so that shortcut is the defect driven here.
    """
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    paved = next(row for row in store.excluded_rows()
                 if row["listing_id"] == DASH_EXCLUDED)

    store.conn.execute(
        "INSERT INTO matches(property_id, site, listing_id, bw_id, tier, category, "
        "score, rent_total, commute_min, relevance, address, quarantined, "
        "is_responsible_pick, complex_key) "
        "VALUES(?,?,?,?,'A',1,70.0,8300,15,'actionable',?,0,0,NULL)",
        (paved["property_id"], paved["site"], paved["listing_id"],
         format_bw_id(paved["property_id"]), "Excludedvej 4, 2300 København"))
    store.conn.commit()

    with pytest.raises(ValueError) as err:
        viewer.dashboard_payload(store)
    assert "test#DASH-X1" in str(err.value), \
        "the failure names the source that is in two datasets"


def test_dashboard_cache_serves_cached_bytes_and_patches_actions(alert_env):
    """The live page gets pre-serialized bytes, and a shortlist action patches them."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    cache = viewer.DashboardCache(store)

    raw, compressed = cache.snapshot()
    payload = json.loads(raw)
    assert payload["trusted"], "the seeded fixture has no trusted row"
    assert gzip.decompress(compressed) == raw

    pid = payload["trusted"][0]["property_id"]
    cache.apply_action(pid, "save", "from the test")
    raw2, compressed2 = cache.snapshot()
    refreshed = json.loads(raw2)
    assert refreshed["trusted"][0]["shortlist_state"] == "save"
    assert refreshed["trusted"][0]["shortlist_note"] == "from the test"
    feature = [f for f in refreshed["map"]["matches"]["features"]
               if int(f["properties"].get("property_id") or 0) == pid][0]
    assert feature["properties"]["shortlist_state"] == "save"
    assert gzip.decompress(compressed2) == raw2


def test_live_and_static_dashboards_serve_the_identical_payload(alert_env, tmp_path):
    """Duty 1: both dashboard modes serve one payload, built by one function."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)

    server, thread, base = _server(store)
    try:
        status, live = _request(base + "/api/dashboard")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert status == 200

    path = tmp_path / "report.html"
    viewer.export_static_html(str(path), store=store)
    content = path.read_text(encoding="utf-8")
    found = re.search(r"DATA = (\{.*?\});\nasync function loadData\(\)", content, re.S)
    assert found, "the inlined payload was not found in the static export"
    static = json.loads(found.group(1))

    for key in viewer.DASHBOARD_DATASET_KEYS + ("counts", "integrity", "migration",
                                               "default_view", "map", "rows"):
        assert static[key] == live[key], f"{key} differs between the two modes"
    live_stats, static_stats = dict(live["stats"]), dict(static["stats"])
    assert live_stats.pop("exported_at", None) is None, \
        "the live payload is not an export"
    assert static_stats.pop("exported_at", None), "the export stamps exported_at"
    assert static_stats == live_stats


def test_shortlist_actions_are_bound_to_the_trusted_rows(alert_env):
    """Duty 2: a property that is only in a non-trusted dataset is not actionable."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    payload = viewer.dashboard_payload(store)
    trusted_id = payload["trusted"][0]["property_id"]
    review_id = payload["review"][0]["property_id"]

    assert viewer.trusted_property_ids(payload["trusted"]) == [trusted_id]
    assert review_id not in viewer.trusted_property_ids(payload["trusted"])

    server, thread, base = _server(store)
    try:
        status, action = _request(base + "/api/action", "POST",
                                  {"property_id": trusted_id, "action": "save",
                                   "note": ""})
        assert status == 200 and action["shortlist_state"] == "save"
        try:
            _request(base + "/api/action", "POST",
                     {"property_id": review_id, "action": "save", "note": ""})
        except urllib.error.HTTPError as err:
            assert err.code == 404
        else:
            raise AssertionError("a non-trusted property must not be actionable")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert store.get_action_states().get(review_id) is None, \
        "the rejected action wrote nothing"


def test_the_payload_block_is_emoji_free_and_still_valid_json(alert_env, tmp_path):
    """The page script's contract: JSON in, no emoji, `STATIC` still switched."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    path = tmp_path / "report.html"
    viewer.export_static_html(str(path), store=store)
    content = path.read_text(encoding="utf-8")
    assert "let DATA = null;" in content
    assert not EMOJI_RE.search(content)


# ==============================================================================
# Spec 07 section 7 - four rendered views, one search and the section 7.1
# disclosures (v36u2)
# ==============================================================================
# The pinned mechanics: render the static export into `tmp_path`, parse it with
# BeautifulSoup, read the inlined `DATA` JSON out of the `<script>` text, and
# assert on the shell's element ids/classes and data attributes plus the payload
# those attributes read. The rendered (post-JavaScript) DOM is driven by
# `PHASE_v36/v36u2_dashboard_controls/probe_rendered_views.py`.

def _static_document(tmp_path, store):
    path = tmp_path / "report.html"
    viewer.export_static_html(str(path), store=store)
    return BeautifulSoup(path.read_text(encoding="utf-8"), "lxml")


def _inlined_payload(doc):
    """The `DATA` JSON the page script reads, straight from the `<script>` text."""
    for script in doc.find_all("script"):
        found = re.search(r"DATA = (\{.*?\});\nasync function loadData\(\)",
                          script.get_text(), re.S)
        if found:
            return json.loads(found.group(1))
    raise AssertionError("the inlined payload was not found in the static export")


def _page_function(start_marker, end_marker):
    """One function body out of the page script, for a wiring assertion."""
    source = viewer.HTML_TEMPLATE
    start = source.index(start_marker)
    return source[start:source.index(end_marker, start)]


def test_the_static_shell_has_a_view_container_and_control_per_dataset(alert_env, tmp_path):
    """Duty 1: four views, one container and one labelled control each."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    doc = _static_document(tmp_path, store)

    root = doc.find("html")
    assert root["data-default-view"] == viewer.DEFAULT_DASHBOARD_VIEW == "trusted", \
        "the root names Trusted blocks as the view the dashboard opens on"

    containers = doc.select(".view")
    assert {container.get("id") for container in containers} == {
        f"view-{key}" for key in viewer.DASHBOARD_DATASET_KEYS}
    for key in viewer.DASHBOARD_DATASET_KEYS:
        container = doc.select_one(f"#view-{key}")
        assert container is not None, f"{key} has no view container"
        assert container.has_attr("data-collapsed"), \
            f"{key}'s container states its collapsed state"
    assert not doc.select_one("#view-trusted").has_attr("hidden"), \
        "Trusted blocks is the view that is open on load"
    for key in ("review", "gone", "excluded"):
        assert doc.select_one(f"#view-{key}").has_attr("hidden"), \
            f"{key} is not the default view"

    controls = doc.select("[data-view][data-count-key]")
    assert len(controls) == len(viewer.DASHBOARD_DATASET_KEYS)
    assert {control["data-view"] for control in controls} == set(viewer.DASHBOARD_DATASET_KEYS)
    for control in controls:
        key = control["data-view"]
        label, scope = next((lab, sc) for view, lab, sc in viewer.DASHBOARD_VIEW_LABELS
                            if view == key)
        assert control.name == "button", "a view control is a keyboard-usable button"
        assert control["data-count-key"] == viewer.DASHBOARD_COUNT_KEYS[key]
        assert control["data-scope"] == scope
        assert control["aria-controls"] == f"view-{key}"
        text = control.get_text(" ", strip=True)
        assert label in text, f"{key}'s control is labelled {label}"
        assert viewer.DASHBOARD_SCOPE_WORDS[scope] in text, \
            f"{key}'s count is labelled with its scope ({scope})"


def test_the_excluded_view_opens_collapsed_and_the_others_do_not(alert_env, tmp_path):
    """Duty 1: Excluded is collapsed by default (section 7 item 4)."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    doc = _static_document(tmp_path, store)

    excluded = doc.select_one("#view-excluded")
    assert excluded["data-collapsed"] == "true"
    assert excluded.select_one(".source-rows").has_attr("hidden"), \
        "Excluded's rows stay collapsed until it is opened and expanded"
    expander = excluded.select_one("[data-expand='excluded']")
    assert expander is not None and expander.name == "button"
    assert expander["aria-controls"] == "rows-excluded"
    assert expander["aria-expanded"] == "false"
    for key in ("trusted", "review", "gone"):
        assert doc.select_one(f"#view-{key}")["data-collapsed"] == "false"
    assert len(doc.select("[data-expand]")) == 1, "only Excluded opens collapsed"


def test_the_controls_label_the_scoped_counts_the_payload_serves(alert_env, tmp_path):
    """Duty 1: labelled counts, grouped for trusted and source-level elsewhere."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    doc = _static_document(tmp_path, store)
    payload = _inlined_payload(doc)
    counts = payload["counts"]

    assert counts["trusted_scope"] == "grouped"
    assert counts["source_scope"] == ["review", "gone", "excluded"]
    controls = doc.select("[data-view][data-count-key]")
    assert len(controls) == len(viewer.DASHBOARD_DATASET_KEYS), \
        "one control per dataset, each naming the count it labels"
    assert {control["data-view"] for control in controls} == set(viewer.DASHBOARD_DATASET_KEYS)
    for control in controls:
        key = control["data-view"]
        count_key = control["data-count-key"]
        assert count_key in counts, f"{key}'s control labels the count {count_key}"
        assert control.select_one(".view-count").get_text(strip=True) == "0", \
            "the shell ships the count placeholder the page script fills"
        if key == "trusted":
            assert counts[count_key] == len(payload["trusted"])
            assert control["data-scope"] == "grouped"
        else:
            assert counts[count_key] == len(payload[key])
            assert control["data-scope"] == "source", \
                "a source-level count is labelled as source-level"
    assert counts["trusted_blocks"] == 1, "one grouped block, not one row per source"
    for key in ("needs_verification", "recently_gone", "excluded"):
        assert counts[key] == 1


def test_one_search_reads_the_address_and_title_of_every_dataset(alert_env, tmp_path):
    """Duty 2: one address/title search spans all four datasets."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    doc = _static_document(tmp_path, store)
    payload = _inlined_payload(doc)

    assert len(doc.select("input[type=search]")) == 1, "one search box, not one per view"
    search = doc.select_one("#fSearch")
    assert search["oninput"] == "render()", "the one search re-renders the active view"
    blob = _page_function("function searchableText(", "function passes(")
    assert "row.address" in blob and "row.title" in blob, \
        "the one search reads an address and a title in every dataset"

    for key in viewer.DASHBOARD_DATASET_KEYS:
        rows = payload["trusted"] if key == "trusted" else payload[key]
        assert rows, f"the {key} dataset is not empty"
        for row in rows:
            assert row.get("address") or row.get("title"), \
                f"a {key} row carries no address or title for the search to read"
    for row in payload["trusted"]:
        for field in ("address", "title", "nearest_station", "bw_id", "major_street"):
            assert field in row, f"the trusted search reads {field}"


def test_every_non_trusted_row_carries_its_disclosure_order(alert_env):
    """Duty 3: what the disclosure renders - a stable primary reason, the rest
    of the reasons, and each reason's section 7.1 rank. This is the read-model
    contract the disclosure consumes; the JS side is pinned by the two tests
    below that read the page script."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    payload = viewer.dashboard_payload(store)

    for key in ("review", "gone", "excluded"):
        assert payload[key], f"the {key} dataset is not empty"
        for row in payload[key]:
            reasons, ranks = row["reasons"], row["reason_ranks"]
            assert reasons, f"a {key} row has no reason to explain it"
            assert len(ranks) == len(reasons)
            assert row["primary_reason"] == reasons[0], \
                "the primary reason leads the disclosure"
            for text, rank in zip(reasons, ranks):
                assert isinstance(rank, int)
                assert rank == reason_rank(text), \
                    f"{text!r} is not carried at its section 7.1 rank"
    # The payload hands the same string from two fields (the trust reason and the
    # evaluation's reason), so a row can carry it twice; the renderer prints the
    # payload's list as it stands rather than editing it.


def test_a_review_row_shows_its_link_missing_fact_rent_availability_and_source(alert_env):
    """Duty 2: what a Needs verification row must render (section 7 item 2)."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    payload = viewer.dashboard_payload(store)

    row = next(item for item in payload["review"] if item["listing_id"] == DASH_REVIEW)
    assert row["view"] == "needs_verification", "the payload's own view name"
    assert row["url"], "a review row keeps its exact listing link"
    assert row["primary_reason"] == "rent unknown"
    assert row["source_listing_id"] and row["site"] == "test"
    assert row["rent_total"] is None, "Q1 holds a row whose rent is unknown"
    assert row["available_from"] == "2027-01-20", "the row shows its availability"
    assert row["property_identity_state"] == "resolved"
    # What each of those becomes in the rendered row (the rendered check itself
    # is in the probe).
    facts = _page_function("function sourceFactRow(", "function sourceRow(")
    for hook in ("source-missing-fact", "source-rent", "source-availability", "source-site"):
        assert hook in facts, f"the review row renders {hook}"
    assert "sourceLink(" in _page_function("function sourceRow(", "function paintViewControls("), \
        "the row carries its exact listing link"


def test_a_recently_gone_row_keeps_only_its_last_seen_and_last_decision(alert_env):
    """Duty 2/section 7 item 3: how long ago, never a rental date."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    payload = viewer.dashboard_payload(store)

    row = next(item for item in payload["gone"] if item["listing_id"] == DASH_GONE)
    assert row["view"] == "recently_gone", "the payload's own view name"
    assert row["last_seen_days_ago"] == 3
    assert row["outcome"] == "quarantined", "the gone row carries its last decision"
    assert "lifecycle expired" in row["reasons"], \
        "section 7.1 item 7 explains why a gone row is not ranked"
    # The renderer's gone branch must not reach for a rental date at all; the
    # rendered check is in the probe.
    body = _page_function("function sourceFactRow(", "function sourceRow(")
    gone_branch = body[:body.index("} else {")]
    assert "source-last-seen" in gone_branch
    assert "availabilityText" not in gone_branch, \
        "a gone row says only how long ago the source was last seen"
    assert "available_from" not in gone_branch


def test_the_disclosure_keeps_the_payload_reason_order_and_labels_the_score():
    """Duty 3: the order is the payload's, the retained score is provisional."""
    body = _page_function("function whyNotRanked(", "function sourceFactRow(")
    assert "row.reasons" in body and "row.reason_ranks" in body
    assert ".sort(" not in body, "the section 7.1 reason order is never re-sorted"
    assert "reason-primary" in body and "reason-secondary" in body
    assert "Previous score" in body and "provisional" in body
    assert "sourceLink" in body, "the disclosure carries the exact source link"
    assert "provenanceText" in body and "evidenceDateText" in body
    assert "reason-excerpt" in body


def test_no_non_trusted_row_is_rendered_with_a_rank_badge(alert_env, tmp_path):
    """Duty 3: no S-D badge and no rank treatment outside Trusted blocks."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    doc = _static_document(tmp_path, store)
    payload = _inlined_payload(doc)

    for key in ("review", "gone", "excluded"):
        for row in payload[key]:
            for forbidden in ("rank", "tier", "display_tier", "badge"):
                assert forbidden not in row
    body = _page_function("function sourceRow(", "function paintViewControls(")
    assert 'class="tier' not in body and "display_tier" not in body, \
        "the source-level row renderer carries no rank badge"
    container = doc.select_one("#view-review")
    assert not container.select(".tier"), "a non-trusted container ships no rank badge"


def test_the_page_script_switches_views_from_the_pinned_attributes(alert_env, tmp_path):
    """Duty 4: the static DOM and the live page read the same attributes."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    doc = _static_document(tmp_path, store)
    script = "\n".join(node.get_text() for node in doc.find_all("script"))

    assert "document.documentElement.dataset.defaultView" in script, \
        "the page script reads the default view from the root"
    assert "[data-view][data-count-key]" in script, "the controls are read by attribute"
    assert "dataset.collapsed" in script, "Excluded's collapsed state is read by attribute"
    assert "DATA.review" in script and "DATA.gone" in script and "DATA.excluded" in script, \
        "every dataset the payload carries is reachable by the page script"


def test_a_rendered_title_keeps_the_page_free_of_portal_emoji():
    """v36u1's finding, decided here: three production advert titles carry emoji.

    A source with no stored address is named by its portal title, so the row
    shows the title; the displayed copy has those characters stripped, which
    keeps the dashboard's own text emoji-free. The payload's `title` is not
    changed and stays the search key. The renderer runs here under node, the
    same engine the dashboard uses for its script.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    body = _page_function("const EMOJI_RE = ", "function sourceRow(")
    titles = ["Lille hyggelig lejlighed ved christianshavns kanal \u2764\ufe0f",
              "PENDLER k\u00e6lderlejlighed i hjertet af S\u00f8borg \U0001f3e1",
              "\U0001f512 RESERVERET. Nyistandsat og fuldt m\u00f8bleret",
              "1-v\u00e6relses", None, "  Spaced   title  "]
    program = (body + "\nconst cases = " + json.dumps(titles) + ";\n"
               "console.log(JSON.stringify(cases.map(t => plainTitle({title: t}))));")
    done = subprocess.run([node, "-e", program], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    stripped = json.loads(done.stdout)

    for text in stripped:
        assert not EMOJI_RE.search(text), f"{text!r} still carries an emoji"
    assert stripped[0] == "Lille hyggelig lejlighed ved christianshavns kanal"
    assert stripped[1] == "PENDLER k\u00e6lderlejlighed i hjertet af S\u00f8borg"
    assert stripped[2] == "RESERVERET. Nyistandsat og fuldt m\u00f8bleret"
    assert stripped[3] == "1-v\u00e6relses", "a room count is not touched"
    assert stripped[4] == "" and stripped[5] == "Spaced title"
    row_body = _page_function("function sourceRow(", "function paintViewControls(")
    assert "plainTitle(row)" in row_body, "the rendered title goes through the stripper"


# ==============================================================================
# Spec 07 sections 7, 7.2 and 7.3 - evidence labels and freshness, the Evidence
# problem filter, the integrity warning and the migration summary (v36u3)
# ==============================================================================
# The patterns above are reused: the static shell is parsed with BeautifulSoup,
# and the page's own functions are executed under node. A rendered row is a claim
# about the page's renderer, so the renderer runs; the operator surfaces
# (`paintIntegrity`, `paintMigration`) run against a stub element with a real
# `localStorage`, so show, suppress and dismiss are measured rather than read.

def _node(program):
    """Run one page-script program under node and read its JSON answer.

    The program goes to a file rather than `-e`: a real payload inlined into the
    source exceeds the shell's argument limit.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(program)
        path = handle.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True)
    finally:
        os.unlink(path)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# The real slices of the page script the v36u3 tests drive, in file order.
PAGE_ROW_HELPERS = (
    _page_function("const kr = v =>", "function commuteText(")
    + _page_function("function availabilityText(", "function relevanceText("))
PAGE_EVIDENCE_CONSTANTS = _page_function(
    "const TENANCY_PROFESSIONAL_STATE", "// Expired listings stay in local processing")
PAGE_EVIDENCE_FUNCTIONS = _page_function(
    "function managementVerified(", "function sourceFactRow(")
PAGE_SOURCE_FACT_ROW = _page_function("function sourceFactRow(", "const EMOJI_RE = ")
# The time helpers the source-row renderer reads (v38u1's explicit-gone label
# states when the advert was checked). They sit later in the file than the
# evidence block, so they are sliced by name.
PAGE_TIME_HELPERS = (_page_function("function lastSeenText(", "function evidenceDateText(")
                     + _page_function("function shortWhen(", "function ageText("))
PAGE_ROW_PRELUDE = (PAGE_ROW_HELPERS + PAGE_EVIDENCE_CONSTANTS
                    + PAGE_EVIDENCE_FUNCTIONS + PAGE_SOURCE_FACT_ROW
                    + PAGE_TIME_HELPERS)
# The same evidence vocabulary and functions, without the row renderer.
PAGE_OPERATOR_PRELUDE = PAGE_EVIDENCE_CONSTANTS + PAGE_EVIDENCE_FUNCTIONS

# The four problem values, in the order the control offers them.
EVIDENCE_PROBLEM_VALUES = [value for value, _label in viewer.DASHBOARD_EVIDENCE_PROBLEMS]


def _days_ago(days):
    return (datetime.datetime.now()
            - datetime.timedelta(days=days)).isoformat(timespec="seconds")


def _evidence_row(**over):
    """One source-level read-model row, as the payload serves it (v36q1's shape)."""
    row = {
        "view": "needs_verification",
        "site": "test",
        "listing_id": "EV-1",
        "address": "Reviewvej 2, 2300 København",
        "rent_total": 8300,
        "available_from": "2027-01-20",
        "outcome": "quarantined",
        "trust_outcome": "review",
        "tenancy_channel_state": "unknown",
        "building_form_state": "unknown",
        "property_identity_state": "resolved",
        "building_profile_id": None,
        "building_profile_verified_at": None,
        "reasons": ["landlord / direct lease unverified"],
        "reason_ranks": [2],
        "primary_reason": "landlord / direct lease unverified",
    }
    row.update(over)
    return row


def _render_facts(cases):
    """`sourceFactRow` over named (row, view) cases, under node."""
    program = PAGE_ROW_PRELUDE + """
const cases = %s;
const out = {};
for (const name of Object.keys(cases)) out[name] = sourceFactRow(cases[name].row, cases[name].view);
console.log(JSON.stringify(out));
""" % json.dumps(cases)
    return _node(program)


def test_the_management_and_building_badges_need_positive_evidence():
    """Duty 2 / spec 08 section 7: a badge only with evidence, and no twin.

    An absent badge is the whole statement, so the two former "unverified"
    labels and every place that rendered them are gone; a badge that IS shown
    carries its provenance on the `title` attribute, and a block badge older
    than the threshold carries section 7.2's suffix.
    """
    out = _render_facts({
        "unverified": {"row": _evidence_row(), "view": "review"},
        "verified": {"row": _evidence_row(
            tenancy_channel_state=viewer.TENANCY_PROFESSIONAL_STATE,
            building_form_state=viewer.BUILDING_MANAGED_STATE), "view": "review"},
        "private_only": {"row": _evidence_row(
            tenancy_channel_state=viewer.TENANCY_PROFESSIONAL_STATE), "view": "review"},
        "with_excerpt": {"row": _evidence_row(
            tenancy_channel_state=viewer.TENANCY_PROFESSIONAL_STATE,
            landlord_evidence_excerpt="BoligPortal advertiser portfolio: 7 rentables"),
            "view": "review"},
        "block_stale": {"row": _evidence_row(
            building_form_state=viewer.BUILDING_MANAGED_STATE,
            building_profile_verified_at=_days_ago(100)), "view": "review"},
    })

    # No evidence: no badge at all, and no "unverified" twin anywhere.
    assert "source-management" not in out["unverified"]
    assert "source-building" not in out["unverified"]
    assert "source-unverified" not in out["unverified"]
    assert "Management unverified" not in out["unverified"]
    assert "Building type unverified" not in out["unverified"]
    assert viewer.MANAGEMENT_VERIFIED_LABEL not in out["unverified"]
    assert viewer.BUILDING_VERIFIED_LABEL not in out["unverified"]
    assert not hasattr(viewer, "MANAGEMENT_UNVERIFIED_LABEL")
    assert not hasattr(viewer, "BUILDING_UNVERIFIED_LABEL")

    # Both verdicts: both badges, each labelled as its own positive state.
    assert viewer.MANAGEMENT_VERIFIED_LABEL in out["verified"]
    assert viewer.BUILDING_VERIFIED_LABEL in out["verified"]
    assert "source-management source-verified" in out["verified"]
    assert "source-building source-verified" in out["verified"]

    # Duty 2's provenance: the badge's title carries the row's evidence excerpt.
    assert 'title="BoligPortal advertiser portfolio: 7 rentables"' in out["with_excerpt"], \
        "a management badge states where its evidence came from"

    # The two facts are independent: professional management alone is one badge.
    assert viewer.MANAGEMENT_VERIFIED_LABEL in out["private_only"]
    assert "source-building" not in out["private_only"]

    # Section 7.2: a stale block badge keeps the badge and states the flag.
    assert f"{viewer.BUILDING_VERIFIED_LABEL} ({config.TRUST_REASON_STALE})" \
        in out["block_stale"]
    assert "source-building source-verified" in out["block_stale"]

    assert json.dumps(viewer.TENANCY_PROFESSIONAL_STATE) in viewer.HTML_TEMPLATE
    assert json.dumps(viewer.BUILDING_MANAGED_STATE) in viewer.HTML_TEMPLATE
    assert viewer.TENANCY_PROFESSIONAL_STATE in config.TRUST_TENANCY_CHANNEL_STATES
    assert viewer.BUILDING_MANAGED_STATE in config.TRUST_BUILDING_FORM_STATES


def test_the_evidence_date_is_shown_and_old_evidence_reads_stale():
    """Duty 1 / section 7.2: the verified date, and `verification stale` >90 days."""
    out = _render_facts({
        "fresh": {"row": _evidence_row(building_profile_verified_at=_days_ago(10)),
                  "view": "review"},
        "stale": {"row": _evidence_row(building_profile_verified_at=_days_ago(100)),
                  "view": "review"},
        "undated": {"row": _evidence_row(), "view": "review"},
        "stale_excluded": {"row": _evidence_row(
            building_profile_verified_at=_days_ago(100)), "view": "excluded"},
    })

    assert "source-evidence-date" in out["stale"]
    assert "source-stale" in out["stale"]
    assert config.TRUST_REASON_STALE in out["stale"], \
        "the flag is section 7.2's own wording, not a second label"

    assert "source-evidence-date" in out["fresh"]
    assert "source-stale" not in out["fresh"], \
        "10-day-old evidence is inside the threshold"

    assert "Evidence verified: unknown" in out["undated"], \
        "a Needs verification row states the missing date rather than implying one"
    assert "source-stale" not in out["undated"]

    assert "source-stale" not in out["stale_excluded"], \
        "section 7.2 flags staleness in Needs verification"
    assert "source-evidence-date" not in out["stale_excluded"]

    assert f"const EVIDENCE_MAX_DAYS = {int(config.BUILDING_EVIDENCE_MAX_DAYS)};" \
        in viewer.HTML_TEMPLATE
    assert f"const STALE_REASON = {json.dumps(config.TRUST_REASON_STALE)};" \
        in viewer.HTML_TEMPLATE


def test_the_evidence_problem_filter_offers_the_four_pinned_problems(alert_env, tmp_path):
    """Duty 3 / spec 08 section 7: the compact filter and its four arms."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    doc = _static_document(tmp_path, store)

    select = doc.select_one("#fProblem")
    assert select is not None and select.name == "select", \
        "the Evidence problem filter is one compact control"
    assert select["onchange"] == "render()"
    options = select.select("option")
    assert options[0]["value"] == "" and options[0].get_text(strip=True) == "All"
    offered = [(option["value"], option.get_text(strip=True)) for option in options[1:]]
    assert offered == list(viewer.DASHBOARD_EVIDENCE_PROBLEMS), \
        "the shell offers exactly the four pinned problems, in order"
    assert [option.get("data-problem") for option in options[1:]] \
        == EVIDENCE_PROBLEM_VALUES


def test_the_evidence_problem_arms_name_the_row_they_match():
    """Duty 3 / spec 08 section 7: each arm answers for the fact it names.

    The `management` and `building` arms are gone; `housing` names the largest
    remaining hold and matches the row's own reason text, so a known rejection
    (a student-housing row) is never reported as an unknown fact.
    """
    program = PAGE_OPERATOR_PRELUDE + """
const problems = %s;
const rows = %s;
const out = {};
for (const name of Object.keys(rows)) out[name] = problems.map(p => hasEvidenceProblem(rows[name], p));
console.log(JSON.stringify(out));
""" % (json.dumps(EVIDENCE_PROBLEM_VALUES), json.dumps({
        "housing_review": _evidence_row(reasons=["housing eligibility unverified"],
                                        reason_ranks=[3]),
        "housing_failed": _evidence_row(reasons=["housing eligibility fetch failed"],
                                        reason_ranks=[3]),
        "student_rejected": _evidence_row(
            view="excluded", outcome="rejected",
            reasons=["student housing (student housing)"], reason_ranks=[3]),
        "lease_review": _evidence_row(reasons=["unverified lease duration"],
                                      reason_ranks=[5]),
        "kitchen_review": _evidence_row(
            reasons=["shared kitchen mentioned with no private-kitchen evidence"],
            reason_ranks=[5]),
        "rejected_lease": _evidence_row(
            view="excluded", outcome="rejected",
            reasons=["time-limited lease (time-limited (12 months))"], reason_ranks=[5]),
        "gate_reason": _evidence_row(reasons=["rent 9650 > 9500"], reason_ranks=[6]),
        "professional": _evidence_row(
            tenancy_channel_state=viewer.TENANCY_PROFESSIONAL_STATE,
            building_form_state=viewer.BUILDING_MANAGED_STATE),
        "unresolved": _evidence_row(property_identity_state="unresolved"),
    }))
    answers = {name: dict(zip(EVIDENCE_PROBLEM_VALUES, flags))
               for name, flags in _node(program).items()}

    assert answers["housing_review"]["housing"] is True
    assert answers["housing_failed"]["housing"] is True
    assert answers["housing_review"]["kitchen"] is False
    assert answers["housing_review"]["lease"] is False
    assert answers["housing_review"]["identity"] is False

    # A known rejection names the fact; it is not an unknown one.
    assert answers["student_rejected"]["housing"] is False, \
        "a student-housing rejection is a fact, not a coverage gap"

    assert answers["lease_review"]["lease"] is True
    assert answers["lease_review"]["kitchen"] is False
    assert answers["kitchen_review"]["kitchen"] is True
    assert answers["kitchen_review"]["lease"] is False
    assert answers["lease_review"]["housing"] is False

    assert answers["rejected_lease"]["lease"] is False
    assert answers["gate_reason"]["lease"] is False
    assert answers["gate_reason"]["kitchen"] is False

    # The identity arm reads the row's own identity state.
    assert answers["unresolved"]["identity"] is True
    assert answers["unresolved"]["housing"] is False
    assert answers["professional"]["identity"] is False

    # The two removed arms cannot be asked for at all.
    assert "management" not in EVIDENCE_PROBLEM_VALUES
    assert "building" not in EVIDENCE_PROBLEM_VALUES

    assert f"const EVIDENCE_PROBLEM_RANK = {viewer.EVIDENCE_PROBLEM_REASON_RANK};" \
        in viewer.HTML_TEMPLATE


def test_the_evidence_problem_filter_applies_where_the_evidence_is_rendered():
    """Duty 2: the filter narrows the views that show the evidence facts."""
    body = _page_function("function passes(", "const SORT_VALUE")
    assert "fProblem" in body and "hasEvidenceProblem(m, problem)" in body
    assert "ACTIVE_DATASET === 'review' || ACTIVE_DATASET === 'excluded'" in body

    chips = _page_function("function paintActiveFilters(", "function datasetRows(")
    assert "clearFilter('fProblem')" in chips, "an active problem says so where the others do"
    reset = _page_function("function resetFilters(", "function toggleColumns(")
    assert "document.getElementById('fProblem').value = ''" in reset


def test_the_integrity_warning_shows_the_counts_and_never_a_clean_status(alert_env, tmp_path):
    """Duty 3 / section 7: the operator warning, and nothing that reads clean."""
    store, _sent, stations = alert_env
    program = PAGE_OPERATOR_PRELUDE + """
const box = { dataset: {}, hidden: true, innerHTML: 'placeholder' };
global.document = { getElementById: id => (id === 'integrity' ? box : null) };
let DATA = { integrity: { null_linked_current_matches: 115,
                          forbidden_trust_in_matches: 12, warning: true } };
paintIntegrity();
const warned = { hidden: box.hidden, state: box.dataset.integrityWarning, text: box.innerHTML };
DATA = { integrity: { null_linked_current_matches: 0,
                      forbidden_trust_in_matches: 0, warning: false } };
paintIntegrity();
const clean = { hidden: box.hidden, state: box.dataset.integrityWarning, text: box.innerHTML };
console.log(JSON.stringify({ warned: warned, clean: clean }));
"""
    out = _node(program)
    assert out["warned"]["state"] == "true" and out["warned"]["hidden"] is False
    assert "115" in out["warned"]["text"] and "12" in out["warned"]["text"], \
        "the warning carries the two measured counts"
    assert "local operator" in out["warned"]["text"].lower()
    assert out["clean"]["state"] == "false" and out["clean"]["hidden"] is True
    assert out["clean"]["text"] == "", \
        "with no violation the surface states nothing rather than reading clean"

    _seed_payload_fixtures(store, stations)
    doc = _static_document(tmp_path, store)
    shell = doc.select_one("#integrity")
    assert shell is not None and "integrity" in shell["class"]
    assert shell["data-integrity-warning"] == "false", \
        "the shipped shell is the suppressed state the page script replaces"
    assert shell.has_attr("hidden") and shell.get_text(strip=True) == ""


def _migration_payload(store, summary, **over):
    """A payload shaped like the page's, for the banner's own conditions.

    The store is not read: each crafted payload drives exactly one of section
    7.3's three conditions, so the suppression cases stay separable.
    """
    payload = {
        "stats": {"local_rules": {"applied_at": "2026-09-15T16:00:00"}},
        "integrity": {"null_linked_current_matches": 0,
                      "forbidden_trust_in_matches": 0, "warning": False},
        "migration": {"mode": "no-network", "migration": summary,
                      "completed_at": "2026-09-15T16:00:00"},
    }
    payload.update(over)
    return payload


def _paint_operator_surfaces(payload, dismiss=False):
    """Both operator surfaces against a stub element and a real localStorage."""
    program = PAGE_OPERATOR_PRELUDE + """
const elements = {};
for (const id of ['integrity', 'migrationBanner']) {
  elements[id] = { dataset: {}, hidden: true, innerHTML: 'placeholder' };
}
global.document = { getElementById: id => elements[id] || null };
const stored = {};
global.localStorage = {
  getItem: key => (key in stored ? stored[key] : null),
  setItem: (key, value) => { stored[key] = String(value); },
  removeItem: key => { delete stored[key]; },
};
let DATA = %s;
paintIntegrity();
paintMigration();
if (%s) dismissMigration();
console.log(JSON.stringify({
  integrity: { hidden: elements.integrity.hidden,
               state: elements.integrity.dataset.integrityWarning,
               text: elements.integrity.innerHTML },
  banner: { hidden: elements.migrationBanner.hidden,
            state: elements.migrationBanner.dataset.migrationState,
            text: elements.migrationBanner.innerHTML },
  storage: Object.keys(stored),
}));
""" % (json.dumps(payload), "true" if dismiss else "false")
    return _node(program)


def test_the_migration_banner_renders_the_counts_the_run_recorded(alert_env):
    """Duty 4 / section 7.3: the banner's numbers are the run's own, measured."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    counts = viewer.dashboard_payload(store)["counts"]
    relinked = store.conn.execute("SELECT COUNT(*) FROM source_listings").fetchone()[0]
    summary = {"relinked_sources": relinked,
               "trusted_blocks": counts["trusted_blocks"],
               "needs_verification": counts["needs_verification"],
               "excluded": counts["excluded"]}
    run.refresh_local(store, export=False, quiet=True, migration_summary=summary)
    payload = viewer.dashboard_payload(store)

    assert payload["migration"]["migration"] == summary, \
        "the payload serves the run's own summary, verbatim"
    assert payload["migration"]["mode"] == "no-network"
    assert payload["migration"]["completed_at"] \
        == payload["stats"]["local_rules"]["applied_at"], \
        "the run that carried the summary is the run the export reflects"
    assert payload["integrity"]["warning"] is False

    out = _paint_operator_surfaces(payload)
    assert out["banner"]["hidden"] is False
    assert out["banner"]["state"] == "shown"
    text = out["banner"]["text"]
    for value in (summary["relinked_sources"], summary["trusted_blocks"],
                  summary["needs_verification"], summary["excluded"]):
        assert str(value) in text, f"the banner prints the measured {value}"
    assert "Inventory repaired:" in text
    assert "source listings relinked" in text
    assert "trusted blocks" in text and "need verification" in text and "excluded" in text
    assert "Room rentals and private/sublet listings are no longer ranked." in text
    assert "Dismiss" in text
    assert out["storage"] == [], "showing the banner writes nothing"


def test_the_migration_banner_names_the_new_ranking_rules_when_nothing_relinked(alert_env):
    """Duty 4 / spec 08 section 7.3: `relinked_sources == 0` uses the new text.

    The v3.7 migration relinks nothing, so the banner drops the repair sentence
    and states the new rules and their counts; the dismissal keying is the same
    run key the other cases use.
    """
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    summary = {"relinked_sources": 0, "trusted_blocks": 25,
               "needs_verification": 911, "excluded": 1488}
    payload = _migration_payload(store, summary)
    assert payload["integrity"]["warning"] is False

    out = _paint_operator_surfaces(payload)
    assert out["banner"]["hidden"] is False and out["banner"]["state"] == "shown"
    text = out["banner"]["text"]
    assert "Ranking rules updated: 25 ranked &middot; 911 need verification " \
           "&middot; 1488 excluded." in text, \
        "the first sentence carries the completed refresh's own counts, in order"
    assert "Missing landlord or building evidence no longer holds a listing; " \
           "room rentals, sublets and private landlords are still excluded." in text, \
        "spec 08 section 7.3's second sentence follows it"
    assert "Inventory repaired" not in text, \
        "a migration that relinked nothing never claims a repair"
    assert "Dismiss" in text
    assert out["storage"] == [], "showing the banner writes nothing"

    # The same summary through the run's own key still dismisses it.
    dismissed = _paint_operator_surfaces(payload, dismiss=True)
    assert dismissed["banner"]["hidden"] is True
    assert dismissed["storage"] == ["boligwatch.migration." + payload["migration"]["completed_at"]]


def test_the_migration_banner_is_suppressed_without_a_four_key_summary(alert_env):
    """Duty 4, condition 1: missing, typed-wrong or failed summary data."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    good = {"relinked_sources": 3, "trusted_blocks": 1,
            "needs_verification": 2, "excluded": 4}
    cases = {
        "no_summary": _migration_payload(store, None),
        "empty": _migration_payload(store, {}),
        "three_keys": _migration_payload(store, {k: v for k, v in good.items()
                                                 if k != "excluded"}),
        "string_count": _migration_payload(store, dict(good, excluded="4")),
        "bool_count": _migration_payload(store, dict(good, excluded=True)),
        "list": _migration_payload(store, [4]),
    }
    for name, payload in cases.items():
        out = _paint_operator_surfaces(payload)
        assert out["banner"]["hidden"] is True, f"{name} must render nothing"
        assert out["banner"]["state"] == "suppressed", name
        assert out["banner"]["text"] == "", \
            f"{name} renders no placeholder text"

    # A real payload carries no summary until a run records one: same suppression.
    payload = viewer.dashboard_payload(store)
    assert payload["migration"] == {"mode": None, "migration": None,
                                    "completed_at": None}
    out = _paint_operator_surfaces(payload)
    assert out["banner"]["hidden"] is True and out["banner"]["text"] == ""


def test_the_migration_banner_yields_to_an_active_integrity_warning(alert_env):
    """Duty 4, condition 2 / duty 3: the warning wins, and it is the only state."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    summary = {"relinked_sources": 3, "trusted_blocks": 1,
               "needs_verification": 2, "excluded": 4}
    payload = _migration_payload(store, summary, integrity={
        "null_linked_current_matches": 115, "forbidden_trust_in_matches": 12,
        "warning": True})

    out = _paint_operator_surfaces(payload)
    assert out["integrity"]["state"] == "true" and out["integrity"]["hidden"] is False
    assert out["banner"]["hidden"] is True, \
        "a clean migration banner must never appear beside a live violation"
    assert out["banner"]["text"] == "" and out["banner"]["state"] == "suppressed"


def test_the_migration_banner_is_suppressed_for_an_export_the_run_did_not_build(alert_env):
    """Duty 4, condition 3: the export's rules stamp is not that run's stamp."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    summary = {"relinked_sources": 3, "trusted_blocks": 1,
               "needs_verification": 2, "excluded": 4}
    payload = _migration_payload(store, summary)
    assert payload["stats"]["local_rules"]["applied_at"] \
        == payload["migration"]["completed_at"], "the fixture starts valid"

    older = _migration_payload(store, summary, stats={
        "local_rules": {"applied_at": "2026-09-15T15:00:00"}})
    out = _paint_operator_surfaces(older)
    assert out["banner"]["hidden"] is True and out["banner"]["text"] == ""

    missing_stamp = _migration_payload(store, summary, stats={"local_rules": {}})
    out = _paint_operator_surfaces(missing_stamp)
    assert out["banner"]["hidden"] is True and out["banner"]["text"] == ""


def test_dismissing_the_migration_banner_is_keyed_by_the_run(alert_env):
    """Duty 4 / section 7.3: dismissal is local-only and belongs to the run."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    summary = {"relinked_sources": 3, "trusted_blocks": 1,
               "needs_verification": 2, "excluded": 4}
    payload = _migration_payload(store, summary)

    out = _paint_operator_surfaces(payload, dismiss=True)
    assert out["banner"]["hidden"] is True and out["banner"]["text"] == ""
    assert out["banner"]["state"] == "suppressed"
    assert out["storage"] == ["boligwatch.migration." + payload["migration"]["completed_at"]], \
        "the dismissal is keyed by the run's completed_at"

    later = _migration_payload(store, summary)
    later["migration"]["completed_at"] = "2026-10-01T16:00:00"
    later["stats"]["local_rules"]["applied_at"] = "2026-10-01T16:00:00"
    out = _paint_operator_surfaces(later)
    assert out["banner"]["hidden"] is False, \
        "a later migration shows its own banner"


def test_the_static_shell_ships_no_operator_text(alert_env, tmp_path):
    """Duty 3/4: the export carries the surfaces, never their content."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    path = tmp_path / "report.html"
    viewer.export_static_html(str(path), store=store)
    content = path.read_text(encoding="utf-8")
    doc = BeautifulSoup(content, "lxml")

    banner = doc.select_one("#migrationBanner")
    assert banner is not None and "migration-banner" in banner["class"]
    assert banner["data-migration-state"] == "suppressed"
    assert banner.has_attr("hidden") and banner.get_text(strip=True) == ""
    assert "Inventory repaired: " in viewer.HTML_TEMPLATE, \
        "the page script is where the summary is built"

    # The export's markup carries no operator text: the strings exist only inside
    # the page script, which paints them from the inlined payload.
    markup = BeautifulSoup(content, "lxml")
    for script in markup.find_all("script"):
        script.decompose()
    rendered_text = markup.get_text(" ", strip=True)
    assert "Inventory repaired" not in rendered_text
    assert "Integrity violation" not in rendered_text

    payload = _inlined_payload(doc)
    assert payload["migration"] == store.last_local_run_migration()
    assert payload["migration"] == {"mode": None, "migration": None,
                                    "completed_at": None}
    assert not EMOJI_RE.search(content)


# ==============================================================================
# Spec 09 sections 5.3, 8.2, 9.1 and 10.5 plus acceptance 29-30 - the v38u1 row:
# the preferred-locality disclosure, the missing-scan and explicit-gone
# statements, the age-rule disclosure without the profile age, and export privacy
# ==============================================================================

# The private profile vocabulary a candidate export may never carry (section 6.1
# names the three fields; the rest is the date-of-birth wording acceptance 29
# forbids). The two field names are matched with a left word boundary because the
# read model's own listing-side fields are `unit_type_evidence_source` and
# `landlord_evidence_source`: a BARE occurrence is the profile's column.
PRIVATE_PROFILE_KEYS = ("age_years", "search_profile", "evidence_source", "updated_at")
PRIVATE_PROFILE_PATTERNS = (
    (r"age_years", "the profile age field"),
    (r"search_profile", "the local profile table"),
    (r"(?<![A-Za-z0-9_])evidence_source", "a bare evidence source (the profile's column)"),
    (r"(?<![A-Za-z0-9_])updated_at", "a bare update time (the profile's column)"),
    (r"[Dd]ate of birth", "date-of-birth vocabulary"),
    (r"fødselsdato", "date-of-birth vocabulary"),
    (r"\b[dD]ob\b", "date-of-birth vocabulary"),
    (r"\bbirth\b", "date-of-birth vocabulary"),
)


def _payload_keys(node, out=None):
    """Every dict key anywhere in the payload, for a private-field sweep."""
    out = set() if out is None else out
    if isinstance(node, dict):
        for key, value in node.items():
            out.add(str(key))
            _payload_keys(value, out)
    elif isinstance(node, list):
        for item in node:
            _payload_keys(item, out)
    return out


PAGE_PREFERENCE_HELPERS = _page_function("function preferredLocality(", "function badgeLine(")
PAGE_BADGE_LINE = _page_function("function badgeLine(", "function detailsHtml(")
PAGE_PREFERENCE_PRELUDE = (PAGE_ROW_HELPERS + PAGE_EVIDENCE_CONSTANTS
                           + PAGE_PREFERENCE_HELPERS + PAGE_BADGE_LINE)


def _render_badge_line(cases):
    """`badgeLine` over named rows, under node."""
    program = PAGE_PREFERENCE_PRELUDE + """
const cases = %s;
const out = {};
for (const name of Object.keys(cases)) out[name] = badgeLine(cases[name]);
console.log(JSON.stringify(out));
""" % json.dumps(cases)
    return _node(program)


def test_the_preference_badge_is_rendered_only_for_the_stored_kind():
    """Duty 1 / section 5.3: one badge for the one kind v3.8 stores, and no twin."""
    out = _render_badge_line({
        "preferred_b": {"display_tier": "B", "score": 54.0, "rank_priority": 1,
                        "rank_priority_kind": viewer.PREFERRED_LOCALITY_KIND,
                        "rank_priority_note": "Nicer locality and good value"},
        "preferred_a": {"display_tier": "A", "score": 65.0, "rank_priority": 1,
                        "rank_priority_kind": viewer.PREFERRED_LOCALITY_KIND},
        "cleared": {"display_tier": "B", "score": 58.0, "rank_priority": 0},
        "other_kind": {"display_tier": "B", "score": 54.0, "rank_priority": 1,
                       "rank_priority_kind": "objective_quality"},
    })
    assert len(out) == 4, "every case rendered"
    assert out["preferred_b"] == (
        ' <span class="pill preferred-locality" '
        'title="Personally preferred locality; affects order within Tier B only">'
        f'{viewer.PREFERRED_LOCALITY_LABEL}</span>'), \
        "a preferred row renders the badge, the label and the within-tier title"
    assert "affects order within Tier A only" in out["preferred_a"], \
        "the accessible title states the row's own tier"
    assert out["cleared"] == "", "a property with no preference renders no badge at all"
    assert out["other_kind"] == "", \
        "only the one kind v3.8 stores earns the badge; an unknown kind is not guessed"
    assert viewer.PREFERRED_LOCALITY_LABEL in out["preferred_a"]
    for placeholder in ("__PREFERRED_LOCALITY_KIND__", "__PREFERRED_LOCALITY_LABEL__"):
        assert placeholder not in viewer.HTML_TEMPLATE, \
            "a forgotten placeholder would render as literal text"
    assert f"const PREFERRED_LOCALITY_KIND = {json.dumps(viewer.PREFERRED_LOCALITY_KIND)};" \
        in viewer.HTML_TEMPLATE
    assert f"const PREFERRED_LOCALITY_LABEL = {json.dumps(viewer.PREFERRED_LOCALITY_LABEL)};" \
        in viewer.HTML_TEMPLATE


def test_a_preferred_property_discloses_the_badge_and_note_without_rescoring(alert_env):
    """Duty 1 / sections 5.2-5.3: the disclosure rides the row, the row does not move."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    before = viewer.dashboard_payload(store)["trusted"][0]
    assert before["rank_priority_kind"] is None, "the fixture starts unpreferred"

    store.set_rank_priority(before["property_id"], 1,
                            kind=viewer.PREFERRED_LOCALITY_KIND,
                            note="Nicer locality and good value")
    after = viewer.dashboard_payload(store)["trusted"][0]

    assert after["rank_priority"] == 1
    assert after["rank_priority_kind"] == viewer.PREFERRED_LOCALITY_KIND
    assert after["rank_priority_note"] == "Nicer locality and good value"
    assert after["score"] == before["score"], "the objective score is unchanged"
    assert after["display_tier"] == before["display_tier"], "the objective tier is unchanged"
    assert after["rent_total"] == before["rent_total"]

    rendered = _render_badge_line({"row": after})
    assert viewer.PREFERRED_LOCALITY_LABEL in rendered["row"]
    assert f"affects order within Tier {after['display_tier']} only" in rendered["row"]

    details = _page_function("function detailsHtml(", "function comparisonId(")
    assert "preferredLocality(m)" in details and "m.rank_priority_note" in details, \
        "the expanded details show the optional note of a preferred property"
    assert "preferredLocalityTitle(m)" in details, \
        "the details repeat the narrow ordering statement"


def test_a_missing_row_states_how_many_complete_scans_missed_it(alert_env):
    """Duty 2 / section 9.1: exactly the one- and two-miss statements, nothing else."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)

    for misses, label in ((1, "Not seen in 1 complete scan"),
                          (2, "Not seen in 2 complete scans")):
        store.conn.execute(
            "UPDATE source_listings SET lifecycle_state='missing', consecutive_misses=? "
            "WHERE site='test' AND listing_id=?", (misses, DASH_REVIEW))
        store.conn.commit()
        row = next(item for item in viewer.dashboard_payload(store)["review"]
                   if item["listing_id"] == DASH_REVIEW)
        assert row["view"] == "needs_verification"
        assert row["missing_label"] == label

        out = _render_facts({"row": {"row": row, "view": "review"}})
        assert f'<li class="source-missing-scan">{label}</li>' in out["row"], \
            f"the row renders exactly {label!r}"
        assert "source-decision" not in out["row"], \
            "a review row carries no gone-row decision line"
        assert "expired" not in out["row"], \
            "one or two complete misses are never called expired"


def test_an_explicitly_gone_row_states_the_advert_is_gone_and_when_it_was_checked(alert_env):
    """Duty 2 / section 8.2 item 5: explicit-gone wording, and no other row wears it."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    absence = next(item for item in viewer.dashboard_payload(store)["gone"]
                   if item["listing_id"] == DASH_GONE)
    assert absence["explicit_gone_at"] is None, "this fixture expired by absence"

    stamp = _days_ago(2)
    store.conn.execute(
        "UPDATE source_listings SET explicit_gone_at=?, explicit_gone_marker=?, "
        "detail_state='failed:gone' WHERE site='test' AND listing_id=?",
        (stamp, "boligen er udlejet", DASH_GONE))
    store.conn.commit()
    explicit = next(item for item in viewer.dashboard_payload(store)["gone"]
                    if item["listing_id"] == DASH_GONE)
    assert explicit["view"] == "recently_gone"
    assert explicit["explicit_gone_at"] == stamp
    assert explicit["explicit_gone_marker"] == "boligen er udlejet"

    out = _render_facts({"explicit": {"row": explicit, "view": "gone"},
                         "absence": {"row": absence, "view": "gone"}})
    assert '<li class="source-explicit-gone">Advert says no longer available · checked ' \
        in out["explicit"]
    assert "checked never" not in out["explicit"], \
        "the label states when the own detail page was checked"
    assert "Advert says no longer available" not in out["absence"]
    assert "source-explicit-gone" not in out["absence"]
    assert "source-last-seen" in out["explicit"] and "source-decision" in out["explicit"], \
        "a gone row still states how long ago it was last seen and its last decision"


def test_the_age_rule_is_disclosed_without_the_profile_age():
    """Duty 2 / sections 6.2-6.3 and 10.5: the bounds and the outcome, never the age."""
    out = _render_facts({
        "satisfied": {"row": _evidence_row(age_requirement_state="bounded", age_min=18,
                                           age_max=35, profile_age_fit="eligible"),
                      "view": "review"},
        "outside": {"row": _evidence_row(age_requirement_state="bounded", age_min=18,
                                         age_max=35, profile_age_fit="ineligible"),
                    "view": "excluded"},
        "min_only": {"row": _evidence_row(age_requirement_state="bounded", age_min=21,
                                          age_max=None, profile_age_fit="eligible"),
                     "view": "review"},
        "no_restriction": {"row": _evidence_row(age_requirement_state="none", age_min=None,
                                                age_max=None, profile_age_fit="unknown"),
                           "view": "review"},
        "unreadable": {"row": _evidence_row(age_requirement_state="unknown", age_min=18,
                                            age_max=35, profile_age_fit="unknown"),
                       "view": "review"},
    })
    assert len(out) == 5, "every case rendered"
    assert '<li class="source-age-requirement">Age requirement 18–35</li>' in out["satisfied"]
    assert '<li class="source-age-requirement">Age requirement satisfied</li>' in out["satisfied"]
    assert '<li class="source-age-requirement">Age requirement 18–35</li>' in out["outside"]
    assert "Age requirement satisfied" not in out["outside"], \
        "a range the profile is outside of is never called satisfied"
    assert '<li class="source-age-requirement">Age requirement min 21</li>' in out["min_only"]
    for name in ("no_restriction", "unreadable"):
        assert "Age requirement" not in out[name], \
            "a row with no readable bounds discloses nothing rather than guessing"
    for name, html in out.items():
        assert "age_years" not in html and "profile_age" not in html
        assert not re.search(r"\b23\b", html), f"{name} renders the local profile age"


def test_acceptance_29_the_candidate_export_carries_no_private_profile_record(alert_env, tmp_path):
    """Acceptance 29: no profile age, no date of birth, no raw private profile record."""
    store, _sent, stations = alert_env
    store.set_search_profile_age(23, evidence_source="user")
    _seed_payload_fixtures(store, stations)
    profile = store.get_search_profile()
    assert profile["age_years"] == 23

    trusted = viewer.dashboard_payload(store)["trusted"]
    store.set_rank_priority(trusted[0]["property_id"], 1,
                            kind=viewer.PREFERRED_LOCALITY_KIND,
                            note="Nicer locality and good value")

    path = tmp_path / "report.html"
    viewer.export_static_html(str(path), store=store)
    content = path.read_text(encoding="utf-8")
    doc = BeautifulSoup(content, "lxml")
    payload = _inlined_payload(doc)

    keys = _payload_keys(payload)
    assert not (set(PRIVATE_PROFILE_KEYS) & keys), \
        f"the payload carries a private profile field: {sorted(set(PRIVATE_PROFILE_KEYS) & keys)}"
    for pattern, why in PRIVATE_PROFILE_PATTERNS:
        assert not re.search(pattern, content), f"the candidate export carries {why}"
    # The profile's own record cannot be reconstructed from the export: no key of
    # its row appears anywhere (above), and neither the stats block nor any row
    # carries one. (The profile's update stamp is a second-resolution timestamp
    # that legitimately coincides with the snapshot's own time, so the value is
    # not the contract - the field name is.)
    assert not (set(profile) & set(payload["stats"])), "the stats block carries a profile column"
    for name in ("trusted", "review", "gone", "excluded"):
        for row in payload[name]:
            assert not (set(PRIVATE_PROFILE_KEYS) & set(row)), \
                f"a {name} row carries a private profile field"
    # The listing-side age facts are disclosed, and the profile's own record is not.
    assert "source-age-requirement" in viewer.HTML_TEMPLATE
    assert "profile_age_fit" in keys, "the listing-side eligibility fact is what a row discloses"
    assert not EMOJI_RE.search(content)


def test_acceptance_30_the_shell_keeps_four_controls_ranked_default_and_the_disclosures(
        alert_env, tmp_path):
    """Acceptance 30: the four controls, the Ranked default, the collapsed Excluded
    view and every v3.8 disclosure hook, in the exported document."""
    store, _sent, stations = alert_env
    _seed_payload_fixtures(store, stations)
    doc = _static_document(tmp_path, store)
    payload = _inlined_payload(doc)

    controls = doc.select("[data-view][data-count-key]")
    assert len(controls) == len(viewer.DASHBOARD_DATASET_KEYS) == 4
    assert {control["data-view"] for control in controls} == set(viewer.DASHBOARD_DATASET_KEYS)
    assert doc.find("html")["data-default-view"] == viewer.DEFAULT_DASHBOARD_VIEW == "trusted"
    assert doc.select_one("#view-excluded")["data-collapsed"] == "true"
    assert "sourceLink(" in _page_function("function sourceRow(", "function paintViewControls("), \
        "the exact source link survives"

    # The payload serves the facts each new disclosure reads.
    assert payload["trusted"], "the Ranked dataset is served"
    for key in ("rank_priority", "rank_priority_kind", "rank_priority_note"):
        assert key in payload["trusted"][0], f"a Ranked row carries {key}"
    for key in ("missing_label", "consecutive_misses", "age_requirement_state",
                "age_min", "age_max", "profile_age_fit"):
        assert key in payload["review"][0], f"a Needs verification row carries {key}"
    for key in ("explicit_gone_at", "explicit_gone_marker"):
        assert key in payload["gone"][0], f"a Recently gone row carries {key}"

    # And the page script renders them: the preference badge, the missing-scan
    # count, the explicit-gone label and the age rule.
    for hook in ("preferred-locality", "source-missing-scan", "source-explicit-gone",
                 "source-age-requirement"):
        assert hook in viewer.HTML_TEMPLATE, f"the page renders {hook}"
    facts = _page_function("function sourceFactRow(", "function sourceRow(")
    assert "missingScanText(row)" in facts and "explicitGoneText(row)" in facts
    assert "ageRequirementLines(row)" in facts
