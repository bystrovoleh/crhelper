"""
Auto Teacher
============
Generates high-quality RAG examples from historical price data by using
an LLM agent that sees BOTH the past and future candles (oracle mode).

The teacher is NOT a trading agent — it is a pattern extractor.
It looks at a window of candles [T-lookback ... T+lookahead] and identifies
the best structural trade that occurred in that window.

Usage (via CLI):
    python main.py teach BTC 2020-10-01 2021-04-01 --phase bull
    python main.py teach BTC 2021-11-01 2022-06-01 --phase bear --lookahead 14
    python main.py teach BTC 2023-01-01 2023-10-01 --phase accumulation --min-move 10

Design principles:
- Completely isolated from TradingAgent and ExitAgent
- Saves to same examples.db with source='auto' and market_phase tag
- Can be run multiple times for different phases — no overlap risk (deduplicated by date+asset)
- Lookahead and min-move are configurable per run
- Only saves examples where price moved >= min_move_pct AND RR >= 1.5
"""

import json
import time as _time
from datetime import datetime, timezone, timedelta

from data.mexc_client import MEXCClient
from agent.llm import LLMClient
from data.indicators import compute_indicators
from examples.db import init_db, insert_example, get_all_examples

# ---------------------------------------------------------------------------
# Market phases — used as tags in examples.db
# ---------------------------------------------------------------------------

MARKET_PHASES = (
    "bull",           # strong uptrend, higher highs
    "bear",           # strong downtrend, lower lows
    "accumulation",   # sideways after bear, smart money loading
    "distribution",   # sideways after bull, smart money exiting
    "recovery",       # early bull after bear, first higher lows
)

# ---------------------------------------------------------------------------
# Teacher prompt
# ---------------------------------------------------------------------------

SYSTEM_TEACHER = """You are a professional trading educator and pattern extractor for cryptocurrency futures.

You have access to BOTH historical candles (context) AND future candles (oracle).
This is NOT real-time trading — you are looking at a completed price window to extract
the single best trade setup that occurred.

Your job: find ONE high-quality structural trade that happened in this window.

Criteria for a valid trade:
1. Entry must be at a clear structural level — swing high/low, POC, value area boundary, MA
2. Price must have moved at least MIN_MOVE_PCT% from entry in the trade direction
3. SL must be placed beyond the nearest structural level (not arbitrary)
4. RR (TP1 vs SL) must be >= 1.5
5. The setup must be representative of the market phase (bull/bear/accumulation/etc.)

What makes a good example for RAG:
- Clear, unambiguous entry reason
- SL that makes structural sense
- TP that was actually reached (you know because you see future candles)
- Notes that explain WHY this was a good entry — this is what the RAG agent will learn

If no valid setup exists in this window (price just chopped or moved without structure),
return has_example: false.

Always respond with valid JSON only. No markdown, no explanation outside the JSON."""


def build_teacher_prompt(
    symbol: str,
    context_candles: list[dict],
    future_candles: list[dict],
    market_phase: str,
    min_move_pct: float,
    window_start: str,
    window_end: str,
) -> str:
    # Summarise context candles (past) — last 30 daily for brevity
    ctx = context_candles[-30:] if len(context_candles) > 30 else context_candles
    ctx_summary = [
        {"date": c["datetime"][:10], "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"], "v": round(c["volume"])}
        for c in ctx
    ]

    # Future candles (oracle) — this is what actually happened
    fut_summary = [
        {"date": c["datetime"][:10], "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"], "v": round(c["volume"])}
        for c in future_candles
    ]

    entry_price_ref = context_candles[-1]["close"] if context_candles else "N/A"
    future_high = max((c["high"] for c in future_candles), default=0)
    future_low = min((c["low"] for c in future_candles), default=0)

    return f"""Extract the best trade example from this historical window for {symbol}.

=== MARKET PHASE ===
Phase: {market_phase.upper()}
Window: {window_start} → {window_end}
Price at window start: {entry_price_ref}
Future high: {future_high} | Future low: {future_low}
Min required move: {min_move_pct}%

=== CONTEXT CANDLES (daily, last 30 before window) ===
{json.dumps(ctx_summary, indent=2)}

=== FUTURE CANDLES (daily, oracle — what actually happened) ===
{json.dumps(fut_summary, indent=2)}

=== YOUR TASK ===
Looking at both the context (market structure) and future (what actually happened):

1. Did price make a clean move of >= {min_move_pct}% in either direction?
2. Was there a clear structural entry point (swing level, POC, MA) at the start of that move?
3. Where would a reasonable SL have been placed (beyond structure, not too wide)?
4. What was the first logical TP1 target (a major level that was actually reached)?
5. Was there a TP2 beyond that?

If yes to all — extract the trade. If no clear structure → return has_example: false.

Respond with this exact JSON:
{{
  "has_example": true | false,
  "no_example_reason": "<why no valid setup, or null>",
  "direction": "long" | "short" | null,
  "entry1": <price at structural level or null>,
  "entry2": <slightly better fill price or null>,
  "sl": <stop loss price or null>,
  "tp1": <first target — must have been touched in future candles, or null>,
  "tp2": <second target or null>,
  "trade_date": "<YYYY-MM-DD — date of the entry candle>",
  "outcome": "tp1_hit" | "tp2_hit" | "sl_hit",
  "rr": <risk/reward ratio as number or null>,
  "move_pct": <actual price move % from entry to TP1 or null>,
  "entry_reason": "<structural reason for entry — swing low, POC, MA support, etc.>",
  "sl_reason": "<why SL is placed here>",
  "tp_reason": "<why TP1/TP2 at these levels>",
  "notes": "<key lesson from this example — what made it work, what to replicate>"
}}"""


