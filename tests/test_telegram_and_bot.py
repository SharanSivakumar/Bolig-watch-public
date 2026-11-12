"""Telegram formatters, shortlist commands, alert copy and the no-emoji guard."""
import re

import pytest

import bot
import config
import digest
import notify
import run
import viewer
from conftest import _candidate
from store import Store, format_bw_id

# Emoji and pictographic ranges. Nothing the bot emits may contain these.
# Written as escapes, never as literal characters, so the acceptance-criterion
# grep over *.py does not match this file itself.
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]"
)


def _match(**over):
    m = {
        "tier": "A",
        "category": 1,
        "badge": "Close",
        "score": 78,
        "address": "Jorisvej 1a, 2300 København",
        "rent_total": 8300,
        "size_m2": 32.0,
        "kr_per_m2": 259.4,
        "value_ratio": 0.89,
        "value_label": "-11% vs market",
        "commute_min": 18,
        "commute_route": {"station": "Femøren", "network": "Metro",
                          "walk_min": 4, "ride_min": 13, "via": "M2"},
        "buzz_detail": "Fronts Amagerbrogade (primary), 5 bus routes",
        "water_dist_m": 650.0,
        "water_name": "Amager Strand",
        "available_from": "2027-02-15",
        "relevance": "actionable",
        "bw_id": "BW-0000045",
        "property_id": 45,
    }
    m.update(over)
    return m


def test_brief_alert_matches_spec_layout():
    msg = notify.fmt_brief_alert(_match())
    lines = [l for l in msg.split("\n") if l.strip()]

    assert lines[0] == "<b>Tier S · 78/100 · BW-0000045</b>"
    assert lines[1] == "Jorisvej 1a, 2300 København"
    assert lines[2] == "8.300 kr/md · 32 m2 · 259 kr/m2 (-11% vs market)"
    assert lines[3] == "Nørreport 18 min: walk 4 + M2 13"
    assert lines[4] == "Fronts Amagerbrogade (primary), 5 bus routes · Water 650 m (Amager Strand)"
    assert lines[5] == "Available 15 Feb 2027 · Actionable"
    assert lines[6] == "/details 45"
    assert len(lines) == 7


def test_brief_alert_omits_unknown_facts_but_names_unknown_street():
    msg = notify.fmt_brief_alert(_match(size_m2=None, kr_per_m2=None,
                                        value_label="size unknown",
                                        buzz_detail=None, water_dist_m=None,
                                        available_from=None, relevance=None))
    assert "8.300 kr/md · size unknown" in msg
    assert "Street: unknown" in msg
    assert "Available" not in msg
    assert "Ukendt" not in msg


def test_brief_alert_direct_walk_and_now_actionable_prefix():
    msg = notify.fmt_brief_alert(
        _match(commute_min=12, commute_route={"mode": "walk"}), prefix="Now actionable:")
    assert msg.startswith("<b>Now actionable: Tier S")
    assert "Nørreport 12 min: direct walk" in msg


