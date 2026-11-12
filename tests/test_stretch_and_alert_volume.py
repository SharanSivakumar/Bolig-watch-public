"""v3.3: the studerende false positive, the hard 9,500 kr cap and the alert diet.

Every case here comes from real production data on 6 September 2026, when the
whole Viola Nørløvs Gade building was invisible: 37 listings were rejected by a
boilerplate paragraph and 52 more sat 50 to 350 kr over the tier ceiling.
v3.3 replaces the stretch band with the hard 9,500 kr cap and the Responsible
Pick rule.
"""
import datetime

import pytest

import config
import digest
import filters
import geo
import notify
import run
from store import format_bw_id
from conftest import with_detail_evidence

TODAY = datetime.date(2026, 9, 6)

# The paragraph Lejebolig appends to every description in the area. It names the
# schools within 5 km; it says nothing about who may rent the flat.
BOILERPLATE = (
    "Uddannelsessteder i nærheden omfatter Aalborg Universitet København, "
    "NEXT - Sukkertoppen Gymnasium, SOSU H og Hærens Officersskole. Dette gør "
    "området relevant for studerende og personer under uddannelse."
)


def _rec(**over):
    rec = {
        "site": "test",
        "listing_id": "L1",
        "title": "1-værelses lejlighed",
        "address": "Viola Nørløvs Gade 3, 2450 København",
        "listing_type": "lejlighed",
        "rooms": 1,
        "size_m2": 31.0,
        "rent_total": 9350,
        "deposit": 26850,
        "prepaid_rent": 8950,
        "available_from": "2027-01-20",
        "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "description": "Eget køkken og eget badeværelse. Ubegrænset lejeperiode. "
                       + BOILERPLATE,
        "lat": 55.6670,
        "lon": 12.5450,
    }
    rec.update(over)
    return with_detail_evidence(rec)


def _evaluate(rec, stations, commute_min=12):
    return filters.evaluate_listing(rec, stations, today=TODAY)


# ==============================================================================
# 1. The studerende false positive
# ==============================================================================
def test_nearby_schools_boilerplate_is_not_student_housing(mock_stations):
    """The defect that hid the whole Viola Nørløvs Gade building.

    'relevant for studerende' describes the neighbourhood, not the tenancy.
    """
    res = _evaluate(_rec(), mock_stations)
    assert res.passed, res.rejection_reason


def test_wording_that_actually_restricts_the_tenancy_still_rejects(mock_stations):
    for phrase in ("Lejligheden er forbeholdt studerende.",
                   "Udlejes kun til studerende.",
                   "Der er krav om studieaktivitet.",
                   "Værelse på kollegiet."):
        res = _evaluate(_rec(description=f"Eget køkken og bad. {phrase}"), mock_stations)
        assert not res.passed, f"{phrase!r} must still be rejected"
        # Spec 07 section 4.4 row 1 rejects an explicit room/share advert before
        # the v3.5 housing classifier runs, and section 6.2 fixes the reason
        # wording, which section 7.1 ranks first. The two v3.5 reasons are still
        # admitted for a phrase that reaches the housing gate first.
        assert res.rejection_reason in ("student housing", "not a studio (kollegie)",
                                        "room rental / not self-contained")


def test_ungdomsbolig_alone_is_rejected_as_student_only(mock_stations):
    """A bare ungdomsbolig is a student product; a working professional cannot apply."""
    res = _evaluate(_rec(description="Eget køkken og bad. Boligen er en ungdomsbolig."), mock_stations)
    assert res.rejected is True
    assert res.rejection_reason == config.ELIGIBILITY_REASON_YOUTH


@pytest.mark.parametrize("text", [
    "Eget køkken og bad. Ungdomsbolig, alderskrav på 18-35 år.",
    "Eget køkken og bad. Studie/ ungdomsbolig (uden studiepligt).",
    "Eget køkken og bad. Lejemålet udlejes som ungdomsbolig, maks. 40 år.",
])
def test_ungdomsbolig_is_always_rejected_even_with_an_open_age_range(mock_stations, text):
    res = _evaluate(_rec(description=text), mock_stations)
    assert res.rejected is True
    assert res.rejection_reason == config.ELIGIBILITY_REASON_YOUTH


