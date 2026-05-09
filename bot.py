"""
bot.py — Main entry point for the Hayes Trading Bot
Run modes:
  python bot.py scan      — Weekly scan, produces report (no trades)
  python bot.py monitor   — Check open positions for stop losses
  python bot.py execute   — Execute pre-approved trades from scan
  python bot.py status    — Show portfolio status
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
    """Wires all modules together."""
    from execution.alpaca_client import AlpacaClient
    from data.market_data import MarketDataAnalyzer
    from data.options_chain import OptionsChainAnalyzer
    from data.fundamentals import FundamentalsFetcher
    from philosophy.scorer import HardRules, PhilosophyScorer
    from risk.exit_monitor import ExitMonitor
    from scanner.weekly_scanner import WeeklyScanner

    client = AlpacaClient()
    market = MarketDataAnalyzer(client)
    options = OptionsChainAnalyzer(client, philosophy)
    fundamentals = FundamentalsFetcher()
    rules = HardRules(philosophy)
    scorer = PhilosophyScorer(philosophy)
    exit_monitor = ExitMonitor(client)

    scanner = WeeklyScanner(
        alpaca_client=client,
        market_analyzer=market,
        options_analyzer=options,
        hard_rules=rules,
        scorer=scorer,
        fundamentals_fetcher=fundamentals,
        philosophy=philosophy,
    )

    return client, market, options, rules, scorer, exit_monitor, scanner


def cmd_scan(components):
    """Sunday/Monday scan — produces report, no trades executed."""
    *_, scanner = components
    logger.info("Starting weekly scan...")
    scanner.run()


def cmd_monitor(components):
    """Check open positions for 10% stop loss triggers."""
    client, *_, exit_monitor, _ = components
    logger.info("Checking open positions for stop losses...")

    if not client.is_market_open():
        logger.info("Market is closed — stop loss check skipped")
        return

    signals = exit_monitor.check_all()

    if not signals:
        logger.info("All positions within tolerance ✓")
        return

    for signal in signals:
        if signal.action == "CLOSE":
            logger.warning(f"🚨 {signal.reason}")
        else:
            logger.info(f"⚠️  {signal.reason}")

    # Execute stop losses
    closed = exit_monitor.execute_exits(signals)
    if closed:
        logger.info(f"Closed positions: {', '.join(closed)}")


def cmd_status(components):
    """Print current portfolio status."""
    client = components[0]
    account = client.get_account()
    positions = client.get_open_option_positions()

    print(f"\nPortfolio Value:  ${float(account.portfolio_value):,.2f}")
    print(f"Buying Power:     ${float(account.buying_power):,.2f}")
    print(f"Open Options:     {len(positions)} / 20")

    if positions:
        print("\nOpen Positions:")
        for p in positions:
            print(f"  {p.symbol:30s}  {p.qty} contracts  P&L: ${float(p.unrealized_pl):+,.2f}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"

    # Validate environment
    if not os.environ.get("ALPACA_API_KEY"):
        print("\n❌ ALPACA_API_KEY not set.")
        print("Export your Alpaca paper trading keys:")
        print("  export ALPACA_API_KEY=your_key_here")
        print("  export ALPACA_SECRET_KEY=your_secret_here")
        print("  export ALPACA_PAPER=true   # Always start in paper mode!\n")
        sys.exit(1)

    philosophy = load_philosophy()
    components = build_components(philosophy)

    commands = {
        "scan": cmd_scan,
        "monitor": cmd_monitor,
        "status": cmd_status,
    }

    if mode not in commands:
        print(f"Unknown mode '{mode}'. Use: scan | monitor | status")
        sys.exit(1)

    commands[mode](components)


if __name__ == "__main__":
    main()
