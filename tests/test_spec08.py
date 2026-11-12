"""Spec 08 section 9 - the reject-only trust gate, as the tests read it (v37c1).

This file carries spec 08 section 9's cases **1-8**. Cases 1-7 are v37c1's
(classifiers, suppression, the audit invariant); case 8 - "the dashboard shell's
default control reads `Ranked`; a row with unknown tenancy renders no badge and
no 'unverified' text" - is a `viewer.py` fact and is v37u1's, added here on top
of the seven.

Everything here is local and deterministic: no store is connected to
production, no network call is made and no alert is sent. `temp_db` and the
`mock_stations` / autouse place-feature fixtures come from `conftest.py`.
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile

import pytest
from bs4 import BeautifulSoup

import config
import filters
import geo
import viewer
from filters import classify_listing_trust, classify_tenancy_channel


# --- fixtures -----------------------------------------------------------------
def _base(**over):
    """One stored advert with no advertiser evidence and no building evidence."""
    rec = {
        "site": "boligportal",
        "listing_id": "S8-1",
        "url": "https://example.test/s8-1",
        "title": "1-værelses lejlighed",
        "address": "Jorisvej 1a, 2300 København",
        "normalized_address": "Jorisvej 1a, 2300 København",
        "postcode": "2300",
        "geo_precision": "street",
        "listing_type": "lejlighed",
        "card_class": "apartment",
        "rooms": 1,
        "size_m2": 32.0,
        "rent_total": 8300,
        "deposit": 20000,
        "prepaid_rent": 0,
        "available_from": "2027-01-20",
        "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "description": "Eget køkken og eget badeværelse. Ubegrænset lejeperiode.",
        "eligibility_text": ("Boligtype Lejlighed. Eget køkken og eget badeværelse. "
                             "Ubegrænset lejeperiode."),
        "eligibility_source_url": "https://example.test/s8-1",
        "eligibility_evidence_state": "verified",
        "eligibility_checked_at": "2026-09-15T10:00:00",
        "detail_state": "fetched",
        "lat": 55.65,
        "lon": 12.60,
    }
    rec.update(over)
    return rec


def _unknown_unit(**over):
    """A self-contained apartment whose unit type is itself unanswered.

    Under spec 08 section 4.3 row 5 an unknown unit type is the ONLY route to a
    `review` verdict, so this is the fixture the review cases need: no card
    marker in the title, no `Boligtype` label in the detail text and a neutral
    `listing_type`.
    """
    rec = _base(listing_id="S8-U", title="Annonce 5", listing_type="ukendt",
                card_class="unknown")
    rec["url"] = "https://example.test/s8-u"
    rec["eligibility_text"] = "Eget køkken og eget badeværelse. Ubegrænset lejeperiode."
    rec.update(over)
    return rec


def _own_landlord_block(rec, ad_id=5660253, num_rentables=220):
    """This advert's own BoligPortal landlord block, keyed on `ad.id`."""
    rec = dict(rec)
    rec["source_record"] = {"id": ad_id, "is_newbuild": False}
    rec["detail_hydration"] = {"props": {"page_props": {
        "ad": {"id": ad_id},
        "landlord_information": {"num_rentables": num_rentables},
    }}}
    return rec


@pytest.fixture(autouse=True)
def offline_metro(monkeypatch):
    """A deterministic metro at Rådhuspladsen, as the spec 07 fixtures have."""
    monkeypatch.setattr(geo, "nearest_metro",
                        lambda lat, lon, st: ("Rådhuspladsen", 300.0, 400.0, 5))
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda lat, lon, st: ("Rådhuspladsen", 300.0, 400.0, 5,
                                              "metro"))


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


def _seed_evaluation(store, listing_id, *, outcome="match", verdict):
    """One property + one source listing + its current evaluation."""
    pid, _bw, _status = store.resolve_property(
        addr=f"Spec8vej {listing_id}, 2300 København", lat=55.65, lon=12.60,
        rooms=1, size_m2=32.0, postcode="2300", geo_precision="street")
    sid = store.record_source_listing(
        pid, "test", str(listing_id), f"https://example.test/{listing_id}",
        {"site": "test", "listing_id": str(listing_id), "rent_total": 8300})
    store.record_evaluation(
        source_listing_id=sid, property_id=pid, observation_id=None,
        outcome=outcome, rules_version=config.eligibility_rules_version(),
        eval_res=verdict)
    return pid, sid