def test_andelsbolig_is_rejected(mock_stations):
    res = _evaluate(_rec(description="Eget køkken og bad. Andelsbolig i foreningen."), mock_stations)
    assert res.rejected is True
    assert res.rejection_reason == config.ELIGIBILITY_REASON_COOP


def test_handel_boilerplate_is_not_andelsbolig(mock_stations):
    res = _evaluate(_rec(description="Eget køkken og bad. Gode muligheder for dagligvarehandel."),
                    mock_stations)
    assert res.rejection_reason != config.ELIGIBILITY_REASON_COOP


# ==============================================================================
# 2. The v3.3 affordability-first model: hard 9,500 kr cap + Responsible Pick
# ==============================================================================
def test_rent_inside_the_hard_cap_is_admitted(mock_stations):
    res = _evaluate(_rec(rent_total=9350), mock_stations)
    assert res.passed
    assert res.budget_band == "within"


def test_the_hard_cap_is_inclusive_at_9700_and_hard_above(mock_stations):
    """9,700 all-in is the v3.12 (spec 11) hard ceiling; no stretch band above it."""
    assert _evaluate(_rec(rent_total=config.RENT_CAP_MAX), mock_stations).passed
    over = _evaluate(_rec(rent_total=config.RENT_CAP_MAX + 1), mock_stations)
    assert not over.passed
    assert str(config.RENT_CAP_MAX) in (over.rejection_reason or ""), \
        "the reject names the hard cap"


def test_no_band_ever_lets_a_flat_over_9700_through(mock_stations, place_features):
    """The old tier-C + stretch carve-outs are gone in v3.3 — the cap is uniform."""
    place_features["commute_min"] = 12
    res = _evaluate(_rec(rent_total=config.RENT_CAP_MAX + 1), mock_stations)
    assert not res.passed
    assert str(config.RENT_CAP_MAX) in (res.rejection_reason or "")


def test_a_within_cap_match_is_never_responsible_pick(mock_stations):
    row = _evaluate(_rec(), mock_stations).to_dict()
    assert notify.is_responsible_pick(row) is False
    assert "Responsible Pick" not in notify.fmt_brief_alert(row)


def test_a_legacy_row_with_no_budget_band_reads_as_a_normal_pick():
    """Rows written before the v3.3 migration have no RP flag at all."""
    assert notify.is_responsible_pick({"tier": "A", "budget_band": None}) is False
    assert notify.display_tier_of({"tier": "A"}) == "D"



# ==============================================================================
# 2b. Responsible Pick classification (decided 10 Sep 2026)
# ==============================================================================
def _no_metro(monkeypatch):
    monkeypatch.setattr(geo, "nearest_metro", lambda lat, lon, st: (None, None, None, None))


def test_rp_when_metro_too_far_but_s_train_within_800(mock_stations, monkeypatch):
    _no_metro(monkeypatch)
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda lat, lon, st: ("Sydhavn", 300.0, 400.0, 5, "s-tog"))
    res = _evaluate(_rec(rent_total=8500), mock_stations)
    assert res.passed, res.rejection_reason
    assert res.is_responsible_pick is True
    assert res.used_transit_walk_m == 400.0


def test_rp_s_train_curve_400_gives_24_800_gives_18(mock_stations, monkeypatch):
    _no_metro(monkeypatch)
    with_rp = _evaluate_rp(monkeypatch, mock_stations, walk_m=400.0)
    at_800 = _evaluate_rp(monkeypatch, mock_stations, walk_m=800.0)
    assert with_rp.score_breakdown["transit"] == 16.0  # v3.10 RP curve
    assert at_800.score_breakdown["transit"] == 12.0


def _evaluate_rp(monkeypatch, mock_stations, walk_m):
    _no_metro(monkeypatch)
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda lat, lon, st: ("Sydhavn", walk_m * 0.6, walk_m, 5, "s-tog"))
    return _evaluate(_rec(rent_total=8500), mock_stations)


def test_rp_s_train_walk_above_800_ranks_with_no_transit(mock_stations, monkeypatch):
    _no_metro(monkeypatch)
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda lat, lon, st: ("Sydhavn", 500.0, 801.0, 5, "s-tog"))
    res = _evaluate(_rec(rent_total=8500), mock_stations)
    assert res.rejected is False
    assert any(f.code == "no_transit" for f in res.soft_flags)


