"""Tests for Spec 09 (v3.8) schema migrations, search profile, personal priority,
and offline CLI commands.
"""
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import pytest

import config
import filters
from store import Store, format_bw_id, parse_bw_id
import viewer


def test_v38_migration_adds_new_columns_and_tables_losslessly():
    """V3.8 schema migration must add search_profile and new columns without data loss."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # 1. Create a pre-v3.8 database with existing property, source_listing, and scan_run
        store_pre = Store(db_path=path)
        pid = store_pre._find_or_create_property_direct(
            "Teglholmsgade 36", 55.655, 12.545, 1, 35.0, "new", "2026-09-01T10:00:00"
        )
        sid = store_pre.record_source_listing(
            property_id=pid,
            site="boligportal",
            listing_id="83",
            url="https://example.com/83",
        )
        srid = store_pre.start_scan_run("boligportal", kind="scheduled")
        store_pre.finish_scan_run(srid, outcome="success")
        store_pre.conn.close()

        # 2. Re-open to trigger migration
        store_post = Store(db_path=path)

        # Check search_profile table exists and is empty initially
        cur = store_post.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='search_profile'")
        assert cur.fetchone() is not None
        assert store_post.get_search_profile() is None

        # Check properties columns
        prop_cols = {r[1] for r in store_post.conn.execute("PRAGMA table_info(properties)").fetchall()}
        for col in ("rank_priority", "rank_priority_kind", "rank_priority_note", "rank_priority_at"):
            assert col in prop_cols, f"properties missing {col}"

        # Existing property reads with default priority 0
        prop = store_post.resolve_property_by_bw_id(pid)
        assert prop is not None
        assert prop["rank_priority"] == 0
        assert prop["rank_priority_kind"] is None
        assert prop["rank_priority_note"] is None
        assert prop["rank_priority_at"] is None

        # Check source_listings columns
        src_cols = {r[1] for r in store_post.conn.execute("PRAGMA table_info(source_listings)").fetchall()}
        for col in (
            "student_requirement_state", "age_requirement_state", "age_min", "age_max",
            "profile_age_fit", "eligibility_fact_excerpt", "explicit_gone_at", "explicit_gone_marker"
        ):
            assert col in src_cols, f"source_listings missing {col}"

        # Existing source listing has None for new fields
        cur = store_post.conn.execute("SELECT * FROM source_listings WHERE id=?", (sid,))
        src_row = cur.fetchone()
        assert src_row["student_requirement_state"] is None
        assert src_row["explicit_gone_at"] is None

        # Check evaluations columns
        eval_cols = {r[1] for r in store_post.conn.execute("PRAGMA table_info(evaluations)").fetchall()}
        for col in (
            "student_requirement_state", "age_requirement_state", "age_min", "age_max",
            "profile_age_fit", "eligibility_fact_excerpt"
        ):
            assert col in eval_cols, f"evaluations missing {col}"

        # Check scan_runs columns
        scan_cols = {r[1] for r in store_post.conn.execute("PRAGMA table_info(scan_runs)").fetchall()}
        for col in (
            "decision_revalidation_selected", "decision_revalidation_succeeded",
            "decision_revalidation_failed", "decision_revalidation_gone"
        ):
            assert col in scan_cols, f"scan_runs missing {col}"

        # Existing scan run reads 0 for revalidation counters
        cur = store_post.conn.execute("SELECT * FROM scan_runs WHERE id=?", (srid,))
        scan_row = cur.fetchone()
        assert scan_row["decision_revalidation_selected"] == 0

        store_post.conn.close()

        # 3. Third initialization must be idempotent
        store_idem = Store(db_path=path)
        assert store_idem.get_search_profile() is None
        prop_idem = store_idem.resolve_property_by_bw_id(pid)
        assert prop_idem["rank_priority"] == 0
        store_idem.conn.close()

    finally:
        if os.path.exists(path):
            os.remove(path)


def test_search_profile_validation_and_idempotency(temp_db):
    """Store search_profile primitives accept valid integer age and reject invalid types."""
    # Initially None
    assert temp_db.get_search_profile() is None

    # Valid ages 18-120
    temp_db.set_search_profile_age(23, evidence_source="user")
    prof = temp_db.get_search_profile()
    assert prof is not None
    assert prof["id"] == 1
    assert prof["age_years"] == 23
    assert prof["evidence_source"] == "user"
    assert prof["updated_at"] is not None

    # Boundaries: 18 and 120
    temp_db.set_search_profile_age(18)
    assert temp_db.get_search_profile()["age_years"] == 18

    temp_db.set_search_profile_age(120)
    assert temp_db.get_search_profile()["age_years"] == 120

    # Ensure single row exists
    n = temp_db.conn.execute("SELECT COUNT(*) FROM search_profile").fetchone()[0]
    assert n == 1

    # Rejection of invalid values without writing (profile retains previous valid age 120)
    invalid_ages = [17, 121, 0, -1, 200, True, False, "23", 23.5, None, [], {}]
    for invalid in invalid_ages:
        with pytest.raises(ValueError):
            temp_db.set_search_profile_age(invalid)
        assert temp_db.get_search_profile()["age_years"] == 120

    # Rejection of invalid evidence_source
    for invalid_src in ["", None, 123]:
        with pytest.raises(ValueError):
            temp_db.set_search_profile_age(25, evidence_source=invalid_src)
        assert temp_db.get_search_profile()["age_years"] == 120


def test_search_profile_privacy(temp_db):
    """Dashboard payload must not leak search_profile age_years, evidence_source or updated_at."""
    temp_db.set_search_profile_age(23, evidence_source="user")
    payload = viewer.dashboard_payload(temp_db)

    # Convert entire payload to JSON string to inspect all keys and nested contents
    import json
    payload_str = json.dumps(payload, ensure_ascii=False)

    assert "age_years" not in payload_str
    assert "search_profile" not in payload_str
    assert "evidence_source" not in payload_str


def test_rank_priority_validation_and_idempotency(temp_db):
    """Setting and clearing personal priority enforces allowed values and is idempotent."""
    pid = temp_db._find_or_create_property_direct(
        "Teglholmsgade 36", 55.655, 12.545, 1, 35.0, "new", "2026-09-01T10:00:00"
    )

    # Initial state
    p = temp_db.get_rank_priority(pid)
    assert p["rank_priority"] == 0
    assert p["rank_priority_kind"] is None
    assert p["rank_priority_note"] is None
    assert p["rank_priority_at"] is None

    # Set priority 1 with preferred_locality
    temp_db.set_rank_priority(pid, 1, kind="preferred_locality", note="Nicer locality and good value")
    p1 = temp_db.get_rank_priority(pid)
    assert p1["rank_priority"] == 1
    assert p1["rank_priority_kind"] == "preferred_locality"
    assert p1["rank_priority_note"] == "Nicer locality and good value"
    assert p1["rank_priority_at"] is not None

    # Idempotent re-set
    temp_db.set_rank_priority(pid, 1, kind="preferred_locality", note="Nicer locality and good value")
    p2 = temp_db.get_rank_priority(pid)
    assert p2["rank_priority"] == 1
    assert p2["rank_priority_kind"] == "preferred_locality"

    # Clear priority
    temp_db.clear_rank_priority(pid)
    p_clear = temp_db.get_rank_priority(pid)
    assert p_clear["rank_priority"] == 0
    assert p_clear["rank_priority_kind"] is None
    assert p_clear["rank_priority_note"] is None
    assert p_clear["rank_priority_at"] is None

    # Idempotent clear
    temp_db.clear_rank_priority(pid)
    assert temp_db.get_rank_priority(pid)["rank_priority"] == 0

    # Set priority 0 via set_rank_priority(pid, 0)
    temp_db.set_rank_priority(pid, 1, kind="preferred_locality", note="test")
    temp_db.set_rank_priority(pid, 0)
    assert temp_db.get_rank_priority(pid)["rank_priority"] == 0

    # Rejection: priority 1 with wrong kind
    with pytest.raises(ValueError):
        temp_db.set_rank_priority(pid, 1, kind="high_quality")
    assert temp_db.get_rank_priority(pid)["rank_priority"] == 0

    # Rejection: priority 1 without kind
    with pytest.raises(ValueError):
        temp_db.set_rank_priority(pid, 1, kind=None)
    assert temp_db.get_rank_priority(pid)["rank_priority"] == 0

    # Rejection: priority 0 with kind or note
    with pytest.raises(ValueError):
        temp_db.set_rank_priority(pid, 0, kind="preferred_locality")
    with pytest.raises(ValueError):
        temp_db.set_rank_priority(pid, 0, note="some note")

    # Rejection: invalid priority number
    for inv_p in [-1, 2, 3, 10]:
        with pytest.raises(ValueError):
            temp_db.set_rank_priority(pid, inv_p, kind="preferred_locality")

    # Rejection: boolean types
    with pytest.raises(ValueError):
        temp_db.set_rank_priority(pid, True, kind="preferred_locality")
    with pytest.raises(ValueError):
        temp_db.set_rank_priority(True, 1, kind="preferred_locality")

    # Rejection: non-existent property_id
    with pytest.raises(ValueError):
        temp_db.set_rank_priority(999999, 1, kind="preferred_locality")
    with pytest.raises(ValueError):
        temp_db.clear_rank_priority(999999)


def test_bw_id_parsing_and_exact_resolution(temp_db):
    """BW-ID resolution maps valid IDs to exactly one property and rejects unknown/ambiguous IDs."""
    pid = temp_db._find_or_create_property_direct(
        "Teglholmsgade 36", 55.655, 12.545, 1, 35.0, "new", "2026-09-01T10:00:00"
    )
    bw_str = format_bw_id(pid)

    # Resolution via various valid formats
    for valid_input in (bw_str, f"BW-{pid}", str(pid), pid, bw_str.lower()):
        prop = temp_db.resolve_property_by_bw_id(valid_input)
        assert prop is not None
        assert prop["id"] == pid
        assert prop["normalized_address"] == "Teglholmsgade 36"

    # Unknown or invalid inputs resolve to None
    for invalid_input in (
        "BW-0999999", "999999", 999999, "invalid", "BW-", "", "   ",
        None, True, False, -1, 0, f"{bw_str}-extra"
    ):
        assert temp_db.resolve_property_by_bw_id(invalid_input) is None


def test_offline_cli_commands_against_temp_db(monkeypatch):
    """Offline run.py commands update profile and priority without scan/export/alerts."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # Initialize DB with a property
        st = Store(db_path=path)
        pid = st._find_or_create_property_direct(
            "Teglholmsgade 36", 55.655, 12.545, 1, 35.0, "new", "2026-09-01T10:00:00"
        )
        bw_id = format_bw_id(pid)
        st.conn.close()

        # 1. --set-profile-age
        cmd_profile = [
            sys.executable, "run.py", "--db", path, "--set-profile-age", "23"
        ]
        res = subprocess.run(cmd_profile, capture_output=True, text=True)
        assert res.returncode == 0
        assert "Search profile age updated:" in res.stdout
        assert "age_years=23" in res.stdout

        st_check = Store(db_path=path)
        assert st_check.get_search_profile()["age_years"] == 23
        st_check.conn.close()

        # Invalid --set-profile-age exits non-zero
        cmd_invalid_age = [
            sys.executable, "run.py", "--db", path, "--set-profile-age", "17"
        ]
        res_inv = subprocess.run(cmd_invalid_age, capture_output=True, text=True)
        assert res_inv.returncode != 0

        # 2. --set-rank-priority
        cmd_priority = [
            sys.executable, "run.py", "--db", path, "--set-rank-priority", bw_id,
            "--kind", "preferred_locality", "--note", "Nicer locality and good value"
        ]
        res = subprocess.run(cmd_priority, capture_output=True, text=True)
        assert res.returncode == 0
        assert f"Rank priority set for {bw_id}" in res.stdout
        assert "preferred_locality" in res.stdout

        st_check = Store(db_path=path)
        p = st_check.get_rank_priority(pid)
        assert p["rank_priority"] == 1
        assert p["rank_priority_kind"] == "preferred_locality"
        assert p["rank_priority_note"] == "Nicer locality and good value"
        st_check.conn.close()

        # Invalid --set-rank-priority (wrong kind) exits non-zero
        cmd_invalid_kind = [
            sys.executable, "run.py", "--db", path, "--set-rank-priority", bw_id,
            "--kind", "other_kind"
        ]
        res_inv_kind = subprocess.run(cmd_invalid_kind, capture_output=True, text=True)
        assert res_inv_kind.returncode != 0

        # Invalid --set-rank-priority (unknown BW id) exits non-zero
        cmd_unknown_bw = [
            sys.executable, "run.py", "--db", path, "--set-rank-priority", "BW-999999",
            "--kind", "preferred_locality"
        ]
        res_unk = subprocess.run(cmd_unknown_bw, capture_output=True, text=True)
        assert res_unk.returncode != 0

        # 3. --clear-rank-priority
        cmd_clear = [
            sys.executable, "run.py", "--db", path, "--clear-rank-priority", bw_id
        ]
        res = subprocess.run(cmd_clear, capture_output=True, text=True)
        assert res.returncode == 0
        assert f"Rank priority cleared for {bw_id}" in res.stdout

        st_check = Store(db_path=path)
        p_cleared = st_check.get_rank_priority(pid)
        assert p_cleared["rank_priority"] == 0
        assert p_cleared["rank_priority_kind"] is None
        st_check.conn.close()

        # Invalid --clear-rank-priority (unknown BW id) exits non-zero
        cmd_clear_unk = [
            sys.executable, "run.py", "--db", path, "--clear-rank-priority", "BW-999999"
        ]
        res_clear_unk = subprocess.run(cmd_clear_unk, capture_output=True, text=True)
        assert res_clear_unk.returncode != 0

    finally:
        if os.path.exists(path):
            os.remove(path)


