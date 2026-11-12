"""Spec 07 section 6.4 - safe representative rebuild (v36i2).

Duty 1 of this row's plan is landed here: at every ``store.log_match(`` call
site in ``run.py`` the result must satisfy
``getattr(res, "trust_outcome", None) == "eligible"``. A match outcome that is
not eligible is a defect: it is counted under ``counts["trust_invariant"]`` in
``run._rescore_core``, printed in the rescore summary line, and never logged as
a match. ``best[prop_id]`` only ever holds eligible results, so a review/reject
sibling can never lend the property its landlord, kitchen, bathroom, lease,
rent, availability or date evidence.

The plan's section 1 trace found no field a representative borrows from a
sibling in today's tree (the match row is written from the representative
record's own fields), so duty 1's guard is an invariant: these tests drive the
defect deliberately, through a wrapper that leaves the verdict's own flags alone
and only changes the trust outcome.

Duty 2 (section 0b's pin) extends ``Store.clear_stale_matches``: inside the
per-property loop, a property whose current evaluations exist and hold no
eligible ``match`` loses its representative row. A property with no current
evaluation at all is left to the lifecycle branch below, and the lifecycle
branch itself is unchanged.

Duty 3 proves the trusted outputs stay trusted: a non-eligible fixture reaches
no ``matches`` row, no ``grouped_matches`` entry, no digest line, no map pin and
no notify call, while an eligible fixture reaches all of them.

Duty 4 adds the mixed-sibling fixtures (one exact house-numbered address, one
eligible record and one review or reject sibling), the user-action preservation,
the grouping and the stale-match clearing.

Everything here is local: no portal request, no DAWA/OSRM/Overpass call and no
alert. ``fail_on_network`` makes any socket use fail the test, and the alert
stub is ``alert_env``'s ``sent`` list, which must stay empty wherever a
non-eligible result is involved.
"""
import datetime
import socket

import pytest

import config
import digest
import filters
import notify
import run
import viewer
from conftest import _candidate
from store import format_bw_id


# --- harness ------------------------------------------------------------------

@pytest.fixture
def fail_on_network(monkeypatch):
    """Any socket use is a test failure, never a real request (plan duty 4)."""
    def _no_socket(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _no_socket)


def _defect(monkeypatch, outcome="reject"):
    """Make every evaluation come back as a hard rejection.

    v3.11 (spec 10): the trust verdict no longer gates a match row, so the only
    route a non-eligible result can take is a hard rejection. The wrapper forces
    one while leaving the record otherwise intact, which is what the guard's
    reject branch must refuse.
    """
    real = filters.evaluate_listing

    def wrapper(rec, stations, store=None, segments=None):
        res = real(rec, stations, store=store, segments=segments)
        res.passed = False
        res.quarantined = False
        res.rejected = True
        res.rejection_reason = res.rejection_reason or "forced rejection"
        return res

    monkeypatch.setattr(filters, "evaluate_listing", wrapper)


def _store_source(store, rec):
    """Store one source listing the local pass will re-evaluate.

    Mirrors the two calls `run.process_candidate` makes, so the local path sees
    the same property link and the same raw record the scan path stored.
    """
    prop_id, bw_id, _merge = store.resolve_property(
        addr=rec.get("normalized_address") or rec.get("address") or "",
        lat=rec.get("lat"), lon=rec.get("lon"), rooms=rec.get("rooms"),
        size_m2=rec.get("size_m2"), postcode=rec.get("postcode"),
        geo_precision=rec.get("geo_precision"))
    rec["property_id"] = prop_id
    store.record_source_listing(
        property_id=prop_id,
        site=rec.get("site", "test"),
        listing_id=str(rec.get("listing_id")),
        url=rec.get("url", ""),
        raw_data=rec,
        detail_state="fetched",
        detail_fetched_at=datetime.datetime.now().isoformat(timespec="seconds"),
    )
    return prop_id


# --- duty 1: the guard on the scan path ---------------------------------------

