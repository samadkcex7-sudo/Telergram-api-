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
export WATCHLIST_FILE='watchlist.json'
export DRY_RUN=false
python whale_alert_bot.py
```

The bot token must never be committed or pasted into source control. The Telegram bot must first receive `/start` from the target chat. Use a numeric `chat.id` for `TELEGRAM_CHAT_ID`.

## Manage addresses from Telegram

Only the configured `TELEGRAM_CHAT_ID` can change the watchlist. Send any of these messages to the bot:

```text
0x0123456789012345678901234567890123456789
/add 0x0123456789012345678901234567890123456789
/list
/remove 0x0123456789012345678901234567890123456789
/help
```

Addresses are validated as 42-character hexadecimal `0x...` master-wallet addresses, persisted atomically in `WATCHLIST_FILE`, and started/stopped live without restarting the service. Messages from other chats are ignored and logged. In `DRY_RUN=true`, addresses are still processed but confirmation replies are not sent.

## Logging and error notifications

The service writes logs to both stdout and a rotating file (`whale_alert_bot.log` by default). Configure rotation with:

```bash
export LOG_FILE='whale_alert_bot.log'
export LOG_MAX_BYTES=10485760
export LOG_BACKUPS=5
export LOG_LEVEL=INFO
```

Unexpected wallet-stream, metadata, watcher, and Telegram-delivery failures are logged with traceback context. A rate-limited error message is sent to `ERROR_NOTIFY_CHAT_ID` (defaulting to `TELEGRAM_CHAT_ID`). Set `ERROR_NOTIFY_COOLDOWN_SECONDS` to change the per-error-type cooldown. If Telegram is unavailable, the failure is recorded locally and is not retried recursively.

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

## Docker and systemd deployment

The repository includes `Dockerfile`, `docker-compose.yml`, `hyperliquid-whale-alert.service`, and `deploy_cloud.sh` for Ubuntu 24.04. The service stores the Telegram-managed watchlist and rotating logs in a persistent Docker volume and starts automatically after reboot.

On the cloud server:

```bash
sudo mkdir -p /etc/hyperliquid-whale-alert
sudo install -m 600 /dev/null /etc/hyperliquid-whale-alert/.env
sudo editor /etc/hyperliquid-whale-alert/.env
```

Set at minimum:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
HL_WHALE_ADDRESSES=
DRY_RUN=false
```

Then run:

```bash
sudo REPO_URL=https://github.com/samadkcex7-sudo/Telergram-api-.git ./deploy_cloud.sh
sudo journalctl -u hyperliquid-whale-alert.service -f
```

The deployment script installs Docker if needed, clones or updates `/opt/hyperliquid-whale-alert`, installs the systemd unit, and enables it at boot. Do not place the Telegram token in Git. On a managed cloud computer, keep the default restrictive UFW policy; this bot needs no inbound port because it uses Telegram long polling and outbound Hyperliquid WebSocket traffic.
