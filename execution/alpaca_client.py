"""
alpaca_client.py
Wrapper around alpaca-py for the Francis-Hayes Trading Bot.

Handles authentication, account queries, option positions,
options chain data, and order submission.

Paper trading by default — set ALPACA_PAPER=false for live.

Required env vars:
    ALPACA_API_KEY     — from alpaca.markets → Paper Trading → API Keys
    ALPACA_SECRET_KEY  — same location
    ALPACA_PAPER       — "true" (default) or "false"
"""

import os
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# THIN DATA WRAPPERS
# (so the rest of the codebase doesn't know alpaca-py internals)
# ─────────────────────────────────────────────

@dataclass
class OptionContractInfo:
    """
    Normalised view of an option contract.
    Parsed from the OCC symbol so the upstream code only sees .type,
    .strike_price, .bid_price, .ask_price, .open_interest.
    """
    symbol: str            # Full OCC option symbol
    underlying: str
    expiry: str            # "YYYY-MM-DD"
    type: str              # "call" or "put"
    strike_price: float
    bid_price: Optional[float]
    ask_price: Optional[float]
    open_interest: Optional[int]
    implied_volatility: Optional[float]

    @classmethod
    def from_snapshot(cls, snap) -> Optional["OptionContractInfo"]:
        """Build from an alpaca-py OptionSnapshot object."""
        try:
            sym = snap.symbol
            underlying, expiry, opt_type, strike = _parse_occ(sym)
            bid = None
            ask = None
            oi = None
            iv = None
            if snap.latest_quote:
                bid = float(snap.latest_quote.bid_price) if snap.latest_quote.bid_price else None
                ask = float(snap.latest_quote.ask_price) if snap.latest_quote.ask_price else None
            if hasattr(snap, "open_interest") and snap.open_interest:
                oi = int(snap.open_interest)
            if hasattr(snap, "implied_volatility") and snap.implied_volatility:
                iv = float(snap.implied_volatility)
            return cls(
                symbol=sym,
                underlying=underlying,
                expiry=expiry,
                type=opt_type,
                strike_price=strike,
                bid_price=bid,
                ask_price=ask,
                open_interest=oi,
                implied_volatility=iv,
            )
        except Exception as e:
            logger.debug(f"Could not parse option snapshot {getattr(snap, 'symbol', '?')}: {e}")
            return None


def _parse_occ(symbol: str):
    """
    Parse an OCC option symbol into (underlying, expiry, type, strike).
    Format: {underlying up to 6 chars}{YYMMDD}{C/P}{8-digit strike * 1000}
    e.g.  AAPL240119C00185000 → AAPL, 2024-01-19, call, 185.0
    """
    # Strike is always last 8 chars
    strike_str = symbol[-8:]
    type_char = symbol[-9]
    date_str = symbol[-15:-9]
    underlying = symbol[:-15].rstrip()

    opt_type = "call" if type_char == "C" else "put"
    expiry = datetime.strptime(date_str, "%y%m%d").strftime("%Y-%m-%d")
    strike = int(strike_str) / 1000.0
    return underlying, expiry, opt_type, strike


def _build_occ(underlying: str, expiry: str, opt_type: str, strike: float) -> str:
    """
    Build OCC option symbol from components.
    e.g. AAPL, 2024-01-19, call, 185.0 → AAPL240119C00185000
    """
    dt = datetime.strptime(expiry, "%Y-%m-%d")
    date_str = dt.strftime("%y%m%d")
    type_char = "C" if opt_type.lower() in ("call", "c") else "P"
    strike_int = int(round(strike * 1000))
    return f"{underlying}{date_str}{type_char}{strike_int:08d}"


# ─────────────────────────────────────────────
# MAIN CLIENT
# ─────────────────────────────────────────────