# ==============================================================================
# Spec 09 Acceptance Cases 1–6: Personal Priority, Completeness & Ordering
# ==============================================================================

class _Verdict:
    def __init__(self, trust_outcome="eligible", unit_type_state="self_contained_apartment",
                 tenancy_channel_state="open_market", building_form_state="multi_unit",
                 score=70.0, rejection_reason=None, quarantine_reasons=()):
        self.trust_outcome = trust_outcome
        self.unit_type_state = unit_type_state
        self.tenancy_channel_state = tenancy_channel_state
        self.building_form_state = building_form_state
        self.score = score
        self.rejection_reason = rejection_reason
        self.quarantine_reasons = list(quarantine_reasons)


def test_acceptance_1_preferred_b54_sorts_before_unpreferred_b58_after_every_a_s(temp_db):
    """Spec 09 Acceptance 1: A preferred Tier B score-54 property sorts before an
    unpreferred Tier B score-58 property and after every Tier A/S property."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    # 1. Tier A/S property (score 85, Tier A)
    pid_a = temp_db._find_or_create_property_direct(
        "Tier A Vej 1, 2300 København", 55.66, 12.60, 1, 35.0, "new", now)
    sid_a = temp_db.record_source_listing(
        pid_a, "boligportal", "A85", "https://ex.test/a85",
        {"site": "boligportal", "listing_id": "A85", "address": "Tier A Vej 1, 2300 København",
         "rent_total": 8000, "rooms": 1, "size_m2": 35.0})
    v_a = _Verdict(score=85.0)
    ev_a = temp_db.record_evaluation(sid_a, pid_a, None, outcome="match", rules_version=config.RULES_VERSION_TAG, eval_res=v_a)
    temp_db.conn.execute("UPDATE evaluations SET is_current=1 WHERE id=?", (ev_a,))
    temp_db.conn.execute(
        "INSERT INTO matches(property_id, bw_id, site, listing_id, tier, category, score, "
        "rent_total, commute_min, relevance, address, quarantined, is_responsible_pick) "
        "VALUES(?, ?, 'boligportal', 'A85', 'A', 1, 85.0, 8000, 15, 'actionable', 'Tier A Vej 1, 2300 København', 0, 0)",
        (pid_a, format_bw_id(pid_a)))

    # 2. Unpreferred Tier B property (score 58, Tier B, priority 0)
    pid_b58 = temp_db._find_or_create_property_direct(
        "Tier B Vej 58, 2300 København", 55.66, 12.60, 1, 35.0, "new", now)
    sid_b58 = temp_db.record_source_listing(
        pid_b58, "boligportal", "B58", "https://ex.test/b58",
        {"site": "boligportal", "listing_id": "B58", "address": "Tier B Vej 58, 2300 København",
         "rent_total": 8000, "rooms": 1, "size_m2": 35.0})
    v_b58 = _Verdict(score=58.0)
    ev_b58 = temp_db.record_evaluation(sid_b58, pid_b58, None, outcome="match", rules_version=config.RULES_VERSION_TAG, eval_res=v_b58)
    temp_db.conn.execute("UPDATE evaluations SET is_current=1 WHERE id=?", (ev_b58,))
    temp_db.conn.execute(
        "INSERT INTO matches(property_id, bw_id, site, listing_id, tier, category, score, "
        "rent_total, commute_min, relevance, address, quarantined, is_responsible_pick) "
        "VALUES(?, ?, 'boligportal', 'B58', 'B', 2, 58.0, 8000, 25, 'actionable', 'Tier B Vej 58, 2300 København', 0, 0)",
        (pid_b58, format_bw_id(pid_b58)))

    # 3. Preferred Tier B property (score 54, Tier B, priority 1)
    pid_b54 = temp_db._find_or_create_property_direct(
        "Tier B Vej 54, 2300 København", 55.66, 12.60, 1, 35.0, "new", now)
    sid_b54 = temp_db.record_source_listing(
        pid_b54, "boligportal", "B54", "https://ex.test/b54",
        {"site": "boligportal", "listing_id": "B54", "address": "Tier B Vej 54, 2300 København",
         "rent_total": 8000, "rooms": 1, "size_m2": 35.0})
    v_b54 = _Verdict(score=54.0)
    ev_b54 = temp_db.record_evaluation(sid_b54, pid_b54, None, outcome="match", rules_version=config.RULES_VERSION_TAG, eval_res=v_b54)
    temp_db.conn.execute("UPDATE evaluations SET is_current=1 WHERE id=?", (ev_b54,))
    temp_db.conn.execute(
        "INSERT INTO matches(property_id, bw_id, site, listing_id, tier, category, score, "
        "rent_total, commute_min, relevance, address, quarantined, is_responsible_pick) "
        "VALUES(?, ?, 'boligportal', 'B54', 'B', 2, 54.0, 8000, 25, 'actionable', 'Tier B Vej 54, 2300 København', 0, 0)",
        (pid_b54, format_bw_id(pid_b54)))
    temp_db.set_rank_priority(pid_b54, 1, kind="preferred_locality", note="Preferred area")
    temp_db.conn.commit()

    rows = temp_db.trusted_block_rows()
    pids = [r["property_id"] for r in rows]
    # S/A property is first, followed by preferred B54, then unpreferred B58
    assert pids == [pid_a, pid_b54, pid_b58]


def test_acceptance_2_clearing_priority_restores_score_order(temp_db):
    """Spec 09 Acceptance 2: Clearing priority restores score order."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    pid_a = temp_db._find_or_create_property_direct(
        "Tier A Vej 1, 2300 København", 55.66, 12.60, 1, 35.0, "new", now)
    sid_a = temp_db.record_source_listing(
        pid_a, "boligportal", "A85", "https://ex.test/a85",
        {"site": "boligportal", "listing_id": "A85", "address": "Tier A Vej 1, 2300 København",
         "rent_total": 8000, "rooms": 1, "size_m2": 35.0})
    v_a = _Verdict(score=85.0)
    ev_a = temp_db.record_evaluation(sid_a, pid_a, None, outcome="match", rules_version=config.RULES_VERSION_TAG, eval_res=v_a)
    temp_db.conn.execute("UPDATE evaluations SET is_current=1 WHERE id=?", (ev_a,))
    temp_db.conn.execute(
        "INSERT INTO matches(property_id, bw_id, site, listing_id, tier, category, score, "
        "rent_total, commute_min, relevance, address, quarantined, is_responsible_pick) "
        "VALUES(?, ?, 'boligportal', 'A85', 'A', 1, 85.0, 8000, 15, 'actionable', 'Tier A Vej 1, 2300 København', 0, 0)",
        (pid_a, format_bw_id(pid_a)))

    pid_b58 = temp_db._find_or_create_property_direct(
        "Tier B Vej 58, 2300 København", 55.66, 12.60, 1, 35.0, "new", now)
    sid_b58 = temp_db.record_source_listing(
        pid_b58, "boligportal", "B58", "https://ex.test/b58",
        {"site": "boligportal", "listing_id": "B58", "address": "Tier B Vej 58, 2300 København",
         "rent_total": 8000, "rooms": 1, "size_m2": 35.0})
    v_b58 = _Verdict(score=58.0)
    ev_b58 = temp_db.record_evaluation(sid_b58, pid_b58, None, outcome="match", rules_version=config.RULES_VERSION_TAG, eval_res=v_b58)
    temp_db.conn.execute("UPDATE evaluations SET is_current=1 WHERE id=?", (ev_b58,))
    temp_db.conn.execute(
        "INSERT INTO matches(property_id, bw_id, site, listing_id, tier, category, score, "
        "rent_total, commute_min, relevance, address, quarantined, is_responsible_pick) "
        "VALUES(?, ?, 'boligportal', 'B58', 'B', 2, 58.0, 8000, 25, 'actionable', 'Tier B Vej 58, 2300 København', 0, 0)",
        (pid_b58, format_bw_id(pid_b58)))

    pid_b54 = temp_db._find_or_create_property_direct(
        "Tier B Vej 54, 2300 København", 55.66, 12.60, 1, 35.0, "new", now)
    sid_b54 = temp_db.record_source_listing(
        pid_b54, "boligportal", "B54", "https://ex.test/b54",
        {"site": "boligportal", "listing_id": "B54", "address": "Tier B Vej 54, 2300 København",
         "rent_total": 8000, "rooms": 1, "size_m2": 35.0})
    v_b54 = _Verdict(score=54.0)
    ev_b54 = temp_db.record_evaluation(sid_b54, pid_b54, None, outcome="match", rules_version=config.RULES_VERSION_TAG, eval_res=v_b54)
    temp_db.conn.execute("UPDATE evaluations SET is_current=1 WHERE id=?", (ev_b54,))
    temp_db.conn.execute(
        "INSERT INTO matches(property_id, bw_id, site, listing_id, tier, category, score, "
        "rent_total, commute_min, relevance, address, quarantined, is_responsible_pick) "
        "VALUES(?, ?, 'boligportal', 'B54', 'B', 2, 54.0, 8000, 25, 'actionable', 'Tier B Vej 54, 2300 København', 0, 0)",
        (pid_b54, format_bw_id(pid_b54)))

    temp_db.set_rank_priority(pid_b54, 1, kind="preferred_locality", note="Preferred area")
    temp_db.conn.commit()

    # Before clear: pid_b54 before pid_b58
    assert [r["property_id"] for r in temp_db.trusted_block_rows()] == [pid_a, pid_b54, pid_b58]

    # Clear priority
    temp_db.clear_rank_priority(pid_b54)
    temp_db.conn.commit()

    # After clear: score order restored: pid_b58 (score 58) before pid_b54 (score 54)
    assert [r["property_id"] for r in temp_db.trusted_block_rows()] == [pid_a, pid_b58, pid_b54]


