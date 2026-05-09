"""
fundamentals.py
Fetches company fundamentals needed for Hayes' philosophy scoring.
Uses Financial Modeling Prep (FMP) API — free tier is sufficient.

Get your free API key at: https://financialmodelingprep.com/developer/docs

Set environment variable:
    export FMP_API_KEY=your_key_here

Hayes' fundamentals priorities (from recording):
  - Revenue growth (CAGR) is the #1 metric for growth companies
  - "You can't lie about revenue"
  - Revenue deceleration is a red flag: "paid for 20%, got 10%"
  - For income stocks: dividend yield must beat 10yr treasury
  - Beta tells you volatility relative to market
  - Know the company — "if you're not familiar, don't touch it"
"""

import os
import requests
import logging
from dataclasses import dataclass
from typing import Optional
from functools import lru_cache

logger = logging.getLogger(__name__)

FMP_BASE = "https://financialmodelingprep.com/api"


@dataclass
class FundamentalData:
    symbol: str
    stock_type: str                     # 'growth' or 'income'
    # Revenue metrics — Hayes' #1 priority
    revenue_cagr: Optional[float]       # 3-year CAGR as decimal e.g. 0.20 = 20%
    revenue_growth_yoy: Optional[float] # Most recent YoY growth %
    revenue_accelerating: bool          # Is CAGR improving or holding?
    # Income metrics
    dividend_yield: Optional[float]     # Annual yield as decimal e.g. 0.03 = 3%
    dividend_growing: bool              # Has dividend grown each year?
    # Risk metrics
    beta: Optional[float]               # Volatility vs market
    # Company context — for the scan report
    sector: Optional[str]
    industry: Optional[str]
    description: Optional[str]
    market_cap: Optional[float]
    brief: str                          # One-line summary for scan report


