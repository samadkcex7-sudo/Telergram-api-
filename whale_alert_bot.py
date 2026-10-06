"""Monitor configured Hyperliquid wallets and send large opening-fill alerts to Telegram.

This intentionally watches configured wallet addresses. Hyperliquid's public API does
not provide a global all-traders position stream, so it cannot discover every whale
without an additional indexed data provider.
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import aiohttp
import websockets

LOG = logging.getLogger("whale-alert")
HL_INFO_URL = os.getenv("HL_INFO_URL", "https://api.hyperliquid.xyz/info")
HL_WS_URL = os.getenv("HL_WS_URL", "wss://api.hyperliquid.xyz/ws")
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
HL_CHART_URL_TEMPLATE = os.getenv("HL_CHART_URL_TEMPLATE", "https://app.hyperliquid.xyz/trade/{coin}")
HYPERDASH_REVIEW_URL = "https://hyperdash.com/"
ERROR_NOTIFY_COOLDOWN_SECONDS = int(os.getenv("ERROR_NOTIFY_COOLDOWN_SECONDS", "300"))

FIXED_THRESHOLDS = {
    "BTC": Decimal("5000000"),
    "ETH": Decimal("2000000"),
    "SOL": Decimal("750000"),
    "XRP": Decimal("500000"),
}


def dec(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def parse_addresses(raw: str) -> list[str]:
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Alert:
    wallet: str
    coin: str
    direction: str
    notional: Decimal
    threshold: Decimal
    px: Decimal
    size: Decimal
    timestamp_ms: int
    tid: str
    hash: str = ""


def opening_direction(fill: dict[str, Any]) -> str | None:
    direction = str(fill.get("dir", ""))
    if direction.lower().startswith("open long"):
        return "LONG / BUY"
    if direction.lower().startswith("open short"):
        return "SHORT / SELL"
    return None


def classify_fill(
    wallet: str,
    fill: dict[str, Any],
    day_volume: Decimal,
    fixed_thresholds: dict[str, Decimal] | None = None,
    other_asset_pct: Decimal = Decimal("0.01"),
) -> Alert | None:
    direction = opening_direction(fill)
    if not direction:
        return None
    coin = str(fill.get("coin", "")).upper()
    px = dec(fill.get("px"))
    size = dec(fill.get("sz"))
    notional = abs(px * size)
    if not coin or notional <= 0:
        return None
    thresholds = fixed_thresholds or FIXED_THRESHOLDS
    threshold = thresholds.get(coin, day_volume * other_asset_pct)
    if threshold <= 0 or notional < threshold:
        return None
    return Alert(
        wallet=wallet,
        coin=coin,
        direction=direction,
        notional=notional,
        threshold=threshold,
        px=px,
        size=size,
        timestamp_ms=int(fill.get("time") or time.time() * 1000),
        tid=str(fill.get("tid") or fill.get("oid") or fill.get("hash") or ""),
        hash=str(fill.get("hash") or ""),
    )


class HyperliquidClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.day_volume: dict[str, Decimal] = {}

    async def refresh_day_volumes(self) -> None:
        async with self.session.post(HL_INFO_URL, json={"type": "metaAndAssetCtxs"}) as r:
            r.raise_for_status()
            payload = await r.json()
        meta, contexts = payload
        for asset, ctx in zip(meta.get("universe", []), contexts):
            coin = str(asset.get("name", "")).upper()
            self.day_volume[coin] = dec(ctx.get("dayNtlVlm"))
        LOG.info("Loaded 24h notional volume for %d assets", len(self.day_volume))

    async def send_telegram(self, token: str, chat_id: str, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        url = TELEGRAM_API.format(token=token, method="sendMessage")
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with self.session.post(url, json=payload) as r:
            body = await r.text()
            if r.status >= 300:
                raise RuntimeError(f"Telegram send failed ({r.status}): {body[:500]}")


class ErrorNotifier:
    """Log failures and optionally notify Telegram without creating an alert loop."""

    def __init__(self, client: HyperliquidClient, token: str, chat_id: str, dry_run: bool):
        self.client = client
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.last_sent: dict[str, float] = {}

    async def report(self, kind: str, exc: BaseException, wallet: str | None = None) -> None:
        context = f" wallet={wallet}" if wallet else ""
        LOG.error("%s%s: %s", kind, context, exc, exc_info=True)
        if self.dry_run or not self.chat_id:
            return
        now = time.monotonic()
        if now - self.last_sent.get(kind, 0) < ERROR_NOTIFY_COOLDOWN_SECONDS:
            LOG.info("Suppressed duplicate Telegram error notification: %s", kind)
            return
        message = f"Whale alert bot error\nType: {kind}{context}\nDetails: {str(exc)[:500]}"
        try:
            await self.client.send_telegram(self.token, self.chat_id, message)
            self.last_sent[kind] = now
        except Exception as notify_exc:
            # Never recurse: Telegram failure is recorded locally only.
            LOG.error("Could not send Telegram error notification: %s", notify_exc, exc_info=True)


def format_alert(alert: Alert) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(alert.timestamp_ms / 1000))
    review = HYPERDASH_REVIEW_URL  # manual review only; do not automate Hyperdash.
    return (
        f"WHALE POSITION ALERT\n"
        f"{alert.coin} — {alert.direction}\n"
        f"Notional: ${alert.notional:,.0f}\n"
        f"Threshold: ${alert.threshold:,.0f}\n"
        f"Entry: ${alert.px:,.8g} | Size: {alert.size:,.8g}\n"
        f"Wallet: {alert.wallet}\n"
        f"Time: {ts}\n"
        f"Review manually: {review}"
    )


def alert_keyboard(alert: Alert) -> dict[str, list[list[dict[str, str]]]]:
    """Build safe URL buttons; buttons only open pages and never place trades."""
    coin_path = quote(alert.coin, safe=":@._-")
    chart_url = HL_CHART_URL_TEMPLATE.format(coin=coin_path)
    return {
        "inline_keyboard": [
            [{"text": f"Open {alert.coin} chart", "url": chart_url}],
            [{"text": "Open Hyperdash (manual)", "url": HYPERDASH_REVIEW_URL}],
        ]
    }


async def watch_wallet(
    client: HyperliquidClient,
    wallet: str,
    token: str,
    chat_id: str,
    seen: set[tuple[str, str]],
    dry_run: bool,
    notifier: ErrorNotifier,
) -> None:
    while True:
        try:
            async with websockets.connect(HL_WS_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "userFills", "user": wallet}}))
                LOG.info("Watching %s", wallet)
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get("channel") != "userFills":
                        continue
                    data = message.get("data", {})
                    if data.get("isSnapshot"):
                        continue
                    fills = data if isinstance(data, list) else data.get("fills", [])
                    for fill in fills:
                        alert = classify_fill(wallet, fill, client.day_volume.get(str(fill.get("coin", "")).upper(), Decimal("0")))
                        if not alert or (wallet, alert.tid) in seen:
                            continue
                        seen.add((wallet, alert.tid))
                        text = format_alert(alert)
                        if dry_run:
                            LOG.info("DRY RUN\n%s\nKeyboard: %s", text, alert_keyboard(alert))
                        else:
                            await client.send_telegram(token, chat_id, text, alert_keyboard(alert))
                            LOG.info("Sent alert for %s %s", wallet, alert.tid)
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException, json.JSONDecodeError) as exc:
            await notifier.report("wallet_stream", exc, wallet)
            LOG.warning("%s disconnected; retrying in 5s", wallet)
            await asyncio.sleep(5)


async def main() -> None:
    log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    log_file = os.getenv("LOG_FILE", "whale_alert_bot.log")
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=int(os.getenv("LOG_MAX_BYTES", "10485760")),
        backupCount=int(os.getenv("LOG_BACKUPS", "5")),
    )
    file_handler.setFormatter(formatter)
    LOG.setLevel(log_level)
    LOG.addHandler(stream)
    LOG.addHandler(file_handler)
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    error_chat_id = os.getenv("ERROR_NOTIFY_CHAT_ID", chat_id)
    wallets = parse_addresses(os.environ["HL_WHALE_ADDRESSES"])
    if not wallets:
        raise RuntimeError("Set HL_WHALE_ADDRESSES to one or more Hyperliquid wallet addresses")
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    async with aiohttp.ClientSession() as session:
        client = HyperliquidClient(session)
        notifier = ErrorNotifier(client, token, error_chat_id, dry_run)
        try:
            await client.refresh_day_volumes()
        except Exception as exc:
            await notifier.report("market_metadata", exc)
            raise
        while True:
            try:
                await asyncio.gather(*(watch_wallet(client, w, token, chat_id, set(), dry_run, notifier) for w in wallets))
            except Exception as exc:
                await notifier.report("watcher_group", exc)
                LOG.warning("Refreshing volumes in 30s")
                await asyncio.sleep(30)
                try:
                    await client.refresh_day_volumes()
                except Exception as refresh_exc:
                    await notifier.report("market_metadata", refresh_exc)


if __name__ == "__main__":
    asyncio.run(main())
