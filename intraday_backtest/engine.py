"""
Intraday backtest engine.

Steps through historical dates at a given interval (e.g. every 4 hours),
builds historical intraday snapshots, runs IntradayAgent, then evaluates
whether the signal's TP/SL was hit within INTRADAY_EVAL_HOURS hours.

Usage:
    engine = IntradayBacktestEngine()
    results = engine.run("BTCUSDT", "2024-01-01", "2024-01-31", step_hours=4)
"""

import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field

from data.mexc_client import MEXCClient
from intraday_agent.analyzer import IntradayAgent
from intraday_agent.indicators import compute_intraday_indicators
from config.settings import (
    INTRADAY_TIMEFRAMES, INTRADAY_CANDLE_LIMITS,
    INTRADAY_EVAL_HOURS, INTRADAY_MAX_ENTRY_WAIT_HOURS,
)


@dataclass
class IntradaySignalResult:
    signal_datetime: str
    symbol: str
    has_setup: bool
    direction: str | None
    entry1: float | None
    sl: float | None
    tp1: float | None
    tp2: float | None
    risk_reward: float | None
    confidence: str | None
    session: str | None
    reasoning: str | None
    outcome: str | None          # "tp1_hit" | "tp2_hit" | "sl_hit" | "open" | "no_setup" | "missed_entry"
    close_price: float | None
    close_datetime: str | None
    hours_to_close: float | None


def _build_intraday_snapshot_at(
    client: MEXCClient,
    symbol: str,
    dt: datetime,
) -> dict:
    """
    Build a historical intraday snapshot as of datetime dt.
    Fetches H4, H1, M15, M5 candles ending at dt.
    Note: orderbook/trades are live-only — filled with None in backtest.
    """
    snapshot = {
        "symbol": symbol,
        "timestamp": int(dt.timestamp()),
        "candles": {},
        "ticker": None,
        "open_interest": None,
        "funding_rate": None,
        "long_short_ratio": None,
        "orderbook": None,        # not available in backtest
        "recent_trades": None,    # not available in backtest
    }

    tf_config = {
        "h4":  ("Hour4", 120 * 4 * 3600,  120),
        "h1":  ("Min60",  96 * 3600,        96),
        "m15": ("Min15", 192 * 15 * 60,   192),
        "m5":  ("Min5",  288 *  5 * 60,   288),
    }

    end_ts = int(dt.timestamp())

    for name, (interval, lookback_sec, limit) in tf_config.items():
        start_ts = end_ts - lookback_sec
        try:
            from_dt_tf = datetime.fromtimestamp(start_ts, tz=timezone.utc)
            to_dt_tf   = datetime.fromtimestamp(end_ts,   tz=timezone.utc)
            candles = client.get_historical_candles(symbol, interval, from_dt_tf, to_dt_tf)
            snapshot["candles"][name] = [
                c for c in candles if c["timestamp"] <= end_ts
            ]
        except Exception as e:
            snapshot["candles"][name] = []
            print(f"    [warn] backtest candles {name} @ {dt}: {e}")
        time.sleep(0.2)

    # Build ticker from M15 → H1 → H4
    ticker_candle = None
    for tf in ("m15", "h1", "h4"):
        tf_candles = snapshot["candles"].get(tf, [])
        if tf_candles:
            ticker_candle = tf_candles[-1]
            break

    if ticker_candle:
        snapshot["ticker"] = {
            "last_price": ticker_candle["close"],
            "funding_rate": 0,
            "hold_vol": 0,
            "volume_24h": 0,
            "high_24h": ticker_candle["high"],
            "low_24h": ticker_candle["low"],
            "timestamp": ticker_candle["timestamp"],
        }
        snapshot["funding_rate"] = {"funding_rate": 0}
        snapshot["long_short_ratio"] = {"long_ratio": 0.5, "short_ratio": 0.5}

    return snapshot


def _get_eval_candles(
    client: MEXCClient,
    symbol: str,
    from_dt: datetime,
    eval_hours: int,
) -> list[dict]:
    """Fetch M15 candles for the evaluation window after signal."""
    to_dt = from_dt + timedelta(hours=eval_hours)
    try:
        candles = client.get_historical_candles(symbol, "Min15", from_dt, to_dt)
        return [c for c in candles if c["timestamp"] > int(from_dt.timestamp())]
    except Exception as e:
        print(f"    [warn] eval candles: {e}")
        return []


