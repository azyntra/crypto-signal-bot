# 🤖 Crypto Signal Bot

Multi-exchange crypto signal generator for Binance, Bybit, OKX, and KuCoin.
Scans top-100 coins by market cap, applies technical analysis, and pushes
high-confidence trade signals to Telegram — both spot and futures markets.

---

## Features

- **Multi-exchange**: Binance, Bybit, OKX, KuCoin (spot + futures)
- **Top-100 coins**: Auto-refreshed from CoinGecko every 30 minutes
- **Two strategies**: Scalping (1m/5m/15m) + Swing trading (1h/4h/1d)
- **Indicators**: RSI, MACD, EMA 9/21/50/200, Bollinger Bands, ADX, ATR, Stochastic, OBV
- **Smart validation**: Minimum confidence 70%, minimum R:R 1:2, 3+ indicators must agree
- **ATR-based SL/TP**: Dynamic stop loss + three take-profit targets
- **Deduplication**: Won't resend the same signal within 30–240 min
- **Rate limiting**: Max 10 signals/hour to keep channel clean
- **Signal logging**: SQLite DB with win/loss tracking
- **Telegram commands**: `/stats` `/status` `/start`
- **systemd ready**: Auto-restart on crash, survives reboots

---

## Quick Start

### 1. Clone and deploy on Oracle Ubuntu Server

```bash
git clone <your-repo-url> crypto-signal-bot
cd crypto-signal-bot
bash scripts/deploy.sh
```

The deploy script stops after creating `.env` the first time — fill in your keys, then run it again.

### 2. Configure `.env`

```bash
nano .env
```

Fill in:

| Variable | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Already set — `8303795974:AAH6T4s1wgKAEWoUXNzY1vAr_AIVSH-XFCc` |
| `TELEGRAM_CHANNEL_ID` | Create a Telegram channel → Add bot as admin → Get ID via @userinfobot |
| `TELEGRAM_ADMIN_ID` | Your personal Telegram user ID (via @userinfobot) |
| `BINANCE_API_KEY/SECRET` | binance.com → API Management (read-only is enough for signals) |
| `BYBIT_API_KEY/SECRET` | bybit.com → API (read-only) |
| `OKX_API_KEY/SECRET/PASSPHRASE` | okx.com → API (read-only) |
| `KUCOIN_API_KEY/SECRET/PASSPHRASE` | kucoin.com → API (read-only) |

> **Note**: For Phase 1 (signals only), API keys are optional — public endpoints are used for OHLCV data.
> Keys become required in Phase 2 for auto-execution.

### 3. Start the bot

```bash
sudo systemctl start crypto-signal-bot
sudo journalctl -u crypto-signal-bot -f
```

---

## Telegram Channel Setup

1. Open Telegram → Create a new Channel
2. Go to Channel Settings → Administrators → Add your bot as admin
3. Send a message in the channel
4. Visit: `https://api.telegram.org/bot<TOKEN>/getUpdates`
5. Find `"chat":{"id":-100XXXXXXXXXX}` — that's your `TELEGRAM_CHANNEL_ID`

---

## Signal Format Example

```
────────────────────────────────
🚨 SIGNAL ALERT — LONG 🟢
────────────────────────────────

📊 BTC/USDT  ·  BYBIT  ·  FUTURES
📈 SWING  |  ⏱ Timeframe: 4h

🟢 ENTRY ZONE
   $67,400 – $67,600

🎯 TAKE PROFITS
   TP1 → $68,500  (+1.6%)
   TP2 → $69,800  (+3.5%)
   TP3 → $71,200  (+5.6%)

🛡 STOP LOSS
   $66,100  (-2.0%)

⚖️ Risk:Reward → 1 : 2.8
💰 Leverage: 5–10x

📊 CONFIDENCE: ████████░░ 82%

🔍 SETUP
   ✅ EMA 9>21>50 bull alignment
   ✅ MACD bullish crossover
   ✅ ADX trending bull (28)
   ✅ OBV rising — accumulation
   📌 RSI: 52.4  ·  ADX: 28  ·  Vol ×1.8

🕐 Valid for: 4–48 hours

#BTC #LONG #SWING #BYBIT #FUTURES
────────────────────────────────
```

---

## Configuration Tuning (`config/settings.py`)

| Setting | Default | Description |
|---|---|---|
| `MIN_CONFIDENCE` | 70 | Minimum signal confidence % |
| `MIN_RR_RATIO` | 2.0 | Minimum risk:reward ratio |
| `MIN_INDICATORS_AGREE` | 3 | Minimum indicators that must agree |
| `ATR_SL_MULTIPLIER` | 1.5 | Stop loss = ATR × this |
| `TOP_N_COINS` | 100 | How many top coins to scan |
| `MIN_VOLUME_USDT` | 1,000,000 | Minimum 24h volume filter |
| `MAX_SIGNALS_PER_HOUR` | 10 | Rate limit per hour |
| `SCALP_SCAN_INTERVAL_MIN` | 5 | Scalp scan every N minutes |
| `SWING_SCAN_INTERVAL_MIN` | 60 | Swing scan every N minutes |

---

## Phase 2: Auto-Execution (coming next)

When you're ready to add auto-trading:
- Add exchange API keys with trade permissions
- Enable `AUTO_EXECUTE=true` in `.env`
- The bot will place orders directly via ccxt, track positions, and close at TP/SL

---

## Useful Commands

```bash
# Service management
sudo systemctl start/stop/restart crypto-signal-bot
sudo journalctl -u crypto-signal-bot -f

# View logs
tail -f logs/bot.log

# Manual test run (no scheduler, runs one scan immediately)
source venv/bin/activate
python -c "import asyncio; from src.scanner import run_swing_scan; asyncio.run(run_swing_scan())"
```

---

## Disclaimer

This bot is a technical analysis tool, not financial advice.
Crypto markets are highly volatile. Always use proper risk management.
Never trade more than you can afford to lose.
