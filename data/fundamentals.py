"""
fundamentals.py
Fetches company fundamentals using yfinance — no API key required.

Install: pip install yfinance

Hayes' fundamentals priorities (from recording):
  - Revenue CAGR is the #1 metric for growth companies
  - "You can't lie about revenue"
  - Revenue deceleration is a red flag: "paid for 20%, got 10%"
  - For income stocks: dividend yield must beat 10yr treasury
  - Beta tells you volatility relative to market
  - "If you're not familiar with the company — don't touch it"
"""

import logging
from dataclasses import dataclass
from typing import Optional

import yfinance as yf
import numpy as np

logger = logging.getLogger(__name__)

GROWTH_CAGR_THRESHOLD = 0.08        # >8% 3yr CAGR = growth company
INCOME_YIELD_THRESHOLD = 0.02       # >2% dividend yield = income candidate


@dataclass
class FundamentalData:
    symbol: str
    stock_type: str                     # 'growth' or 'income'
    revenue_cagr: Optional[float]       # 3-year CAGR as decimal e.g. 0.20 = 20%
    revenue_growth_yoy: Optional[float] # Most recent YoY growth %
    revenue_accelerating: bool          # Not decelerating sharply
    dividend_yield: Optional[float]     # Annual yield as decimal e.g. 0.03 = 3%
    dividend_growing: bool
    beta: Optional[float]
    sector: Optional[str]
    industry: Optional[str]
    market_cap: Optional[float]
    brief: str


