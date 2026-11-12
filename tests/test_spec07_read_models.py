"""Spec 07 sections 7 and 7.1 - the source-level four-view read models (v36q1).

The dashboard reads only `matches`, which makes quarantine, rejection, orphaning
and expiry all look like disappearance. This row adds the read models the four
views need, and this file pins them:

* every in-window current source maps to exactly ONE view, with the plan's
  pinned lifecycle-first partition order;
* the section 7.1 reason order is stable, and an unmatched reason is a gate
  (rank 6) unless it is lifecycle or identity wording (rank 7);
* the rows carry the fields the views must show (address, rent, availability,
  source, exact link, trust states, evidence provenance, provisional score,
  property identity state, every non-ranked reason) and no S-D rank;
* counts have the right scope (source-level for the three non-trusted views,
  grouped for Trusted blocks);
* the integrity primitives expose the section 3.4 null link and a forbidden
  trust state in `matches`;
* the accounting helper covers all 53 frozen former options, with
  `out_of_window` the one explained absence and `missing_source` unexplained;
* no read model writes to `matches` and none touches the network.

All work is local: `temp_db` or an explicit copy, never production
`boligwatch.db`, and `fail_on_network` turns any socket use into a failure.
"""
import datetime
import socket
from types import SimpleNamespace

import pytest

import config
import store as store_mod
from store import Store, format_bw_id


# --- harness ------------------------------------------------------------------

@pytest.fixture
def fail_on_network(monkeypatch):
    """Any socket use is a test failure, never a real request."""
    def _no_socket(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _no_socket)


class _Verdict:
    """The attributes `record_evaluation` reads off an EvaluationResult."""

    def __init__(self, quarantine_reasons=(), rejection_reason=None,
                 trust_outcome=None, trust_reason=None, score=70.0,
                 unit_type_state=None, tenancy_channel_state=None,
                 building_form_state=None):
        self.quarantine_reasons = list(quarantine_reasons)
        self.rejection_reason = rejection_reason
        self.trust_outcome = trust_outcome
        self.trust_reason = trust_reason
        self.score = score
        self.unit_type_state = unit_type_state
        self.tenancy_channel_state = tenancy_channel_state
        self.building_form_state = building_form_state


def _seed_source(store, site, listing_id, *, outcome=None, addr=None, rent=8300,
                 available="2027-01-20", lifecycle="active", expired_at=None,
                 last_seen_days=1, property_id=True, match_row=False,
                 verdict=None, url=None, title=None):
    """One property + one source listing + its current evaluation.

    `property_id=False` stores the source unlinked - the section 3.4 orphan.
    `match_row=True` seeds a `matches` row for the property by SQL, which is
    exactly the state the Trusted branch reads. Returns a dict of the ids.
    """
    now = datetime.datetime.now()
    addr = addr or f"Testgade {store.conn.execute('SELECT COUNT(*) FROM properties').fetchone()[0] + 1}, 2300 København"
    pid = None
    if property_id:
        pid = store._find_or_create_property_direct(
            addr, 55.65, 12.60, 1, 32.0, "new", now.isoformat())
    url = url or f"https://example.test/{site}/{listing_id}"
    sid = store.record_source_listing(
        pid, site, str(listing_id), url,
        {"site": site, "listing_id": str(listing_id), "address": addr,
         "title": title, "rent_total": rent, "available_from": available,
         "rooms": 1, "size_m2": 32.0})
    store.conn.execute(
        "UPDATE source_listings SET lifecycle_state=?, expired_at=?, last_seen_at=? "
        "WHERE id=?",
        (lifecycle, expired_at,
         (now - datetime.timedelta(days=last_seen_days)).isoformat(timespec="seconds"),
         sid))
    store.conn.commit()
    if match_row and pid is not None:
        store.conn.execute(
            "INSERT INTO matches(property_id, bw_id, tier, category, score, "
            "rent_total, commute_min, relevance, address, quarantined, "
            "is_responsible_pick, complex_key) "
            "VALUES(?,?,'A',1,80.0,?,15,'actionable',?,0,0,NULL)",
            (pid, format_bw_id(pid), rent, addr))
        store.conn.commit()
    if outcome is not None:
        store.record_evaluation(
            source_listing_id=sid, property_id=pid, observation_id=None,
            outcome=outcome, rules_version=config.eligibility_rules_version(),
            eval_res=verdict or _Verdict())
    return {"property_id": pid, "source_listing_id": sid, "site": site,
            "listing_id": str(listing_id)}


def _days_ago(n):
    return (datetime.datetime.now() - datetime.timedelta(days=n)).isoformat(
        timespec="seconds")