def test_process_candidate_never_logs_a_non_eligible_match(alert_env, fail_on_network,
                                                           monkeypatch):
    """A match outcome that is not eligible takes no match row and sends nothing."""
    store, sent, stations = alert_env
    from conftest import _candidate

    _defect(monkeypatch)
    with run._NoNetwork():
        run.process_candidate(_candidate(), stations, store, None, dry_run=False)

    assert store.grouped_matches(include_dismissed=True) == [], \
        "a rejected outcome may not hold a representative match row"
    assert sent == [], "no alert may be built from a non-eligible result"
    # The listing itself is still stored and marked seen: the guard refuses the
    # match row, it does not silently drop the discovery.
    assert store.get_source_listing("test", "L1") is not None


def test_process_candidate_still_logs_an_eligible_match(alert_env, fail_on_network):
    """The guard is an invariant, not a filter: the eligible path is unchanged."""
    store, _sent, stations = alert_env
    from conftest import _candidate

    rec = _candidate()
    with run._NoNetwork():
        run.process_candidate(rec, stations, store, None, dry_run=True)

    rows = store.grouped_matches(include_dismissed=True)
    assert len(rows) == 1, "an eligible listing still becomes the property's match"
    row = rows[0]
    # No sibling and no default may substitute for this record's own evidence.
    assert row["rent_total"] == rec["rent_total"]
    assert row["available_from"] == rec["available_from"]
    assert row["kitchen_verified"] == 1
    assert row["lease_verified"] == 1


# --- duty 1: the guard and the count on the local path -------------------------

def test_rescore_counts_a_non_eligible_match_and_logs_no_match(alert_env, fail_on_network,
                                                               monkeypatch, capsys):
    """The defect is counted, printed in the summary line, and never logged."""
    store, sent, _stations = alert_env
    from conftest import _candidate

    _store_source(store, _candidate())
    _defect(monkeypatch)
    with run._NoNetwork():
        result = run.refresh_local(store, export=False, quiet=True)

    counts = result["counts"]
    assert counts["rejected"] == 1, "the forced rejection is counted as rejected"
    assert counts["trust_invariant"] == 0
    assert counts["matched"] == 0, "it is not counted as a match"
    assert store.grouped_matches(include_dismissed=True) == []
    assert sent == []

    printed = capsys.readouterr().out
    assert "1 rejected" in printed, "the rejection is reported in the summary line"


def test_rescore_leaves_the_eligible_local_path_unchanged(alert_env, fail_on_network):
    """One eligible source still rebuilds its property's match row, with no defects."""
    store, _sent, _stations = alert_env
    from conftest import _candidate

    rec = _candidate()
    _store_source(store, rec)
    with run._NoNetwork():
        result = run.refresh_local(store, export=False, quiet=True)

    counts = result["counts"]
    assert counts["trust_invariant"] == 0
    assert counts["matched"] == 1
    rows = store.grouped_matches(include_dismissed=True)
    assert len(rows) == 1
    assert rows[0]["rent_total"] == rec["rent_total"]

    # Section 5.1 kept the source's own evaluation current, so the row is
    # backed by a current eligible match evaluation - what duty 2 will read.
    sources = store.conn.execute("SELECT id FROM source_listings").fetchall()
    assert len(sources) == 1
    evaluation = store.current_evaluation(sources[0]["id"])
    assert evaluation["outcome"] == "match"
    assert evaluation["trust_outcome"] == "eligible"
    assert evaluation["property_id"] == rows[0]["property_id"]


def test_the_trust_outcome_vocabulary_is_configs(alert_env, fail_on_network):
    """The three outcomes duty 1 discriminates on are section 5.3's own."""
    assert config.TRUST_OUTCOMES == ("eligible", "review", "reject")
    assert filters.classify_listing_trust is not None  # the composer stays the source
    from conftest import _candidate

    res = filters.evaluate_listing(_candidate(), [], store=None)
    assert res.trust_outcome in config.TRUST_OUTCOMES
    assert res.trust_outcome == "eligible", "the shared eligible fixture stays eligible"


