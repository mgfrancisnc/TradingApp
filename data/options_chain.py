"""
options_chain.py
Options chain analysis for the Francis-Hayes Trading Bot.

Pulls live options data via Alpaca, computes:
  - Put/call ratio 6-12 months out (Hayes' sentiment measure)
  - Recommended strike (5% OTM default, ATM when S&P is downtrending)
  - Expiry selection (28-35 DTE)
  - Contracts affordable at ≤5% of portfolio per position

Hayes' rules:
  'Put to call — are more people buying calls or puts? Look 6-12 months out.'
  'No more than 10% OTM. Can write ATM if S&P trend is downward.'
  'Expiry: a month out — 28 to 35 days.'
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

SENTIMENT_BULLISH = "bullish"
SENTIMENT_BEARISH = "bearish"
SENTIMENT_NEUTRAL = "neutral"


@dataclass
class OptionsAnalysis:
    symbol: str
    put_call_ratio: float           # calls / puts by open interest (>1.0 = more calls)
    sentiment: str                  # 'bullish', 'bearish', 'neutral'
    sentiment_score: float          # 0.0 – 1.0 for scoring
    recommended_strike: Optional[float]
    recommended_expiry: Optional[str]       # "YYYY-MM-DD"
    strike_type: Optional[str]             # "OTM" or "ATM"
    otm_percent: Optional[float]            # % out of the money
    estimated_premium: Optional[float]     # per-contract mid price (not × 100)
    contracts_affordable: Optional[int]    # contracts within 5% portfolio cap


class OptionsChainAnalyzer:
    """
    Uses Alpaca options data to compute sentiment and select strikes.
    Falls back gracefully when data is unavailable (e.g. paper account limits).
    """

    def __init__(self, alpaca_client, philosophy: dict):
        self.client = alpaca_client
        self.phil = philosophy

    def analyze(
        self,
        symbol: str,
        current_price: float,
        sp500_trend: str,
        portfolio_value: float,
    ) -> Optional[OptionsAnalysis]:
        """
        Full options analysis for one symbol.
        Returns None if no options data is available.
        """
        try:
            # 1. Put/call sentiment (6-12 months out)
            ratio, sentiment, sentiment_score = self._put_call_ratio(symbol)

            # 2. Strike + expiry selection (28-35 DTE)
            strike_info = self._select_strike(symbol, current_price, sp500_trend)

            if not strike_info:
                # Still return sentiment even without a strike
                return OptionsAnalysis(
                    symbol=symbol,
                    put_call_ratio=ratio,
                    sentiment=sentiment,
                    sentiment_score=sentiment_score,
                    recommended_strike=None,
                    recommended_expiry=None,
                    strike_type=None,
                    otm_percent=None,
                    estimated_premium=None,
                    contracts_affordable=None,
                )

            strike, expiry, strike_type, otm_pct, premium = strike_info
            contracts = self._contracts_affordable(portfolio_value, premium)

            return OptionsAnalysis(
                symbol=symbol,
                put_call_ratio=ratio,
                sentiment=sentiment,
                sentiment_score=sentiment_score,
                recommended_strike=strike,
                recommended_expiry=expiry,
                strike_type=strike_type,
                otm_percent=otm_pct,
                estimated_premium=premium,
                contracts_affordable=contracts,
            )

        except Exception as e:
            logger.error(f"{symbol}: options analysis failed — {e}")
            return None

    # ─────────────────────────────────────────────
    # PUT/CALL RATIO
    # ─────────────────────────────────────────────

    def _put_call_ratio(self, symbol: str) -> tuple[float, str, float]:
        """
        Computes call/put open interest ratio from 6-12 month options.
        Hayes: '2:1 calls to puts = very bullish. Below 1:1 = stay away.'

        Returns (ratio, sentiment_label, sentiment_score_0_to_1)
        """
        phil = self.phil.get("options_sentiment", {})
        bullish_threshold = phil.get("put_call_bullish_threshold", 1.0)
        strong_threshold = phil.get("strong_bullish_threshold", 2.0)

        contracts = self.client.get_options_snapshot_long(
            symbol, dte_min=180, dte_max=365
        )

        if not contracts:
            # No data available — return neutral
            logger.debug(f"{symbol}: no long-dated options data, defaulting to neutral")
            return 1.0, SENTIMENT_NEUTRAL, 0.4

        calls = [c for c in contracts if c.type == "call"]
        puts = [c for c in contracts if c.type == "put"]

        call_oi = sum(c.open_interest or 0 for c in calls)
        put_oi = sum(p.open_interest or 0 for p in puts)

        if put_oi == 0 and call_oi == 0:
            return 1.0, SENTIMENT_NEUTRAL, 0.4

        if put_oi == 0:
            ratio = float("inf")
        else:
            ratio = call_oi / put_oi

        # Classify
        if ratio >= strong_threshold:
            sentiment = SENTIMENT_BULLISH
            # Score: linearly scale 0.7→1.0 between strong_threshold and 3.0
            score = min(1.0, 0.7 + (ratio - strong_threshold) / (3.0 - strong_threshold) * 0.3)
        elif ratio >= bullish_threshold:
            sentiment = SENTIMENT_BULLISH
            # Score: 0.55→0.70 between threshold and strong_threshold
            score = 0.55 + (ratio - bullish_threshold) / (strong_threshold - bullish_threshold) * 0.15
        elif ratio >= 0.7:
            sentiment = SENTIMENT_NEUTRAL
            score = 0.4 + (ratio - 0.7) / 0.3 * 0.15
        else:
            sentiment = SENTIMENT_BEARISH
            score = max(0.0, ratio / 0.7 * 0.4)

        logger.debug(f"{symbol}: P/C ratio {ratio:.2f} calls:{call_oi} puts:{put_oi} → {sentiment}")
        return round(ratio, 2), sentiment, round(score, 3)

    # ─────────────────────────────────────────────
    # STRIKE SELECTION
    # ─────────────────────────────────────────────

    def _select_strike(
        self,
        symbol: str,
        current_price: float,
        sp500_trend: str,
    ) -> Optional[tuple]:
        """
        Selects the best strike and expiry from the near-term chain (28-35 DTE).

        Returns (strike, expiry, strike_type, otm_pct, estimated_premium)
        or None if no suitable options found.
        """
        phil_strike = self.phil.get("strike_selection", {})
        max_otm = phil_strike.get("max_otm_percent", 10.0)
        target_otm = phil_strike.get("default_target_otm_percent", 5.0)
        atm_condition = phil_strike.get("atm_condition", "sp500_monthly_downtrend")
        dte_min = phil_strike.get("expiry_dte_min", 28)
        dte_max = phil_strike.get("expiry_dte_max", 35)

        # When S&P is downtrending, go ATM
        use_atm = sp500_trend == "bearish" and atm_condition == "sp500_monthly_downtrend"
        if use_atm:
            target_otm_pct = 0.0
            strike_type = "ATM"
        else:
            target_otm_pct = target_otm / 100.0
            strike_type = "OTM"

        max_otm_pct = max_otm / 100.0
        target_strike = current_price * (1 + target_otm_pct)
        max_strike = current_price * (1 + max_otm_pct)

        # Fetch the near-term chain
        chain = self.client.get_options_chain(symbol, dte_min=dte_min, dte_max=dte_max)

        if not chain:
            # Fall back to synthetic estimate
            return self._synthetic_strike(
                current_price, target_otm_pct, max_otm_pct,
                dte_min, dte_max, strike_type
            )

        # Filter to valid calls with pricing data
        valid = [
            c for c in chain
            if c.type == "call"
            and c.strike_price <= max_strike
            and c.bid_price is not None
            and c.ask_price is not None
            and c.bid_price > 0
        ]

        if not valid:
            return self._synthetic_strike(
                current_price, target_otm_pct, max_otm_pct,
                dte_min, dte_max, strike_type
            )

        # Find the strike closest to target
        best = min(valid, key=lambda c: abs(c.strike_price - target_strike))
        mid_price = (best.bid_price + best.ask_price) / 2
        actual_otm = (best.strike_price - current_price) / current_price * 100

        return (
            best.strike_price,
            best.expiry,
            strike_type,
            round(actual_otm, 1),
            round(mid_price, 2),
        )

    def _synthetic_strike(
        self,
        current_price: float,
        target_otm_pct: float,
        max_otm_pct: float,
        dte_min: int,
        dte_max: int,
        strike_type: str,
    ) -> Optional[tuple]:
        """
        When no live chain is available, synthesize a reasonable estimate.
        Premium is approximated as ~2-4% of strike for a 30-DTE call.
        """
        strike = round(current_price * (1 + target_otm_pct) / 2.5) * 2.5  # round to nearest 2.50
        otm_pct = (strike - current_price) / current_price * 100
        mid_dte = (dte_min + dte_max) / 2
        expiry = (datetime.now() + timedelta(days=mid_dte)).strftime("%Y-%m-%d")
        # Rough premium estimate: ~2% of stock price for near-ATM 30-DTE call
        premium = round(current_price * 0.02 * (1 - target_otm_pct * 3), 2)
        premium = max(0.10, premium)  # floor

        logger.debug(f"Using synthetic strike estimate: ${strike} ({otm_pct:.1f}% OTM)")
        return (strike, expiry, strike_type, round(otm_pct, 1), premium)

    # ─────────────────────────────────────────────
    # POSITION SIZING
    # ─────────────────────────────────────────────

    def _contracts_affordable(
        self,
        portfolio_value: float,
        premium_per_contract: float,
    ) -> int:
        """
        Calculates how many contracts fit within the 5% position size limit.
        Each contract controls 100 shares.

        Hayes: 'Never more than 5% of portfolio per name.'
        """
        if not premium_per_contract or premium_per_contract <= 0:
            return 1

        max_pct = self.phil.get("risk", {}).get("max_position_size_pct", 5.0) / 100.0
        max_dollars = portfolio_value * max_pct
        cost_per_contract = premium_per_contract * 100  # 1 contract = 100 shares

        contracts = int(max_dollars / cost_per_contract)
        return max(1, contracts)  # Always at least 1 if affordable