class AlpacaClient:
    """
    Thin wrapper around alpaca-py.
    Reads credentials from environment variables set in .env.
    """

    def __init__(self):
        api_key = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        paper = os.environ.get("ALPACA_PAPER", "true").lower() != "false"

        if not api_key or not secret_key:
            raise EnvironmentError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set. "
                "Copy .env.example → .env and fill in your keys."
            )

        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient

        self._trading = TradingClient(api_key, secret_key, paper=paper)
        self._stock_data = StockHistoricalDataClient(api_key, secret_key)
        self._option_data = OptionHistoricalDataClient(api_key, secret_key)
        self._paper = paper

        mode = "PAPER" if paper else "LIVE ⚠️"
        logger.info(f"AlpacaClient ready [{mode}]")

    # ─────────────────────────────────────────────
    # ACCOUNT
    # ─────────────────────────────────────────────

    def get_account(self):
        """Returns the Alpaca account object (portfolio_value, buying_power, etc.)."""
        return self._trading.get_account()

    def get_portfolio_value(self) -> float:
        """Returns total portfolio equity in USD."""
        account = self.get_account()
        return float(account.portfolio_value)

    def is_market_open(self) -> bool:
        """Returns True if the US market is currently open."""
        clock = self._trading.get_clock()
        return clock.is_open

    # ─────────────────────────────────────────────
    # POSITIONS
    # ─────────────────────────────────────────────

    def get_open_option_positions(self) -> list:
        """Returns all open options positions as Alpaca Position objects."""
        from alpaca.trading.enums import AssetClass
        positions = self._trading.get_all_positions()
        return [p for p in positions if p.asset_class == AssetClass.US_OPTION]

    def get_latest_stock_price(self, symbol: str) -> Optional[float]:
        """Returns the most recent trade price for a stock."""
        try:
            from alpaca.data.requests import StockLatestTradeRequest
            req = StockLatestTradeRequest(symbol_or_symbols=symbol)
            trades = self._stock_data.get_stock_latest_trade(req)
            if symbol in trades:
                return float(trades[symbol].price)
        except Exception as e:
            logger.warning(f"{symbol}: price fetch failed — {e}")
        return None

    # ─────────────────────────────────────────────
    # OPTIONS CHAIN
    # ─────────────────────────────────────────────

    def get_options_chain(
        self,
        symbol: str,
        dte_min: int = 25,
        dte_max: int = 38,
        option_type: str = "call",
    ) -> list[OptionContractInfo]:
        """
        Returns call (or put) options within the DTE window,
        as a list of OptionContractInfo objects.
        """
        from alpaca.data.requests import OptionChainRequest

        now = datetime.now()
        expiry_start = (now + timedelta(days=dte_min)).date()
        expiry_end = (now + timedelta(days=dte_max)).date()

        # Map friendly name → Alpaca enum value
        try:
            from alpaca.data.enums import ContractType
            ctype = ContractType.CALL if option_type.lower() == "call" else ContractType.PUT
            req = OptionChainRequest(
                symbol_or_symbols=symbol,
                expiration_date_gte=expiry_start,
                expiration_date_lte=expiry_end,
                type=ctype,
            )
        except (ImportError, AttributeError):
            # Older alpaca-py versions use string literals
            req = OptionChainRequest(
                symbol_or_symbols=symbol,
                expiration_date_gte=expiry_start,
                expiration_date_lte=expiry_end,
            )

        try:
            raw = self._option_data.get_option_chain(req)
            if not raw:
                return []
            contracts = []
            for snap in raw.values():
                info = OptionContractInfo.from_snapshot(snap)
                if info and (option_type == "all" or info.type == option_type.lower()):
                    contracts.append(info)
            return contracts
        except Exception as e:
            logger.warning(f"{symbol}: options chain fetch failed — {e}")
            return []

    def get_options_snapshot_long(
        self,
        symbol: str,
        dte_min: int = 180,
        dte_max: int = 365,
    ) -> list[OptionContractInfo]:
        """
        Returns all options 6-12 months out for put/call ratio analysis.
        Hayes: 'Look 6-12 months out, not daily — daily means nothing.'
        """
        from alpaca.data.requests import OptionChainRequest

        now = datetime.now()
        expiry_start = (now + timedelta(days=dte_min)).date()
        expiry_end = (now + timedelta(days=dte_max)).date()

        try:
            req = OptionChainRequest(
                symbol_or_symbols=symbol,
                expiration_date_gte=expiry_start,
                expiration_date_lte=expiry_end,
            )
            raw = self._option_data.get_option_chain(req)
            if not raw:
                return []
            contracts = []
            for snap in raw.values():
                info = OptionContractInfo.from_snapshot(snap)
                if info:
                    contracts.append(info)
            return contracts
        except Exception as e:
            logger.warning(f"{symbol}: long-dated options fetch failed — {e}")
            return []

    # ─────────────────────────────────────────────
    # ORDER SUBMISSION
    # ─────────────────────────────────────────────

    def buy_call(
        self,
        symbol: str,
        expiry: str,          # "YYYY-MM-DD"
        strike: float,
        contracts: int,
        order_type: str = "limit",
        limit_price: float = None,
    ):
        """
        Submits a buy-to-open limit (or market) order for a call option.
        Returns the Alpaca order object.
        """
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        option_symbol = _build_occ(symbol, expiry, "call", strike)
        logger.info(
            f"{'PAPER ' if self._paper else ''}ORDER: BUY {contracts}x {option_symbol} "
            f"@ {'$' + str(round(limit_price, 2)) if limit_price else 'MKT'}"
        )

        if order_type == "limit" and limit_price:
            req = LimitOrderRequest(
                symbol=option_symbol,
                qty=contracts,
                side=OrderSide.BUY,
                type="limit",
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
            )
        else:
            req = MarketOrderRequest(
                symbol=option_symbol,
                qty=contracts,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )

        order = self._trading.submit_order(req)
        logger.info(f"Order accepted: {order.id}  status={order.status}")
        return order

    def close_position(self, option_symbol: str):
        """Closes (sells) an open options position."""
        try:
            result = self._trading.close_position(option_symbol)
            logger.info(f"Closed: {option_symbol}")
            return result
        except Exception as e:
            logger.error(f"Failed to close {option_symbol}: {e}")
            raise
