import sys
import json
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box
from examples.db import init_db, get_all_examples, update_outcome, delete_example
from examples.manager import add_example
from agent.analyzer import TradingAgent
from backtest.engine import BacktestEngine
from backtest.full_pipeline import FullPipelineBacktest
from examples.auto_teacher import AutoTeacher, MARKET_PHASES
from config.settings import ASSETS
from telegram_bot.sender import send_signal, send_analyze_all
from telegram_bot.bot import run as run_bot
from intraday_agent.analyzer import IntradayAgent
from intraday_backtest.engine import IntradayBacktestEngine
from trading.order_manager import process_signal
from trading.scheduler import run as run_scheduler, run_once
from intraday_examples.db import init_db as init_intraday_db, get_all_examples as get_intraday_examples, update_outcome as update_intraday_outcome, delete_example as delete_intraday_example

console = Console()


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def print_signal(signal: dict):
    direction = signal.get("direction", "N/A")
    color = "green" if direction == "long" else "red" if direction == "short" else "yellow"

    entry_analysis = signal.get("entry_analysis") or {}
    has_proposed = not signal.get("has_setup") and entry_analysis.get("entry1")

    proposed_section = ""
    if has_proposed:
        proposed_section = (
            f"[dim]Proposed (rejected):[/dim]\n"
            f"Entry 1:  [dim]{entry_analysis.get('entry1')}[/dim]\n"
            f"Entry 2:  [dim]{entry_analysis.get('entry2', '—')}[/dim]\n"
            f"Stop Loss: [dim]{entry_analysis.get('sl')}[/dim]\n"
            f"TP1:      [dim]{entry_analysis.get('tp1')}[/dim]\n"
            f"TP2:      [dim]{entry_analysis.get('tp2', '—')}[/dim]\n"
            f"RR:       [dim]{entry_analysis.get('risk_reward', '—')}[/dim]\n\n"
        )

    watch = signal.get("watch_level")
    watch_section = f"[yellow]Watch: {watch}[/yellow]\n\n" if watch else ""

    vol = signal.get("volatility_analysis") or {}
    vol_label = f"  |  {vol.get('regime', '')} vol ({vol.get('sl_buffer_pct', '')} SL)" if vol.get("regime") else ""

    console.print(Panel(
        f"[bold {color}]{signal.get('symbol')} — {direction.upper() if direction else 'NO SETUP'}[/bold {color}]\n"
        f"Confidence: [bold]{signal.get('confidence', 'N/A')}[/bold]{vol_label}\n\n"
        + (
            f"Entry 1:  [bold]{signal.get('entry1')}[/bold]\n"
            f"Entry 2:  [bold]{signal.get('entry2', '—')}[/bold]\n"
            f"Stop Loss: [bold red]{signal.get('sl')}[/bold red]\n"
            f"TP1:      [bold green]{signal.get('tp1')}[/bold green]\n"
            f"TP2:      [bold green]{signal.get('tp2', '—')}[/bold green]\n\n"
            if signal.get("has_setup") else "[yellow]No setup found at this time.[/yellow]\n\n"
        )
        + proposed_section
        + watch_section
        + f"[italic]{signal.get('reasoning', '')}[/italic]\n\n"
        + (f"[dim]Risks: {signal.get('risks', '')}[/dim]" if signal.get("risks") else ""),
        title="[bold]Trading Signal[/bold]",
        border_style=color,
    ))

    if signal.get("key_levels_used"):
        console.print("[dim]Key levels used:[/dim]")
        for lvl in signal["key_levels_used"]:
            console.print(f"  • {lvl}")

    console.print(f"\n[dim]Similar examples used: {signal.get('similar_examples_count', 0)}[/dim]")