class FundamentalsFetcher:
    """
    Fetches and interprets fundamentals per Hayes' philosophy.
    Uses yfinance — free, no API key needed.
    Results cached per session.
    """

    def __init__(self):
        self._cache: dict[str, Optional[FundamentalData]] = {}

    def get(self, symbol: str) -> Optional[FundamentalData]:
        if symbol in self._cache:
            return self._cache[symbol]
        try:
            data = self._fetch(symbol)
            self._cache[symbol] = data
            return data
        except Exception as e:
            logger.error(f"{symbol}: Fundamentals fetch failed — {e}")
            self._cache[symbol] = None
            return None

    def get_treasury_rate(self) -> float:
        """
        Fetches current 10-year treasury yield via yfinance (^TNX).
        Hayes: 'All returns are based on the 10-year treasury — the risk-free rate.'
        """
        try:
            tnx = yf.Ticker("^TNX")
            rate = tnx.fast_info.get("lastPrice")
            if rate:
                rate = rate / 100
                logger.info(f"10yr treasury rate: {rate:.2%}")
                return rate
        except Exception as e:
            logger.warning(f"Could not fetch treasury rate: {e} — using 4.5% fallback")
        return 0.045

    def _fetch(self, symbol: str) -> Optional[FundamentalData]:
        ticker = yf.Ticker(symbol)
        info = ticker.info

        if not info or info.get("regularMarketPrice") is None:
            logger.warning(f"{symbol}: No data from yfinance")
            return None

        financials = ticker.financials
        revenue_cagr, revenue_yoy, accelerating = self._analyze_revenue(symbol, financials)

        dividend_yield = self._safe_float(info.get("dividendYield"))
        dividend_growing = self._is_dividend_growing(ticker.dividends)
        beta = self._safe_float(info.get("beta"))
        sector = info.get("sector", "Unknown")
        industry = info.get("industry", "Unknown")
        market_cap = self._safe_float(info.get("marketCap"))
        stock_type = self._classify_stock(revenue_cagr, dividend_yield, sector)

        brief = self._build_brief(
            symbol, stock_type, sector, revenue_cagr,
            revenue_yoy, dividend_yield, beta, market_cap
        )

        return FundamentalData(
            symbol=symbol,
            stock_type=stock_type,
            revenue_cagr=revenue_cagr,
            revenue_growth_yoy=revenue_yoy,
            revenue_accelerating=accelerating,
            dividend_yield=dividend_yield,
            dividend_growing=dividend_growing,
            beta=beta,
            sector=sector,
            industry=industry,
            market_cap=market_cap,
            brief=brief,
        )

    def _analyze_revenue(self, symbol, financials) -> tuple[Optional[float], Optional[float], bool]:
        """
        Returns (3yr_cagr, yoy_growth, is_not_decelerating).
        Hayes: 'CAGR is the #1 metric. Revenue deceleration is a red flag.'
        """
        try:
            if financials is None or financials.empty:
                return None, None, True
            if "Total Revenue" not in financials.index:
                return None, None, True

            rev_row = financials.loc["Total Revenue"].dropna()
            revenues = rev_row.sort_index(ascending=False).values.tolist()

            if len(revenues) < 2:
                return None, None, True

            yoy = (revenues[0] - revenues[1]) / revenues[1] if revenues[1] > 0 else 0

            cagr = None
            if len(revenues) >= 4:
                start, end = revenues[3], revenues[0]
                if start > 0 and end > 0:
                    cagr = (end / start) ** (1 / 3) - 1
            else:
                cagr = yoy

            # Deceleration check — Hayes: 'paid for 20%, got 10%'
            accelerating = True
            if len(revenues) >= 3:
                prior_yoy = (revenues[1] - revenues[2]) / revenues[2] if revenues[2] > 0 else 0
                if prior_yoy > 0.05 and yoy < prior_yoy * 0.5:
                    accelerating = False
                    logger.info(f"{symbol}: Revenue decelerating {prior_yoy:.1%} → {yoy:.1%}")

            return (
                round(cagr, 4) if cagr is not None else None,
                round(yoy, 4),
                accelerating,
            )
        except Exception as e:
            logger.warning(f"{symbol}: Revenue analysis error — {e}")
            return None, None, True

    def _is_dividend_growing(self, dividends) -> bool:
        """Hayes: 'Does the dividend grow each and every year?'"""
        try:
            if dividends is None or dividends.empty:
                return False
            div_df = dividends.copy()
            div_df.index = div_df.index.year
            yearly = div_df.groupby(div_df.index).sum().sort_index(ascending=False)
            if len(yearly) < 3:
                return False
            years = yearly.values[:4]
            return all(years[i] >= years[i + 1] for i in range(min(3, len(years) - 1)))
        except Exception:
            return False

    def _classify_stock(self, revenue_cagr, dividend_yield, sector) -> str:
        """Hayes: 'You either buy growth or you buy income.'"""
        has_growth = revenue_cagr is not None and revenue_cagr >= GROWTH_CAGR_THRESHOLD
        has_income = dividend_yield is not None and dividend_yield >= INCOME_YIELD_THRESHOLD
        income_sectors = {"utilities", "real estate", "consumer defensive", "financial services"}
        s = sector.lower()
        if has_growth and not has_income:
            return "growth"
        elif has_income and not has_growth:
            return "income"
        elif has_growth and has_income:
            return "income" if any(x in s for x in income_sectors) else "growth"
        else:
            return "income" if any(x in s for x in income_sectors) else "growth"

    def _build_brief(self, symbol, stock_type, sector, revenue_cagr,
                     revenue_yoy, dividend_yield, beta, market_cap) -> str:
        parts = [f"{sector}  |  {stock_type.upper()}"]
        if revenue_yoy is not None:
            parts.append(f"Rev YoY: {revenue_yoy:+.1%}")
        if revenue_cagr is not None:
            parts.append(f"3Y CAGR: {revenue_cagr:.1%}")
        if dividend_yield is not None:
            parts.append(f"Yield: {dividend_yield:.1%}")
        if beta is not None:
            parts.append(f"Beta: {beta:.2f}")
        if market_cap:
            cap = f"${market_cap/1e9:.0f}B" if market_cap >= 1e9 else f"${market_cap/1e6:.0f}M"
            parts.append(cap)
        return "  |  ".join(parts)

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
