"""
scanner/weekly_scanner.py
The discovery engine for the Francis-Hayes Trading Bot.

Runs every Sunday evening / Monday pre-market.
Scans a universe of stocks, scores them against Hayes' philosophy,
and produces a ranked shortlist report for human review.

'Most buying occurs Friday afternoons and Monday mornings.'
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    symbol: str
    score: float
    approved: bool
    stock_type: str
    monthly_trend: str
    weekly_confirms: bool
    put_call_sentiment: str
    put_call_ratio: float
    volume_trend: str
    recommended_strike: Optional[float]
    recommended_expiry: Optional[str]
    strike_type: Optional[str]
    otm_pct: Optional[float]
    contracts: Optional[int]
    estimated_cost: Optional[float]     # Total cost in USD (premium × 100 × contracts)
    failed_rules: list[str]
    score_breakdown: dict
    company_brief: str                  # One-liner for the report


class WeeklyScanner:
    """
    The discovery engine — finds names Hayes might not be watching.
    Produces a ranked report for human review before any trade is placed.
    """

    def __init__(
        self,
        alpaca_client,
        market_analyzer,
        options_analyzer,
        hard_rules,
        scorer,
        fundamentals_fetcher,
        philosophy: dict,
    ):
        self.client = alpaca_client
        self.market = market_analyzer
        self.options = options_analyzer
        self.rules = hard_rules
        self.scorer = scorer
        self.fundamentals = fundamentals_fetcher
        self.phil = philosophy

    # ─────────────────────────────────────────────
    # MAIN ENTRY POINT
    # ─────────────────────────────────────────────

    def run(self, universe: list[str] = None) -> list[ScanResult]:
        """
        Scans the universe, returns a ranked list of candidates.
        Approved trades (passed rules + score ≥ 60%) appear first.
        """
        if universe is None:
            universe = self._build_universe()

        # Portfolio state
        portfolio_value = self.client.get_portfolio_value()
        open_positions = self.client.get_open_option_positions()
        max_positions = self.phil["risk"]["max_positions"]
        max_reached = len(open_positions) >= max_positions
        open_symbols = {p.symbol for p in open_positions}

        # Market context — fetched once for all symbols
        sp500_trend = self.market.get_sp500_trend()
        treasury_rate = self.fundamentals.get_treasury_rate()

        logger.info(f"Scanning {len(universe)} symbols... S&P trend: {sp500_trend}")
        results = []

        for symbol in universe:
            if symbol in open_symbols:
                logger.debug(f"{symbol}: already in portfolio, skipping")
                continue

            try:
                result = self._scan_symbol(
                    symbol=symbol,
                    sp500_trend=sp500_trend,
                    portfolio_value=portfolio_value,
                    treasury_rate=treasury_rate,
                    max_positions_reached=max_reached,
                )
                if result:
                    results.append(result)

            except Exception as e:
                logger.error(f"Error scanning {symbol}: {e}")

        # Sort: approved first, then by score
        results.sort(key=lambda r: (not r.approved, -r.score))

        self._print_report(results, sp500_trend, portfolio_value, open_positions)
        return results

    # ─────────────────────────────────────────────
    # UNIVERSE BUILDING
    # ─────────────────────────────────────────────

    def _build_universe(self) -> list[str]:
        """
        Uses TradingView screener to discover candidates dynamically.
        Falls back to a curated static list if the screener is unavailable.
        Hayes: 'Advice on stocks we may not be watching.'
        """
        try:
            from scanner.universe import UniverseScreener
            screener = UniverseScreener()
            symbols = screener.get_universe()
            if symbols:
                # Add any volume-surge discovery candidates
                extra = screener.get_discovery_candidates()
                seen = set(symbols)
                for s in extra:
                    if s not in seen:
                        symbols.append(s)
                        seen.add(s)
                return symbols
        except Exception as e:
            logger.warning(f"Universe screener error: {e} — using static list")

        return [
            "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
            "CRM", "NOW", "SNOW", "DDOG", "CRWD", "NET",
            "KO", "JNJ", "PG", "V", "MA", "BRK-B",
        ]

    # ─────────────────────────────────────────────
    # PER-SYMBOL SCAN
    # ─────────────────────────────────────────────

    def _scan_symbol(
        self,
        symbol: str,
        sp500_trend: str,
        portfolio_value: float,
        treasury_rate: float,
        max_positions_reached: bool,
    ) -> Optional[ScanResult]:

        # 1. Trend analysis (monthly + weekly)
        trend = self.market.analyze(symbol)
        if not trend:
            logger.debug(f"{symbol}: no trend data")
            return None

        # 2. Fundamentals
        fund = self.fundamentals.get(symbol)
        if not fund:
            logger.debug(f"{symbol}: no fundamentals data")
            return None

        # 3. Options analysis (put/call ratio + strike selection)
        opts = self.options.analyze(
            symbol=symbol,
            current_price=trend.current_price,
            sp500_trend=sp500_trend,
            portfolio_value=portfolio_value,
        )

        # 4. Hard rules gate
        rules_result = self.rules.check(
            symbol=symbol,
            monthly_trend=trend.monthly_trend,
            weekly_trend=trend.weekly_trend,
            weekly_confirms_monthly=trend.weekly_confirms_monthly,
            breaking_support=trend.breaking_support,
            revenue_growth_pct=fund.revenue_growth_yoy,
            revenue_accelerating=fund.revenue_accelerating,
            put_call_sentiment=opts.sentiment if opts else "neutral",
            max_positions_reached=max_positions_reached,
        )

        # 5. Score (even failures get scored — shows up in watchlist)
        score_result = self.scorer.score(
            symbol=symbol,
            monthly_trend_strength=trend.monthly_trend_strength,
            weekly_confirms_monthly=trend.weekly_confirms_monthly,
            revenue_cagr_pct=fund.revenue_cagr,
            revenue_growth_yoy=fund.revenue_growth_yoy,
            stock_type=fund.stock_type,
            dividend_yield=fund.dividend_yield,
            treasury_rate=treasury_rate,
            beta=fund.beta,
            put_call_sentiment_score=opts.sentiment_score if opts else 0.3,
            volume_trend_score=trend.volume_trend_score,
        )

        # 6. Estimated total cost
        estimated_cost = None
        if opts and opts.estimated_premium and opts.contracts_affordable:
            estimated_cost = opts.estimated_premium * 100 * opts.contracts_affordable

        return ScanResult(
            symbol=symbol,
            score=score_result.total_score,
            approved=rules_result.passed and score_result.approved,
            stock_type=fund.stock_type,
            monthly_trend=trend.monthly_trend,
            weekly_confirms=trend.weekly_confirms_monthly,
            put_call_sentiment=opts.sentiment if opts else "unknown",
            put_call_ratio=opts.put_call_ratio if opts else 0.0,
            volume_trend=trend.volume_trend,
            recommended_strike=opts.recommended_strike if opts else None,
            recommended_expiry=opts.recommended_expiry if opts else None,
            strike_type=opts.strike_type if opts else None,
            otm_pct=opts.otm_percent if opts else None,
            contracts=opts.contracts_affordable if opts else None,
            estimated_cost=estimated_cost,
            failed_rules=rules_result.failed_rules,
            score_breakdown=score_result.component_scores,
            company_brief=fund.brief,
        )

    # ─────────────────────────────────────────────
    # REPORT
    # ─────────────────────────────────────────────

    def _print_report(
        self,
        results: list[ScanResult],
        sp500_trend: str,
        portfolio_value: float,
        open_positions: list,
    ):
        now = datetime.now().strftime("%A %B %d, %Y %I:%M %p")
        approved = [r for r in results if r.approved]
        watchlist = [r for r in results if not r.approved]

        print("\n" + "=" * 65)
        print(f"  FRANCIS-HAYES TRADING BOT — WEEKLY SCAN REPORT")
        print(f"  {now}")
        print("=" * 65)
        print(f"  S&P 500 Trend:    {sp500_trend.upper()}")
        print(f"  Portfolio Value:  ${portfolio_value:,.0f}")
        print(f"  Open Positions:   {len(open_positions)} / 20")
        print(f"  Symbols Scanned:  {len(results)}")
        print(f"  Approved Trades:  {len(approved)}")
        print("=" * 65)

        if approved:
            print(f"\n  ✅ APPROVED FOR REVIEW ({len(approved)} names)")
            print("  " + "-" * 60)
            for r in approved:
                cost_str = f"~${r.estimated_cost:,.0f}" if r.estimated_cost else "unknown cost"
                print(f"\n  {r.symbol} [{r.stock_type.upper()}]  Score: {r.score:.0%}")
                print(f"  {r.company_brief}")
                if r.recommended_strike and r.recommended_expiry:
                    print(
                        f"  Strike: {r.strike_type} ${r.recommended_strike:.2f} "
                        f"({r.otm_pct:.1f}% OTM)  |  Expiry: {r.recommended_expiry}"
                    )
                    print(f"  Contracts: {r.contracts}  |  Est. Cost: {cost_str}")
                print(
                    f"  Put/Call: {r.put_call_ratio:.1f}x ({r.put_call_sentiment})  |  "
                    f"Volume: {r.volume_trend}  |  "
                    f"Monthly: {r.monthly_trend} {'✓' if r.weekly_confirms else '~'}"
                )

        if watchlist:
            print(f"\n\n  👀 WATCHLIST — Not Yet Meeting Conditions ({len(watchlist)} names)")
            print("  " + "-" * 60)
            for r in watchlist[:8]:  # Top 8 near-misses
                print(
                    f"  {r.symbol:6s}  Score: {r.score:.0%}  "
                    f"Failed: {', '.join(r.failed_rules[:2]) or 'score below 60%'}"
                )

        print("\n" + "=" * 65)
        print("  ⚠️  REMINDER: Review each approved name before executing.")
        print("  Hayes: 'If you're not familiar with the company — don't touch it.'")
        print("=" * 65 + "\n")
