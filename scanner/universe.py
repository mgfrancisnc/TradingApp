"""
universe.py
Discovers stock candidates using tradingview-screener.
Replaces the static DEFAULT_UNIVERSE list in weekly_scanner.py.

Install: pip install tradingview-screener

This solves Hayes' use case: 'advice on stocks we may not be watching.'
Instead of a fixed list, we screen the entire market for names that
match the broad conditions of his philosophy, then score them properly.

No API key needed — uses TradingView's public screener endpoint.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Fallback list if screener fails or is unavailable
FALLBACK_UNIVERSE = [
    # Large cap growth
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META",
    # Quality growth
    "CRM", "NOW", "CRWD", "NET", "DDOG",
    # Income / dividend growth
    "KO", "JNJ", "PG", "V", "MA",
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
    recommendation: Optional[str]   # TradingView's own signal: BUY/SELL/NEUTRAL


class UniverseScreener:
    """
    Uses tradingview-screener to find stocks matching Hayes' broad criteria.
    Runs at the start of each weekly scan to build a fresh candidate list.

    Screens for:
    - US equities only (NYSE + NASDAQ)
    - Market cap > $2B (liquid enough for options)
    - Volume > 500K average (options liquidity)
    - Beta between 0.5 and 3.0 (not too dull, not too wild)
    - Excludes ETFs, funds, and ADRs
    """

    MIN_MARKET_CAP = 2_000_000_000     # $2B minimum
    MIN_AVG_VOLUME = 500_000           # 500K avg daily volume
    MAX_RESULTS = 100                  # Screen top 100, scorer narrows it down

    def get_universe(self) -> list[str]:
        """
        Returns a list of ticker symbols to scan.
        Falls back to static list if screener unavailable.
        """
        try:
            from tradingview_screener import Query
            symbols = self._run_screen()
            if symbols:
                logger.info(f"TradingView screener returned {len(symbols)} candidates")
                return symbols
        except ImportError:
            logger.warning(
                "tradingview-screener not installed. "
                "Run: pip install tradingview-screener\n"
                "Falling back to static universe."
            )
        except Exception as e:
            logger.warning(f"Screener error: {e} — falling back to static universe")

        logger.info(f"Using fallback universe of {len(FALLBACK_UNIVERSE)} symbols")
        return FALLBACK_UNIVERSE

    def get_discovery_candidates(self) -> list[str]:
        """
        Runs a more aggressive screen specifically to surface names
        Hayes may not be watching — strong momentum + volume surge.
        Returns up to 30 additional candidates beyond the base screen.
        """
        try:
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
                    "relative_volume_10d_calc",  # Volume vs 10-day avg
                )
                .where(
                    col("market_cap_basic") > self.MIN_MARKET_CAP,
                    col("average_volume_10d_calc") > self.MIN_AVG_VOLUME,
                    col("relative_volume_10d_calc") > 1.5,     # Volume surging
                    col("beta_1_year").between(0.5, 3.0),
                    col("type") == "stock",
                    col("subtype") == "common",                # No ETFs/ADRs
                    col("exchange").isin(["NYSE", "NASDAQ"]),
                )
                .order_by("relative_volume_10d_calc", ascending=False)
                .limit(30)
                .get_scanner_data()
            )
            symbols = df["name"].tolist() if df is not None and not df.empty else []
            if symbols:
                logger.info(f"Discovery screen found {len(symbols)} volume-surge candidates")
            return symbols

        except Exception as e:
            logger.warning(f"Discovery screen failed: {e}")
            return []

    # ─────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────

    def _run_screen(self) -> list[str]:
        """
        Base screen: liquid US stocks with options-friendly characteristics.
        Broad enough to catch everything — the philosophy scorer narrows it down.
        """
        from tradingview_screener import Query, col

        # ── Growth screen ─────────────────────────
        # High CAGR, momentum, volume confirmation
        _, growth_df = (
            Query()
            .select(
                "name", "close", "volume", "market_cap_basic",
                "sector", "beta_1_year",
                "revenue_per_employee",         # Proxy for efficiency
                "earnings_per_share_diluted_ttm",
                "dividends_yield_current",
                "Recommend.All",                # TradingView composite signal
            )
            .where(
                col("market_cap_basic") > self.MIN_MARKET_CAP,
                col("average_volume_10d_calc") > self.MIN_AVG_VOLUME,
                col("beta_1_year").between(0.8, 3.0),   # Growth needs some beta
                col("earnings_per_share_diluted_ttm") > 0,  # Profitable
                col("type") == "stock",
                col("subtype") == "common",
                col("exchange").isin(["NYSE", "NASDAQ"]),
            )
            .order_by("market_cap_basic", ascending=False)
            .limit(60)
            .get_scanner_data()
        )

        # ── Income screen ─────────────────────────
        # Dividend payers with yield above ~2%
        _, income_df = (
            Query()
            .select(
                "name", "close", "volume", "market_cap_basic",
                "sector", "beta_1_year",
                "dividends_yield_current",
                "Recommend.All",
            )
            .where(
                col("market_cap_basic") > self.MIN_MARKET_CAP,
                col("average_volume_10d_calc") > self.MIN_AVG_VOLUME,
                col("dividends_yield_current") > 2.0,   # >2% yield
                col("beta_1_year") < 1.5,               # Income = lower beta
                col("type") == "stock",
                col("subtype") == "common",
                col("exchange").isin(["NYSE", "NASDAQ"]),
            )
            .order_by("dividends_yield_current", ascending=False)
            .limit(40)
            .get_scanner_data()
        )

        # Combine and deduplicate
        symbols = []
        seen = set()

        for df in [growth_df, income_df]:
            if df is not None and not df.empty:
                for sym in df["name"].tolist():
                    if sym not in seen:
                        symbols.append(sym)
                        seen.add(sym)

        return symbols[:self.MAX_RESULTS]
