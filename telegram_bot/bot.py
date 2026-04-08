from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, filters, ContextTypes,
)
from config.settings import TELEGRAM_TOKEN, ASSETS
from agent.analyzer import TradingAgent
from intraday_agent.analyzer import IntradayAgent
from examples.manager import add_example
from telegram_bot.sender import format_signal, format_intraday_signal, send_intraday_analyze_all
from positions.db import (
    init_db as init_positions_db,
    insert_position, close_position,
    get_open_positions, get_position_by_id,
)
from exit_agent.analyzer import ExitAgent

MAX_MESSAGE_LENGTH = 4096

# Conversation states
(
    EX_ASSET, EX_DIRECTION, EX_ENTRY1, EX_ENTRY2,
    EX_SL, EX_TP1, EX_TP2, EX_DATE, EX_NOTES, EX_CONFIRM,
    POS_SYMBOL, POS_DIRECTION, POS_SIZE, POS_LEVERAGE,
    POS_ENTRY, POS_SL, POS_TP1, POS_TP2, POS_CONFIRM,
) = range(19)


async def _send_long(update: Update, text: str):
    for i in range(0, len(text), MAX_MESSAGE_LENGTH):
        await update.message.reply_text(
            text[i:i + MAX_MESSAGE_LENGTH],
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# analyze / analyze_all
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>crhelper bot</b>\n\n"
        "<b>Свинг:</b>\n"
        "/analyze BTC — анализ одного актива\n"
        "/analyze_all — анализ всех активов\n\n"
        "<b>Интрадей:</b>\n"
        "/intraday BTC — интрадей анализ одного актива\n"
        "/intraday_all — интрадей анализ всех активов\n\n"
        "<b>Позиции:</b>\n"
        "/add_position — открыть позицию\n"
        "/positions — список открытых позиций\n"
        "/close ID цена — закрыть позицию\n"
        "/check_position ID — анализ выхода по позиции\n"
        "/check_all — анализ выхода по всем открытым\n\n"
        "/add_example — добавить пример в базу\n"
        "/help — показать команды",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Команды:</b>\n\n"
        "<b>— Свинг —</b>\n"
        "/analyze BTC — анализ одного актива\n"
        "/analyze_all — анализ всех активов из списка\n\n"
        "<b>— Интрадей —</b>\n"
        "/intraday BTC — интрадей анализ одного актива\n"
        "/intraday_all — интрадей анализ всех активов\n\n"
        "<b>— Позиции —</b>\n"
        "/add_position — записать открытую позицию\n"
        "/positions — список открытых позиций\n"
        "/close ID цена — закрыть позицию (пример: /close 3 96500)\n"
        "/check_position ID — анализ выхода по конкретной позиции\n"
        "/check_all — анализ выхода по всем открытым позициям\n\n"
        "/add_example — добавить торговый пример в базу\n"
        "/help — эта справка",
        parse_mode="HTML",
    )


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Укажи символ: /analyze BTC")
        return

    symbol = args[0].upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"

    await update.message.reply_text(f"⏳ Анализирую {symbol}...")

    try:
        agent = TradingAgent()
        signal = agent.analyze(symbol)
        text = format_signal(signal)
        await _send_long(update, text)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_analyze_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"⏳ Анализирую {len(ASSETS)} активов...")

    agent = TradingAgent()
    setup_signals = []
    errors = []

    for asset in ASSETS:
        try:
            signal = agent.analyze(asset)
            signal["_asset"] = asset
            if signal.get("has_setup"):
                setup_signals.append(signal)
        except Exception as e:
            errors.append(f"{asset}: {e}")

    summary_lines = [f"✅ <b>Scan complete — {len(setup_signals)} setup(s)</b>"]

    for s in setup_signals:
        symbol = s.get("_asset", "?")
        direction = "🟢 LONG" if s.get("direction") == "long" else "🔴 SHORT"
        confidence = s.get("confidence", "—")
        entry = s.get("entry1", "—")
        entry2 = s.get("entry2", "—")
        sl = s.get("sl", "—")
        tp1 = s.get("tp1", "—")
        tp2 = s.get("tp2", "—")
        rr = s.get("risk_reward", "—")
        rag_count = s.get("similar_examples_count", 0)
        rag_note = f"\n  📚 Based on {rag_count} similar example(s)" if rag_count else ""
        summary_lines.append(
            f"\n<b>{symbol}</b> {direction} | {confidence}\n"
            f"  Entry: {entry} / {entry2}  SL: {sl}\n"
            f"  TP1: {tp1}  TP2: {tp2}  RR: {rr}"
            f"{rag_note}"
        )

    if errors:
        summary_lines.append(f"\n⚠️ Errors: {', '.join(errors)}")

    await update.message.reply_text("\n".join(summary_lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# intraday analyze / intraday_all
# ---------------------------------------------------------------------------

async def cmd_intraday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Укажи символ: /intraday BTC")
        return

    symbol = args[0].upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"

    await update.message.reply_text(f"⏳ Интрадей анализ {symbol}...")

    try:
        agent = IntradayAgent()
        signal = agent.analyze(symbol)
        text = format_intraday_signal(signal)
        await _send_long(update, text)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_intraday_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"⏳ Интрадей анализ {len(ASSETS)} активов...")

    agent = IntradayAgent()
    setup_signals = []
    all_signals = []
    errors = []

    for asset in ASSETS:
        try:
            signal = agent.analyze(asset)
            signal["_asset"] = asset
            all_signals.append(signal)
            if signal.get("has_setup"):
                setup_signals.append(signal)
        except Exception as e:
            errors.append(f"{asset}: {e}")

    summary_lines = [f"⚡ <b>Intraday scan — {len(setup_signals)} setup(s)</b>"]

    for s in setup_signals:
        symbol = s.get("_asset", "?")
        direction = "🟢 LONG" if s.get("direction") == "long" else "🔴 SHORT"
        confidence = s.get("confidence", "—")
        sess = (s.get("session_analysis") or {}).get("current_session", "?")
        entry = s.get("entry1", "—")
        sl = s.get("sl", "—")
        tp1 = s.get("tp1", "—")
        rr = s.get("risk_reward", "—")
        summary_lines.append(
            f"\n<b>{symbol}</b> {direction} | {confidence} | {sess}\n"
            f"  Entry: {entry}  SL: {sl}  TP1: {tp1}  RR: {rr}"
        )

    if errors:
        summary_lines.append(f"\n⚠️ Errors: {', '.join(errors)}")

    await update.message.reply_text("\n".join(summary_lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# add_example conversation
# ---------------------------------------------------------------------------

async def ex_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Актив (например BTC):")
    return EX_ASSET


async def ex_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["asset"] = update.message.text.strip().upper()
    await update.message.reply_text(
        "Направление:",
        reply_markup=ReplyKeyboardMarkup([["long", "short"]], one_time_keyboard=True, resize_keyboard=True),
    )
    return EX_DIRECTION


async def ex_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip().lower()
    if val not in ("long", "short"):
        await update.message.reply_text("Введи long или short:")
        return EX_DIRECTION
    context.user_data["direction"] = val
    await update.message.reply_text("Вход 1:", reply_markup=ReplyKeyboardRemove())
    return EX_ENTRY1


async def ex_entry1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["entry1"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введи число:")
        return EX_ENTRY1
    await update.message.reply_text("Вход 2 (или /skip):")
    return EX_ENTRY2


async def ex_entry2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["entry2"] = float(update.message.text.strip())
    except ValueError:
        context.user_data["entry2"] = None
    await update.message.reply_text("Стоп-лосс:")
    return EX_SL


async def ex_sl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["sl"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введи число:")
        return EX_SL
    await update.message.reply_text("TP1:")
    return EX_TP1


async def ex_tp1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["tp1"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введи число:")
        return EX_TP1
    await update.message.reply_text("TP2 (или /skip):")
    return EX_TP2


async def ex_tp2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["tp2"] = float(update.message.text.strip())
    except ValueError:
        context.user_data["tp2"] = None
    await update.message.reply_text("Дата YYYY-MM-DD (или /skip для сегодня):")
    return EX_DATE


async def ex_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["trade_date"] = update.message.text.strip() or None
    await update.message.reply_text("Заметки (или /skip):")
    return EX_NOTES


async def ex_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["notes"] = update.message.text.strip() or None
    d = context.user_data
    preview = (
        f"<b>Проверь данные:</b>\n\n"
        f"Актив: <b>{d['asset']}</b>\n"
        f"Направление: <b>{d['direction']}</b>\n"
        f"Вход 1: <b>{d['entry1']}</b>\n"
        f"Вход 2: <b>{d.get('entry2', '—')}</b>\n"
        f"Стоп: <b>{d['sl']}</b>\n"
        f"TP1: <b>{d['tp1']}</b>\n"
        f"TP2: <b>{d.get('tp2', '—')}</b>\n"
        f"Дата: <b>{d.get('trade_date', 'сегодня')}</b>\n"
        f"Заметки: <b>{d.get('notes', '—')}</b>"
    )
    await update.message.reply_text(
        preview,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([["✅ Сохранить", "❌ Отмена"]], one_time_keyboard=True, resize_keyboard=True),
    )
    return EX_CONFIRM


async def ex_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "Сохранить" not in text:
        await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    d = context.user_data
    await update.message.reply_text("⏳ Сохраняю и загружаю контекст...", reply_markup=ReplyKeyboardRemove())

    try:
        example_id = add_example(
            asset=d["asset"],
            direction=d["direction"],
            entry1=d["entry1"],
            entry2=d.get("entry2"),
            sl=d["sl"],
            tp1=d["tp1"],
            tp2=d.get("tp2"),
            trade_date=d.get("trade_date"),
            notes=d.get("notes"),
        )
        await update.message.reply_text(f"✅ Пример #{example_id} сохранён.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

    return ConversationHandler.END


async def ex_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /skip command — move to next field with None."""
    state = context.user_data.get("_state")
    # Just send empty string to trigger the current handler
    update.message.text = ""
    return None


async def ex_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# positions — add_position conversation
# ---------------------------------------------------------------------------

async def pos_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Символ (например BTC или BTCUSDT):")
    return POS_SYMBOL


async def pos_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text.strip().upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    context.user_data["symbol"] = symbol
    await update.message.reply_text(
        "Направление:",
        reply_markup=ReplyKeyboardMarkup([["long", "short"]], one_time_keyboard=True, resize_keyboard=True),
    )
    return POS_DIRECTION


async def pos_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip().lower()
    if val not in ("long", "short"):
        await update.message.reply_text("Введи long или short:")
        return POS_DIRECTION
    context.user_data["direction"] = val
    await update.message.reply_text("Размер позиции в USD:", reply_markup=ReplyKeyboardRemove())
    return POS_SIZE


async def pos_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["size_usd"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введи число:")
        return POS_SIZE
    await update.message.reply_text("Плечо (Enter — по умолчанию 10):")
    return POS_LEVERAGE


async def pos_leverage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        context.user_data["leverage"] = int(text) if text else 10
    except ValueError:
        await update.message.reply_text("Введи целое число (или Enter для 10):")
        return POS_LEVERAGE
    await update.message.reply_text("Цена входа:")
    return POS_ENTRY


async def pos_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["entry_price"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введи число:")
        return POS_ENTRY
    await update.message.reply_text("Стоп-лосс:")
    return POS_SL


async def pos_sl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["sl_price"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введи число:")
        return POS_SL
    await update.message.reply_text("TP1:")
    return POS_TP1


async def pos_tp1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["tp1_price"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введи число:")
        return POS_TP1
    await update.message.reply_text("TP2 (или /skip):")
    return POS_TP2


async def pos_tp2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["tp2_price"] = float(update.message.text.strip())
    except ValueError:
        context.user_data["tp2_price"] = None
    return await _pos_show_preview(update, context)


async def _pos_show_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    exposure = d["size_usd"] * d.get("leverage", 10)
    preview = (
        f"<b>Проверь данные:</b>\n\n"
        f"Символ: <b>{d['symbol']}</b>\n"
        f"Направление: <b>{d['direction'].upper()}</b>\n"
        f"Размер: <b>${d['size_usd']}</b> × {d.get('leverage', 10)}x = <b>${exposure}</b> экспозиция\n"
        f"Вход: <b>{d['entry_price']}</b>\n"
        f"Стоп: <b>{d['sl_price']}</b>\n"
        f"TP1: <b>{d['tp1_price']}</b>\n"
        f"TP2: <b>{d.get('tp2_price') or '—'}</b>"
    )
    await update.message.reply_text(
        preview,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([["✅ Сохранить", "❌ Отмена"]], one_time_keyboard=True, resize_keyboard=True),
    )
    return POS_CONFIRM


async def pos_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "Сохранить" not in text:
        await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    d = context.user_data
    await update.message.reply_text("Сохраняю...", reply_markup=ReplyKeyboardRemove())

    try:
        pos_id = insert_position(d)
        await update.message.reply_text(f"✅ Позиция <b>#{pos_id}</b> открыта.", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

    return ConversationHandler.END


async def pos_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# positions — /positions list
# ---------------------------------------------------------------------------

async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    positions = get_open_positions()
    if not positions:
        await update.message.reply_text("Нет открытых позиций.")
        return

    lines = ["<b>Открытые позиции:</b>\n"]
    for p in positions:
        direction = "🟢 LONG" if p["direction"] == "long" else "🔴 SHORT"
        tp2 = f"  TP2: <b>{p['tp2_price']}</b>" if p.get("tp2_price") else ""
        lines.append(
            f"<b>#{p['id']} {p['symbol']}</b> {direction}\n"
            f"  Вход: <b>{p['entry_price']}</b>  SL: <b>{p['sl_price']}</b>\n"
            f"  TP1: <b>{p['tp1_price']}</b>{tp2}\n"
            f"  Размер: ${p['size_usd']} × {p['leverage']}x\n"
            f"  Открыта: {p['opened_at'][:16]}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# positions — /close ID price
# ---------------------------------------------------------------------------

async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Формат: /close <ID> <цена>\nПример: /close 3 96500")
        return

    try:
        pos_id = int(args[0])
        close_price = float(args[1])
    except ValueError:
        await update.message.reply_text("Неверный формат. Пример: /close 3 96500")
        return

    pos = get_position_by_id(pos_id)
    if not pos:
        await update.message.reply_text(f"Позиция #{pos_id} не найдена.")
        return
    if pos["status"] == "closed":
        await update.message.reply_text(f"Позиция #{pos_id} уже закрыта.")
        return

    result = close_position(pos_id, close_price)
    if not result:
        await update.message.reply_text("Ошибка при закрытии позиции.")
        return

    pnl = result["pnl_usd"]
    pnl_pct = result["pnl_percent"]
    emoji = "🟢" if pnl >= 0 else "🔴"

    await update.message.reply_text(
        f"{emoji} <b>Позиция #{pos_id} закрыта</b>\n\n"
        f"Символ: <b>{result['symbol']}</b>\n"
        f"Вход: <b>{result['entry_price']}</b> → Выход: <b>{close_price}</b>\n"
        f"PnL: <b>{'+' if pnl >= 0 else ''}{pnl} USD</b> ({'+' if pnl_pct >= 0 else ''}{pnl_pct}%)",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# check_position / check_all
# ---------------------------------------------------------------------------

def _esc(text) -> str:
    """Escape < and > in free-form LLM text to avoid Telegram HTML parse errors."""
    if not isinstance(text, str):
        return str(text) if text is not None else "—"
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_exit_result(result: dict) -> str:
    """Format ExitAgent result into a readable Telegram message."""
    if result.get("error"):
        return f"❌ {result['error']}"

    pos = result.get("_position", {})
    action = result.get("action", "N/A")
    confidence = result.get("confidence", "N/A")
    reasoning = _esc(result.get("reasoning", "—"))
    risks = _esc(result.get("key_risks", "—"))

    action_emoji = {
        "hold": "✅",
        "adjust_tp": "🎯",
        "partial_exit": "📤",
        "exit_now": "🚨",
    }.get(action, "❓")

    action_label = {
        "hold": "ДЕРЖАТЬ",
        "adjust_tp": "СКОРРЕКТИРОВАТЬ TP",
        "partial_exit": "ЧАСТИЧНЫЙ ВЫХОД",
        "exit_now": "ВЫЙТИ СЕЙЧАС",
    }.get(action, action.upper())

    lines = [
        f"{action_emoji} <b>#{pos.get('id', '?')} {pos.get('symbol', '?')} "
        f"{'LONG' if pos.get('direction') == 'long' else 'SHORT'}</b>",
        f"Рекомендация: <b>{action_label}</b> (уверенность: {confidence})",
        "",
    ]

    # SL to breakeven
    if result.get("move_sl_to_breakeven"):
        lines.append("🔒 <b>Перенести SL в безубыток</b>")
        lines.append("")

    # Action-specific details
    if action == "adjust_tp":
        new_tp1 = result.get("suggested_tp1")
        new_tp2 = result.get("suggested_tp2")
        if new_tp1:
            lines.append(f"Новый TP1: <b>{new_tp1}</b>")
        if new_tp2:
            lines.append(f"Новый TP2: <b>{new_tp2}</b>")
        lines.append("")

    elif action == "partial_exit":
        pct = result.get("partial_exit_pct")
        exit_price = result.get("exit_price_suggestion")
        new_tp1 = result.get("suggested_tp1")
        if pct:
            lines.append(f"Закрыть: <b>{pct}%</b> позиции")
        if exit_price:
            lines.append(f"Цена выхода: <b>{exit_price}</b>")
        if new_tp1:
            lines.append(f"TP1 для остатка: <b>{new_tp1}</b>")
        lines.append("")

    elif action == "exit_now":
        exit_price = result.get("exit_price_suggestion")
        if exit_price:
            lines.append(f"Цена выхода: <b>{exit_price}</b>")
        lines.append("")

    # Sub-agent verdicts summary
    macro = result.get("_macro", {})
    local = result.get("_local", {})
    momentum = result.get("_momentum", {})

    lines.append("<b>Анализ:</b>")
    lines.append(f"  Макро: {macro.get('macro_verdict', '—')} | тренд {macro.get('macro_trend', '—')} ({macro.get('trend_health', '—')})")
    lines.append(f"  Локально (4h): {local.get('local_verdict', '—')} | моментум {local.get('momentum_4h', '—')}")
    lines.append(f"  Моментум (1h): риск разворота {result.get('reversal_risk', '—')} | сентимент {momentum.get('sentiment_verdict', '—')}")
    lines.append("")

    lines.append(f"<b>Обоснование:</b>\n{reasoning}")
    lines.append("")
    lines.append(f"<b>Риски:</b>\n{risks}")

    return "\n".join(lines)


async def cmd_check_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Формат: /check_position <ID>\nПример: /check_position 3")
        return

    try:
        pos_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    pos = get_position_by_id(pos_id)
    if not pos:
        await update.message.reply_text(f"Позиция #{pos_id} не найдена.")
        return
    if pos["status"] == "closed":
        await update.message.reply_text(f"Позиция #{pos_id} уже закрыта.")
        return

    await update.message.reply_text(f"⏳ Анализирую позицию #{pos_id} ({pos['symbol']})...")

    try:
        agent = ExitAgent()
        result = agent.check_position(pos_id)
        text = _format_exit_result(result)
        await _send_long(update, text)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_check_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    positions = get_open_positions()
    if not positions:
        await update.message.reply_text("Нет открытых позиций.")
        return

    await update.message.reply_text(f"⏳ Анализирую {len(positions)} открытых позиций...")

    try:
        agent = ExitAgent()
        results = agent.check_all_open()
        for result in results:
            text = _format_exit_result(result)
            await _send_long(update, text)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run():
    if not TELEGRAM_TOKEN:
        print("TELEGRAM_TOKEN не задан в .env")
        return

    init_positions_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    async def skip_entry2(u, c):
        c.user_data["entry2"] = None
        await u.message.reply_text("Стоп-лосс:")
        return EX_SL

    async def skip_tp2(u, c):
        c.user_data["tp2"] = None
        await u.message.reply_text("Дата YYYY-MM-DD (или /skip для сегодня):")
        return EX_DATE

    async def skip_date(u, c):
        c.user_data["trade_date"] = None
        await u.message.reply_text("Заметки (или /skip):")
        return EX_NOTES

    async def skip_notes(u, c):
        c.user_data["notes"] = None
        return await ex_notes(u, c)

    add_example_conv = ConversationHandler(
        entry_points=[CommandHandler("add_example", ex_start)],
        states={
            EX_ASSET:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ex_asset)],
            EX_DIRECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ex_direction)],
            EX_ENTRY1:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ex_entry1)],
            EX_ENTRY2:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ex_entry2),
                           CommandHandler("skip", skip_entry2)],
            EX_SL:        [MessageHandler(filters.TEXT & ~filters.COMMAND, ex_sl)],
            EX_TP1:       [MessageHandler(filters.TEXT & ~filters.COMMAND, ex_tp1)],
            EX_TP2:       [MessageHandler(filters.TEXT & ~filters.COMMAND, ex_tp2),
                           CommandHandler("skip", skip_tp2)],
            EX_DATE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ex_date),
                           CommandHandler("skip", skip_date)],
            EX_NOTES:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ex_notes),
                           CommandHandler("skip", skip_notes)],
            EX_CONFIRM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, ex_confirm)],
        },
        fallbacks=[CommandHandler("cancel", ex_cancel)],
    )

    async def skip_pos_tp2(u, c):
        c.user_data["tp2_price"] = None
        return await _pos_show_preview(u, c)

    add_position_conv = ConversationHandler(
        entry_points=[CommandHandler("add_position", pos_start)],
        states={
            POS_SYMBOL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, pos_symbol)],
            POS_DIRECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, pos_direction)],
            POS_SIZE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, pos_size)],
            POS_LEVERAGE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, pos_leverage)],
            POS_ENTRY:     [MessageHandler(filters.TEXT & ~filters.COMMAND, pos_entry)],
            POS_SL:        [MessageHandler(filters.TEXT & ~filters.COMMAND, pos_sl)],
            POS_TP1:       [MessageHandler(filters.TEXT & ~filters.COMMAND, pos_tp1)],
            POS_TP2:       [MessageHandler(filters.TEXT & ~filters.COMMAND, pos_tp2),
                            CommandHandler("skip", skip_pos_tp2)],
            POS_CONFIRM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, pos_confirm)],
        },
        fallbacks=[CommandHandler("cancel", pos_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("analyze_all", cmd_analyze_all))
    app.add_handler(CommandHandler("intraday", cmd_intraday))
    app.add_handler(CommandHandler("intraday_all", cmd_intraday_all))
    app.add_handler(add_example_conv)
    app.add_handler(add_position_conv)
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("check_position", cmd_check_position))
    app.add_handler(CommandHandler("check_all", cmd_check_all))

    print("Bot started. Waiting for commands...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run()