def _rows_by_id(rows):
    return {(r["site"], r["listing_id"]): r for r in rows}


# --- duty 1/2: one view per source, exact link, pinned partition order --------

def test_the_four_views_each_hold_exactly_one_of_the_four_fixtures(temp_db,
                                                                   fail_on_network):
    """Spec 07 section 9.4 item 1: each fixture is in exactly one view."""
    store = temp_db
    trusted = _seed_source(store, "test", "T1", outcome="match", match_row=True,
                           addr="Trustedvej 1, 2300 København")
    review = _seed_source(store, "test", "R1", outcome="quarantined",
                          addr="Reviewvej 2, 2300 København",
                          verdict=_Verdict(quarantine_reasons=["unverified lease duration"]))
    gone = _seed_source(store, "test", "G1", outcome="quarantined",
                        lifecycle="expired", expired_at=_days_ago(3),
                        last_seen_days=3, addr="Gonevej 3, 2300 København")
    excluded = _seed_source(store, "test", "E1", outcome="rejected",
                            addr="Excludedvej 4, 2300 København",
                            verdict=_Verdict(rejection_reason="rent 9800 > 9500"))

    views = {
        "trusted_blocks": store.trusted_block_rows(),
        "needs_verification": store.needs_verification_rows(),
        "recently_gone": store.recently_gone_rows(),
        "excluded": store.excluded_rows(),
    }
    # Source-level views: the fixture is in exactly its own view.
    source_level = {key: _rows_by_id(rows) for key, rows in views.items()
                    if key != "trusted_blocks"}
    for key, expected in (("needs_verification", review),
                          ("recently_gone", gone),
                          ("excluded", excluded)):
        key_id = (expected["site"], expected["listing_id"])
        assert key_id in source_level[key], f"{key_id} missing from {key}"
        for other, rows in source_level.items():
            if other != key:
                assert key_id not in rows, f"{key_id} also in {other}"
    # Trusted blocks is the grouped match path, so the trusted source has no
    # source-level row of its own anywhere.
    trusted_id = (trusted["site"], trusted["listing_id"])
    for rows in source_level.values():
        assert trusted_id not in rows
    assert [r["property_id"] for r in views["trusted_blocks"]] == [trusted["property_id"]]

    # Every row names its own view, and no row is in two.
    for key, rows in views.items():
        if key == "trusted_blocks":
            continue
        assert {r["view"] for r in rows} == {key}
    all_rows = store.source_view_rows()
    assert len(all_rows) == 3, "the three source-level views, one row each"
    assert sorted(r["view"] for r in all_rows) == [
        "excluded", "needs_verification", "recently_gone"]


def test_the_expiry_window_boundary_decides_recently_gone(temp_db):
    """Rule 1 uses `config.EXPIRED_VISIBLE_DAYS`, and beyond it is no view."""
    store = temp_db
    inside = _seed_source(store, "test", "IN", outcome="quarantined",
                          lifecycle="expired", expired_at=_days_ago(
                              config.EXPIRED_VISIBLE_DAYS - 1),
                          last_seen_days=13,
                          addr="Insidevej 1, 2300 København")
    outside = _seed_source(store, "test", "OUT", outcome="quarantined",
                           lifecycle="expired", expired_at=_days_ago(
                               config.EXPIRED_VISIBLE_DAYS + 1),
                           last_seen_days=15,
                           addr="Outsidevej 2, 2300 København")

    gone = _rows_by_id(store.recently_gone_rows())
    assert (inside["site"], inside["listing_id"]) in gone
    assert (outside["site"], outside["listing_id"]) not in gone, \
        "an expired source beyond the window is in no view"

    row = gone[(inside["site"], inside["listing_id"])]
    assert row["lifecycle_state"] == "expired"
    assert row["last_seen_days_ago"] == 13, "the row carries last seen N days ago"
    assert store_mod.REASON_LIFECYCLE_EXPIRED in row["reasons"], \
        "the lifecycle is stated as the section 7.1 item 7 reason"

    # The out-of-window source is in no view at all, and the accounting helper
    # names its absence as the explained one.
    for rows in (store.source_view_rows(), store.needs_verification_rows(),
                 store.excluded_rows()):
        assert (outside["site"], outside["listing_id"]) not in _rows_by_id(rows)
    internal = {(r["site"], r["listing_id"]): r
                for r in store._source_read_model_rows()}
    assert internal[(outside["site"], outside["listing_id"])]["view"] == \
        store_mod.VIEW_OUT_OF_WINDOW
    assert internal[(inside["site"], inside["listing_id"])]["view"] == \
        store_mod.VIEW_RECENTLY_GONE


