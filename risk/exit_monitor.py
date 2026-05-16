"""
exit_monitor.py
Position tracking and expiration monitoring for the Francis-Hayes Trading Bot.

Assignment-targeting strategy — what we DO and DON'T do:
  DO:  Track DTE on open covered calls
  DO:  Alert when approaching expiration (≤7 DTE)
  DO:  Alert when ITM near expiry (≤3 DTE and stock above strike)
  DO:  Log assignment events (shares called away, cash returned to pool)
  DO:  Enforce circuit breaker (halt if portfolio drops ≥3% today)
  DON'T: Close positions early
  DON'T: Roll positions
  DON'T: Watch stop losses — assignment is the goal, not a failure

Alert types:
  EXPIRING_SOON      — ≤7 DTE, heads-up
  ITM_WARNING        — stock above strike with ≤3 DTE, assignment likely
  ASSIGNED           — confirmed assignment detected
  EXPIRED_WORTHLESS  — expired below strike, keep premium and shares, write next cycle
  CIRCUIT_BREAKER    — portfolio dropped ≥3% today, halt new trades
"""

import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger(__name__)

POSITIONS_FILE = "data/open_positions.json"
EXPIRING_SOON_DTE = 7       # Alert when ≤7 days to expiry
ITM_WARNING_DTE = 3         # ITM alert triggers only within 3 days of expiry


@dataclass
class OpenPosition:
    symbol: str
    option_symbol: str      # Full OCC option symbol
    strike: float
    expiry: str             # "YYYY-MM-DD"
    contracts: int
    entry_premium: float    # Per-share premium collected at entry
    entry_date: str         # "YYYY-MM-DD"
    entry_stock_price: float

    @property
    def dte(self) -> int:
        """Days to expiration from today."""
        expiry_date = datetime.strptime(self.expiry, "%Y-%m-%d").date()
        return max(0, (expiry_date - date.today()).days)

    @property
    def total_premium_collected(self) -> float:
        """Total premium collected for this position (per-share × 100 × contracts)."""
        return self.entry_premium * 100 * self.contracts


@dataclass
class MonitorAlert:
    symbol: str
    option_symbol: str
    alert_type: str         # EXPIRING_SOON | ITM_WARNING | ASSIGNED | EXPIRED_WORTHLESS | CIRCUIT_BREAKER
    message: str
    dte: Optional[int]
    current_stock_price: Optional[float]
    strike: Optional[float]


