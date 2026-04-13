from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters, ContextTypes,
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
from trading.mexc_trader import MEXCTrader, _to_mexc_symbol
from trading.order_manager import swing_process_signal, rebalance_orders, check_trailing_stops

MAX_MESSAGE_LENGTH = 4096

# ---------------------------------------------------------------------------
# Main menu keyboard
# ---------------------------------------------------------------------------

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["📊 Analyze All", "⚖️ Rebalance"],
        ["🔍 Analyze Symbol", "📋 Check Orders"],
        ["📈 Intraday All", "🎯 Trail Stops"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

MENU_WAITING_SYMBOL = "menu_waiting_symbol"

# Conversation states
(
    EX_ASSET, EX_DIRECTION, EX_ENTRY1, EX_ENTRY2,
    EX_SL, EX_TP1, EX_TP2, EX_DATE, EX_NOTES, EX_CONFIRM,
    POS_SYMBOL, POS_DIRECTION, POS_SIZE, POS_LEVERAGE,
    POS_ENTRY, POS_SL, POS_TP1, POS_TP2, POS_CONFIRM,
    # analyze_all flow
    AA_CLOSE_POSITIONS,
    AA_CONFLICT_QUESTION,
    AA_REBALANCE_CONFIRM,
) = range(22)


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
        "Yo",
        reply_markup=MAIN_MENU,
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Main menu:",
        reply_markup=MAIN_MENU,
    )


async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle taps on the persistent reply keyboard buttons (except Analyze All — handled by ConversationHandler)."""
    text = update.message.text

    if text == "⚖️ Rebalance":
        return await cmd_rebalance(update, context)

    if text == "🔍 Analyze Symbol":
        context.user_data[MENU_WAITING_SYMBOL] = True
        await update.message.reply_text("Enter symbol (e.g. BTC or BTCUSDT):")
        return

    if text == "📋 Check Orders":
        return await cmd_check_all(update, context)

    if text == "📈 Intraday All":
        return await cmd_intraday_all(update, context)

    if text == "🎯 Trail Stops":
        return await cmd_trail(update, context)


async def handle_symbol_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive symbol typed after tapping 🔍 Analyze Symbol."""
    if not context.user_data.get(MENU_WAITING_SYMBOL):
        return
    context.user_data[MENU_WAITING_SYMBOL] = False
    symbol = update.message.text.strip().upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    # Reuse cmd_analyze logic inline
    await update.message.reply_text(f"⏳ Analyzing <b>{symbol}</b>...", parse_mode="HTML")
    try:
        from agent.analyzer import TradingAgent
        agent = TradingAgent()
        signal = agent.analyze(symbol)
        from telegram_bot.sender import format_signal
        text = format_signal(signal)
        await _send_long(update, text)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Commands:</b>\n\n"
        "<b>— Swing —</b>\n"
        "/analyze BTC — analyze single asset\n"
        "/analyze_all — analyze all assets + place orders\n"
        "/rebalance — rebalance margin across pending orders\n\n"
        "<b>— Intraday —</b>\n"
        "/intraday BTC — intraday analysis for single asset\n"
        "/intraday_all — intraday analysis for all assets\n\n"
        "<b>— Positions —</b>\n"
        "/add_position — record open position\n"
        "/positions — list open positions\n"
        "/close ID price — close position (example: /close 3 96500)\n"
        "/check_position ID — exit analysis for specific position\n"
        "/check_all — exit analysis for all open positions\n\n"
        "/add_example — add trade example to database\n"
        "/help — this help",
        parse_mode="HTML",
    )


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Specify symbol: /analyze BTC")
        return

    symbol = args[0].upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"

    await update.message.reply_text(f"⏳ Analyzing {symbol}...")

    try:
        agent = TradingAgent()
        signal = agent.analyze(symbol)
        text = format_signal(signal)
        await _send_long(update, text)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


def _build_close_positions_keyboard(mexc_positions: list[dict]) -> InlineKeyboardMarkup:
    """Build inline keyboard with one button per open position + Close All + Skip."""
    buttons = []
    for p in mexc_positions:
        sym = p.get("symbol", "?")
        direction = "L" if int(p.get("positionType", 1)) == 1 else "S"
        vol = p.get("vol", "?")
        buttons.append([InlineKeyboardButton(
            f"Close {sym} {direction} (vol {vol})",
            callback_data=f"close_pos:{sym}:{direction}",
        )])
    buttons.append([InlineKeyboardButton("Close ALL positions", callback_data="close_pos:ALL")])
    buttons.append([InlineKeyboardButton("Skip →", callback_data="close_pos:SKIP")])
    return InlineKeyboardMarkup(buttons)


