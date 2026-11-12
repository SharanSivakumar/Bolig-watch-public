"""Tests for BoligWatch v3.2 Plan 1 rank tiers and clickable legend."""
import datetime
import pytest

import config
import digest
import notify
import run
import tiers
import viewer
from conftest import _candidate, EMOJI_RE
from store import Store


@pytest.mark.parametrize("score,expected", [
    (100, "S"),
    (75, "S"),
    (74.6, "A"),
    (74, "A"),
    (60, "A"),
    (59, "B"),
    (45, "B"),
    (44, "C"),
    (30, "C"),
    (29, "D"),
    (0, "D"),
    (None, "D"),
    ("abc", "D"),
])
def test_rank_tier_boundaries(score, expected):
    assert tiers.rank_tier_of(score) == expected


def test_rank_range():
    assert tiers.rank_range("S") == (75, 100)
    assert tiers.rank_range("A") == (60, 74)
    assert tiers.rank_range("B") == (45, 59)
    assert tiers.rank_range("C") == (30, 44)
    assert tiers.rank_range("D") == (0, 29)


def test_rank_boundary_just_below_and_at_s():
    # Plan 1's ranking uses >= on the raw float (no rounding of the boundary).
    assert tiers.rank_tier_of(74.999) == "A"
    assert tiers.rank_tier_of(75.0) == "S"


def test_display_tier_of_score_none_is_d():
    # A legacy row with no score must never display as a non-D rank, however
    # it reads on the commute tier.
    assert notify.display_tier_of({"tier": "A", "score": None}) == "D"


def test_stretch_does_not_change_rank():
    row_within = {"score": 65, "budget_band": "within", "rent_total": 9350}
    row_stretch = {"score": 65, "budget_band": "stretch", "rent_total": 9650}
    assert notify.display_tier_of(row_within) == "A"
    assert notify.display_tier_of(row_stretch) == "A"


def test_display_tier_of_ignores_commute_tier():
    row_d = {"tier": "A", "score": 20}
    row_s = {"tier": "C", "score": 80}
    assert notify.display_tier_of(row_d) == "D"
    assert notify.display_tier_of(row_s) == "S"
    assert notify.tier_of(row_d) == "A"
    assert notify.tier_of(row_s) == "C"


def test_over_budget_kr():
    assert tiers.over_budget_kr({"budget_band": "stretch", "rent_total": 9650}) == 150
    assert tiers.over_budget_kr({"budget_band": "within", "rent_total": 9350}) is None
    assert tiers.over_budget_kr({"tier": "A", "budget_band": None}) is None


def test_legend_entries_structure():
    legend = tiers.legend_entries()
    assert "ranks" in legend and "bands" in legend and "cap" in legend
    ranks = legend["ranks"]
    assert len(ranks) == 5
    letters = [r["letter"] for r in ranks]
    assert letters == ["S", "A", "B", "C", "D"]

    # Verify contiguous non-overlapping ranges
    for i in range(len(ranks) - 1):
        assert ranks[i]["min"] == ranks[i + 1]["max"] + 1

    bands = legend["bands"]
    assert len(bands) == 3
    assert [b["letter"] for b in bands] == ["A", "B", "C"]

    cap = legend["cap"]
    assert cap["ceiling"] == config.RENT_CAP_MAX
    assert cap["max"] == config.RENT_CAP_MAX


def _eval_with_score(target_score):
    """Wrap filters.evaluate_listing to force a score, so the rank gate can be pinned."""
    import filters
    orig_eval = filters.evaluate_listing

    def _wrapper(*args, **kwargs):
        res = orig_eval(*args, **kwargs)
        res.score = target_score
        return res
    return _wrapper


def test_alert_gate_fires_for_a_rank_s_flat_in_commute_band_b(alert_env, place_features, monkeypatch):
    """Rank S interrupts whatever the commute band (alert policy)."""
    store, sent, stations = alert_env
    place_features["commute_min"] = 28  # band B
    monkeypatch.setattr(run.filters, "evaluate_listing", _eval_with_score(85.0))
    alerted = run.process_candidate(_candidate(listing_id="BAND_B_RANK_S", rent_total=8500),
                                    stations, store, None, dry_run=False)
    assert alerted is True
    assert len(sent) == 1


