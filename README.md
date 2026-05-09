# Francis-Hayes Trading Bot

Monthly call buying strategy encoded from Hayes' philosophy.
Built on Alpaca Markets API. **Always start in paper mode.**

---

## Setup

### 1. Get Alpaca API Keys
Sign up at https://alpaca.markets (free)
Go to Paper Trading → API Keys → Generate

### 2. Set Environment Variables
```bash
export ALPACA_API_KEY=your_key_here
export ALPACA_SECRET_KEY=your_secret_here
export ALPACA_PAPER=true        # Keep this true until ready for live
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Get a Fundamentals Data API Key
Sign up at https://financialmodelingprep.com (free tier is sufficient)
```bash
export FMP_API_KEY=your_fmp_key_here
```

---

## Usage

```bash
# Sunday evening or Monday morning — scan universe, get report
python bot.py scan

# Run during market hours to watch for stop losses (every 15 min)
python bot.py monitor

# Check current portfolio status
python bot.py status
```

---

## Hayes' Rules — Encoded

| Rule | Value |
|------|-------|
| Primary timeframe | Monthly chart |
| Confirmation timeframe | Weekly chart |
| Ignore | Daily (too noisy) |
| Max OTM | 10% |
| Use ATM when | S&P monthly trend is down |
| Target expiry | 28–35 DTE |
| Stop loss | Exit call if stock drops 10% from entry |
| Max position size | 5% of portfolio |
| Max positions | 20 names |
| Put/call window | 6–12 months out |
| Best buy times | Friday PM, Monday AM |

---

## What's Next to Build

- [ ] `fundamentals.py` — Revenue CAGR, beta, dividend yield from FMP API
- [ ] `execute` command — Submit approved trades after human review  
- [ ] Scheduler — Cron job for Sunday scan + 15-min monitor during hours
- [ ] Telegram/email alerts — Get stop loss notifications on your phone
- [ ] Backtesting harness — Validate rules against historical data

---

## Project Structure

```
trading_bot/
├── bot.py                        # Main entry point
├── requirements.txt
├── config/
│   └── philosophy.yaml           # Hayes' complete philosophy
├── data/
│   ├── market_data.py            # Price, volume, trend analysis
│   └── options_chain.py          # Options chain + strike selection
├── philosophy/
│   └── scorer.py                 # Hard rules + weighted scoring
├── risk/
│   └── exit_monitor.py           # 10% stop loss watcher
├── execution/
│   └── alpaca_client.py          # Alpaca API wrapper
└── scanner/
    └── weekly_scanner.py         # Sunday/Monday scan + report
```
