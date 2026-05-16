"""
market_data.py
Monthly and weekly trend analysis for the Francis-Hayes Trading Bot.

Uses yfinance — no API key required.

Hayes' rules encoded here:
  - Monthly chart dictates direction and trend
  - Weekly must confirm monthly (same direction)
  - Daily is too noisy — never trade off daily
  - 'Volume predicts price — the more volume, the more price'
  - Breaking support is a hard stop
"""

import logging
from dataclasses import dataclass
from typing import Optional

import yfinance as yf
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Trend labels
BULLISH = "bullish"
BEARISH = "bearish"
SIDEWAYS = "sideways"


@dataclass
class TrendAnalysis:
    symbol: str
    current_price: float
    monthly_trend: str              # 'bullish', 'bearish', 'sideways'
    monthly_trend_strength: float   # 0.0 – 1.0 (used in scoring)
    weekly_trend: str               # 'bullish', 'bearish', 'sideways'
    weekly_confirms_monthly: bool   # True when weekly direction agrees with monthly
    breaking_support: bool          # True if price has broken below recent support
    volume_trend: str               # 'rising', 'falling', 'flat'
    volume_trend_score: float       # 0.0 – 1.0


class MarketDataAnalyzer:
    """
    Analyses monthly and weekly price + volume trends using yfinance.
    Results are cached per session to avoid redundant network calls.
    """

    def __init__(self, alpaca_client=None):
        # alpaca_client kept for interface compatibility; yfinance is used here
        self._cache: dict[str, Optional[TrendAnalysis]] = {}
        self._sp500_trend_cache: Optional[str] = None

    # ─────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────

    def analyze(self, symbol: str) -> Optional[TrendAnalysis]:
        """
        Returns TrendAnalysis for the given symbol.
        Returns None if data is unavailable.
        """
        if symbol in self._cache:
            return self._cache[symbol]

        try:
            result = self._run_analysis(symbol)
            self._cache[symbol] = result
            return result
        except Exception as e:
            logger.error(f"{symbol}: trend analysis failed — {e}")
            self._cache[symbol] = None
            return None

    def get_sp500_trend(self) -> str:
        """
        Returns the monthly trend of the S&P 500 ('bullish', 'bearish', 'sideways').
        Hayes uses this to decide ATM vs OTM strike selection.
        Cached for the session.
        """
        if self._sp500_trend_cache:
            return self._sp500_trend_cache

        try:
            result = self._run_analysis("SPY")
            trend = result.monthly_trend if result else SIDEWAYS
        except Exception:
            trend = SIDEWAYS

        self._sp500_trend_cache = trend
        logger.info(f"S&P 500 monthly trend: {trend}")
        return trend

    # ─────────────────────────────────────────────
    # ANALYSIS ENGINE
    # ─────────────────────────────────────────────

    def _run_analysis(self, symbol: str) -> Optional[TrendAnalysis]:
        ticker = yf.Ticker(symbol)

        # Pull 3 years of monthly data (need 12+ bars for SMA)
        monthly = ticker.history(period="3y", interval="1mo")
        # Pull 1 year of weekly data
        weekly = ticker.history(period="1y", interval="1wk")

        if monthly.empty or len(monthly) < 6:
            logger.warning(f"{symbol}: insufficient monthly data")
            return None

        # ── Current price ────────────────────────
        current_price = float(monthly["Close"].iloc[-1])

        # ── Monthly trend ────────────────────────
        monthly_trend, monthly_strength = self._monthly_trend(monthly)

        # ── Weekly trend ────────────────────────
        weekly_trend = self._weekly_trend(weekly) if not weekly.empty else SIDEWAYS

        # ── Alignment ────────────────────────────
        weekly_confirms = self._trends_aligned(monthly_trend, weekly_trend)

        # ── Support break ────────────────────────
        breaking_support = self._is_breaking_support(monthly)

        # ── Volume trend ────────────────────────
        volume_trend, volume_score = self._volume_trend(monthly)

        return TrendAnalysis(
            symbol=symbol,
            current_price=current_price,
            monthly_trend=monthly_trend,
            monthly_trend_strength=monthly_strength,
            weekly_trend=weekly_trend,
            weekly_confirms_monthly=weekly_confirms,
            breaking_support=breaking_support,
            volume_trend=volume_trend,
            volume_trend_score=volume_score,
        )

    # ─────────────────────────────────────────────
    # MONTHLY TREND
    # ─────────────────────────────────────────────

    def _monthly_trend(self, monthly: pd.DataFrame) -> tuple[str, float]:
        """
        Determines monthly trend using a 10-month simple moving average.
        Strength is a 0-1 score:
          - price vs SMA direction
          - SMA slope over last 3 months
          - how far price is from SMA
        """
        closes = monthly["Close"].dropna()

        sma_period = min(10, len(closes) - 1)
        sma = closes.rolling(sma_period).mean()

        current = float(closes.iloc[-1])
        current_sma = float(sma.iloc[-1])

        if pd.isna(current_sma):
            return SIDEWAYS, 0.5

        # Price above/below SMA
        above_sma = current > current_sma
        pct_from_sma = (current - current_sma) / current_sma  # +ve = above

        # SMA slope (3 months)
        if len(sma.dropna()) >= 3:
            sma_3m_ago = float(sma.dropna().iloc[-3])
            sma_slope = (current_sma - sma_3m_ago) / sma_3m_ago
        else:
            sma_slope = 0.0

        # Classify trend
        if above_sma and sma_slope > 0:
            trend = BULLISH
        elif not above_sma and sma_slope < 0:
            trend = BEARISH
        else:
            trend = SIDEWAYS

        # Strength: 0–1
        #   Start at 0.5, add for being above SMA, rising SMA slope, larger gap
        strength = 0.5
        if trend == BULLISH:
            strength += min(0.25, abs(pct_from_sma) * 2)     # distance above SMA
            strength += min(0.25, sma_slope * 5)              # slope contribution
        elif trend == BEARISH:
            strength = 0.5
            strength -= min(0.25, abs(pct_from_sma) * 2)
            strength -= min(0.25, abs(sma_slope) * 5)
        else:
            strength = 0.5

        strength = max(0.0, min(1.0, strength))
        return trend, round(strength, 3)

    # ─────────────────────────────────────────────
    # WEEKLY TREND
    # ─────────────────────────────────────────────

    def _weekly_trend(self, weekly: pd.DataFrame) -> str:
        """
        Simple weekly trend: price vs 26-week SMA.
        Hayes: 'Weekly must agree with monthly — not fight it.'
        """
        closes = weekly["Close"].dropna()
        if len(closes) < 10:
            return SIDEWAYS

        sma = closes.rolling(min(26, len(closes))).mean()
        current = float(closes.iloc[-1])
        sma_now = float(sma.iloc[-1])

        if pd.isna(sma_now):
            return SIDEWAYS

        if current > sma_now * 1.01:
            return BULLISH
        elif current < sma_now * 0.99:
            return BEARISH
        else:
            return SIDEWAYS

    # ─────────────────────────────────────────────
    # ALIGNMENT
    # ─────────────────────────────────────────────

    def _trends_aligned(self, monthly: str, weekly: str) -> bool:
        """
        True when weekly confirms monthly.
        Sideways weekly is acceptable with bullish monthly (not a contradiction).
        """
        if monthly == BULLISH:
            return weekly in (BULLISH, SIDEWAYS)
        elif monthly == BEARISH:
            return weekly in (BEARISH, SIDEWAYS)
        else:
            return True  # Sideways monthly — weekly doesn't need to confirm

    # ─────────────────────────────────────────────
    # SUPPORT
    # ─────────────────────────────────────────────

    def _is_breaking_support(self, monthly: pd.DataFrame) -> bool:
        """
        Flags if price is making new 6-month lows on a monthly close basis.
        Hayes: avoid stocks breaking down through support.
        """
        try:
            closes = monthly["Close"].dropna()
            if len(closes) < 7:
                return False
            current = float(closes.iloc[-1])
            prior_6m_low = float(closes.iloc[-7:-1].min())
            # Breaking support = current close below the 6-month prior low
            return current < prior_6m_low * 0.97  # 3% buffer
        except Exception:
            return False

    # ─────────────────────────────────────────────
    # VOLUME TREND
    # ─────────────────────────────────────────────

    def _volume_trend(self, monthly: pd.DataFrame) -> tuple[str, float]:
        """
        Compares recent 3-month average volume to prior 6-month average.
        Hayes: 'Volume predicts price — the more volume, the more price.'
        """
        try:
            vol = monthly["Volume"].dropna()
            if len(vol) < 9:
                return "flat", 0.5

            recent_avg = float(vol.iloc[-3:].mean())
            prior_avg = float(vol.iloc[-9:-3].mean())

            if prior_avg == 0:
                return "flat", 0.5

            ratio = recent_avg / prior_avg
            if ratio > 1.15:
                return "rising", min(1.0, 0.5 + (ratio - 1.0) * 1.5)
            elif ratio < 0.85:
                return "falling", max(0.0, 0.5 - (1.0 - ratio) * 1.5)
            else:
                return "flat", 0.5
        except Exception:
            return "flat", 0.5