def test_alert_gate_fires_for_a_rank_a_flat_in_commute_band_c(alert_env, place_features, monkeypatch):
    """Rank A interrupts even in the far commute band C."""
    store, sent, stations = alert_env
    place_features["commute_min"] = 45  # band C
    monkeypatch.setattr(run.filters, "evaluate_listing", _eval_with_score(65.0))
    alerted = run.process_candidate(_candidate(listing_id="BAND_C_RANK_A", rent_total=8400),
                                    stations, store, None, dry_run=False)
    assert alerted is True
    assert len(sent) == 1


def test_alert_gate_stays_silent_for_a_rank_d_flat_in_commute_band_a(alert_env, place_features, monkeypatch):
    """A commute band on the old instant list no longer interrupts on its own."""
    store, sent, stations = alert_env
    place_features["commute_min"] = 15  # band A
    monkeypatch.setattr(run.filters, "evaluate_listing", _eval_with_score(20.0))
    alerted = run.process_candidate(_candidate(listing_id="BAND_A_RANK_D", rent_total=9400),
                                    stations, store, None, dry_run=False)
    assert alerted is False
    assert sent == []


def test_alert_gate_stays_silent_for_a_rank_b_flat(alert_env, place_features, monkeypatch):
    store, sent, stations = alert_env
    place_features["commute_min"] = 15
    monkeypatch.setattr(run.filters, "evaluate_listing", _eval_with_score(50.0))
    alerted = run.process_candidate(_candidate(listing_id="BAND_A_RANK_B", rent_total=9400),
                                    stations, store, None, dry_run=False)
    assert alerted is False
    assert sent == []


def test_rank_s_in_band_a_dry_run_logs_new_match_rankS_and_sends_nothing(
        alert_env, place_features, monkeypatch):
    store, sent, stations = alert_env
    import filters
    orig_eval = filters.evaluate_listing

    def _eval_s(*args, **kwargs):
        res = orig_eval(*args, **kwargs)
        res.score = 85.0
        return res

    logged = []
    monkeypatch.setattr(run, "append_log", lambda rec, status, notes="": logged.append((status, notes)))
    monkeypatch.setattr(run.filters, "evaluate_listing", _eval_s)

    # Band A so the row reaches the alert-path log at all; the guard on this
    # test is the rank-letter and band carried by the new_match_rank event, and
    # that a dry run still sends nothing. (A rank-S row in band B is filtered by
    # the alert gate before this log and instead returns at run.py:272 - see r1
    # report "Needs a decision": the plan's literal band-B variant is not
    # implementable without changing alert-gate/match-log semantics.)
    place_features["commute_min"] = 15
    candidate = _candidate(listing_id="BAND_A_RANK_S_DRY", rent_total=8500)
    run.process_candidate(candidate, stations, store, None, dry_run=True)

    rank_events = [n for s, n in logged if s.startswith("new_match_rankS")]
    assert rank_events, logged
    assert "band=A" in rank_events[0], rank_events[0]
    assert len(sent) == 0


def test_rank_s_in_band_b_logs_new_match_rankS_but_a_dry_run_sends_nothing(
        alert_env, place_features, monkeypatch):
    # Sharan's decision (2026-10-15): the rank letter, not the commute band,
    # gates the instant alert. A rank-S flat in commute band B is now on the
    # alert path, so a dry run records new_match_rankS and still sends nothing.
    # (The band-A analog, above, pins the same from the other side.)
    store, sent, stations = alert_env
    import filters
    orig_eval = filters.evaluate_listing

    def _eval_s(*args, **kwargs):
        res = orig_eval(*args, **kwargs)
        res.score = 85.0
        return res

    logged = []
    monkeypatch.setattr(run, "append_log", lambda rec, status, notes="": logged.append((status, notes)))
    monkeypatch.setattr(run.filters, "evaluate_listing", _eval_s)

    place_features["commute_min"] = 28  # band B (Commutable)
    candidate = _candidate(listing_id="BAND_B_RANK_S_DRY", rent_total=8500)
    run.process_candidate(candidate, stations, store, None, dry_run=True)

    rank_events = [n for s, n in logged if s.startswith("new_match_rankS")]
    assert rank_events, logged
    assert "band=B" in rank_events[0], rank_events[0]
    assert len(sent) == 0