def print_backtest_results(result: dict):
    m = result.get("metrics", {})
    console.print(Panel(
        f"Symbol: [bold]{result['symbol']}[/bold]  |  "
        f"{result['date_from']} → {result['date_to']}\n\n"
        f"Total signals:   [bold]{m.get('total_signals', 0)}[/bold]\n"
        f"Wins (TP hit):   [bold green]{m.get('wins', 0)}[/bold green]\n"
        f"Losses (SL hit): [bold red]{m.get('losses', 0)}[/bold red]\n"
        f"Open/expired:    [bold yellow]{m.get('open', 0)}[/bold yellow]\n"
        f"Win rate:        [bold]{m.get('win_rate_pct', 0)}%[/bold]\n"
        f"Avg Risk/Reward: [bold]{m.get('avg_risk_reward', 'N/A')}[/bold]",
        title="[bold]Backtest Results[/bold]",
        border_style="blue",
    ))

    signals_with_setup = [s for s in result["signals"] if s.get("has_setup")]
    if signals_with_setup:
        table = Table(title="Signals detail", box=box.SIMPLE)
        table.add_column("Date", style="dim")
        table.add_column("Direction")
        table.add_column("Entry")
        table.add_column("SL")
        table.add_column("TP1")
        table.add_column("RR")
        table.add_column("Confidence")
        table.add_column("Outcome")

        for s in signals_with_setup:
            outcome = (s.get("backtest_outcome") or {}).get("result", "—")
            color = "green" if "tp" in outcome else "red" if "sl" in outcome else "yellow"
            # Calculate actual RR
            rr_display = "—"
            entry = s.get("entry1")
            sl = s.get("sl")
            tp1 = s.get("tp1")
            if entry and sl and tp1:
                risk = abs(entry - sl)
                reward = abs(tp1 - entry)
                if risk > 0:
                    rr_val = round(reward / risk, 2)
                    rr_color = "green" if rr_val >= 2 else "yellow" if rr_val >= 1.5 else "red"
                    rr_display = f"[{rr_color}]{rr_val}[/{rr_color}]"
            table.add_row(
                s.get("date", "—"),
                s.get("direction", "—"),
                str(s.get("entry1", "—")),
                str(s.get("sl", "—")),
                str(s.get("tp1", "—")),
                rr_display,
                s.get("confidence", "—"),
                f"[{color}]{outcome}[/{color}]",
            )
        console.print(table)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_analyze(args: list[str]):
    """analyze [SYMBOL] [--liq 80000,79000] [--tg]"""
    symbol = args[0].upper() if args else None
    liquidity_levels = None
    send_tg = "--tg" in args

    if "--liq" in args:
        idx = args.index("--liq")
        if idx + 1 < len(args):
            liquidity_levels = [float(x) for x in args[idx + 1].split(",")]

    if not symbol:
        console.print("[yellow]Usage: analyze SYMBOL [--liq 80000,79000] [--tg][/yellow]")
        return

    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"

    console.print(f"\n[bold]Analyzing {symbol}...[/bold]")
    agent = TradingAgent()
    signal = agent.analyze(symbol, liquidity_levels=liquidity_levels)
    print_signal(signal)

    if send_tg:
        send_signal(signal)
        console.print("[dim]→ Sent to Telegram[/dim]")


def cmd_analyze_all(args: list[str]):
    """analyze-all [--tg] — run analysis for all configured assets"""
    send_tg = "--tg" in args
    agent = TradingAgent()
    all_signals = []
    for asset in ASSETS:
        console.print(f"\n[bold]— {asset} —[/bold]")
        try:
            signal = agent.analyze(asset)
            print_signal(signal)
            all_signals.append(signal)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    if send_tg and all_signals:
        send_analyze_all(all_signals)
        console.print("\n[dim]→ Sent to Telegram[/dim]")