def test_details_carries_every_v3_fact():
    msg = notify.fmt_details_message(_match(
        utilities_amount=400, move_in_cost=46500, rental_period="ubegrænset",
        lease_verified=True, kitchen_verified=True, bathroom_verified=True,
        nearest_station="Femøren", distance_m=320, walk_min=4,
        city_distance_m=5400, benchmark_median=291.0, benchmark_segment="2300|A",
        geo_precision="street", shortlist_action="save", site="lejebolig",
        risk_state="low", risk_evidence=["No obvious warning signals"],
        confidence_points=5,
        score_breakdown={"affordability": 20.0, "transit": 26.0, "commute": 15.0,
                         "water": 5.0, "centre": 4.0, "buzz": 2.0,
                         "value": 18.0, "date": 9.0},
        url="https://www.lejebolig.dk/test-url"))

    assert "Rent: 8.300 kr/md including 400 kr aconto" in msg
    assert "Value: 32 m2 · 259 kr/m2 · -11% vs market" in msg
    assert "Benchmark: 291 kr/m2 (segment 2300|A)" in msg
    assert "Move-in cost: 46.500 kr" in msg
    assert "Available: 15 February 2027" in msg
    assert "Relevance: Actionable" in msg
    assert "Commute: Nørreport 18 min: walk 4 + M2 13" in msg
    assert "Address resolved to street level" in msg
    assert "Street: Fronts Amagerbrogade (primary), 5 bus routes" in msg
    assert "Water: 650 m (Amager Strand)" in msg
    assert "Shortlist: save" in msg
    assert "Verified 5/5" in msg
    # §3.5: ranking points are affordability/transit/commute + location.
    assert "Score: Affordable 20/35 · Transit 26/20 · Commute 15/16" in msg
    assert "Area 0/15 · Floor 0/4" in msg
    assert "Value 18/25 · Date 9/10" in msg


def test_price_and_lifecycle_message_formats():
    assert (notify.fmt_price_drop("BW-0000045", 8700, 8300)
            == "Price drop BW-0000045: 8.700 -> 8.300 kr/md")
    assert (notify.fmt_removed("BW-0000045", "Jorisvej 1a, 2300 København")
            == "Removed from portal: BW-0000045 (Jorisvej 1a)")
    assert (notify.fmt_relisted("BW-0000045", "Jorisvej 1a, 2300 København")
            == "Relisted: BW-0000045 (Jorisvej 1a)")


def test_no_emoji_in_any_formatter_output(temp_db):
    outputs = [
        notify.fmt_brief_alert(_match()),
        notify.fmt_details_message(_match()),
        notify.fmt_price_drop("BW-0000045", 8700, 8300),
        notify.fmt_removed("BW-0000045", "Jorisvej 1a"),
        notify.fmt_relisted("BW-0000045", "Jorisvej 1a"),
        digest.generate_daily_digest(temp_db, segments={}),
    ]
    for text in outputs:
        found = EMOJI_RE.findall(text)
        assert not found, f"emoji {found!r} found in: {text[:120]}"


# --- Bot commands ----------------------------------------------------------

@pytest.fixture
def bot_env(temp_db, monkeypatch):
    replies = []
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(bot, "send_reply",
                        lambda chat_id, text, **kw: replies.append(text))
    now = "2026-09-06T10:00:00"
    pid = temp_db._find_or_create_property_direct("Jorisvej 1a", 55.65, 12.60, 1, 32.0,
                                                  "new", now)
    temp_db.conn.execute("""
        INSERT INTO matches(property_id, bw_id, tier, category, score, rent_total,
                            commute_min, relevance, address, quarantined)
        VALUES(?,?,'A',1,78,8300,18,'actionable','Jorisvej 1a, 2300 København',0)
    """, (pid, format_bw_id(pid)))
    temp_db.conn.commit()
    return temp_db, pid, replies


def test_save_applied_and_shortlist(bot_env):
    store, pid, replies = bot_env

    bot.process_message({"text": f"/save {pid} south facing", "chat": {"id": 1}}, store)
    assert store.get_action_state(pid)["action"] == "save"
    assert "Saved" in replies[-1] and "south facing" in replies[-1]

    bot.process_message({"text": "/shortlist", "chat": {"id": 1}}, store)
    assert format_bw_id(pid) in replies[-1]
    assert "8.300 kr" in replies[-1]

    bot.process_message({"text": f"/applied BW-{pid}", "chat": {"id": 1}}, store)
    assert store.get_action_state(pid)["action"] == "applied"

    bot.process_message({"text": f"/unsave {format_bw_id(pid)}", "chat": {"id": 1}}, store)
    assert store.get_action_state(pid) is None


