"""Telegram Bot interactive command listener for BoligWatch v3.2.

Commands supported:
- /details <id>: Full listing record, commute route, value, buzz, water,
  relevance and shortlist state. Accepts BW-0000045, BW-45 or 45.
- /latest [n]: Top n matches by rank.
- /save <id> [note] / /unsave <id>: shortlist a property and arm its price and
  lifecycle alerts.
- /dismiss <id> [reason]: hide it from alerts, digest and the dashboard.
- /applied <id>: mark as applied (implies saved).
- /shortlist: saved and applied properties with rent, last change and lifecycle.
- /summary: Generates the daily housing digest.
- /status: Shows portal health, scan metrics, quarantine counts, and database health.
- /search: Triggers a fresh on-demand scan with concurrency lock.
- /help: Lists available commands.

STRICT RULES:
1. Only text messages, NEVER photos or previews.
2. NO emojis.
3. Reject unauthorized chat IDs.
"""
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from profile import prescan_profile
prescan_profile()

import config
import notify
from store import Store, format_bw_id, parse_bw_id

_scan_lock = threading.Lock()
_is_scanning = False


def _call_tg(method: str, **params: Any) -> Optional[Dict[str, Any]]:
    if not config.TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception as e:
        if config.VERBOSE:
            print(f"[tg-bot] error calling {method}: {e}")
        return None


def send_reply(chat_id: Any, text: str, parse_html: bool = False):
    """Send text reply. Strictly no photos, no previews, no emojis."""
    kwargs = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }
    if parse_html:
        kwargs["parse_mode"] = "HTML"
    _call_tg("sendMessage", **kwargs)


def is_authorized(chat_id: Any) -> bool:
    """Check if sender chat ID matches configured chat ID."""
    if not config.TELEGRAM_CHAT_ID:
        return True
    return str(chat_id).strip() == str(config.TELEGRAM_CHAT_ID).strip()


def handle_details(chat_id: Any, store: Store, args: List[str]):
    if not args:
        send_reply(chat_id, "Usage: /details <BW-ID> (e.g. /details BW-0001842 or /details 1842)")
        return
    raw_id = args[0].strip()
    prop = store.get_property_by_bw_id(raw_id)
    if not prop:
        send_reply(chat_id, f"Listing '{raw_id}' not found.")
        return
    state = store.get_action_state(prop.get("property_id") or parse_bw_id(raw_id))
    if state:
        prop["shortlist_action"] = state["action"]
    msg = notify.fmt_details_message(prop)
    send_reply(chat_id, msg, parse_html=True)


def handle_latest(chat_id: Any, store: Store, args: List[str]):
    n = 5
    if args:
        try:
            n = min(15, max(1, int(args[0])))
        except ValueError:
            pass
    matches = store.grouped_matches(limit=n, order="score")
    if not matches:
        send_reply(chat_id, "No confirmed matches found in database.")
        return

    send_reply(chat_id, f"Showing top {len(matches)} matches by rank:")
    for m in matches:
        msg = notify.fmt_brief_alert(m)
        send_reply(chat_id, msg, parse_html=True)
        time.sleep(0.3)


_ACTION_REPLIES = {
    "save": "Saved {bw}. Price and lifecycle alerts are on for it.",
    "unsave": "Removed {bw} from the shortlist.",
    "dismiss": "Dismissed {bw}. It is now hidden from alerts, digest and dashboard.",
    "applied": "Marked {bw} as applied.",
}


def handle_action(chat_id: Any, store: Store, action: str, args: List[str]):
    """/save, /unsave, /dismiss and /applied all share this shape."""
    if not args:
        send_reply(chat_id, f"Usage: /{action} <id> (e.g. /{action} BW-0000045, /{action} 45)")
        return
    prop_id = parse_bw_id(args[0])
    if prop_id is None:
        send_reply(chat_id, f"Could not read an ID from '{args[0]}'.")
        return
    prop = store.get_property_by_bw_id(prop_id)
    if not prop:
        send_reply(chat_id, f"Listing '{args[0]}' not found.")
        return

    note = " ".join(args[1:]).strip()
    store.record_user_action(prop_id, action, note)
    bw = format_bw_id(prop_id)
    msg = _ACTION_REPLIES[action].format(bw=bw)
    if note:
        msg += f" Note: {note}"
    send_reply(chat_id, msg)


def handle_shortlist(chat_id: Any, store: Store):
    rows = store.get_shortlist()
    if not rows:
        send_reply(chat_id, "Shortlist is empty. Add one with /save <id>.")
        return

    lines = ["Shortlist"]
    for r in rows:
        bw = r.get("bw_id") or format_bw_id(r.get("property_id") or 0)
        addr = str(r.get("address") or "address unknown").split(",")[0].strip()
        parts = [f"{bw} {addr}", str(r.get("shortlist_action") or "saved")]
        if r.get("rent_total") is not None:
            parts.append(f"{notify.format_dkk(r.get('rent_total'))} kr")
        state = store.lifecycle_state_for(r.get("property_id"))
        if state:
            parts.append(state)
        if r.get("shortlist_note"):
            parts.append(str(r["shortlist_note"]))
        lines.append(" · ".join(parts))
    send_reply(chat_id, "\n".join(lines))


def handle_summary(chat_id: Any, store: Store):
    from digest import generate_daily_digest
    digest_text = generate_daily_digest(store)
    send_reply(chat_id, digest_text)