def cmd_add_example(args: list[str]):
    """Interactive prompt to add a new trade example."""
    console.print("\n[bold]Add trade example[/bold]")

    asset = console.input("Asset (e.g. BTC): ").strip().upper()
    direction = console.input("Direction (long/short): ").strip().lower()
    entry1 = float(console.input("Entry 1: ").strip())
    entry2_raw = console.input("Entry 2 (or enter to skip): ").strip()
    entry2 = float(entry2_raw) if entry2_raw else None
    sl = float(console.input("Stop Loss: ").strip())
    tp1 = float(console.input("TP1: ").strip())
    tp2_raw = console.input("TP2 (or enter to skip): ").strip()
    tp2 = float(tp2_raw) if tp2_raw else None
    date_raw = console.input("Date YYYY-MM-DD (or enter for today): ").strip()
    trade_date = date_raw if date_raw else None
    notes = console.input("Notes (optional): ").strip() or None
    liq_raw = console.input("Liquidation levels, comma-separated (or enter to skip): ").strip()
    liq_levels = [float(x) for x in liq_raw.split(",")] if liq_raw else None

    example_id = add_example(
        asset=asset,
        direction=direction,
        entry1=entry1,
        entry2=entry2,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        trade_date=trade_date,
        notes=notes,
        liquidity_levels=liq_levels,
    )
    console.print(f"\n[green]Example #{example_id} added successfully.[/green]")


def cmd_list_examples(args: list[str]):
    """list-examples [SYMBOL]"""
    asset = args[0].upper() if args else None
    examples = get_all_examples(asset)

    if not examples:
        console.print("[yellow]No examples found.[/yellow]")
        return

    table = Table(title=f"Examples{' for ' + asset if asset else ''}", box=box.SIMPLE)
    table.add_column("ID", style="dim")
    table.add_column("Asset")
    table.add_column("Date")
    table.add_column("Direction")
    table.add_column("Entry1")
    table.add_column("SL")
    table.add_column("TP1")
    table.add_column("Outcome")
    table.add_column("Notes", max_width=30)

    for ex in examples:
        direction = ex["direction"]
        color = "green" if direction == "long" else "red"
        outcome = ex.get("outcome") or "—"
        table.add_row(
            str(ex["id"]),
            ex["asset"],
            ex["trade_date"],
            f"[{color}]{direction}[/{color}]",
            str(ex["entry1"]),
            str(ex["sl"]),
            str(ex["tp1"]),
            outcome,
            ex.get("notes") or "—",
        )

    console.print(table)


def cmd_update_outcome(args: list[str]):
    """update-outcome ID OUTCOME"""
    if len(args) < 2:
        console.print("[yellow]Usage: update-outcome ID OUTCOME[/yellow]")
        console.print("  OUTCOME: tp1_hit | tp2_hit | sl_hit | open")
        return
    example_id = int(args[0])
    outcome = args[1]
    update_outcome(example_id, outcome)
    console.print(f"[green]Example #{example_id} outcome updated to '{outcome}'.[/green]")


def cmd_delete_example(args: list[str]):
    """delete-example ID"""
    if not args:
        console.print("[yellow]Usage: delete-example ID[/yellow]")
        return
    example_id = int(args[0])
    deleted = delete_example(example_id)
    if deleted:
        console.print(f"[green]Example #{example_id} deleted.[/green]")
    else:
        console.print(f"[red]Example #{example_id} not found.[/red]")


def _parse_rag_source(args: list[str]) -> str | None:
    """Parse --rag flag: manual | auto | all (None). Defaults to settings.RAG_SOURCE."""
    if "--rag" in args:
        val = args[args.index("--rag") + 1]
        return None if val == "all" else val
    from config.settings import RAG_SOURCE
    return RAG_SOURCE