class FundamentalsFetcher:
    """
    Fetches and interprets fundamentals per Hayes' philosophy.
    Results are cached per session to avoid redundant API calls during scans.
    """

    # Revenue CAGR thresholds for growth vs income classification
    GROWTH_CAGR_THRESHOLD = 0.08        # >8% CAGR = growth company
    INCOME_YIELD_THRESHOLD = 0.02       # >2% yield = income candidate

    def __init__(self):
        self.api_key = os.environ.get("FMP_API_KEY")
        if not self.api_key:
            raise EnvironmentError(
                "FMP_API_KEY not set. Get a free key at "
                "https://financialmodelingprep.com/developer/docs"
            )
        self._cache: dict[str, FundamentalData] = {}

    def get(self, symbol: str) -> Optional[FundamentalData]:
        """
        Main entry point. Returns FundamentalData or None if data unavailable.
        Results cached for the session.
        """
        if symbol in self._cache:
            return self._cache[symbol]

        try:
            profile = self._fetch_profile(symbol)
            growth = self._fetch_revenue_growth(symbol)
            income = self._fetch_income_statements(symbol)
            dividends = self._fetch_dividends(symbol)

            if not profile:
                logger.warning(f"{symbol}: No profile data available")
                return None

            data = self._build_fundamental_data(
                symbol, profile, growth, income, dividends
            )
            self._cache[symbol] = data
            return data

        except Exception as e:
            logger.error(f"Error fetching fundamentals for {symbol}: {e}")
            return None

    def get_treasury_rate(self) -> float:
        """
        Fetches current 10-year treasury rate.
        Hayes: 'All returns are based on the 10-year treasury — the risk-free rate.'
        Falls back to 4.5% if unavailable.
        """
        try:
            url = f"{FMP_BASE}/v4/treasury"
            params = {"apikey": self.api_key}
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()
            if data and isinstance(data, list):
                # Find the 10-year rate
                for item in data:
                    if item.get("month") == 120 or "10" in str(item.get("name", "")):
                        rate = float(item.get("rate", 4.5)) / 100
                        logger.info(f"10yr treasury rate: {rate:.2%}")
                        return rate
        except Exception as e:
            logger.warning(f"Could not fetch treasury rate: {e} — using 4.5% fallback")

        return 0.045   # Fallback: 4.5%

    # ─────────────────────────────────────────────
    # FMP API CALLS
    # ─────────────────────────────────────────────

    def _fetch_profile(self, symbol: str) -> Optional[dict]:
        """
        Company profile — gives us beta, dividend yield, sector, description.
        FMP endpoint: /api/v3/profile/{symbol}
        """
        url = f"{FMP_BASE}/v3/profile/{symbol}"
        params = {"apikey": self.api_key}
        resp = requests.get(url, params=params, timeout=10)

        if resp.status_code != 200:
            logger.warning(f"{symbol}: Profile API returned {resp.status_code}")
            return None

        data = resp.json()
        return data[0] if data else None

    def _fetch_revenue_growth(self, symbol: str) -> Optional[dict]:
        """
        Revenue growth metrics including 3Y and 5Y CAGR.
        FMP endpoint: /api/v3/financial-growth/{symbol}
        Returns the most recent growth data row.
        Hayes: 'CAGR is the #1 metric for growth companies.'
        """
        url = f"{FMP_BASE}/v3/financial-growth/{symbol}"
        params = {"apikey": self.api_key, "limit": 1}
        resp = requests.get(url, params=params, timeout=10)

        if resp.status_code != 200:
            return None

        data = resp.json()
        return data[0] if data else None

    def _fetch_income_statements(self, symbol: str) -> list[dict]:
        """
        Annual income statements — used to calculate revenue trend
        and check for deceleration.
        FMP endpoint: /api/v3/income-statement/{symbol}
        Hayes: 'Is revenue growing or decelerating?'
        """
        url = f"{FMP_BASE}/v3/income-statement/{symbol}"
        params = {"apikey": self.api_key, "limit": 4, "period": "annual"}
        resp = requests.get(url, params=params, timeout=10)

        if resp.status_code != 200:
            return []

        return resp.json() or []

    def _fetch_dividends(self, symbol: str) -> list[dict]:
        """
        Historical dividend payments — checks if dividend is growing.
        FMP endpoint: /api/v3/historical-price-full/stock_dividend/{symbol}
        Hayes: 'Does the dividend grow each and every year?'
        """
        url = f"{FMP_BASE}/v3/historical-price-full/stock_dividend/{symbol}"
        params = {"apikey": self.api_key}
        resp = requests.get(url, params=params, timeout=10)

        if resp.status_code != 200:
            return []

        data = resp.json()
        return data.get("historical", [])[:8] if data else []  # Last 8 payments

    # ─────────────────────────────────────────────
    # DATA INTERPRETATION
    # ─────────────────────────────────────────────

    def _build_fundamental_data(
        self,
        symbol: str,
        profile: dict,
        growth: Optional[dict],
        income: list[dict],
        dividends: list[dict],
    ) -> FundamentalData:

        # ── Beta ─────────────────────────────────
        beta = self._safe_float(profile.get("beta"))

        # ── Revenue CAGR ─────────────────────────
        # FMP gives us 3Y and 5Y per-share revenue growth
        # We prefer 3Y as it's more current
        revenue_cagr = None
        if growth:
            cagr_3y = self._safe_float(growth.get("threeYRevenueGrowthPerShare"))
            cagr_5y = self._safe_float(growth.get("fiveYRevenueGrowthPerShare"))
            revenue_cagr = cagr_3y if cagr_3y is not None else cagr_5y

        # ── Revenue YoY & Deceleration ───────────
        revenue_growth_yoy, revenue_accelerating = self._analyze_revenue_trend(income)

        # ── Dividend ─────────────────────────────
        dividend_yield = self._safe_float(profile.get("lastDiv"))
        current_price = self._safe_float(profile.get("price"))
        if dividend_yield and current_price and current_price > 0:
            # FMP lastDiv is annual dividend amount, not yield — convert
            dividend_yield = dividend_yield / current_price
        else:
            dividend_yield = None

        dividend_growing = self._is_dividend_growing(dividends)

        # ── Stock Classification ──────────────────
        # Hayes: 'Are you buying growth or income?'
        stock_type = self._classify_stock(
            revenue_cagr, dividend_yield, profile
        )

        # ── Company Brief ─────────────────────────
        sector = profile.get("sector", "Unknown")
        industry = profile.get("industry", "Unknown")
        mkt_cap = self._safe_float(profile.get("mktCap"))
        description = profile.get("description", "")

        brief = self._build_brief(
            symbol, stock_type, sector, revenue_cagr,
            revenue_growth_yoy, dividend_yield, beta, mkt_cap
        )

        return FundamentalData(
            symbol=symbol,
            stock_type=stock_type,
            revenue_cagr=revenue_cagr,
            revenue_growth_yoy=revenue_growth_yoy,
            revenue_accelerating=revenue_accelerating,
            dividend_yield=dividend_yield,
            dividend_growing=dividend_growing,
            beta=beta,
            sector=sector,
            industry=industry,
            description=description[:200] if description else None,
            market_cap=mkt_cap,
            brief=brief,
        )

    def _analyze_revenue_trend(
        self, income: list[dict]
    ) -> tuple[Optional[float], bool]:
        """
        Calculates YoY revenue growth and checks for deceleration.
        Hayes: 'If it decelerates from 20% to 10% — you paid for 20% and got 10%.'

        Returns: (yoy_growth_pct, is_accelerating_or_stable)
        """
        if len(income) < 2:
            return None, True   # Unknown — give benefit of doubt

        revenues = []
        for stmt in income[:4]:    # Up to 4 years
            rev = self._safe_float(stmt.get("revenue"))
            if rev:
                revenues.append(rev)

        if len(revenues) < 2:
            return None, True

        # Most recent YoY
        yoy = (revenues[0] - revenues[1]) / revenues[1] if revenues[1] > 0 else 0

        # Check deceleration — is recent growth lower than prior period?
        accelerating = True
        if len(revenues) >= 3:
            prior_yoy = (revenues[1] - revenues[2]) / revenues[2] if revenues[2] > 0 else 0
            # Flag if growth dropped by more than 50% of prior rate
            max_decel = 0.50
            if prior_yoy > 0 and yoy < prior_yoy * (1 - max_decel):
                accelerating = False
                logger.info(
                    f"Revenue decelerating: {prior_yoy:.1%} → {yoy:.1%}"
                )

        return round(yoy * 100, 2), accelerating   # Return as percentage

    def _is_dividend_growing(self, dividends: list[dict]) -> bool:
        """
        Checks if annual dividend has grown consistently.
        Hayes: 'Does the dividend grow each and every year?'
        """
        if len(dividends) < 4:
            return False

        # Group by year, sum dividends per year
        yearly: dict[int, float] = {}
        for d in dividends:
            date = d.get("date", "")
            amount = self._safe_float(d.get("dividend", 0)) or 0
            if date:
                year = int(date[:4])
                yearly[year] = yearly.get(year, 0) + amount

        if len(yearly) < 3:
            return False

        sorted_years = sorted(yearly.keys(), reverse=True)
        # Check last 3 years are growing
        return all(
            yearly[sorted_years[i]] >= yearly[sorted_years[i + 1]]
            for i in range(min(3, len(sorted_years) - 1))
        )

    def _classify_stock(
        self,
        revenue_cagr: Optional[float],
        dividend_yield: Optional[float],
        profile: dict,
    ) -> str:
        """
        Classifies stock as 'growth' or 'income'.
        Hayes: 'You either buy growth or you buy income.'
        """
        has_meaningful_dividend = (
            dividend_yield is not None and dividend_yield >= self.INCOME_YIELD_THRESHOLD
        )
        has_strong_growth = (
            revenue_cagr is not None and revenue_cagr >= self.GROWTH_CAGR_THRESHOLD
        )

        # Sector hints
        sector = profile.get("sector", "").lower()
        income_sectors = {"utilities", "real estate", "consumer defensive", "financial services"}
        growth_sectors = {"technology", "communication services", "healthcare", "consumer cyclical"}

        if has_strong_growth and not has_meaningful_dividend:
            return "growth"
        elif has_meaningful_dividend and not has_strong_growth:
            return "income"
        elif has_strong_growth and has_meaningful_dividend:
            # Both — lean on sector
            return "income" if any(s in sector for s in income_sectors) else "growth"
        else:
            # Default by sector
            return "income" if any(s in sector for s in income_sectors) else "growth"

    def _build_brief(
        self,
        symbol: str,
        stock_type: str,
        sector: str,
        revenue_cagr: Optional[float],
        revenue_yoy: Optional[float],
        dividend_yield: Optional[float],
        beta: Optional[float],
        market_cap: Optional[float],
    ) -> str:
        """One-line summary for the weekly scan report."""
        parts = [f"{sector}  |  {stock_type.upper()}"]

        if revenue_yoy is not None:
            parts.append(f"Rev YoY: {revenue_yoy:+.1f}%")
        if revenue_cagr is not None:
            parts.append(f"3Y CAGR: {revenue_cagr:.1%}")
        if dividend_yield is not None:
            parts.append(f"Yield: {dividend_yield:.1%}")
        if beta is not None:
            parts.append(f"Beta: {beta:.2f}")
        if market_cap:
            cap_str = f"${market_cap/1e9:.0f}B" if market_cap >= 1e9 else f"${market_cap/1e6:.0f}M"
            parts.append(cap_str)

        return "  |  ".join(parts)

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        """Safely converts a value to float, returning None on failure."""
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