def _evaluate_signal(
    eval_candles: list[dict],
    signal: dict,
) -> tuple[str, float | None, str | None, float | None]:
    """
    Walk through M15 eval candles to check if TP or SL was hit first.
    Returns (outcome, close_price, close_datetime, hours_to_close).
    """
    if not eval_candles:
        return "open", None, None, None

    entry = signal.get("entry1")
    sl = signal.get("sl")
    tp1 = signal.get("tp1")
    tp2 = signal.get("tp2")
    direction = signal.get("direction")

    if not entry or not sl or not tp1 or not direction:
        return "open", None, None, None

    signal_ts = eval_candles[0]["timestamp"] if eval_candles else 0

    # First check: wait for entry activation (up to INTRADAY_MAX_ENTRY_WAIT_HOURS)
    max_entry_wait_ts = signal_ts + INTRADAY_MAX_ENTRY_WAIT_HOURS * 3600
    entry_activated = False
    entry_ts = None

    for c in eval_candles:
        if c["timestamp"] > max_entry_wait_ts and not entry_activated:
            return "missed_entry", None, None, None

        if not entry_activated:
            if direction == "long" and c["low"] <= entry:
                entry_activated = True
                entry_ts = c["timestamp"]
            elif direction == "short" and c["high"] >= entry:
                entry_activated = True
                entry_ts = c["timestamp"]

        if not entry_activated:
            continue

        # Check SL hit
        if direction == "long" and c["low"] <= sl:
            hours = (c["timestamp"] - signal_ts) / 3600
            return "sl_hit", sl, c["datetime"], round(hours, 1)
        if direction == "short" and c["high"] >= sl:
            hours = (c["timestamp"] - signal_ts) / 3600
            return "sl_hit", sl, c["datetime"], round(hours, 1)

        # Check TP2 first (better outcome)
        if tp2:
            if direction == "long" and c["high"] >= tp2:
                hours = (c["timestamp"] - signal_ts) / 3600
                return "tp2_hit", tp2, c["datetime"], round(hours, 1)
            if direction == "short" and c["low"] <= tp2:
                hours = (c["timestamp"] - signal_ts) / 3600
                return "tp2_hit", tp2, c["datetime"], round(hours, 1)

        # Check TP1
        if direction == "long" and c["high"] >= tp1:
            hours = (c["timestamp"] - signal_ts) / 3600
            return "tp1_hit", tp1, c["datetime"], round(hours, 1)
        if direction == "short" and c["low"] <= tp1:
            hours = (c["timestamp"] - signal_ts) / 3600
            return "tp1_hit", tp1, c["datetime"], round(hours, 1)

    return "open", None, None, None


def _compute_metrics(results: list[IntradaySignalResult]) -> dict:
    """Aggregate backtest statistics."""
    setups = [r for r in results if r.has_setup]
    no_setups = [r for r in results if not r.has_setup]
    missed = [r for r in setups if r.outcome == "missed_entry"]
    activated = [r for r in setups if r.outcome != "missed_entry"]

    wins = [r for r in activated if r.outcome in ("tp1_hit", "tp2_hit")]
    losses = [r for r in activated if r.outcome == "sl_hit"]
    open_trades = [r for r in activated if r.outcome == "open"]

    win_rate = round(len(wins) / len(activated) * 100, 1) if activated else 0
    rr_values = [r.risk_reward for r in activated if r.risk_reward]
    avg_rr = round(sum(rr_values) / len(rr_values), 2) if rr_values else 0

    avg_hours = round(
        sum(r.hours_to_close for r in activated if r.hours_to_close) /
        max(len([r for r in activated if r.hours_to_close]), 1), 1
    )

    # By direction
    by_dir = {}
    for direction in ("long", "short"):
        dir_trades = [r for r in activated if r.direction == direction]
        dir_wins = [r for r in dir_trades if r.outcome in ("tp1_hit", "tp2_hit")]
        dir_losses = [r for r in dir_trades if r.outcome == "sl_hit"]
        by_dir[direction] = {
            "total": len(dir_trades),
            "wins": len(dir_wins),
            "losses": len(dir_losses),
            "open": len([r for r in dir_trades if r.outcome == "open"]),
            "win_rate": round(len(dir_wins) / len(dir_trades) * 100, 1) if dir_trades else 0,
        }

    # By session
    by_session = {}
    for r in activated:
        sess = r.session or "unknown"
        if sess not in by_session:
            by_session[sess] = {"wins": 0, "losses": 0, "open": 0}
        if r.outcome in ("tp1_hit", "tp2_hit"):
            by_session[sess]["wins"] += 1
        elif r.outcome == "sl_hit":
            by_session[sess]["losses"] += 1
        else:
            by_session[sess]["open"] += 1

    return {
        "total_steps": len(results),
        "setups_found": len(setups),
        "no_setup": len(no_setups),
        "missed_entry": len(missed),
        "activated_trades": len(activated),
        "wins": len(wins),
        "losses": len(losses),
        "open": len(open_trades),
        "win_rate_pct": win_rate,
        "avg_risk_reward": avg_rr,
        "avg_hours_to_close": avg_hours,
        "by_direction": by_dir,
        "by_session": by_session,
    }


