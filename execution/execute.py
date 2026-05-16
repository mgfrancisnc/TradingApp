"""
execute.py
Submits approved covered call trades from the weekly scan report.
Always requires human confirmation before any order is placed.

Workflow:
  1. python bot.py scan      → finds uncovered lots + new candidates, saves approved list
  2. Review the report with Hayes
  3. python bot.py execute   → shows approved trades, asks for confirmation
  4. Confirm each trade individually or all at once
  5. Bot submits sell-to-open orders to Alpaca and registers positions for monitoring

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
from risk.position_check import PositionChecker

logger = logging.getLogger(__name__)

APPROVED_TRADES_FILE = "data/approved_trades.json"


@dataclass
class ApprovedTrade:
    symbol: str
    score: float
    stock_type: str
    recommended_strike: float
    recommended_expiry: str
    delta: float                    # Delta of selected strike (~0.50)
    contracts_to_write: int         # shares_owned // 100
    premium_per_share: float        # Premium collected per share
    premium_total: float            # premium_per_share × 100 × contracts
    current_stock_price: float
    iv_rank: Optional[float]
    shares_owned: int
    scan_type: str                  # "uncovered_lot" or "new_candidate"
    brief: str
    approved_at: str = ""


class TradeExecutor:
    """
    Handles the execute command — presents approved covered call trades for
    human confirmation then submits sell-to-open orders to Alpaca.
    """

    def __init__(self, alpaca_client, philosophy: dict):
        self.client = alpaca_client
        self.phil = philosophy
        self.exit_monitor = ExitMonitor(alpaca_client, philosophy)
        self.position_checker = PositionChecker(alpaca_client)

    def run(self, scan_results: list = None):
        """
        Main execute flow (terminal). Loads approved trades, presents them
        for confirmation via stdin, then submits to Alpaca.
        """
        trades, open_positions, notices = self._prepare_trades(scan_results)
        for n in notices:
            print(f"\n  {n}")

        if not trades:
            return

        self._print_trade_review(trades, open_positions)
        confirmed = self._get_confirmation(trades)

        if not confirmed:
            print("\n  No trades submitted. Run again when ready.\n")
            return

        self._execute_trades(confirmed)

    # ─────────────────────────────────────────────
    # SHARED PREP (terminal + web)
    # ─────────────────────────────────────────────

    def _prepare_trades(self, scan_results: list = None):
        """
        Load approved trades and apply the position-limit cap.
        Returns (trades, open_positions, notices) — notices are
        human-readable strings explaining market/limit state.
        """
        notices: list[str] = []
        if not self.client.is_market_open():
            notices.append(
                "Market is currently closed. Orders can only be submitted during "
                "market hours (9:30am-4pm ET). You can review now and submit later."
            )

        if scan_results:
            approved = [r for r in scan_results if r.approved]
            trades = self._scan_results_to_trades(approved)
        else:
            trades = self._load_saved_trades()

        if not trades:
            notices.append(
                "No approved trades found. Run a scan first to generate candidates."
            )
            return [], [], notices

        open_positions = self.client.get_open_option_positions()
        max_positions = self.phil["risk"]["max_positions"]
        remaining_slots = max_positions - len(open_positions)

        if remaining_slots <= 0:
            notices.append(
                f"Portfolio is at the {max_positions}-position limit. "
                f"No new trades possible."
            )
            return [], open_positions, notices

        if len(trades) > remaining_slots:
            notices.append(
                f"{len(trades)} trades approved but only {remaining_slots} slot(s) "
                f"available. Showing top {remaining_slots} by score."
            )
            trades = trades[:remaining_slots]

        return trades, open_positions, notices

    # ─────────────────────────────────────────────
    # WEB API (non-interactive — approval happens in the browser)
    # ─────────────────────────────────────────────

    def get_pending_trades(self) -> dict:
        """Approved trades ready for browser review, plus context."""
        trades, open_positions, notices = self._prepare_trades()
        return {
            "trades": [asdict(t) for t in trades],
            "notices": notices,
            "open_positions": len(open_positions),
            "max_positions": self.phil["risk"]["max_positions"],
            "portfolio_value": (
                self.client.get_portfolio_value() if trades else 0.0
            ),
            "total_premium": sum(t.premium_total for t in trades),
        }

    def execute_selected(self, symbols: list[str]) -> dict:
        """
        Submit sell-to-open orders for the chosen symbols. This is the
        human-in-the-loop confirmation point — the caller (web UI) has
        already collected explicit approval.
        """
        wanted = set(symbols)
        chosen = [t for t in self._load_saved_trades() if t.symbol in wanted]
        if not chosen:
            print("\n  No matching approved trades to submit.\n")
            return {"succeeded": [], "failed": []}

        succeeded, failed = self._execute_trades(chosen)
        return {"succeeded": succeeded, "failed": failed}

    # ─────────────────────────────────────────────
    # DISPLAY
    # ─────────────────────────────────────────────

    def _print_trade_review(self, trades: list[ApprovedTrade], open_positions: list):
        portfolio_value = self.client.get_portfolio_value()
        total_income = sum(t.premium_total for t in trades)

        print("\n" + "=" * 65)
        print("  FRANCIS-HAYES TRADING BOT — COVERED CALL EXECUTION REVIEW")
        print("  " + datetime.now().strftime("%A %B %d, %Y %I:%M %p"))
        print("=" * 65)
        print(f"  Portfolio Value:      ${portfolio_value:,.2f}")
        print(f"  Open Positions:       {len(open_positions)} / {self.phil['risk']['max_positions']}")
        print(f"  Trades to Review:     {len(trades)}")
        print(f"  Total Premium Income: ${total_income:,.2f}")
        print("=" * 65)

        for i, trade in enumerate(trades, 1):
            tag = "[UNCOVERED LOT]" if trade.scan_type == "uncovered_lot" else "[BUY-WRITE]"
            print(f"\n  [{i}] {trade.symbol}  {tag}  Score: {trade.score:.0%}  [{trade.stock_type.upper()}]")
            print(f"      {trade.brief}")
            print(f"      Shares owned:  {trade.shares_owned}")
            print(f"      Strike:        ${trade.recommended_strike:.2f}  (delta {trade.delta:.2f})")
            print(f"      Expiry:        {trade.recommended_expiry}  |  Contracts: {trade.contracts_to_write}")
            print(f"      IV Rank:       {trade.iv_rank:.0f}" if trade.iv_rank else "      IV Rank:       N/A")
            print(f"      Premium:       ${trade.premium_per_share:.2f}/share  =  ${trade.premium_total:,.2f} total income")

        print("\n" + "=" * 65)
        print("  Strategy: hold to expiration. Assignment = success.")
        print("  No stop losses. No early closes. No rolls.")
        print("=" * 65)

    # ─────────────────────────────────────────────
    # CONFIRMATION
    # ─────────────────────────────────────────────

    def _get_confirmation(self, trades: list[ApprovedTrade]) -> list[ApprovedTrade]:
        print("\n  Options:")
        print("  [A] Approve ALL trades")
        print("  [1,2,...] Approve individual trades by number (comma-separated)")
        print("  [N] Cancel — submit nothing")
        print()

        choice = input("  Your choice: ").strip().upper()

        if choice in ("N", ""):
            return []

        if choice == "A":
            confirm = input(f"\n  Confirm ALL {len(trades)} trades? (yes/no): ").strip().lower()
            return trades if confirm == "yes" else []

        confirmed = []
        try:
            indices = [int(x.strip()) - 1 for x in choice.split(",")]
            for i in indices:
                if 0 <= i < len(trades):
                    confirm = input(
                        f"\n  Confirm {trades[i].symbol} — "
                        f"sell {trades[i].contracts_to_write} call(s) @ "
                        f"${trades[i].premium_per_share:.2f}/share "
                        f"(${trades[i].premium_total:,.2f} income)? (yes/no): "
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

    def _execute_trades(self, trades: list[ApprovedTrade]) -> tuple[list, list]:
        print()
        succeeded = []
        failed = []

        for trade in trades:
            try:
                print(f"  Submitting {trade.symbol}...", end=" ")

                # Verify shares still owned before submitting
                status = self.position_checker.check(trade.symbol)
                if not status.eligible_to_write:
                    reason = "already has open call" if status.has_open_call else f"only {status.shares_owned} shares owned"
                    print(f"  Skipping — {reason}")
                    failed.append(trade.symbol)
                    continue

                # Get fresh mid price for limit order
                chain = self.client.get_options_chain(trade.symbol, dte_min=25, dte_max=38)
                limit_price = self._get_mid_price(chain, trade.recommended_strike)

                if not limit_price:
                    print("  Could not get limit price — skipped")
                    failed.append(trade.symbol)
                    continue

                # Submit sell-to-open order
                order = self.client.sell_call(
                    symbol=trade.symbol,
                    expiry=trade.recommended_expiry,
                    strike=trade.recommended_strike,
                    contracts=trade.contracts_to_write,
                    order_type="limit",
                    limit_price=round(limit_price, 2),
                )

                # Register with exit monitor for DTE tracking
                position = OpenPosition(
                    symbol=trade.symbol,
                    option_symbol=order.symbol,
                    strike=trade.recommended_strike,
                    expiry=trade.recommended_expiry,
                    contracts=trade.contracts_to_write,
                    entry_premium=limit_price,
                    entry_date=datetime.now().strftime("%Y-%m-%d"),
                    entry_stock_price=trade.current_stock_price,
                )
                self.exit_monitor.add_position(position)

                print(f"  Order submitted — sell {trade.contracts_to_write} call(s) @ ${limit_price:.2f} limit")
                succeeded.append(trade.symbol)

            except Exception as e:
                print(f"  Failed: {e}")
                logger.error(f"{trade.symbol}: execution failed — {e}")
                failed.append(trade.symbol)

        print("\n" + "=" * 65)
        if succeeded:
            print(f"  Submitted: {', '.join(succeeded)}")
        if failed:
            print(f"  Failed:    {', '.join(failed)}")
        print(f"\n  Positions registered for DTE monitoring.")
        print(f"  Run 'python bot.py monitor' during market hours to track positions.")
        print("=" * 65 + "\n")

        return succeeded, failed

    # ─────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────

    def _get_mid_price(self, chain, strike: float) -> Optional[float]:
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
        trades = []
        for r in results:
            if not r.recommended_strike or not r.premium_per_share:
                continue
            trades.append(ApprovedTrade(
                symbol=r.symbol,
                score=r.score,
                stock_type=r.stock_type,
                recommended_strike=r.recommended_strike,
                recommended_expiry=r.recommended_expiry,
                delta=r.delta or 0.50,
                contracts_to_write=r.contracts_to_write or 1,
                premium_per_share=r.premium_per_share,
                premium_total=r.premium_total or 0.0,
                current_stock_price=r.current_stock_price,
                iv_rank=r.iv_rank,
                shares_owned=r.shares_owned,
                scan_type=r.scan_type,
                brief=r.company_brief,
                approved_at=datetime.now().isoformat(),
            ))
        trades.sort(key=lambda t: (-int(t.scan_type == "uncovered_lot"), -t.score))
        return trades

    def _load_saved_trades(self) -> list[ApprovedTrade]:
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
        os.makedirs(os.path.dirname(APPROVED_TRADES_FILE), exist_ok=True)
        with open(APPROVED_TRADES_FILE, "w") as f:
            json.dump([asdict(t) for t in trades], f, indent=2)
        logger.info(f"Saved {len(trades)} approved trades to {APPROVED_TRADES_FILE}")
