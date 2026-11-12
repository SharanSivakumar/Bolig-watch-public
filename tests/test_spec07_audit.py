"""Spec 07 section 8 - the read-only integrity audit command (v36a1).

`Store.audit_integrity()` is one result object and `run.py --audit-integrity`
renders it twice (human text, stable JSON). This file pins:

* every section 8 fact is present in the result and in the human output:
  source counts by site/lifecycle/link, current evaluation counts by
  outcome/link, null-linked current match evaluations, grouped omissions,
  abnormal omissions, eligible rows failing a trust invariant, room/sublet/
  private rows in `matches`, and review counts by reason;
* the critical set is exactly the pinned invariants (`null_linked_current_matches`,
  `forbidden_in_matches`, `eligible_invariant_failures`) in pinned order, `ok` is
  its negation, and the command exits 2 on a violation and 0 otherwise;
* normal building consolidation is REPORTED and is not an error (section 8);
* the JSON is `json.dumps(result, sort_keys=True, ensure_ascii=False, indent=1)`
  and nothing else on stdout;
* the audit is strictly read-only: every statement it issues is a SELECT/WITH/
  PRAGMA, no write transaction is opened, and it never touches the network or an
  alert;
* `--db` audits the path it names (a copy), never production.

All work is local: `temp_db` or an explicit copy, never production
`boligwatch.db`, and `fail_on_network` turns any socket use into a failure.
"""
import datetime
import json
import socket
import sys

import pytest

import config
import run
import store as store_mod
from store import Store, format_bw_id

# The pinned result keys (plan duty 1 & spec 09).
AUDIT_KEYS = {"sources", "evaluations", "null_linked_current_matches",
              "grouped_omissions", "abnormal_omissions",
              "eligible_invariant_failures", "forbidden_in_matches",
              "explicit_gone_in_matches", "invalid_preference_values",
              "demographic_fact_conflicts", "duplicate_dataset_placement",
              "review_by_reason", "critical", "ok"}

# The pinned critical invariants, in report order (plan duty 2 & spec 09).
CRITICAL_INVARIANTS = ("null_linked_current_matches", "forbidden_in_matches",
                       "eligible_invariant_failures", "explicit_gone_in_matches",
                       "duplicate_dataset_placement")

READ_ONLY_PREFIXES = ("SELECT", "WITH", "PRAGMA")


# --- harness ------------------------------------------------------------------

@pytest.fixture
def fail_on_network(monkeypatch):
    """Any socket use is a test failure, never a real request."""
    def _no_socket(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _no_socket)
    monkeypatch.setattr(socket, "create_connection", _no_socket)


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


def _eligible_verdict(**over):
    """An `eligible` verdict carrying every positive fact (the badge shape).

    Spec 08 section 6.2 no longer requires the triple for `eligible` - it only
    forbids explicit rejection evidence - but a fully evidenced row is still the
    shape a trusted row has, so the audit cases seed it and override one field.
    """
    fields = {"trust_outcome": store_mod.ELIGIBLE_TRUST_OUTCOME,
              "unit_type_state": store_mod.ELIGIBLE_UNIT_TYPE_STATE,
              "tenancy_channel_state": store_mod.ELIGIBLE_TENANCY_CHANNEL_STATE,
              "building_form_state": store_mod.ELIGIBLE_BUILDING_FORM_STATE}
    fields.update(over)
    return _Verdict(**fields)