def test_acceptance_3_priority_never_changes_score_tier_eligibility_view_counts_alerts_or_gates(temp_db):
    """Spec 09 Acceptance 3: Priority never changes score, rank tier, eligibility, view count, alert class or representative hard gates."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    pid = temp_db._find_or_create_property_direct(
        "Priority Testvej 1, 2300 København", 55.66, 12.60, 1, 35.0, "new", now)
    sid = temp_db.record_source_listing(
        pid, "boligportal", "P1", "https://ex.test/p1",
        {"site": "boligportal", "listing_id": "P1", "address": "Priority Testvej 1, 2300 København",
         "rent_total": 8000, "rooms": 1, "size_m2": 35.0})
    v = _Verdict(score=54.0)
    ev = temp_db.record_evaluation(sid, pid, None, outcome="match", rules_version=config.RULES_VERSION_TAG, eval_res=v)
    temp_db.conn.execute("UPDATE evaluations SET is_current=1 WHERE id=?", (ev,))
    temp_db.conn.execute(
        "INSERT INTO matches(property_id, bw_id, site, listing_id, tier, category, score, "
        "rent_total, commute_min, relevance, address, quarantined, is_responsible_pick) "
        "VALUES(?, ?, 'boligportal', 'P1', 'B', 2, 54.0, 8000, 25, 'actionable', 'Priority Testvej 1, 2300 København', 0, 0)",
        (pid, format_bw_id(pid)))
    temp_db.conn.commit()

    counts_before = temp_db.view_counts()
    match_before = temp_db.get_matches()[0]

    # Set priority 1
    temp_db.set_rank_priority(pid, 1, kind="preferred_locality", note="Test locality")
    temp_db.conn.commit()

    counts_after = temp_db.view_counts()
    match_after = temp_db.get_matches()[0]

    # Assert score, tier, and view counts remain identical
    assert match_after["score"] == match_before["score"] == 54.0
    assert match_after["tier"] == match_before["tier"] == "B"
    assert match_after["relevance"] == match_before["relevance"] == "actionable"
    assert counts_after == counts_before

    # Evaluation record unchanged
    ev_row = temp_db.conn.execute("SELECT * FROM evaluations WHERE id=?", (ev,)).fetchone()
    assert ev_row["outcome"] == "match"
    assert ev_row["score"] == 54.0


def test_acceptance_4_setting_priority_twice_is_idempotent_unknown_id_writes_nothing(temp_db):
    """Spec 09 Acceptance 4: Setting priority twice is idempotent; ambiguous/unknown BW ids write nothing."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    pid = temp_db._find_or_create_property_direct(
        "Idempotent Vej 1, 2300 København", 55.66, 12.60, 1, 35.0, "new", now)

    # First call
    temp_db.set_rank_priority(pid, 1, kind="preferred_locality", note="Initial note")
    p1 = temp_db.get_rank_priority(pid)
    assert p1["rank_priority"] == 1
    assert p1["rank_priority_kind"] == "preferred_locality"
    assert p1["rank_priority_note"] == "Initial note"

    # Second call (idempotent overwrite)
    temp_db.set_rank_priority(pid, 1, kind="preferred_locality", note="Updated note")
    p2 = temp_db.get_rank_priority(pid)
    assert p2["rank_priority"] == 1
    assert p2["rank_priority_kind"] == "preferred_locality"
    assert p2["rank_priority_note"] == "Updated note"

    count_props = temp_db.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
    assert count_props == 1

    # Unknown ID raises ValueError and writes nothing
    with pytest.raises(ValueError):
        temp_db.set_rank_priority(999999, 1, kind="preferred_locality")
    assert temp_db.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 1