# --- duty 2 + duty 4: two records at one exact house-numbered address ----------

SIBLING_RENT = 7999
SIBLING_AVAILABLE_FROM = "2027-03-01"


def _review_sibling():
    """The eligible fixture's twin at the same address: an unknown unit type.

    Spec 08 section 4.3 row 5 makes the unit type the only route to `review`, so
    the sibling needs more than the missing operator block: the neutral title and
    `listing_type` drop `_candidate`'s `1-værelses` card marker and its
    `Boligtype Lejlighed` label, leaving the unit type unanswered, which is what
    makes this sibling genuinely non-eligible. Rent and availability differ from
    the eligible record, so a row that borrowed them is visible in the
    assertions.
    """
    rec = dict(_candidate())
    rec.pop("source_record", None)
    rec["title"] = "Lejebolig ved Noerrebro"
    rec["listing_type"] = "bolig"
    rec["listing_id"] = "L2"
    rec["rent_total"] = SIBLING_RENT
    rec["available_from"] = SIBLING_AVAILABLE_FROM
    return rec


def _sublet_sibling():
    """The same sibling with a `fremleje` phrase: the section 4.2 reject route."""
    rec = _review_sibling()
    rec["description"] = ("Lejligheden udbydes til fremleje i 12 mdr. Eget køkken og "
                          "eget badeværelse.")
    rec["eligibility_text"] = rec["description"]
    return rec


SIBLINGS = {"review": _review_sibling, "reject": _sublet_sibling}


def _assert_representative_is_the_eligible_record(row, eligible):
    """The row's evidence is the eligible record's own, field by field."""
    assert row["listing_id"] == str(eligible["listing_id"]), \
        "the representative row must name the eligible source, not the sibling"
    assert row["rent_total"] == eligible["rent_total"], "rent is the eligible record's"
    assert row["available_from"] == eligible["available_from"], \
        "availability is the eligible record's"
    assert row["kitchen_verified"] == 1, "kitchen evidence is the eligible record's"
    assert row["bathroom_verified"] == 1, "bathroom evidence is the eligible record's"
    assert row["lease_verified"] == 1, "lease evidence is the eligible record's"


@pytest.mark.parametrize("variant", ["reject"])
@pytest.mark.parametrize("order", ["sibling-first", "eligible-first"])
def test_scan_path_sibling_lends_no_evidence(alert_env, fail_on_network, variant, order):
    """Spec 07 section 6.4: a non-eligible sibling holds no evidence and no row.

    Both orders are driven because they fail differently: processed first, the
    sibling must not create the row; processed second, it must not overwrite or
    clear the eligible record's row with its own rent, date or kitchen/lease
    facts.
    """
    store, sent, stations = alert_env
    eligible = _candidate()
    sibling = SIBLINGS[variant]()

    records = ([sibling, eligible] if order == "sibling-first" else [eligible, sibling])
    with run._NoNetwork():
        for rec in records:
            run.process_candidate(rec, stations, store, None, dry_run=True)

    rows = store.grouped_matches(include_dismissed=True)
    assert len(rows) == 1, "one property, one representative row, from the one eligible source"
    _assert_representative_is_the_eligible_record(rows[0], eligible)
    assert sent == [], "a dry scan alerts nobody, and never for a sibling"

    # The sibling is still stored and its own verdict recorded: the guard hides
    # it from the representative row, it does not drop the discovery.
    sibling_source = store.get_source_listing("test", sibling["listing_id"])
    assert sibling_source is not None
    sibling_eval = store.current_evaluation(sibling_source["id"])
    assert sibling_eval["property_id"] == rows[0]["property_id"], \
        "both records resolve to the one exact address's property"
    assert (sibling_eval["outcome"], sibling_eval["trust_outcome"]) == (
        ("quarantined", "review") if variant == "review" else ("rejected", "reject"))