# --------------------------------------------------------------------------- #
# Case 1 - apartment + unknown advertiser + unknown building -> eligible        #
# --------------------------------------------------------------------------- #
def test_case_1_apartment_with_unknown_advertiser_and_building_is_eligible():
    """Spec 08 section 4.3 row 4 / section 9 case 1.

    Missing landlord and building evidence no longer holds a listing: the
    unknown advert is a pass on both evidence axes and the composed verdict is
    the one outcome that may rank.
    """
    rec = _base()
    decision = classify_listing_trust(rec, None)

    assert decision.outcome == "eligible"
    assert decision.reason is None
    assert decision.unit_type_state == "self_contained_apartment"
    assert decision.tenancy_channel_state == "unknown"
    assert decision.tenancy_channel.decision == "pass"
    assert decision.tenancy_channel.reason == "advertiser not identified"
    assert decision.building_form_state == "unknown"
    assert decision.building_form.decision == "pass"
    assert decision.building_form.reason == "building form not identified"
    # No badge: the badge predicate is a present professional verdict, and this
    # advert has neither (section 7).
    assert decision.tenancy_channel_state != "professional_direct"
    assert decision.building_form_state != "managed_multi_unit_block"


def test_case_1_an_unknown_apartment_still_traverses_the_gates(mock_stations):
    """Spec 08 section 11: the row continues through the existing gates."""
    res = filters.evaluate_listing(_base(), mock_stations)

    assert res.trust_outcome == "eligible"
    assert res.trust_reason is None
    assert res.passed is True
    assert res.quarantined is False and res.rejected is False


# --------------------------------------------------------------------------- #
# Case 2 - the hard rejections are untouched                                    #
# --------------------------------------------------------------------------- #
def test_case_2_a_room_still_rejects():
    """Spec 08 section 4.3 row 1 / section 9 case 2."""
    rec = _base(listing_id="S8-R", title="14m² værelse på Frederiksberg",
                listing_type="ukendt", card_class="room")
    rec["eligibility_text"] = "Boligtype Værelse. Eget bad og tekøkken."
    decision = classify_listing_trust(rec, None)

    assert decision.outcome == "reject"
    assert decision.reason == config.TRUST_REASON_ROOM
    assert decision.unit_type_state == "room_rental"


def test_case_2_fremleje_still_rejects_as_sublet():
    """Spec 08 section 4.3 row 2 / section 9 case 2."""
    rec = _base(listing_id="S8-S",
                description="Boligen udbydes som fremleje i seks måneder.",
                eligibility_text=("Boligtype Lejlighed. Boligen udbydes som "
                                  "fremleje i seks måneder."))
    decision = classify_listing_trust(rec, None)

    assert decision.outcome == "reject"
    assert decision.reason == "sublet"
    assert decision.tenancy_channel_state == "sublet"


def test_case_2_jeg_udlejer_min_lejlighed_still_rejects_as_private_landlord():
    """Spec 08 section 4.3 row 2 / section 9 case 2."""
    rec = _base(listing_id="S8-P",
                description="Jeg udlejer min lejlighed direkte til lejer.",
                eligibility_text=("Boligtype Lejlighed. Jeg udlejer min lejlighed "
                                  "direkte til lejer."))
    decision = classify_listing_trust(rec, None)

    assert decision.outcome == "reject"
    assert decision.reason == "private landlord"
    assert decision.tenancy_channel_state == "private_landlord"


# --------------------------------------------------------------------------- #
# Case 3 - the advert's own BoligPortal portfolio is badge evidence             #
# --------------------------------------------------------------------------- #
def test_case_3_own_portfolio_of_220_rentables_is_professional_direct():
    """Spec 08 section 4.1 route 3c / section 9 case 3."""
    rec = _own_landlord_block(_base(listing_id="S8-220"), num_rentables=220)
    decision = classify_tenancy_channel(rec)

    assert decision.state == "professional_direct"
    assert decision.decision == "pass"
    assert decision.source == "structured"
    assert decision.reason == "advertiser portfolio of 220 rentables"
    assert decision.excerpt == "BoligPortal advertiser portfolio: 220 rentables"
    # Badge evidence only: route 3c never names a landlord (section 12).
    assert decision.landlord_name is None