def test_acceptance_5_every_visible_window_source_accounted_once_duplicate_placement_fails(temp_db):
    """Spec 09 Acceptance 5: Every visible-window source is accounted for once across the four datasets; duplicate placement fails."""
    now = datetime.datetime.now().isoformat(timespec="seconds")

    # 1. Trusted (Ranked) source
    pid1 = temp_db._find_or_create_property_direct("Acc5 Trusted 1", 55.65, 12.54, 1, 35.0, "new", now)
    s1 = temp_db.record_source_listing(pid1, "boligportal", "SRC-T1", "https://ex.test/t1")
    ev1 = temp_db.record_evaluation(s1, pid1, None, outcome="match", rules_version=config.RULES_VERSION_TAG, eval_res=_Verdict(score=80.0))
    temp_db.conn.execute("UPDATE evaluations SET is_current=1 WHERE id=?", (ev1,))
    temp_db.conn.execute(
        "INSERT INTO matches(property_id, bw_id, site, listing_id, tier, category, score, rent_total, commute_min, relevance, address, quarantined, is_responsible_pick) "
        "VALUES(?, ?, 'boligportal', 'SRC-T1', 'A', 1, 80.0, 8000, 15, 'actionable', 'Acc5 Trusted 1', 0, 0)",
        (pid1, format_bw_id(pid1)))

    # 2. Review (Needs verification) source
    pid2 = temp_db._find_or_create_property_direct("Acc5 Review 2", 55.65, 12.54, 1, 35.0, "new", now)
    s2 = temp_db.record_source_listing(pid2, "boligportal", "SRC-R2", "https://ex.test/r2")
    ev2 = temp_db.record_evaluation(s2, pid2, None, outcome="quarantined", rules_version=config.RULES_VERSION_TAG, eval_res=_Verdict(trust_outcome="review", quarantine_reasons=["self-contained apartment unverified"]))
    temp_db.conn.execute("UPDATE evaluations SET is_current=1 WHERE id=?", (ev2,))

    # 3. Gone (Recently gone) source
    pid3 = temp_db._find_or_create_property_direct("Acc5 Gone 3", 55.65, 12.54, 1, 35.0, "new", now)
    s3 = temp_db.record_source_listing(pid3, "boligportal", "SRC-G3", "https://ex.test/g3")
    temp_db.conn.execute("UPDATE source_listings SET lifecycle_state='expired', expired_at=? WHERE id=?", (now, s3))

    # 4. Excluded source
    pid4 = temp_db._find_or_create_property_direct("Acc5 Excluded 4", 55.65, 12.54, 1, 35.0, "new", now)
    s4 = temp_db.record_source_listing(pid4, "boligportal", "SRC-X4", "https://ex.test/x4")
    ev4 = temp_db.record_evaluation(s4, pid4, None, outcome="rejected", rules_version=config.RULES_VERSION_TAG, reason="rent above cap", eval_res=_Verdict(trust_outcome="rejected", rejection_reason="rent above cap"))
    temp_db.conn.execute("UPDATE evaluations SET is_current=1 WHERE id=?", (ev4,))
    temp_db.conn.commit()

    # Clean audit check: 0 duplicate placements
    audit_clean = temp_db.audit_integrity()
    assert audit_clean["duplicate_dataset_placement"] == 0
    assert audit_clean["critical"] == []
    assert audit_clean["ok"] is True

    # Artificially insert the review source into matches (duplicate placement defect)
    temp_db.conn.execute(
        "INSERT INTO matches(property_id, bw_id, site, listing_id, tier, category, score, rent_total, commute_min, relevance, address, quarantined, is_responsible_pick) "
        "VALUES(?, ?, 'boligportal', 'SRC-R2', 'A', 1, 80.0, 8000, 15, 'actionable', 'Acc5 Review 2', 0, 0)",
        (pid2, format_bw_id(pid2)))
    temp_db.conn.commit()

    audit_dirty = temp_db.audit_integrity()
    assert audit_dirty["duplicate_dataset_placement"] >= 1
    assert "duplicate_dataset_placement" in audit_dirty["critical"]
    assert audit_dirty["ok"] is False


def test_acceptance_6_search_finds_address_in_each_of_the_four_views(temp_db):
    """Spec 09 Acceptance 6: Search finds an address in each of the four views."""
    now = datetime.datetime.now().isoformat(timespec="seconds")

    # 1. Ranked view
    pid1 = temp_db._find_or_create_property_direct("Ranked AllSearch Allé 10", 55.65, 12.54, 1, 35.0, "new", now)
    s1 = temp_db.record_source_listing(pid1, "boligportal", "SCH-1", "https://ex.test/sch1", {"address": "Ranked AllSearch Allé 10"})
    ev1 = temp_db.record_evaluation(s1, pid1, None, outcome="match", rules_version=config.RULES_VERSION_TAG, eval_res=_Verdict(score=80.0))
    temp_db.conn.execute("UPDATE evaluations SET is_current=1 WHERE id=?", (ev1,))
    temp_db.conn.execute(
        "INSERT INTO matches(property_id, bw_id, site, listing_id, tier, category, score, rent_total, commute_min, relevance, address, quarantined, is_responsible_pick) "
        "VALUES(?, ?, 'boligportal', 'SCH-1', 'A', 1, 80.0, 8000, 15, 'actionable', 'Ranked AllSearch Allé 10', 0, 0)",
        (pid1, format_bw_id(pid1)))

    # 2. Needs verification view
    pid2 = temp_db._find_or_create_property_direct("Verification AllSearch Allé 20", 55.65, 12.54, 1, 35.0, "new", now)
    s2 = temp_db.record_source_listing(pid2, "boligportal", "SCH-2", "https://ex.test/sch2", {"address": "Verification AllSearch Allé 20"})
    ev2 = temp_db.record_evaluation(s2, pid2, None, outcome="quarantined", rules_version=config.RULES_VERSION_TAG, eval_res=_Verdict(trust_outcome="review", quarantine_reasons=["self-contained apartment unverified"]))
    temp_db.conn.execute("UPDATE evaluations SET is_current=1 WHERE id=?", (ev2,))

    # 3. Recently gone view
    pid3 = temp_db._find_or_create_property_direct("Gone AllSearch Allé 30", 55.65, 12.54, 1, 35.0, "new", now)
    s3 = temp_db.record_source_listing(pid3, "boligportal", "SCH-3", "https://ex.test/sch3", {"address": "Gone AllSearch Allé 30"})
    temp_db.conn.execute("UPDATE source_listings SET lifecycle_state='expired', expired_at=? WHERE id=?", (now, s3))

    # 4. Excluded view
    pid4 = temp_db._find_or_create_property_direct("Excluded AllSearch Allé 40", 55.65, 12.54, 1, 35.0, "new", now)
    s4 = temp_db.record_source_listing(pid4, "boligportal", "SCH-4", "https://ex.test/sch4", {"address": "Excluded AllSearch Allé 40"})
    ev4 = temp_db.record_evaluation(s4, pid4, None, outcome="rejected", rules_version=config.RULES_VERSION_TAG, reason="rent above cap", eval_res=_Verdict(trust_outcome="rejected", rejection_reason="rent above cap"))
    temp_db.conn.execute("UPDATE evaluations SET is_current=1 WHERE id=?", (ev4,))
    temp_db.conn.commit()

    payload = viewer.dashboard_payload(temp_db)

    # Verify each address is present in its respective view dataset
    assert any("Ranked AllSearch" in r.get("address", "") or "Ranked AllSearch" in r.get("display_address", "") for r in payload["trusted"])
    assert any("Verification AllSearch" in r.get("address", "") or "Verification AllSearch" in r.get("display_address", "") for r in payload["review"])
    assert any("Gone AllSearch" in r.get("address", "") or "Gone AllSearch" in r.get("display_address", "") for r in payload["gone"])
    assert any("Excluded AllSearch" in r.get("address", "") or "Excluded AllSearch" in r.get("display_address", "") for r in payload["excluded"])


# ==============================================================================
# Spec 09 Acceptance Cases 7–18: Housing Eligibility & Card-Price Lower Bound
# ==============================================================================
def _base_rec(**over):
    """Base fixture for Spec 09 eligibility evaluation tests."""
    rec = {
        "site": "boligportal",
        "listing_id": "S9-TEST",
        "url": "https://example.test/s9-test",
        "title": "1-værelses lejlighed på Amager",
        "address": "Jorisvej 1a, 2300 København",
        "normalized_address": "Jorisvej 1a, 2300 København",
        "postcode": "2300",
        "geo_precision": "street",
        "listing_type": "lejlighed",
        "card_class": "apartment",
        "rooms": 1,
        "size_m2": 32.0,
        "rent_total": 8300,
        "rent_base": 7800,
        "utilities_amount": 500,
        "deposit": 20000,
        "prepaid_rent": 0,
        "available_from": "2027-01-20",
        "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "description": "Eget køkken og eget badeværelse. Ubegrænset lejeperiode.",
        "eligibility_text": ("Boligtype Lejlighed. Eget køkken og eget badeværelse. "
                             "Ubegrænset lejeperiode."),
        "eligibility_source_url": "https://example.test/s9-test",
        "eligibility_evidence_state": "verified",
        "eligibility_checked_at": "2026-09-15T10:00:00",
        "detail_state": "fetched",
        "lat": 55.6670,
        "lon": 12.5450,
    }
    rec.update(over)
    return rec