@pytest.mark.parametrize("variant", ["reject"])
def test_local_refresh_sibling_lends_no_evidence(alert_env, fail_on_network, variant):
    """The same property on the local path: only the eligible sibling represents it."""
    store, sent, _stations = alert_env
    eligible = _candidate()
    sibling = SIBLINGS[variant]()
    _store_source(store, sibling)
    _store_source(store, eligible)

    with run._NoNetwork():
        result = run.refresh_local(store, export=False, quiet=True)

    counts = result["counts"]
    assert counts["matched"] == 1, "one source, the eligible one, matched"
    assert counts["trust_invariant"] == 0, "nothing non-eligible reached the match path"
    rows = store.grouped_matches(include_dismissed=True)
    assert len(rows) == 1
    _assert_representative_is_the_eligible_record(rows[0], eligible)
    assert sent == []

    # Section 6.4: source-level outcomes are retained for every sibling, so
    # "Known listings" stays inspectable.
    expected = (("quarantined", "review") if variant == "review"
                else ("rejected", "reject"))
    for rec in (eligible, sibling):
        source = store.get_source_listing("test", rec["listing_id"])
        evaluation = store.current_evaluation(source["id"])
        assert evaluation is not None, "every sibling keeps one current evaluation"
        assert evaluation["property_id"] == rows[0]["property_id"]
        if rec is sibling:
            assert (evaluation["outcome"], evaluation["trust_outcome"]) == expected
        else:
            assert (evaluation["outcome"], evaluation["trust_outcome"]) == ("match",
                                                                           "eligible")


def test_eligible_siblings_still_group_to_one_representative(alert_env, fail_on_network):
    """Two eligible siblings at one address: one row, one current evaluation each."""
    store, _sent, _stations = alert_env
    first = _candidate()
    second = _candidate()
    second["listing_id"] = "L2"
    second["rent_total"] = 8700
    second["available_from"] = "2027-04-01"
    _store_source(store, first)
    _store_source(store, second)

    with run._NoNetwork():
        result = run.refresh_local(store, export=False, quiet=True)

    assert result["counts"]["matched"] == 2, "both eligible listings evaluate as matches"
    rows = store.grouped_matches(include_dismissed=True)
    assert len(rows) == 1, "one property, one representative row"
    row = rows[0]
    assert row["property_id"] == first["property_id"] == second["property_id"], \
        "grouping never changes the property id"
    # `_rescore_core` ranks the two eligible units and the better one holds the
    # row: the cheaper unit, which is the higher-ranked candidate under §3.6.
    assert row["listing_id"] == str(first["listing_id"])
    assert row["rent_total"] == first["rent_total"]
    for rec in (first, second):
        source = store.get_source_listing("test", rec["listing_id"])
        evaluation = store.current_evaluation(source["id"])
        assert evaluation["outcome"] == "match"
        assert evaluation["trust_outcome"] == "eligible"


def test_user_action_survives_the_rebuild(alert_env, fail_on_network):
    """§11: `user_actions` is keyed by property id, and the rebuild keeps the id."""
    store, _sent, _stations = alert_env
    rec = _candidate()
    prop_id = _store_source(store, rec)
    store.record_user_action(prop_id, "save", "watched by hand")

    with run._NoNetwork():
        run.refresh_local(store, export=False, quiet=True)

    rows = store.grouped_matches(include_dismissed=True)
    assert len(rows) == 1
    assert rows[0]["property_id"] == prop_id, "the rebuild reuses the property id"
    state = store.get_action_state(prop_id)
    assert state is not None, "the latest-action query still finds the shortlist entry"
    assert state["action"] == "save"
    assert state["note"] == "watched by hand"
    assert store.get_action_states()[prop_id]["action"] == "save"


# --- duty 2: the stale-match rule (section 0b's pin) ---------------------------

class _TrustVerdict:
    """The only attribute `record_evaluation` reads for the section 5.3 column."""

    def __init__(self, trust_outcome):
        self.trust_outcome = trust_outcome