def _seed_source(store, site, listing_id, *, outcome=None, lifecycle="active",
                 property_id=True, match_row=False, verdict=None,
                 quarantine_reasons=(), addr=None, rent=8300):
    """One property + one source listing + its current evaluation.

    `property_id=False` stores the source unlinked (the section 3.4 orphan), an
    integer links it to that existing property, and `True` creates its own.
    `match_row=True` seeds the `matches` row the Trusted branch reads. Returns
    the ids it used.
    """
    now = datetime.datetime.now()
    if addr is None:
        count = store.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
        addr = f"Testgade {count + 1}, 2300 Koebenhavn"
    if property_id is False:
        pid = None
    elif property_id is True:
        pid = store._find_or_create_property_direct(
            addr, 55.65, 12.60, 1, 32.0, "new", now.isoformat())
    else:
        pid = property_id
    url = f"https://example.test/{site}/{listing_id}"
    sid = store.record_source_listing(
        pid, site, str(listing_id), url,
        {"site": site, "listing_id": str(listing_id), "address": addr,
         "title": None, "rent_total": rent, "available_from": "2027-01-20",
         "rooms": 1, "size_m2": 32.0})
    store.conn.execute(
        "UPDATE source_listings SET lifecycle_state=?, last_seen_at=? WHERE id=?",
        (lifecycle, now.isoformat(timespec="seconds"), sid))
    store.conn.commit()

    if outcome is not None:
        v = verdict or _Verdict(quarantine_reasons=quarantine_reasons)
        ev_id = store.record_evaluation(
            source_listing_id=sid, property_id=pid, observation_id=None,
            outcome=outcome, rules_version=config.eligibility_rules_version(),
            eval_res=v)
        store.conn.execute(
            "UPDATE evaluations SET is_current=1 WHERE id=?", (ev_id,))
        store.conn.commit()

    if match_row and pid is not None:
        store.conn.execute(
            "INSERT INTO matches(property_id, bw_id, site, listing_id, tier, category, score, "
            "rent_total, commute_min, relevance, address, quarantined, "
            "is_responsible_pick, complex_key) "
            "VALUES(?,?,'boligportal','83','A',1,80.0,?,15,'actionable',?,0,0,NULL)",
            (pid, format_bw_id(pid), rent, addr))
        store.conn.commit()

    return {"property_id": pid, "source_listing_id": sid, "site": site,
            "listing_id": str(listing_id)}


def _linked_current_match_count(store):
    return store.conn.execute(
        "SELECT COUNT(*) FROM evaluations WHERE is_current=1 AND outcome='match' "
        "AND property_id IS NOT NULL").fetchone()[0]


class _SelectOnlyConn:
    """A connection proxy that fails the test on any statement that is not a read.

    Section 8's audit is read-only, so every statement it issues must start with
    SELECT, WITH or PRAGMA. Anything else - INSERT/UPDATE/DELETE/CREATE/COMMIT -
    fails here instead of quietly mutating the database, and any other use of the
    connection (commit, rollback, cursor, executescript) fails too.
    """

    def __init__(self, conn):
        self._conn = conn
        self.statements = []

    def execute(self, sql, *args, **kwargs):
        statement = " ".join(str(sql).split())
        self.statements.append(statement)
        keyword = statement.split(" ", 1)[0].upper() if statement else ""
        if keyword not in READ_ONLY_PREFIXES:
            raise AssertionError(f"audit issued a non-read statement: {statement}")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        raise AssertionError(f"audit used conn.{name}")


# --- the result object (plan duty 1) ------------------------------------------

def test_result_object_carries_the_pinned_keys_and_types(temp_db):
    result = temp_db.audit_integrity()
    assert set(result) == AUDIT_KEYS
    for block in (result["sources"], result["evaluations"]):
        assert {"total", "linked", "unlinked"}.issubset(block)
        assert all(isinstance(block[key], int)
                   for key in ("total", "linked", "unlinked"))
    assert isinstance(result["sources"]["by_site"], dict)
    assert isinstance(result["sources"]["by_lifecycle"], dict)
    assert isinstance(result["evaluations"]["by_outcome"], dict)
    for key in ("null_linked_current_matches", "grouped_omissions",
                "abnormal_omissions", "eligible_invariant_failures",
                "forbidden_in_matches", "explicit_gone_in_matches",
                "invalid_preference_values", "demographic_fact_conflicts",
                "duplicate_dataset_placement"):
        assert isinstance(result[key], int), key
    assert isinstance(result["review_by_reason"], dict)
    assert result["critical"] == []
    assert result["ok"] is True