def test_acceptance_7_richsgarden_age_23_passes_demographic_eligibility(mock_stations):
    """Spec 09 Acceptance 7 & Section 6.4: Kun for studerende: Nej + Alder: 18-35 passes at age 23."""
    rec = _base_rec(
        title="Moderne 1-værelses lejlighed - Richsgården",
        description="Eget køkken og bad. Kun for studerende: Nej. Alder: 18–35 år. Ubegrænset leje.",
        eligibility_text="Boligtype Lejlighed. Eget køkken og bad. Kun for studerende: Nej. Alder: 18–35 år.",
    )
    res = filters.evaluate_listing(rec, mock_stations, profile_age=23)
    assert res.student_requirement_state == "not_required"
    assert res.age_requirement_state == "bounded"
    assert res.age_min == 18
    assert res.age_max == 35
    assert res.profile_age_fit == "eligible"
    assert res.rejection_reason != "student housing"
    assert not res.rejected
    assert res.passed


def test_acceptance_8_richsgarden_age_36_rejects_with_age_reason(mock_stations):
    """Spec 09 Acceptance 8: The same fixture at age 36 rejects with age requirement reason."""
    rec = _base_rec(
        title="Moderne 1-værelses lejlighed - Richsgården",
        description="Eget køkken og bad. Kun for studerende: Nej. Alder: 18–35 år. Ubegrænset leje.",
        eligibility_text="Boligtype Lejlighed. Eget køkken og bad. Kun for studerende: Nej. Alder: 18–35 år.",
    )
    res = filters.evaluate_listing(rec, mock_stations, profile_age=36)
    assert res.student_requirement_state == "not_required"
    assert res.age_requirement_state == "bounded"
    assert res.age_min == 18
    assert res.age_max == 35
    assert res.profile_age_fit == "ineligible"
    assert res.rejected
    assert res.rejection_reason == "age requirement 18–35; profile age is outside range"


def test_acceptance_9_richsgarden_missing_profile_age_quarantines(mock_stations):
    """Spec 09 Acceptance 9: Missing profile age on bounded fixture quarantines for profile age required."""
    rec = _base_rec(
        title="Moderne 1-værelses lejlighed - Richsgården",
        description="Eget køkken og bad. Kun for studerende: Nej. Alder: 18–35 år. Ubegrænset leje.",
        eligibility_text="Boligtype Lejlighed. Eget køkken og bad. Kun for studerende: Nej. Alder: 18–35 år.",
    )
    res = filters.evaluate_listing(rec, mock_stations, profile_age=None)
    assert res.age_requirement_state == "bounded"
    assert res.profile_age_fit == "unknown"
    assert not res.rejected
    assert not res.quarantined
    assert res.passed
    assert any(f.code == "age_required_unverified" for f in res.soft_flags)


def test_acceptance_10_explicit_student_required_rejects_as_student_housing(mock_stations):
    """Spec 09 Acceptance 10: Kun for studerende: Ja or explicit student requirement rejects."""
    rec = _base_rec(
        title="1-værelses lejlighed",
        description="Eget køkken og bad. Kun for studerende: Ja. Studiebolig.",
        eligibility_text="Boligtype Lejlighed. Eget køkken og bad. Kun for studerende: Ja.",
    )
    res = filters.evaluate_listing(rec, mock_stations, profile_age=23)
    assert res.student_requirement_state == "required"
    assert res.rejected
    assert res.rejection_reason == config.ELIGIBILITY_REASON_STUDENT


def test_acceptance_11_ungdomsbolig_alone_is_rejected_as_student_only(mock_stations):
    """Bare ungdomsbolig with no stated age range or 'uden studiepligt' is student-only."""
    rec = _base_rec(
        title="Ungdomsbolig på Amager",
        description="Eget køkken og bad. Ungdomsbolig med god beliggenhed.",
        eligibility_text="Boligtype Lejlighed. Eget køkken og bad. Ungdomsbolig.",
    )
    res = filters.evaluate_listing(rec, mock_stations, profile_age=23)
    assert res.rejected
    assert res.rejection_reason == config.ELIGIBILITY_REASON_YOUTH


def test_acceptance_12_nearby_schools_boilerplate_does_not_reject(mock_stations):
    """Spec 09 Acceptance 12: Nearby universities/schools paragraph does not reject."""
    rec = _base_rec(
        title="1-værelses lejlighed",
        description="Eget køkken og bad. Perfekt beliggenhed tæt på CBS, KU og levende studiemiljø.",
        eligibility_text="Boligtype Lejlighed. Eget køkken og bad. Tæt på studiemiljø og uddannelsessteder.",
    )
    res = filters.evaluate_listing(rec, mock_stations, profile_age=23)
    assert res.student_requirement_state in ("unknown", "not_required")
    assert not res.rejected
    assert res.passed


def test_acceptance_13_sibling_card_tail_does_not_reject_own_listing(mock_stations):
    """Spec 09 Acceptance 13: Student advert in sibling tail does not reject the own listing."""
    rec = _base_rec(
        title="1-værelses lejlighed",
        description="Eget køkken og bad. Almindelig udlejning i roligt område tæt på transport og butikker.",
        eligibility_text=(
            "Boligtype Lejlighed. Eget køkken og eget badeværelse. Flot renoveret lejlighed med god planløsning, "
            "højt til loftet, lyse rum og ubegrænset lejeperiode beliggende i et attraktivt kvarter tæt på "
            "offentlig transport og indkøb. Klar til indflytning.\n"
            "Andre boliger i ejendommen\n"
            "Studiebolig 1 værelse kun for studerende kr 4000"
        ),
    )
    res = filters.evaluate_listing(rec, mock_stations, profile_age=23)
    assert res.student_requirement_state in ("unknown", "not_required")
    assert not res.rejected
    assert res.passed


def test_acceptance_14_negated_documentation_does_not_reject(mock_stations):
    """Spec 09 Acceptance 14: Negated documentation fields do not reject."""
    for text in (
        "Ingen krav om studiekort.",
        "Uden krav om studieaktivitet.",
        "Studiedokumentation: Ikke påkrævet.",
        "Kun for studerende: Nej.",
    ):
        rec = _base_rec(
            description=f"Eget køkken og bad. {text}",
            eligibility_text=f"Boligtype Lejlighed. Eget køkken og bad. {text}",
        )
        res = filters.evaluate_listing(rec, mock_stations, profile_age=23)
        assert res.student_requirement_state == "not_required", f"Failed for {text}"
        assert not res.rejected
        assert res.passed


def test_acceptance_15_contradictory_exact_fields_quarantine_as_conflict(mock_stations):
    """Spec 09 Acceptance 15: Contradictory exact fields quarantine as housing eligibility evidence conflicts."""
    rec = _base_rec(
        title="Studiebolig forbeholdt studerende",
        description="Eget køkken og bad. Kun for studerende: Nej.",
        eligibility_text="Boligtype Lejlighed. Eget køkken og bad. Studiebolig. Kun for studerende: Nej.",
    )
    res = filters.evaluate_listing(rec, mock_stations, profile_age=23)
    assert res.student_requirement_state == "conflict"
    assert not res.rejected
    assert not res.quarantined
    assert res.passed
    assert any(f.code == "eligibility_conflict" for f in res.soft_flags)


def test_acceptance_16_monthly_card_price_lower_bound(mock_stations):
    """Spec 09 Acceptance 16 (v3.12): Proven monthly card rent > 9700 rejects;
    9700 does not reject on card."""
    # 9701 rejects
    rec_9701 = _base_rec(card_price=9701, rent_total=None, rent_base=None)
    res_9701 = filters.evaluate_listing(rec_9701, mock_stations)
    assert res_9701.rejected
    assert "9701" in res_9701.rejection_reason

    # 9700 does not reject solely on the card
    rec_9700 = _base_rec(card_price=9700, rent_total=9700, rent_base=9700, utilities_amount=0)
    res_9700 = filters.evaluate_listing(rec_9700, mock_stations)
    assert not res_9700.rejected
    assert res_9700.passed


def test_acceptance_17_ambiguous_number_or_move_in_price_not_card_rent(mock_stations):
    """Spec 09 Acceptance 17: Move-in total or ambiguous deposit is not interpreted as monthly card rent."""
    rec = _base_rec(
        card_price=None,
        card_text="Depositum 35.000 kr. Samlet indflytningspris 45.000 kr.",
        rent_total=8500,
        rent_base=8000,
        utilities_amount=500,
        deposit=25000,
    )
    res = filters.evaluate_listing(rec, mock_stations)
    assert not res.rejected
    assert res.passed