def test_rp_rent_cap_is_informational_only(mock_stations, monkeypatch):
    _no_metro(monkeypatch)
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda lat, lon, st: ("Sydhavn", 300.0, 400.0, 5, "s-tog"))
    assert _evaluate(_rec(rent_total=8500), mock_stations).passed
    assert _evaluate(_rec(rent_total=8501), mock_stations).passed


def test_a_metro_within_1500_is_a_normal_pick_not_rp(mock_stations, monkeypatch):
    monkeypatch.setattr(geo, "nearest_metro",
                        lambda lat, lon, st: ("Rådhuspladsen", 400.0, 1200.0, 6))
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda lat, lon, st: ("Rådhuspladsen", 400.0, 1200.0, 6, "metro"))
    res = _evaluate(_rec(rent_total=9400), mock_stations)
    assert res.passed, res.rejection_reason
    assert res.is_responsible_pick is False
    # transit points from the metro curve (500->30, 800->24, 1500->0):
    # at 1200 m: 24 - (1200-800)*(24/700) = 10.2857, rounded to 1 dp in the row
    assert round(res.score_breakdown["transit"], 1) == 6.9  # v3.10: 10.3 * 20/30


# ==============================================================================
# 3. Alert volume
# ==============================================================================
def _candidate(available_from="2027-01-20", **over):
    rec = {
        "site": "test",
        "listing_id": "L1",
        "title": "1-værelses",
        "address": "Jorisvej 1a, 2300 København",
        "listing_type": "lejlighed",
        "rooms": 1,
        "size_m2": 32.0,
        "rent_total": 8300,
        "deposit": 20000,
        "prepaid_rent": 0,
        "available_from": available_from,
        "rental_period": "ubegrænset",
        "rental_period_raw": 0,
        "description": "Eget køkken og eget badeværelse. Ubegrænset lejeperiode.",
        "lat": 55.65,
        "lon": 12.60,
    }
    rec.update(over)
    return with_detail_evidence(rec)


@pytest.fixture
def alert_env(temp_db, monkeypatch, mock_stations):
    sent = []
    monkeypatch.setattr(notify, "notify_match",
                        lambda payload, prefix="": sent.append((prefix, payload)) or 200)
    monkeypatch.setattr(notify, "send_text",
                        lambda text, parse_html=False: sent.append(text) or 200)
    monkeypatch.setattr(geo, "load_stations", lambda: mock_stations)
    monkeypatch.setattr(geo, "nearest_metro",
                        lambda lat, lon, st: ("Rådhuspladsen", 300.0, 400.0, 5))
    monkeypatch.setattr(geo, "nearest_transit",
                        lambda lat, lon, st: ("Rådhuspladsen", 300.0, 400.0, 5, "metro"))
    monkeypatch.setattr(run.geo, "geocode_chain",
                        lambda k, a: (55.65, 12.60, "Jorisvej 1a, 2300 København", "exact"))
    monkeypatch.setattr(config, "BOT_CONFIRMED", True)
    return temp_db, sent, mock_stations


def test_tier_a_actionable_still_interrupts(alert_env):
    store, sent, stations = alert_env
    run.process_candidate(_candidate(), stations, store, None, dry_run=False)
    assert len(sent) == 1


def test_a_lower_rank_actionable_match_is_stored_but_never_sent(alert_env, place_features, monkeypatch):
    """Rank B and C belong in the daily digest, not on a phone.

    The instant gate reads the rank letter, not the commute band (user decision,
    15 Oct 2026), so a rank-B flat in the close band still stays off the phone.
    """
    store, sent, stations = alert_env
    place_features["commute_min"] = 15

    orig_eval = filters.evaluate_listing

    def _eval_b(*args, **kwargs):
        res = orig_eval(*args, **kwargs)
        res.score = 50.0
        return res

    monkeypatch.setattr(run.filters, "evaluate_listing", _eval_b)
    alerted = run.process_candidate(_candidate(), stations, store, None, dry_run=False)
    assert alerted is False
    assert sent == []
    row = store.get_match_row(1)
    assert row["relevance"] == "actionable", "still actionable, just not shouted about"
    assert row["alerted_actionable"] == 0