# ---------------------------------------------------------------------------
# Deduplication helper
# ---------------------------------------------------------------------------

def _already_exists(asset: str, trade_date: str, direction: str) -> bool:
    """Check if an auto example for this asset/date/direction already exists."""
    existing = get_all_examples(asset)
    for ex in existing:
        if (
            ex.get("trade_date") == trade_date
            and ex.get("direction") == direction
            and ex.get("source") == "auto"
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Main teacher class
# ---------------------------------------------------------------------------

class AutoTeacher:
    """
    Generates auto examples for RAG from historical data.

    For each step in the date range:
      1. Fetch context candles (past N days)
      2. Fetch future candles (lookahead days — oracle)
      3. Run teacher LLM to extract best trade
      4. Validate (move >= min_move_pct, RR >= 1.5, no duplicate)
      5. Save to examples.db with source='auto'
    """

    def __init__(self):
        self.mexc = MEXCClient()
        self.llm = LLMClient()

    def run(
        self,
        symbol: str,
        date_from: str,
        date_to: str,
        market_phase: str,
        step_days: int = 7,
        lookahead_days: int = 14,
        min_move_pct: float = 10.0,
        context_days: int = 60,
    ) -> int:
        """
        Run teacher over a date range. Returns number of examples saved.

        Args:
            symbol:        e.g. 'BTCUSDT'
            date_from:     start of range 'YYYY-MM-DD'
            date_to:       end of range 'YYYY-MM-DD'
            market_phase:  'bull' | 'bear' | 'accumulation' | 'distribution' | 'recovery'
            step_days:     how often to run the teacher (default 7)
            lookahead_days: how many days forward the teacher can see (default 14)
            min_move_pct:  minimum price move % to consider a valid trade (default 10)
            context_days:  how many days of history to show as context (default 60)
        """
        if market_phase not in MARKET_PHASES:
            raise ValueError(f"market_phase must be one of: {MARKET_PHASES}")

        init_db()

        asset = symbol.upper()
        if not asset.endswith("USDT"):
            asset = f"{asset}USDT"

        dt_from = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dt_to = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        saved = 0
        skipped_no_setup = 0
        skipped_duplicate = 0
        skipped_quality = 0
        current_dt = dt_from

        print(f"\n{'='*60}")
        print(f"  AUTO TEACHER: {asset}")
        print(f"  {date_from} → {date_to} | phase={market_phase}")
        print(f"  step={step_days}d | lookahead={lookahead_days}d | min_move={min_move_pct}%")
        print(f"{'='*60}\n")

        while current_dt <= dt_to:
            window_start = current_dt.strftime("%Y-%m-%d")
            window_end = (current_dt + timedelta(days=lookahead_days)).strftime("%Y-%m-%d")
            print(f"  [{window_start}] analyzing window → {window_end}")

            try:
                result = self._process_window(
                    symbol=asset,
                    window_dt=current_dt,
                    market_phase=market_phase,
                    lookahead_days=lookahead_days,
                    min_move_pct=min_move_pct,
                    context_days=context_days,
                )

                if result == "saved":
                    saved += 1
                elif result == "no_setup":
                    skipped_no_setup += 1
                elif result == "duplicate":
                    skipped_duplicate += 1
                elif result == "quality":
                    skipped_quality += 1

            except Exception as e:
                print(f"    [error] {e}")

            current_dt += timedelta(days=step_days)

        print(f"\n{'─'*60}")
        print(f"  Done. Saved: {saved} | No setup: {skipped_no_setup} | "
              f"Quality filtered: {skipped_quality} | Duplicates: {skipped_duplicate}")
        print(f"{'─'*60}\n")

        return saved

    def _process_window(
        self,
        symbol: str,
        window_dt: datetime,
        market_phase: str,
        lookahead_days: int,
        min_move_pct: float,
        context_days: int,
    ) -> str:
        """Process one window. Returns 'saved'|'no_setup'|'duplicate'|'quality'."""

        # Fetch context candles (past)
        context_candles = self.mexc.get_historical_candles(
            symbol, "Day1",
            date_from=window_dt - timedelta(days=context_days),
            date_to=window_dt,
        )
        _time.sleep(0.15)

        # Fetch future candles (oracle)
        future_candles = self.mexc.get_historical_candles(
            symbol, "Day1",
            date_from=window_dt,
            date_to=window_dt + timedelta(days=lookahead_days),
        )
        _time.sleep(0.15)

        if not context_candles or not future_candles:
            print(f"    [warn] not enough candles")
            return "no_setup"

        window_start = window_dt.strftime("%Y-%m-%d")
        window_end = (window_dt + timedelta(days=lookahead_days)).strftime("%Y-%m-%d")

        prompt = build_teacher_prompt(
            symbol=symbol,
            context_candles=context_candles,
            future_candles=future_candles,
            market_phase=market_phase,
            min_move_pct=min_move_pct,
            window_start=window_start,
            window_end=window_end,
        )

        result = self.llm.complete_json(SYSTEM_TEACHER, prompt)

        if not result.get("has_example"):
            reason = result.get("no_example_reason", "—")
            print(f"    → No setup: {reason[:80]}")
            return "no_setup"

        # Quality checks
        rr = result.get("rr") or 0
        move = result.get("move_pct") or 0
        if rr < 1.5:
            print(f"    → Filtered: RR={rr} < 1.5")
            return "quality"
        if move < min_move_pct:
            print(f"    → Filtered: move={move}% < {min_move_pct}%")
            return "quality"

        trade_date = result.get("trade_date") or window_start
        direction = result.get("direction", "long")
        asset_clean = symbol.replace("USDT", "")

        # Deduplication
        if _already_exists(asset_clean, trade_date, direction):
            print(f"    → Duplicate: {asset_clean} {direction} {trade_date}")
            return "duplicate"

        # Build notes combining teacher insight + meta info
        notes = (
            f"[AUTO/{market_phase.upper()}] {result.get('notes', '')}\n"
            f"Entry reason: {result.get('entry_reason', '')}\n"
            f"SL reason: {result.get('sl_reason', '')}"
        )

        # Fetch market indicators snapshot for RAG matching
        try:
            trade_dt = datetime.strptime(trade_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            snapshot = self._build_snapshot(symbol, trade_dt)
            from data.indicators import compute_indicators
            indicators = compute_indicators(snapshot)
            indicators_json = json.dumps(indicators)
            snapshot_json = json.dumps(snapshot)
        except Exception as e:
            print(f"    [warn] indicators fetch failed: {e}")
            indicators_json = json.dumps({})
            snapshot_json = json.dumps({})

        example_id = insert_example({
            "asset": asset_clean,
            "direction": direction,
            "entry1": result["entry1"],
            "entry2": result.get("entry2"),
            "sl": result["sl"],
            "tp1": result["tp1"],
            "tp2": result.get("tp2"),
            "trade_date": trade_date,
            "outcome": result.get("outcome", "tp1_hit"),
            "notes": notes,
            "liquidity_levels": [],
            "market_snapshot": json.loads(snapshot_json),
            "indicators": json.loads(indicators_json),
            "source": "auto",
            "market_phase": market_phase,
        })

        print(f"    → Saved #{example_id}: {direction.upper()} {trade_date} | "
              f"RR={rr} | move={move}% | outcome={result.get('outcome')}")
        return "saved"

    def _build_snapshot(self, symbol: str, trade_dt: datetime) -> dict:
        """Build historical market snapshot for indicator computation."""
        snapshot = {
            "symbol": symbol,
            "timestamp": int(trade_dt.timestamp()),
            "candles": {},
            "open_interest": None,
            "funding_rate": None,
            "long_short_ratio": None,
            "ticker": None,
        }
        lookbacks = {
            "weekly": (timedelta(weeks=52), "Week1"),
            "daily": (timedelta(days=100), "Day1"),
            "h4": (timedelta(hours=400), "Hour4"),
        }
        for name, (lb, interval) in lookbacks.items():
            try:
                snapshot["candles"][name] = self.mexc.get_historical_candles(
                    symbol, interval,
                    date_from=trade_dt - lb,
                    date_to=trade_dt + timedelta(days=1),
                )
                _time.sleep(0.1)
            except Exception:
                snapshot["candles"][name] = []

        daily = snapshot["candles"].get("daily", [])
        if daily:
            last = daily[-1]
            snapshot["ticker"] = {
                "symbol": symbol,
                "last_price": last["close"],
                "price_change_pct": round((last["close"] - last["open"]) / last["open"] * 100, 2),
                "volume_24h": last["volume"],
                "high_24h": last["high"],
                "low_24h": last["low"],
            }
        return snapshot