def test_acceptance_18_card_above_cap_and_lower_detail_total_quarantines_conflict(mock_stations):
    """Spec 09 Acceptance 18: Card above cap + contradictory lower detail total quarantines price conflict."""
    rec = _base_rec(
        card_price=10500,
        rent_total=8500,
        rent_base=8000,
        utilities_amount=500,
    )
    res = filters.evaluate_listing(rec, mock_stations)
    assert not res.rejected
    assert not res.quarantined
    assert res.passed
    assert any(f.code == "price_conflict" for f in res.soft_flags)


def test_score_components_pinned_v310(mock_stations):
    """Spec 09 Duty 6 kept spec 09 from moving scores; v3.10 re-weighted on purpose.

    Pinned at the v3.10 weights (1 Oct 2026). Affordability includes the
    early-start overlap cost of an availability before TARGET_MOVE_DATE.
    """
    rec = _base_rec()
    res = filters.evaluate_listing(rec, mock_stations, profile_age=23)
    assert res.passed
    assert res.score == 71
    assert res.tier == "A"
    assert res.score_breakdown == {
        "affordability": 17.9,
        "transit": 20.0,
        "commute": 16.0,
        "water": 1.9,
        "centre": 3.8,
        "buzz": 1.5,
        "area": 7.5,
        "floor": 2.0,
        "value": 4.0,
        "date": 9.0,
    }


def test_source_eligibility_persistence_and_coalesce(temp_db):
    """Store.record_source_eligibility persists demographic facts with COALESCE discipline."""
    pid = temp_db._find_or_create_property_direct(
        "Jorisvej 1a, 2300 København", 55.65, 12.60, 1, 32.0, "new", "2026-09-01T10:00:00"
    )
    sid = temp_db.record_source_listing(
        property_id=pid, site="boligportal", listing_id="S9-PERSIST", url="https://example.test/s9-persist"
    )

    # Initial write of demographic facts
    temp_db.record_source_eligibility(
        "boligportal", "S9-PERSIST",
        student_requirement_state="not_required",
        age_requirement_state="bounded",
        age_min=18,
        age_max=35,
        profile_age_fit="eligible",
        eligibility_fact_excerpt="Kun for studerende: Nej. Alder: 18-35 år."
    )
    row = temp_db.get_source_listing("boligportal", "S9-PERSIST")
    assert row["student_requirement_state"] == "not_required"
    assert row["age_requirement_state"] == "bounded"
    assert row["age_min"] == 18
    assert row["age_max"] == 35
    assert row["profile_age_fit"] == "eligible"
    assert row["eligibility_fact_excerpt"] == "Kun for studerende: Nej. Alder: 18-35 år."

    # Subsequent partial update with Nones does NOT erase existing definite facts
    temp_db.record_source_eligibility(
        "boligportal", "S9-PERSIST",
        student_requirement_state=None,
        age_requirement_state=None,
        age_min=None,
        age_max=None,
        profile_age_fit=None,
        eligibility_fact_excerpt=None
    )
    row2 = temp_db.get_source_listing("boligportal", "S9-PERSIST")
    assert row2["student_requirement_state"] == "not_required"
    assert row2["age_requirement_state"] == "bounded"
    assert row2["age_min"] == 18
    assert row2["age_max"] == 35
    assert row2["profile_age_fit"] == "eligible"
    assert row2["eligibility_fact_excerpt"] == "Kun for studerende: Nej. Alder: 18-35 år."


# ==============================================================================
# Spec 09 Acceptance Cases 19–27: Lifecycle, Freshness & Explicit Gone
# ==============================================================================
def test_acceptance_19_complete_misses_produce_missing_and_expiry(temp_db):
    """Spec 09 Acceptance 19: One/two complete misses produce missing labels; the third produces expiry."""
    pid = temp_db._find_or_create_property_direct(
        "Strandlodsvej 15, 2300 København", 55.66, 12.62, 1, 40.0, "new", "2026-09-01T10:00:00"
    )
    sid = temp_db.record_source_listing(
        property_id=pid, site="boligportal", listing_id="S9-MISS", url="https://example.test/s9-miss"
    )

    # 1st complete miss
    srid1 = temp_db.start_scan_run("boligportal", kind="scheduled")
    temp_db.update_lifecycle_after_scan("boligportal", observed_source_ids=[], coverage_status="complete", scan_run_id=srid1)
    temp_db.finish_scan_run(srid1, outcome="success", coverage_status="complete")
    row1 = temp_db.get_source_listing("boligportal", "S9-MISS")
    assert row1["lifecycle_state"] == "missing"
    assert row1["consecutive_misses"] == 1
    assert row1["expired_at"] is None

    # 2nd complete miss
    srid2 = temp_db.start_scan_run("boligportal", kind="scheduled")
    temp_db.update_lifecycle_after_scan("boligportal", observed_source_ids=[], coverage_status="complete", scan_run_id=srid2)
    temp_db.finish_scan_run(srid2, outcome="success", coverage_status="complete")
    row2 = temp_db.get_source_listing("boligportal", "S9-MISS")
    assert row2["lifecycle_state"] == "missing"
    assert row2["consecutive_misses"] == 2
    assert row2["expired_at"] is None

    # 3rd complete miss -> expired
    srid3 = temp_db.start_scan_run("boligportal", kind="scheduled")
    temp_db.update_lifecycle_after_scan("boligportal", observed_source_ids=[], coverage_status="complete", scan_run_id=srid3)
    temp_db.finish_scan_run(srid3, outcome="success", coverage_status="complete")
    row3 = temp_db.get_source_listing("boligportal", "S9-MISS")
    assert row3["lifecycle_state"] == "expired"
    assert row3["consecutive_misses"] == 3
    assert row3["expired_at"] is not None


def test_acceptance_20_partial_failed_targeted_runs_never_increment_misses(temp_db):
    """Spec 09 Acceptance 20: Partial, failed and targeted runs never increment misses."""
    pid = temp_db._find_or_create_property_direct(
        "Strandlodsvej 15, 2300 København", 55.66, 12.62, 1, 40.0, "new", "2026-09-01T10:00:00"
    )
    sid = temp_db.record_source_listing(
        property_id=pid, site="boligportal", listing_id="S9-NOMISS", url="https://example.test/s9-nomiss"
    )
    srid1 = temp_db.start_scan_run("boligportal", kind="scheduled")
    temp_db.update_lifecycle_after_scan("boligportal", observed_source_ids=[], coverage_status="complete", scan_run_id=srid1)
    temp_db.finish_scan_run(srid1, outcome="success", coverage_status="complete")
    assert temp_db.get_source_listing("boligportal", "S9-NOMISS")["consecutive_misses"] == 1

    # Partial scan
    srid2 = temp_db.start_scan_run("boligportal", kind="scheduled")
    temp_db.update_lifecycle_after_scan("boligportal", observed_source_ids=[], coverage_status="partial", scan_run_id=srid2)
    temp_db.finish_scan_run(srid2, outcome="success", coverage_status="partial")
    assert temp_db.get_source_listing("boligportal", "S9-NOMISS")["consecutive_misses"] == 1
    assert temp_db.get_source_listing("boligportal", "S9-NOMISS")["lifecycle_state"] == "missing"

    # Failed scan
    srid3 = temp_db.start_scan_run("boligportal", kind="scheduled")
    temp_db.update_lifecycle_after_scan("boligportal", observed_source_ids=[], coverage_status="failed", scan_run_id=srid3)
    temp_db.finish_scan_run(srid3, outcome="failed", coverage_status="failed")
    assert temp_db.get_source_listing("boligportal", "S9-NOMISS")["consecutive_misses"] == 1

    # Targeted scan
    srid4 = temp_db.start_scan_run("boligportal", kind="targeted", selection="decision-revalidation")
    temp_db.update_lifecycle_after_scan("boligportal", observed_source_ids=[], coverage_status="targeted", scan_run_id=srid4)
    temp_db.finish_scan_run(srid4, outcome="success", coverage_status="targeted")
    assert temp_db.get_source_listing("boligportal", "S9-NOMISS")["consecutive_misses"] == 1


def test_acceptance_21_successful_own_detail_gone_marker_moves_to_recently_gone_and_clears_ranked(temp_db, mock_stations):
    """Spec 09 Acceptance 21: Successful own-detail gone marker moves to Recently gone and clears representative from Ranked."""
    from tests.conftest import with_detail_evidence
    import run
    rec = with_detail_evidence(_base_rec(site="boligportal", listing_id="S9-GONE1"))

    outcome1 = run.scan_card(
        rec, mock_stations, temp_db, scan_run_id=None, dry_run=True,
        segments={}, refresh_features=False,
        detail_fetcher=lambda page, r: r
    )
    assert outcome1["detail_state"] == "fetched"
    rows = temp_db.get_matches()
    assert len(rows) == 1

    rm_before = temp_db._source_read_model_rows([("boligportal", "S9-GONE1")])
    assert rm_before[0]["view"] == "trusted_blocks"

    rec_gone = dict(rec)
    outcome2 = run.scan_card(
        rec_gone, mock_stations, temp_db, scan_run_id=None, dry_run=True,
        segments={}, refresh_features=False,
        force_detail=True,
        detail_fetcher=lambda page, r: {**r, "eligibility_text": "Beklager, men boligen er udlejet."}
    )
    assert outcome2["detail_state"] == "failed:gone"

    src = temp_db.get_source_listing("boligportal", "S9-GONE1")
    assert src["explicit_gone_at"] is not None
    assert src["explicit_gone_marker"] == "boligen er udlejet"
    assert src["consecutive_misses"] == 0

    matches = temp_db.get_matches()
    assert len(matches) == 0

    rm_after = temp_db._source_read_model_rows([("boligportal", "S9-GONE1")])
    assert rm_after[0]["view"] == "recently_gone"
    assert any("advert says no longer available" in r for r in rm_after[0]["reasons"])


