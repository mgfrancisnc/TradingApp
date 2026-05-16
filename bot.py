"""
bot.py — Francis-Hayes Trading Bot
Run modes:
  python bot.py scan             — Weekly scan, produces report (no trades)
  python bot.py execute          — Review and submit approved covered calls
  python bot.py monitor          — Check positions for expiration/assignment
  python bot.py schedule         — Run continuous scheduler
  python bot.py schedule --cron  — Print cron job entries
  python bot.py status           — Show portfolio status
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
    from risk.position_check import PositionChecker
    from execution.execute import TradeExecutor
    from scanner.weekly_scanner import WeeklyScanner
    from notifications.email_notifier import EmailNotifier

    client = AlpacaClient()
    market = MarketDataAnalyzer(client)
    options = OptionsChainAnalyzer(client, philosophy)
    fundamentals = FundamentalsFetcher()
    rules = HardRules(philosophy)
    scorer = PhilosophyScorer(philosophy)
    notifier = EmailNotifier()
    exit_monitor = ExitMonitor(client, philosophy)
    position_checker = PositionChecker(client)
    executor = TradeExecutor(client, philosophy)

    scanner = WeeklyScanner(
        alpaca_client=client,
        market_analyzer=market,
        options_analyzer=options,
        hard_rules=rules,
        scorer=scorer,
        fundamentals_fetcher=fundamentals,
        position_checker=position_checker,
        philosophy=philosophy,
    )

    return (
        client, market, options, rules, scorer,
        exit_monitor, executor, scanner, notifier, position_checker,
    )


def cmd_scan(components):
    """Sunday scan — finds uncovered lots + new candidates, saves approved trades."""
    *_, executor, scanner, notifier, _pc = components
    logger.info("Starting weekly scan...")
    results = scanner.run()

    approved = [r for r in results if r.approved]
    if approved:
        trades = executor._scan_results_to_trades(approved)
        executor.save_approved_trades(trades)
        print(f"\n  {len(trades)} approved trade(s) saved.")
        print("  Review with Hayes, then run: python bot.py execute\n")

    notifier.notify_scan_complete(results)


def cmd_execute(components):
    """Review approved trades and submit to Alpaca after confirmation."""
    *_, executor, _scanner, _notifier, _pc = components
    executor.run()


def cmd_monitor(components):
    """
    Check open covered call positions for expiration events and alerts.
    Also detects assignments and logs them.
    """
    client = components[0]
    exit_monitor = components[5]
    notifier = components[8]

    if not client.is_market_open():
        logger.info("Market is closed — monitor check skipped")
        return

    # Detect and log any assignments or expirations since last check
    exit_monitor.handle_assignments()

    # Check all open positions for DTE alerts and circuit breaker
    alerts = exit_monitor.check_all()

    if not alerts:
        logger.info("All positions nominal ✓")
        return

    for alert in alerts:
        if alert.alert_type == "CIRCUIT_BREAKER":
            logger.warning(alert.message)
        elif alert.alert_type == "ITM_WARNING":
            logger.warning(alert.message)
        else:
            logger.info(alert.message)

    notifier.notify_alerts(alerts)


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
    exit_monitor = components[5]

    account = client.get_account()
    option_positions = client.get_open_option_positions()
    stock_positions = client.get_stock_positions()
    tracked = exit_monitor.get_positions()

    print(f"\n  FRANCIS-HAYES TRADING BOT — STATUS")
    print(f"  {datetime.now().strftime('%A %B %d, %Y %I:%M %p')}")
    print(f"  {'─'*45}")
    print(f"  Portfolio Value:   ${float(account.portfolio_value):,.2f}")
    print(f"  Buying Power:      ${float(account.buying_power):,.2f}")
    print(f"  Stock Positions:   {len(stock_positions)}")
    print(f"  Open Calls:        {len(option_positions)} / {20}")

    if tracked:
        print(f"\n  Tracked Covered Calls:")
        for pos in tracked:
            dte = pos.dte
            print(
                f"    {pos.symbol:<6}  Strike: ${pos.strike:.2f}  "
                f"Exp: {pos.expiry}  DTE: {dte}  "
                f"x{pos.contracts} @ ${pos.entry_premium:.2f}/share"
            )

    if stock_positions:
        print(f"\n  Stock Holdings:")
        for p in stock_positions:
            pl = float(getattr(p, "unrealized_pl", 0) or 0)
            pl_str = f"+${pl:,.0f}" if pl >= 0 else f"-${abs(pl):,.0f}"
            print(f"    {p.symbol:<6}  {int(float(p.qty))} shares  P&L: {pl_str}")

    print()


def main():
    args = sys.argv[1:]
    mode = args[0] if args else "status"

    if mode == "schedule" and "--cron" in args:
        cmd_schedule(cron_only=True)
        return

    if not os.environ.get("ALPACA_API_KEY"):
        print("\n  ALPACA_API_KEY not set.")
        print("  source .env   (or export your environment variables)")
        print("  export ALPACA_API_KEY=your_key_here")
        print("  export ALPACA_SECRET_KEY=your_secret_here")
        print("  export ALPACA_PAPER=true\n")
        sys.exit(1)

    philosophy = load_philosophy()

    commands = {
        "scan":    cmd_scan,
        "execute": cmd_execute,
        "monitor": cmd_monitor,
        "status":  cmd_status,
    }

    if mode == "schedule":
        build_components(philosophy)  # validate env first
        cmd_schedule()
        return

    if mode not in commands:
        print(f"\n  Unknown command '{mode}'.")
        print("  Usage: python bot.py [scan | execute | monitor | schedule | status]\n")
        sys.exit(1)

    components = build_components(philosophy)
    commands[mode](components)


if __name__ == "__main__":
    main()
