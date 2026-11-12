"""Tests for SQLite database migrations, schema idempotency, and data retention."""
import os
import sqlite3
import tempfile
import pytest
from store import Store, format_bw_id


def test_migrations_can_run_twice_without_error_or_loss():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    try:
        # 1. Create a legacy v2.0 database with existing seen and matches
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE seen (
                site TEXT, listing_id TEXT, first_seen_at TEXT,
                PRIMARY KEY (site, listing_id)
            )""")
        conn.execute("""
            CREATE TABLE matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT, listing_id TEXT, tier TEXT, badge TEXT,
                title TEXT, address TEXT, rent_base REAL, utilities_amount REAL,
                rent_total REAL, move_in_price REAL, nearest_station TEXT,
                straight_dist_m REAL, distance_m REAL, walk_min INTEGER, bike_min INTEGER,
                available_from TEXT, rental_period TEXT, url TEXT, photo_url TEXT,
                kitchen_status TEXT, is_sydhavn INTEGER DEFAULT 0,
                issues TEXT, alert_text TEXT, first_seen_at TEXT
            )""")
        conn.execute("""
            INSERT INTO seen(site, listing_id, first_seen_at) VALUES('lejebolig', '101', '2026-09-01T10:00:00')
        """)
        conn.execute("""
            INSERT INTO matches(site, listing_id, tier, title, address, rent_total, first_seen_at)
            VALUES('lejebolig', '101', 'cat1', 'Lejlighed på Vesterbro', 'Vesterbrogade 1', 8500, '2026-09-01T10:00:00')
        """)
        conn.commit()
        conn.close()

        # 2. First migration run with Store
        store1 = Store(db_path=path)
        stats1 = store1.get_stats()
        assert stats1["total_seen"] == 1
        assert stats1["total_matches"] == 1

        # Verify property was created and linked
        cur = store1.conn.execute("SELECT * FROM properties")
        props1 = cur.fetchall()
        assert len(props1) == 1
        assert props1[0]["normalized_address"] == "Vesterbrogade 1"

        store1.conn.close()

        # 3. Second migration run with Store (must be idempotent)
        store2 = Store(db_path=path)
        stats2 = store2.get_stats()
        assert stats2["total_seen"] == 1
        assert stats2["total_matches"] == 1

        cur = store2.conn.execute("SELECT * FROM properties")
        props2 = cur.fetchall()
        assert len(props2) == 1

        # Check all required v2.1 tables exist
        tables = [r[0] for r in store2.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for req_table in ["properties", "source_listings", "observations", "matches", "scan_runs", "incidents", "quarantine"]:
            assert req_table in tables

        store2.conn.close()

    finally:
        if os.path.exists(path):
            os.remove(path)


V3_MATCH_COLUMNS = [
    "commute_min", "commute_route", "size_m2", "kr_per_m2", "value_ratio",
    "benchmark_median", "benchmark_segment", "buzz_score", "buzz_detail",
    "major_street", "bus_routes", "water_dist_m", "water_name", "relevance",
    "alerted_actionable", "geo_precision", "features_pending",
]
V3_TABLES = ["place_features", "market_benchmarks", "user_actions"]


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_v3_migration_adds_every_column_and_table_without_data_loss():
    """Migrating a real v2.1 schema must be additive and lossless."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # A v2.1 database, built by the v2.1 code path itself.
        store_v21 = Store(db_path=path)
        pid = store_v21._find_or_create_property_direct(
            "Vesterbrogade 1", 55.67, 12.55, 1, 32.0, "new", "2026-09-01T10:00:00")
        store_v21.conn.execute("""
            INSERT INTO matches(property_id, bw_id, category, score, rent_total, address)
            VALUES(?,?,1,88,8500,'Vesterbrogade 1')
        """, (pid, format_bw_id(pid)))
        store_v21.conn.execute(
            "INSERT INTO seen(site, listing_id, first_seen_at) VALUES('lejebolig','101','2026-09-01T10:00:00')")
        store_v21.conn.commit()

        # Strip the v3 additions back out to simulate the pre-upgrade database.
        for table in V3_TABLES:
            store_v21.conn.execute(f"DROP TABLE IF EXISTS {table}")
        store_v21.conn.commit()
        store_v21.conn.close()

        store_v3 = Store(db_path=path)
        cols = _columns(store_v3.conn, "matches")
        for col in V3_MATCH_COLUMNS:
            assert col in cols, f"matches is missing {col}"
        for col in ("postcode", "geo_precision", "rank_priority", "rank_priority_kind", "rank_priority_note", "rank_priority_at"):
            assert col in _columns(store_v3.conn, "properties"), f"properties is missing {col}"

        tables = {r[0] for r in store_v3.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table in V3_TABLES + ["search_profile"]:
            assert table in tables

        stats = store_v3.get_stats()
        assert stats["total_matches"] == 1
        assert stats["total_seen"] == 1
        row = store_v3.get_match_row(pid)
        assert row["rent_total"] == 8500
        assert row["alerted_actionable"] == 0
        store_v3.conn.close()
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_place_features_round_trip(temp_db):
    payload = {"commute_min": 18, "commute_route": '{"mode": "walk"}',
               "buzz_score": 0.75, "buzz_detail": "Fronts Testvej (primary), 3 bus routes",
               "major_street": "Testvej", "major_street_dist_m": 21.0, "bus_routes": 3,
               "water_dist_m": 640.0, "water_name": "Havnen", "water_kind": "harbour",
               "provider": "overpass"}
    temp_db.save_place_features("55.65098,12.50988", payload)
    got = temp_db.get_place_features("55.65098,12.50988")
    assert got["commute_min"] == 18
    assert got["buzz_score"] == 0.75
    assert got["water_kind"] == "harbour"
    assert got["computed_at"]

    # Upsert on the same key overwrites rather than duplicating.
    temp_db.save_place_features("55.65098,12.50988", dict(payload, commute_min=22))
    assert temp_db.get_place_features("55.65098,12.50988")["commute_min"] == 22
    n = temp_db.conn.execute("SELECT COUNT(*) FROM place_features").fetchone()[0]
    assert n == 1


def test_user_action_state_is_the_latest_action(temp_db):
    pid = temp_db._find_or_create_property_direct(
        "Jorisvej 1a", 55.65, 12.60, 1, 32.0, "new", "2026-09-01T10:00:00")
    assert temp_db.get_action_state(pid) is None

    temp_db.record_user_action(pid, "save", "nice light")
    assert temp_db.get_action_state(pid)["action"] == "save"
    temp_db.record_user_action(pid, "applied")
    assert temp_db.get_action_state(pid)["action"] == "applied"
    temp_db.record_user_action(pid, "unsave")
    assert temp_db.get_action_state(pid) is None
    assert pid not in temp_db.get_dismissed_ids()

    temp_db.record_user_action(pid, "dismiss", "too far")
    assert temp_db.get_dismissed_ids() == {pid}
    # Every action is kept as history, only the latest one is the state.
    assert temp_db.conn.execute("SELECT COUNT(*) FROM user_actions").fetchone()[0] == 4


def test_lifecycle_transitions_report_affected_properties(temp_db):
    import config
    pid = temp_db._find_or_create_property_direct(
        "Jorisvej 1a", 55.65, 12.60, 1, 32.0, "new", "2026-09-01T10:00:00")
    sid = temp_db.record_source_listing(pid, "test", "L1", "http://x")

    for _ in range(config.EXPIRED_SCANS_THRESHOLD):
        transitions = temp_db.update_lifecycle_after_scan("test", [])
    assert transitions["expired"] == 1
    assert transitions["expired_properties"] == [pid]
    assert temp_db.lifecycle_state_for(pid) == "expired"

    transitions = temp_db.update_lifecycle_after_scan("test", [sid])
    assert transitions["relisted_properties"] == [pid]
    assert temp_db.lifecycle_state_for(pid) == "relisted"


def test_quarantine_is_resolved_when_a_listing_no_longer_qualifies(temp_db):
    """The backlog must shrink as well as grow, or the digest count lies."""
    pid = temp_db._find_or_create_property_direct(
        "Jorisvej 1a", 55.65, 12.60, 1, 32.0, "new", "2026-09-01T10:00:00")
    sid = temp_db.record_source_listing(pid, "test", "L1", "http://x")
    temp_db.log_quarantine(pid, sid, "test", "L1", ["ungeocodable address"])
    assert len(temp_db.get_quarantined_listings()) == 1

    assert temp_db.resolve_quarantine("test", "L1") == 1
    assert temp_db.get_quarantined_listings() == []
    assert temp_db.get_stats()["quarantine_count"] == 0
    # Resolving twice is a no-op, not an error.
    assert temp_db.resolve_quarantine("test", "L1") == 0
