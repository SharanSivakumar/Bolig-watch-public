"""Tests for the frozen building-profile manifest and its local loader.

Row v36m1 (spec 07 section 5.2, section 7.2, plan duties 1-3). The tracked
`building_profiles_v36.json` is the frozen copy of the nine proposals v36m0 read
out of stored, source-scoped operator evidence; `Store.load_building_profiles_manifest`
is the only path that writes them.

Everything runs on `temp_db` or on a throwaway file in `tmp_path`. Production
`boligwatch.db` is never opened, the loader is proven to fetch nothing, and no
test asserts a value that was derived from the thing it checks: the manifest is
compared against the v36m0 pack itself, whose SHA-256 is the hash that row
reported.
"""
import hashlib
import json
import os
import socket

import pytest

import filters
from store import (BUILDING_PROFILE_MANIFEST_PATH, BUILDING_PROFILE_MANIFEST_VERSION,
                   EVIDENCE_EXCERPT_MAX, Store, read_building_profile_manifest,
                   validate_building_profile_manifest)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(ROOT, BUILDING_PROFILE_MANIFEST_PATH)
PACK_PATH = os.path.join(ROOT, "tests", "fixtures", "v36m0_proposed_profiles.json")

# The hash v36m0 reported in REPORTS/v36m0_20260915/sha256sums.txt and in its
# REPORT and PROGRESS - the value every consumer compares against.
PACK_SHA256 = "27729248a77a11726735b7bfb27b57142f55b7951324a396e1e73a45ff32852d"
PROFILE_COUNT = 9

# Frozen scopes used by the exact-inheritance cases (source pack order).
SCOPE = "Poppelstykket 6, 2450 København SV"
SIBLING_SCOPE = "Poppelstykket 8, 2450 København SV"
STREET_ONLY = "Poppelstykket, 2450 København SV"
OTHER_CITY = "Poppelstykket 6, 2450 Aalborg"


def _manifest():
    return read_building_profile_manifest(MANIFEST_PATH)


def _manifest_with(**changes):
    manifest = json.loads(json.dumps(_manifest()))
    manifest.update(changes)
    return manifest


