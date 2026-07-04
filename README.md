# 🤖 Crypto Signal Bot v3

Regime-gated crypto signal generator with honest, candle-accurate outcome
tracking. Scans Binance futures pairs, publishes signals + chart images to
Telegram, and reports performance in R-multiples (not vanity win rates).

---

## What changed in v3 (full redesign)

**Signal quality**
- Dropped 1m/5m scalping (noise). Intraday = 15m entries, swing = 1h entries.
- Hard-gated strategies instead of additive scoring:
  - `trend_pullback` — buy pullbacks to EMA21 in an established 4h trend
  - `range_fade` — fade BB extremes at S/R in quiet ranges only
  - `squeeze_breakout` — volatility squeeze breaks with 1.8x+ volume
- Choppy regime = **zero signals**. Ever.
- BTC filter: alt longs blocked while BTC downtrends; shock circuit breaker
  pauses everything on a ±1.5%/15m BTC move.
- News guard: signals paused ±45 min around events in `data/events.json`.
- Structure-based stops (behind swing points + ATR buffer), TP3 capped at
  the next major level.

**Honest tracking (this is why v2's "accuracy" was fake)**
- Signals start PENDING; they only become trades if price actually trades
  through the entry zone. Runaways close as NOFILL — not counted as wins.
- Outcomes checked against 1m candle highs/lows — wicks count. If a candle
  touches SL and TP, SL is counted first.
- ALL results posted, wins and losses. Scaled exit model (⅓ per TP,
  SL→BE after TP1) with realized R per trade, MFE/MAE recorded.

**New features**
- Chart image attached to every signal (entry zone, TPs, SL, EMAs)
- `/backtest BTC intraday 30` — same engine as live, run on history
- `/equity` — equity curve image; `/report` — expectancy, PF, max DD
- `/ai SOL` — on-demand Gemini analysis; daily AI market brief at 08:05 UTC
- `/regime`, `/events`, `/addevent` (admin)
- Funding-rate crowding penalty, Fear & Greed bias, ML predictor
  (auto-retrains weekly once 80+ closed trades exist)

---

## Quick start (Oracle Ubuntu)

```bash
cd crypto-signal-bot
git pull
pip install -r requirements.txt
python scripts/migrate_v3.py        # one-time DB migration from v2
sudo systemctl restart crypto-signal-bot
```

### .env

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHANNEL_ID=...
TELEGRAM_ADMIN_ID=...
GEMINI_API_KEY=...          # optional but recommended
ML_PREDICTOR_ENABLED=false  # flip to true after 80+ closed trades
```

### Sanity checks before going live

```bash
PYTHONPATH=. python scripts/debug_scan.py intraday   # dry run, no Telegram
# then in Telegram:
/backtest BTC intraday 30
/backtest ETH swing 60
```

Run backtests on your main pairs first. If expectancy is negative on a
pair, the bot has no edge there — that's the tool telling you the truth.

---

## Telegram commands

`/stats` `/report [days]` `/open` `/best` `/equity` `/backtest SYM [style] [days]`
`/ai SYM` `/regime` `/events` `/addevent YYYY-MM-DD HH:MM | name` `/status`

## Architecture

```
main.py                     scheduler (15m intraday / 1h swing / 60s tracker)
config/settings.py          all tuneables
src/
  data/fetcher.py           bulk tickers, cached OHLCV, funding, history
  data/coin_universe.py     CoinGecko top coins + tradability filter
  analysis/regime.py        coin regime, BTC filter, shock breaker, news guard
  analysis/strategies.py    trend_pullback / range_fade / squeeze_breakout
  analysis/indicators.py    RSI MACD BB EMA ADX ATR Stoch MFI CMF OBV
                            SuperTrend Donchian VWAP patterns percentiles
  analysis/ai_filter.py     Gemini review (async), daily brief, /ai
  analysis/ml_predictor.py  XGBoost win-probability model
  signals/validator.py      structural SL, R:R gate, entry zone
  signals/charting.py       signal charts + equity curve (mplfinance)
  tracking/outcome_tracker.py  1m-candle fill + exit engine
  tracking/performance.py   expectancy, PF, drawdown, per-strategy stats
  backtest/engine.py        historical simulation, /backtest
  delivery/telegram_bot.py  channel delivery + commands
```

⚠️ Signals are informational only — not financial advice. Never risk more
than 1-2% of your account per trade.