def test_an_over_cap_flat_never_interrupts_or_matches(alert_env):
    """/v3.12 (spec 11): a flat above the hard 9,700 cap is rejected outright - no alert, no row."""
    store, sent, stations = alert_env
    run.process_candidate(_candidate(rent_total=config.RENT_CAP_MAX + 1), stations,
                          store, None, dry_run=False)
    assert sent == []
    assert store.get_match_row(1) is None


def test_one_property_cannot_ping_twice_inside_the_cooldown(alert_env):
    """Viola Nørløvs Gade relists a dozen units as tenants move out.

    Each carries a new listing id, so without a per-property cooldown the
    building would alert every single day.
    """
    store, sent, stations = alert_env
    run.process_candidate(_candidate(), stations, store, None, dry_run=False)
    assert len(sent) == 1
    prop_id = store.get_match_row(1)["property_id"]

    # A different unit in the same building, a day later.
    store.conn.execute("UPDATE matches SET alerted_actionable=0 WHERE property_id=?",
                       (prop_id,))
    store.conn.commit()
    run.process_candidate(_candidate(listing_id="L2", rent_total=8100),
                          stations, store, None, dry_run=False)
    assert len(sent) == 1, "the cooldown must swallow the second unit"


def test_a_failed_send_does_not_start_a_cooldown(alert_env, monkeypatch):
    """Pitfall 9: an unconfirmed delivery must be retried, not muted for a week."""
    store, sent, stations = alert_env
    monkeypatch.setattr(notify, "notify_match", lambda payload, prefix="": None)
    run.process_candidate(_candidate(), stations, store, None, dry_run=False)
    prop_id = store.get_match_row(1)["property_id"]
    assert store.alert_cooldown_active(prop_id) is False
    assert store.get_match_row(1)["alerted_actionable"] == 0


def test_a_dry_run_never_starts_a_cooldown(alert_env):
    store, sent, stations = alert_env
    run.process_candidate(_candidate(), stations, store, None, dry_run=True)
    assert store.alert_cooldown_active(1) is False


# ==============================================================================
# 4. One pin per building, showing its best unit
# ==============================================================================
def test_the_best_scoring_live_unit_holds_the_match_row(alert_env, place_features):
    """43 listings, one property. The row must not be whichever came last."""
    store, sent, stations = alert_env
    run.process_candidate(_candidate(listing_id="GOOD", rent_total=7000),
                          stations, store, None, dry_run=False)
    good_score = store.get_match_row(1)["score"]

    run.process_candidate(_candidate(listing_id="WORSE", rent_total=9400),
                          stations, store, None, dry_run=False)
    row = store.get_match_row(1)
    assert row["listing_id"] == "GOOD"
    assert row["score"] == good_score


def test_a_better_unit_takes_the_row_over_a_worse_incumbent(alert_env):
    store, sent, stations = alert_env
    run.process_candidate(_candidate(listing_id="WORSE", rent_total=9400),
                          stations, store, None, dry_run=False)
    run.process_candidate(_candidate(listing_id="BEST", rent_total=7000),
                          stations, store, None, dry_run=False)
    assert store.get_match_row(1)["listing_id"] == "BEST"


def test_a_dead_incumbent_does_not_hold_the_row(alert_env):
    store, sent, stations = alert_env
    run.process_candidate(_candidate(listing_id="GONE", rent_total=7000),
                          stations, store, None, dry_run=False)
    store.conn.execute("UPDATE source_listings SET lifecycle_state='expired' "
                       "WHERE listing_id='GONE'")
    store.conn.commit()
    run.process_candidate(_candidate(listing_id="LIVE", rent_total=9400),
                          stations, store, None, dry_run=False)
    assert store.get_match_row(1)["listing_id"] == "LIVE"


def test_unit_summary_counts_the_live_listings_of_a_building(alert_env):
    store, sent, stations = alert_env
    run.process_candidate(_candidate(listing_id="A1", rent_total=9350),
                          stations, store, None, dry_run=False)
    run.process_candidate(_candidate(listing_id="A2", rent_total=9400),
                          stations, store, None, dry_run=False)
    summary = store.property_unit_summary(1)
    assert summary["units"] == 2
    assert summary["rent_min"] == 9350
    assert summary["rent_max"] == 9400


