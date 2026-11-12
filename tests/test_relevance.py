"""Timeline relevance: which listings are actionable for the 1 Feb 2027 move."""
import datetime

import pytest

import config
import filters
import geo
import notify
import run
from conftest import with_detail_evidence
from store import format_bw_id

TARGET = datetime.date.fromisoformat(config.TARGET_MOVE_DATE)          # 2027-02-01
START = TARGET - datetime.timedelta(days=config.ACTIONABLE_BEFORE_DAYS)  # 2026-12-18
END = TARGET + datetime.timedelta(days=config.ACTIONABLE_AFTER_DAYS)     # 2027-03-03
UNKNOWN_LIVE = TARGET - datetime.timedelta(days=config.UNKNOWN_LIVE_BEFORE_DAYS)  # 2026-11-18

TODAY = datetime.date(2026, 9, 6)


def _rel(avail, today=TODAY, avail_str=None):
    date = datetime.date.fromisoformat(avail) if avail else None
    return filters.relevance_for(date, avail_str if avail_str is not None else avail, today)


def test_window_boundaries():
    assert START == datetime.date(2026, 12, 18)
    assert END == datetime.date(2027, 3, 3)
    assert UNKNOWN_LIVE == datetime.date(2026, 11, 18)


# The seven cases of spec section 7, with today frozen at 6 September 2026.

def test_case_1_date_inside_the_window_is_actionable_today():
    assert _rel("2027-01-15") == "actionable"
    assert _rel("2027-01-20") == "actionable"


def test_case_2_window_edges_are_inclusive():
    assert _rel(START.isoformat()) == "actionable"
    assert _rel(END.isoformat()) == "actionable"


def test_case_3_just_outside_the_window_is_intel():
    assert _rel((START - datetime.timedelta(days=1)).isoformat()) == "intel"
    assert _rel((END + datetime.timedelta(days=1)).isoformat()) == "intel"


def test_case_4_a_september_2026_listing_is_intel_today():
    assert _rel("2026-10-01") == "intel"


def test_case_5_unknown_availability_is_intel_before_18_november_2026():
    assert _rel(None) == "intel"
    assert _rel("2027-01-15", avail_str="snarest") == "intel"
    assert _rel(None, today=UNKNOWN_LIVE - datetime.timedelta(days=1)) == "intel"


def test_case_6_unknown_availability_goes_live_on_18_november_2026():
    assert _rel(None, today=UNKNOWN_LIVE) == "actionable"
    assert _rel(None, today=UNKNOWN_LIVE + datetime.timedelta(days=30)) == "actionable"


def test_case_7_a_past_date_counts_as_unknown():
    """A date already gone tells us nothing; it follows the unknown rule."""
    assert _rel("2026-08-01") == "intel"
    assert _rel("2026-08-01", today=UNKNOWN_LIVE) == "actionable"


def test_after_the_target_date_the_horizon_takes_over():
    after = TARGET + datetime.timedelta(days=60)   # 2 April 2027
    assert _rel(None, today=after) == "actionable"
    assert _rel((after + datetime.timedelta(days=30)).isoformat(), today=after) == "actionable"
    assert _rel((after + datetime.timedelta(days=90)).isoformat(), today=after) == "intel"


# --- Alert policy ----------------------------------------------------------

def _seed_match(store, relevance, alerted=0, listing_id="L1"):
    now = "2026-09-06T10:00:00"
    pid = store._find_or_create_property_direct("Jorisvej 1a", 55.65, 12.60, 1, 32.0, "new", now)
    store.conn.execute("""
        INSERT INTO matches(property_id, bw_id, tier, category, score, rent_total,
                            commute_min, relevance, alerted_actionable, address, quarantined)
        VALUES(?,?,'A',1,78,8300,18,?,?,'Jorisvej 1a, 2300 København',0)
    """, (pid, format_bw_id(pid), relevance, alerted))
    store.record_source_listing(pid, "test", listing_id, "http://x")
    store.mark_seen("test", listing_id)
    store.conn.commit()
    return pid


def _candidate(available_from):
    return with_detail_evidence({
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
    })


