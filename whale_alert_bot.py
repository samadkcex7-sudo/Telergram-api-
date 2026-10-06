"""Monitor configured Hyperliquid wallets and send large opening-fill alerts to Telegram.

This intentionally watches configured wallet addresses. Hyperliquid's public API does
not provide a global all-traders position stream, so it cannot discover every whale
without an additional indexed data provider.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp
import websockets

LOG = logging.getLogger("whale-alert")
HL_INFO_URL = os.getenv("HL_INFO_URL", "https://api.hyperliquid.xyz/info")
HL_WS_URL = os.getenv("HL_WS_URL", "wss://api.hyperliquid.xyz/ws")
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

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

    async def send_telegram(self, token: str, chat_id: str, text: str) -> None:
        url = TELEGRAM_API.format(token=token, method="sendMessage")
        async with self.session.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}) as r:
            body = await r.text()
            if r.status >= 300:
                raise RuntimeError(f"Telegram send failed ({r.status}): {body[:500]}")


def format_alert(alert: Alert) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(alert.timestamp_ms / 1000))
    review = f"https://hyperdash.com/"  # manual review only; do not automate Hyperdash.
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


async def watch_wallet(
    client: HyperliquidClient,
    wallet: str,
    token: str,
    chat_id: str,
    seen: set[tuple[str, str]],
    dry_run: bool,
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
                            LOG.info("DRY RUN\n%s", text)
                        else:
                            await client.send_telegram(token, chat_id, text)
                            LOG.info("Sent alert for %s %s", wallet, alert.tid)
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException, json.JSONDecodeError) as exc:
            LOG.warning("%s disconnected: %s; retrying in 5s", wallet, exc)
            await asyncio.sleep(5)


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    wallets = parse_addresses(os.environ["HL_WHALE_ADDRESSES"])
    if not wallets:
        raise RuntimeError("Set HL_WHALE_ADDRESSES to one or more Hyperliquid wallet addresses")
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    async with aiohttp.ClientSession() as session:
        client = HyperliquidClient(session)
        await client.refresh_day_volumes()
        while True:
            try:
                await asyncio.gather(*(watch_wallet(client, w, token, chat_id, set(), dry_run) for w in wallets))
            except Exception:
                LOG.exception("Watcher group failed; refreshing volumes in 30s")
                await asyncio.sleep(30)
                await client.refresh_day_volumes()


if __name__ == "__main__":
    asyncio.run(main())