def test_dismiss_removes_from_latest(bot_env):
    store, pid, replies = bot_env

    bot.process_message({"text": "/latest", "chat": {"id": 1}}, store)
    assert any(format_bw_id(pid) in r for r in replies)

    replies.clear()
    bot.process_message({"text": f"/dismiss {pid} too far", "chat": {"id": 1}}, store)
    assert store.get_action_state(pid)["action"] == "dismiss"

    replies.clear()
    bot.process_message({"text": "/latest", "chat": {"id": 1}}, store)
    assert not any(format_bw_id(pid) in r for r in replies)
    assert store.get_matches() == []


def test_details_id_parsing(temp_db):
    store = temp_db
    p_id = store._find_or_create_property_direct("Valby Langgade 5", 55.66, 12.51, 1, 32.0,
                                                 "new", "2026-09-01")
    bw_id = format_bw_id(p_id)
    store.conn.execute(
        "INSERT INTO matches(property_id, bw_id, category, score, rent_total) VALUES(?,?,1,90,8000)",
        (p_id, bw_id))
    store.conn.commit()

    for probe in (bw_id, f"BW-{p_id}", str(p_id)):
        found = store.get_property_by_bw_id(probe)
        assert found is not None
        assert found["property_id"] == p_id


def test_unauthorized_chat_id_rejected(monkeypatch, temp_db):
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123456789")
    assert bot.is_authorized("123456789") is True
    assert bot.is_authorized(123456789) is True
    assert bot.is_authorized("999999999") is False

    replies = []
    monkeypatch.setattr(bot, "send_reply", lambda chat_id, text, **kw: replies.append(text))
    bot.process_message({"text": "/summary", "chat": {"id": 999999999}}, temp_db)
    assert len(replies) == 0


def test_value_label_is_rebuilt_from_a_stored_row():
    """matches stores value_ratio, not the label, so /latest must derive it."""
    stored = _match(value_label=None, value_ratio=0.73)
    assert notify.value_label_of(stored) == "-27% vs market"
    assert "268 kr/m2 (-27% vs market)" not in notify.fmt_value_line(stored)
    assert "(-27% vs market)" in notify.fmt_value_line(stored)

    no_size = _match(value_label=None, value_ratio=None, kr_per_m2=None)
    assert notify.value_label_of(no_size) == "size unknown"

    no_bench = _match(value_label=None, value_ratio=None)
    assert notify.value_label_of(no_bench) == "no market benchmark"


# ==============================================================================
# Spec 09 sections 10.6 and 12.4 - the v38u1 row's notification compatibility:
# the migration sends nothing, an explicit-gone listing leaves the digest and the
# bot commands, and personal priority never becomes a claim about quality
# ==============================================================================

def test_acceptance_28_the_migration_suppresses_its_backlog_and_alerts_nothing(alert_env):
    """Acceptance 28: migration-created matches are marked already alerted, a
    migration sends zero messages, and a genuinely new match still alerts."""
    store, sent, stations = alert_env
    first = _candidate(listing_id="V38-MIG-1", address="Old Match Vej 1, 2300 København")
    assert run.process_candidate(first, stations, store, None, dry_run=True) is True
    assert store.get_matches(), "the migration-created match exists before the scan"

    # The migration's own suppression step (spec 08 section 6.4's one UPDATE).
    assert store.suppress_alert_backlog("2026-09-16T08:48:00") == 1
    row = store.get_matches()[0]
    assert row["alerted_actionable"] == 1
    assert row["last_alert_at"] == "2026-09-16T08:48:00"
    assert store.suppress_alert_backlog("2026-09-16T08:48:00") == 0, "idempotent"

    # Re-seeing the old match on a live scan sends nothing at all.
    live_first = _candidate(listing_id="V38-MIG-1", address="Old Match Vej 1, 2300 København")
    assert run.process_candidate(live_first, stations, store, None, dry_run=False) is False
    assert sent == [], "a suppressed migration match produces no message"

    # A genuinely new post-release match keeps the normal behaviour: one message.
    second = _candidate(listing_id="V38-NEW-2", address="New Match Vej 2, 2300 København")
    second["lat"], second["lon"] = 55.67, 12.55
    assert run.process_candidate(second, stations, store, None, dry_run=False) is True
    assert len(sent) == 1, "a new match still alerts exactly once"
    prefix, payload = sent[0]
    assert prefix == "" and "New Match Vej 2" in notify.fmt_brief_alert(payload)