def test_case_3_two_rentables_is_unknown_and_the_row_is_still_eligible():
    """Spec 08 section 4.1 route 3c's threshold / section 9 case 3."""
    rec = _own_landlord_block(_base(listing_id="S8-2"), num_rentables=2)
    decision = classify_tenancy_channel(rec)

    assert decision.state == "unknown"
    assert decision.decision == "pass"
    assert decision.reason == "advertiser not identified"
    assert classify_listing_trust(rec, None).outcome == "eligible"


def test_case_3_the_threshold_is_ten_and_another_adverts_block_is_not_read():
    """Spec 08 section 4.1 route 3c and section 12's own-advert scope."""
    threshold = config.TENANCY_PORTFOLIO_MIN_RENTABLES
    at_threshold = _own_landlord_block(_base(listing_id="S8-T"),
                                       num_rentables=threshold)
    assert classify_tenancy_channel(at_threshold).state == "professional_direct"

    below = _own_landlord_block(_base(listing_id="S8-B"),
                                num_rentables=threshold - 1)
    assert classify_tenancy_channel(below).state == "unknown"

    # `page_props.ad.id` is another advert's id, so the block is not this
    # advert's evidence even though it sits on the same detail page.
    foreign = _base(listing_id="S8-F")
    foreign["source_record"] = {"id": 1, "is_newbuild": False}
    foreign["detail_hydration"] = {"props": {"page_props": {
        "ad": {"id": 999999},
        "landlord_information": {"num_rentables": 500},
    }}}
    assert classify_tenancy_channel(foreign).state == "unknown"


# --------------------------------------------------------------------------- #
# Case 4 - no portal is professional by identity after FindBolig was retired    #
# --------------------------------------------------------------------------- #
def test_case_4_no_portal_is_professional_by_identity():
    """v3.11 (spec 10 section 7): FindBolig was retired, so route 3d is empty."""
    assert config.TENANCY_PROFESSIONAL_PORTALS == ()
    decision = classify_tenancy_channel(_base(listing_id="S8-FB", site="findbolig"))

    assert decision.state == "unknown"
    assert decision.decision == "pass"


def test_case_4_a_site_outside_the_professional_portals_is_unknown():
    """The route is the pinned portal tuple, not "any portal" (section 4.1)."""
    assert "boligportal" not in config.TENANCY_PROFESSIONAL_PORTALS
    decision = classify_tenancy_channel(_base(listing_id="S8-BP"))
    assert decision.state == "unknown"


# --------------------------------------------------------------------------- #
# Case 5 - a review verdict never hides a gate rejection                        #
# --------------------------------------------------------------------------- #
def test_case_5_a_review_verdict_keeps_the_rent_rejection(mock_stations):
    """Spec 08 section 4.4 / section 9 case 5.

    The unit type is unanswered, so the trust verdict is `review`; the rent gate
    has already rejected the 13,900 kr advert, so the result stays rejected with
    the gate's own reason and the trust reason is listed beneath it.
    """
    rec = _unknown_unit(listing_id="S8-13900", rent_total=13900,
                        deposit=20000, prepaid_rent=0)
    assert classify_listing_trust(rec, None).outcome == "review"

    res = filters.evaluate_listing(rec, mock_stations)

    assert res.rejected is True
    assert res.quarantined is False
    assert res.passed is False
    assert res.rejection_reason == f"rent 13900 > {config.RENT_CAP_MAX}"
    assert res.trust_outcome == "review"
    assert res.trust_reason == config.TRUST_REASON_UNIT_UNVERIFIED


# --------------------------------------------------------------------------- #
# Case 6 - the alert backlog is suppressed once                                  #
# --------------------------------------------------------------------------- #
def _seed_match(store, listing_id, alerted_actionable):
    """One `matches` row with the alert flag this case varies."""
    pid, _bw, _status = store.resolve_property(
        addr=f"Backlogvej {listing_id}, 2300 København", lat=55.65,
        lon=12.60, rooms=1, size_m2=32.0, postcode="2300",
        geo_precision="street")
    store.conn.execute(
        "INSERT INTO matches(property_id, bw_id, tier, category, score, "
        "rent_total, commute_min, relevance, address, quarantined, "
        "is_responsible_pick, complex_key, alerted_actionable) "
        "VALUES(?,?,'A',1,80.0,8300,15,'actionable',?,0,0,NULL,?)",
        (pid, f"BW-9{listing_id}", f"Backlogvej {listing_id}, 2300 København",
         alerted_actionable))
    store.conn.commit()
    return pid