# ==============================================================================
# 5. Digest shape
# ==============================================================================
def test_digest_shows_three_per_tier():
    assert digest.MAX_PER_TIER == 3


def test_digest_fills_a_thin_tier_with_the_best_intel():
    actionable = [{"score": 80, "relevance": "actionable"}]
    intel = [{"score": 90, "relevance": "intel"},
             {"score": 70, "relevance": "intel"},
             {"score": 95, "relevance": "intel"}]
    rows = digest._top_for_rank(actionable, intel, "S", 3)
    assert [r["score"] for r in rows] == [80, 95, 90], "actionable leads, intel fills"
    assert all(notify.display_tier_of(r) == "S" for r in rows)


def test_digest_groups_rows_by_rank_v33():
    """/v3.3: a Responsible Pick is ranked by its score like any other row."""
    rp = {"is_responsible_pick": True, "score": 60, "relevance": "intel"}
    normal = {"is_responsible_pick": False, "score": 62, "relevance": "intel"}
    assert len(digest._top_for_rank([], [rp, normal], "A", 3)) == 2
    assert len(digest._top_for_rank([], [rp, normal], "B", 3)) == 0
    assert notify.display_tier_of(rp) == "A", "RP must not be auto-lifted to tier S"


# ==============================================================================
# 6. Tie-breaks between the units of one building
# ==============================================================================
def test_an_equal_scoring_unit_breaks_to_the_cheaper_rent():
    """v3.3 has no stretch band; equal scores break on rent within the hard cap."""
    from store import Store
    assert Store.match_rank(65, "within", 9350) > Store.match_rank(65, "within", 9700)


def test_a_tie_inside_the_same_band_breaks_towards_the_cheaper_rent():
    from store import Store
    assert Store.match_rank(65, "within", 8900) > Store.match_rank(65, "within", 9400)


def test_score_still_outranks_both_tie_breaks():
    from store import Store
    assert Store.match_rank(70, "within", 9800) > Store.match_rank(65, "within", 7000)


def test_the_cheaper_unit_of_a_tied_building_holds_the_match_row(alert_env, monkeypatch):
    store, sent, stations = alert_env
    # Force the tie the production data produced.
    monkeypatch.setattr(filters, "score_listing", None, raising=False)
    run.process_candidate(_candidate(listing_id="DEAR", rent_total=9400),
                          stations, store, None, dry_run=False)
    dear_score = store.get_match_row(1)["score"]
    run.process_candidate(_candidate(listing_id="CHEAP", rent_total=8900),
                          stations, store, None, dry_run=False)
    row = store.get_match_row(1)
    if row["score"] == dear_score:
        assert row["listing_id"] == "CHEAP", "a tie must go to the cheaper unit"
    assert row["budget_band"] == "within"


# ==============================================================================
# 7. Telegram formatters
# ==============================================================================
def test_brief_alert_labels_a_responsible_pick():
    row = {"tier": "B", "is_responsible_pick": True, "score": 57, "property_id": 218,
           "bw_id": "BW-0000218", "address": "Viola Nørløvs Gade 1", "rent_total": 8500}
    alert = notify.fmt_brief_alert(row)
    assert "Tier B · 57/100" in alert
    assert "Responsible Pick" in alert


def test_details_names_the_rp_budget_and_keeps_the_commute_tier():
    """Badge carries the commute name; the budget line flags the Responsible Pick."""
    row = {"tier": "B", "is_responsible_pick": True, "badge": "Commutable", "score": 57,
           "property_id": 218, "bw_id": "BW-0000218",
           "address": "Viola Nørløvs Gade 1", "rent_total": 8500}
    msg = notify.fmt_details_message(row)
    header = msg.splitlines()[0]
    assert "Tier B Solid · 57/100" in header
    assert "Commute band: Commutable" in msg
    assert "Responsible Pick" in msg
    assert "stretch" not in msg.lower()


