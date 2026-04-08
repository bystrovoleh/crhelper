import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent

# LLM
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "claude_cli")  # "claude_cli" | "claude_api"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# MEXC
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")
MEXC_BASE_URL = "https://contract.mexc.com"

# Database
DB_PATH = BASE_DIR / "examples" / "examples.db"

# Assets
def load_assets() -> list[str]:
    assets_file = BASE_DIR / "config" / "assets.json"
    with open(assets_file) as f:
        return json.load(f)["futures"]

ASSETS = load_assets()

# Timeframes for swing trading
TIMEFRAMES = {
    "weekly": "Week1",
    "daily": "Day1",
    "h4": "Hour4",
}

# How many candles to fetch per timeframe
CANDLE_LIMITS = {
    "weekly": 150,   # ~3 years
    "daily": 500,    # ~1.5 years, enough for MA200
    "h4": 500,       # ~83 days
}

# Timeframes for exit agent (adds 1h for intraday momentum)
EXIT_TIMEFRAMES = {
    "weekly": "Week1",
    "daily": "Day1",
    "h4": "Hour4",
    "h1": "Min60",
}

EXIT_CANDLE_LIMITS = {
    "weekly": 52,    # ~1 year
    "daily": 90,     # ~3 months
    "h4": 120,       # ~20 days
    "h1": 96,        # ~4 days
}

# Telegram
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# RAG
RAG_TOP_K = 5  # how many similar examples to retrieve
RAG_SOURCE = "manual"  # "manual" | "auto" | None (None = all sources)

# Intraday agent DB
INTRADAY_DB_PATH = BASE_DIR / "intraday_examples" / "intraday_examples.db"

# Timeframes for intraday agent
# H4 = HTF context, H1 = structure, M15 = entry timeframe, M5 = precision entry
INTRADAY_TIMEFRAMES = {
    "h4":  "Hour4",
    "h1":  "Min60",
    "m15": "Min15",
    "m5":  "Min5",
}

INTRADAY_CANDLE_LIMITS = {
    "h4":  120,   # ~20 days of H4
    "h1":  96,    # ~4 days of H1
    "m15": 192,   # ~2 days of M15
    "m5":  288,   # ~24h of M5
}

# Intraday backtest: how far to look for TP/SL hit after signal
INTRADAY_EVAL_HOURS = 12   # max hours to evaluate a signal
INTRADAY_MAX_ENTRY_WAIT_HOURS = 4  # max hours to wait for entry activation