def test_lifecycle_precedes_the_outcome_and_the_other_rules_are_a_chain(temp_db):
    """Rule 1 beats every outcome rule; a match with no match row is review."""
    store = temp_db
    expired_reject = _seed_source(store, "test", "XR", outcome="rejected",
                                  lifecycle="expired", expired_at=_days_ago(2),
                                  addr="Expiredreject 1, 2300 København",
                                  verdict=_Verdict(rejection_reason="rent 9800 > 9500"))
    orphan_match = _seed_source(store, "test", "OM", outcome="match",
                                property_id=False,
                                addr="Orphan 2, 2300 København")
    unrepresented_match = _seed_source(store, "test", "UM", outcome="match",
                                       addr="Norep 3, 2300 København")

    gone_ids = _rows_by_id(store.recently_gone_rows())
    excluded_ids = _rows_by_id(store.excluded_rows())
    review_ids = _rows_by_id(store.needs_verification_rows())

    assert (expired_reject["site"], expired_reject["listing_id"]) in gone_ids
    assert (expired_reject["site"], expired_reject["listing_id"]) not in excluded_ids
    gone_row = gone_ids[(expired_reject["site"], expired_reject["listing_id"])]
    assert gone_row["outcome"] == "rejected", "the last decision is shown"

    for case, label in ((orphan_match, "no property link"),
                        (unrepresented_match, "no matches row")):
        key = (case["site"], case["listing_id"])
        assert key in review_ids, f"a match with {label} needs verification"
        assert review_ids[key]["primary_reason"] == \
            store_mod.REASON_IDENTITY_UNRESOLVED
        assert review_ids[key]["property_identity_state"] == (
            "unresolved" if case is orphan_match else "resolved")
        assert key not in excluded_ids and key not in gone_ids


def test_a_view_row_carries_the_fields_the_views_must_show_and_no_rank(temp_db):
    """Section 7 item 2 / section 9.4 item 2: link and reasons, no S-D badge."""
    store = temp_db
    seeded = _seed_source(
        store, "test", "F1", outcome="quarantined", rent=8750,
        available="2027-02-01", addr="Fieldvej 7, 2300 København",
        title="Lys 2-vaerelses", verdict=_Verdict(
            quarantine_reasons=["unverified bathroom", "rent 8750 > 8500"],
            trust_outcome="review",
            trust_reason=config.TRUST_REASON_UNIT_UNVERIFIED,
            score=63.0, unit_type_state="self_contained_apartment",
            tenancy_channel_state="unknown", building_form_state="unknown"))

    row = _rows_by_id(store.needs_verification_rows())[("test", "F1")]
    assert row["url"] == "https://example.test/test/F1", "the exact listing URL"
    assert row["site"] == "test" and row["listing_id"] == "F1"
    assert row["title"] == "Lys 2-vaerelses"
    assert row["address"] == "Fieldvej 7, 2300 København"
    assert row["rent_total"] == 8750
    assert row["available_from"] == "2027-02-01"
    assert row["outcome"] == "quarantined"
    assert row["trust_outcome"] == "review"
    assert row["trust_reason"] == config.TRUST_REASON_UNIT_UNVERIFIED
    assert row["unit_type_state"] == "self_contained_apartment"
    assert row["tenancy_channel_state"] == "unknown"
    assert row["building_form_state"] == "unknown"
    assert row["provisional_score"] == 63.0, "a review score is provisional"
    assert row["property_identity_state"] == "resolved"
    assert row["property_id"] == seeded["property_id"]
    for forbidden in ("rank", "tier", "display_tier", "badge"):
        assert forbidden not in row, "a non-ranked row carries no S-D badge"
    assert row["reasons"] == sorted(
        row["reasons"], key=store_mod.reason_rank), \
        "the reasons are stored in section 7.1 order"
    assert row["primary_reason"] == row["reasons"][0]


# --- duty 3: the reason order -------------------------------------------------