def test_details_of_a_within_budget_match_is_unchanged():
    row = {"tier": "A", "budget_band": "within", "badge": "Close", "score": 70,
           "property_id": 45, "bw_id": "BW-0000045",
           "address": "Jorisvej 1a", "rent_total": 8300}
    msg = notify.fmt_details_message(row)
    assert "Tier A Strong · 70/100" in msg.splitlines()[0]
    assert "Commute band: Close" in msg
    assert "Budget: within" in msg


# ==============================================================================
# 8. The v3.3 rapid-transit rule (Metro 1.500 m, else Responsible Pick + S-train)
# ==============================================================================
def _far_from_metro(monkeypatch, metro_walk_m, s_walk_m=None):
    """Put the flat metro_walk_m from the Metro (and s_walk_m from an S-train if given),
    or no transit at all when both are None."""
    if metro_walk_m is None:
        monkeypatch.setattr(geo, "nearest_metro", lambda lat, lon, st: (None, None, None, None))
    else:
        monkeypatch.setattr(geo, "nearest_metro",
                            lambda lat, lon, st: ("Sluseholmen", metro_walk_m * 0.6,
                                                  metro_walk_m, 7))
    if s_walk_m is None:
        monkeypatch.setattr(geo, "nearest_transit", lambda lat, lon, st: (None, None, None, None, "s-tog"))
    else:
        monkeypatch.setattr(geo, "nearest_transit",
                            lambda lat, lon, st: ("Sluseholmen", s_walk_m * 0.6, s_walk_m, 7, "s-tog"))


def test_normal_candidate_requires_metro_walk_of_1500_or_less(mock_stations, monkeypatch):
    _far_from_metro(monkeypatch, 1500, s_walk_m=500)
    res = _evaluate(_rec(rent_total=7000), mock_stations)
    assert res.passed, res.rejection_reason
    assert res.is_responsible_pick is False


def test_metro_walk_over_1500_turns_the_flat_into_a_responsible_pick(mock_stations, monkeypatch):
    _far_from_metro(monkeypatch, 1600, s_walk_m=600)
    res = _evaluate(_rec(rent_total=8000), mock_stations)
    assert res.passed, res.rejection_reason
    assert res.is_responsible_pick is True
    assert res.used_transit_walk_m == 600.0


def test_no_rapid_transit_at_all_ranks_with_a_badge(mock_stations, monkeypatch):
    _far_from_metro(monkeypatch, None, s_walk_m=None)
    res = _evaluate(_rec(rent_total=7000), mock_stations)
    assert res.rejected is False
    assert any(f.code == "no_transit" for f in res.soft_flags)


def test_a_far_flat_over_the_rp_cap_ranks_despite_an_s_train(mock_stations, monkeypatch):
    _far_from_metro(monkeypatch, None, s_walk_m=500)
    res = _evaluate(_rec(rent_total=8600), mock_stations)
    assert res.rejected is False
    assert res.passed is True


def test_the_rapid_transit_rule_uses_routed_walking_metres():
    """1.500 m to a Metro, 800 m to an S-train, at 80 m/min = 10 min."""
    assert config.NORMAL_METRO_WALK_MAX_M == 1500
    assert config.RP_S_TRAIN_MAX_M == 800
    assert config.RP_S_TRAIN_MAX_M == 10 * config.WALK_SPEED_M_PER_MIN


# ==============================================================================
# 9. Student housing that hid behind a sibling listing
# ==============================================================================
def test_a_brand_name_in_the_title_is_student_housing(mock_stations):
    """'Canvas Student Nordhavn' held a tier A match at 66/100."""
    res = _evaluate(_rec(title="Canvas Student Nordhavn"), mock_stations)
    assert not res.passed
    assert res.rejection_reason == "student housing"


def test_studiebolig_in_the_title_is_student_housing(mock_stations):
    for title in ("1-værelses studiebolig på Amager",
                  "Studiebolig tæt på indkøb og metro!",
                  "Skøn 1-værelses studiebolig!"):
        res = _evaluate(_rec(title=title), mock_stations)
        assert not res.passed, title
        assert res.rejection_reason == "student housing"