def test_an_explicitly_gone_listing_leaves_the_digest_and_the_latest_command(alert_env, monkeypatch):
    """Duty 4 / section 10.6: the digest and /latest read the same Ranked
    representatives, and an explicitly gone advert is not one of them."""
    store, _sent, stations = alert_env
    listing_id = "V38-GONE-1"
    rec = _candidate(listing_id=listing_id, address="Gonevej 7, 2300 København")
    run.scan_card(rec, stations, store, scan_run_id=None, dry_run=True, segments={},
                  refresh_features=False, detail_fetcher=lambda page, row: row)

    before = digest.generate_daily_digest(store, segments={})
    assert "Gonevej 7" in before, "the ranked listing is in the digest while it is live"

    outcome = run.scan_card(
        rec, stations, store, scan_run_id=None, dry_run=True, segments={},
        refresh_features=False, force_detail=True,
        detail_fetcher=lambda page, row: {**row, "eligibility_text": "Beklager, men boligen er udlejet."})
    assert outcome["detail_state"] == "failed:gone"
    assert store.get_source_listing("test", listing_id)["explicit_gone_at"], \
        "the own detail page's gone marker is stored"
    assert store.get_matches() == [], "the representative no longer holds a match row"

    after = digest.generate_daily_digest(store, segments={})
    assert "Gonevej 7" not in after, "an explicit-gone listing is not in the digest"
    assert "Actionable 0" in after

    replies = []
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(bot, "send_reply",
                        lambda chat_id, text, **kw: replies.append(text))
    bot.process_message({"text": "/latest", "chat": {"id": 1}}, store)
    assert replies, "the command answers"
    assert not any("Gonevej 7" in text for text in replies), \
        "an explicit-gone listing is not served by the bot either"
    assert "No confirmed matches found" in replies[0]


def test_personal_priority_never_reads_as_objective_quality_in_the_copy(alert_env, monkeypatch):
    """Duty 4 / section 10.6: the preference adds no message, no line and no
    quality claim to the digest or to the alert copy."""
    store, sent, stations = alert_env
    rec = _candidate(listing_id="V38-PREF-1", address="Preferencevej 9, 2300 København")
    assert run.process_candidate(rec, stations, store, None, dry_run=True) is True

    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "1")
    digest.send_daily_digest(store, live=True)
    assert len(sent) == 1
    before_text = sent[0]
    before_row = store.get_matches(order="score")[0]
    assert "Preferencevej 9" in before_text

    store.set_rank_priority(before_row["property_id"], 1,
                            kind=viewer.PREFERRED_LOCALITY_KIND,
                            note="Nicer locality and good value")
    sent.clear()
    digest.send_daily_digest(store, live=True)
    assert len(sent) == 1, "the preference adds no message"
    assert sent[0] == before_text, "the preference changes no line of the digest"

    after_row = store.get_matches(order="score")[0]
    assert after_row["rank_priority_kind"] == viewer.PREFERRED_LOCALITY_KIND
    for formatter in (notify.fmt_brief_alert, notify.fmt_details_message):
        assert formatter(after_row) == formatter(before_row), \
            "the preference is not a fact the alert copy reports"
    for text in (before_text, notify.fmt_brief_alert(after_row),
                 notify.fmt_details_message(after_row)):
        assert "preferred" not in text.lower(), \
            "the preference is never phrased as objective quality"
        assert viewer.PREFERRED_LOCALITY_LABEL not in text, \
            "the dashboard badge is not an alert or digest message type"
