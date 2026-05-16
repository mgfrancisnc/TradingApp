# Francis-Hayes Trading Bot — Claude Code Context

## What This Project Is
A covered call writing bot built on Alpaca Markets API (Python).
Encodes Hayes' trading philosophy into systematic rules.
Weekly human-in-the-loop workflow — no trade fires without explicit approval.

## Strategy (v3.0 — Assignment-Targeting Covered Calls)
Sell ATM calls (0.45-0.55 delta) on large-cap stocks we own, 28-35 DTE.
Hold all positions to expiration. Accept assignment as the intended outcome.
No early closes. No rolls. Assignment = success, not failure.

## Single Source of Truth
`config/philosophy.yaml` — all rule values live here. Never hardcode in Python.

## Hard Rules (binary gates — any failure = trade rejected)
1. Monthly trend must be bullish
2. Weekly must confirm monthly
3. Not breaking support
4. Revenue positive or stable (not decelerating >50% YoY)
5. Put/call sentiment not bearish (6-12mo window)
6. Portfolio below 20-position cap
7. Stock must be familiar to the team
8. Must own ≥100 shares before writing a call (`risk/position_check.py`)
9. IV rank > 30 (only sell premium when IV is elevated)
10. Options liquidity gates pass (volume ≥1000, OI ≥500, spread ≤5%)
11. No earnings event within the option's expiry window

## Project Structure
```
trading_bot/
├── bot.py                        # Main entry point
├── CLAUDE.md                     # This file
├── requirements.txt
├── config/
│   └── philosophy.yaml           # ALL rule values — single source of truth
├── data/
│   ├── market_data.py            # Price, volume, trend (monthly/weekly only — daily ignored)
│   ├── options_chain.py          # Delta-based strike selection, IV rank, liquidity gates
│   └── fundamentals.py           # Revenue, beta, dividend (yfinance)
├── philosophy/
│   └── scorer.py                 # HardRules (11 binary gates) + PhilosophyScorer (10 factors)
├── risk/
│   ├── exit_monitor.py           # DTE tracking, ITM alerts, assignment logging, circuit breaker
│   └── position_check.py         # Verify ≥100 shares owned before writing a call
├── execution/
│   ├── alpaca_client.py          # Alpaca API wrapper (paper/live toggle)
│   ├── execute.py                # Sell-to-open submission with human confirmation
│   ├── run_logger.py             # Shared run capture → data/run_logs/*.log + *.meta.json
│   └── scheduler.py              # Automated scan/monitor + PID file + heartbeat
├── scanner/
│   ├── universe.py               # Large-cap screener (≥$10B, NYSE/NASDAQ)
│   └── weekly_scanner.py         # Two jobs: uncovered lots + new candidates
└── web/
    ├── app.py                    # Localhost Flask console (run/execute/history/reports)
    ├── templates/index.html      # Single-page UI
    └── static/                   # Plain CSS + JS (no framework, no build step)
```

## Weekly Workflow
```
Sunday 6pm ET  →  python bot.py scan      # Finds uncovered lots + new candidates
Sunday evening →  Francis + Hayes review the printed report
Monday AM      →  python bot.py execute   # Confirm trades, bot submits sell-to-open
Mon-Fri 30min  →  python bot.py monitor   # DTE tracking, ITM alerts, assignment logging
```

## Running the Bot
```bash
export ALPACA_PAPER=true   # Always paper mode first

python bot.py scan         # Weekly scan — no trades, report only
python bot.py execute      # Review approved trades, confirm, submit
python bot.py monitor      # Check DTE and assignments (run every 30min)
python bot.py schedule     # Run continuous scheduler
python bot.py status       # Portfolio overview
python bot.py web          # Localhost web console — 127.0.0.1:8787
```

## Web Console (`python bot.py web`)
- Localhost only (`127.0.0.1`), no auth — never expose the port (can submit real trades)
- Independent of the scheduler: separate process, communicates via files in `data/`
- Can start/stop/restart the scheduler (spawns it detached; scheduler owns its own
  `data/scheduler.pid` and clears it on graceful SIGTERM, so it survives a web restart)
- Every run (scheduled or manual) is captured to `data/run_logs/` via `run_logger`
- Execute approval happens in the browser via `TradeExecutor.get_pending_trades()` /
  `execute_selected()`; the interactive CLI `execute` path is unchanged

## Data Sources (no paid API keys beyond Alpaca)
- **Alpaca** — price bars, options chain (delta, IV, OI), order execution
- **yfinance** — revenue, beta, dividend yield, 10yr treasury rate
- **tradingview-screener** — large-cap universe discovery

## Key Design Decisions
- All Alpaca interactions go through `execution/alpaca_client.py` only
- Hard rules are binary gates — any failure skips scoring entirely
- Scorer produces 0.0-1.0; threshold is 0.60 (from philosophy.yaml)
- Positions persisted to `data/open_positions.json`
- Approved trades saved to `data/approved_trades.json` between scan and execute
- Paper/live toggle: env var `ALPACA_PAPER=true/false` — never hardcode

## Coding Conventions
- Python 3.11+
- Dataclasses for structured return types
- All monetary values as float in USD
- Dates as strings "YYYY-MM-DD" unless doing date math
- Log with `logging` module — bare `print()` only for report output
- Read all rule values from `philosophy.yaml` — never hardcode thresholds

## When Adding Strategy #2
The covered call strategy is Strategy #1. Future strategies (cash-secured puts,
wheel) should add new scorer logic and philosophy.yaml sections without modifying
Strategy #1 code. The data layer and execution layer are shared.