def _pack_items():
    with open(PACK_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _row(store, key):
    return store.get_building_profile(key)


def _rec(address, postcode="2450", **over):
    """A stored apartment advert at one address, for the building classifier."""
    rec = {"site": "boligportal", "listing_id": "1", "title": "1-værelses",
           "listing_type": "lejlighed", "card_class": "apartment", "rooms": 1,
           "address": address, "normalized_address": address, "postcode": postcode}
    rec.update(over)
    return rec


# --------------------------------------------------------------------------- #
# Duty 1 - the tracked manifest equals the hash-verified v36m0 proposal        #
# --------------------------------------------------------------------------- #
def test_tracked_manifest_is_the_hash_verified_v36m0_pack():
    """The frozen file is the pack: same hash, same count, same items, verbatim."""
    with open(PACK_PATH, "rb") as handle:
        pack_bytes = handle.read()
    assert hashlib.sha256(pack_bytes).hexdigest() == PACK_SHA256

    manifest = _manifest()
    assert manifest["source_pack_sha256"] == PACK_SHA256
    assert manifest["source_pack"].endswith("v36m0_proposed_profiles.json")
    assert manifest["manifest_version"] == BUILDING_PROFILE_MANIFEST_VERSION
    assert manifest["frozen_at"] == "2026-09-15"
    assert manifest["profile_count"] == len(manifest["profiles"]) == PROFILE_COUNT
    assert manifest["profiles"] == _pack_items()


def test_no_entry_added_a_field_the_proposal_did_not_carry():
    """`id` is the database's own key and stays out of the manifest."""
    for entry in _manifest()["profiles"]:
        assert "id" not in entry
        assert set(entry) == set(_pack_items()[0])


def test_every_frozen_scope_is_the_folded_house_numbered_address():
    """Section 5.2: an entry's scope is exact, and `profile_key` is its fold."""
    for entry in _manifest()["profiles"]:
        address = entry["normalized_address"]
        assert Store.address_is_one_building(address)
        assert entry["profile_key"] == Store.normalize_exact_address(address)
        assert entry["building_form_state"] == "managed_multi_unit_block"
        assert entry["management_state"] == "professional"
        assert entry["evidence_kind"] == "source_detail"
        assert entry["state"] == "active"
        assert entry["evidence_url"] and entry["verified_at"]
        assert len(entry["evidence_excerpt"]) <= EVIDENCE_EXCERPT_MAX


def test_the_manifest_records_the_gate_as_a_pre_release():
    """Plan duty 5: a pre-release, and no claim that a human reviewed it."""
    note = _manifest()["note"]
    assert "pre-released at 2026-09-15T11:39:36" in note
    assert "no human review or approval" in note
    for word in ("approved by", "Sharan reviewed", "operator-approved"):
        assert word not in json.dumps(_manifest())


# --------------------------------------------------------------------------- #
# Manifest validation                                                          #
# --------------------------------------------------------------------------- #
def test_a_valid_manifest_has_no_problems():
    assert validate_building_profile_manifest(_manifest()) == []


def test_an_entry_missing_a_required_field_is_rejected():
    manifest = _manifest_with()
    del manifest["profiles"][0]["evidence_kind"]
    problems = validate_building_profile_manifest(manifest)
    assert any("profiles[0]" in p and "evidence_kind" in p for p in problems)


def test_out_of_vocabulary_states_are_rejected():
    """The section 4.3/5.2 vocabularies, including v36c2's `managed_block` typo."""
    manifest = _manifest_with()
    manifest["profiles"][0]["building_form_state"] = "managed_block"
    manifest["profiles"][1]["management_state"] = "landlord"
    manifest["profiles"][2]["evidence_kind"] = "card"
    problems = validate_building_profile_manifest(manifest)
    assert any("building_form_state" in p for p in problems)
    assert any("management_state" in p for p in problems)
    assert any("evidence_kind" in p for p in problems)


def test_a_key_that_is_not_the_folded_scope_is_rejected():
    manifest = _manifest_with()
    manifest["profiles"][0]["profile_key"] = "somewhere else 1 2450"
    problems = validate_building_profile_manifest(manifest)
    assert any("is not the folded scope" in p for p in problems)


def test_a_scopeless_or_street_only_entry_is_rejected():
    manifest = _manifest_with()
    manifest["profiles"][0]["normalized_address"] = None
    manifest["profiles"][0]["complex_key"] = None
    manifest["profiles"][1]["normalized_address"] = "Poppelstykket, 2450 København SV"
    manifest["profiles"][1]["profile_key"] = "poppelstykket 2450 koebenhavn sv"
    problems = validate_building_profile_manifest(manifest)
    assert any("no scope" in p for p in problems)
    assert any("has no house number" in p for p in problems)


def test_a_header_mismatch_an_over_long_excerpt_and_duplicates_are_rejected():
    manifest = _manifest_with(profile_count=8)
    manifest["profiles"][0]["evidence_excerpt"] = "x" * (EVIDENCE_EXCERPT_MAX + 1)
    manifest["profiles"][1] = dict(manifest["profiles"][1],
                                   profile_key=manifest["profiles"][0]["profile_key"])
    problems = validate_building_profile_manifest(manifest)
    assert any("profile_count" in p for p in problems)
    assert any("evidence_excerpt is longer" in p for p in problems)
    assert any("duplicate profile_key" in p for p in problems)


def test_a_malformed_manifest_file_raises_and_writes_nothing(tmp_path, temp_db):
    """A bad manifest fails loudly rather than seeding part of a set."""
    broken = tmp_path / "broken.json"
    manifest = _manifest_with()
    del manifest["profiles"][3]["operator_name"]
    broken.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError) as raised:
        read_building_profile_manifest(str(broken))
    assert "operator_name" in str(raised.value)

    with pytest.raises(ValueError):
        temp_db.load_building_profiles_manifest(manifest)
    assert temp_db.conn.execute(
        "SELECT COUNT(*) FROM building_profiles").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# Duty 2 - the loader: nine profiles, deterministic and idempotent            #
# --------------------------------------------------------------------------- #
def test_the_loader_seeds_the_nine_frozen_profiles(temp_db):
    report = temp_db.seed_building_profiles()
    assert report["manifest_version"] == BUILDING_PROFILE_MANIFEST_VERSION
    assert report["profiles_in_manifest"] == PROFILE_COUNT
    assert len(report["created"]) == PROFILE_COUNT
    assert report["unchanged"] == [] and report["conflicts"] == []
    assert report["rows_written"] == PROFILE_COUNT and report["dry_run"] is False

    assert temp_db.conn.execute(
        "SELECT COUNT(*) FROM building_profiles").fetchone()[0] == PROFILE_COUNT
    profile = temp_db.active_building_profile_for_address(SCOPE)
    assert profile["profile_key"] == Store.normalize_exact_address(SCOPE)
    assert profile["building_form_state"] == "managed_multi_unit_block"
    assert profile["management_state"] == "professional"
    assert profile["evidence_kind"] == "source_detail"
    assert profile["state"] == "active"
    # The seeded facts are the frozen ones, unaltered.
    entry = [e for e in _manifest()["profiles"]
             if e["profile_key"] == profile["profile_key"]][0]
    assert profile["operator_name"] == entry["operator_name"]
    assert profile["evidence_url"] == entry["evidence_url"]
    assert profile["evidence_excerpt"] == entry["evidence_excerpt"]
    assert profile["verified_at"] == entry["verified_at"]
    assert profile["display_name"] == entry["display_name"]
    assert profile["complex_key"] == entry["complex_key"]