@pytest.fixture
def alert_env(temp_db, monkeypatch, mock_stations):
    sent = []
    # Returning a status matters: run.py treats None as a failed send.
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


def test_actionable_listing_alerts_on_first_sight(alert_env):
    store, sent, stations = alert_env
    alerted = run.process_candidate(_candidate("2027-01-20"), stations, store, None,
                                    dry_run=False)
    assert alerted is True
    assert len(sent) == 1
    assert sent[0][0] == ""
    assert store.get_match_row(1)["alerted_actionable"] == 1


def test_intel_listing_is_stored_but_never_alerted(alert_env):
    store, sent, stations = alert_env
    alerted = run.process_candidate(_candidate("2026-10-01"), stations, store, None,
                                    dry_run=False)
    assert alerted is False
    assert sent == []
    row = store.get_match_row(1)
    assert row["relevance"] == "intel"
    assert row["alerted_actionable"] == 0


def test_a_stored_actionable_match_that_was_never_alerted_still_alerts(alert_env):
    """A --rescore can turn a stored listing actionable without alerting.

    already_seen alone would swallow that alert forever, so alerted_actionable
    is what decides.
    """
    store, sent, stations = alert_env
    pid = _seed_match(store, "actionable", alerted=0)

    run.process_candidate(_candidate("2027-01-20"), stations, store, None, dry_run=False)
    assert len(sent) == 1
    assert sent[0][0] == "Now actionable:"
    assert store.get_match_row(pid)["alerted_actionable"] == 1

    run.process_candidate(_candidate("2027-01-20"), stations, store, None, dry_run=False)
    assert len(sent) == 1, "the flag must make it exactly one alert"


def test_intel_to_actionable_transition_alerts_exactly_once(alert_env):
    store, sent, stations = alert_env
    pid = _seed_match(store, "intel", alerted=0)

    run.process_candidate(_candidate("2027-01-20"), stations, store, None, dry_run=False)
    assert len(sent) == 1
    assert sent[0][0] == "Now actionable:"
    assert store.get_match_row(pid)["alerted_actionable"] == 1

    # A second scan of the same still-actionable listing must stay silent.
    run.process_candidate(_candidate("2027-01-20"), stations, store, None, dry_run=False)
    assert len(sent) == 1


def test_dismissed_listing_is_never_alerted(alert_env):
    store, sent, stations = alert_env
    pid = _seed_match(store, "intel", alerted=0)
    store.record_user_action(pid, "dismiss", "too dark")

    run.process_candidate(_candidate("2027-01-20"), stations, store, None, dry_run=False)
    assert sent == []


def test_a_dry_run_does_not_burn_the_one_shot_alert(alert_env):
    """A dry run must leave the flag alone, or the real alert is lost."""
    store, sent, stations = alert_env

    assert run.process_candidate(_candidate("2027-01-20"), stations, store, None,
                                 dry_run=True) is True
    assert sent == [], "dry run must not send"
    assert store.get_match_row(1)["alerted_actionable"] == 0

    run.process_candidate(_candidate("2027-01-20"), stations, store, None, dry_run=False)
    assert len(sent) == 1, "the live scan must still deliver the alert"
    assert store.get_match_row(1)["alerted_actionable"] == 1


def test_a_failed_telegram_send_is_reported_and_retried(alert_env, monkeypatch, capsys):
    """An unchecked send would log a delivery that never happened."""
    store, sent, stations = alert_env
    monkeypatch.setattr(notify, "notify_match", lambda payload, prefix="": None)

    run.process_candidate(_candidate("2027-01-20"), stations, store, None, dry_run=False)
    out = capsys.readouterr().out
    assert "TELEGRAM SEND FAILED" in out
    assert "NOTIFIED ON TELEGRAM" not in out
    assert store.get_match_row(1)["alerted_actionable"] == 0, "a failed send must not burn the alert"

    # The next scan, with Telegram back, delivers it.
    monkeypatch.setattr(notify, "notify_match",
                        lambda payload, prefix="": sent.append((prefix, payload)) or 200)
    run.process_candidate(_candidate("2027-01-20"), stations, store, None, dry_run=False)
    assert len(sent) == 1
    assert store.get_match_row(1)["alerted_actionable"] == 1
