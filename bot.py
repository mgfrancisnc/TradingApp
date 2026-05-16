"""
bot.py — Francis-Hayes Trading Bot
Run modes:
  python bot.py scan          — Weekly scan, produces report (no trades)
  python bot.py execute       — Review and submit approved trades
  python bot.py monitor       — Check open positions for stop losses
  python bot.py schedule      — Run continuous scheduler
  python bot.py schedule --cron  — Print cron job entries
  python bot.py status        — Show portfolio status
"""

import os
import sys
import yaml
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("francis_hayes_bot")


def load_philosophy() -> dict:
    with open("config/philosophy.yaml") as f:
        return yaml.safe_load(f)


def build_components(philosophy: dict):
    from execution.alpaca_client import AlpacaClient
    from data.market_data import MarketDataAnalyzer
    from data.options_chain import OptionsChainAnalyzer
    from data.fundamentals import FundamentalsFetcher
    from philosophy.scorer import HardRules, PhilosophyScorer
    from risk.exit_monitor import ExitMonitor
    from execution.execute import TradeExecutor
    from scanner.weekly_scanner import WeeklyScanner

    client = AlpacaClient()
    market = MarketDataAnalyzer(client)
    options = OptionsChainAnalyzer(client, philosophy)
    fundamentals = FundamentalsFetcher()
    rules = HardRules(philosophy)
    scorer = PhilosophyScorer(philosophy)
    exit_monitor = ExitMonitor(client)
    executor = TradeExecutor(client, philosophy)

    scanner = WeeklyScanner(
        alpaca_client=client,
        market_analyzer=market,
        options_analyzer=options,
        hard_rules=rules,
        scorer=scorer,
        fundamentals_fetcher=fundamentals,
        philosophy=philosophy,
    )

    return client, market, options, rules, scorer, exit_monitor, executor, scanner


def cmd_scan(components):
    """Sunday/Monday scan — produces report, saves approved trades, no orders placed."""
    *_, executor, scanner = components
    logger.info("Starting weekly scan...")
    results = scanner.run()

    # Save approved trades to disk for execute command
    approved = [r for r in results if r.approved]
    if approved:
        trades = executor._scan_results_to_trades(approved)
        executor.save_approved_trades(trades)
        print(f"\n  {len(trades)} approved trade(s) saved.")
        print("  Review with Hayes, then run: python bot.py execute\n")


def cmd_execute(components):
    """Review approved trades and submit to Alpaca after confirmation."""
    *_, executor, _ = components
    executor.run()


def cmd_monitor(components):
    """Check open positions for 10% stop loss triggers."""
    client = components[0]
    exit_monitor = components[5]

    if not client.is_market_open():
        logger.info("Market is closed — stop loss check skipped")
        return

    signals = exit_monitor.check_all()

    if not signals:
        logger.info("All positions within tolerance ✓")
        return

    for signal in signals:
        if signal.action == "CLOSE":
            logger.warning(f"STOP LOSS TRIGGERED: {signal.reason}")
        else:
            logger.info(f"WARNING: {signal.reason}")

    closed = exit_monitor.execute_exits(signals)
    if closed:
        logger.info(f"Closed positions: {', '.join(closed)}")


def cmd_schedule(cron_only: bool = False):
    """Run the continuous scheduler or print cron entries."""
    from execution.scheduler import Scheduler
    s = Scheduler()
    if cron_only:
        s.print_cron()
    else:
        s.run_forever()


def cmd_status(components):
    """Print current portfolio status."""
    client = components[0]
    account = client.get_account()
    positions = client.get_open_option_positions()

    print(f"\n  FRANCIS-HAYES TRADING BOT — STATUS")
    print(f"  {datetime.now().strftime('%A %B %d, %Y %I:%M %p')}")
    print(f"  {'─'*40}")
    print(f"  Portfolio Value:  ${float(account.portfolio_value):,.2f}")
    print(f"  Buying Power:     ${float(account.buying_power):,.2f}")
    print(f"  Open Options:     {len(positions)} / 20")

    if positions:
        print(f"\n  Open Positions:")
        for p in positions:
            pl = float(p.unrealized_pl)
            pl_str = f"+${pl:,.2f}" if pl >= 0 else f"-${abs(pl):,.2f}"
            print(f"    {p.symbol:<30}  {p.qty} contracts  P&L: {pl_str}")
    print()


def main():
    args = sys.argv[1:]
    mode = args[0] if args else "status"

    # Schedule mode doesn't need Alpaca connection to print cron
    if mode == "schedule":
        cron_only = "--cron" in args
        if cron_only:
            cmd_schedule(cron_only=True)
            return
        # Still need env vars for full scheduler
        pass

    # Validate environment
    if not os.environ.get("ALPACA_API_KEY"):
        print("\n❌ ALPACA_API_KEY not set.")
        print("  source .env   (or set your environment variables)")
        print("  export ALPACA_API_KEY=your_key_here")
        print("  export ALPACA_SECRET_KEY=your_secret_here")
        print("  export ALPACA_PAPER=true\n")
        sys.exit(1)

    philosophy = load_philosophy()

    commands = {
        "scan": lambda c: cmd_scan(c),
        "execute": lambda c: cmd_execute(c),
        "monitor": lambda c: cmd_monitor(c),
        "status": lambda c: cmd_status(c),
    }

    if mode == "schedule":
        components = build_components(philosophy)
        cmd_schedule()
        return

    if mode not in commands:
        print(f"\nUnknown command '{mode}'.")
        print("Usage: python bot.py [scan | execute | monitor | schedule | status]\n")
        sys.exit(1)

    components = build_components(philosophy)
    commands[mode](components)


if __name__ == "__main__":
    main()