def test_students_mentioned_only_in_the_description_still_passes(mock_stations):
    """The title names the product; the description describes the area."""
    res = _evaluate(_rec(title="1-værelses lejlighed",
                         description="Eget køkken og bad. " + BOILERPLATE +
                                     " Der er mange student-venlige cafeer."),
                    mock_stations)
    assert res.passed, res.rejection_reason


def test_boligportal_unselected_student_lifestyle_chip_passes(mock_stations):
    """BoligPortal renders 'Kun for studerende' in Livsstil chips even when inactive."""
    rec = _rec(title="2 værelses lejlighed på 45 m²",
               description="Dejlig moderniseret lejlighed tæt på stationen.")
    rec["detail_hydration"] = {
        "props": {
            "page_props": {
                "ad": {
                    "description": "Dejlig moderniseret lejlighed tæt på stationen.",
                    "features": {
                        "student_only": False,
                        "shareable": True
                    }
                }
            }
        }
    }
    rec["eligibility_text"] = "Livsstil Delevenlig Kun for studerende Husdyr tilladt Seniorvenlig Faciliteter Parkering"
    res = _evaluate(rec, mock_stations)
    assert res.passed, res.rejection_reason
    assert res.student_requirement_state == "not_required"


def test_address_house_number_detection():
    from store import Store
    assert Store.address_is_one_building("Jorisvej 1a, 2300") is True
    assert Store.address_is_one_building("Helsinkigade 27, 2150 Nordhavn") is True
    assert Store.address_is_one_building("Gammel Køge Landevej, 2500 Valby") is False
    assert Store.address_is_one_building("Finsensvej, 2000 Frederiksberg") is False
    assert Store.address_is_one_building(None) is False


def test_a_sibling_listing_condemns_the_whole_building(alert_env):
    """Jorisvej 1a: 2 of its 4 listings said studiebolig, the match row did not."""
    store, sent, stations = alert_env
    run.process_candidate(_candidate(listing_id="QUIET"), stations, store, None,
                          dry_run=False)
    assert store.get_match_row(1) is not None, "the quiet listing matches on its own"

    # A second advert for the same address, openly a studiebolig.
    run.process_candidate(_candidate(listing_id="LOUD", title="Skøn studiebolig!"),
                          stations, store, None, dry_run=False)
    assert store.property_housing_disqualification(1) == "student housing"

    # The quiet one must no longer hold a match either.
    kept = run.process_candidate(_candidate(listing_id="QUIET"), stations, store, None,
                                 dry_run=False)
    assert kept is False
    assert store.get_match_row(1) is None


def test_stickiness_does_not_apply_to_a_street_level_address(alert_env, monkeypatch):
    """A street aggregates buildings; one student block must not condemn it."""
    store, sent, stations = alert_env
    monkeypatch.setattr(run.geo, "geocode_chain",
                        lambda k, a: (55.65, 12.60, "Gammel Køge Landevej, 2500 Valby", "street"))
    run.process_candidate(_candidate(listing_id="LOUD", title="Skøn studiebolig!",
                                     address="Gammel Køge Landevej, 2500 Valby"),
                          stations, store, None, dry_run=False)
    run.process_candidate(_candidate(listing_id="QUIET",
                                     address="Gammel Køge Landevej, 2500 Valby"),
                          stations, store, None, dry_run=False)
    assert store.property_housing_disqualification(1) is None
    assert store.get_match_row(1) is not None


# ==============================================================================
# 10. Overpass circuit breaker (handoff pitfall 3, on a service that lacked one)
# ==============================================================================
@pytest.mark.real_features
def test_overpass_gives_up_after_repeated_failures(monkeypatch):
    """A blackholed Overpass must not cost a socket timeout per coordinate.

    Both endpoints down cost ~3 s refused plus a 40 s read timeout on EVERY new
    coordinate, which turned a 30-minute cold scan into a 2-hour one.
    """
    import features
    features.reset_overpass_state()
    calls = []

    def _boom(req, timeout=None):
        calls.append(req.full_url)
        raise OSError("Network is unreachable")

    monkeypatch.setattr(features.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(features.time, "sleep", lambda s: None)

    for i in range(10):
        assert features._overpass_buzz(55.68 + i / 1000, 12.57) is None

    assert features._overpass_disabled is True
    # 3 coordinates x 2 endpoints x 2 retries is the ceiling; the other 7
    # coordinates must cost no network calls at all.
    assert len(calls) <= 3 * len(config.OVERPASS_URLS) * 2
    features.reset_overpass_state()


@pytest.mark.real_features
def test_a_successful_call_re_arms_the_breaker(monkeypatch):
    import features
    features.reset_overpass_state()
    features._overpass_failures = 2

    class _OK:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"elements": []}'

    monkeypatch.setattr(features.json, "load", lambda r: {"elements": []})
    monkeypatch.setattr(features.urllib.request, "urlopen", lambda req, timeout=None: _OK())
    monkeypatch.setattr(features.time, "sleep", lambda s: None)

    assert features._overpass_buzz(55.68, 12.57) == {"elements": []}
    assert features._overpass_failures == 0
    assert features._overpass_disabled is False
    features.reset_overpass_state()


