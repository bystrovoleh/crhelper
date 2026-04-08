import urllib.request
import urllib.parse
import json
from config.settings import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID


def _send(text: str, chat_id: str = None) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    token = TELEGRAM_TOKEN
    cid = chat_id or TELEGRAM_CHAT_ID

    if not token or not cid:
        print("  [telegram] Not configured (TELEGRAM_TOKEN or TELEGRAM_CHAT_ID missing)")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": cid,
        "text": text,
        "parse_mode": "HTML",
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"  [telegram] Send failed: {e}")
        return False


def format_signal(signal: dict) -> str:
    """Format a trading signal as a Telegram message."""
    symbol = signal.get("symbol") or signal.get("trend_analysis", {}).get("symbol", "???")
    direction = signal.get("direction")
    has_setup = signal.get("has_setup", False)

    if has_setup:
        dir_label = "🟢 LONG" if direction == "long" else "🔴 SHORT"
        confidence = signal.get("confidence", "N/A")
        rr = signal.get("risk_reward", "N/A")

        vol = signal.get("volatility_analysis") or {}
        vol_label = f" | {vol.get('regime', '')} volatility ({vol.get('sl_buffer_pct', '')} SL)" if vol.get("regime") else ""

        lines = [
            f"<b>{symbol} — {dir_label}</b>",
            f"Confidence: <b>{confidence}</b>{vol_label}",
            f"",
            f"Entry 1:   <b>{signal.get('entry1')}</b>",
            f"Entry 2:   <b>{signal.get('entry2', '—')}</b>",
            f"Stop Loss: <b>{signal.get('sl')}</b>",
            f"TP1:       <b>{signal.get('tp1')}</b>",
            f"TP2:       <b>{signal.get('tp2', '—')}</b>",
            f"RR:        <b>{rr}</b>",
            f"",
            f"{signal.get('reasoning', '')}",
        ]
        if signal.get("risks"):
            lines += ["", f"<i>Risks: {signal.get('risks')}</i>"]
    else:
        entry_analysis = signal.get("entry_analysis") or {}
        lines = [
            f"<b>{symbol} — ⚪ NO SETUP</b>",
            f"",
            f"{signal.get('reasoning', 'No valid setup found.')}",
        ]
        if entry_analysis.get("entry1"):
            lines += [
                f"",
                f"<i>Proposed (rejected):</i>",
                f"Entry: {entry_analysis.get('entry1')} / {entry_analysis.get('entry2', '—')}",
                f"SL: {entry_analysis.get('sl')}  TP1: {entry_analysis.get('tp1')}  RR: {entry_analysis.get('risk_reward', '—')}",
            ]
        if signal.get("watch_level"):
            lines += ["", f"👁 <b>Watch:</b> {signal.get('watch_level')}"]

    return "\n".join(lines)


def format_intraday_signal(signal: dict) -> str:
    """Format an intraday signal as a Telegram message."""
    symbol = signal.get("symbol", "???")
    direction = signal.get("direction")
    has_setup = signal.get("has_setup", False)

    sess = (signal.get("session_analysis") or {}).get("current_session", "?")
    h4 = (signal.get("session_analysis") or {}).get("h4_trend", "?")
    flow = (signal.get("flow_analysis") or {}).get("flow_verdict", "?")

    if has_setup:
        dir_label = "🟢 LONG" if direction == "long" else "🔴 SHORT"
        confidence = signal.get("confidence", "N/A")
        rr = signal.get("risk_reward", "N/A")
        lines = [
            f"<b>⚡ INTRADAY: {symbol} — {dir_label}</b>",
            f"Confidence: <b>{confidence}</b> | Session: {sess} | H4: {h4} | Flow: {flow}",
            f"",
            f"Entry 1:   <b>{signal.get('entry1')}</b>",
            f"Entry 2:   <b>{signal.get('entry2', '—')}</b>",
            f"Stop Loss: <b>{signal.get('sl')}</b>",
            f"TP1:       <b>{signal.get('tp1')}</b>",
            f"TP2:       <b>{signal.get('tp2', '—')}</b>",
            f"RR:        <b>{rr}</b>",
            f"",
            f"{signal.get('reasoning', '')}",
        ]
        if signal.get("risks"):
            lines += ["", f"<i>Risks: {signal.get('risks')}</i>"]
    else:
        lines = [
            f"<b>⚡ INTRADAY: {symbol} — ⚪ NO SETUP</b>",
            f"Session: {sess} | H4: {h4} | Flow: {flow}",
            f"",
            f"{signal.get('reasoning', 'No valid intraday setup found.')}",
        ]
        watch_level = signal.get("watch_level")
        watch_cond = signal.get("watch_condition")
        if watch_level:
            lines += ["", f"👁 <b>Watch: {watch_level}</b>"]
            if watch_cond:
                lines.append(f"<i>{watch_cond}</i>")

    return "\n".join(lines)


def send_intraday_signal(signal: dict) -> bool:
    text = format_intraday_signal(signal)
    return _send(text)


def send_intraday_analyze_all(signals: list[dict]) -> bool:
    """Send a summary of intraday signals from intraday-analyze-all."""
    setups = [s for s in signals if s.get("has_setup")]
    header = f"<b>⚡ Intraday scan — {len(signals)} assets</b>\n"
    header += f"Setups found: <b>{len(setups)}</b>\n"
    header += "─" * 20
    ok = _send(header)
    for signal in signals:
        ok = send_intraday_signal(signal) and ok
    return ok


def send_signal(signal: dict) -> bool:
    text = format_signal(signal)
    return _send(text)


def send_analyze_all(signals: list[dict]) -> bool:
    """Send a summary of all signals from analyze-all."""
    setups = [s for s in signals if s.get("has_setup")]
    header = f"<b>📊 Market scan — {len(signals)} assets</b>\n"
    header += f"Setups found: <b>{len(setups)}</b>\n"
    header += "─" * 20

    ok = _send(header)
    for signal in signals:
        ok = send_signal(signal) and ok
    return ok
