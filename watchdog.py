#!/usr/bin/env python3
"""Telegram alerts for a scan that crashed or stopped producing data.

The in-scan portal incidents in run.py only fire once the browser is up. A crash
before that (a missing Chromium build, a broken venv) used to fail silently for
days, so this runs outside the scan:

  watchdog.py --failed   called by systemd OnFailure= when boligwatch-scan.service fails
  watchdog.py --stale    called after the daily digest; alerts if no complete scan
                         of lejebolig or boligportal has started within STALE_HOURS
"""
import argparse
import datetime
import html
import os
import re
import sqlite3
import sys

from profile import prescan_profile
prescan_profile()

import config
import notify

STALE_HOURS = 26
LOG_TAIL_LINES = 400
_ERROR_RE = re.compile(r"^\w*(Error|Exception)\b.*|^[\w.]+(Error|Exception):.*")


def last_error_line() -> str:
    try:
        with open(os.path.join(config.BASE_DIR, "boligwatch.log"), errors="replace") as f:
            tail = f.readlines()[-LOG_TAIL_LINES:]
    except OSError:
        return "log unreadable"
    for line in reversed(tail):
        line = line.strip()
        if _ERROR_RE.match(line):
            return line[:300]
    return "no error line found in the log tail"


def alert_failed() -> int:
    msg = ("<b>BoligWatch scan FAILED</b>\n"
           "boligwatch-scan.service exited with an error, so this scan found nothing.\n"
           f"Last error: <code>{html.escape(last_error_line())}</code>\n"
           "Log: boligwatch.log")
    notify.send_text(msg, parse_html=True)
    return 0


def alert_if_stale() -> int:
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        (last,) = con.execute(
            "select max(started_at) from scan_runs "
            "where site in ('lejebolig','boligportal') and coverage_status='complete'"
        ).fetchone()
    finally:
        con.close()
    now = datetime.datetime.now()
    age = (now - datetime.datetime.fromisoformat(last)) if last else None
    if age is not None and age < datetime.timedelta(hours=STALE_HOURS):
        print(f"[watchdog] ok: last complete scan {last}")
        return 0
    since = f"last complete scan started {last}" if last else "no complete scan on record"
    notify.send_text(
        f"<b>BoligWatch is STALE</b>\nNo complete scan in over {STALE_HOURS} hours "
        f"({html.escape(since)}). Check boligwatch-scan.timer and boligwatch.log.",
        parse_html=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--failed", action="store_true")
    g.add_argument("--stale", action="store_true")
    args = ap.parse_args()
    return alert_failed() if args.failed else alert_if_stale()


if __name__ == "__main__":
    sys.exit(main())
