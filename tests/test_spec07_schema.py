"""Tests for spec 07 section 5 - the trust evidence schema and its storage API.

Row v36s1. This row builds schema and storage only: the classification rules
(tenancy, building form, the final trust decision) are later rows and no
classifier is exercised here. Everything runs on `temp_db` or on a throwaway
database in `tmp_path`; production `boligwatch.db` is never opened.
"""
import sqlite3
import types

import pytest

from store import Store, EVIDENCE_EXCERPT_MAX

# Spec 07 section 5.1: the nine latest-parsed trust facts on source_listings.
SOURCE_TRUST_COLUMNS = [
    "unit_type_state",
    "unit_type_evidence_source",
    "unit_type_evidence_excerpt",
    "tenancy_channel_state",
    "landlord_name",
    "landlord_evidence_source",
    "landlord_evidence_excerpt",
    "building_form_state",
    "building_profile_id",
]

# Spec 07 section 5.3: the three state fields plus the trust verdict.
EVALUATION_TRUST_COLUMNS = [
    "unit_type_state",
    "tenancy_channel_state",
    "building_form_state",
    "trust_outcome",
    "trust_reason",
]

PROFILE_COLUMNS = [
    "id", "profile_key", "display_name", "normalized_address", "complex_key",
    "building_form_state", "management_state", "operator_name", "evidence_url",
    "evidence_excerpt", "evidence_kind", "verified_at", "state",
    "superseded_at", "superseded_reason",
]

PROFILE_KEYS = (
    "profile_key", "building_form_state", "management_state", "evidence_kind",
    "display_name", "normalized_address", "complex_key", "operator_name",
    "evidence_url", "evidence_excerpt", "verified_at",
)