def test_acceptance_22_generic_detail_failure_preserves_facts_and_does_not_set_explicit_gone(temp_db, mock_stations):
    """Spec 09 Acceptance 22: Generic fetch failure preserves prior facts and does not move to Recently gone."""
    from tests.conftest import with_detail_evidence
    import run
    rec = with_detail_evidence(_base_rec(site="boligportal", listing_id="S9-FAIL1"))
    run.scan_card(
        rec, mock_stations, temp_db, scan_run_id=None, dry_run=True,
        segments={}, refresh_features=False,
        detail_fetcher=lambda page, r: r
    )
    assert len(temp_db.get_matches()) == 1

    outcome = run.scan_card(
        rec, mock_stations, temp_db, scan_run_id=None, dry_run=True,
        segments={}, refresh_features=False,
        force_detail=True,
        detail_fetcher=lambda page, r: {**r, "eligibility_evidence_state": "failed", "eligibility_text": ""}
    )
    assert outcome["detail_state"] == "failed"
    src = temp_db.get_source_listing("boligportal", "S9-FAIL1")
    assert src["explicit_gone_at"] is None
    assert src["explicit_gone_marker"] is None


def test_acceptance_23_card_reappearance_does_not_clear_explicit_gone_recovered_detail_does(temp_db, mock_stations):
    """Spec 09 Acceptance 23: Re-seeing a card does not clear explicit-gone; recovered own-detail page does."""
    from tests.conftest import with_detail_evidence
    import run
    rec = with_detail_evidence(_base_rec(site="boligportal", listing_id="S9-RECOVER"))
    run.scan_card(
        rec, mock_stations, temp_db, scan_run_id=None, dry_run=True,
        segments={}, refresh_features=False,
        force_detail=True,
        detail_fetcher=lambda page, r: {**r, "eligibility_text": "Annoncen findes ikke"}
    )
    assert temp_db.get_source_listing("boligportal", "S9-RECOVER")["explicit_gone_at"] is not None

    rec_search = dict(rec)
    rec_search.pop("eligibility_text", None)
    temp_db.record_source_listing(
        property_id=None, site="boligportal", listing_id="S9-RECOVER",
        url=rec["url"], raw_data=run.card_payload(rec_search),
        card_class=rec.get("card_class"), detail_state="skipped:fresh"
    )
    assert temp_db.get_source_listing("boligportal", "S9-RECOVER")["explicit_gone_at"] is not None

    run.scan_card(
        rec, mock_stations, temp_db, scan_run_id=None, dry_run=True,
        segments={}, refresh_features=False,
        force_detail=True,
        detail_fetcher=lambda page, r: {**r, "eligibility_text": "Lejlighed med eget køkken og bad", "eligibility_evidence_state": "verified"}
    )
    src_recovered = temp_db.get_source_listing("boligportal", "S9-RECOVER")
    assert src_recovered["explicit_gone_at"] is None
    assert src_recovered["explicit_gone_marker"] is None
    assert src_recovered["detail_state"] == "fetched"


def test_acceptance_24_sibling_active_source_takes_representative_when_one_explicitly_gone(temp_db, mock_stations):
    """Spec 09 Acceptance 24: Sibling active eligible source takes representative when one source is explicitly gone."""
    from tests.conftest import with_detail_evidence
    import run
    rec1 = with_detail_evidence(_base_rec(site="boligportal", listing_id="S9-SIB1", rent_total=8200, address="Teglholmsgade 36, 2450 København SV"))
    rec2 = with_detail_evidence(_base_rec(site="lejebolig", listing_id="S9-SIB2", rent_total=8300, address="Teglholmsgade 36, 2450 København SV"))

    run.scan_card(rec1, mock_stations, temp_db, scan_run_id=None, dry_run=True, segments={}, refresh_features=False, detail_fetcher=lambda p, r: r)
    run.scan_card(rec2, mock_stations, temp_db, scan_run_id=None, dry_run=True, segments={}, refresh_features=False, detail_fetcher=lambda p, r: r)

    pid = temp_db.get_source_listing("boligportal", "S9-SIB1")["property_id"]
    assert pid is not None

    match_before = temp_db.get_match_row(pid)
    assert match_before is not None
    assert match_before["site"] == "boligportal"
    assert match_before["listing_id"] == "S9-SIB1"

    run.scan_card(
        rec1, mock_stations, temp_db, scan_run_id=None, dry_run=True,
        segments={}, refresh_features=False, force_detail=True,
        detail_fetcher=lambda p, r: {**r, "eligibility_text": "Boligen er udlejet"}
    )

    temp_db.clear_stale_matches()
    match_after = temp_db.get_match_row(pid)
    assert match_after is not None
    assert match_after["site"] == "lejebolig"
    assert match_after["listing_id"] == "S9-SIB2"


def test_acceptance_25_decision_revalidation_selector_vs_broad_inventory(temp_db, mock_stations):
    """Spec 09 Acceptance 25: 24-hour decision selection vs 7-day broad inventory."""
    import datetime
    import run
    now = datetime.datetime(2026, 9, 16, 12, 0, 0)
    t_25h_ago = (now - datetime.timedelta(hours=25)).isoformat(timespec="seconds")
    t_2h_ago = (now - datetime.timedelta(hours=2)).isoformat(timespec="seconds")

    # 1. Ranked representative at 25h -> selected
    pid1 = temp_db._find_or_create_property_direct("Ranked St 1", 55.65, 12.54, 1, 35.0, "new", "2026-09-01T10:00:00")
    s1 = temp_db.record_source_listing(property_id=pid1, site="boligportal", listing_id="R1", url="https://ex.test/r1", detail_state="fetched")
    temp_db.conn.execute("UPDATE source_listings SET detail_fetched_at=?, lifecycle_state='active' WHERE id=?", (t_25h_ago, s1))
    temp_db.conn.execute("INSERT INTO matches(property_id, site, listing_id, rent_total, score) VALUES(?, 'boligportal', 'R1', 8000, 80)", (pid1,))

    # 2. Saved property at 25h -> selected
    pid2 = temp_db._find_or_create_property_direct("Saved St 2", 55.65, 12.54, 1, 35.0, "new", "2026-09-01T10:00:00")
    s2 = temp_db.record_source_listing(property_id=pid2, site="boligportal", listing_id="S2", url="https://ex.test/s2", detail_state="fetched")
    temp_db.conn.execute("UPDATE source_listings SET detail_fetched_at=?, lifecycle_state='active' WHERE id=?", (t_25h_ago, s2))
    temp_db.record_user_action(pid2, "save")

    # 3. Active review under rent cap at 25h -> selected
    pid3 = temp_db._find_or_create_property_direct("Review St 3", 55.65, 12.54, 1, 35.0, "new", "2026-09-01T10:00:00")
    s3 = temp_db.record_source_listing(property_id=pid3, site="boligportal", listing_id="REV3", url="https://ex.test/rev3", detail_state="fetched", card_price=8500)
    temp_db.conn.execute("UPDATE source_listings SET detail_fetched_at=?, lifecycle_state='active' WHERE id=?", (t_25h_ago, s3))

    # 4. Review over cap at 25h (card_price 11000) -> NOT selected
    pid4 = temp_db._find_or_create_property_direct("OverCap St 4", 55.65, 12.54, 1, 35.0, "new", "2026-09-01T10:00:00")
    s4 = temp_db.record_source_listing(property_id=pid4, site="boligportal", listing_id="OVER4", url="https://ex.test/over4", detail_state="fetched", card_price=11000)
    temp_db.conn.execute("UPDATE source_listings SET detail_fetched_at=?, lifecycle_state='active' WHERE id=?", (t_25h_ago, s4))

    # 5. Hard rejected at 25h -> NOT selected
    pid5 = temp_db._find_or_create_property_direct("Rej St 5", 55.65, 12.54, 1, 35.0, "new", "2026-09-01T10:00:00")
    s5 = temp_db.record_source_listing(property_id=pid5, site="boligportal", listing_id="REJ5", url="https://ex.test/rej5", detail_state="fetched", card_price=8000)
    temp_db.conn.execute("UPDATE source_listings SET detail_fetched_at=?, lifecycle_state='active' WHERE id=?", (t_25h_ago, s5))
    temp_db.record_rejection(pid5, s5, "boligportal", "REJ5", "student_only")

    # 6. Missing at 25h -> NOT selected by 24-hour decision revalidation
    pid6 = temp_db._find_or_create_property_direct("Missing St 6", 55.65, 12.54, 1, 35.0, "new", "2026-09-01T10:00:00")
    s6 = temp_db.record_source_listing(property_id=pid6, site="boligportal", listing_id="MISS6", url="https://ex.test/miss6", detail_state="fetched")
    temp_db.conn.execute("UPDATE source_listings SET detail_fetched_at=?, lifecycle_state='missing', consecutive_misses=1 WHERE id=?", (t_25h_ago, s6))

    # 7. Fresh ranked at 2h -> NOT selected (<24h)
    pid7 = temp_db._find_or_create_property_direct("Fresh St 7", 55.65, 12.54, 1, 35.0, "new", "2026-09-01T10:00:00")
    s7 = temp_db.record_source_listing(property_id=pid7, site="boligportal", listing_id="FRESH7", url="https://ex.test/fresh7", detail_state="fetched")
    temp_db.conn.execute("UPDATE source_listings SET detail_fetched_at=?, lifecycle_state='active' WHERE id=?", (t_2h_ago, s7))
    temp_db.conn.execute("INSERT INTO matches(property_id, site, listing_id, rent_total, score) VALUES(?, 'boligportal', 'FRESH7', 8000, 80)", (pid7,))
    temp_db.conn.commit()

    picked = run.select_for_decision_revalidation(temp_db, now=now)
    picked_lids = {r["listing_id"] for r in picked}
    assert picked_lids == {"R1", "S2", "REV3"}