def test_case_6_suppress_alert_backlog_marks_every_unsuppressed_row(temp_db):
    """Spec 08 section 6.4 / section 9 case 6."""
    _seed_match(temp_db, "001", 0)
    _seed_match(temp_db, "002", None)
    _seed_match(temp_db, "003", 1)
    temp_db.conn.execute(
        "INSERT INTO user_actions(property_id, action, note, created_at) "
        "VALUES(1, 'shortlist', NULL, '2026-09-14T09:00:00')")
    temp_db.conn.commit()
    stamp = "2026-09-15T18:00:00"

    assert temp_db.suppress_alert_backlog(stamp) == 2

    rows = {row["bw_id"]: (row["alerted_actionable"], row["last_alert_at"])
            for row in temp_db.conn.execute(
                "SELECT bw_id, alerted_actionable, last_alert_at FROM matches")}
    assert rows["BW-9001"] == (1, stamp)
    assert rows["BW-9002"] == (1, stamp)
    assert rows["BW-9003"] == (1, None), "an already-alerted row is untouched"
    assert temp_db.conn.execute(
        "SELECT COUNT(*) FROM matches WHERE alerted_actionable=0 "
        "OR alerted_actionable IS NULL").fetchone()[0] == 0
    assert temp_db.conn.execute(
        "SELECT COUNT(*) FROM user_actions").fetchone()[0] == 1, \
        "the suppression never touches user_actions"


def test_case_6_the_second_call_returns_zero(temp_db):
    """Idempotent: a second call changes nothing (spec 08 section 9 case 6)."""
    _seed_match(temp_db, "011", 0)

    assert temp_db.suppress_alert_backlog("2026-09-15T18:00:00") == 1
    assert temp_db.suppress_alert_backlog("2026-09-15T18:05:00") == 0


# --------------------------------------------------------------------------- #
# Case 7 - the audit's eligibility invariant is reject-only evidence             #
# --------------------------------------------------------------------------- #
def test_case_7_an_eligible_row_with_unknown_tenancy_is_not_a_failure(temp_db):
    """Spec 08 section 6.2 / section 9 case 7."""
    _seed_evaluation(temp_db, "E-UNK", verdict=_Verdict(
        trust_outcome="eligible", unit_type_state="self_contained_apartment",
        tenancy_channel_state="unknown", building_form_state="unknown"))

    result = temp_db.audit_integrity()

    assert result["eligible_invariant_failures"] == 0
    assert "eligible_invariant_failures" not in result["critical"]


def test_case_7_an_eligible_row_with_a_sublet_tenancy_is_a_failure(temp_db):
    """Explicit rejection evidence is still an invariant failure."""
    _seed_evaluation(temp_db, "E-SUB", verdict=_Verdict(
        trust_outcome="eligible", unit_type_state="self_contained_apartment",
        tenancy_channel_state="sublet", building_form_state="unknown"))

    result = temp_db.audit_integrity()

    assert result["eligible_invariant_failures"] == 1
    assert "eligible_invariant_failures" in result["critical"]
    assert result["ok"] is False


# --------------------------------------------------------------------------- #
# Case 8 - the Ranked default control and a badge-only row (viewer.py, v37u1)    #
# --------------------------------------------------------------------------- #
# Spec 08 section 9 case 8: "Dashboard shell: default control reads `Ranked`; a
# row with unknown tenancy renders no badge and no 'unverified' text." Both
# halves are `viewer.py` facts: the shell is exported and parsed with bs4, and
# the row renderer is the page script's own `sourceFactRow`, run under node -
# the engine the dashboard uses for its script.
def _page_function(start, end):
    """One function body out of the page script, for a render assertion."""
    source = viewer.HTML_TEMPLATE
    position = source.index(start)
    return source[position:source.index(end, position)]


def _node(program):
    """Run one page-script program under node and read its JSON answer."""
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


