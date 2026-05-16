"""
execute.py
Submits approved trades from the weekly scan report.
Always requires human approval before any order is placed.

Workflow:
  1. Run: python bot.py scan        → generates approved list
  2. Review the report with Hayes
  3. Run: python bot.py execute     → shows approved trades, asks confirmation
  4. Approve each trade individually or all at once
  5. Bot submits orders to Alpaca and tracks positions

Hayes: 'If you're not familiar with the company — don't touch it.'
Human approval is mandatory. This bot never trades without review.
"""

import json
import os
import logging
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional

from risk.exit_monitor import OpenPosition, ExitMonitor

logger = logging.getLogger(__name__)

APPROVED_TRADES_FILE = "data/approved_trades.json"


@dataclass
class ApprovedTrade:
    symbol: str
    score: float
    stock_type: str
    recommended_strike: float
    recommended_expiry: str
    strike_type: str
    otm_pct: float
    contracts: int
    estimated_cost: float
    current_stock_price: float
    brief: str
    approved_at: str = ""


class TradeExecutor:
    """
    Handles the execute command — presents approved trades for
    final human confirmation then submits to Alpaca.
    """

    def __init__(self, alpaca_client, philosophy: dict):
        self.client = alpaca_client
        self.phil = philosophy
        self.exit_monitor = ExitMonitor(alpaca_client)

    def run(self, scan_results: list = None):
        """
        Main execute flow.
        If scan_results passed in, uses those.
        Otherwise loads last saved approved trades from disk.
        """
        if not self.client.is_market_open():
            print("\n⚠️  Market is currently closed.")
            print("Orders can only be submitted during market hours (9:30am–4pm ET).")
            print("You can still review and pre-approve trades — they'll execute at open.\n")

        # Load trades to review
        if scan_results:
            approved = [r for r in scan_results if r.approved]
            trades = self._scan_results_to_trades(approved)
        else:
            trades = self._load_saved_trades()

        if not trades:
            print("\n  No approved trades found.")
            print("  Run 'python bot.py scan' first to generate candidates.\n")
            return

        # Check position limit
        open_positions = self.client.get_open_option_positions()
        remaining_slots = self.phil["risk"]["max_positions"] - len(open_positions)

        if remaining_slots <= 0:
            print(f"\n  ⛔ Portfolio is at the 20 position limit. No new trades possible.\n")
            return

        if len(trades) > remaining_slots:
            print(f"\n  ⚠️  {len(trades)} trades approved but only {remaining_slots} slots available.")
            print(f"  Showing top {remaining_slots} by score.\n")
            trades = trades[:remaining_slots]

        # Present trades for review
        self._print_trade_review(trades, open_positions)

        # Get confirmation
        confirmed = self._get_confirmation(trades)

        if not confirmed:
            print("\n  No trades submitted. Run again when ready.\n")
            return

        # Execute
        self._execute_trades(confirmed)

    # ─────────────────────────────────────────────
    # TRADE REVIEW DISPLAY
    # ─────────────────────────────────────────────

    def _print_trade_review(self, trades: list[ApprovedTrade], open_positions: list):
        portfolio_value = self.client.get_portfolio_value()
        total_cost = sum(t.estimated_cost for t in trades if t.estimated_cost)

        print("\n" + "=" * 65)
        print("  FRANCIS-HAYES TRADING BOT — TRADE EXECUTION REVIEW")
        print("  " + datetime.now().strftime("%A %B %d, %Y %I:%M %p"))
        print("=" * 65)
        print(f"  Portfolio Value:   ${portfolio_value:,.2f}")
        print(f"  Open Positions:    {len(open_positions)} / 20")
        print(f"  Trades to Review:  {len(trades)}")
        print(f"  Total Est. Cost:   ${total_cost:,.2f}  ({total_cost/portfolio_value:.1%} of portfolio)")
        print("=" * 65)

        for i, trade in enumerate(trades, 1):
            stop_price = trade.current_stock_price * 0.90
            print(f"\n  [{i}] {trade.symbol}  —  Score: {trade.score:.0%}  [{trade.stock_type.upper()}]")
            print(f"      {trade.brief}")
            print(f"      Strike:   {trade.strike_type} ${trade.recommended_strike:.2f} ({trade.otm_pct:.1f}% OTM)")
            print(f"      Expiry:   {trade.recommended_expiry}  |  Contracts: {trade.contracts}")
            print(f"      Est Cost: ${trade.estimated_cost:,.2f}  ({trade.estimated_cost/portfolio_value:.1%} of portfolio)")
            print(f"      Stop Loss: Stock drops to ${stop_price:.2f} (10% below ${trade.current_stock_price:.2f})")

        print("\n" + "=" * 65)

    # ─────────────────────────────────────────────
    # CONFIRMATION
    # ─────────────────────────────────────────────

    def _get_confirmation(self, trades: list[ApprovedTrade]) -> list[ApprovedTrade]:
        """
        Interactive confirmation. Approve all, individual, or none.
        Returns list of confirmed trades.
        """
        print("\n  Options:")
        print("  [A] Approve ALL trades")
        print("  [1-9] Approve individual trade by number")
        print("  [N] Cancel — submit nothing")
        print()

        choice = input("  Your choice: ").strip().upper()

        if choice == "N" or choice == "":
            return []

        if choice == "A":
            confirm = input(f"\n  Confirm ALL {len(trades)} trades? (yes/no): ").strip().lower()
            return trades if confirm == "yes" else []

        # Individual selection
        confirmed = []
        try:
            indices = [int(x.strip()) - 1 for x in choice.split(",")]
            for i in indices:
                if 0 <= i < len(trades):
                    confirm = input(
                        f"\n  Confirm {trades[i].symbol} "
                        f"${trades[i].estimated_cost:,.2f}? (yes/no): "
                    ).strip().lower()
                    if confirm == "yes":
                        confirmed.append(trades[i])
        except ValueError:
            print("  Invalid input — no trades submitted")
            return []

        return confirmed

    # ─────────────────────────────────────────────
    # EXECUTION
    # ─────────────────────────────────────────────

    def _execute_trades(self, trades: list[ApprovedTrade]):
        """Submits orders to Alpaca and registers stop loss tracking."""
        print()
        succeeded = []
        failed = []

        for trade in trades:
            try:
                print(f"  Submitting {trade.symbol}...", end=" ")

                # Get current mid price for limit order
                chain = self.client.get_options_chain(
                    trade.symbol, dte_min=25, dte_max=38
                )
                limit_price = self._get_mid_price(chain, trade.recommended_strike)

                if not limit_price:
                    print("❌ Could not get limit price — skipped")
                    failed.append(trade.symbol)
                    continue

                # Submit the order
                order = self.client.buy_call(
                    symbol=trade.symbol,
                    expiry=trade.recommended_expiry,
                    strike=trade.recommended_strike,
                    contracts=trade.contracts,
                    order_type="limit",
                    limit_price=round(limit_price, 2),
                )

                # Register with exit monitor for stop loss tracking
                stop_price = round(trade.current_stock_price * 0.90, 2)
                position = OpenPosition(
                    symbol=trade.symbol,
                    option_symbol=order.symbol,
                    entry_stock_price=trade.current_stock_price,
                    stop_loss_price=stop_price,
                    entry_date=datetime.now().strftime("%Y-%m-%d"),
                    expiry=trade.recommended_expiry,
                    strike=trade.recommended_strike,
                    contracts=trade.contracts,
                    entry_premium=limit_price,
                )
                self.exit_monitor.add_position(position)

                print(f"✅ Order submitted @ ${limit_price:.2f} limit")
                succeeded.append(trade.symbol)

            except Exception as e:
                print(f"❌ Failed: {e}")
                failed.append(trade.symbol)

        # Summary
        print("\n" + "=" * 65)
        if succeeded:
            print(f"  ✅ Submitted: {', '.join(succeeded)}")
        if failed:
            print(f"  ❌ Failed:    {', '.join(failed)}")
        print(f"\n  Stop losses registered at 10% below entry stock price.")
        print(f"  Run 'python bot.py monitor' during market hours to watch positions.")
        print("=" * 65 + "\n")

    # ─────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────

    def _get_mid_price(self, chain, strike: float) -> Optional[float]:
        """Gets mid of bid/ask for limit order pricing."""
        try:
            match = next(
                (c for c in chain
                 if c.type == "call" and abs(float(c.strike_price) - strike) < 0.01),
                None
            )
            if match and match.bid_price and match.ask_price:
                return (float(match.bid_price) + float(match.ask_price)) / 2
            return None
        except Exception:
            return None

    def _scan_results_to_trades(self, results) -> list[ApprovedTrade]:
        """Converts scan results to ApprovedTrade objects."""
        trades = []
        for r in results:
            if not r.recommended_strike or not r.estimated_cost:
                continue
            trades.append(ApprovedTrade(
                symbol=r.symbol,
                score=r.score,
                stock_type=r.stock_type,
                recommended_strike=r.recommended_strike,
                recommended_expiry=r.recommended_expiry,
                strike_type=r.strike_type or "OTM",
                otm_pct=r.otm_pct or 0.0,
                contracts=r.contracts or 1,
                estimated_cost=r.estimated_cost,
                current_stock_price=0.0,    # Fetched fresh at execution
                brief=r.company_brief,
                approved_at=datetime.now().isoformat(),
            ))
        # Sort by score
        trades.sort(key=lambda t: -t.score)
        return trades

    def _load_saved_trades(self) -> list[ApprovedTrade]:
        """Loads previously saved approved trades from disk."""
        if not os.path.exists(APPROVED_TRADES_FILE):
            return []
        try:
            with open(APPROVED_TRADES_FILE) as f:
                data = json.load(f)
            return [ApprovedTrade(**t) for t in data]
        except Exception as e:
            logger.error(f"Could not load saved trades: {e}")
            return []

    def save_approved_trades(self, trades: list[ApprovedTrade]):
        """Saves approved trades to disk for execution later."""
        os.makedirs(os.path.dirname(APPROVED_TRADES_FILE), exist_ok=True)
        with open(APPROVED_TRADES_FILE, "w") as f:
            json.dump([asdict(t) for t in trades], f, indent=2)
        logger.info(f"Saved {len(trades)} approved trades to {APPROVED_TRADES_FILE}")