def _build_conflict_keyboard(symbol: str, conflict_type: str) -> InlineKeyboardMarkup:
    """
    conflict_type: "position" | "order"
    Keyboard: replace (cancel existing + place new) | skip
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "Replace (cancel existing → place new)",
            callback_data=f"conflict:replace:{symbol}",
        )],
        [InlineKeyboardButton(
            "Skip",
            callback_data=f"conflict:skip:{symbol}",
        )],
    ])


def _format_signal_short(signal: dict) -> str:
    direction = "🟢 LONG" if signal.get("direction") == "long" else "🔴 SHORT"
    entry1 = signal.get("entry1", "—")
    entry2 = signal.get("entry2")
    sl = signal.get("sl", "—")
    tp1 = signal.get("tp1", "—")
    rr = _esc(signal.get("risk_reward", "—"))
    confidence = _esc(signal.get("confidence", "—"))
    e2_str = f" / {entry2}" if entry2 else ""
    return (
        f"{direction} | {confidence} | RR {rr}\n"
        f"Entry: <b>{entry1}</b>{e2_str}  SL: <b>{sl}</b>  TP1: <b>{tp1}</b>"
    )


async def _process_one_ticker(symbol: str, chat, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """
    Analyze one ticker, place order if clean, or return conflict_type if blocked.
    Sends result message to chat.
    Returns None if done, or "position"/"order" if conflict needs user input.
    """
    try:
        agent = TradingAgent()
        signal = agent.analyze(symbol)
        signal["symbol"] = symbol
    except Exception as e:
        await chat.reply_text(f"❌ <b>{symbol}</b> — ошибка анализа: {e}", parse_mode="HTML")
        return None

    if not signal.get("has_setup"):
        watch = signal.get("watch_level")
        watch_note = f"\n👁 <b>Watch:</b> {watch}" if watch else ""
        reasoning = _esc(signal.get("reasoning", ""))[:300]
        await chat.reply_text(
            f"⚪ <b>{symbol}</b> — no setup{watch_note}\n"
            f"<i>{reasoning}</i>",
            parse_mode="HTML",
        )
        return None

    # Signal found — check for conflicts on MEXC
    try:
        trader = MEXCTrader()
        has_position = trader.has_open_position(symbol)
        open_orders = trader.get_open_orders(symbol)
    except Exception as e:
        await chat.reply_text(f"⚠️ <b>{symbol}</b> — не удалось проверить MEXC: {e}", parse_mode="HTML")
        return None

    signal_text = _format_signal_short(signal)

    if has_position:
        # Get position details for PnL display
        positions = trader.get_open_positions(symbol)
        sym_mexc = _to_mexc_symbol(symbol)
        pos_data = next((p for p in positions if p.get("symbol") == sym_mexc), {})

        pos_lines = []
        if pos_data:
            direction = "LONG" if int(pos_data.get("positionType", 1)) == 1 else "SHORT"
            avg_price = pos_data.get("holdAvgPrice", pos_data.get("avgPrice", "?"))
            vol = pos_data.get("vol", "?")
            pnl_usdt = pos_data.get("unrealisedPnl", 0)
            try:
                avg = float(avg_price)
                pnl = float(pnl_usdt)
                v = float(vol)
                leverage = int(pos_data.get("leverage", 10))
                margin = (avg * v) / leverage if avg > 0 and v > 0 else 0
                pnl_pct = (pnl / margin * 100) if margin > 0 else 0
                pnl_sign = "+" if pnl >= 0 else ""
                pnl_emoji = "🟢" if pnl >= 0 else "🔴"
                pos_lines.append(
                    f"<b>Open position:</b> {direction}  entry={avg_price}  vol={vol}\n"
                    f"PnL: {pnl_emoji} <b>{pnl_sign}{pnl:.2f} USDT ({pnl_sign}{pnl_pct:.1f}%)</b>"
                )
            except Exception:
                pos_lines.append(f"<b>Open position:</b> {direction}  entry={avg_price}  vol={vol}")

        pos_str = "\n".join(pos_lines) + "\n\n" if pos_lines else ""

        context.user_data["aa_conflict_signal"] = signal
        context.user_data["aa_conflict_type"] = "position"
        await chat.reply_text(
            f"⚠️ <b>{symbol}</b> — new setup found, but position already open!\n\n"
            f"{pos_str}"
            f"<b>New setup:</b>\n{signal_text}\n\n"
            f"What to do?",
            parse_mode="HTML",
            reply_markup=_build_conflict_keyboard(symbol, "position"),
        )
        return "conflict"

    if open_orders:
        oldest = max(open_orders, key=lambda o: trader.get_order_age_hours(o))
        age_h = trader.get_order_age_hours(oldest)

        # Get current price for context
        try:
            sym_mexc = _to_mexc_symbol(symbol)
            resp = trader.session.get(
                f"{trader.base_url}/api/v1/contract/ticker",
                params={"symbol": sym_mexc}, timeout=10,
            )
            current_price = float(resp.json().get("data", {}).get("lastPrice", 0))
        except Exception:
            current_price = 0

        # Format existing order details
        order_lines = []
        for o in open_orders:
            o_price = o.get("price", "?")
            o_sl = o.get("stopLossPrice", "—")
            o_tp = o.get("takeProfitPrice", "—")
            o_side = {1: "LONG", 2: "Close Short", 3: "SHORT", 4: "Close Long"}.get(int(o.get("side", 0)), "?")
            o_vol = o.get("vol", "?")
            o_age = trader.get_order_age_hours(o)
            order_lines.append(
                f"  {o_side}  price={o_price}  vol={o_vol}\n"
                f"  SL={o_sl}  TP={o_tp}  age={o_age:.1f}h"
            )

        price_str = f"Current price: <b>{current_price}</b>\n\n" if current_price else ""
        existing_str = "<b>Existing order(s):</b>\n" + "\n".join(order_lines)

        context.user_data["aa_conflict_signal"] = signal
        context.user_data["aa_conflict_type"] = "order"
        await chat.reply_text(
            f"⚠️ <b>{symbol}</b> — new setup found, but order already pending!\n\n"
            f"{price_str}"
            f"{existing_str}\n\n"
            f"<b>New setup:</b>\n{signal_text}\n\n"
            f"What to do?",
            parse_mode="HTML",
            reply_markup=_build_conflict_keyboard(symbol, "order"),
        )
        return "conflict"

    # No conflict — place order immediately
    try:
        result = swing_process_signal(signal)
        action = result["action"]
        if action == "orders_placed":
            n = len(result["orders_placed"])
            entry1 = signal.get("entry1", "—")
            entry2 = signal.get("entry2")
            e2_str = f" / {entry2}" if entry2 else ""
            sl = signal.get("sl", "—")
            tp1 = signal.get("tp1", "—")
            direction = "🟢 LONG" if signal.get("direction") == "long" else "🔴 SHORT"
            await chat.reply_text(
                f"✅ <b>{symbol}</b> {direction} — {n} order(s) placed\n"
                f"Entry: <b>{entry1}</b>{e2_str}  SL: <b>{sl}</b>  TP1: <b>{tp1}</b>",
                parse_mode="HTML",
            )
        else:
            await chat.reply_text(
                f"⚠️ <b>{symbol}</b> — order not placed: {result['reason']}",
                parse_mode="HTML",
            )
    except Exception as e:
        await chat.reply_text(f"❌ <b>{symbol}</b> — order error: {e}", parse_mode="HTML")

    return None


async def _continue_ticker_loop(chat, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Process next ticker from the queue stored in context.user_data.
    Returns next ConversationHandler state or END.
    """
    queue: list = context.user_data.get("aa_queue", [])

    while queue:
        symbol = queue.pop(0)
        context.user_data["aa_queue"] = queue
        await chat.reply_text(f"⏳ Analyzing <b>{symbol}</b>...", parse_mode="HTML")
        result = await _process_one_ticker(symbol, chat, context)
        if result == "conflict":
            # Store current signal, wait for user answer
            return AA_CONFLICT_QUESTION

    # Queue empty — ask user whether to rebalance
    await chat.reply_text("✅ <b>Scan complete.</b>", parse_mode="HTML")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚖️ Rebalance orders", callback_data="rebalance:yes"),
            InlineKeyboardButton("Skip →", callback_data="rebalance:skip"),
        ]
    ])
    await chat.reply_text(
        "⚖️ Redistribute margin equally across all pending orders?",
        reply_markup=keyboard,
        parse_mode="HTML",
    )
    return AA_REBALANCE_CONFIRM


