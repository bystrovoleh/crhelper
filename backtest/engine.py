from datetime import datetime, timezone, timedelta
from data.mexc_client import MEXCClient
from agent.analyzer import TradingAgent


class BacktestEngine:
    """
    Backtest engine: runs the agent on historical data points
    and evaluates its signal predictions against real price movements.
    """

    def __init__(self, rag_source: str = None):
        self.mexc = MEXCClient()
        self.agent = TradingAgent(rag_source=rag_source)

    def run(
        self,
        symbol: str,
        date_from: str,
        date_to: str,
        interval: str = "Day1",
        step_days: int = 7,
        liquidity_levels: list[float] = None,
        use_rag: bool = True,
    ) -> dict:
        """
        Run backtest for a symbol over a date range.

        Args:
            symbol: e.g. "BTCUSDT"
            date_from: "YYYY-MM-DD"
            date_to: "YYYY-MM-DD"
            interval: candle interval for stepping through time
            step_days: how many days to step between analysis points
            liquidity_levels: optional manual liquidation levels

        Returns:
            dict with signals list and metrics
        """
        dt_from = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dt_to = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        signals = []
        current_dt = dt_from

        rag_label = "with RAG" if use_rag else "without RAG"
        print(f"\n  Running backtest: {symbol} | {date_from} → {date_to} | {rag_label}")
        print(f"  Step: {step_days} days\n")

        while current_dt <= dt_to:
            date_label = current_dt.strftime("%Y-%m-%d")
            print(f"  [{date_label}] Analyzing...")

            try:
                snapshot = self._build_snapshot_at(symbol, current_dt)
                signal = self.agent.analyze_with_snapshot(
                    symbol=symbol,
                    snapshot=snapshot,
                    liquidity_levels=liquidity_levels,
                    date_label=date_label,
                    use_rag=use_rag,
                )
                signal["date"] = date_label

                if signal.get("has_setup"):
                    outcome = self._evaluate_signal(symbol, signal, current_dt)
                    signal["backtest_outcome"] = outcome
                    print(f"    Setup found: {signal.get('direction')} | outcome: {outcome.get('result')}")
                else:
                    signal["backtest_outcome"] = None
                    print(f"    No setup found.")

                signals.append(signal)

            except Exception as e:
                print(f"    [error] {e}")
                signals.append({"date": date_label, "error": str(e)})

            current_dt += timedelta(days=step_days)

        metrics = self._compute_metrics(signals)
        return {"symbol": symbol, "date_from": date_from, "date_to": date_to, "signals": signals, "metrics": metrics}

    def _build_snapshot_at(self, symbol: str, dt: datetime) -> dict:
        """Build market snapshot for a specific historical date."""
        from config.settings import TIMEFRAMES
        import time as _time

        lookbacks = {
            "weekly": (timedelta(weeks=52), "Week1"),
            "daily": (timedelta(days=100), "Day1"),
            "h4": (timedelta(hours=400), "Hour4"),
        }

        snapshot = {
            "symbol": symbol,
            "timestamp": int(dt.timestamp()),
            "candles": {},
            "open_interest": None,
            "funding_rate": None,
            "long_short_ratio": None,
            "ticker": None,
        }

        for name, (lookback, interval) in lookbacks.items():
            try:
                candles = self.mexc.get_historical_candles(
                    symbol, interval,
                    date_from=dt - lookback,
                    date_to=dt,
                )
                snapshot["candles"][name] = candles
                _time.sleep(0.15)
            except Exception as e:
                snapshot["candles"][name] = []
                print(f"      [warn] candles {name}: {e}")

        # Approximate ticker from last candle
        daily_candles = snapshot["candles"].get("daily", [])
        if daily_candles:
            last = daily_candles[-1]
            snapshot["ticker"] = {
                "symbol": symbol,
                "last_price": last["close"],
                "price_change_pct": round((last["close"] - last["open"]) / last["open"] * 100, 2),
                "volume_24h": last["volume"],
                "high_24h": last["high"],
                "low_24h": last["low"],
            }

        return snapshot

    def _evaluate_signal(self, symbol: str, signal: dict, signal_dt: datetime) -> dict:
        """
        Check if the signal's TP/SL was hit in the following candles.
        Looks ahead up to 30 daily candles from signal date.
        """
        if not signal.get("has_setup"):
            return {"result": "no_setup"}

        entry = signal.get("entry1")
        sl = signal.get("sl")
        tp1 = signal.get("tp1")
        tp2 = signal.get("tp2")
        direction = signal.get("direction")

        if not all([entry, sl, tp1, direction]):
            return {"result": "incomplete_signal"}

        look_ahead_end = signal_dt + timedelta(days=30)

        try:
            future_candles = self.mexc.get_historical_candles(
                symbol, "Day1",
                date_from=signal_dt + timedelta(days=1),
                date_to=look_ahead_end,
            )
        except Exception as e:
            return {"result": "data_error", "error": str(e)}

        for candle in future_candles:
            h = candle["high"]
            l = candle["low"]

            if direction == "long":
                if l <= sl:
                    return {"result": "sl_hit", "date": candle["datetime"], "price": sl}
                if tp2 and h >= tp2:
                    return {"result": "tp2_hit", "date": candle["datetime"], "price": tp2}
                if h >= tp1:
                    return {"result": "tp1_hit", "date": candle["datetime"], "price": tp1}
            elif direction == "short":
                if h >= sl:
                    return {"result": "sl_hit", "date": candle["datetime"], "price": sl}
                if tp2 and l <= tp2:
                    return {"result": "tp2_hit", "date": candle["datetime"], "price": tp2}
                if l <= tp1:
                    return {"result": "tp1_hit", "date": candle["datetime"], "price": tp1}

        return {"result": "open", "note": "Neither TP nor SL hit within 30 days"}

    def _compute_metrics(self, signals: list[dict]) -> dict:
        """Compute backtest performance metrics."""
        setups = [s for s in signals if s.get("has_setup") and s.get("backtest_outcome")]
        if not setups:
            return {"total_signals": 0, "note": "No setups found in this period"}

        outcomes = [s["backtest_outcome"].get("result") for s in setups]
        wins = sum(1 for o in outcomes if o in ("tp1_hit", "tp2_hit"))
        losses = sum(1 for o in outcomes if o == "sl_hit")
        open_trades = sum(1 for o in outcomes if o == "open")
        total = len(setups)

        win_rate = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0

        # RR estimation from signal data
        rr_list = []
        for s in setups:
            entry = s.get("entry1")
            sl = s.get("sl")
            tp1 = s.get("tp1")
            if entry and sl and tp1:
                risk = abs(entry - sl)
                reward = abs(tp1 - entry)
                if risk > 0:
                    rr_list.append(round(reward / risk, 2))

        avg_rr = round(sum(rr_list) / len(rr_list), 2) if rr_list else None

        by_direction = {}
        for s in setups:
            d = s.get("direction", "unknown")
            o = s["backtest_outcome"].get("result")
            if d not in by_direction:
                by_direction[d] = {"wins": 0, "losses": 0, "open": 0}
            if o in ("tp1_hit", "tp2_hit"):
                by_direction[d]["wins"] += 1
            elif o == "sl_hit":
                by_direction[d]["losses"] += 1
            else:
                by_direction[d]["open"] += 1

        return {
            "total_signals": total,
            "wins": wins,
            "losses": losses,
            "open": open_trades,
            "win_rate_pct": win_rate,
            "avg_risk_reward": avg_rr,
            "by_direction": by_direction,
        }
