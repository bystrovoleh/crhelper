"""
Auto-trading scheduler.

Runs IntradayAgent on all configured assets every INTERVAL_MINUTES.
For each signal with has_setup=True, applies order_manager rules.
Sends Telegram notifications for trades placed.
"""

import time
import signal
import sys
from datetime import datetime, timezone

from config.settings import ASSETS
from intraday_agent.analyzer import IntradayAgent
from trading.order_manager import process_signal
from telegram_bot.sender import send_intraday_signal, _send

# How often to run the full scan (minutes)
INTERVAL_MINUTES = 25

_running = True


def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _signal_handler(sig, frame):
    global _running
    print("\n[scheduler] Stopping...")
    _running = False


def _send_trade_notification(signal: dict, order_result: dict):
    """Send Telegram message when an order is actually placed."""
    symbol = signal.get("symbol", "?")
    direction = signal.get("direction", "?")
    dir_label = "🟢 LONG" if direction == "long" else "🔴 SHORT"
    orders = order_result.get("orders_placed", [])
    cancelled = order_result.get("orders_cancelled", 0)

    lines = [
        f"<b>🤖 AUTO TRADE: {symbol} — {dir_label}</b>",
        f"Entry1: <b>{signal.get('entry1')}</b>",
    ]
    if signal.get("entry2"):
        lines.append(f"Entry2: <b>{signal.get('entry2')}</b>")
    lines += [
        f"SL: <b>{signal.get('sl')}</b>",
        f"TP1: <b>{signal.get('tp1')}</b>",
        f"RR: <b>{signal.get('risk_reward')}</b>  |  Conf: {signal.get('confidence')}",
        f"Orders placed: {len(orders)}",
    ]
    if cancelled:
        lines.append(f"<i>({cancelled} stale order(s) cancelled before placing)</i>")

    sess = (signal.get("session_analysis") or {}).get("current_session", "?")
    lines.append(f"Session: {sess}")

    _send("\n".join(lines))


def run_once(agent: IntradayAgent, dry_run: bool = False) -> list[dict]:
    """Run one full scan cycle. Returns list of order results."""
    print(f"\n{'='*60}")
    print(f"[scheduler] Scan started — {_now()}")
    print(f"{'='*60}")

    results = []
    for asset in ASSETS:
        print(f"\n[{asset}]")
        try:
            sig = agent.analyze(asset)

            if sig.get("has_setup"):
                print(f"  ★ {sig.get('direction').upper()} entry={sig.get('entry1')} "
                      f"sl={sig.get('sl')} tp1={sig.get('tp1')} RR={sig.get('risk_reward')}")

                order_result = process_signal(sig, dry_run=dry_run)
                print(f"  → {order_result['action']}: {order_result['reason']}")

                if order_result["action"] == "orders_placed" and order_result["orders_placed"]:
                    _send_trade_notification(sig, order_result)

                results.append({"symbol": asset, "signal": sig, "order_result": order_result})
            else:
                watch = sig.get("watch_level")
                watch_str = f"  👁 watch={watch}" if watch else ""
                reason = (sig.get("reasoning") or "")[:60]
                print(f"  ─ no setup  {watch_str}")
                print(f"    {reason}")

        except Exception as e:
            print(f"  [error] {asset}: {e}")

        time.sleep(1.0)  # small pause between assets

    print(f"\n[scheduler] Scan done — {_now()}")
    return results


def run(dry_run: bool = False):
    """
    Main loop. Runs forever until Ctrl+C.
    dry_run=True logs what would happen without placing real orders.
    """
    global _running

    # Graceful shutdown on Ctrl+C
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"\n[scheduler] Starting auto-trader ({mode})")
    print(f"[scheduler] Assets: {ASSETS}")
    print(f"[scheduler] Interval: {INTERVAL_MINUTES} min")
    print(f"[scheduler] Press Ctrl+C to stop\n")

    _send(f"🤖 Auto-trader started ({mode})\nAssets: {', '.join(ASSETS)}\nInterval: {INTERVAL_MINUTES}min")

    agent = IntradayAgent()

    while _running:
        try:
            run_once(agent, dry_run=dry_run)
        except Exception as e:
            print(f"[scheduler] Scan error: {e}")

        if not _running:
            break

        # Wait for next cycle
        print(f"\n[scheduler] Next scan in {INTERVAL_MINUTES} min...")
        for _ in range(INTERVAL_MINUTES * 60):
            if not _running:
                break
            time.sleep(1)

    _send("🛑 Auto-trader stopped.")
    print("[scheduler] Stopped.")
