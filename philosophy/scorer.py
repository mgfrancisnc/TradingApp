"""
scorer.py
Hard rules gate and philosophy scoring engine for the Francis-Hayes Trading Bot.

Two components:
  HardRules        — binary pass/fail checks; any failure blocks the trade
  PhilosophyScorer — weighted 0-1 conviction score; must reach ≥60% to approve

Hard rules (all must pass):
  - Monthly trend must be bullish
  - Weekly must confirm monthly
  - Not breaking support
  - Revenue growing or stable (not decelerating >50% YoY)
  - Put/call sentiment not bearish (6-12mo)
  - Portfolio below 20-position cap
  - Stock sufficiently familiar
  - Must own ≥100 shares of underlying before writing a call
  - IV rank must be >30
  - Options liquidity gates must pass
  - No earnings event within the option's expiry window

Scoring weights (v3.0 assignment-targeting covered calls):
  iv_rank:                 0.20
  monthly_trend_strength:  0.20
  options_liquidity:       0.10
  put_call_ratio:          0.10
  volume_trend:            0.10
  beta_fit:                0.10
  premium_to_capital:      0.05
  revenue_cagr_stability:  0.05
  yield_vs_risk_free:      0.05
  weekly_trend:            0.05
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RulesResult:
    passed: bool
    failed_rules: list[str] = field(default_factory=list)


@dataclass
class ScoreResult:
    total_score: float      # 0.0-1.0
    approved: bool          # total_score >= threshold from philosophy.yaml
    component_scores: dict  # {component_name: weighted_contribution}
    raw_scores: dict        # {component_name: 0-1 sub-score before weighting}


class HardRules:
    """
    Binary gate: all rules must pass or the trade is rejected.
    Failed candidates still appear in the report watchlist — just not approved.
    """

    def __init__(self, philosophy: dict):
        self.phil = philosophy

    def check(
        self,
        symbol: str,
        monthly_trend: str,
        weekly_confirms_monthly: bool,
        breaking_support: bool,
        revenue_growth_pct: Optional[float],    # YoY revenue growth as decimal (0.10 = 10%)
        revenue_deceleration_ok: bool,          # False if growth dropped >50% YoY
        put_call_sentiment: str,
        max_positions_reached: bool,
        familiarity_confirmed: bool,
        shares_owned: int,                      # Must be ≥100 to write a covered call
        iv_rank: Optional[float],               # Must be >30
        liquidity_ok: bool,                     # Options chain liquidity gates
        earnings_in_window: bool,               # True if earnings fall within expiry window
    ) -> RulesResult:

        rules = self.phil.get("hard_rules", {})
        failed = []

        # ── Portfolio cap ──────────────────────────────────────
        if max_positions_reached:
            failed.append("portfolio at 20-position limit")

        # ── Monthly trend — the boss ───────────────────────────
        if monthly_trend != "bullish":
            failed.append(f"monthly trend is {monthly_trend} (must be bullish)")

        # ── Weekly confirmation ────────────────────────────────
        if not weekly_confirms_monthly:
            failed.append("weekly trend contradicts monthly")

        # ── Support break ──────────────────────────────────────
        if breaking_support:
            failed.append("price breaking through support")

        # ── Revenue must be positive or stable ─────────────────
        if revenue_growth_pct is not None and revenue_growth_pct <= 0:
            failed.append(f"revenue growth negative ({revenue_growth_pct:.1%})")

        # ── Revenue deceleration — Hayes: 'paid for 20%, got 10%' ─
        if not revenue_deceleration_ok:
            failed.append("revenue decelerating >50% YoY")

        # ── Options sentiment ──────────────────────────────────
        if put_call_sentiment == "bearish":
            failed.append("options sentiment bearish (6-12mo)")

        # ── Familiarity — Hayes: 'if you don't know it, don't touch it' ─
        if not familiarity_confirmed:
            failed.append("stock not sufficiently familiar")

        # ── Must own ≥100 shares before writing a covered call ─
        min_shares = rules.get("min_shares_owned", 100)
        if shares_owned < min_shares:
            failed.append(f"only {shares_owned} shares owned (need ≥{min_shares} to write a call)")

        # ── IV rank gate — only sell premium when IV is elevated ─
        iv_min = rules.get("iv_rank_min", 30)
        if iv_rank is not None and iv_rank <= iv_min:
            failed.append(f"IV rank {iv_rank:.0f} too low (need >{iv_min})")
        # If iv_rank is None, data was unavailable — log but don't block

        # ── Options liquidity gates ────────────────────────────
        if not liquidity_ok:
            failed.append("options chain fails liquidity gates (volume/OI/spread)")

        # ── Earnings blackout ──────────────────────────────────
        if earnings_in_window:
            failed.append("earnings event falls within option expiry window")

        passed = len(failed) == 0
        if not passed:
            logger.debug(f"{symbol}: hard rules failed — {failed}")

        return RulesResult(passed=passed, failed_rules=failed)


class PhilosophyScorer:
    """
    Weighted conviction scorer — runs only after all hard rules pass.
    Weights and threshold are read from philosophy.yaml → scoring section.
    """

    def __init__(self, philosophy: dict):
        self.phil = philosophy
        scoring = philosophy.get("scoring", {})
        weights = scoring.get("weights", {})

        self.threshold = scoring.get("approval_threshold", 0.60)

        self.w_iv_rank    = weights.get("iv_rank", 0.20)
        self.w_monthly    = weights.get("monthly_trend_strength", 0.20)
        self.w_liquidity  = weights.get("options_liquidity", 0.10)
        self.w_pc         = weights.get("put_call_ratio", 0.10)
        self.w_volume     = weights.get("volume_trend", 0.10)
        self.w_beta       = weights.get("beta_fit", 0.10)
        self.w_premium    = weights.get("premium_to_capital_ratio", 0.05)
        self.w_revenue    = weights.get("revenue_cagr_stability", 0.05)
        self.w_yield      = weights.get("yield_vs_risk_free", 0.05)
        self.w_weekly     = weights.get("weekly_trend_confirmation", 0.05)

    def score(
        self,
        symbol: str,
        # Trend
        monthly_trend_strength: float,          # 0-1 from market_data
        weekly_confirms_monthly: bool,
        volume_trend_score: float,              # 0-1 from market_data
        # Options quality
        iv_rank: Optional[float],               # 0-100 from options_chain
        put_call_sentiment_score: float,        # 0-1 from options_chain
        liquidity_ok: bool,                     # from options_chain
        premium_to_stock_pct: Optional[float],  # e.g. 1.5 means 1.5% of stock price
        # Fundamentals
        stock_type: str,                        # 'growth' or 'income'
        revenue_cagr_pct: Optional[float],      # e.g. 0.10 = 10%
        revenue_growth_yoy: Optional[float],
        beta: Optional[float],
        dividend_yield: Optional[float],
        treasury_rate: float,
    ) -> ScoreResult:

        raw = {}

        raw["iv_rank"]     = self._score_iv_rank(iv_rank)
        raw["monthly"]     = monthly_trend_strength
        raw["liquidity"]   = 1.0 if liquidity_ok else 0.0
        raw["put_call"]    = put_call_sentiment_score
        raw["volume"]      = volume_trend_score
        raw["beta"]        = self._score_beta(stock_type, beta)
        raw["premium"]     = self._score_premium(premium_to_stock_pct)
        raw["revenue"]     = self._score_revenue(stock_type, revenue_cagr_pct, revenue_growth_yoy)
        raw["yield"]       = self._score_yield(stock_type, dividend_yield, treasury_rate)
        raw["weekly"]      = 1.0 if weekly_confirms_monthly else 0.0

        weighted = {
            "iv_rank":    raw["iv_rank"]   * self.w_iv_rank,
            "monthly":    raw["monthly"]   * self.w_monthly,
            "liquidity":  raw["liquidity"] * self.w_liquidity,
            "put_call":   raw["put_call"]  * self.w_pc,
            "volume":     raw["volume"]    * self.w_volume,
            "beta":       raw["beta"]      * self.w_beta,
            "premium":    raw["premium"]   * self.w_premium,
            "revenue":    raw["revenue"]   * self.w_revenue,
            "yield":      raw["yield"]     * self.w_yield,
            "weekly":     raw["weekly"]    * self.w_weekly,
        }

        total = round(min(1.0, max(0.0, sum(weighted.values()))), 3)
        approved = total >= self.threshold

        logger.debug(
            f"{symbol}: score {total:.1%} {'✅' if approved else '❌'} "
            f"iv={raw['iv_rank']:.2f} trend={raw['monthly']:.2f} "
            f"liq={raw['liquidity']:.2f} premium={raw['premium']:.2f}"
        )

        return ScoreResult(
            total_score=total,
            approved=approved,
            component_scores=weighted,
            raw_scores=raw,
        )

    # ─────────────────────────────────────────────
    # SUB-SCORERS
    # ─────────────────────────────────────────────

    def _score_iv_rank(self, iv_rank: Optional[float]) -> float:
        """
        Higher IV rank = better premium collected for the risk taken.
        Hard rule already blocks <30, so scoring starts above that.
        """
        if iv_rank is None:
            return 0.0
        if iv_rank >= 80:   return 1.0   # Exceptional premium environment
        if iv_rank >= 60:   return 0.85  # Very good
        if iv_rank >= 45:   return 0.70  # Good
        if iv_rank >= 30:   return 0.50  # Minimum acceptable (just clears hard rule)
        return 0.0

    def _score_premium(self, premium_to_stock_pct: Optional[float]) -> float:
        """
        Premium as a percentage of the stock price — measures yield quality.
        Philosophy: min 0.5% per cycle; 1%+ is excellent for a monthly call.
        """
        if premium_to_stock_pct is None:
            return 0.3
        if premium_to_stock_pct >= 2.0:   return 1.0   # ≥2% of stock value — excellent
        if premium_to_stock_pct >= 1.5:   return 0.85
        if premium_to_stock_pct >= 1.0:   return 0.70
        if premium_to_stock_pct >= 0.5:   return 0.50  # Minimum acceptable
        return 0.1                                      # Below floor — barely worth writing

    def _score_revenue(
        self,
        stock_type: str,
        cagr: Optional[float],
        yoy: Optional[float],
    ) -> float:
        """
        Revenue stability matters more than explosive growth for covered call writing.
        Hayes: 'You can't lie about revenue.'
        """
        if stock_type == "growth":
            if cagr is None:    return 0.3
            if cagr >= 0.20:    return 1.0
            if cagr >= 0.10:    return 0.75
            if cagr >= 0.05:    return 0.50
            if cagr >= 0.0:     return 0.30
            return 0.0
        else:  # income — stability over speed
            if yoy is None:     return 0.5
            if yoy >= 0.10:     return 1.0
            if yoy >= 0.05:     return 0.80
            if yoy >= 0.0:      return 0.60
            return 0.2

    def _score_beta(self, stock_type: str, beta: Optional[float]) -> float:
        """
        For covered call writing, lower beta is generally preferred — less whipsaw.
        Income stocks: 0.3-0.9 is ideal. Growth: 1.0-1.5 is acceptable.
        Hayes: 'Beta tells you volatility relative to the market.'
        """
        if beta is None:
            return 0.5

        if stock_type == "income":
            if 0.3 <= beta <= 0.9:    return 1.0
            if 0.9 < beta <= 1.2:     return 0.70
            if 0.1 <= beta < 0.3:     return 0.60
            if 1.2 < beta <= 1.5:     return 0.40
            return 0.2
        else:  # growth
            if 1.0 <= beta <= 1.5:    return 1.0
            if 0.8 <= beta < 1.0:     return 0.75
            if 1.5 < beta <= 2.0:     return 0.60
            if 0.5 <= beta < 0.8:     return 0.40
            return 0.2

    def _score_yield(
        self,
        stock_type: str,
        dividend_yield: Optional[float],
        treasury_rate: float,
    ) -> float:
        """
        Income stocks: dividend yield must beat the 10-year treasury.
        Hayes: 'All returns are based on the 10-year treasury. You have to get above it.'
        Growth stocks: not applicable — score is neutral.
        """
        if stock_type == "growth":
            return 0.7  # Not the primary metric for growth names

        if dividend_yield is None:
            return 0.0  # Income stock with no dividend is a problem

        spread = dividend_yield - treasury_rate
        if spread >= 0.04:    return 1.0
        if spread >= 0.02:    return 0.85
        if spread >= 0.01:    return 0.70
        if spread >= 0.0:     return 0.50
        if spread >= -0.01:   return 0.25
        return 0.0