def cmd_backtest(args: list[str]):
    """backtest SYMBOL DATE_FROM DATE_TO [--step N] [--liq prices] [--no-rag] [--rag manual|auto|all]"""
    if len(args) < 3:
        console.print("[yellow]Usage: backtest SYMBOL YYYY-MM-DD YYYY-MM-DD [--step N] [--liq prices] [--no-rag] [--rag manual|auto|all][/yellow]")
        return

    symbol = args[0].upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    date_from = args[1]
    date_to = args[2]
    step_days = 7
    liquidity_levels = None
    use_rag = "--no-rag" not in args
    rag_source = _parse_rag_source(args)

    if "--step" in args:
        idx = args.index("--step")
        step_days = int(args[idx + 1])

    if "--liq" in args:
        idx = args.index("--liq")
        liquidity_levels = [float(x) for x in args[idx + 1].split(",")]

    engine = BacktestEngine(rag_source=rag_source)
    result = engine.run(
        symbol=symbol,
        date_from=date_from,
        date_to=date_to,
        step_days=step_days,
        liquidity_levels=liquidity_levels,
        use_rag=use_rag,
    )
    print_backtest_results(result)


def cmd_teach(args: list[str]):
    """teach SYMBOL DATE_FROM DATE_TO --phase PHASE [--step N] [--lookahead N] [--min-move N] [--rag auto|manual|all]"""
    if len(args) < 3:
        console.print(
            "[yellow]Usage: teach SYMBOL YYYY-MM-DD YYYY-MM-DD --phase PHASE\n"
            f"  Phases: {', '.join(MARKET_PHASES)}\n"
            "  Options: --step N (default 7) | --lookahead N (default 14) | --min-move N (default 10)[/yellow]"
        )
        return

    symbol = args[0].upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    date_from = args[1]
    date_to = args[2]

    if "--phase" not in args:
        console.print(f"[red]--phase is required. Options: {', '.join(MARKET_PHASES)}[/red]")
        return

    phase = args[args.index("--phase") + 1]
    if phase not in MARKET_PHASES:
        console.print(f"[red]Invalid phase '{phase}'. Options: {', '.join(MARKET_PHASES)}[/red]")
        return

    step_days = 7
    lookahead_days = 14
    min_move_pct = 10.0

    if "--step" in args:
        step_days = int(args[args.index("--step") + 1])
    if "--lookahead" in args:
        lookahead_days = int(args[args.index("--lookahead") + 1])
    if "--min-move" in args:
        min_move_pct = float(args[args.index("--min-move") + 1])

    teacher = AutoTeacher()
    saved = teacher.run(
        symbol=symbol,
        date_from=date_from,
        date_to=date_to,
        market_phase=phase,
        step_days=step_days,
        lookahead_days=lookahead_days,
        min_move_pct=min_move_pct,
    )
    console.print(f"\n[green]Done. {saved} examples saved to database (source=auto, phase={phase}).[/green]")


def cmd_backtest_full(args: list[str]):
    """backtest-full SYMBOL DATE_FROM DATE_TO [--step N] [--leverage N] [--size N] [--no-rag] [--rag manual|auto|all]"""
    if len(args) < 3:
        console.print(
            "[yellow]Usage: backtest-full SYMBOL YYYY-MM-DD YYYY-MM-DD "
            "[--step N] [--leverage N] [--size N] [--no-rag] [--rag manual|auto|all][/yellow]"
        )
        return

    symbol = args[0].upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    date_from = args[1]
    date_to = args[2]

    step_days = 7
    leverage = 10
    size_usd = 100
    use_rag = "--no-rag" not in args
    rag_source = _parse_rag_source(args)

    if "--step" in args:
        idx = args.index("--step")
        step_days = int(args[idx + 1])
    if "--leverage" in args:
        idx = args.index("--leverage")
        leverage = int(args[idx + 1])
    if "--size" in args:
        idx = args.index("--size")
        size_usd = float(args[idx + 1])

    engine = FullPipelineBacktest(leverage=leverage, size_usd=size_usd, rag_source=rag_source)
    engine.run(
        symbol=symbol,
        date_from=date_from,
        date_to=date_to,
        step_days=step_days,
        use_rag=use_rag,
    )