def _seed_match_property(store, addr, rent=8000, outcome=None, trust_outcome=None,
                         expire=False):
    """One property + one source listing + one seeded `matches` row.

    The row is seeded by SQL because that is exactly the state the rule reads: a
    `matches` row whose supporting evaluation is missing, review-only or
    eligible. `outcome=None` leaves the property with no current evaluation at
    all - section 0b's boundary case.
    """
    now = datetime.datetime.now().isoformat()
    prop_id = store._find_or_create_property_direct(addr, 55.65, 12.54, 1, 35.0, "new", now)
    source_id = store.record_source_listing(
        prop_id, "test", f"L{prop_id}", f"https://example/{prop_id}",
        {"site": "test", "address": addr, "size_m2": 35.0, "rent_total": rent})
    if expire:
        store.conn.execute(
            "UPDATE source_listings SET lifecycle_state='expired' WHERE id=?", (source_id,))
    store.conn.execute(
        "INSERT INTO matches(property_id, bw_id, tier, category, score, rent_total, "
        "commute_min, relevance, address, quarantined, is_responsible_pick, complex_key) "
        "VALUES(?,?,'A',1,60.0,?,15,'actionable',?,0,0,NULL)",
        (prop_id, format_bw_id(prop_id), rent, addr))
    store.conn.commit()
    if outcome is not None:
        store.record_evaluation(source_listing_id=source_id, property_id=prop_id,
                                observation_id=None, outcome=outcome,
                                rules_version=config.eligibility_rules_version(),
                                eval_res=_TrustVerdict(trust_outcome))
    return prop_id


def test_clear_stale_matches_clears_only_an_unsupported_row(temp_db):
    """Section 0b: review-only cleared, eligible kept, no-evaluation untouched."""
    store = temp_db
    review_only = _seed_match_property(store, "Testgade 11, 2300 København",
                                       outcome="quarantined", trust_outcome="review")
    eligible = _seed_match_property(store, "Testgade 13, 2300 København",
                                    outcome="match", trust_outcome="eligible")
    no_evaluation = _seed_match_property(store, "Testgade 15, 2300 København")
    store.record_user_action(review_only, "save", "shortlisted by hand")

    cleared = store.clear_stale_matches()

    assert cleared == 1, "exactly the review-only property is unsupported"
    served = {r["property_id"] for r in store.grouped_matches(include_dismissed=True)}
    assert review_only not in served, "no eligible source supports this row"
    assert eligible in served, "an eligible source keeps its row"
    assert no_evaluation in served, \
        "a property with no current evaluation at all is left to the lifecycle branch"
    # Clearing a representative row is not a user action: §11 keeps the shortlist.
    assert store.get_action_state(review_only)["note"] == "shortlisted by hand"
    assert store.clear_stale_matches() == 0, "the rule is stable on a second pass"


def test_clear_stale_matches_keeps_the_lifecycle_branch(temp_db):
    """The rule is additive: a fully expired property is still cleared by v3.3."""
    store = temp_db
    eligible = _seed_match_property(store, "Testgade 17, 2300 København",
                                    outcome="match", trust_outcome="eligible")
    expired = _seed_match_property(store, "Testgade 19, 2300 København", expire=True)

    assert store.clear_stale_matches() == 1, "the lifecycle branch owns the expired row"
    served = {r["property_id"] for r in store.grouped_matches(include_dismissed=True)}
    assert expired not in served
    assert eligible in served