def test_loading_the_same_manifest_twice_changes_no_row(temp_db):
    first = temp_db.seed_building_profiles()
    ids = [r["id"] for r in temp_db.conn.execute(
        "SELECT id FROM building_profiles ORDER BY id").fetchall()]
    stamps = {r["profile_key"]: r["verified_at"] for r in temp_db.conn.execute(
        "SELECT profile_key, verified_at FROM building_profiles").fetchall()}

    second = temp_db.seed_building_profiles()
    assert second["created"] == [] and second["conflicts"] == []
    assert len(second["unchanged"]) == PROFILE_COUNT
    assert second["rows_written"] == 0
    assert [r["id"] for r in temp_db.conn.execute(
        "SELECT id FROM building_profiles ORDER BY id").fetchall()] == ids
    assert {r["profile_key"]: r["verified_at"] for r in temp_db.conn.execute(
        "SELECT profile_key, verified_at FROM building_profiles").fetchall()} == stamps
    assert temp_db.conn.execute(
        "SELECT COUNT(*) FROM building_profiles").fetchone()[0] == PROFILE_COUNT


def test_a_dry_run_reports_the_work_without_writing_it(temp_db):
    report = temp_db.seed_building_profiles(dry_run=True)
    assert len(report["created"]) == PROFILE_COUNT
    assert report["rows_written"] == 0 and report["dry_run"] is True
    assert temp_db.conn.execute(
        "SELECT COUNT(*) FROM building_profiles").fetchone()[0] == 0