# ==============================================================================
# 11. Cold-start benchmarks
# ==============================================================================
def test_value_collapses_to_the_rent_proxy_without_a_market_table(mock_stations):
    """The cold-start trap: benchmarks are computed FROM the scan's own data.

    A fresh database has no market table when its first scan evaluates listings,
    so value falls back to a bare rent proxy. Near the rent ceiling that proxy is
    almost nothing - Njalsgade 151-169 scored 0.0 of 25 in production - which is
    why run.py re-scores after a cold start.
    """
    res = _evaluate(_rec(), mock_stations)
    assert res.passed
    assert res.value_ratio is None, "no market table means nothing to compare against"
    assert res.score_breakdown.get("value") < 1.0


def test_value_scores_properly_once_the_market_table_exists(mock_stations):
    segments = {"B": {"median_kr_m2": 305.0, "n": 40},
                "A": {"median_kr_m2": 347.0, "n": 40}}
    bare = _evaluate(_rec(), mock_stations)
    res = filters.evaluate_listing(_rec(), mock_stations, segments=segments, today=TODAY)
    assert res.passed
    assert res.value_ratio is not None
    assert res.score_breakdown["value"] > bare.score_breakdown["value"], \
        "the market table must be worth more than the rent proxy"


def test_boligportal_unselected_chips_without_hydration_passes(mock_stations):
    """BoligPortal raw inner_text with unselected Livsstil chips must not reject when hydration is absent."""
    rec = _rec(site="boligportal", title="2 værelses lejlighed på 45 m²",
               description="Dejlig moderniseret lejlighed tæt på stationen.")
    rec["eligibility_text"] = "Livsstil Delevenlig Kun for studerende Husdyr tilladt Seniorvenlig Faciliteter Parkering"
    res = _evaluate(rec, mock_stations)
    assert res.passed, res.rejection_reason


def test_boligportal_marked_as_reserved_rejected(mock_stations):
    """Listing marked as reserved by landlord must be rejected and not ranked."""
    rec = _rec(site="boligportal", title="Dejlig lejlighed",
               description="Lejemålet er blevet markeret som reserveret af udlejeren. Det bliver måske ledigt igen, hvis den valgte lejer springer fra. Ellers har vi stadig mange lignende lejemål til dig - Se alle lignende lejemål")
    res = _evaluate(rec, mock_stations)
    assert res.rejected
    assert "reserved" in res.rejection_reason or "gone" in res.rejection_reason


def test_select_for_decision_revalidation_skips_reserved_listings(alert_env):
    """select_for_decision_revalidation must skip listings with reserved banner."""
    store, sent, stations = alert_env
    # Record a listing that has the reserved banner
    src_id = store.record_source_listing(
        property_id=1, site="boligportal", listing_id="RES1", url="http://example.com/res1",
        raw_data={"site": "boligportal", "listing_id": "RES1", "rent_total": 8000,
                  "description": "Lejemålet er blevet markeret som reserveret af udlejeren."}
    )
    store.log_match(
        property_id=1, bw_id="BW-0000001",
        rec={"site": "boligportal", "listing_id": "RES1", "rent_total": 8000},
        eval_res=_evaluate(_rec(), stations),
        alert_text="test", alert_status="sent"
    )
    picked = run.select_for_decision_revalidation(store)
    assert not any(r["listing_id"] == "RES1" for r in picked)