def test_the_audit_vocabulary_is_the_config_vocabulary():
    assert store_mod.ELIGIBLE_TRUST_OUTCOME in config.TRUST_OUTCOMES
    assert store_mod.ELIGIBLE_UNIT_TYPE_STATE in config.TRUST_UNIT_TYPE_STATES
    assert (store_mod.ELIGIBLE_TENANCY_CHANNEL_STATE
            in config.TRUST_TENANCY_CHANNEL_STATES)
    assert (store_mod.ELIGIBLE_BUILDING_FORM_STATE
            in config.TRUST_BUILDING_FORM_STATES)
    assert all(state in config.TRUST_UNIT_TYPE_STATES
               for state in store_mod.FORBIDDEN_UNIT_TYPE_STATES)
    assert all(state in config.TRUST_TENANCY_CHANNEL_STATES
               for state in store_mod.FORBIDDEN_TENANCY_CHANNEL_STATES)
    assert store_mod.AUDIT_CRITICAL_INVARIANTS == CRITICAL_INVARIANTS


def test_source_counts_by_site_lifecycle_and_link_state(temp_db):
    _seed_source(temp_db, "site-a", "A1", lifecycle="active")
    _seed_source(temp_db, "site-a", "A2", lifecycle="missing", property_id=False)
    _seed_source(temp_db, "site-b", "B1", lifecycle="expired", property_id=False)

    sources = temp_db.audit_integrity()["sources"]

    assert (sources["total"], sources["linked"], sources["unlinked"]) == (3, 1, 2)
    assert sources["by_site"]["site-a"] == {"total": 2, "linked": 1, "unlinked": 1}
    assert sources["by_site"]["site-b"] == {"total": 1, "linked": 0, "unlinked": 1}
    assert sources["by_lifecycle"]["active"] == {"total": 1, "linked": 1,
                                                 "unlinked": 0}
    assert sources["by_lifecycle"]["missing"] == {"total": 1, "linked": 0,
                                                  "unlinked": 1}
    assert sources["by_lifecycle"]["expired"] == {"total": 1, "linked": 0,
                                                  "unlinked": 1}


def test_current_evaluation_counts_by_outcome_and_link_state(temp_db):
    _seed_source(temp_db, "site-a", "M1", outcome="match",
                 verdict=_eligible_verdict())
    _seed_source(temp_db, "site-a", "M2", outcome="match", property_id=False,
                 verdict=_eligible_verdict())
    _seed_source(temp_db, "site-a", "Q1", outcome="quarantined",
                 quarantine_reasons=["unverified lease duration"])
    _seed_source(temp_db, "site-a", "R1", outcome="rejected", property_id=False,
                 quarantine_reasons=["rent above cap"])
    # A retired evaluation is not a current one and must not be counted.
    retired = _seed_source(temp_db, "site-a", "X1", outcome="quarantined",
                           quarantine_reasons=["stale verdict"])
    _seed_source(temp_db, "site-a", "X1", outcome="rejected",
                 property_id=retired["property_id"],
                 quarantine_reasons=["rent above cap"])

    evaluations = temp_db.audit_integrity()["evaluations"]

    assert (evaluations["total"], evaluations["linked"],
            evaluations["unlinked"]) == (5, 3, 2)
    assert evaluations["by_outcome"]["match"] == {"total": 2, "linked": 1,
                                                  "unlinked": 1}
    assert evaluations["by_outcome"]["quarantined"] == {"total": 1, "linked": 1,
                                                        "unlinked": 0}
    assert evaluations["by_outcome"]["rejected"] == {"total": 2, "linked": 1,
                                                     "unlinked": 1}


# --- the critical invariants and the exit status (plan duty 2) ----------------

