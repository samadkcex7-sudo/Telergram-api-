# Hyperliquid Whale Alert Bot

A small Python service that watches **configured Hyperliquid wallet addresses** for newly opened perpetual positions and sends qualifying alerts to Telegram.

## Important scope

Hyperdash's Terms prohibit automated monitoring, crawlers, bots, scripts, and data extraction. This project therefore does **not** scrape or automate Hyperdash. It uses Hyperliquid's official public API/WebSocket and includes Hyperdash only as a manual review link.

Hyperliquid's public API exposes user streams for a specified address, not a global all-traders whale stream. Set `HL_WHALE_ADDRESSES` to the wallets you want to monitor. Global discovery would require a separate compliant indexed data provider.

## Whale rules

- BTC: `$5,000,000` fixed notional threshold
- ETH: `$2,000,000`
- SOL: `$750,000`
- XRP: `$500,000`
- Other assets: `1%` of Hyperliquid's reported 24-hour notional volume

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set values in the environment (or export them in the shell):

```bash
export TELEGRAM_BOT_TOKEN='token-from-BotFather'
export TELEGRAM_CHAT_ID='your-numeric-chat-id'
export HL_WHALE_ADDRESSES='0xabc...,0xdef...'
export DRY_RUN=false
python whale_alert_bot.py
```

The bot token must never be committed or pasted into source control. The Telegram bot must first receive `/start` from the target chat. Use a numeric `chat.id` for `TELEGRAM_CHAT_ID`.

## Telegram inline buttons

Each alert includes:

- **Open `<asset>` chart** — opens the asset-specific Hyperliquid trading chart.
- **Open Hyperdash (manual)** — opens Hyperdash for manual review only; the bot does not scrape or automate it.

The chart URL can be changed with `HL_CHART_URL_TEMPLATE`; use `{coin}` as the placeholder:

```bash
export HL_CHART_URL_TEMPLATE='https://app.hyperliquid.xyz/trade/{coin}'
```

## Behavior

- Subscribes to Hyperliquid `userFills` for each configured wallet.
- Ignores the initial snapshot to avoid historical alert spam.
- Alerts only fills whose direction begins with `Open Long` or `Open Short`.
- Deduplicates by wallet and fill ID for the current process lifetime.
- Reconnects after a WebSocket interruption.
- `DRY_RUN=true` logs alerts and the generated inline keyboard without sending Telegram messages.

## Test

```bash
pytest -q
```