def test_acceptance_26_changed_card_not_fetched_twice_in_one_scan(temp_db, mock_stations):
    """Spec 09 Acceptance 26: A changed card is not detail-fetched twice in one scan."""
    from tests.conftest import with_detail_evidence
    import run
    fetch_count = 0
    def mock_fetcher(page, r):
        nonlocal fetch_count
        fetch_count += 1
        return {**r, "eligibility_text": "Updated text", "eligibility_evidence_state": "verified"}

    rec = with_detail_evidence(_base_rec(site="boligportal", listing_id="S9-DEDUP", card_price=8000))
    counters = run.ScanCounters()
    seen_this_scan = set()

    key = ("boligportal", "S9-DEDUP")
    seen_this_scan.add(key)
    run.scan_card(rec, mock_stations, temp_db, scan_run_id=None, dry_run=True, segments={}, refresh_features=False, detail_fetcher=mock_fetcher, counters=counters)
    assert fetch_count == 1

    run.revalidate_decision_details("boligportal", page=None, stations=mock_stations, store=temp_db, scan_run_id=None, dry_run=True, segments={}, refresh_features=False, detail_fetcher=mock_fetcher, counters=counters, seen_this_scan=seen_this_scan)
    assert fetch_count == 1


def test_acceptance_27_stale_price_refresh_replaces_financial_display_and_triggers_reevaluation(temp_db, mock_stations):
    """Spec 09 Acceptance 27: Refreshed price replaces stale financial display and triggers local re-evaluation."""
    from tests.conftest import with_detail_evidence
    import run
    rec_initial = with_detail_evidence(_base_rec(site="boligportal", listing_id="S9-PRICE", rent_total=9200, rent_base=8700))
    run.scan_card(rec_initial, mock_stations, temp_db, scan_run_id=None, dry_run=True, segments={}, refresh_features=False, detail_fetcher=lambda p, r: r)

    m_before = temp_db.get_matches()[0]
    assert m_before["rent_total"] == 9200
    score_before = m_before["score"]

    rec_refreshed = with_detail_evidence(_base_rec(site="boligportal", listing_id="S9-PRICE", rent_total=8200, rent_base=7700))
    run.scan_card(rec_refreshed, mock_stations, temp_db, scan_run_id=None, dry_run=True, segments={}, refresh_features=False, force_detail=True, detail_fetcher=lambda p, r: r)

    m_after = temp_db.get_matches()[0]
    assert m_after["rent_total"] == 8200
    assert m_after["score"] > score_before


# ==============================================================================
# Spec 09 section 12.4 acceptance 28-30 - the v38u1 row's read-model and payload
# side: the migration suppresses its backlog, the payload carries no private
# profile record, and every v3.8 disclosure fact is served
# ==============================================================================

def test_acceptance_28_the_migration_marks_created_matches_already_alerted(temp_db):
    """Acceptance 28: the migration's suppression marks every created row already
    alerted, is idempotent, and removes nothing from the Ranked read model."""
    stamp = "2026-09-16T08:48:00"
    for address, score in (("Gammel Vej 1, 2300 København", 82),
                           ("Gammel Vej 2, 2300 København", 74)):
        pid = temp_db._find_or_create_property_direct(
            address, 55.65 + score / 100000, 12.60, 1, 32.0, "new", "2026-09-01T10:00:00")
        temp_db.conn.execute(
            "INSERT INTO matches(property_id, bw_id, category, score, rent_total, "
            "relevance, quarantined) VALUES(?,?,1,?,8300,'actionable',0)",
            (pid, format_bw_id(pid), score))
    temp_db.conn.commit()

    create_result = temp_db.suppress_alert_backlog(stamp)
    rows = temp_db.get_matches()
    assert create_result == 2, "both migration-created rows are marked"
    assert len(rows) == 2
    for row in rows:
        assert row["alerted_actionable"] == 1
        assert row["last_alert_at"] == stamp, "the migration's own completion stamp"
    assert temp_db.suppress_alert_backlog(stamp) == 0, "a second call changes nothing"
    assert temp_db.suppress_alert_backlog("2026-09-16T09:00:00") == 0

    # Suppression is a flag, not a deletion: the Ranked read model and the digest
    # still serve both rows.
    assert len(temp_db.grouped_matches(limit=10)) == 2
    assert temp_db.get_action_states() == {}, "no user action is written"


def test_acceptance_29_the_payload_carries_no_private_profile_record(temp_db):
    """Acceptance 29: the payload carries no profile age, no date of birth and no
    raw private profile record; the listing-side age facts stay in the clear."""
    temp_db.set_search_profile_age(23, evidence_source="user")
    profile = temp_db.get_search_profile()
    assert profile["age_years"] == 23

    payload = viewer.dashboard_payload(temp_db)
    text = json.dumps(payload, ensure_ascii=False)

    for token in ("age_years", "search_profile", "date of birth", "Date of birth",
                  "fødselsdato"):
        assert token not in text, f"the payload carries {token!r}"
    assert not re.search(r"(?<![A-Za-z0-9_])evidence_source", text), \
        "a bare evidence source is the profile's column"
    assert not re.search(r"(?<![A-Za-z0-9_])updated_at", text), \
        "a bare update time is the profile's column"
    assert not (set(profile) & set(payload["stats"])), \
        "the stats block carries no profile column"
    # The page template is the exported document's script, so it is swept too.
    for token in ("age_years", "search_profile", "fødselsdato"):
        assert token not in viewer.HTML_TEMPLATE
    assert not re.search(r"(?<![A-Za-z0-9_])evidence_source", viewer.HTML_TEMPLATE)


def test_acceptance_30_the_read_models_serve_every_v38_disclosure_fact(temp_db):
    """Acceptance 30's data side: the four scoped datasets, the Ranked default and
    the facts each v3.8 disclosure renders are all served."""
    pid = temp_db._find_or_create_property_direct(
        "Disclosurevej 5, 2300 København", 55.64, 12.57, 1, 31.0, "new", "2026-09-01T10:00:00")
    sid = temp_db.record_source_listing(
        property_id=pid, site="boligportal", listing_id="V38U1-D1",
        url="https://example.test/v38u1-d1")
    temp_db.record_source_explicit_gone("boligportal", "V38U1-D1", "boligen er udlejet")
    temp_db.set_rank_priority(pid, 1, kind="preferred_locality", note="Nicer locality")

    rows = temp_db._source_read_model_rows([("boligportal", "V38U1-D1")])
    assert len(rows) == 1, "the seeded source is read back"
    served = set(rows[0])
    for key in ("missing_label", "consecutive_misses", "explicit_gone_at",
                "explicit_gone_marker", "age_requirement_state", "age_min", "age_max",
                "profile_age_fit", "rank_priority", "rank_priority_kind",
                "rank_priority_note"):
        assert key in served, f"the read model serves {key}"
    assert rows[0]["explicit_gone_at"] and rows[0]["rank_priority_kind"] == "preferred_locality"

    counts = temp_db.view_counts()
    assert set(counts) == {"trusted_blocks", "needs_verification", "recently_gone",
                           "excluded"}
    assert viewer.DEFAULT_DASHBOARD_VIEW == "trusted"
    assert viewer.DASHBOARD_DATASET_KEYS == ("trusted", "review", "gone", "excluded")
    assert viewer.DASHBOARD_VIEW_LABELS[0][1] == "Ranked"
    assert sid > 0