def handle_status(chat_id: Any, store: Store):
    from viewer import dashboard_stats
    stats = dashboard_stats(store)
    health = store.get_portal_health()
    rank_counts = stats.get("rank_counts") or {}
    rank_str = " / ".join(f"{l} {rank_counts.get(l, 0)}" for l in config.RANK_TIER_LETTERS)

    lines = [
        "<b>BoligWatch v3.2 — System Status</b>",
        "",
        f"• Total Observed Listings: {stats['total_seen']}",
        f"• Unique Physical Properties: {stats['total_properties']}",
        f"• Confirmed buildings/complexes: {stats['total_matches']}",
        f"   - Commute band A Close (<= {config.TIER_A_MAX_MIN} min): {stats['cat1_count']}",
        f"   - Commute band B Commutable (<= {config.TIER_B_MAX_MIN} min): {stats['cat2_count']}",
        f"   - Commute band C Far (<= {config.TIER_C_MAX_MIN} min): {stats['cat3_count']}",
        f"   - Rank S/A/B/C/D: {rank_str}",
        f"• Actionable: {stats['actionable_count']} · Intel: {stats['intel_count']}",
        f"• Quarantine Backlog: {stats['quarantine_count']} "
        f"(ungeocodable: {stats['ungeocodable_count']})",
        f"• Last Match Discovered: {stats['last_match_time'] or 'N/A'}",
        "",
        "<b>Portal Scan Health:</b>"
    ]

    for site, h in health.items():
        fails = h.get("consecutive_failures", 0)
        status_txt = "Healthy" if fails == 0 else f"Failing ({fails} consecutive)"
        last_s = h.get("last_success")
        last_s_time = last_s.get("completed_at", "Never") if last_s else "Never"
        lines.append(f"• {site.capitalize()}: {status_txt} (Last success: {last_s_time})")

    lines.append("")
    lines.append(f"• Engine State: {'Scanning in progress' if _is_scanning else 'Idle and operational'}")
    send_reply(chat_id, "\n".join(lines), parse_html=True)


def handle_search(chat_id: Any):
    global _is_scanning
    with _scan_lock:
        if _is_scanning:
            send_reply(chat_id, "A scan is already running. Please wait for it to finish.")
            return
        _is_scanning = True

    send_reply(chat_id, "Triggering immediate on-demand scan across all portals...")

    def _run_scan_thread():
        global _is_scanning
        try:
            import subprocess
            subprocess.run([sys.executable, "run.py", "--live"], cwd=config.BASE_DIR, check=True)
            send_reply(chat_id, "On-demand scan completed successfully.")
        except Exception as e:
            send_reply(chat_id, f"On-demand scan encountered an error: {e}")
        finally:
            with _scan_lock:
                _is_scanning = False

    threading.Thread(target=_run_scan_thread, daemon=True).start()


def handle_help(chat_id: Any):
    help_text = (
        "<b>BoligWatch v3.2 Bot Commands:</b>\n\n"
        "• /details &lt;id&gt; — Full record: commute, value, street, water, relevance\n"
        "• /latest [n] — Top n matches by rank\n"
        "• /save &lt;id&gt; [note] — Shortlist it and arm price and lifecycle alerts\n"
        "• /unsave &lt;id&gt; — Remove it from the shortlist\n"
        "• /applied &lt;id&gt; — Mark as applied (implies saved)\n"
        "• /dismiss &lt;id&gt; [reason] — Hide it from alerts, digest and dashboard\n"
        "• /shortlist — Saved and applied properties with rent and status\n"
        "• /summary — Show today's housing digest\n"
        "• /status — Show scan health, portal status, and quarantine backlog\n"
        "• /search — Request an immediate scan\n"
        "• /help — Show this menu"
    )
    send_reply(chat_id, help_text, parse_html=True)


def process_message(msg: Dict[str, Any], store: Store):
    """Process a single incoming Telegram message."""
    if not msg or "text" not in msg:
        return
    text = msg["text"].strip()
    chat_id = msg["chat"]["id"]

    # Strict chat ID authorization
    if not is_authorized(chat_id):
        if config.VERBOSE:
            print(f"[bot] Ignored command from unauthorized chat_id: {chat_id}")
        return

    parts = text.split()
    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]  # Strip @botname if present
    cmd_args = parts[1:]

    if cmd == "/details":
        handle_details(chat_id, store, cmd_args)
    elif cmd in ("/save", "/unsave", "/dismiss", "/applied"):
        handle_action(chat_id, store, cmd.lstrip("/"), cmd_args)
    elif cmd == "/shortlist":
        handle_shortlist(chat_id, store)
    elif cmd == "/latest":
        handle_latest(chat_id, store, cmd_args)
    elif cmd in ("/summary", "/digest"):
        handle_summary(chat_id, store)
    elif cmd == "/status":
        handle_status(chat_id, store)
    elif cmd == "/search":
        handle_search(chat_id)
    elif cmd in ("/help", "/start"):
        handle_help(chat_id)


def poll_updates():
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        print("Telegram token or chat ID missing in config/secrets. Bot polling inactive.")
        return

    print("BoligWatch v2.1 Telegram Bot listener started...")
    offset = 0
    store = Store()

    while True:
        try:
            res = _call_tg("getUpdates", offset=offset, timeout=20)
            if res and res.get("ok"):
                for upd in res.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("channel_post")
                    if msg:
                        process_message(msg, store)
        except Exception as e:
            if config.VERBOSE:
                print(f"[bot-poll] error: {e}")
            time.sleep(3)
        time.sleep(1)


if __name__ == "__main__":
    poll_updates()