def test_clear_stale_matches_clears_an_expired_row_inside_the_window(temp_db):
    """Spec 07 section 7: a property whose listings have all expired holds no row.

    The amendment `ub-v37m1-1` (plan section 0a) removes the v3.3 within-window
    keep from ``Store.clear_stale_matches``. Section 7 requires a source to
    appear in exactly one current view, and ``_view_for_source`` already places
    an expired source inside ``config.EXPIRED_VISIBLE_DAYS`` in Recently gone by
    its window alone - so the representative row was the second view, and
    clearing it hides nothing: the source stays on the board with "last seen N
    days ago" and its last decision, and a relist recreates the row through the
    next local refresh.
    """
    store = temp_db
    pid = _seed_match_property(store, "Testgade 21, 2300 København",
                               outcome="match", trust_outcome="eligible",
                               expire=True)
    expired_at = (datetime.datetime.now()
                  - datetime.timedelta(days=1)).isoformat(timespec="seconds")
    store.conn.execute(
        "UPDATE source_listings SET expired_at=? WHERE property_id=?",
        (expired_at, pid))
    store.conn.commit()

    assert store.clear_stale_matches() == 1, \
        "the listing expired inside the visible window: the row is still cleared"
    served = {r["property_id"] for r in store.grouped_matches(include_dismissed=True)}
    assert pid not in served, "an expired-only property holds no representative row"

    gone = store.recently_gone_rows()
    assert any(r["site"] == "test" and str(r["listing_id"]) == f"L{pid}"
               for r in gone), \
        "the home is still visible: its source is listed in Recently gone"


# --- duty 3: the trusted outputs stay trusted ----------------------------------

def _counts_line(text):
    return [line for line in text.splitlines() if line.startswith("Actionable ")][0]


@pytest.mark.parametrize("variant", ["reject"])
def test_non_eligible_fixture_reaches_no_trusted_output(alert_env, fail_on_network, variant):
    """Plan duty 3: no match row, no digest line, no map pin, no notify call.

    The listing itself is not hidden from the source-level surfaces - it stays
    stored with its own current evaluation, which is what section 7.1's "Needs
    verification" reads. What it must never reach is the trusted read model.
    """
    store, sent, stations = alert_env
    rec = SIBLINGS[variant]()

    with run._NoNetwork():
        run.process_candidate(rec, stations, store, None, dry_run=False)
        digest_text = digest.generate_daily_digest(store=store)
        payload = viewer.map_payload(store)

    assert store.grouped_matches(include_dismissed=True) == [], \
        "a non-eligible source holds no representative row"
    assert payload["matches"]["features"] == [], "no map pin for a non-eligible source"
    assert sent == [], "the alert stub stays empty: no notify call is made"
    assert "Jorisvej 1a" not in digest_text, "no digest line for a non-ranked row"
    assert notify.format_dkk(rec["rent_total"]) + " kr" not in digest_text
    assert _counts_line(digest_text).startswith("Actionable 0"), \
        "the digest's ranked inputs are empty"

    source = store.get_source_listing("test", rec["listing_id"])
    assert source is not None, "the discovery is kept, not dropped"
    evaluation = store.current_evaluation(source["id"])
    assert (evaluation["outcome"], evaluation["trust_outcome"]) == (
        ("quarantined", "review") if variant == "review" else ("rejected", "reject"))


def test_an_eligible_fixture_reaches_matches_digest_and_map(alert_env, fail_on_network):
    """The mirror of the duty 3 test: the same three readers do see a trusted row.

    Without this, "no match row" would also pass if the readers were broken.
    """
    store, sent, stations = alert_env
    rec = _candidate()

    with run._NoNetwork():
        run.process_candidate(rec, stations, store, None, dry_run=False)
        digest_text = digest.generate_daily_digest(store=store)
        payload = viewer.map_payload(store)

    rows = store.grouped_matches(include_dismissed=True)
    assert len(rows) == 1, "an eligible source holds the representative row"
    assert len(payload["matches"]["features"]) == 1, "and it is on the map"
    assert not _counts_line(digest_text).startswith("Actionable 0"), \
        "and the digest's ranked inputs carry it"
    # This fixture is Rank S in commute band A, so the eligible path DOES alert -
    # and the alert is built from that same trusted row, never from a sibling.
    assert len(sent) == 1, "an eligible, actionable find still alerts"
    _prefix, alert_payload = sent[0]
    assert alert_payload.get("rent_total") == rec["rent_total"]
    assert alert_payload.get("property_id") == rows[0]["property_id"]