CASE8_ROW_HARNESS = (
    _page_function("const kr = v =>", "function commuteText(")
    + _page_function("function availabilityText(", "function relevanceText(")
    + _page_function("const TENANCY_PROFESSIONAL_STATE",
                     "// Expired listings stay in local processing")
    + _page_function("function managementVerified(", "function sourceFactRow(")
    + _page_function("function sourceFactRow(", "const EMOJI_RE = "))


def test_case_8_the_default_control_reads_ranked(temp_db, tmp_path):
    """Case 8a: the shell's default control is `Ranked`, its keys unchanged."""
    path = tmp_path / "report.html"
    viewer.export_static_html(str(path), store=temp_db)
    doc = BeautifulSoup(path.read_text(encoding="utf-8"), "lxml")

    assert viewer.DASHBOARD_VIEW_LABELS[0] == ("trusted", "Ranked", "grouped")
    control = doc.select_one('[data-view="trusted"][data-count-key="trusted_blocks"]')
    assert control is not None, "the trusted control is in the shell"
    text = control.get_text(" ", strip=True)
    assert text.startswith("Ranked"), f"the default control reads {text!r}"
    assert control["data-scope"] == "grouped", "the scope word and key are unchanged"
    assert control["aria-pressed"] == "true", "Ranked is the view that is open"
    assert doc.find("html")["data-default-view"] == "trusted"

    # The shell ships no "unverified" text at all, apart from the filter arm that
    # names the housing-eligibility reason (section 7 of the release removes the
    # management/building twins and every place that rendered them).
    for element_node in doc.find_all(["script", "style"]):
        element_node.decompose()
    carriers = {element.parent.get_text(" ", strip=True)
                for element in doc.find_all(string=True) if "unverified" in element}
    assert carriers == {"Housing eligibility unverified"}, repr(carriers)


def test_case_8_an_unknown_tenancy_row_renders_no_badge():
    """Case 8b: an unknown tenancy renders no badge and no 'unverified' text.

    The row's own reason ("self-contained apartment unverified") is the reason
    it is not ranked, not a badge; the same renderer DOES show the badge for a
    row that carries the evidence, so the absence measures the predicate.
    """
    row = {
        "view": "needs_verification",
        "site": "test",
        "listing_id": "S8-C8",
        "address": "Jorisvej 1a, 2300 København",
        "rent_total": 8300,
        "outcome": "quarantined",
        "trust_outcome": "review",
        "tenancy_channel_state": "unknown",
        "building_form_state": "unknown",
        "property_identity_state": "resolved",
        "reasons": ["self-contained apartment unverified"],
        "reason_ranks": [1],
        "primary_reason": "self-contained apartment unverified",
    }
    rendered = _node(CASE8_ROW_HARNESS + """
const cases = %s;
const out = {};
for (const name of Object.keys(cases)) out[name] = sourceFactRow(cases[name], 'review');
console.log(JSON.stringify(out));
""" % json.dumps({
        "unknown": row,
        "professional": dict(row, tenancy_channel_state="professional_direct"),
    }))

    facts = rendered["unknown"]
    assert "source-management" not in facts and "source-building" not in facts, \
        "an unknown tenancy renders no badge at all"
    assert "source-unverified" not in facts
    assert "Management unverified" not in facts
    assert "Building type unverified" not in facts
    assert "unverified" not in facts.replace("self-contained apartment unverified", ""), \
        "the only 'unverified' word is the row's own reason text"
    assert "self-contained apartment unverified" in facts, \
        "the row still states the reason it is not ranked"

    assert "Professionally managed" in rendered["professional"], \
        "the same renderer shows the badge when the row carries evidence"


# --------------------------------------------------------------------------- #
# Contract - local only                                                         #
# --------------------------------------------------------------------------- #
def test_the_new_file_makes_no_network_call(monkeypatch):
    """Spec 08 section 12: no classifier reaches the network."""
    def _no_socket(*args, **kwargs):
        raise AssertionError("spec 08 classification must not touch the network")

    monkeypatch.setattr(socket, "socket", _no_socket)
    monkeypatch.setattr(socket, "create_connection", _no_socket)
    assert classify_listing_trust(_base(), None).outcome == "eligible"
    assert classify_tenancy_channel(_base(site="findbolig")).state == "unknown"