def test_digest_top_for_rank_grouping():
    actionable = [
        {"score": 80, "relevance": "actionable", "address": "S Flat"},
        {"score": 62, "relevance": "actionable", "address": "A Flat"},
        {"score": 50, "relevance": "actionable", "address": "B Flat"},
    ]
    intel = [
        {"score": 30, "relevance": "intel", "address": "C Flat"},
        {"score": 10, "relevance": "intel", "address": "D Flat"},
    ]

    s_rows = digest._top_for_rank(actionable, intel, "S", 3)
    a_rows = digest._top_for_rank(actionable, intel, "A", 3)
    b_rows = digest._top_for_rank(actionable, intel, "B", 3)

    assert len(s_rows) == 1 and s_rows[0]["score"] == 80
    assert len(a_rows) == 1 and a_rows[0]["score"] == 62
    assert len(b_rows) == 1 and b_rows[0]["score"] == 50


def test_digest_tail_line(temp_db, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "1")
    # Insert matches into temp_db
    p1 = temp_db._find_or_create_property_direct("S Flat", 55.65, 12.60, 1, 32.0, "new", "2026-09-01")
    p2 = temp_db._find_or_create_property_direct("A Flat", 55.65, 12.60, 1, 32.0, "new", "2026-09-01")
    p3 = temp_db._find_or_create_property_direct("B Flat", 55.65, 12.60, 1, 32.0, "new", "2026-09-01")
    p4 = temp_db._find_or_create_property_direct("C Flat", 55.65, 12.60, 1, 32.0, "new", "2026-09-01")
    p5 = temp_db._find_or_create_property_direct("D Flat", 55.65, 12.60, 1, 32.0, "new", "2026-09-01")

    temp_db.conn.execute("INSERT INTO matches(property_id, bw_id, tier, category, score, rent_total, relevance, quarantined) VALUES(?,?,'A',1,80,8300,'actionable',0)", (p1, "BW-0000001"))
    temp_db.conn.execute("INSERT INTO matches(property_id, bw_id, tier, category, score, rent_total, relevance, quarantined) VALUES(?,?,'A',1,62,8300,'actionable',0)", (p2, "BW-0000002"))
    temp_db.conn.execute("INSERT INTO matches(property_id, bw_id, tier, category, score, rent_total, relevance, quarantined) VALUES(?,?,'B',2,50,8300,'actionable',0)", (p3, "BW-0000003"))
    temp_db.conn.execute("INSERT INTO matches(property_id, bw_id, tier, category, score, rent_total, relevance, quarantined) VALUES(?,?,'B',2,30,8300,'intel',0)", (p4, "BW-0000004"))
    temp_db.conn.execute("INSERT INTO matches(property_id, bw_id, tier, category, score, rent_total, relevance, quarantined) VALUES(?,?,'C',3,10,8300,'intel',0)", (p5, "BW-0000005"))
    temp_db.conn.commit()

    text = digest.generate_daily_digest(temp_db, segments={})
    assert "C 1 · D 1 on the dashboard only" in text


def test_brief_alert_format_within_and_responsible_pick():
    row_within = {
        "score": 65, "property_id": 1, "bw_id": "BW-0000001",
        "address": "Viola Nørløvs Gade 3", "rent_total": 9350, "budget_band": "within",
    }
    brief_within = notify.fmt_brief_alert(row_within)
    assert "Tier A · 65/100 · BW-0000001" in brief_within
    assert "Over the" not in brief_within

    row_rp = {
        "score": 57, "property_id": 34, "bw_id": "BW-0000034",
        "address": "Viola Nørløvs Gade 1", "rent_total": 8500,
        "is_responsible_pick": True,
    }
    brief_rp = notify.fmt_brief_alert(row_rp)
    assert "Tier B · 57/100 · BW-0000034" in brief_rp
    assert "Responsible Pick" in brief_rp