def print_intraday_signal(signal: dict):
    direction = signal.get("direction", "N/A")
    color = "green" if direction == "long" else "red" if direction == "short" else "yellow"

    sess = (signal.get("session_analysis") or {}).get("current_session", "?")
    flow = (signal.get("flow_analysis") or {}).get("flow_verdict", "?")
    h4 = (signal.get("session_analysis") or {}).get("h4_trend", "?")

    context_line = f"Session: [bold]{sess}[/bold]  |  H4: {h4}  |  Flow: {flow}\n"

    if signal.get("has_setup"):
        entry_sect = (
            f"Entry 1:   [bold]{signal.get('entry1')}[/bold]\n"
            f"Entry 2:   [bold]{signal.get('entry2', '—')}[/bold]\n"
            f"Stop Loss: [bold red]{signal.get('sl')}[/bold red]\n"
            f"TP1:       [bold green]{signal.get('tp1')}[/bold green]\n"
            f"TP2:       [bold green]{signal.get('tp2', '—')}[/bold green]\n"
            f"RR:        [bold]{signal.get('risk_reward', '—')}[/bold]\n\n"
        )
        watch_sect = ""
    else:
        entry_sect = "[yellow]No intraday setup found.[/yellow]\n\n"
        watch_level = signal.get("watch_level")
        watch_cond = signal.get("watch_condition")
        if watch_level:
            watch_sect = f"[yellow]👁 Watch: [bold]{watch_level}[/bold][/yellow]\n"
            if watch_cond:
                watch_sect += f"[dim]{watch_cond}[/dim]\n"
            watch_sect += "\n"
        else:
            watch_sect = ""

    console.print(Panel(
        f"[bold {color}]{signal.get('symbol')} — {direction.upper() if direction else 'NO SETUP'}[/bold {color}]\n"
        f"Confidence: [bold]{signal.get('confidence', '—')}[/bold]  |  " + context_line + "\n"
        + entry_sect
        + watch_sect
        + f"[italic]{signal.get('reasoning', '')}[/italic]\n\n"
        + (f"[dim]Risks: {signal.get('risks', '')}[/dim]" if signal.get("risks") else ""),
        title="[bold]Intraday Signal[/bold]",
        border_style=color,
    ))


def print_intraday_backtest_results(result: dict):
    m = result.get("metrics", {})

    by_sess = m.get("by_session", {})
    sess_lines = "\n".join(
        f"  {s}: W={v['wins']} L={v['losses']} O={v['open']}"
        for s, v in by_sess.items()
    ) if by_sess else "  —"

    by_dir = m.get("by_direction", {})
    long_d = by_dir.get("long", {})
    short_d = by_dir.get("short", {})

    console.print(Panel(
        f"Symbol: [bold]{result['symbol']}[/bold]  |  "
        f"{result['date_from']} → {result['date_to']}  |  step={result['step_hours']}h\n\n"
        f"Steps total:       [bold]{m.get('total_steps', 0)}[/bold]\n"
        f"Setups found:      [bold]{m.get('setups_found', 0)}[/bold]\n"
        f"Missed entry:      [yellow]{m.get('missed_entry', 0)}[/yellow]\n"
        f"Activated trades:  [bold]{m.get('activated_trades', 0)}[/bold]\n"
        f"Wins (TP hit):     [bold green]{m.get('wins', 0)}[/bold green]\n"
        f"Losses (SL hit):   [bold red]{m.get('losses', 0)}[/bold red]\n"
        f"Open/expired:      [yellow]{m.get('open', 0)}[/yellow]\n"
        f"Win rate:          [bold]{m.get('win_rate_pct', 0)}%[/bold]\n"
        f"Avg RR:            [bold]{m.get('avg_risk_reward', '—')}[/bold]\n"
        f"Avg hours to close:[bold]{m.get('avg_hours_to_close', '—')}[/bold]\n\n"
        f"By direction:\n"
        f"  Long:  W={long_d.get('wins',0)} L={long_d.get('losses',0)} WR={long_d.get('win_rate',0)}%\n"
        f"  Short: W={short_d.get('wins',0)} L={short_d.get('losses',0)} WR={short_d.get('win_rate',0)}%\n\n"
        f"By session:\n{sess_lines}",
        title="[bold]Intraday Backtest Results[/bold]",
        border_style="blue",
    ))

    setups = [s for s in result["signals"] if s.get("has_setup")]
    if setups:
        table = Table(title="Signals detail", box=box.SIMPLE)
        table.add_column("Datetime", style="dim")
        table.add_column("Session")
        table.add_column("Dir")
        table.add_column("Entry")
        table.add_column("SL")
        table.add_column("TP1")
        table.add_column("RR")
        table.add_column("Conf")
        table.add_column("Outcome")

        for s in setups:
            outcome = s.get("outcome", "—")
            color = "green" if "tp" in (outcome or "") else "red" if "sl" in (outcome or "") else "yellow"
            rr = s.get("risk_reward")
            rr_str = f"{rr}" if rr else "—"
            table.add_row(
                (s.get("signal_datetime") or "—")[:16],
                s.get("session") or "—",
                s.get("direction") or "—",
                str(s.get("entry1") or "—"),
                str(s.get("sl") or "—"),
                str(s.get("tp1") or "—"),
                rr_str,
                s.get("confidence") or "—",
                f"[{color}]{outcome}[/{color}]",
            )
        console.print(table)