def test_reason_rank_follows_section_7_1():
    """The seven ranks, the gate fallback and the lifecycle/identity exception."""
    rank = store_mod.reason_rank
    # 1. room rental / not self-contained (the v3.5 composite takes rank 1).
    assert rank(config.TRUST_REASON_ROOM) == 1
    assert rank("shared housing / room rental") == 1
    assert rank(config.TRUST_REASON_UNIT_UNVERIFIED) == 1
    # 2. sublet or private landlord. Spec 08 section 6.2 removed the
    #    `landlord / direct lease unverified` string from this matcher: no code
    #    path emits it, so there is no rank left to assert for it.
    assert rank("sublet") == 2
    assert rank("private landlord") == 2
    # 3. student, youth, senior, care or shared housing (and the v3.4
    #    housing-evidence quarantine, which is that evidence).
    for text in ("student housing", "not a studio (kollegie)",
                 "elderly / senior housing", "care housing", "shared housing",
                 "housing eligibility unverified"):
        assert rank(text) == 3
    # 4. professional management or apartment-block evidence missing. Spec 08
    #    section 6.2 dropped the removed `managed apartment block unverified`
    #    string; the rank keeps the section 7.2 lifecycle wording this release
    #    still emits as badge-side notes.
    assert rank(config.TRUST_REASON_STALE) == 4
    assert rank(config.TRUST_REASON_CONFLICT) == 4
    # 5. private kitchen, bathroom or unlimited lease unverified.
    assert rank("shared kitchen") == 5
    assert rank("unverified bathroom") == 5
    assert rank("unverified lease duration") == 5
    assert rank("time-limited lease (time-limited (12 months))") == 5
    # 6. rent, move-in, centrality, transit or commute gate.
    assert rank("rent 9800 > 9500") == 6
    assert rank("move-in cost 58065 > 50000") == 6
    assert rank("commute 51 min > 50") == 6
    assert rank("no Metro within 1500.0 m and no S-train within 800.0 m") == 6
    # 7. lifecycle state or unresolved property identity.
    assert rank(store_mod.REASON_IDENTITY_UNRESOLVED) == 7
    assert rank(store_mod.REASON_LIFECYCLE_EXPIRED) == 7
    # Unmatched is a gate - the 1-2 room gate states no reason of its own.
    assert rank("apartment with 3 rooms (1-2 rooms required)") == 6
    assert rank("something nobody has seen before") == 6
    assert rank(None) == store_mod.REASON_RANK_GATE


def test_ordered_reasons_rank_then_original_order():
    """Ranks decide first; equal ranks keep the order they were collected in."""
    ordered = store_mod.ordered_reasons([
        "rent 9800 > 9500",                     # 6
        "unverified lease duration",            # 5
        "private landlord",                     # 2
        "shared kitchen",                       # 5
        store_mod.REASON_IDENTITY_UNRESOLVED,   # 7
    ])
    assert [rank for rank, _text in ordered] == [2, 5, 5, 6, 7]
    assert [text for _rank, text in ordered][1:3] == [
        "unverified lease duration", "shared kitchen"], \
        "equal ranks keep their original order"
    assert store_mod.ordered_reasons([]) == []


def test_the_rank_table_is_module_level_and_uses_the_config_constants():
    """Section 7.1's table is one module-level (rank, matcher) list."""
    assert isinstance(store_mod.REASON_PRECEDENCE, list)
    assert [rank for rank, _matcher in store_mod.REASON_PRECEDENCE] == \
        [1, 2, 3, 4, 5, 6, 7]
    for rank, matcher, reason in (
            (1, store_mod.REASON_PRECEDENCE[0][1], config.TRUST_REASON_ROOM),
            (1, store_mod.REASON_PRECEDENCE[0][1],
             config.TRUST_REASON_UNIT_UNVERIFIED),
            (2, store_mod.REASON_PRECEDENCE[1][1], "private landlord"),
            (4, store_mod.REASON_PRECEDENCE[3][1], config.TRUST_REASON_STALE),
            (4, store_mod.REASON_PRECEDENCE[3][1], config.TRUST_REASON_CONFLICT)):
        assert matcher.search(reason), \
            f"rank {rank} matches the reason it is built from"
    # Spec 08 section 6.2 removed the two v3.6 hold reasons from the rank-2 and
    # rank-4 matchers. The landlord string is claimed by no rank any more, so it
    # falls to the gate fallback; the block wording is still matched by rank 4's
    # generic "block unverified" alternative, which section 6.2 keeps for
    # ordering stability (rank 4 now matches only the section 7.2 badge-side
    # notes this release still emits).
    assert store_mod.reason_rank("landlord / direct lease unverified") == \
        store_mod.REASON_RANK_GATE
    assert store_mod.reason_rank("managed apartment block unverified") == 4
    # The forbidden-state predicates are drawn from the config vocabularies.
    assert set(store_mod.FORBIDDEN_UNIT_TYPE_STATES) <= set(config.TRUST_UNIT_TYPE_STATES)
    assert set(store_mod.FORBIDDEN_TENANCY_CHANNEL_STATES) <= \
        set(config.TRUST_TENANCY_CHANNEL_STATES)