def test_the_default_manifest_path_does_not_depend_on_the_working_directory(
        monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert read_building_profile_manifest()["profiles"] == _pack_items()


# --------------------------------------------------------------------------- #
# No cross-address inheritance                                                 #
# --------------------------------------------------------------------------- #
def test_a_frozen_profile_is_found_only_at_its_own_exact_scope(temp_db):
    temp_db.seed_building_profiles()
    assert temp_db.active_building_profile_for_address(SCOPE)
    assert temp_db.building_profiles_for_exact_address(SCOPE)
    for miss in (SIBLING_SCOPE, STREET_ONLY, OTHER_CITY, "Njalsgade 153, 2300 København S"):
        assert temp_db.active_building_profile_for_address(miss) is None
        assert temp_db.building_profiles_for_exact_address(miss) == []
    assert temp_db.active_building_profile_for_complex("viola-noerloevs-gade") is None


def test_a_listing_at_another_house_number_does_not_inherit_the_block_verdict(temp_db):
    """Section 4.3/9.3.9: exact house number only, or the verdict leaks."""
    temp_db.seed_building_profiles()
    here = filters.classify_building_form(_rec(SCOPE), temp_db)
    assert here.state == "managed_multi_unit_block" and here.decision == "pass"

    for miss in (SIBLING_SCOPE, STREET_ONLY, OTHER_CITY):
        decision = filters.classify_building_form(_rec(miss, postcode="2450"), temp_db)
        assert decision.state == "unknown", miss
        assert decision.reason == filters.BUILDING_UNVERIFIED_REASON, miss

    # Negative control: the sibling's `unknown` is the missing profile, not the
    # record. One profile written for the sibling address makes that same record
    # carry the block's own state - a bare `pass` proves nothing here, because
    # spec 08 section 4.2 makes every unknown a pass too - and the original scope
    # still reads nothing from it.
    temp_db.upsert_building_profile(
        profile_key=Store.normalize_exact_address(SIBLING_SCOPE),
        building_form_state="managed_multi_unit_block", management_state="professional",
        evidence_kind="manual", normalized_address=SIBLING_SCOPE,
        operator_name="Sibling Operator", evidence_url="https://operator.test/sibling",
        evidence_excerpt="sibling block", verified_at="2026-09-14T10:00:00")
    assert filters.classify_building_form(
        _rec(SIBLING_SCOPE, postcode="2450"), temp_db).state == "managed_multi_unit_block"
    assert len(temp_db.building_profiles_for_exact_address(SCOPE)) == 1


# --------------------------------------------------------------------------- #
# Manual preservation, supersession, and a differing stored row                #
# --------------------------------------------------------------------------- #
def _manual_profile(store):
    return store.upsert_building_profile(
        profile_key=Store.normalize_exact_address(SCOPE),
        building_form_state="small_or_private_property", management_state="private",
        evidence_kind="manual", display_name="reviewer's own note",
        normalized_address=SCOPE, operator_name="Beboerforeningen",
        evidence_url="https://review.test/poppelstykket",
        evidence_excerpt="reviewer read the operator letter",
        verified_at="2026-09-08T08:00:00")


def test_a_standing_manual_profile_is_preserved_and_comes_back_for_review(temp_db):
    """Section 7.2: a parser run cannot silently overwrite manual evidence."""
    manual_id = _manual_profile(temp_db)
    report = temp_db.seed_building_profiles()

    assert len(report["created"]) == PROFILE_COUNT - 1
    assert len(report["conflicts"]) == 1
    conflict = report["conflicts"][0]
    assert conflict["profile_key"] == Store.normalize_exact_address(SCOPE)
    assert "manual" in conflict["reason"]
    assert "building_form_state" in conflict["differences"]

    stored = _row(temp_db, Store.normalize_exact_address(SCOPE))
    assert stored["id"] == manual_id
    assert stored["evidence_kind"] == "manual"
    assert stored["building_form_state"] == "small_or_private_property"
    assert stored["management_state"] == "private"
    assert stored["operator_name"] == "Beboerforeningen"
    assert stored["evidence_excerpt"] == "reviewer read the operator letter"
    assert stored["verified_at"] == "2026-09-08T08:00:00"
    assert stored["state"] == "active"


def test_a_superseded_profile_is_not_silently_reinstated(temp_db):
    temp_db.seed_building_profiles()
    key = Store.normalize_exact_address(SCOPE)
    assert temp_db.supersede_building_profile(key, "operator changed") == 1

    report = temp_db.seed_building_profiles()
    conflicts = {c["profile_key"]: c for c in report["conflicts"]}
    assert key in conflicts and "superseded" in conflicts[key]["reason"]
    assert len(report["unchanged"]) == PROFILE_COUNT - 1
    assert report["rows_written"] == 0

    stored = _row(temp_db, key)
    assert stored["state"] == "superseded"
    assert stored["superseded_reason"] == "operator changed"
    assert temp_db.active_building_profile_for_address(SCOPE) is None


def test_a_differing_stored_profile_is_left_as_stored(temp_db):
    key = Store.normalize_exact_address(SCOPE)
    temp_db.upsert_building_profile(
        profile_key=key, building_form_state="managed_multi_unit_block",
        management_state="professional", evidence_kind="source_detail",
        normalized_address=SCOPE, operator_name="Somebody Else",
        evidence_url="https://other.test/x", evidence_excerpt="other evidence",
        verified_at="2026-09-01T00:00:00")

    report = temp_db.seed_building_profiles()
    conflicts = {c["profile_key"]: c for c in report["conflicts"]}
    assert key in conflicts and "differs" in conflicts[key]["reason"]
    assert set(conflicts[key]["differences"]) >= {"operator_name", "evidence_url",
                                                 "evidence_excerpt", "verified_at"}
    stored = _row(temp_db, key)
    assert stored["operator_name"] == "Somebody Else"
    assert stored["evidence_excerpt"] == "other evidence"
    assert stored["verified_at"] == "2026-09-01T00:00:00"
    assert len(report["created"]) == PROFILE_COUNT - 1


# --------------------------------------------------------------------------- #
# An empty approved manifest, and the offline guarantee                        #
# --------------------------------------------------------------------------- #
def test_an_empty_approved_manifest_loads_cleanly(temp_db):
    empty = _manifest_with(profiles=[], profile_count=0)
    assert validate_building_profile_manifest(empty) == []
    report = temp_db.load_building_profiles_manifest(empty)
    assert report["profiles_in_manifest"] == 0
    assert report["created"] == [] and report["unchanged"] == []
    assert report["conflicts"] == [] and report["rows_written"] == 0
    assert temp_db.conn.execute(
        "SELECT COUNT(*) FROM building_profiles").fetchone()[0] == 0

    # An empty manifest cannot delete what is already stored either.
    temp_db.seed_building_profiles()
    again = temp_db.load_building_profiles_manifest(empty)
    assert again["created"] == [] and again["rows_written"] == 0
    assert temp_db.conn.execute(
        "SELECT COUNT(*) FROM building_profiles").fetchone()[0] == PROFILE_COUNT


def test_the_loader_makes_no_network_call(temp_db, monkeypatch):
    """Section 12: local seeding fetches nothing."""
    def _boom(*args, **kwargs):
        raise AssertionError("the profile loader attempted a network call")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    report = temp_db.seed_building_profiles()
    assert report["rows_written"] == PROFILE_COUNT