def cmd_intraday_analyze(args: list[str]):
    """intraday-analyze SYMBOL [--tg] [--debug]"""
    if not args:
        console.print("[yellow]Usage: intraday-analyze SYMBOL [--tg] [--debug][/yellow]")
        return

    symbol = args[0].upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    send_tg = "--tg" in args
    debug = "--debug" in args

    console.print(f"\n[bold]Intraday analysis: {symbol}...[/bold]")
    agent = IntradayAgent()
    signal = agent.analyze(symbol, debug=debug)
    print_intraday_signal(signal)

    if send_tg:
        send_signal(signal)
        console.print("[dim]→ Sent to Telegram[/dim]")


def cmd_intraday_backtest(args: list[str]):
    """intraday-backtest SYMBOL DATE_FROM DATE_TO [--step N] [--debug]"""
    if len(args) < 3:
        console.print("[yellow]Usage: intraday-backtest SYMBOL YYYY-MM-DD YYYY-MM-DD [--step N][/yellow]")
        console.print("  --step N  Hours between each agent run (default: 4)")
        return

    symbol = args[0].upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    date_from = args[1]
    date_to = args[2]
    step_hours = 4

    debug = "--debug" in args

    if "--step" in args:
        step_hours = int(args[args.index("--step") + 1])

    engine = IntradayBacktestEngine(debug=debug)
    result = engine.run(
        symbol=symbol,
        date_from=date_from,
        date_to=date_to,
        step_hours=step_hours,
    )
    print_intraday_backtest_results(result)


def cmd_intraday_analyze_all(args: list[str]):
    """intraday-analyze-all [--tg] [--debug]"""
    send_tg = "--tg" in args
    debug = "--debug" in args
    agent = IntradayAgent()
    all_signals = []

    for asset in ASSETS:
        console.print(f"\n[bold]— {asset} —[/bold]")
        try:
            signal = agent.analyze(asset, debug=debug)
            print_intraday_signal(signal)
            all_signals.append(signal)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    if send_tg and all_signals:
        send_analyze_all(all_signals)
        console.print("\n[dim]→ Sent to Telegram[/dim]")