def test_a_review_row_orders_its_reasons_by_rank(temp_db):
    """The stored order does not decide the shown order; the rank does."""
    store = temp_db
    _seed_source(store, "test", "ORD", outcome="quarantined",
                 addr="Ordrevej 1, 2300 København",
                 verdict=_Verdict(
                     quarantine_reasons=["rent 9800 > 9500", "shared kitchen"],
                     trust_outcome="review",
                     trust_reason=config.TRUST_REASON_UNIT_UNVERIFIED))
    row = _rows_by_id(store.needs_verification_rows())[("test", "ORD")]
    # v3.11 (spec 10): a Q1/Q2 hold leads with its own reason; the trust reason is
    # a badge on ranked rows and is not a hold reason.
    assert row["reasons"] == ["shared kitchen", "rent 9800 > 9500"]
    assert row["reason_ranks"] == [5, 6]
    assert row["primary_reason"] == "shared kitchen"


# --- evidence provenance ------------------------------------------------------

def test_evidence_provenance_comes_from_the_section_5_1_columns_and_the_profile(
        temp_db):
    """Section 7.1: the row says where the reason's evidence came from, and when."""
    store = temp_db
    seeded = _seed_source(store, "test", "EV", outcome="quarantined",
                          addr="Evidencevej 9, 2300 København",
                          verdict=_Verdict(
                              quarantine_reasons=[config.TRUST_REASON_UNIT_UNVERIFIED],
                              trust_outcome="review",
                              trust_reason=config.TRUST_REASON_UNIT_UNVERIFIED))
    store.record_source_trust(
        "test", "EV",
        unit_type_state="self_contained_apartment",
        unit_type_evidence_source="detail",
        unit_type_evidence_excerpt="Boligtype Lejlighed",
        tenancy_channel_state="professional_direct",
        landlord_name="Test Ejendomme A/S",
        landlord_evidence_source="structured",
        landlord_evidence_excerpt="Testgade Have - Test Ejendomme A/S",
        building_form_state="managed_multi_unit_block",
        building_profile_id=store.upsert_building_profile(
            "testgade-have", "managed_multi_unit_block", "professional",
            "operator_page", display_name="Testgade Have",
            complex_key="testgade-have",
            operator_name="Test Ejendomme A/S",
            evidence_url="https://example.test/operator/testgade-have",
            evidence_excerpt="Test Ejendomme A/S administers Testgade Have",
            verified_at="2026-09-10T09:00:00"))

    row = _rows_by_id(store.needs_verification_rows())[("test", "EV")]
    # The latest observation, not raw_data, is the row's rent and availability
    # (plan duty 1: "current evaluations and latest observation/snapshot only").
    store.record_observation(
        seeded["source_listing_id"], seeded["property_id"], None,
        {"site": "test", "listing_id": "EV", "rent_total": 8750,
         "available_from": "2027-03-01", "detail_state": "fetched"},
        _Verdict())
    row = _rows_by_id(store.needs_verification_rows())[("test", "EV")]
    assert row["rent_total"] == 8750, "the latest observation wins"
    assert row["available_from"] == "2027-03-01"
    assert row["unit_type_state"] == "self_contained_apartment"
    assert row["unit_type_evidence_source"] == "detail"
    assert row["unit_type_evidence_excerpt"] == "Boligtype Lejlighed"
    assert row["landlord_name"] == "Test Ejendomme A/S"
    assert row["landlord_evidence_source"] == "structured"
    assert row["landlord_evidence_excerpt"].startswith("Testgade Have")
    assert row["building_form_state"] == "managed_multi_unit_block"
    assert row["building_profile_id"] is not None
    assert row["building_profile_verified_at"] == "2026-09-10T09:00:00", \
        "section 7.2's evidence date comes through building_profile_id"
    assert row["observed_at"] is not None, "the row carries its observation time"
    assert row["last_seen_days_ago"] == 1
    assert seeded["property_id"] is not None


# --- duty 3: counts, and what they are counted over ---------------------------

def test_view_counts_keys_are_exactly_the_four_named_views(temp_db):
    """The plan pins the key names; the reader labels the trusted one."""
    counts = temp_db.view_counts()
    assert list(counts) == ["trusted_blocks", "needs_verification",
                            "recently_gone", "excluded"]
    assert set(counts) == set(store_mod.VIEW_KEYS)