async def cmd_analyze_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: check MEXC open positions first, then start per-ticker loop."""
    try:
        trader = MEXCTrader()
        mexc_positions = trader.get_open_positions()
    except Exception as e:
        mexc_positions = []
        await update.message.reply_text(
            f"⚠️ Failed to fetch MEXC positions: {e}\nContinuing without position check.",
            parse_mode="HTML",
        )

    active_positions = [p for p in mexc_positions if float(p.get("holdVol") or p.get("vol") or 0) > 0]

    if active_positions:
        context.user_data["aa_mexc_positions"] = mexc_positions
        lines = [f"<b>Open positions on MEXC ({len(active_positions)}):</b>"]
        total_pnl = 0.0
        for p in active_positions:
            text, pnl_usdt = _build_position_line(trader, p)
            lines.append(text)
            total_pnl += pnl_usdt
        total_sign = "+" if total_pnl >= 0 else ""
        total_emoji = "🟢" if total_pnl >= 0 else "🔴"
        lines.append(f"\n{total_emoji} <b>Total PnL: {total_sign}{total_pnl:.2f} USDT</b>")

        await update.message.reply_text(
            "\n".join(lines) + "\n\nWhat to do with positions?",
            parse_mode="HTML",
            reply_markup=_build_close_positions_keyboard(mexc_positions),
        )
        return AA_CLOSE_POSITIONS

    # No open positions — start ticker loop immediately
    context.user_data["aa_queue"] = list(ASSETS)
    return await _continue_ticker_loop(update.message, context)


async def aa_close_positions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button press for close positions step."""
    query = update.callback_query
    await query.answer()

    data = query.data  # "close_pos:SKIP" | "close_pos:ALL" | "close_pos:BTC_USDT:L"
    mexc_positions = context.user_data.get("aa_mexc_positions", [])
    trader = MEXCTrader()

    if data == "close_pos:SKIP":
        await query.edit_message_text("⏭ Positions kept.", parse_mode="HTML")

    elif data == "close_pos:ALL":
        lines = ["<b>Closing all positions...</b>"]
        for p in mexc_positions:
            sym_raw = p.get("symbol", "")
            sym = sym_raw.replace("_", "")
            direction = "long" if int(p.get("positionType", 1)) == 1 else "short"
            try:
                result = trader.close_position_limit(sym, direction)
                if result:
                    lines.append(f"✅ {sym_raw} {direction.upper()} — limit @ {result['price']}")
                    _close_local_position(sym, result["price"])
                else:
                    lines.append(f"⚠️ {sym_raw} — failed to close")
            except Exception as e:
                lines.append(f"❌ {sym_raw} — error: {e}")
        await query.edit_message_text("\n".join(lines), parse_mode="HTML")

    else:
        # "close_pos:BTC_USDT:L"
        parts = data.split(":")
        if len(parts) == 3:
            sym_raw = parts[1]
            direction_code = parts[2]
            sym = sym_raw.replace("_", "")
            direction = "long" if direction_code == "L" else "short"
            try:
                result = trader.close_position_limit(sym, direction)
                if result:
                    await query.edit_message_text(
                        f"✅ <b>{sym_raw}</b> {direction.upper()} closed at limit @ {result['price']}",
                        parse_mode="HTML",
                    )
                    _close_local_position(sym, result["price"])
                else:
                    await query.edit_message_text(f"⚠️ Failed to close {sym_raw}.", parse_mode="HTML")
            except Exception as e:
                await query.edit_message_text(f"❌ Error closing {sym_raw}: {e}", parse_mode="HTML")
        else:
            await query.edit_message_text("⚠️ Unknown command.", parse_mode="HTML")

    # Start ticker loop
    context.user_data["aa_queue"] = list(ASSETS)
    return await _continue_ticker_loop(query.message, context)