def test_null_linked_current_match_evaluation_is_critical(temp_db):
    _seed_source(temp_db, "site-a", "N1", outcome="match", property_id=False,
                 verdict=_eligible_verdict())

    result = temp_db.audit_integrity()

    assert result["null_linked_current_matches"] == 1
    assert result["critical"] == ["null_linked_current_matches"]
    assert result["ok"] is False


@pytest.mark.parametrize("states", [
    {"unit_type_state": "room_rental"},
    {"tenancy_channel_state": "sublet"},
])
def test_room_sublet_and_private_rows_in_matches_are_critical(temp_db, states):
    over = {"trust_outcome": store_mod.ELIGIBLE_TRUST_OUTCOME}
    over.update(states)
    _seed_source(temp_db, "site-a", "F1", outcome="match", match_row=True,
                 verdict=_eligible_verdict(**over))

    result = temp_db.audit_integrity()

    assert result["forbidden_in_matches"] == 1
    assert "forbidden_in_matches" in result["critical"]
    assert result["ok"] is False


def test_a_match_row_without_an_eligible_verdict_is_forbidden(temp_db):
    # v3.11 (spec 10): a review verdict may now hold a match row; only a sublet
    # or a room rental is forbidden.
    _seed_source(temp_db, "site-a", "F2", outcome="match", match_row=True,
                 verdict=_Verdict(trust_outcome="reject",
                                  tenancy_channel_state="sublet"))

    result = temp_db.audit_integrity()

    assert result["forbidden_in_matches"] == 1
    assert result["critical"] == ["forbidden_in_matches"]


def test_normal_building_consolidation_is_reported_and_is_not_an_error(temp_db):
    first = _seed_source(temp_db, "site-a", "G1", outcome="match", match_row=True,
                         verdict=_eligible_verdict())
    for listing_id in ("G2", "G3"):
        _seed_source(temp_db, "site-a", listing_id, outcome="match",
                     property_id=first["property_id"],
                     verdict=_eligible_verdict())

    result = temp_db.audit_integrity()

    assert result["grouped_omissions"] == 2
    assert result["abnormal_omissions"] == 0
    assert result["forbidden_in_matches"] == 0
    assert result["critical"] == []
    assert result["ok"] is True


def test_linked_match_evaluations_with_no_match_row_are_reported_not_critical(
        temp_db):
    first = _seed_source(temp_db, "site-a", "A1", outcome="match",
                         verdict=_eligible_verdict())
    _seed_source(temp_db, "site-a", "A2", outcome="match",
                 property_id=first["property_id"], verdict=_eligible_verdict())

    result = temp_db.audit_integrity()

    assert result["abnormal_omissions"] == 2
    assert result["grouped_omissions"] == 0
    assert result["critical"] == []
    assert result["ok"] is True


def test_the_three_buckets_partition_the_linked_current_match_evaluations(
        temp_db):
    consolidated = _seed_source(temp_db, "site-a", "C1", outcome="match",
                                match_row=True, verdict=_eligible_verdict())
    for listing_id in ("C2", "C3"):
        _seed_source(temp_db, "site-a", listing_id, outcome="match",
                     property_id=consolidated["property_id"],
                     verdict=_eligible_verdict())
    orphan = _seed_source(temp_db, "site-a", "O1", outcome="match",
                          verdict=_eligible_verdict())
    _seed_source(temp_db, "site-a", "O2", outcome="match",
                 property_id=orphan["property_id"], verdict=_eligible_verdict())
    _seed_source(temp_db, "site-a", "N1", outcome="match", property_id=False,
                 verdict=_eligible_verdict())

    result = temp_db.audit_integrity()

    represented = len({r[0] for r in temp_db.conn.execute(
        "SELECT property_id FROM matches").fetchall()})
    assert _linked_current_match_count(temp_db) == 5
    assert (represented + result["grouped_omissions"]
            + result["abnormal_omissions"]) == 5
    assert result["grouped_omissions"] == 2
    assert result["abnormal_omissions"] == 2