def cmd_intraday_list_examples(args: list[str]):
    """intraday-list-examples [SYMBOL]"""
    asset = args[0].upper() if args else None
    examples = get_intraday_examples(asset)

    if not examples:
        console.print("[yellow]No intraday examples found.[/yellow]")
        return

    table = Table(title=f"Intraday examples{' for ' + asset if asset else ''}", box=box.SIMPLE)
    table.add_column("ID", style="dim")
    table.add_column("Asset")
    table.add_column("Datetime")
    table.add_column("Session")
    table.add_column("Direction")
    table.add_column("Entry1")
    table.add_column("SL")
    table.add_column("TP1")
    table.add_column("Outcome")
    table.add_column("Source")

    for ex in examples:
        direction = ex["direction"]
        color = "green" if direction == "long" else "red"
        outcome = ex.get("outcome") or "—"
        table.add_row(
            str(ex["id"]),
            ex["asset"],
            (ex.get("trade_datetime") or "—")[:16],
            ex.get("session") or "—",
            f"[{color}]{direction}[/{color}]",
            str(ex["entry1"]),
            str(ex["sl"]),
            str(ex["tp1"]),
            outcome,
            ex.get("source") or "manual",
        )

    console.print(table)


def cmd_trade_balance(args: list[str]):
    """trade-balance — show MEXC futures account balance and open positions"""
    from trading.mexc_trader import MEXCTrader
    trader = MEXCTrader()

    try:
        balance = trader.get_balance()
        console.print(f"\n[bold]MEXC Futures Account[/bold]")
        console.print(f"Available balance: [bold green]{balance:.4f} USDT[/bold green]")
    except Exception as e:
        console.print(f"[red]Balance error: {e}[/red]")
        return

    try:
        positions = trader.get_open_positions()
        if positions:
            console.print(f"\nOpen positions: [bold]{len(positions)}[/bold]")
            for p in positions:
                sym = p.get("symbol", "?")
                side = "LONG" if p.get("positionType") == 1 else "SHORT"
                vol = p.get("vol", 0)
                entry = p.get("openAvgPrice", "?")
                pnl = p.get("unrealized", 0)
                color = "green" if float(pnl) >= 0 else "red"
                console.print(f"  {sym} {side}  vol={vol}  entry={entry}  PnL=[{color}]{pnl}[/{color}]")
        else:
            console.print("Open positions: [dim]none[/dim]")
    except Exception as e:
        console.print(f"[red]Positions error: {e}[/red]")

    try:
        from config.settings import ASSETS
        total_orders = 0
        for asset in ASSETS:
            orders = trader.get_open_orders(asset)
            if orders:
                console.print(f"\nOpen orders {asset}: {len(orders)}")
                for o in orders:
                    side = o.get("side", "?")
                    price = o.get("price", "?")
                    vol = o.get("vol", "?")
                    age_h = trader.get_order_age_hours(o)
                    console.print(f"  side={side}  price={price}  vol={vol}  age={age_h:.1f}h")
                total_orders += len(orders)
        if total_orders == 0:
            console.print("Open orders:    [dim]none[/dim]")
    except Exception as e:
        console.print(f"[red]Orders error: {e}[/red]")

    console.print()


def cmd_trade_start(args: list[str]):
    """trade-start [--dry]"""
    dry_run = "--dry" in args
    run_scheduler(dry_run=dry_run)


def cmd_trade_once(args: list[str]):
    """trade-once [--dry] — run one scan cycle and exit"""
    dry_run = "--dry" in args
    agent = IntradayAgent()
    run_once(agent, dry_run=dry_run)