async def aa_conflict_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user answer to conflict question (replace/skip)."""
    query = update.callback_query
    await query.answer()

    # data: "conflict:replace:BTCUSDT" | "conflict:skip:BTCUSDT"
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else "skip"
    symbol = parts[2] if len(parts) > 2 else ""

    signal = context.user_data.get("aa_conflict_signal", {})

    if action == "replace" and signal:
        await query.edit_message_text(
            f"🔄 <b>{symbol}</b> — cancelling existing order/position and placing new...",
            parse_mode="HTML",
        )
        try:
            trader = MEXCTrader()
            # Close open position if exists
            if trader.has_open_position(symbol):
                direction = signal.get("direction", "long")
                trader.close_position_limit(symbol, direction)
            # Cancel open orders if any
            trader.cancel_all_orders(symbol)
            # Now place new order
            result = swing_process_signal(signal)
            if result["action"] == "orders_placed":
                n = len(result["orders_placed"])
                entry1 = signal.get("entry1", "—")
                entry2 = signal.get("entry2")
                e2_str = f" / {entry2}" if entry2 else ""
                sl = signal.get("sl", "—")
                tp1 = signal.get("tp1", "—")
                direction_label = "🟢 LONG" if signal.get("direction") == "long" else "🔴 SHORT"
                await query.message.reply_text(
                    f"✅ <b>{symbol}</b> {direction_label} — {n} order(s) placed\n"
                    f"Entry: <b>{entry1}</b>{e2_str}  SL: <b>{sl}</b>  TP1: <b>{tp1}</b>",
                    parse_mode="HTML",
                )
            else:
                await query.message.reply_text(
                    f"⚠️ <b>{symbol}</b> — order not placed: {result['reason']}",
                    parse_mode="HTML",
                )
        except Exception as e:
            await query.message.reply_text(
                f"❌ <b>{symbol}</b> — replace error: {e}",
                parse_mode="HTML",
            )
    else:
        await query.edit_message_text(
            f"⏭ <b>{symbol}</b> — skipped.",
            parse_mode="HTML",
        )

    # Continue with remaining tickers
    return await _continue_ticker_loop(query.message, context)


async def aa_rebalance_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle rebalance yes/skip after analyze_all scan."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "skip":
        await query.message.reply_text("⏭ Rebalance skipped.", parse_mode="HTML")
        return ConversationHandler.END

    await query.message.reply_text("⚖️ Rebalancing orders...", parse_mode="HTML")
    try:
        rb = rebalance_orders(list(ASSETS))
        await query.message.reply_text(_format_rebalance_result(rb), parse_mode="HTML")
    except Exception as e:
        await query.message.reply_text(f"⚠️ Rebalance error: {e}", parse_mode="HTML")

    return ConversationHandler.END


async def cmd_rebalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rebalance — standalone rebalance command."""
    await update.message.reply_text("⚖️ Rebalancing orders...", parse_mode="HTML")
    try:
        rb = rebalance_orders(list(ASSETS))
        await update.message.reply_text(_format_rebalance_result(rb), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Rebalance error: {e}", parse_mode="HTML")


async def cmd_trail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/trail — check trailing stops and show suggestions (notification only, no auto-changes)."""
    await update.message.reply_text("🔍 Checking positions...", parse_mode="HTML")
    try:
        results = check_trailing_stops()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}", parse_mode="HTML")
        return

    to_move = [r for r in results if r["action"] == "move_sl"]
    already = [r for r in results if r["action"] == "already_protected"]

    if not results:
        await update.message.reply_text("No open positions to check.", parse_mode="HTML")
        return

    lines = ["🎯 <b>Trailing stop suggestions</b>\n"]

    for r in to_move:
        pnl_sign = "+" if r['pnl_pct'] >= 0 else ""
        sl_str = f"{r['current_sl']}" if r['current_sl'] > 0 else "none"
        lines.append(
            f"⚠️ <b>{r['symbol']}</b> {r['direction'].upper()}  PnL: {pnl_sign}{r['pnl_pct']*100:.0f}%\n"
            f"   Move SL: {sl_str} → <b>{r['new_sl']}</b>"
        )

    for r in already:
        lines.append(f"✅ <b>{r['symbol']}</b> — already protected (SL {r['current_sl']})")

    if not to_move:
        lines.append("No adjustments needed.")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def _format_rebalance_result(rb: dict) -> str:
    if not rb["rebalanced"] and not rb["cancelled"] and not rb["errors"]:
        return "⚖️ <b>Orders already balanced</b> — no changes needed"

    lines = [f"⚖️ <b>Rebalance complete</b>  target margin: <b>{rb['target_margin']:.2f} USDT</b> each\n"]
    if rb["rebalanced"]:
        for r in rb["rebalanced"]:
            lines.append(f"  🔄 <b>{r['symbol']}</b>  vol {r['old_vol']} → {r['new_vol']}  @ {r['price']}")
    if rb["cancelled"]:
        for c in rb["cancelled"]:
            lines.append(f"  ❌ <b>{c['symbol']}</b>  cancelled (vol &lt; 1)")
    if rb["skipped"]:
        lines.append(f"  ⏭ {len(rb['skipped'])} order(s) already balanced")
    if rb["errors"]:
        lines.append(f"  ⚠️ Errors: {', '.join(rb['errors'])}")
    return "\n".join(lines)


def _close_local_position(symbol: str, close_price: float):
    """Close matching open position in local DB if exists (best-effort)."""
    try:
        positions = get_open_positions()
        sym_upper = symbol.upper()
        if not sym_upper.endswith("USDT"):
            sym_upper = sym_upper + "USDT"
        for p in positions:
            if p["symbol"].upper() == sym_upper and p["status"] == "open":
                close_position(p["id"], close_price, close_reason="closed_via_analyze_all")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# intraday analyze / intraday_all
# ---------------------------------------------------------------------------

async def cmd_intraday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Specify symbol: /intraday BTC")
        return

    symbol = args[0].upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"

    await update.message.reply_text(f"⏳ Intraday analysis {symbol}...")

    try:
        agent = IntradayAgent()
        signal = agent.analyze(symbol)
        text = format_intraday_signal(signal)
        await _send_long(update, text)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_intraday_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"⏳ Intraday analysis — {len(ASSETS)} assets...")

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
    await update.message.reply_text("Asset (e.g. BTC):")
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
        await update.message.reply_text("Enter long or short:")
        return EX_DIRECTION
    context.user_data["direction"] = val
    await update.message.reply_text("Entry 1:", reply_markup=ReplyKeyboardRemove())
    return EX_ENTRY1


async def ex_entry1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["entry1"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Enter a number:")
        return EX_ENTRY1
    await update.message.reply_text("Entry 2 (or /skip):")
    return EX_ENTRY2


async def ex_entry2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["entry2"] = float(update.message.text.strip())
    except ValueError:
        context.user_data["entry2"] = None
    await update.message.reply_text("Stop-loss:")
    return EX_SL


async def ex_sl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["sl"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Enter a number:")
        return EX_SL
    await update.message.reply_text("TP1:")
    return EX_TP1


async def ex_tp1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["tp1"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Enter a number:")
        return EX_TP1
    await update.message.reply_text("TP2 (or /skip):")
    return EX_TP2


async def ex_tp2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["tp2"] = float(update.message.text.strip())
    except ValueError:
        context.user_data["tp2"] = None
    await update.message.reply_text("Date YYYY-MM-DD (or /skip for today):")
    return EX_DATE


async def ex_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["trade_date"] = update.message.text.strip() or None
    await update.message.reply_text("Notes (or /skip):")
    return EX_NOTES


async def ex_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["notes"] = update.message.text.strip() or None
    d = context.user_data
    preview = (
        f"<b>Confirm:</b>\n\n"
        f"Asset: <b>{d['asset']}</b>\n"
        f"Direction: <b>{d['direction']}</b>\n"
        f"Entry 1: <b>{d['entry1']}</b>\n"
        f"Entry 2: <b>{d.get('entry2', '—')}</b>\n"
        f"Stop: <b>{d['sl']}</b>\n"
        f"TP1: <b>{d['tp1']}</b>\n"
        f"TP2: <b>{d.get('tp2', '—')}</b>\n"
        f"Date: <b>{d.get('trade_date', 'today')}</b>\n"
        f"Notes: <b>{d.get('notes', '—')}</b>"
    )
    await update.message.reply_text(
        preview,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([["✅ Save", "❌ Cancel"]], one_time_keyboard=True, resize_keyboard=True),
    )
    return EX_CONFIRM


async def ex_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "Save" not in text:
        await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    d = context.user_data
    await update.message.reply_text("⏳ Saving...", reply_markup=ReplyKeyboardRemove())

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
        await update.message.reply_text(f"✅ Example #{example_id} saved.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

    return ConversationHandler.END


async def ex_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /skip command — move to next field with None."""
    state = context.user_data.get("_state")
    # Just send empty string to trigger the current handler
    update.message.text = ""
    return None


async def ex_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# positions — add_position conversation
# ---------------------------------------------------------------------------

async def pos_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Symbol (e.g. BTC or BTCUSDT):")
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
        await update.message.reply_text("Enter long or short:")
        return POS_DIRECTION
    context.user_data["direction"] = val
    await update.message.reply_text("Position size in USD:", reply_markup=ReplyKeyboardRemove())
    return POS_SIZE


async def pos_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["size_usd"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Enter a number:")
        return POS_SIZE
    await update.message.reply_text("Leverage (Enter — default 10):")
    return POS_LEVERAGE


async def pos_leverage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        context.user_data["leverage"] = int(text) if text else 10
    except ValueError:
        await update.message.reply_text("Enter an integer (or Enter for 10):")
        return POS_LEVERAGE
    await update.message.reply_text("Entry price:")
    return POS_ENTRY


async def pos_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["entry_price"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Enter a number:")
        return POS_ENTRY
    await update.message.reply_text("Stop-loss:")
    return POS_SL


async def pos_sl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["sl_price"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Enter a number:")
        return POS_SL
    await update.message.reply_text("TP1:")
    return POS_TP1


async def pos_tp1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["tp1_price"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Enter a number:")
        return POS_TP1
    await update.message.reply_text("TP2 (or /skip):")
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
        f"<b>Confirm:</b>\n\n"
        f"Symbol: <b>{d['symbol']}</b>\n"
        f"Direction: <b>{d['direction'].upper()}</b>\n"
        f"Size: <b>${d['size_usd']}</b> × {d.get('leverage', 10)}x = <b>${exposure}</b> exposure\n"
        f"Entry: <b>{d['entry_price']}</b>\n"
        f"Stop: <b>{d['sl_price']}</b>\n"
        f"TP1: <b>{d['tp1_price']}</b>\n"
        f"TP2: <b>{d.get('tp2_price') or '—'}</b>"
    )
    await update.message.reply_text(
        preview,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([["✅ Save", "❌ Cancel"]], one_time_keyboard=True, resize_keyboard=True),
    )
    return POS_CONFIRM


async def pos_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "Save" not in text:
        await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    d = context.user_data
    await update.message.reply_text("Saving...", reply_markup=ReplyKeyboardRemove())

    try:
        pos_id = insert_position(d)
        await update.message.reply_text(f"✅ Position <b>#{pos_id}</b> opened.", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

    return ConversationHandler.END


async def pos_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# positions — /positions list
# ---------------------------------------------------------------------------

async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    positions = get_open_positions()
    if not positions:
        await update.message.reply_text("No open positions.")
        return

    lines = ["<b>Open positions:</b>\n"]
    for p in positions:
        direction = "🟢 LONG" if p["direction"] == "long" else "🔴 SHORT"
        tp2 = f"  TP2: <b>{p['tp2_price']}</b>" if p.get("tp2_price") else ""
        lines.append(
            f"<b>#{p['id']} {p['symbol']}</b> {direction}\n"
            f"  Entry: <b>{p['entry_price']}</b>  SL: <b>{p['sl_price']}</b>\n"
            f"  TP1: <b>{p['tp1_price']}</b>{tp2}\n"
            f"  Size: ${p['size_usd']} × {p['leverage']}x\n"
            f"  Opened: {p['opened_at'][:16]}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# positions — /close ID price
# ---------------------------------------------------------------------------

async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Format: /close <ID> <price>\nExample: /close 3 96500")
        return

    try:
        pos_id = int(args[0])
        close_price = float(args[1])
    except ValueError:
        await update.message.reply_text("Invalid format. Example: /close 3 96500")
        return

    pos = get_position_by_id(pos_id)
    if not pos:
        await update.message.reply_text(f"Position #{pos_id} not found.")
        return
    if pos["status"] == "closed":
        await update.message.reply_text(f"Position #{pos_id} is already closed.")
        return

    result = close_position(pos_id, close_price)
    if not result:
        await update.message.reply_text("Error closing position.")
        return

    pnl = result["pnl_usd"]
    pnl_pct = result["pnl_percent"]
    emoji = "🟢" if pnl >= 0 else "🔴"

    await update.message.reply_text(
        f"{emoji} <b>Position #{pos_id} closed</b>\n\n"
        f"Symbol: <b>{result['symbol']}</b>\n"
        f"Entry: <b>{result['entry_price']}</b> → Exit: <b>{close_price}</b>\n"
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
        "hold": "HOLD",
        "adjust_tp": "ADJUST TP",
        "partial_exit": "PARTIAL EXIT",
        "exit_now": "EXIT NOW",
    }.get(action, action.upper())

    lines = [
        f"{action_emoji} <b>#{pos.get('id', '?')} {pos.get('symbol', '?')} "
        f"{'LONG' if pos.get('direction') == 'long' else 'SHORT'}</b>",
        f"Recommendation: <b>{action_label}</b> (confidence: {confidence})",
        "",
    ]

    # SL to breakeven
    if result.get("move_sl_to_breakeven"):
        lines.append("🔒 <b>Move SL to breakeven</b>")
        lines.append("")

    # Action-specific details
    if action == "adjust_tp":
        new_tp1 = result.get("suggested_tp1")
        new_tp2 = result.get("suggested_tp2")
        if new_tp1:
            lines.append(f"New TP1: <b>{new_tp1}</b>")
        if new_tp2:
            lines.append(f"New TP2: <b>{new_tp2}</b>")
        lines.append("")

    elif action == "partial_exit":
        pct = result.get("partial_exit_pct")
        exit_price = result.get("exit_price_suggestion")
        new_tp1 = result.get("suggested_tp1")
        if pct:
            lines.append(f"Close: <b>{pct}%</b> of position")
        if exit_price:
            lines.append(f"Exit price: <b>{exit_price}</b>")
        if new_tp1:
            lines.append(f"TP1 for remainder: <b>{new_tp1}</b>")
        lines.append("")

    elif action == "exit_now":
        exit_price = result.get("exit_price_suggestion")
        if exit_price:
            lines.append(f"Exit price: <b>{exit_price}</b>")
        lines.append("")

    # Sub-agent verdicts summary
    macro = result.get("_macro", {})
    local = result.get("_local", {})
    momentum = result.get("_momentum", {})

    lines.append("<b>Analysis:</b>")
    lines.append(f"  Macro: {macro.get('macro_verdict', '—')} | trend {macro.get('macro_trend', '—')} ({macro.get('trend_health', '—')})")
    lines.append(f"  Local (4h): {local.get('local_verdict', '—')} | momentum {local.get('momentum_4h', '—')}")
    lines.append(f"  Momentum (1h): reversal risk {result.get('reversal_risk', '—')} | sentiment {momentum.get('sentiment_verdict', '—')}")
    lines.append("")

    lines.append(f"<b>Reasoning:</b>\n{reasoning}")
    lines.append("")
    lines.append(f"<b>Risks:</b>\n{risks}")

    return "\n".join(lines)


async def cmd_check_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Format: /check_position <ID>\nExample: /check_position 3")
        return

    try:
        pos_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID must be a number.")
        return

    pos = get_position_by_id(pos_id)
    if not pos:
        await update.message.reply_text(f"Position #{pos_id} not found.")
        return
    if pos["status"] == "closed":
        await update.message.reply_text(f"Position #{pos_id} is already closed.")
        return

    await update.message.reply_text(f"⏳ Analyzing position #{pos_id} ({pos['symbol']})...")

    try:
        agent = ExitAgent()
        result = agent.check_position(pos_id)
        text = _format_exit_result(result)
        await _send_long(update, text)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


def _get_position_sl_tp(trader: MEXCTrader, symbol: str) -> tuple[float, float]:
    """Fetch SL and TP for an open position from stop orders."""
    sl, tp = 0.0, 0.0
    try:
        stop_orders = trader.get_stop_orders(symbol)
        for o in stop_orders:
            if o.get("stopLossPrice"):
                sl = float(o["stopLossPrice"])
            if o.get("takeProfitPrice"):
                tp = float(o["takeProfitPrice"])
    except Exception:
        pass
    return sl, tp


def _build_position_line(trader: MEXCTrader, p: dict) -> tuple[str, float]:
    """
    Build a formatted position string and return (text, pnl_usdt).
    Fetches mark price from ticker and SL/TP from stop orders.
    """
    sym = p.get("symbol", "?")
    pos_type = int(p.get("positionType", 1))
    direction = "🟢 LONG" if pos_type == 1 else "🔴 SHORT"
    vol = float(p.get("holdVol") or p.get("vol") or 0)
    entry = float(p.get("holdAvgPrice") or p.get("openAvgPrice") or 0)
    leverage = int(p.get("leverage", 10))
    im = float(p.get("im") or p.get("oim") or 0)

    # Mark price from ticker
    mark = 0.0
    try:
        ticker_resp = trader.session.get(
            f"{trader.base_url}/api/v1/contract/ticker",
            params={"symbol": sym}, timeout=20,
        )
        mark = float(ticker_resp.json().get("data", {}).get("lastPrice", 0))
    except Exception:
        pass

    # SL/TP from stop orders
    sl, tp = _get_position_sl_tp(trader, sym)

    # PnL
    pnl_usdt, pnl_pct = 0.0, 0.0
    if mark > 0 and entry > 0 and vol > 0:
        contract_size, _ = trader.get_contract_size(sym.replace("_", ""))
        if pos_type == 1:
            pnl_usdt = (mark - entry) * vol * contract_size
        else:
            pnl_usdt = (entry - mark) * vol * contract_size
        pnl_pct = (pnl_usdt / im * 100) if im > 0 else 0

    pnl_sign = "+" if pnl_usdt >= 0 else ""
    pnl_emoji = "🟢" if pnl_usdt >= 0 else "🔴"
    sl_str = f"{sl}" if sl > 0 else "—"
    tp_str = f"{tp}" if tp > 0 else "—"
    mark_str = f"{mark}" if mark > 0 else "—"

    text = (
        f"\n<b>{sym}</b> {direction}  {leverage}x\n"
        f"  Entry: <b>{entry}</b>   Mark: {mark_str}   Vol: {int(vol)}\n"
        f"  Margin: <b>{im:.2f} USDT</b>\n"
        f"  SL: {sl_str}   TP: {tp_str}\n"
        f"  PnL: {pnl_emoji} <b>{pnl_sign}{pnl_usdt:.2f} USDT ({pnl_sign}{pnl_pct:.1f}%)</b>"
    )
    return text, pnl_usdt


async def cmd_check_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        trader = MEXCTrader()
        positions = trader.get_open_positions()
        equity = trader.get_equity()
        balance = trader.get_balance()
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to fetch data from MEXC: {e}")
        return

    lines = [
        f"💼 <b>Account</b>",
        f"  Equity:    <b>{equity:.2f} USDT</b>",
        f"  Available: <b>{balance:.2f} USDT</b>",
    ]

    active = [p for p in positions if float(p.get("holdVol") or p.get("vol") or 0) > 0]

    if not active:
        lines.append("\nNo open positions.")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    total_pnl = 0.0
    lines.append(f"\n📊 <b>Open positions ({len(active)})</b>")

    for p in active:
        text, pnl_usdt = _build_position_line(trader, p)
        lines.append(text)
        total_pnl += pnl_usdt

    total_sign = "+" if total_pnl >= 0 else ""
    total_emoji = "🟢" if total_pnl >= 0 else "🔴"
    lines.append(f"\n{total_emoji} <b>Total PnL: {total_sign}{total_pnl:.2f} USDT</b>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run():
    if not TELEGRAM_TOKEN:
        print("TELEGRAM_TOKEN is not set in .env")
        return

    init_positions_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    async def skip_entry2(u, c):
        c.user_data["entry2"] = None
        await u.message.reply_text("Stop-loss:")
        return EX_SL

    async def skip_tp2(u, c):
        c.user_data["tp2"] = None
        await u.message.reply_text("Date YYYY-MM-DD (or /skip for today):")
        return EX_DATE

    async def skip_date(u, c):
        c.user_data["trade_date"] = None
        await u.message.reply_text("Notes (or /skip):")
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

    analyze_all_conv = ConversationHandler(
        entry_points=[
            CommandHandler("analyze_all", cmd_analyze_all),
            MessageHandler(filters.Text(["📊 Analyze All"]), cmd_analyze_all),
        ],
        states={
            AA_CLOSE_POSITIONS: [
                CallbackQueryHandler(aa_close_positions_callback, pattern=r"^close_pos:"),
            ],
            AA_CONFLICT_QUESTION: [
                CallbackQueryHandler(aa_conflict_callback, pattern=r"^conflict:"),
            ],
            AA_REBALANCE_CONFIRM: [
                CallbackQueryHandler(aa_rebalance_confirm_callback, pattern=r"^rebalance:"),
            ],
        },
        fallbacks=[],
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("analyze", cmd_analyze))

    # analyze_all ConversationHandler must be registered before generic text handlers
    app.add_handler(analyze_all_conv)
    app.add_handler(CommandHandler("intraday", cmd_intraday))
    app.add_handler(CommandHandler("intraday_all", cmd_intraday_all))
    app.add_handler(add_example_conv)
    app.add_handler(add_position_conv)
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("check_position", cmd_check_position))
    app.add_handler(CommandHandler("check_all", cmd_check_all))
    app.add_handler(CommandHandler("rebalance", cmd_rebalance))
    app.add_handler(CommandHandler("trail", cmd_trail))

    # Persistent reply keyboard buttons
    menu_filter = filters.Text([
        "⚖️ Rebalance", "🔍 Analyze Symbol", "📋 Check Orders", "📈 Intraday All", "🎯 Trail Stops",
    ])
    app.add_handler(MessageHandler(menu_filter, handle_menu_buttons))
    # Symbol input after tapping 🔍 Analyze Symbol — only fires when flag is set
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_symbol_input))

    print("Bot started. Waiting for commands...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run()