# --- the eligible trust invariant (section 11) --------------------------------

def test_eligible_row_with_the_positive_evidence_triple_passes(temp_db):
    _seed_source(temp_db, "site-a", "E1", outcome="match", match_row=True,
                 verdict=_eligible_verdict())

    result = temp_db.audit_integrity()

    assert result["eligible_invariant_failures"] == 0
    assert result["critical"] == []
    assert result["ok"] is True


@pytest.mark.parametrize("states,expected", [
    # Spec 08 section 6.2's own two halves: a unit type other than
    # `self_contained_apartment` is explicit rejection evidence, while a missing
    # landlord or building verdict is exactly what this release stopped holding a
    # listing for (section 9 case 7). The v3.6 positive-evidence triple is gone,
    # so only the first two combinations are still failures.
    ({"unit_type_state": None}, 1),
    ({"unit_type_state": "unknown"}, 1),
    ({"tenancy_channel_state": None}, 0),
    ({"tenancy_channel_state": "unknown"}, 0),
    ({"building_form_state": None}, 0),
    ({"building_form_state": "unknown"}, 0),
])
def test_eligible_row_without_positive_evidence_fails_for_the_unit_type_only(
        temp_db, states, expected):
    over = {"trust_outcome": store_mod.ELIGIBLE_TRUST_OUTCOME}
    over.update(states)
    _seed_source(temp_db, "site-a", "E2", outcome="match", match_row=True,
                 verdict=_eligible_verdict(**over))

    result = temp_db.audit_integrity()

    assert result["eligible_invariant_failures"] == expected, states
    assert ("eligible_invariant_failures" in result["critical"]) is bool(expected)
    assert result["ok"] is (expected == 0)


def test_the_critical_list_keeps_the_pinned_order_and_ok_negates_it(temp_db):
    _seed_source(temp_db, "site-a", "N1", outcome="match", property_id=False,
                 verdict=_eligible_verdict())
    _seed_source(temp_db, "site-a", "F1", outcome="match", match_row=True,
                 verdict=_eligible_verdict(unit_type_state="room_rental"))
    _seed_source(temp_db, "site-a", "E1", outcome="match", match_row=True,
                 verdict=_eligible_verdict(tenancy_channel_state="sublet"))

    result = temp_db.audit_integrity()

    assert result["critical"] == ["null_linked_current_matches", "forbidden_in_matches", "eligible_invariant_failures"]
    assert result["critical"] == [k for k in CRITICAL_INVARIANTS if result[k] > 0]
    assert result["ok"] is False
    assert result["null_linked_current_matches"] == 1
    # An `eligible` room row breaks TWO invariants at once, and so does the
    # sublet row: each is a forbidden row in `matches` AND carries explicit
    # rejection evidence. The audit reports both rather than picking one.
    assert result["forbidden_in_matches"] == 2
    assert result["eligible_invariant_failures"] == 2


# --- review counts by reason --------------------------------------------------

def test_review_counts_group_by_primary_reason_and_total_the_review_view(temp_db):
    _seed_source(temp_db, "site-a", "Q1", outcome="quarantined",
                 quarantine_reasons=[config.TRUST_REASON_UNIT_UNVERIFIED])
    _seed_source(temp_db, "site-a", "Q2", outcome="quarantined",
                 quarantine_reasons=["unverified lease duration"])
    _seed_source(temp_db, "site-a", "R1", outcome="rejected",
                 quarantine_reasons=["rent above cap"])
    _seed_source(temp_db, "site-a", "O1", outcome="match",
                 verdict=_eligible_verdict())

    result = temp_db.audit_integrity()

    assert result["review_by_reason"] == {
        config.TRUST_REASON_UNIT_UNVERIFIED: 1,
        "property identity unresolved": 1,
        "unverified lease duration": 1,
    }
    assert sum(result["review_by_reason"].values()) == 3
    assert (sum(result["review_by_reason"].values())
            == temp_db.view_counts()["needs_verification"])