def test_view_counts_scope_is_source_level_except_trusted(temp_db):
    """Review/gone/excluded count sources; trusted counts grouped properties."""
    store = temp_db
    # Two eligible siblings in ONE property, one match row -> one trusted block.
    pid = store._find_or_create_property_direct(
        "Groupvej 1, 2300 København", 55.65, 12.60, 1, 32.0, "new",
        datetime.datetime.now().isoformat())
    for lid in ("GA", "GB"):
        store.record_source_listing(
            pid, "test", lid, f"https://example.test/test/{lid}",
            {"site": "test", "address": "Groupvej 1, 2300 København",
             "rent_total": 8000})
        sid = store.conn.execute(
            "SELECT id FROM source_listings WHERE site='test' AND listing_id=?",
            (lid,)).fetchone()[0]
        store.record_evaluation(sid, pid, None, "match",
                                config.eligibility_rules_version(),
                                eval_res=_Verdict(trust_outcome="eligible"))
    store.conn.execute(
        "INSERT INTO matches(property_id, bw_id, tier, category, score, rent_total, "
        "commute_min, relevance, address, quarantined, is_responsible_pick, "
        "complex_key) VALUES(?,?,'A',1,80.0,8000,15,'actionable',?,0,0,NULL)",
        (pid, format_bw_id(pid), "Groupvej 1, 2300 København"))
    store.conn.commit()
    _seed_source(store, "test", "RV", outcome="quarantined",
                 addr="Reviewvej 5, 2300 København")
    _seed_source(store, "test", "EX", outcome="rejected",
                 addr="Exvej 6, 2300 København",
                 verdict=_Verdict(rejection_reason="rent 9800 > 9500"))
    _seed_source(store, "test", "GN", outcome="quarantined", lifecycle="expired",
                 expired_at=_days_ago(4), addr="Gonevej 7, 2300 København")

    counts = store.view_counts()
    assert counts["needs_verification"] == 1, "source-level: one review source"
    assert counts["excluded"] == 1
    assert counts["recently_gone"] == 1
    assert counts["trusted_blocks"] == 1, \
        "grouped: two eligible siblings collapse to one trusted block"
    assert len(store.trusted_block_rows()) == counts["trusted_blocks"]


def test_trusted_view_is_the_existing_grouped_match_path(temp_db):
    """Section 7: a sibling is counted inside its property group, not listed."""
    store = temp_db
    pid = store._find_or_create_property_direct(
        "Siblingvej 1, 2300 København", 55.65, 12.60, 1, 32.0, "new",
        datetime.datetime.now().isoformat())
    for lid in ("SA", "SB"):
        store.record_source_listing(
            pid, "test", lid, f"https://example.test/test/{lid}",
            {"site": "test", "address": "Siblingvej 1, 2300 København",
             "rent_total": 8000})
    store.conn.execute(
        "INSERT INTO matches(property_id, bw_id, tier, category, score, rent_total, "
        "commute_min, relevance, address, quarantined, is_responsible_pick, "
        "complex_key) VALUES(?,?,'A',1,80.0,8000,15,'actionable',?,0,0,NULL)",
        (pid, format_bw_id(pid), "Siblingvej 1, 2300 København"))
    store.conn.commit()

    rows = store.trusted_block_rows()
    assert [r["property_id"] for r in rows] == [pid]
    assert rows[0]["group_members"] == [pid]
    assert store.trusted_block_rows() == store.grouped_matches(), \
        "the trusted view IS the existing match path"

    # The trustworthy sibling whose outcome is review never appears there.
    _seed_source(store, "test", "SC", outcome="quarantined", addr=None,
                 property_id=True)
    assert len(store.trusted_block_rows()) == 1


# --- duty 3: the integrity primitives -----------------------------------------

def test_integrity_counts_report_a_null_link_and_a_forbidden_row(temp_db):
    """Section 9.4 item 4: a null-linked current match raises the warning."""
    store = temp_db
    orphan = _seed_source(store, "test", "NL", outcome="match",
                          property_id=False, addr="Nullvej 1, 2300 København")
    assert orphan["property_id"] is None
    # A `matches` row supported by a sublet verdict is forbidden.
    bad = _seed_source(store, "test", "FB", outcome="match", match_row=True,
                       addr="Forbiddenvej 2, 2300 København",
                       verdict=_Verdict(trust_outcome="reject",
                                        tenancy_channel_state="sublet"))

    counts = store.integrity_counts()
    assert counts == {"null_linked_current_matches": 1,
                      "forbidden_trust_in_matches": 1,
                      "explicit_gone_in_matches": 0}, (counts, bad)

    # A room rental in `matches` is forbidden even when its outcome is a match.
    _seed_source(store, "test", "RM", outcome="match", match_row=True,
                 addr="Roomvej 3, 2300 København",
                 verdict=_Verdict(trust_outcome="eligible",
                                  unit_type_state="room_rental"))
    assert store.integrity_counts()["forbidden_trust_in_matches"] == 2
    # A sublet sibling likewise.
    _seed_source(store, "test", "SB", outcome="match", match_row=True,
                 addr="Subletvej 4, 2300 København",
                 verdict=_Verdict(trust_outcome="eligible",
                                  tenancy_channel_state="sublet"))
    assert store.integrity_counts()["forbidden_trust_in_matches"] == 3


