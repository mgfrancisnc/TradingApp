"""
universe.py
Large-cap stock universe for the Francis-Hayes Trading Bot.

Screens for stocks that meet the covered call universe criteria from philosophy.yaml:
  - Market cap ≥ $10B (large-cap only — no small caps)
  - Average daily volume ≥ 1M shares (deep liquidity)
  - NYSE or NASDAQ only
  - Common stocks only (no ETFs, ADRs, or preferred shares)

The screener returns a broad candidate list. The philosophy scorer narrows it down.
Falls back to a curated S&P 100 + mega-cap static list if the screener is unavailable.

No API key required — uses TradingView's public screener endpoint.
Install: pip install tradingview-screener
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# S&P 100 + select mega-caps + major sector leaders
# Used when tradingview-screener is unavailable
FALLBACK_UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META",
    # Large-cap tech
    "AVGO", "ORCL", "CRM", "ADBE", "AMD", "INTC", "QCOM",
    # Financials
    "BRK-B", "JPM", "BAC", "WFC", "GS", "MS", "AXP", "BLK",
    # Healthcare
    "JNJ", "UNH", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT",
    # Consumer staples (income)
    "PG", "KO", "PEP", "WMT", "COST", "MCD", "PM",
    # Industrials
    "CAT", "HON", "UPS", "RTX", "BA", "GE", "MMM",
    # Energy
    "XOM", "CVX", "COP",
    # Utilities (income)
    "NEE", "DUK", "SO",
    # Telecom (income)
    "VZ", "T",
    # Payment networks
    "V", "MA",
    # Real estate (income)
    "AMT", "PLD",
]


@dataclass
class ScreenerResult:
    symbol: str
    close: float
    volume: float
    market_cap: Optional[float]
    sector: Optional[str]
    beta_1_year: Optional[float]
    revenue_growth_yoy: Optional[float]
    dividend_yield: Optional[float]


class UniverseScreener:
    """
    Builds the weekly candidate list using TradingView's public screener.
    Filters strictly to large-cap, liquid, NYSE/NASDAQ common stocks.
    """

    MIN_MARKET_CAP = 10_000_000_000    # $10B — large-cap only
    MIN_AVG_VOLUME = 1_000_000          # 1M shares/day avg
    MAX_RESULTS = 150                   # Screen up to 150 names

    def get_universe(self) -> list[str]:
        """
        Returns ticker symbols to scan this week.
        Falls back to static list if screener unavailable.
        """
        try:
            from tradingview_screener import Query
            symbols = self._run_screen()
            if symbols:
                logger.info(f"TradingView screener returned {len(symbols)} large-cap candidates")
                return symbols
        except ImportError:
            logger.warning(
                "tradingview-screener not installed. "
                "Run: pip install tradingview-screener\n"
                "Falling back to static universe."
            )
        except Exception as e:
            logger.warning(f"Screener error: {e} — falling back to static universe")

        logger.info(f"Using fallback universe ({len(FALLBACK_UNIVERSE)} symbols)")
        return FALLBACK_UNIVERSE

    # ─────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────

    def _run_screen(self) -> list[str]:
        """
        Large-cap screen — broad enough to catch the full opportunity set,
        tight enough to stay within Hayes' 'familiar company' universe.
        """
        from tradingview_screener import Query, col

        _, df = (
            Query()
            .select(
                "name",
                "close",
                "volume",
                "market_cap_basic",
                "sector",
                "beta_1_year",
                "dividends_yield_current",
                "revenue_per_employee",
                "earnings_per_share_diluted_ttm",
                "Recommend.All",
            )
            .where(
                col("market_cap_basic") > self.MIN_MARKET_CAP,
                col("average_volume_10d_calc") > self.MIN_AVG_VOLUME,
                col("type") == "stock",
                col("subtype") == "common",
                col("exchange").isin(["NYSE", "NASDAQ"]),
            )
            .order_by("market_cap_basic", ascending=False)
            .limit(self.MAX_RESULTS)
            .get_scanner_data()
        )

        if df is None or df.empty:
            return []

        symbols = df["name"].tolist()
        logger.debug(f"Screen returned {len(symbols)} symbols (top by market cap)")
        return symbols