def test_a_review_row_with_no_recorded_reason_is_bucketed_as_unspecified(temp_db):
    _seed_source(temp_db, "site-a", "Q1", outcome="quarantined")

    result = temp_db.audit_integrity()

    # The bucket name is part of the JSON contract, so it is pinned literally;
    # the exported constant has to carry that same name.
    assert store_mod.AUDIT_UNSPECIFIED == "unspecified"
    assert result["review_by_reason"] == {"unspecified": 1}


# --- the human rendering (plan duty 2) ---------------------------------------

def test_human_output_carries_every_section_8_fact(temp_db):
    consolidated = _seed_source(temp_db, "site-a", "C1", outcome="match",
                                match_row=True, verdict=_eligible_verdict())
    _seed_source(temp_db, "site-a", "C2", outcome="match",
                 property_id=consolidated["property_id"],
                 verdict=_eligible_verdict())
    _seed_source(temp_db, "site-a", "O1", outcome="match", property_id=False,
                 verdict=_eligible_verdict())
    _seed_source(temp_db, "site-b", "Q1", outcome="quarantined",
                 quarantine_reasons=[config.TRUST_REASON_UNIT_UNVERIFIED])

    text = run._format_integrity_audit(temp_db.audit_integrity(), "audit-copy.db")

    for expected in (
            "BoligWatch integrity audit (spec 07 section 8)",
            "Database: audit-copy.db",
            "Sources (source_listings): 4 total - 3 linked, 1 unlinked",
            "by site:",
            "by lifecycle:",
            "Current evaluations (is_current=1): 4 total - 3 linked, 1 unlinked",
            "by outcome:",
            "current match evaluations with no property link: 1",
            "linked current match evaluations folded by normal building "
            "consolidation: 1",
            "spec 07 section 8: normal building consolidation is reported, "
            "not an error",
            "linked current match evaluations not represented for any other "
            "reason: 0",
            "eligible evaluations with explicit rejection evidence: 0",
            "room / sublet / private rows present in matches: 0",
            "Needs verification rows by section 7.1 primary reason",
            config.TRUST_REASON_UNIT_UNVERIFIED,
            "property identity unresolved",
            "total",
    ):
        assert expected in text, expected
    assert "Result: 1 critical invariant violation(s): " \
           "null_linked_current_matches" in text


def test_human_output_renders_an_empty_database(temp_db):
    text = run._format_integrity_audit(temp_db.audit_integrity(), "empty.db")

    assert "Sources (source_listings): 0 total - 0 linked, 0 unlinked" in text
    # Four empty cross-tabs: by site, by lifecycle, by outcome and the reasons.
    assert text.count("(none)") == 4
    assert "Result: no critical invariant violated." in text


def test_human_output_has_no_emoji(temp_db):
    text = run._format_integrity_audit(temp_db.audit_integrity(), "empty.db")
    assert not any("\U0001F300" <= ch <= "\U0001FAFF" for ch in text)


# --- the command, its output contract and its exit status ---------------------

def test_clean_audit_exits_zero_and_prints_only_the_stable_json(
        temp_db, monkeypatch, capsys, fail_on_network):
    first = _seed_source(temp_db, "site-a", "C1", outcome="match",
                         match_row=True, verdict=_eligible_verdict())
    _seed_source(temp_db, "site-a", "C2", outcome="match",
                 property_id=first["property_id"], verdict=_eligible_verdict())
    monkeypatch.setattr(sys, "argv", ["run.py", "--audit-integrity", "--json",
                                      "--db", temp_db.db_path])

    run.main()

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert out == json.dumps(payload, sort_keys=True, ensure_ascii=False,
                             indent=1) + "\n"
    assert payload["ok"] is True
    assert payload["critical"] == []
    assert payload["grouped_omissions"] == 1


