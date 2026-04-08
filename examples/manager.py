from datetime import datetime, timezone
from examples.db import init_db, insert_example, update_example_context, get_all_examples
from data.mexc_client import MEXCClient
from data.indicators import compute_indicators


def add_example(
    asset: str,
    direction: str,
    entry1: float,
    sl: float,
    tp1: float,
    entry2: float = None,
    tp2: float = None,
    trade_date: str = None,
    notes: str = None,
    liquidity_levels: list[float] = None,
) -> int:
    """
    Add a new trade example to the database.
    Automatically fetches and attaches market context for the given date.
    """
    init_db()

    if not trade_date:
        trade_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    example_id = insert_example({
        "asset": asset,
        "direction": direction,
        "entry1": entry1,
        "entry2": entry2,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "trade_date": trade_date,
        "notes": notes,
        "liquidity_levels": liquidity_levels or [],
    })

    print(f"  Saved example #{example_id}. Fetching market context for {asset} on {trade_date}...")

    try:
        client = MEXCClient()
        symbol = asset if asset.endswith("USDT") else f"{asset}USDT"

        # For historical dates, fetch candles up to that date
        trade_dt = datetime.strptime(trade_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        is_historical = (datetime.now(tz=timezone.utc) - trade_dt).days > 1

        if is_historical:
            snapshot = _build_historical_snapshot(client, symbol, trade_dt)
        else:
            snapshot = client.get_market_snapshot(symbol)

        indicators = compute_indicators(snapshot)
        update_example_context(example_id, snapshot, indicators)
        print(f"  Market context attached to example #{example_id}.")
    except Exception as e:
        print(f"  [warn] Could not fetch market context: {e}")

    return example_id


def _build_historical_snapshot(client: MEXCClient, symbol: str, trade_dt: datetime) -> dict:
    """Build a market snapshot for a historical date."""
    from datetime import timedelta
    from config.settings import TIMEFRAMES
    import time as _time

    end_dt = trade_dt + timedelta(days=1)
    snapshot = {
        "symbol": symbol,
        "timestamp": int(trade_dt.timestamp()),
        "candles": {},
        "open_interest": None,
        "funding_rate": None,
        "long_short_ratio": None,
        "ticker": None,
    }

    from config.settings import CANDLE_LIMITS
    interval_map = {
        "weekly": ("Week1", CANDLE_LIMITS.get("weekly", 150)),
        "daily": ("Day1", CANDLE_LIMITS.get("daily", 500)),
        "h4": ("Hour4", CANDLE_LIMITS.get("h4", 500)),
    }

    for name, (interval, limit) in interval_map.items():
        try:
            candles = client.get_historical_candles(
                symbol, interval,
                date_from=trade_dt - _get_lookback(interval, limit),
                date_to=end_dt,
            )
            snapshot["candles"][name] = candles
            _time.sleep(0.1)
        except Exception as e:
            snapshot["candles"][name] = []
            print(f"    [warn] historical candles {name}: {e}")

    return snapshot


def _get_lookback(interval: str, limit: int):
    from datetime import timedelta
    # Add 20% buffer to ensure we get enough candles after pagination
    buf = int(limit * 1.2)
    mapping = {
        "Week1": timedelta(weeks=buf),
        "Day1": timedelta(days=buf),
        "Hour4": timedelta(hours=buf * 4),
    }
    return mapping.get(interval, timedelta(days=buf))


def get_examples_for_rag(source: str = None) -> list[dict]:
    """
    Return examples with their indicators — ready for RAG retrieval.
    source: None = all, 'manual' = hand-added only, 'auto' = teacher-generated only.
    """
    examples = get_all_examples()
    if source:
        examples = [e for e in examples if e.get("source", "manual") == source]
    return [e for e in examples if e.get("indicators")]
