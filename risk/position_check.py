"""
position_check.py
Pre-trade share ownership verification for the Francis-Hayes Trading Bot.

Hard rule: must own ≥100 shares of the underlying before writing a covered call.
Queries Alpaca for current stock positions and open short call positions to
identify which lots are eligible for covered call writing.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PositionStatus:
    symbol: str
    shares_owned: int
    lots_available: int             # shares_owned // 100
    has_open_call: bool             # True if a covered call is already written
    call_option_symbol: Optional[str]
    call_expiry: Optional[str]
    call_strike: Optional[float]
    eligible_to_write: bool         # lots_available > 0 and not has_open_call


class PositionChecker:
    """
    Queries Alpaca to verify share ownership and open covered call status.
    Called before writing a covered call and during the weekly uncovered-lot scan.
    """

    def __init__(self, alpaca_client):
        self.client = alpaca_client

    def check(self, symbol: str) -> PositionStatus:
        """Returns PositionStatus for a single symbol."""
        shares_owned = self._get_shares_owned(symbol)
        open_call = self._get_open_call(symbol)
        lots = shares_owned // 100
        has_call = open_call is not None

        return PositionStatus(
            symbol=symbol,
            shares_owned=shares_owned,
            lots_available=lots,
            has_open_call=has_call,
            call_option_symbol=open_call.get("option_symbol") if open_call else None,
            call_expiry=open_call.get("expiry") if open_call else None,
            call_strike=open_call.get("strike") if open_call else None,
            eligible_to_write=lots > 0 and not has_call,
        )

    def get_uncovered_lots(self) -> list[PositionStatus]:
        """
        Returns all stock positions with ≥100 shares and no open covered call.
        These are the primary candidates for the weekly scan — we own the stock,
        we just need to write the call.
        """
        try:
            stock_positions = self.client.get_stock_positions()
        except Exception as e:
            logger.error(f"Could not fetch stock positions: {e}")
            return []

        uncovered = []
        for pos in stock_positions:
            try:
                status = self.check(pos.symbol)
                if status.eligible_to_write:
                    uncovered.append(status)
                    logger.debug(
                        f"{pos.symbol}: {status.shares_owned} shares owned, "
                        f"{status.lots_available} lot(s) available to write"
                    )
            except Exception as e:
                logger.warning(f"{pos.symbol}: position check failed — {e}")

        logger.info(f"Found {len(uncovered)} uncovered lot(s) eligible for covered call writing")
        return uncovered

    # ─────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────

    def _get_shares_owned(self, symbol: str) -> int:
        """Returns number of shares currently held. Returns 0 if not held."""
        try:
            for p in self.client.get_stock_positions():
                if p.symbol == symbol:
                    return int(float(p.qty))
            return 0
        except Exception as e:
            logger.warning(f"{symbol}: could not verify share ownership — {e}")
            return 0

    def _get_open_call(self, symbol: str) -> Optional[dict]:
        """
        Checks for an existing open covered call on this symbol.
        Returns option details dict if found, else None.
        """
        try:
            for pos in self.client.get_open_option_positions():
                opt_sym = getattr(pos, "symbol", "") or ""
                if opt_sym.startswith(symbol) and self._is_short_call(pos):
                    return {
                        "option_symbol": opt_sym,
                        "strike": getattr(pos, "strike_price", None),
                        "expiry": getattr(pos, "expiry", None),
                    }
            return None
        except Exception as e:
            logger.warning(f"{symbol}: could not check open option positions — {e}")
            return None

    def _is_short_call(self, position) -> bool:
        """True if position is a short call (i.e. a covered call we wrote)."""
        qty = float(getattr(position, "qty", 0) or 0)
        side = (getattr(position, "side", "") or "").lower()
        option_type = (getattr(position, "option_type", "") or "").lower()
        return (qty < 0 or side == "short") and option_type == "call"