def _columns(store, table):
    return {r[1] for r in store.conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _tables(store):
    return {r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _counting_profile(store, key="block-1", address="Jorisvej 1a, 2300 København",
                      excerpt=None):
    """One reviewed managed-block profile, the way a later row would write it."""
    return store.upsert_building_profile(
        profile_key=key, building_form_state="managed_block",
        management_state="professional", evidence_kind="operator_page",
        display_name="Jorisvej 1a", normalized_address=address,
        operator_name="Ejendomsselskabet Test A/S",
        evidence_url="https://operator.test/jorisvej-1a",
        evidence_excerpt=excerpt or "Jorisvej 1a administreres af Test A/S",
        verified_at="2026-09-10T09:00:00")


def _legacy_rows(store):
    """A linked source listing and one evaluation, as pre-v3.6 code left them."""
    store.conn.execute(
        "INSERT INTO properties(normalized_address, lat, lon, bw_id) "
        "VALUES('Jorisvej 1a', 55.65, 12.60, 'BW900')")
    pid = store.conn.execute("SELECT id FROM properties WHERE bw_id='BW900'").fetchone()["id"]
    store.conn.execute(
        "INSERT INTO source_listings(property_id, site, listing_id, url, "
        "lifecycle_state, first_seen_at, last_seen_at, raw_data) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (pid, "lejebolig", "L900", "https://example.test/L900", "active",
         "2026-09-01T10:00:00", "2026-09-01T10:00:00", '{"rent_total": 8300}'))
    sid = store.conn.execute(
        "SELECT id FROM source_listings WHERE listing_id='L900'").fetchone()["id"]
    store.conn.execute(
        "INSERT INTO evaluations(source_listing_id, property_id, evaluated_at, "
        "rules_version, outcome, reason, score, is_current) "
        "VALUES(?,?,?,?,?,?,?,1)",
        (sid, pid, "2026-09-01T10:05:00", "v3.5-1", "rejected", "rent over cap", 0.0))
    store.conn.commit()
    return pid, sid


# --------------------------------------------------------------------------- #
# Fresh database creation                                                      #
# --------------------------------------------------------------------------- #
def test_fresh_db_carries_the_exact_section_5_schema(temp_db):
    """A newly created database has every section 5 column, table and index."""
    src = _columns(temp_db, "source_listings")
    for col in SOURCE_TRUST_COLUMNS:
        assert col in src, f"source_listings is missing {col}"

    evals = _columns(temp_db, "evaluations")
    for col in EVALUATION_TRUST_COLUMNS:
        assert col in evals, f"evaluations is missing {col}"

    assert "building_profiles" in _tables(temp_db)
    assert set(PROFILE_COLUMNS) == _columns(temp_db, "building_profiles")

    indexes = {r[0] for r in temp_db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "idx_building_profiles_address" in indexes
    assert "idx_building_profiles_complex" in indexes

    # Section 5.2's database-level scope check is in the table definition.
    sql = temp_db.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='building_profiles'").fetchone()[0]
    assert "CHECK (normalized_address IS NOT NULL OR complex_key IS NOT NULL)" in sql
    for not_null in ("profile_key TEXT UNIQUE NOT NULL",
                     "building_form_state TEXT NOT NULL",
                     "management_state TEXT NOT NULL",
                     "evidence_kind TEXT NOT NULL",
                     "state TEXT NOT NULL DEFAULT 'active'"):
        assert not_null in sql, f"building_profiles is missing {not_null!r}"

    # Section 5.4's identity guard is still installed (Phase 1).
    triggers = {r[0] for r in temp_db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}
    assert "source_listings_no_clear_link" in triggers


def test_existing_tables_keep_their_columns(temp_db):
    """The v3.6 additions are additive: nothing an older reader uses is gone."""
    src = _columns(temp_db, "source_listings")
    for col in ("property_id", "site", "listing_id", "raw_data", "card_class",
                "eligibility_text"):
        assert col in src
    evals = _columns(temp_db, "evaluations")
    for col in ("outcome", "reason", "quarantine_reasons", "score", "kitchen_state",
                "is_current"):
        assert col in evals


# --------------------------------------------------------------------------- #
# Migration of a legacy database, and idempotence                              #
# --------------------------------------------------------------------------- #
def _strip_v36_schema(store):
    """Turn a current database back into a pre-v3.6 one."""
    for col in SOURCE_TRUST_COLUMNS:
        store.conn.execute(f"ALTER TABLE source_listings DROP COLUMN {col}")
    for col in EVALUATION_TRUST_COLUMNS:
        store.conn.execute(f"ALTER TABLE evaluations DROP COLUMN {col}")
    store.conn.execute("DROP TABLE building_profiles")
    store.conn.commit()


def test_legacy_db_migrates_additively_and_losslessly(tmp_path):
    """Opening a pre-v3.6 database adds the schema and keeps every row."""
    path = str(tmp_path / "legacy.db")
    legacy = Store(db_path=path)
    pid, sid = _legacy_rows(legacy)
    _strip_v36_schema(legacy)
    assert "building_profiles" not in _tables(legacy)
    assert not set(SOURCE_TRUST_COLUMNS) & _columns(legacy, "source_listings")
    legacy.conn.close()

    migrated = Store(db_path=path)
    for col in SOURCE_TRUST_COLUMNS:
        assert col in _columns(migrated, "source_listings")
    for col in EVALUATION_TRUST_COLUMNS:
        assert col in _columns(migrated, "evaluations")
    assert "building_profiles" in _tables(migrated)

    row = migrated.get_source_listing("lejebolig", "L900")
    assert row["property_id"] == pid
    assert row["url"] == "https://example.test/L900"
    assert row["lifecycle_state"] == "active"
    assert row["raw_data"] == '{"rent_total": 8300}'
    assert row["unit_type_state"] is None

    evaluation = migrated.current_evaluation(sid)
    assert evaluation["outcome"] == "rejected"
    assert evaluation["reason"] == "rent over cap"
    assert evaluation["trust_outcome"] is None
    assert migrated.conn.execute("SELECT COUNT(*) FROM building_profiles").fetchone()[0] == 0
    migrated.conn.close()


def test_repeated_migration_changes_nothing(tmp_path):
    """Migrating an already migrated database is a no-op, twice over."""
    path = str(tmp_path / "repeat.db")
    first = Store(db_path=path)
    _legacy_rows(first)
    profile_id = _counting_profile(first)
    first.record_source_trust("lejebolig", "L900", unit_type_state="self_contained_apartment")
    first.conn.close()

    second = Store(db_path=path)
    before = (sorted(_columns(second, "source_listings")),
              sorted(_columns(second, "evaluations")),
              _columns(second, "building_profiles"))
    counts_before = {
        table: second.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("source_listings", "evaluations", "building_profiles", "properties")}

    second._migrate_schema()
    second._migrate_schema()
    second.conn.close()

    third = Store(db_path=path)
    after = (sorted(_columns(third, "source_listings")),
             sorted(_columns(third, "evaluations")),
             _columns(third, "building_profiles"))
    assert before == after
    counts_after = {
        table: third.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("source_listings", "evaluations", "building_profiles", "properties")}
    assert counts_before == counts_after
    # The stored profile and its facts are untouched by the repeated migrations.
    profile = third.get_building_profile("block-1")
    assert profile["id"] == profile_id
    assert profile["state"] == "active"
    assert third.get_source_listing("lejebolig", "L900")["unit_type_state"] \
        == "self_contained_apartment"
    third.conn.close()


# --------------------------------------------------------------------------- #
# Profile constraints                                                          #
# --------------------------------------------------------------------------- #
def test_profile_key_is_unique_and_upsert_is_stable(temp_db):
    first_id = _counting_profile(temp_db)
    second_id = _counting_profile(temp_db, excerpt="refreshed excerpt")
    assert first_id == second_id
    assert temp_db.conn.execute(
        "SELECT COUNT(*) FROM building_profiles").fetchone()[0] == 1
    assert temp_db.get_building_profile("block-1")["evidence_excerpt"] == "refreshed excerpt"

    with pytest.raises(sqlite3.IntegrityError):
        temp_db.conn.execute(
            "INSERT INTO building_profiles(profile_key, building_form_state, "
            "management_state, evidence_kind, normalized_address) "
            "VALUES('block-1','managed_block','professional','manual','Jorisvej 1a')")
    temp_db.conn.rollback()


def test_profile_needs_an_address_or_a_complex_key(temp_db):
    """An exact scope is required, and the database is what enforces it."""
    with pytest.raises(sqlite3.IntegrityError):
        temp_db.upsert_building_profile(
            profile_key="scopeless", building_form_state="managed_block",
            management_state="professional", evidence_kind="operator_page")
    temp_db.conn.rollback()
    assert temp_db.conn.execute(
        "SELECT COUNT(*) FROM building_profiles").fetchone()[0] == 0

    complex_id = temp_db.upsert_building_profile(
        profile_key="complex-1", building_form_state="managed_block",
        management_state="professional", evidence_kind="operator_page",
        complex_key="viola-noerloevs-gade")
    assert complex_id
    assert temp_db.active_building_profile_for_complex("viola-noerloevs-gade")["id"] == complex_id


def test_profile_required_fields_are_not_null(temp_db):
    for missing in ("building_form_state", "management_state", "evidence_kind"):
        values = {"profile_key": f"missing-{missing}",
                  "building_form_state": "managed_block",
                  "management_state": "professional",
                  "evidence_kind": "manual",
                  "normalized_address": "Jorisvej 1a"}
        values[missing] = None
        with pytest.raises(sqlite3.IntegrityError):
            temp_db.conn.execute(
                "INSERT INTO building_profiles(profile_key, building_form_state, "
                "management_state, evidence_kind, normalized_address) VALUES(?,?,?,?,?)",
                (values["profile_key"], values["building_form_state"],
                 values["management_state"], values["evidence_kind"],
                 values["normalized_address"]))
        temp_db.conn.rollback()


# --------------------------------------------------------------------------- #
# Manual preservation and supersession                                         #
# --------------------------------------------------------------------------- #
def test_a_parser_run_cannot_overwrite_a_manual_profile(temp_db):
    """Spec 07 7.2: manual evidence survives a parser write, unchanged."""
    manual_id = temp_db.upsert_building_profile(
        profile_key="manual-1", building_form_state="managed_block",
        management_state="professional", evidence_kind="manual",
        normalized_address="Jorisvej 1a, 2300 København",
        operator_name="Beboerforeningen", evidence_url="https://review.test/1",
        evidence_excerpt="reviewer saw the operator letter",
        verified_at="2026-09-08T08:00:00")
    temp_db.upsert_building_profile(
        profile_key="manual-1", building_form_state="small_or_private",
        management_state="private", evidence_kind="source_detail",
        normalized_address="Jorisvej 1a, 2300 København",
        operator_name="parser guess", evidence_excerpt="card text")
    stored = temp_db.get_building_profile("manual-1")
    assert stored["id"] == manual_id
    assert stored["evidence_kind"] == "manual"
    assert stored["management_state"] == "professional"
    assert stored["operator_name"] == "Beboerforeningen"
    assert stored["verified_at"] == "2026-09-08T08:00:00"
    assert stored["state"] == "active"
    # A later manual write is still the reviewer's own decision: it wins.
    temp_db.upsert_building_profile(
        profile_key="manual-1", building_form_state="managed_block",
        management_state="unknown", evidence_kind="manual",
        normalized_address="Jorisvej 1a, 2300 København",
        operator_name="Beboerforeningen", evidence_excerpt="re-reviewed")
    assert temp_db.get_building_profile("manual-1")["management_state"] == "unknown"


def test_explicit_supersession_retires_a_profile(temp_db):
    """A superseded profile stops being usable, and the reason is recorded."""
    _counting_profile(temp_db)
    assert temp_db.active_building_profile_for_address("Jorisvej 1a, 2300 København")
    assert temp_db.supersede_building_profile("block-1", "operator changed 2026-09-15") == 1
    profile = temp_db.get_building_profile("block-1")
    assert profile["state"] == "superseded"
    assert profile["superseded_at"]
    assert profile["superseded_reason"] == "operator changed 2026-09-15"
    # The row is kept, but no active lookup finds it.
    assert temp_db.active_building_profile_for_address("Jorisvej 1a, 2300 København") is None

    # Superseding twice is a no-op, not a second timestamp.
    stamp = profile["superseded_at"]
    assert temp_db.supersede_building_profile("block-1", "again") == 0
    assert temp_db.get_building_profile("block-1")["superseded_at"] == stamp
    assert temp_db.get_building_profile("block-1")["superseded_reason"] \
        == "operator changed 2026-09-15"
    # An unknown key changes nothing.
    assert temp_db.supersede_building_profile("no-such-key", "n/a") == 0


def test_a_retired_manual_profile_may_be_replaced_after_supersession(temp_db):
    """The parser write is only unlocked by the explicit supersede."""
    temp_db.upsert_building_profile(
        profile_key="manual-2", building_form_state="managed_block",
        management_state="professional", evidence_kind="manual",
        normalized_address="Jorisvej 1a, 2300 København",
        operator_name="Reviewer", evidence_excerpt="manual decision")
    temp_db.upsert_building_profile(
        profile_key="manual-2", building_form_state="small_or_private",
        management_state="private", evidence_kind="source_detail",
        normalized_address="Jorisvej 1a, 2300 København")
    assert temp_db.get_building_profile("manual-2")["evidence_kind"] == "manual"

    temp_db.supersede_building_profile("manual-2", "reviewer retired the manual decision")
    profile_id = temp_db.upsert_building_profile(
        profile_key="manual-2", building_form_state="small_or_private",
        management_state="private", evidence_kind="source_detail",
        normalized_address="Jorisvej 1a, 2300 København",
        evidence_excerpt="the card says privat udlejning")
    stored = temp_db.get_building_profile("manual-2")
    assert stored["id"] == profile_id
    assert stored["evidence_kind"] == "source_detail"
    assert stored["management_state"] == "private"
    assert stored["state"] == "active"
    assert temp_db.active_building_profile_for_address("Jorisvej 1a, 2300 København")


# --------------------------------------------------------------------------- #
# Exact-address and complex lookup                                             #
# --------------------------------------------------------------------------- #
def test_profile_lookup_is_exact_scope_only(temp_db):
    """One house number, one profile: no neighbouring address, no street."""
    _counting_profile(temp_db)
    # The same building written with a floor/door suffix and other casing.
    found = temp_db.active_building_profile_for_address(
        "jorisvej 1a, 2. th, 2300 københavn")
    assert found and found["profile_key"] == "block-1"
    for miss in ("Jorisvej 1b, 2300 København", "Jorisvej 2, 2300 København",
                 "Jorisvej, 2300 København", "Jorisvej 1a, 2300 Aalborg"):
        assert temp_db.active_building_profile_for_address(miss) is None
    assert temp_db.active_building_profile_for_complex("no-such-complex") is None
    assert temp_db.get_building_profile("no-such-key") is None


def test_profile_address_is_stored_in_the_folded_exact_form(temp_db):
    _counting_profile(temp_db)
    stored = temp_db.get_building_profile("block-1")
    assert stored["normalized_address"] == Store.normalize_exact_address(
        "Jorisvej 1a, 2300 København")
    assert stored["evidence_kind"] == "operator_page"
    assert stored["state"] == "active"


def test_profile_evidence_excerpt_is_bounded(temp_db):
    _counting_profile(temp_db, excerpt="x" * 500)
    stored = temp_db.get_building_profile("block-1")
    assert len(stored["evidence_excerpt"]) == EVIDENCE_EXCERPT_MAX == 200
    assert stored["evidence_excerpt"] == "x" * 200


# --------------------------------------------------------------------------- #
# Source trust facts and evaluation trust facts                                #
# --------------------------------------------------------------------------- #
def test_source_trust_facts_round_trip_with_bounded_excerpts(temp_db):
    sid = temp_db.record_source_listing(
        property_id=None, site="lejebolig", listing_id="TRUST1",
        url="https://example.test/TRUST1", raw_data={"card_price": 8300})
    returned = temp_db.record_source_trust(
        "lejebolig", "TRUST1",
        unit_type_state="self_contained_apartment",
        unit_type_evidence_source="detail",
        unit_type_evidence_excerpt="Boligtype " + "v" * 400,
        tenancy_channel_state="professional_operator",
        landlord_name="Ejendomsselskabet Test A/S",
        landlord_evidence_source="detail",
        landlord_evidence_excerpt="y" * 500,
        building_form_state="managed_block",
        building_profile_id=42)
    assert returned == sid

    row = temp_db.get_source_listing("lejebolig", "TRUST1")
    assert row["unit_type_state"] == "self_contained_apartment"
    assert row["unit_type_evidence_source"] == "detail"
    assert len(row["unit_type_evidence_excerpt"]) == 200
    assert row["unit_type_evidence_excerpt"].startswith("Boligtype ")
    assert row["tenancy_channel_state"] == "professional_operator"
    assert row["landlord_name"] == "Ejendomsselskabet Test A/S"
    assert row["landlord_evidence_source"] == "detail"
    assert row["landlord_evidence_excerpt"] == "y" * 200
    assert row["building_form_state"] == "managed_block"
    assert row["building_profile_id"] == 42


def test_a_later_pass_does_not_blank_a_sibling_fact(temp_db):
    """Each classifier writes its own group; None means "nothing new"."""
    temp_db.record_source_listing(
        property_id=None, site="lejebolig", listing_id="TRUST2",
        url="https://example.test/TRUST2", raw_data={})
    temp_db.record_source_trust(
        "lejebolig", "TRUST2", unit_type_state="room_rental",
        unit_type_evidence_source="detail", unit_type_evidence_excerpt="Boligtype værelse")
    temp_db.record_source_trust(
        "lejebolig", "TRUST2", building_form_state="managed_block",
        building_profile_id=7)

    row = temp_db.get_source_listing("lejebolig", "TRUST2")
    assert row["unit_type_state"] == "room_rental"
    assert row["unit_type_evidence_source"] == "detail"
    assert row["unit_type_evidence_excerpt"] == "Boligtype værelse"
    assert row["building_form_state"] == "managed_block"
    assert row["building_profile_id"] == 7
    assert row["tenancy_channel_state"] is None

    # An unknown listing is not an error, and stores nothing.
    assert temp_db.record_source_trust("lejebolig", "MISSING1",
                                       unit_type_state="room_rental") is None


def test_evaluation_trust_facts_round_trip_and_retire(temp_db):
    _, sid = _legacy_rows(temp_db)
    first = temp_db.record_evaluation(
        source_listing_id=sid, property_id=None, observation_id=None,
        outcome="quarantined", rules_version="v3.6-1",
        eval_res=types.SimpleNamespace(
            quarantine_reasons=["landlord unverified"], score=None, tier=None,
            budget_band=None, unit_type_state="self_contained_apartment",
            tenancy_channel_state="unknown", building_form_state="managed_block",
            trust_outcome="review", trust_reason="landlord unverified"))
    stored = temp_db.current_evaluation(sid)
    assert stored["id"] == first
    assert stored["trust_outcome"] == "review"
    assert stored["trust_reason"] == "landlord unverified"
    assert stored["unit_type_state"] == "self_contained_apartment"
    assert stored["tenancy_channel_state"] == "unknown"
    assert stored["building_form_state"] == "managed_block"
    assert stored["outcome"] == "quarantined"

    second = temp_db.record_evaluation(
        source_listing_id=sid, property_id=None, observation_id=None,
        outcome="rejected", rules_version="v3.6-1",
        eval_res=types.SimpleNamespace(
            rejection_reason="room rental / not self-contained", score=None,
            trust_outcome="reject", trust_reason="room rental / not self-contained",
            unit_type_state="room_rental"))
    current = temp_db.current_evaluation(sid)
    assert current["id"] == second
    assert current["trust_outcome"] == "reject"
    assert current["unit_type_state"] == "room_rental"
    assert current["tenancy_channel_state"] is None
    history = temp_db.evaluation_history(sid)
    assert [r["is_current"] for r in history][0] == 1
    retired = {r["id"] for r in history if r["is_current"] == 0}
    assert first in retired
    assert current["id"] not in retired


def test_evaluation_without_trust_facts_writes_nulls(temp_db):
    """An older evaluator that knows nothing about trust still records a row."""
    _, sid = _legacy_rows(temp_db)
    temp_db.record_evaluation(
        source_listing_id=sid, property_id=None, observation_id=None,
        outcome="match", rules_version="v3.5-1",
        eval_res=types.SimpleNamespace(score=70.0, tier="B", budget_band="within"))
    stored = temp_db.current_evaluation(sid)
    assert stored["outcome"] == "match"
    assert stored["score"] == 70.0
    for col in EVALUATION_TRUST_COLUMNS:
        assert stored[col] is None, f"{col} should be empty for a legacy evaluation"