def test_details_message_formatting():
    row_rp = {
        "tier": "B", "score": 57, "property_id": 34, "bw_id": "BW-0000034",
        "address": "Viola Nørløvs Gade 1", "rent_total": 8500,
        "is_responsible_pick": True,
    }
    msg_rp = notify.fmt_details_message(row_rp)
    header_rp = msg_rp.splitlines()[0]
    assert header_rp == "<b>Tier B Solid · 57/100</b>"
    assert "Commute band: Commutable" in msg_rp
    assert "Budget: Responsible Pick, within the 9.700 kr ceiling" in msg_rp

    row_within = {
        "tier": "A", "score": 70, "property_id": 45, "bw_id": "BW-0000045",
        "address": "Jorisvej 1a", "rent_total": 8300, "budget_band": "within",
    }
    msg_within = notify.fmt_details_message(row_within)
    header_within = msg_within.splitlines()[0]
    assert header_within == "<b>Tier A Strong · 70/100</b>"
    assert "Commute band: Close" in msg_within
    assert "Budget: within the 9.700 kr ceiling" in msg_within


def test_store_stats_rank_counts(alert_env):
    store, sent, stations = alert_env
    run.process_candidate(_candidate(listing_id="M1", rent_total=8300), stations, store, None, dry_run=True)
    run.process_candidate(_candidate(listing_id="M2", rent_total=9650), stations, store, None, dry_run=True)

    stats = store.get_stats()
    rank_counts = stats["rank_counts"]
    assert set(rank_counts.keys()) == set(config.RANK_TIER_LETTERS)
    assert sum(rank_counts.values()) == stats["total_matches"]


def test_viewer_listing_rows(alert_env):
    store, sent, stations = alert_env
    run.process_candidate(_candidate(listing_id="M1", rent_total=8300), stations, store, None, dry_run=True)

    rows = viewer.listing_rows(store)
    assert len(rows) >= 1
    r = rows[0]
    assert r["display_tier"] == tiers.rank_tier_of(r["score"])
    assert r["commute_band"] == r["tier"]
    assert r["over_budget_kr"] is None, "v3.3 has no stretch band"
    assert isinstance(r.get("score_breakdown"), dict)
    assert "is_responsible_pick" in r


def test_viewer_html_template_elements():
    assert "__TIER_LEGEND__" not in viewer.HTML_TEMPLATE
    assert "__STRETCH_MAX__" not in viewer.HTML_TEMPLATE
    assert '"ranks"' in viewer.HTML_TEMPLATE
    assert "<option>D</option>" in viewer.HTML_TEMPLATE
    assert 'id="fBand"' in viewer.HTML_TEMPLATE
    assert 'id="fBudget"' in viewer.HTML_TEMPLATE


def test_export_static_html(alert_env, tmp_path, monkeypatch):
    store, sent, stations = alert_env
    from conftest import EMOJI_RE
    out_file = tmp_path / "report.html"
    viewer.export_static_html(str(out_file), store=store)
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "LEGEND" in content
    assert '"ranks"' in content
    assert not EMOJI_RE.findall(content)


def test_export_legend_has_five_contiguous_ranks(alert_env, tmp_path):
    store, sent, stations = alert_env
    out_file = tmp_path / "report.html"
    viewer.export_static_html(str(out_file), store=store)
    content = out_file.read_text(encoding="utf-8")
    import re as _re
    import json as _json
    m = _re.search(r"const LEGEND = (\{.*?\});\n", content, _re.S)
    assert m, "injected LEGEND JSON not found in export"
    legend = _json.loads(m.group(1))
    ranks = legend["ranks"]
    assert [r["letter"] for r in ranks] == ["S", "A", "B", "C", "D"]
    for i in range(len(ranks) - 1):
        assert ranks[i]["min"] == ranks[i + 1]["max"] + 1
    assert ranks[-1] == {"letter": "D", "min": 0, "max": 29,
                         "name": "Poor",
                         "blurb": "Poor. Kept for the map and the market table, not for viewing."}


def test_deprecated_config_constants_removed():
    assert not hasattr(config, "ALERT_INSTANT_TIERS")
    assert not hasattr(config, "STRETCH_LETTER")
    assert hasattr(config, "ALERT_INSTANT_COMMUTE_TIERS")
