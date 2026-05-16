"""
scorer.py
Hard rules gate and philosophy scoring engine for the Francis-Hayes Trading Bot.

Two components:
  HardRules   — binary pass/fail checks (any failure blocks a trade)
  PhilosophyScorer — weighted 0-1 score from philosophy.yaml weights

Hard rules encoded from Hayes' recording:
  - Monthly trend must be bullish (never fight the monthly)
  - Weekly must confirm monthly (not contradict)
  - Do not buy if breaking support
  - Revenue growth must be positive
  - Revenue cannot be decelerating by more than 50% YoY
  - Put/call sentiment cannot be bearish
  - Portfolio cannot exceed 20 positions

Scoring uses the weights in philosophy.yaml:
  monthly_trend_strength: 0.25
  revenue_growth_cagr:    0.20
  put_call_ratio_skew:    0.15
  volume_trend:           0.15
  beta_fit:               0.10
  yield_vs_risk_free:     0.10
  weekly_trend_confirmation: 0.05
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

APPROVAL_THRESHOLD = 0.60   # Must score ≥ 60% to be approved


@dataclass
class RulesResult:
    passed: bool
    failed_rules: list[str] = field(default_factory=list)


@dataclass
class ScoreResult:
    total_score: float          # 0.0 – 1.0
    approved: bool              # total_score >= APPROVAL_THRESHOLD
    component_scores: dict      # {component_name: score}


class HardRules:
    """
    Binary gate: all rules must pass for a trade to be approved.
    A single failure removes the candidate from the approved list
    (it still appears in the watchlist section of the report).
    """

    def __init__(self, philosophy: dict):
        self.phil = philosophy

    def check(
        self,
        symbol: str,
        monthly_trend: str,
        weekly_trend: str,
        weekly_confirms_monthly: bool,
        breaking_support: bool,
        revenue_growth_pct: Optional[float],
        revenue_accelerating: bool,
        put_call_sentiment: str,
        max_positions_reached: bool,
    ) -> RulesResult:

        failed = []

        # ── Portfolio cap ─────────────────────────────────────
        if max_positions_reached:
            failed.append("portfolio at 20-position limit")

        # ── Monthly trend — the boss ──────────────────────────
        if monthly_trend != "bullish":
            failed.append(f"monthly trend {monthly_trend} (need bullish)")

        # ── Weekly confirmation ───────────────────────────────
        if not weekly_confirms_monthly:
            failed.append("weekly contradicts monthly")

        # ── Breaking support ──────────────────────────────────
        if breaking_support:
            failed.append("breaking support")

        # ── Revenue must be growing ───────────────────────────
        if revenue_growth_pct is not None and revenue_growth_pct <= 0:
            failed.append(f"revenue growth negative ({revenue_growth_pct:.1%})")

        # ── Revenue deceleration check ────────────────────────
        # Hayes: 'Paid for 20% growth, got 10% — that's very hard to compound'
        if not revenue_accelerating:
            failed.append("revenue decelerating >50% YoY")

        # ── Options sentiment ─────────────────────────────────
        if put_call_sentiment == "bearish":
            failed.append("options sentiment bearish (6-12mo)")

        passed = len(failed) == 0
        if not passed:
            logger.debug(f"{symbol}: rules failed — {failed}")

        return RulesResult(passed=passed, failed_rules=failed)


class PhilosophyScorer:
    """
    Weighted scoring engine.
    Each component produces a 0-1 sub-score; final score is the weighted sum.
    Weights come from philosophy.yaml → scoring_weights.
    """

    def __init__(self, philosophy: dict):
        self.phil = philosophy
        weights = philosophy.get("scoring_weights", {})

        self.w_monthly = weights.get("monthly_trend_strength", 0.25)
        self.w_revenue = weights.get("revenue_growth_cagr", 0.20)
        self.w_pc = weights.get("put_call_ratio_skew", 0.15)
        self.w_volume = weights.get("volume_trend", 0.15)
        self.w_beta = weights.get("beta_fit", 0.10)
        self.w_yield = weights.get("yield_vs_risk_free", 0.10)
        self.w_weekly = weights.get("weekly_trend_confirmation", 0.05)

    def score(
        self,
        symbol: str,
        monthly_trend_strength: float,          # 0-1 from market_data
        weekly_confirms_monthly: bool,
        revenue_cagr_pct: Optional[float],      # e.g. 0.20 = 20%
        revenue_growth_yoy: Optional[float],
        stock_type: str,                        # 'growth' or 'income'
        dividend_yield: Optional[float],
        treasury_rate: float,
        beta: Optional[float],
        put_call_sentiment_score: float,        # 0-1 from options_chain
        volume_trend_score: float,              # 0-1 from market_data
    ) -> ScoreResult:

        components = {}

        # ── Monthly trend strength (0-1, already normalised) ──
        components["monthly_trend"] = monthly_trend_strength

        # ── Weekly confirmation ───────────────────────────────
        components["weekly_confirms"] = 1.0 if weekly_confirms_monthly else 0.0

        # ── Revenue growth / CAGR ─────────────────────────────
        components["revenue_cagr"] = self._score_revenue(
            stock_type, revenue_cagr_pct, revenue_growth_yoy
        )

        # ── Put/call sentiment ────────────────────────────────
        components["put_call"] = put_call_sentiment_score

        # ── Volume trend ──────────────────────────────────────
        components["volume"] = volume_trend_score

        # ── Beta fit for strategy ────────────────────────────
        components["beta_fit"] = self._score_beta(stock_type, beta)

        # ── Yield vs risk-free (income stocks) ───────────────
        components["yield_vs_treasury"] = self._score_yield(
            stock_type, dividend_yield, treasury_rate
        )

        # ── Weighted total ────────────────────────────────────
        total = (
            components["monthly_trend"]   * self.w_monthly +
            components["revenue_cagr"]    * self.w_revenue +
            components["put_call"]        * self.w_pc +
            components["volume"]          * self.w_volume +
            components["beta_fit"]        * self.w_beta +
            components["yield_vs_treasury"] * self.w_yield +
            components["weekly_confirms"] * self.w_weekly
        )
        total = round(min(1.0, max(0.0, total)), 3)
        approved = total >= APPROVAL_THRESHOLD

        logger.debug(f"{symbol}: score {total:.1%}  {'✅' if approved else '❌'}  {components}")
        return ScoreResult(
            total_score=total,
            approved=approved,
            component_scores=components,
        )

    # ─────────────────────────────────────────────
    # SUB-SCORERS
    # ─────────────────────────────────────────────

    def _score_revenue(
        self,
        stock_type: str,
        cagr: Optional[float],
        yoy: Optional[float],
    ) -> float:
        """
        Growth stocks: CAGR is #1 metric.
        Income stocks: revenue growth matters less but should be positive.
        """
        if stock_type == "growth":
            if cagr is None:
                return 0.3  # Unknown = moderate penalty
            if cagr >= 0.30:    return 1.0   # ≥30% CAGR
            if cagr >= 0.20:    return 0.85  # ≥20% — excellent
            if cagr >= 0.10:    return 0.70  # ≥10% — solid growth
            if cagr >= 0.05:    return 0.50  # ≥5%  — marginal
            if cagr >= 0.0:     return 0.30  # Positive but weak
            return 0.0                        # Negative CAGR

        else:  # income
            # For income stocks, revenue stability matters more than speed
            if yoy is None:
                return 0.5
            if yoy >= 0.10:   return 0.85
            if yoy >= 0.05:   return 0.70
            if yoy >= 0.0:    return 0.55
            return 0.2  # Declining revenue is a red flag for income too

    def _score_beta(self, stock_type: str, beta: Optional[float]) -> float:
        """
        Growth: beta > 1.0 preferred (more leverage to upside).
        Income: beta < 1.0 preferred (stability for covered call overlay).
        Hayes: 'Beta tells you volatility relative to market.'
        """
        if beta is None:
            return 0.5

        if stock_type == "growth":
            # Sweet spot: 1.0–2.0
            if 1.0 <= beta <= 2.0:   return 1.0
            if 0.8 <= beta < 1.0:    return 0.7
            if 2.0 < beta <= 3.0:    return 0.6   # High vol OK but risky
            if 0.5 <= beta < 0.8:    return 0.4
            return 0.2  # Very low or very high beta

        else:  # income
            # Sweet spot: 0.3–0.9
            if 0.3 <= beta <= 0.9:   return 1.0
            if 0.9 < beta <= 1.2:    return 0.7
            if 0.1 <= beta < 0.3:    return 0.6
            if 1.2 < beta <= 1.5:    return 0.4
            return 0.2

    def _score_yield(
        self,
        stock_type: str,
        dividend_yield: Optional[float],
        treasury_rate: float,
    ) -> float:
        """
        Income stocks: yield must beat the 10-year treasury.
        Hayes: 'All returns are based on the 10-year treasury.'
        Growth stocks: dividend yield doesn't matter for this criterion.
        """
        if stock_type == "growth":
            return 0.7  # Not applicable — give neutral-positive score

        if dividend_yield is None:
            return 0.0  # Income stock with no dividend is a problem

        spread = dividend_yield - treasury_rate
        if spread >= 0.04:    return 1.0    # 4%+ above treasury
        if spread >= 0.02:    return 0.85   # 2%+ above
        if spread >= 0.01:    return 0.70   # 1%+ above
        if spread >= 0.0:     return 0.50   # Barely beats treasury
        if spread >= -0.01:   return 0.25   # Slightly below — borderline
        return 0.0                          # Below treasury rate — fail
