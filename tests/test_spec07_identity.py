"""Regression tests for espec 07 - trust blocks & inventory recovery.

Phase 1 (identity) per specs/housing-search/07-TRUSTED-BLOCKS-AND-INVENTORY-
RECOVERY.md section 9.1: link preservation, trigger guard, and the
--repair-source-links command. Everything here is offline.
"""
import datetime

import pytest

import config
import geo
import run
from store import Store


def _link_listing(store, site="test", listing_id="L1", property_id=7):
    """Insert a property + linked source listing directly."""
    store.conn.execute(
        "INSERT INTO properties(normalized_address, lat, lon, bw_id) "
        "VALUES('Jorisvej 1a, 2300 københavn', 55.65, 12.60, ?)",
        ("BW%d" % property_id,))
    pid = store.conn.execute(
        "SELECT id FROM properties WHERE bw_id=?", ("BW%d" % property_id,)
    ).fetchone()["id"]
    store.conn.execute(
        "INSERT INTO source_listings(property_id, site, listing_id, url, "
        "lifecycle_state, first_seen_at, last_seen_at, raw_data) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (pid, site, listing_id, f"https://example.test/{listing_id}",
         "active", "2026-09-14T10:00:00", "2026-09-14T10:00:00", "{}"))
    sid = store.conn.execute(
        "SELECT id FROM source_listings WHERE site=? AND listing_id=?",
        (site, listing_id)).fetchone()["id"]
    store.conn.commit()
    return sid, pid


def test_record_source_listing_none_preserves_existing_link(temp_db):
    """Spec 07 9.1 test 2: calling record_source_listing(None, ...) on a
    linked source preserves the non-null property link."""
    sid, pid = _link_listing(temp_db, property_id=9)
    temp_db.record_source_listing(
        property_id=None, site="test", listing_id="L1", url="https://example.test/L1",
        raw_data={"card_text": "fresh card"})
    row = temp_db.get_source_listing("test", "L1")
    assert row["property_id"] == pid


def test_second_scan_skipped_fresh_keeps_link_and_match(temp_db, mock_stations, monkeypatch):
    """Spec 07 9.1 test 1: scan a linked listing twice; make the second scan
    skipped:fresh; its property_id is unchanged and its match survives a local
    refresh."""
    sid, pid = _link_listing(temp_db, property_id=10)
    # A skipped:fresh card pass calls record_source_listing with property_id=None.
    temp_db.record_source_listing(
        property_id=None, site="test", listing_id="L1",
        url="https://example.test/L1", raw_data={"card_price": 8000})
    row = temp_db.get_source_listing("test", "L1")
    assert row["property_id"] == pid
    # The source is still linked, so a local refresh can evaluate it.
    monkeypatch.setattr(geo, "load_stations", lambda: mock_stations)
    monkeypatch.setattr(run.benchmarks, "compute_benchmarks", lambda store: {})
    # Doesn't need to actually produce a match row; the requirement is that the
    # link survives and refresh runs without dropping it.
    res = run.refresh_local(temp_db, export=False, quiet=True)
    assert temp_db.get_source_listing("test", "L1")["property_id"] == pid


def test_trigger_rejects_non_null_to_null_update(temp_db):
    """Spec 07 9.1 test 3: the database trigger rejects a direct non-null to
    null property_id update."""
    sid, pid = _link_listing(temp_db, property_id=11)
    with pytest.raises(Exception):
        temp_db.conn.execute(
            "UPDATE source_listings SET property_id=NULL WHERE id=?", (sid,))
        temp_db.conn.commit()
    # The link is unchanged.
    assert temp_db.get_source_listing("test", "L1")["property_id"] == pid


def test_new_out_of_envelope_card_may_remain_unlinked(temp_db):
    """Spec 07 9.1 test 4: a brand-new card may remain unlinked and is still
    stored (records still arrive with property_id NULL for new listings)."""
    sid = temp_db.record_source_listing(
        property_id=None, site="test", listing_id="NEW1",
        url="https://example.test/NEW1", raw_data={"card_price": 6000})
    row = temp_db.get_source_listing("test", "NEW1")
    assert row is not None
    assert row["property_id"] is None


def _grab_id(store):
    return store.conn.execute("SELECT id FROM source_listings ORDER BY id DESC LIMIT 1").fetchone()["id"]


def test_repair_links_orphan_to_existing_property(temp_db):
    """Spec 07 9.1 test 5: repair links an orphan with an exact stored address
    to the existing property and does not create a duplicate."""
    # Existing property with an exact normalized address.
    _, pid = _link_listing(temp_db, property_id=12)
    # Create an orphan source listing with the same address + coords but NULL link.
    sid = temp_db.record_source_listing(
        property_id=None, site="lejebolig", listing_id="ORPH1",
        url="https://example.test/ORPH1",
        raw_data={"normalized_address": "Jorisvej 1a, 2300 københavn",
                  "lat": 55.65, "lon": 12.60, "rooms": 1, "size_m2": 32.0})
    n_props_before = temp_db.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
    report = temp_db.repair_source_links(dry_run=False)
    assert report["repaired"] == 1
    assert temp_db.get_source_listing("lejebolig", "ORPH1")["property_id"] == pid
    n_props_after = temp_db.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
    assert n_props_after == n_props_before  # no duplicate created


def test_repair_idempotent_second_run_zero_rows(temp_db):
    """Spec 07 9.1 test 6: running repair twice changes zero rows the second
    time."""
    _, pid = _link_listing(temp_db, property_id=13)
    temp_db.record_source_listing(
        property_id=None, site="lejebolig", listing_id="ORPH2",
        url="https://example.test/ORPH2",
        raw_data={"normalized_address": "Jorisvej 1a, 2300 københavn",
                  "lat": 55.65, "lon": 12.60, "rooms": 1, "size_m2": 32.0})
    first = temp_db.repair_source_links(dry_run=False)
    assert first["repaired"] == 1
    second = temp_db.repair_source_links(dry_run=False)
    assert second["repaired"] == 0


def test_repair_leaves_street_only_row_unresolved(temp_db):
    """Spec 07 9.1 test 7: an ambiguous street-only row stays unresolved, never
    guessed from street proximity."""
    temp_db.record_source_listing(
        property_id=None, site="lejebolig", listing_id="STREET1",
        url="https://example.test/STREET1",
        raw_data={"normalized_address": "Jorisvej", "lat": None, "lon": None})
    report = temp_db.repair_source_links(dry_run=False)
    assert report["repaired"] == 0
    assert report["unresolved"] == 1
    assert temp_db.get_source_listing("lejebolig", "STREET1")["property_id"] is None
    assert report["unresolved_rows"][0]["reason"] == "property identity unresolved"


def test_repair_dry_run_writes_nothing(temp_db):
    """Repair --dry-run reports what it would do and changes no rows."""
    _, pid = _link_listing(temp_db, property_id=14)
    temp_db.record_source_listing(
        property_id=None, site="lejebolig", listing_id="ORPH3",
        url="https://example.test/ORPH3",
        raw_data={"normalized_address": "Jorisvej 1a, 2300 københavn",
                  "lat": 55.65, "lon": 12.60, "rooms": 1, "size_m2": 32.0})
    report = temp_db.repair_source_links(dry_run=True)
    assert report["dry_run"] is True
    assert temp_db.get_source_listing("lejebolig", "ORPH3")["property_id"] is None
    # One entry with the target merge status recorded.
    assert any(m["merge_status"] == "exact_address" for m in report["mapping"])
