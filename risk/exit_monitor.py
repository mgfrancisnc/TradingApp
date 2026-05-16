"""
exit_monitor.py
Stop loss tracking and exit execution for the Francis-Hayes Trading Bot.

Hayes' exit rule:
  'If the STOCK drops 10% from where I bought it, I close the call —
   regardless of what the option is worth.'

This module:
  - Tracks open positions in a JSON file (risk/positions.json)
  - Checks the underlying stock price against each position's stop loss
  - Triggers close orders via Alpaca when stop is hit
  - Issues warnings at 7% loss (early warning) before the 10% hard stop

Stop loss watches the STOCK price, not the option premium.
This is intentional — Hayes watches the business, not the derivative.
"""

import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

POSITIONS_FILE = "risk/positions.json"
WARNING_PCT = 7.0    # Warn at -7% underlying drop
STOP_LOSS_PCT = 10.0 # Close at -10% underlying drop


@dataclass
class OpenPosition:
    symbol: str
    option_symbol: str          # Full OCC option symbol (used to close the position)
    entry_stock_price: float    # Stock price when we bought the call
    stop_loss_price: float      # = entry_stock_price * 0.90
    entry_date: str             # "YYYY-MM-DD"
    expiry: str                 # Option expiry "YYYY-MM-DD"
    strike: float
    contracts: int
    entry_premium: float        # Per-contract mid price at entry


@dataclass
class ExitSignal:
    symbol: str
    option_symbol: str
    action: str         # "CLOSE" or "WARN"
    reason: str
    current_stock_price: float
    loss_pct: float


class ExitMonitor:
    """
    Monitors open positions for stop loss triggers.
    Positions are persisted to disk so they survive restarts.
    """

    def __init__(self, alpaca_client):
        self.client = alpaca_client
        self._positions: dict[str, OpenPosition] = {}
        self._load_positions()

    # ─────────────────────────────────────────────
    # POSITION MANAGEMENT
    # ─────────────────────────────────────────────

    def add_position(self, position: OpenPosition):
        """Register a new position for stop loss monitoring."""
        self._positions[position.symbol] = position
        self._save_positions()
        logger.info(
            f"Stop loss registered: {position.symbol} "
            f"stock entry ${position.entry_stock_price:.2f} "
            f"stop ${position.stop_loss_price:.2f}"
        )

    def remove_position(self, symbol: str):
        """Remove a closed position from tracking."""
        if symbol in self._positions:
            del self._positions[symbol]
            self._save_positions()
            logger.info(f"Position removed from tracking: {symbol}")

    def get_positions(self) -> list[OpenPosition]:
        """Returns all currently tracked positions."""
        return list(self._positions.values())

    # ─────────────────────────────────────────────
    # STOP LOSS CHECKS
    # ─────────────────────────────────────────────

    def check_all(self) -> list[ExitSignal]:
        """
        Checks every tracked position against its stop loss.
        Returns list of signals (WARN or CLOSE).
        Called during market hours by the scheduler.
        """
        if not self._positions:
            return []

        signals = []
        for symbol, pos in list(self._positions.items()):
            signal = self._check_position(pos)
            if signal:
                signals.append(signal)

        return signals

    def execute_exits(self, signals: list[ExitSignal]) -> list[str]:
        """
        Executes CLOSE orders for triggered stop losses.
        Returns list of symbols that were closed.
        """
        closed = []
        for signal in signals:
            if signal.action != "CLOSE":
                continue
            try:
                logger.warning(
                    f"STOP LOSS: {signal.symbol} stock at ${signal.current_stock_price:.2f} "
                    f"({signal.loss_pct:.1f}% below entry) — closing {signal.option_symbol}"
                )
                self.client.close_position(signal.option_symbol)
                self.remove_position(signal.symbol)
                closed.append(signal.symbol)
            except Exception as e:
                logger.error(f"Failed to close {signal.symbol}: {e}")

        return closed

    # ─────────────────────────────────────────────
    # INTERNAL CHECKS
    # ─────────────────────────────────────────────

    def _check_position(self, pos: OpenPosition) -> Optional[ExitSignal]:
        """
        Gets current stock price and checks against stop loss.
        Returns ExitSignal if action is needed, else None.
        """
        try:
            current_price = self._get_current_stock_price(pos.symbol)
        except Exception as e:
            logger.warning(f"{pos.symbol}: could not fetch price — {e}")
            return None

        if current_price is None:
            return None

        loss_pct = (pos.entry_stock_price - current_price) / pos.entry_stock_price * 100

        if loss_pct >= STOP_LOSS_PCT:
            return ExitSignal(
                symbol=pos.symbol,
                option_symbol=pos.option_symbol,
                action="CLOSE",
                reason=(
                    f"{pos.symbol} stock dropped {loss_pct:.1f}% "
                    f"(${current_price:.2f} vs entry ${pos.entry_stock_price:.2f}) "
                    f"— 10% stop loss triggered"
                ),
                current_stock_price=current_price,
                loss_pct=loss_pct,
            )

        if loss_pct >= WARNING_PCT:
            return ExitSignal(
                symbol=pos.symbol,
                option_symbol=pos.option_symbol,
                action="WARN",
                reason=(
                    f"{pos.symbol} stock down {loss_pct:.1f}% "
                    f"(${current_price:.2f} vs entry ${pos.entry_stock_price:.2f}) "
                    f"— approaching 10% stop"
                ),
                current_stock_price=current_price,
                loss_pct=loss_pct,
            )

        return None

    def _get_current_stock_price(self, symbol: str) -> Optional[float]:
        """Fetches current stock price via Alpaca."""
        price = self.client.get_latest_stock_price(symbol)
        if price:
            return price

        # Fall back: check if it's in open positions
        try:
            positions = self.client.get_open_option_positions()
            for p in positions:
                if hasattr(p, "current_price") and p.symbol.startswith(symbol):
                    return float(p.current_price)
        except Exception:
            pass

        return None

    # ─────────────────────────────────────────────
    # PERSISTENCE
    # ─────────────────────────────────────────────

    def _save_positions(self):
        """Persists positions to disk so they survive restarts."""
        try:
            os.makedirs(os.path.dirname(POSITIONS_FILE), exist_ok=True)
            data = {sym: asdict(pos) for sym, pos in self._positions.items()}
            with open(POSITIONS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save positions: {e}")

    def _load_positions(self):
        """Loads previously saved positions from disk."""
        if not os.path.exists(POSITIONS_FILE):
            return
        try:
            with open(POSITIONS_FILE) as f:
                data = json.load(f)
            self._positions = {
                sym: OpenPosition(**pos_data)
                for sym, pos_data in data.items()
            }
            if self._positions:
                logger.info(f"Loaded {len(self._positions)} tracked position(s) from disk")
        except Exception as e:
            logger.warning(f"Could not load saved positions: {e}")
            self._positions = {}