class IntradayBacktestEngine:
    """
    Backtest engine for the IntradayAgent.
    Steps through time at step_hours intervals, runs the agent,
    evaluates outcomes on subsequent M15 candles.
    """

    def __init__(self, debug: bool = False):
        self.client = MEXCClient()
        self.agent = IntradayAgent()
        self.debug = debug

    def run(
        self,
        symbol: str,
        date_from: str,
        date_to: str,
        step_hours: int = 4,
    ) -> dict:
        """
        Run backtest.
        Args:
            symbol:     e.g. "BTCUSDT"
            date_from:  "YYYY-MM-DD"
            date_to:    "YYYY-MM-DD"
            step_hours: how often to run the agent (default: every 4h)
        Returns:
            dict with all signal results and aggregate metrics.
        """
        from_dt = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        to_dt = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        results: list[IntradaySignalResult] = []
        current_dt = from_dt
        step = 0

        print(f"\nIntraday backtest: {symbol} | {date_from} → {date_to} | step={step_hours}h")
        print(f"Eval window: {INTRADAY_EVAL_HOURS}h | Entry wait: {INTRADAY_MAX_ENTRY_WAIT_HOURS}h")

        # MEXC stores M15 for ~12 months. Cutoff is approximately 13 months ago.
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=390)
        if from_dt < cutoff:
            print(f"  [!] WARNING: M15 data unavailable before ~{cutoff.date()} on MEXC.")
            print(f"      Use dates from {cutoff.date()} onwards for reliable results.\n")
            return {"symbol": symbol, "date_from": date_from, "date_to": date_to,
                    "step_hours": step_hours, "metrics": {}, "signals": [],
                    "error": f"M15 data unavailable before {cutoff.date()}"}
        print()

        while current_dt < to_dt:
            step += 1
            date_label = current_dt.isoformat()
            print(f"\n[{step}] {date_label[:16]}")

            try:
                snapshot = _build_intraday_snapshot_at(self.client, symbol, current_dt)
                signal = self.agent.analyze_with_snapshot(symbol, snapshot, date_label=date_label, debug=self.debug)
            except Exception as e:
                print(f"  [error] agent failed: {e}")
                current_dt += timedelta(hours=step_hours)
                continue

            session = (signal.get("session_analysis") or {}).get("current_session", "?")

            if not signal.get("has_setup"):
                reason = (signal.get("reasoning") or "")[:60]
                print(f"  ─  no setup  [{session}]  {reason}")
                results.append(IntradaySignalResult(
                    signal_datetime=date_label,
                    symbol=symbol,
                    has_setup=False,
                    direction=None,
                    entry1=None, sl=None, tp1=None, tp2=None,
                    risk_reward=None, confidence=None,
                    session=session,
                    reasoning=signal.get("reasoning"),
                    outcome="no_setup",
                    close_price=None, close_datetime=None, hours_to_close=None,
                ))
                current_dt += timedelta(hours=step_hours)
                continue

            # Setup found — log it visibly
            direction = signal.get("direction", "?")
            dir_arrow = "▲ LONG " if direction == "long" else "▼ SHORT"
            print(
                f"  ★  {dir_arrow}  entry={signal.get('entry1')}  "
                f"sl={signal.get('sl')}  tp1={signal.get('tp1')}  "
                f"RR={signal.get('risk_reward')}  conf={signal.get('confidence')}  [{session}]"
            )

            # Evaluate the signal
            try:
                eval_candles = _get_eval_candles(self.client, symbol, current_dt, INTRADAY_EVAL_HOURS)
                outcome, close_price, close_dt, hours = _evaluate_signal(eval_candles, signal)
            except Exception as e:
                print(f"    [error] evaluation failed: {e}")
                outcome, close_price, close_dt, hours = "open", None, None, None

            # Outcome log
            outcome_icons = {
                "tp1_hit": "✓ TP1",
                "tp2_hit": "✓ TP2",
                "sl_hit":  "✗ SL ",
                "missed_entry": "~ MISS",
                "open":    "? OPEN",
            }
            icon = outcome_icons.get(outcome, outcome)
            hours_str = f" in {hours}h" if hours else ""
            close_str = f" @ {close_price}" if close_price else ""
            print(f"         └─ {icon}{close_str}{hours_str}")

            results.append(IntradaySignalResult(
                signal_datetime=date_label,
                symbol=symbol,
                has_setup=True,
                direction=signal.get("direction"),
                entry1=signal.get("entry1"),
                sl=signal.get("sl"),
                tp1=signal.get("tp1"),
                tp2=signal.get("tp2"),
                risk_reward=signal.get("risk_reward"),
                confidence=signal.get("confidence"),
                session=session,
                reasoning=signal.get("reasoning"),
                outcome=outcome,
                close_price=close_price,
                close_datetime=close_dt,
                hours_to_close=hours,
            ))

            current_dt += timedelta(hours=step_hours)
            time.sleep(0.3)  # rate limit

        metrics = _compute_metrics(results)
        return {
            "symbol": symbol,
            "date_from": date_from,
            "date_to": date_to,
            "step_hours": step_hours,
            "metrics": metrics,
            "signals": [r.__dict__ for r in results],
        }