def test_a_null_trust_outcome_does_not_forbid_a_match(temp_db):
    """v3.11 (spec 10): the trust outcome no longer gates a match row."""
    import run
    assert run._trust_eligible(SimpleNamespace(trust_outcome=None)) is False
    store = temp_db
    _seed_source(store, "test", "LEG", outcome="match", match_row=True,
                 addr="Legacyvej 1, 2300 København")
    assert store.integrity_counts() == {"null_linked_current_matches": 0,
                                        "forbidden_trust_in_matches": 0,
                                        "explicit_gone_in_matches": 0}


def test_integrity_counts_leave_a_match_row_with_no_evaluation_alone(temp_db):
    """Section 0b's boundary: a lifecycle/identity state, owned by the audit."""
    store = temp_db
    pid = store._find_or_create_property_direct(
        "Noevalvej 1, 2300 København", 55.65, 12.60, 1, 32.0, "new",
        datetime.datetime.now().isoformat())
    store.conn.execute(
        "INSERT INTO matches(property_id, bw_id, tier, category, score, rent_total, "
        "commute_min, relevance, address, quarantined, is_responsible_pick, "
        "complex_key) VALUES(?,?,'A',1,60.0,8000,15,'actionable',?,0,0,NULL)",
        (pid, format_bw_id(pid), "Noevalvej 1, 2300 København"))
    store.conn.commit()
    assert store.integrity_counts()["forbidden_trust_in_matches"] == 0
    assert store.integrity_counts()["null_linked_current_matches"] == 0


# --- duty 3: the frozen former-options accounting -----------------------------

def test_the_fixture_is_the_frozen_53_row_superset():
    """The accounting set is the fixture, not a re-derived 27-row set."""
    import json
    with open(store_mod.FORMER_OPTIONS_FIXTURE_PATH, encoding="utf-8") as fh:
        fixture = json.load(fh)
    assert fixture["_provenance"]["row_count"] == 53
    assert len(fixture["former_options"]) == 53
    assert len(fixture["_provenance"]["spec_03_3_name_hits"]) == 27
    assert sum(1 for e in fixture["former_options"] if e["spec_03_3_named"]) == 39
    assert fixture["_provenance"]["source_sha256"]


def test_accounting_helper_over_three_fixture_ids(temp_db):
    """Plan duty 4: one trusted, one out of window, one absent."""
    import json
    with open(store_mod.FORMER_OPTIONS_FIXTURE_PATH, encoding="utf-8") as fh:
        entries = json.load(fh)["former_options"]
    keys = [(e["site"], str(e["listing_id"])) for e in entries]

    store = temp_db
    trusted_key, window_key, absent_key = keys[0], keys[1], keys[2]
    _seed_source(store, trusted_key[0], trusted_key[1], outcome="match",
                 match_row=True, addr="Formervej 1, 2300 København")
    _seed_source(store, window_key[0], window_key[1], outcome="quarantined",
                 lifecycle="expired",
                 expired_at=_days_ago(config.EXPIRED_VISIBLE_DAYS + 5),
                 last_seen_days=40, addr="Formervej 2, 2300 København")

    rows = store.former_options_accounting()
    assert len(rows) == 53, "one row per fixture entry, all 53"
    by_id = {(r["site"], r["listing_id"]): r for r in rows}
    assert by_id[trusted_key]["view"] == "trusted"
    assert by_id[window_key]["view"] == "out_of_window"
    assert by_id[absent_key]["view"] == "missing_source"
    # Every entry is named with one of the six pinned values, and the rest of
    # the corpus is the unexplained (and on production zero) absence.
    assert {r["view"] for r in rows} <= {"trusted", "review", "gone", "excluded",
                                         "out_of_window", "missing_source"}
    assert sum(1 for r in rows if r["view"] == "missing_source") == 51
    # The row keeps the fixture's own identity and the section 7.1 reason.
    assert by_id[trusted_key]["spec_03_3_named"] is True
    assert by_id[trusted_key]["url"] == f"https://example.test/{trusted_key[0]}/{trusted_key[1]}"
    assert by_id[absent_key]["property_id"] is None
    assert by_id[absent_key]["url"], "a absent row still cites its frozen URL"
    assert rows[0]["listing_id"] == keys[0][1], "fixture order is preserved"


