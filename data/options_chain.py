"""
options_chain.py
Options chain analysis for the Francis-Hayes Trading Bot.

Pulls live options data via Alpaca, computes:
  - ATM strike selection via delta (target 0.50, range 0.45-0.55)
  - IV rank (current IV vs 52-week range) — must be >30 to sell premium
  - Liquidity gate checks (volume, OI, bid-ask spread)
  - Put/call sentiment ratio (6-12 months out)
  - Contracts to write = shares_owned // 100 (100% coverage rule)

Hayes' rules encoded here:
  'Put to call — look 6-12 months out. Daily means nothing.'
  'Expiry: a month out — 28 to 35 days.'
  Assignment-targeting strategy: we sell ATM (0.50 delta), hold to expiration.
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
    # Strike selection
    recommended_strike: Optional[float]
    recommended_expiry: Optional[str]   # "YYYY-MM-DD"
    delta: Optional[float]              # Delta of selected strike (target ~0.50)
    estimated_premium: Optional[float]  # Per-share mid price (multiply by 100 for contract value)
    contracts_to_write: Optional[int]   # shares_owned // 100
    # IV rank
    iv_rank: Optional[float]            # 0-100; must be >30 per hard rules
    iv_rank_passes: Optional[bool]
    # Liquidity gates
    liquidity_ok: Optional[bool]
    daily_volume: Optional[int]
    open_interest: Optional[int]
    bid_ask_spread_pct: Optional[float]
    # Sentiment
    put_call_ratio: float               # calls / puts OI (>1.0 = more calls)
    sentiment: str
    sentiment_score: float              # 0.0-1.0 for scoring
    # Premium quality
    premium_to_stock_pct: Optional[float]  # Premium as % of stock price
    premium_passes_min: Optional[bool]     # Must be ≥0.5% per philosophy


class OptionsChainAnalyzer:
    """
    Uses Alpaca options data to select ATM strikes and validate trade quality.
    Falls back gracefully when live data is unavailable (e.g. paper account limits).
    """

    def __init__(self, alpaca_client, philosophy: dict):
        self.client = alpaca_client
        self.phil = philosophy

    def analyze(
        self,
        symbol: str,
        current_price: float,
        shares_owned: int,
    ) -> Optional[OptionsAnalysis]:
        """
        Full options analysis for one symbol.
        shares_owned determines how many contracts can be written (100% coverage rule).
        Returns None if no options data is available.
        """
        try:
            ratio, sentiment, sentiment_score = self._put_call_ratio(symbol)
            strike_info = self._select_strike(symbol, current_price, shares_owned)

            if not strike_info:
                return OptionsAnalysis(
                    symbol=symbol,
                    recommended_strike=None,
                    recommended_expiry=None,
                    delta=None,
                    estimated_premium=None,
                    contracts_to_write=None,
                    iv_rank=None,
                    iv_rank_passes=None,
                    liquidity_ok=None,
                    daily_volume=None,
                    open_interest=None,
                    bid_ask_spread_pct=None,
                    put_call_ratio=ratio,
                    sentiment=sentiment,
                    sentiment_score=sentiment_score,
                    premium_to_stock_pct=None,
                    premium_passes_min=None,
                )

            iv_rank = self._iv_rank(symbol)
            iv_min = self.phil.get("hard_rules", {}).get("iv_rank_min", 30)
            iv_rank_passes = iv_rank is not None and iv_rank > iv_min

            (
                strike, expiry, delta, premium,
                daily_vol, oi, spread_pct, liquidity_ok
            ) = strike_info

            contracts_to_write = shares_owned // 100

            min_premium_pct = (
                self.phil.get("covered_call", {}).get("min_premium_pct_of_stock", 0.5)
            )
            premium_to_stock_pct = (premium / current_price * 100) if current_price else None
            premium_passes_min = (
                premium_to_stock_pct is not None
                and premium_to_stock_pct >= min_premium_pct
            )

            return OptionsAnalysis(
                symbol=symbol,
                recommended_strike=strike,
                recommended_expiry=expiry,
                delta=delta,
                estimated_premium=premium,
                contracts_to_write=contracts_to_write,
                iv_rank=iv_rank,
                iv_rank_passes=iv_rank_passes,
                liquidity_ok=liquidity_ok,
                daily_volume=daily_vol,
                open_interest=oi,
                bid_ask_spread_pct=spread_pct,
                put_call_ratio=ratio,
                sentiment=sentiment,
                sentiment_score=sentiment_score,
                premium_to_stock_pct=round(premium_to_stock_pct, 3) if premium_to_stock_pct else None,
                premium_passes_min=premium_passes_min,
            )

        except Exception as e:
            logger.error(f"{symbol}: options analysis failed — {e}")
            return None

    # ─────────────────────────────────────────────
    # STRIKE SELECTION — DELTA BASED
    # ─────────────────────────────────────────────

    def _select_strike(
        self,
        symbol: str,
        current_price: float,
        shares_owned: int,
    ) -> Optional[tuple]:
        """
        Finds the call closest to 0.50 delta in the 28-35 DTE window.
        Validates liquidity gates before returning.

        Returns (strike, expiry, delta, premium, daily_vol, oi, spread_pct, liquidity_ok)
        or None if no chain data available.
        """
        cc = self.phil.get("covered_call", {})
        delta_target = cc.get("delta_target", 0.50)
        delta_min = cc.get("delta_min", 0.45)
        delta_max = cc.get("delta_max", 0.55)
        dte_min = cc.get("expiry_dte_min", 28)
        dte_max = cc.get("expiry_dte_max", 35)

        chain = self.client.get_options_chain(symbol, dte_min=dte_min, dte_max=dte_max)

        if not chain:
            return self._synthetic_strike(current_price, delta_target, dte_min, dte_max)

        # Filter to calls with delta data and valid pricing
        calls = [
            c for c in chain
            if c.type == "call"
            and c.delta is not None
            and delta_min <= c.delta <= delta_max
            and c.bid_price is not None
            and c.ask_price is not None
            and c.bid_price > 0
        ]

        if not calls:
            # Widen to any call with delta data and pick closest to target
            calls = [
                c for c in chain
                if c.type == "call"
                and c.delta is not None
                and c.bid_price is not None
                and c.ask_price is not None
                and c.bid_price > 0
            ]
            if not calls:
                return self._synthetic_strike(current_price, delta_target, dte_min, dte_max)

        best = min(calls, key=lambda c: abs(c.delta - delta_target))
        mid_price = (best.bid_price + best.ask_price) / 2
        spread_pct = (
            (best.ask_price - best.bid_price) / mid_price * 100
            if mid_price > 0 else 999.0
        )

        daily_vol = getattr(best, "volume", None) or 0
        oi = getattr(best, "open_interest", None) or 0
        liquidity_ok = self._check_liquidity(daily_vol, oi, spread_pct)

        logger.debug(
            f"{symbol}: selected strike ${best.strike_price} "
            f"delta={best.delta:.2f} premium=${mid_price:.2f} "
            f"vol={daily_vol} oi={oi} spread={spread_pct:.1f}%"
        )

        return (
            best.strike_price,
            best.expiry,
            round(best.delta, 3),
            round(mid_price, 2),
            daily_vol,
            oi,
            round(spread_pct, 1),
            liquidity_ok,
        )

    def _check_liquidity(self, daily_vol: int, oi: int, spread_pct: float) -> bool:
        """
        Validates options liquidity gates from philosophy.yaml.
        All three must pass for the trade to proceed.
        """
        u = self.phil.get("universe", {}).get("options_liquidity", {})
        vol_min = u.get("daily_contract_volume_min", 1000)
        oi_min = u.get("open_interest_min", 500)
        spread_max = u.get("bid_ask_spread_max_pct", 5.0)

        vol_ok = daily_vol >= vol_min
        oi_ok = oi >= oi_min
        spread_ok = spread_pct <= spread_max

        if not (vol_ok and oi_ok and spread_ok):
            logger.debug(
                f"Liquidity gate failed: vol={daily_vol}(min {vol_min}) "
                f"oi={oi}(min {oi_min}) spread={spread_pct:.1f}%(max {spread_max})"
            )

        return vol_ok and oi_ok and spread_ok

    def _synthetic_strike(
        self,
        current_price: float,
        delta_target: float,
        dte_min: int,
        dte_max: int,
    ) -> Optional[tuple]:
        """
        When no live chain is available, synthesizes a reasonable ATM estimate.
        Premium approximated as ~2% of stock price for a 30-DTE ATM call.
        Liquidity cannot be verified — gates marked as failed.
        """
        strike = round(current_price / 2.5) * 2.5  # round to nearest $2.50
        mid_dte = (dte_min + dte_max) / 2
        expiry = (datetime.now() + timedelta(days=mid_dte)).strftime("%Y-%m-%d")
        premium = round(current_price * 0.02, 2)
        premium = max(0.10, premium)

        logger.debug(
            f"Using synthetic ATM estimate: ${strike} premium=${premium} "
            f"(no live chain — liquidity unverified)"
        )
        # daily_vol=0, oi=0, spread_pct=999 → liquidity_ok=False
        return (strike, expiry, delta_target, premium, 0, 0, 999.0, False)

    # ─────────────────────────────────────────────
    # IV RANK
    # ─────────────────────────────────────────────

    def _iv_rank(self, symbol: str) -> Optional[float]:
        """
        Computes IV rank: where current IV sits within its 52-week range.
        IV rank = (current_IV - 52wk_low) / (52wk_high - 52wk_low) * 100

        Hard rule: must be >30 to sell premium. Low IV rank means we are
        collecting below-average premium for the risk taken.
        """
        try:
            iv_data = self.client.get_iv_history(symbol, days=365)
            if not iv_data or len(iv_data) < 20:
                logger.debug(f"{symbol}: insufficient IV history, skipping IV rank")
                return None

            current_iv = iv_data[-1]
            iv_low = min(iv_data)
            iv_high = max(iv_data)

            if iv_high == iv_low:
                return 50.0  # flat IV history — treat as middle of range

            rank = (current_iv - iv_low) / (iv_high - iv_low) * 100
            logger.debug(f"{symbol}: IV rank {rank:.1f} (current={current_iv:.3f} lo={iv_low:.3f} hi={iv_high:.3f})")
            return round(rank, 1)

        except Exception as e:
            logger.debug(f"{symbol}: IV rank calculation failed — {e}")
            return None

    # ─────────────────────────────────────────────
    # PUT/CALL SENTIMENT
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
            logger.debug(f"{symbol}: no long-dated options data, defaulting to neutral")
            return 1.0, SENTIMENT_NEUTRAL, 0.4

        calls = [c for c in contracts if c.type == "call"]
        puts = [c for c in contracts if c.type == "put"]

        call_oi = sum(c.open_interest or 0 for c in calls)
        put_oi = sum(p.open_interest or 0 for p in puts)

        if put_oi == 0 and call_oi == 0:
            return 1.0, SENTIMENT_NEUTRAL, 0.4

        ratio = float("inf") if put_oi == 0 else call_oi / put_oi

        if ratio >= strong_threshold:
            sentiment = SENTIMENT_BULLISH
            score = min(1.0, 0.7 + (ratio - strong_threshold) / (3.0 - strong_threshold) * 0.3)
        elif ratio >= bullish_threshold:
            sentiment = SENTIMENT_BULLISH
            score = 0.55 + (ratio - bullish_threshold) / (strong_threshold - bullish_threshold) * 0.15
        elif ratio >= 0.7:
            sentiment = SENTIMENT_NEUTRAL
            score = 0.4 + (ratio - 0.7) / 0.3 * 0.15
        else:
            sentiment = SENTIMENT_BEARISH
            score = max(0.0, ratio / 0.7 * 0.4)

        logger.debug(f"{symbol}: P/C ratio {ratio:.2f} calls:{call_oi} puts:{put_oi} → {sentiment}")
        return round(ratio, 2), sentiment, round(score, 3)