class ExitMonitor:
    """
    Monitors open covered call positions for expiration and assignment events.
    Positions are persisted to disk so they survive restarts.
    """

    def __init__(self, alpaca_client, philosophy: dict):
        self.client = alpaca_client
        self.phil = philosophy
        self._positions: dict[str, OpenPosition] = {}
        self._load_positions()

    # ─────────────────────────────────────────────
    # POSITION MANAGEMENT
    # ─────────────────────────────────────────────

    def add_position(self, position: OpenPosition):
        """Register a newly written covered call for monitoring."""
        self._positions[position.symbol] = position
        self._save_positions()
        logger.info(
            f"Covered call registered: {position.symbol} "
            f"${position.strike} exp {position.expiry} "
            f"x{position.contracts} @ ${position.entry_premium:.2f}/share "
            f"(${position.total_premium_collected:.2f} collected)"
        )

    def remove_position(self, symbol: str):
        """Remove a closed or assigned position from tracking."""
        if symbol in self._positions:
            del self._positions[symbol]
            self._save_positions()
            logger.info(f"Position removed from tracking: {symbol}")

    def get_positions(self) -> list[OpenPosition]:
        return list(self._positions.values())

    # ─────────────────────────────────────────────
    # MONITORING
    # ─────────────────────────────────────────────

    def check_all(self) -> list[MonitorAlert]:
        """
        Runs all position checks and the circuit breaker.
        Called every 30 minutes during market hours by the scheduler.
        Returns list of alerts to display or send as notifications.
        """
        alerts = []

        # Check circuit breaker first
        cb_alert = self._check_circuit_breaker()
        if cb_alert:
            alerts.append(cb_alert)

        if not self._positions:
            return alerts

        # Check each position
        for symbol, pos in list(self._positions.items()):
            position_alerts = self._check_position(pos)
            alerts.extend(position_alerts)

        return alerts

    def handle_assignments(self) -> list[str]:
        """
        Detects and logs positions that have been assigned (shares called away).
        Returns list of symbols that were assigned.
        Removes them from tracking and logs cash return to pool.
        """
        assigned = []
        try:
            open_option_symbols = {
                getattr(p, "symbol", "") for p in self.client.get_open_option_positions()
            }
        except Exception as e:
            logger.error(f"Could not fetch open option positions: {e}")
            return []

        for symbol, pos in list(self._positions.items()):
            if pos.option_symbol not in open_option_symbols:
                # Position is gone — either assigned or expired
                current_price = self._get_stock_price(symbol)
                if current_price and current_price >= pos.strike:
                    action = "ASSIGNED"
                    msg = (
                        f"{symbol}: shares assigned at ${pos.strike:.2f}. "
                        f"Premium collected: ${pos.total_premium_collected:.2f}. "
                        f"Cash returned to pool."
                    )
                else:
                    action = "EXPIRED_WORTHLESS"
                    msg = (
                        f"{symbol}: call expired worthless. "
                        f"Premium collected: ${pos.total_premium_collected:.2f}. "
                        f"Shares retained — eligible to write next cycle."
                    )
                logger.info(f"{action}: {msg}")
                self.remove_position(symbol)
                assigned.append(symbol)

        return assigned

    # ─────────────────────────────────────────────
    # INTERNAL CHECKS
    # ─────────────────────────────────────────────

    def _check_position(self, pos: OpenPosition) -> list[MonitorAlert]:
        """Returns any alerts for a single position based on DTE and ITM status."""
        alerts = []
        dte = pos.dte

        if dte <= 0:
            return alerts  # handle_assignments() deals with expired positions

        current_price = self._get_stock_price(pos.symbol)

        # Expiring soon alert
        if dte <= EXPIRING_SOON_DTE:
            itm = current_price is not None and current_price >= pos.strike
            if dte <= ITM_WARNING_DTE and itm:
                alerts.append(MonitorAlert(
                    symbol=pos.symbol,
                    option_symbol=pos.option_symbol,
                    alert_type="ITM_WARNING",
                    message=(
                        f"{pos.symbol}: {dte} DTE, stock ${current_price:.2f} "
                        f"above strike ${pos.strike:.2f} — assignment likely at expiry"
                    ),
                    dte=dte,
                    current_stock_price=current_price,
                    strike=pos.strike,
                ))
            else:
                alerts.append(MonitorAlert(
                    symbol=pos.symbol,
                    option_symbol=pos.option_symbol,
                    alert_type="EXPIRING_SOON",
                    message=(
                        f"{pos.symbol}: {dte} DTE remaining "
                        f"(strike ${pos.strike:.2f}, exp {pos.expiry})"
                        + (f" — OTM at ${current_price:.2f}" if current_price else "")
                    ),
                    dte=dte,
                    current_stock_price=current_price,
                    strike=pos.strike,
                ))

        return alerts

    def _check_circuit_breaker(self) -> Optional[MonitorAlert]:
        """
        Checks if portfolio has dropped ≥3% today.
        If so, returns an alert — the scheduler uses this to halt new trades.
        """
        try:
            max_loss_pct = self.phil.get("risk", {}).get("max_daily_loss_pct", 3.0)
            portfolio = self.client.get_portfolio_history_today()
            if not portfolio:
                return None

            open_value = portfolio.get("open_value")
            current_value = portfolio.get("current_value")
            if not open_value or not current_value or open_value == 0:
                return None

            drop_pct = (open_value - current_value) / open_value * 100
            if drop_pct >= max_loss_pct:
                return MonitorAlert(
                    symbol="PORTFOLIO",
                    option_symbol="",
                    alert_type="CIRCUIT_BREAKER",
                    message=(
                        f"CIRCUIT BREAKER: portfolio down {drop_pct:.1f}% today "
                        f"(threshold {max_loss_pct}%). Halt all new positions."
                    ),
                    dte=None,
                    current_stock_price=current_value,
                    strike=None,
                )
        except Exception as e:
            logger.debug(f"Circuit breaker check failed: {e}")

        return None

    def _get_stock_price(self, symbol: str) -> Optional[float]:
        """Fetches current stock price via Alpaca."""
        try:
            return self.client.get_latest_stock_price(symbol)
        except Exception as e:
            logger.warning(f"{symbol}: could not fetch stock price — {e}")
            return None

    # ─────────────────────────────────────────────
    # PERSISTENCE
    # ─────────────────────────────────────────────

    def _save_positions(self):
        try:
            os.makedirs(os.path.dirname(POSITIONS_FILE), exist_ok=True)
            data = {sym: asdict(pos) for sym, pos in self._positions.items()}
            with open(POSITIONS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save positions: {e}")

    def _load_positions(self):
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
                logger.info(f"Loaded {len(self._positions)} open position(s) from disk")
        except Exception as e:
            logger.warning(f"Could not load saved positions: {e}")
            self._positions = {}
