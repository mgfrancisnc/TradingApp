"""
scanner/weekly_scanner.py
The weekly scan engine for the Francis-Hayes Trading Bot.

Runs every Sunday evening. Two distinct jobs:

  Job 1 — Uncovered Lots (priority)
    Find stocks we already own with ≥100 shares and no covered call written.
    These are ready to trade — score them and recommend the best strike.

  Job 2 — New Candidates
    Screen the large-cap universe for stocks that pass all filters,
    that we could buy and immediately write a call on (buy-write).
    Requires available capital.

Report is printed for Francis + Hayes to review before Monday execution.
No trades fire without explicit human approval.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    symbol: str
    scan_type: str              # "uncovered_lot" or "new_candidate"
    score: float
    approved: bool
    stock_type: str
    monthly_trend: str
    weekly_confirms: bool
    put_call_sentiment: str
    put_call_ratio: float
    volume_trend: str
    # Strike details
    recommended_strike: Optional[float]
    recommended_expiry: Optional[str]
    delta: Optional[float]
    iv_rank: Optional[float]
    # Position details
    shares_owned: int
    contracts_to_write: Optional[int]
    premium_per_share: Optional[float]
    premium_total: Optional[float]
    current_stock_price: float
    # Report metadata
    failed_rules: list[str]
    score_breakdown: dict
    company_brief: str


class WeeklyScanner:
    """
    Runs the weekly scan and produces a ranked report for human review.
    """

    def __init__(
        self,
        alpaca_client,
        market_analyzer,
        options_analyzer,
        hard_rules,
        scorer,
        fundamentals_fetcher,
        position_checker,
        philosophy: dict,
    ):
        self.client = alpaca_client
        self.market = market_analyzer
        self.options = options_analyzer
        self.rules = hard_rules
        self.scorer = scorer
        self.fundamentals = fundamentals_fetcher
        self.position_checker = position_checker
        self.phil = philosophy

    # ─────────────────────────────────────────────
    # MAIN ENTRY POINT
    # ─────────────────────────────────────────────

    def run(self) -> list[ScanResult]:
        """
        Runs both scan jobs and returns a combined ranked list.
        Approved uncovered lots appear first, then approved new candidates.
        """
        portfolio_value = self.client.get_portfolio_value()
        open_option_positions = self.client.get_open_option_positions()
        max_positions = self.phil["risk"]["max_positions"]
        max_reached = len(open_option_positions) >= max_positions
        treasury_rate = self.fundamentals.get_treasury_rate()

        logger.info(
            f"Weekly scan starting — ${portfolio_value:,.0f} portfolio, "
            f"{len(open_option_positions)}/{max_positions} positions open"
        )

        # Job 1: uncovered lots (stocks we own with no call written)
        uncovered_results = self._scan_uncovered_lots(treasury_rate, max_reached)

        # Job 2: new candidates from the large-cap universe
        already_scanning = {r.symbol for r in uncovered_results}
        new_results = self._scan_new_candidates(
            treasury_rate, max_reached, exclude=already_scanning
        )

        all_results = uncovered_results + new_results
        all_results.sort(key=lambda r: (
            not r.approved,
            r.scan_type != "uncovered_lot",  # uncovered lots first within approved
            -r.score,
        ))

        self._print_report(all_results, portfolio_value, open_option_positions)
        return all_results

    # ─────────────────────────────────────────────
    # JOB 1 — UNCOVERED LOTS
    # ─────────────────────────────────────────────

    def _scan_uncovered_lots(
        self,
        treasury_rate: float,
        max_reached: bool,
    ) -> list[ScanResult]:
        """
        Finds owned stock positions with no covered call written on them.
        These are the highest-priority trades — no capital outlay required.
        """
        uncovered = self.position_checker.get_uncovered_lots()
        if not uncovered:
            logger.info("No uncovered lots found")
            return []

        logger.info(f"Scanning {len(uncovered)} uncovered lot(s)...")
        results = []

        for pos_status in uncovered:
            try:
                result = self._scan_symbol(
                    symbol=pos_status.symbol,
                    scan_type="uncovered_lot",
                    shares_owned=pos_status.shares_owned,
                    treasury_rate=treasury_rate,
                    max_reached=max_reached,
                )
                if result:
                    results.append(result)
            except Exception as e:
                logger.error(f"Error scanning uncovered lot {pos_status.symbol}: {e}")

        return results

    # ─────────────────────────────────────────────
    # JOB 2 — NEW CANDIDATES
    # ─────────────────────────────────────────────

    def _scan_new_candidates(
        self,
        treasury_rate: float,
        max_reached: bool,
        exclude: set,
    ) -> list[ScanResult]:
        """
        Screens the large-cap universe for new buy-write opportunities.
        Skips symbols already covered in Job 1 or already in portfolio.
        """
        universe = self._build_universe()
        candidates = [s for s in universe if s not in exclude]

        logger.info(f"Scanning {len(candidates)} new candidate(s) from universe...")
        results = []

        for symbol in candidates:
            try:
                result = self._scan_symbol(
                    symbol=symbol,
                    scan_type="new_candidate",
                    shares_owned=0,     # Don't own yet — buy-write
                    treasury_rate=treasury_rate,
                    max_reached=max_reached,
                )
                if result:
                    results.append(result)
            except Exception as e:
                logger.error(f"Error scanning candidate {symbol}: {e}")

        return results

    # ─────────────────────────────────────────────
    # PER-SYMBOL ANALYSIS
    # ─────────────────────────────────────────────

    def _scan_symbol(
        self,
        symbol: str,
        scan_type: str,
        shares_owned: int,
        treasury_rate: float,
        max_reached: bool,
    ) -> Optional[ScanResult]:

        # 1. Trend analysis (monthly + weekly only — daily ignored)
        trend = self.market.analyze(symbol)
        if not trend:
            logger.debug(f"{symbol}: no trend data")
            return None

        # 2. Fundamentals
        fund = self.fundamentals.get(symbol)
        if not fund:
            logger.debug(f"{symbol}: no fundamentals data")
            return None

        # 3. Options analysis — delta-based strike, IV rank, liquidity
        opts = self.options.analyze(
            symbol=symbol,
            current_price=trend.current_price,
            shares_owned=shares_owned,
        )

        iv_rank = opts.iv_rank if opts else None
        liquidity_ok = opts.liquidity_ok if opts else False
        premium_pct = opts.premium_to_stock_pct if opts else None

        # 4. Hard rules gate
        # For new candidates, skip the shares_owned rule (they'd buy shares first)
        effective_shares = shares_owned if scan_type == "uncovered_lot" else 100
        rules_result = self.rules.check(
            symbol=symbol,
            monthly_trend=trend.monthly_trend,
            weekly_confirms_monthly=trend.weekly_confirms_monthly,
            breaking_support=trend.breaking_support,
            revenue_growth_pct=fund.revenue_growth_yoy,
            revenue_deceleration_ok=fund.revenue_accelerating,
            put_call_sentiment=opts.sentiment if opts else "neutral",
            max_positions_reached=max_reached,
            familiarity_confirmed=fund.familiarity_confirmed,
            shares_owned=effective_shares,
            iv_rank=iv_rank,
            liquidity_ok=liquidity_ok,
            earnings_in_window=fund.earnings_in_window,
        )

        # 5. Conviction score
        score_result = self.scorer.score(
            symbol=symbol,
            monthly_trend_strength=trend.monthly_trend_strength,
            weekly_confirms_monthly=trend.weekly_confirms_monthly,
            volume_trend_score=trend.volume_trend_score,
            iv_rank=iv_rank,
            put_call_sentiment_score=opts.sentiment_score if opts else 0.3,
            liquidity_ok=liquidity_ok,
            premium_to_stock_pct=premium_pct,
            stock_type=fund.stock_type,
            revenue_cagr_pct=fund.revenue_cagr,
            revenue_growth_yoy=fund.revenue_growth_yoy,
            beta=fund.beta,
            dividend_yield=fund.dividend_yield,
            treasury_rate=treasury_rate,
        )

        # 6. Premium income
        contracts_to_write = opts.contracts_to_write if opts else None
        premium_per_share = opts.estimated_premium if opts else None
        premium_total = (
            premium_per_share * 100 * contracts_to_write
            if premium_per_share and contracts_to_write
            else None
        )

        return ScanResult(
            symbol=symbol,
            scan_type=scan_type,
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
            delta=opts.delta if opts else None,
            iv_rank=iv_rank,
            shares_owned=shares_owned,
            contracts_to_write=contracts_to_write,
            premium_per_share=premium_per_share,
            premium_total=premium_total,
            current_stock_price=trend.current_price,
            failed_rules=rules_result.failed_rules,
            score_breakdown=score_result.component_scores,
            company_brief=fund.brief,
        )

    # ─────────────────────────────────────────────
    # UNIVERSE
    # ─────────────────────────────────────────────

    def _build_universe(self) -> list[str]:
        try:
            from scanner.universe import UniverseScreener
            return UniverseScreener().get_universe()
        except Exception as e:
            logger.warning(f"Universe screener error: {e} — using static fallback")
            return [
                "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META",
                "JPM", "BAC", "JNJ", "UNH", "PG", "KO",
                "V", "MA", "XOM", "CVX", "BRK-B",
                "AVGO", "ORCL", "CRM",
            ]

    # ─────────────────────────────────────────────
    # REPORT
    # ─────────────────────────────────────────────

    def _print_report(
        self,
        results: list[ScanResult],
        portfolio_value: float,
        open_positions: list,
    ):
        now = datetime.now().strftime("%A %B %d, %Y %I:%M %p")
        approved_lots = [r for r in results if r.approved and r.scan_type == "uncovered_lot"]
        approved_new = [r for r in results if r.approved and r.scan_type == "new_candidate"]
        watchlist = [r for r in results if not r.approved]
        total_income = sum(r.premium_total or 0 for r in results if r.approved)

        print("\n" + "=" * 65)
        print(f"  FRANCIS-HAYES TRADING BOT — WEEKLY SCAN REPORT")
        print(f"  {now}")
        print("=" * 65)
        print(f"  Portfolio Value:   ${portfolio_value:,.0f}")
        print(f"  Open Positions:    {len(open_positions)} / {self.phil['risk']['max_positions']}")
        print(f"  Uncovered Lots:    {len(approved_lots)} approved")
        print(f"  New Candidates:    {len(approved_new)} approved")
        print(f"  Est. Total Income: ${total_income:,.0f} (if all approved trades execute)")
        print("=" * 65)

        # Uncovered lots — highest priority
        if approved_lots:
            print(f"\n  UNCOVERED LOTS — READY TO WRITE ({len(approved_lots)} positions)")
            print("  We own these shares. Just need to write the call.")
            print("  " + "-" * 60)
            for r in approved_lots:
                self._print_approved(r)

        # New candidates — buy-write
        if approved_new:
            print(f"\n\n  NEW CANDIDATES — BUY-WRITE ({len(approved_new)} names)")
            print("  Would require buying 100 shares + writing a call.")
            print("  " + "-" * 60)
            for r in approved_new:
                self._print_approved(r)

        # Watchlist
        if watchlist:
            print(f"\n\n  WATCHLIST — Not Yet Qualifying ({len(watchlist)} names)")
            print("  " + "-" * 60)
            for r in watchlist[:10]:
                rules_str = ", ".join(r.failed_rules[:2]) or "score below 60%"
                print(
                    f"  {r.symbol:6s}  {r.score:.0%}  "
                    f"IV rank: {r.iv_rank:.0f if r.iv_rank else 'N/A'}  "
                    f"Failed: {rules_str}"
                )

        print("\n" + "=" * 65)
        print("  REMINDER: Review each name before executing.")
        print("  Hayes: 'If you're not familiar with the company — don't touch it.'")
        print("  Run: python bot.py execute")
        print("=" * 65 + "\n")

    def _print_approved(self, r: ScanResult):
        premium_str = f"${r.premium_total:,.0f}" if r.premium_total else "unknown"
        per_share_str = f"${r.premium_per_share:.2f}/share" if r.premium_per_share else ""
        iv_str = f"IV rank: {r.iv_rank:.0f}" if r.iv_rank else "IV rank: N/A"

        print(f"\n  {r.symbol} [{r.stock_type.upper()}]  Score: {r.score:.0%}  {iv_str}")
        print(f"  {r.company_brief}")
        if r.recommended_strike and r.recommended_expiry:
            print(
                f"  Strike: ${r.recommended_strike:.2f}  (delta {r.delta:.2f if r.delta else '~0.50'})  "
                f"Expiry: {r.recommended_expiry}"
            )
        print(
            f"  Contracts: {r.contracts_to_write or '?'}  |  "
            f"Income: {per_share_str}  =  {premium_str}"
        )
        print(
            f"  Put/Call: {r.put_call_ratio:.1f}x ({r.put_call_sentiment})  |  "
            f"Volume: {r.volume_trend}  |  "
            f"Monthly: {r.monthly_trend} {'confirmed' if r.weekly_confirms else 'unconfirmed'}"
        )