def test_critical_audit_exits_two_and_still_prints_the_report(
        temp_db, monkeypatch, capsys, fail_on_network):
    _seed_source(temp_db, "site-a", "N1", outcome="match", property_id=False,
                 verdict=_eligible_verdict())
    monkeypatch.setattr(sys, "argv", ["run.py", "--audit-integrity", "--db",
                                      temp_db.db_path])

    with pytest.raises(SystemExit) as exc:
        run.main()

    assert exc.value.code == 2
    assert "critical invariant violation(s): null_linked_current_matches" \
           in capsys.readouterr().out


def test_critical_audit_exits_two_under_json_too(temp_db, monkeypatch, capsys,
                                                 fail_on_network):
    _seed_source(temp_db, "site-a", "F1", outcome="match", match_row=True,
                 verdict=_eligible_verdict(unit_type_state="room_rental"))
    monkeypatch.setattr(sys, "argv", ["run.py", "--audit-integrity", "--json",
                                      "--db", temp_db.db_path])

    with pytest.raises(SystemExit) as exc:
        run.main()

    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_the_audit_makes_no_network_call_and_sends_no_alert(
        alert_env, monkeypatch, capsys, fail_on_network):
    store, sent, _stations = alert_env
    _seed_source(store, "site-a", "C1", outcome="match", match_row=True,
                 verdict=_eligible_verdict())
    monkeypatch.setattr(sys, "argv", ["run.py", "--audit-integrity", "--json",
                                      "--db", store.db_path])

    with run._NoNetwork():
        run.main()

    assert sent == []
    assert json.loads(capsys.readouterr().out)["ok"] is True


class _RecordingStore(Store):
    """A Store that records which database path it was constructed with."""

    opened = []

    def __init__(self, db_path=None):
        self.opened.append(db_path if db_path is not None else config.DB_PATH)
        super().__init__(db_path)


def test_audit_with_db_never_opens_the_production_database(
        temp_db, monkeypatch, capsys, fail_on_network):
    _RecordingStore.opened = []
    monkeypatch.setattr(run, "Store", _RecordingStore)
    monkeypatch.setattr(config, "DB_PATH", "/nonexistent/boligwatch.db")
    monkeypatch.setattr(sys, "argv", ["run.py", "--audit-integrity", "--json",
                                      "--db", temp_db.db_path])

    run.main()

    assert _RecordingStore.opened == [temp_db.db_path]


def test_audit_defaults_to_the_configured_database(
        temp_db, monkeypatch, capsys, fail_on_network):
    _RecordingStore.opened = []
    monkeypatch.setattr(run, "Store", _RecordingStore)
    monkeypatch.setattr(config, "DB_PATH", temp_db.db_path)
    monkeypatch.setattr(sys, "argv", ["run.py", "--audit-integrity", "--json"])

    run.main()

    assert _RecordingStore.opened == [temp_db.db_path]


# --- strictly read-only -------------------------------------------------------

def test_the_audit_issues_only_read_statements_and_opens_no_write_transaction(
        temp_db, monkeypatch):
    first = _seed_source(temp_db, "site-a", "C1", outcome="match",
                         match_row=True, verdict=_eligible_verdict())
    _seed_source(temp_db, "site-a", "C2", outcome="match",
                 property_id=first["property_id"], verdict=_eligible_verdict())
    _seed_source(temp_db, "site-a", "N1", outcome="match", property_id=False,
                 verdict=_eligible_verdict())
    _seed_source(temp_db, "site-a", "Q1", outcome="quarantined",
                 quarantine_reasons=[config.TRUST_REASON_UNIT_UNVERIFIED])
    guard = _SelectOnlyConn(temp_db.conn)
    monkeypatch.setattr(temp_db, "conn", guard)

    result = temp_db.audit_integrity()

    assert result["null_linked_current_matches"] == 1
    assert guard.statements
    for statement in guard.statements:
        assert statement.split(" ", 1)[0].upper() in READ_ONLY_PREFIXES, statement
    assert guard._conn.in_transaction is False
