# Francis-Hayes Trading Bot — Claude Code Context

## What This Project Is
A monthly call options buying bot built on Alpaca Markets API.
Encodes the trading philosophy of Hayes, a professional trader.
Weekly execution cadence — human reviews scan report before any trade fires.

## Philosophy Source of Truth
`config/philosophy.yaml` is the single source of truth for all trading rules.
Never hardcode rule values in Python files — always read from the YAML.

## Hayes' Core Rules (never violate these in code)
- Monthly chart dictates direction — weekly confirms, daily is ignored
- Max 10% OTM on calls / ATM when S&P monthly trend is down
- Target expiry: 28–35 DTE
- Stop loss: exit call if UNDERLYING STOCK drops 10% from entry price
  (watch stock price, NOT option premium)
- Max position size: 5% of portfolio per name
- Max positions: 20 names hard cap
- Put/call sentiment: look 6–12 months out only (never daily)
- Best execution windows: Friday PM and Monday AM

## Project Structure
```
trading_bot/
├── bot.py                     # Main entry point
├── CLAUDE.md                  # This file
├── requirements.txt
├── config/
│   └── philosophy.yaml        # ALL rule values live here
├── data/
│   ├── market_data.py         # Price, volume, trend (monthly/weekly only)
│   └── options_chain.py       # Chain analysis, strike selection, put/call ratio
├── philosophy/
│   └── scorer.py              # HardRules (binary gates) + PhilosophyScorer
├── risk/
│   └── exit_monitor.py        # 10% stop loss watcher on underlying
├── execution/
│   └── alpaca_client.py       # Alpaca API wrapper (paper/live toggle)
└── scanner/
    └── weekly_scanner.py      # Sunday/Monday scan + ranked report
```

## What's Not Built Yet (next priorities)
1. `execution/execute` command in bot.py — submit approved trades after human review
2. Scheduler — cron for Sunday scan + 15-min monitor during market hours
3. Alerts — Telegram or email for stop loss triggers

## Running the Bot
```bash
# Always paper mode first
export ALPACA_PAPER=true

python bot.py scan      # Weekly scan — no trades, report only
python bot.py monitor   # Check stop losses (run every 15 min during hours)
python bot.py status    # Portfolio overview
```

## Key Design Decisions
- All Alpaca interactions go through `execution/alpaca_client.py` only
- Hard rules are binary gates — if any fail, no score is computed
- Scorer produces 0.0–1.0; trades need >= 0.60 to be approved
- Open positions persisted to `data/open_positions.json`
- Paper/live toggle is env var `ALPACA_PAPER=true/false` — never hardcode

## When Adding New Strategies
The monthly call strategy is Strategy #1. Future strategies (spreads, iron
condors, etc.) should be added as new modules under `execution/` with their
own strategy_selector logic. The scanner and scorer are strategy-agnostic.

## Coding Conventions
- Python 3.11+
- Dataclasses for structured return types
- All monetary values as float in USD
- Dates as strings "YYYY-MM-DD" unless doing date math
- Log with `logging` module — never bare `print()` except in report output
- Every public method needs a docstring explaining the Hayes rule it encodes