def cmd_help():
    console.print(Panel(
        "[bold]Available commands:[/bold]\n\n"
        "  [cyan]analyze SYMBOL[/cyan] [--liq price1,price2] [--tg]\n"
        "    Analyze a single asset for trade setup\n\n"
        "  [cyan]analyze-all[/cyan] [--tg]\n"
        "    Analyze all configured assets\n\n"
        "  [cyan]add-example[/cyan]\n"
        "    Interactively add a trade example to the database\n\n"
        "  [cyan]list-examples[/cyan] [SYMBOL]\n"
        "    Show all saved examples\n\n"
        "  [cyan]update-outcome ID OUTCOME[/cyan]\n"
        "    Update outcome of an example (tp1_hit|tp2_hit|sl_hit|open)\n\n"
        "  [cyan]delete-example ID[/cyan]\n"
        "    Delete an example by ID\n\n"
        "  [cyan]backtest SYMBOL FROM TO[/cyan] [--step N] [--liq prices] [--no-rag]\n"
        "    Run backtest over a date range. --no-rag disables historical examples\n\n"
        "  [cyan]backtest-full SYMBOL FROM TO[/cyan] [--step N] [--leverage N] [--size N] [--no-rag]\n"
        "    Full pipeline backtest: TradingAgent → entry activation → ExitAgent every 8h\n"
        "    Shows PnL with ExitAgent vs baseline (hold to TP), exit recommendation accuracy\n\n"
        f"  [cyan]teach SYMBOL FROM TO --phase PHASE[/cyan] [--step N] [--lookahead N] [--min-move N]\n"
        f"    Generate RAG examples from historical data using oracle mode (agent sees future candles)\n"
        f"    Phases: {', '.join(MARKET_PHASES)}\n"
        f"    Examples saved with source=auto — use --rag auto to activate them\n\n"
        "  [cyan]bot[/cyan]\n"
        "    Start Telegram bot (listens for /analyze, /analyze_all)\n\n"
        "  [bold cyan]── INTRADAY ──[/bold cyan]\n\n"
        "  [cyan]intraday-analyze SYMBOL[/cyan] [--tg]\n"
        "    Intraday analysis: H4/H1/M15 structure, VWAP, CVD, orderbook, session context\n\n"
        "  [cyan]intraday-backtest SYMBOL FROM TO[/cyan] [--step N]\n"
        "    Backtest intraday agent. --step N = hours between agent runs (default 4)\n"
        "    Evaluates TP/SL hit on M15 candles within eval window\n\n"
        "  [cyan]intraday-list-examples[/cyan] [SYMBOL]\n"
        "    Show intraday example database\n\n"
        "  [bold cyan]── AUTO TRADING ──[/bold cyan]\n\n"
        "  [cyan]trade-start[/cyan] [--dry]\n"
        "    Start auto-trader: scans all assets every 25 min, places orders on signals\n"
        "    --dry: dry run mode — logs what would happen without placing real orders\n\n"
        "  [cyan]trade-once[/cyan] [--dry]\n"
        "    Run one scan cycle and exit\n\n"
        "  [cyan]help[/cyan]\n"
        "    Show this message",
        title="[bold]crhelper[/bold]",
        border_style="blue",
    ))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMANDS = {
    "analyze": cmd_analyze,
    "analyze-all": cmd_analyze_all,
    "add-example": cmd_add_example,
    "list-examples": cmd_list_examples,
    "update-outcome": cmd_update_outcome,
    "delete-example": cmd_delete_example,
    "backtest": cmd_backtest,
    "backtest-full": cmd_backtest_full,
    "teach": cmd_teach,
    # Intraday
    "intraday-analyze": cmd_intraday_analyze,
    "intraday-analyze-all": cmd_intraday_analyze_all,
    "intraday-backtest": cmd_intraday_backtest,
    "intraday-list-examples": cmd_intraday_list_examples,
    # Auto-trading
    "trade-balance": cmd_trade_balance,
    "trade-start": cmd_trade_start,
    "trade-once": cmd_trade_once,
    "bot": lambda _: run_bot(),
    "help": lambda _: cmd_help(),
}


def main():
    init_db()
    init_intraday_db()

    args = sys.argv[1:]
    if not args:
        cmd_help()
        return

    command = args[0]
    rest = args[1:]

    handler = COMMANDS.get(command)
    if handler:
        handler(rest)
    else:
        console.print(f"[red]Unknown command: {command}[/red]")
        cmd_help()