def test_accounting_names_a_source_that_has_no_current_verdict(temp_db):
    """Present but unevaluated is unexplained, like an absent fixture ID."""
    store = temp_db
    _seed_source(store, "test", "NOCUR", outcome=None,
                 addr="Nocurrentvej 1, 2300 København")
    internal = {(r["site"], r["listing_id"]): r
                for r in store._source_read_model_rows()}
    assert internal[("test", "NOCUR")]["view"] is None, "no verdict, no view"
    for rows in (store.source_view_rows(), store.needs_verification_rows(),
                 store.recently_gone_rows(), store.excluded_rows()):
        assert ("test", "NOCUR") not in {(r["site"], r["listing_id"]) for r in rows}


def test_accounting_reports_the_exactly_one_partition_on_a_seeded_corpus(temp_db):
    """Acceptance: every in-window current source lands in one view."""
    store = temp_db
    _seed_source(store, "test", "V1", outcome="match", match_row=True,
                 addr="Partvej 1, 2300 København")
    _seed_source(store, "test", "V2", outcome="quarantined",
                 addr="Partvej 2, 2300 København")
    _seed_source(store, "test", "V3", outcome="rejected",
                 addr="Partvej 3, 2300 København",
                 verdict=_Verdict(rejection_reason="rent 9800 > 9500"))
    _seed_source(store, "test", "V4", outcome="quarantined", lifecycle="expired",
                 expired_at=_days_ago(2), addr="Partvej 4, 2300 København")

    internal = store._source_read_model_rows()
    named = [r for r in internal if r["view"] is not None]
    assert len(named) == 4, "every in-window current source has a view"
    assert sorted(r["view"] for r in named) == [
        "excluded", "needs_verification", "recently_gone", "trusted_blocks"]
    counts = store.view_counts()
    assert counts["needs_verification"] + counts["recently_gone"] + \
        counts["excluded"] == 3
    # No view is a second view: the three source-level counts partition the
    # source-level rows exactly.
    source_level = store.source_view_rows()
    assert len(source_level) == 3
    assert len({(r["site"], r["listing_id"]) for r in source_level}) == 3


# --- no writes, no network ----------------------------------------------------

def test_no_read_model_writes_to_matches(temp_db, fail_on_network):
    """Section 7.1: a view is never materialised into `matches`."""
    store = temp_db
    _seed_source(store, "test", "W1", outcome="quarantined",
                 addr="Writevej 1, 2300 København")
    _seed_source(store, "test", "W2", outcome="rejected",
                 addr="Writevej 2, 2300 København",
                 verdict=_Verdict(rejection_reason="rent 9800 > 9500"))
    _seed_source(store, "test", "W3", outcome="match", match_row=True,
                 addr="Writevej 3, 2300 København")

    def snapshot():
        return sorted(tuple(r) for r in store.conn.execute(
            "SELECT * FROM matches").fetchall())

    before = snapshot()
    eval_before = store.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
    store.view_counts()
    store.integrity_counts()
    store.source_view_rows()
    store.needs_verification_rows()
    store.recently_gone_rows()
    store.excluded_rows()
    store.trusted_block_rows()
    store.former_options_accounting()
    assert snapshot() == before, "no read model touched `matches`"
    assert store.conn.execute(
        "SELECT COUNT(*) FROM evaluations").fetchone()[0] == eval_before


def test_no_read_model_touches_the_network(temp_db, fail_on_network):
    """Every read is local: the socket guard is the assertion."""
    store = temp_db
    _seed_source(store, "test", "N1", outcome="quarantined",
                 addr="Netvej 1, 2300 København")
    assert store.view_counts()["needs_verification"] == 1
    assert len(store.former_options_accounting()) == 53
    assert store.integrity_counts()["null_linked_current_matches"] == 0


def test_a_temp_db_file_can_be_reopened_and_the_views_still_agree():
    """The read models need no migration of their own: reopening changes nothing."""
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        first = Store(db_path=path)
        _seed_source(first, "test", "RE", outcome="quarantined",
                     addr="Reopenvej 1, 2300 København")
        counts = first.view_counts()
        first.conn.close()
        second = Store(db_path=path)
        assert second.view_counts() == counts
        assert len(second.needs_verification_rows()) == 1
        second.conn.close()
    finally:
        os.remove(path)
