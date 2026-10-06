"""Monitor configured Hyperliquid wallets and manage the watchlist from Telegram.

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
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
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
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

FIXED_THRESHOLDS = {"BTC": Decimal("5000000"), "ETH": Decimal("2000000"), "SOL": Decimal("750000"), "XRP": Decimal("500000")}


def dec(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def parse_addresses(raw: str) -> list[str]:
    return [x.strip().lower() for x in raw.split(",") if ADDRESS_RE.fullmatch(x.strip())]


def valid_address(value: str) -> bool:
    return bool(ADDRESS_RE.fullmatch(value.strip()))


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


def classify_fill(wallet: str, fill: dict[str, Any], day_volume: Decimal, fixed_thresholds: dict[str, Decimal] | None = None, other_asset_pct: Decimal = Decimal("0.01")) -> Alert | None:
    direction = opening_direction(fill)
    if not direction:
        return None
    coin = str(fill.get("coin", "")).upper()
    px, size = dec(fill.get("px")), dec(fill.get("sz"))
    notional = abs(px * size)
    if not coin or notional <= 0:
        return None
    thresholds = fixed_thresholds or FIXED_THRESHOLDS
    threshold = thresholds.get(coin, day_volume * other_asset_pct)
    if threshold <= 0 or notional < threshold:
        return None
    return Alert(wallet, coin, direction, notional, threshold, px, size, int(fill.get("time") or time.time() * 1000), str(fill.get("tid") or fill.get("oid") or fill.get("hash") or ""), str(fill.get("hash") or ""))


class WatchlistStore:
    def __init__(self, path: str, seed: list[str]):
        self.path = Path(path)
        self.addresses: set[str] = set()
        self.load(seed)

    def load(self, seed: list[str]) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self.addresses = {x.lower() for x in data if isinstance(x, str) and valid_address(x)}
            except (OSError, json.JSONDecodeError) as exc:
                LOG.error("Could not load watchlist %s: %s", self.path, exc, exc_info=True)
        self.addresses.update(seed)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(sorted(self.addresses), indent=2) + "\n")
        tmp.replace(self.path)

    def add(self, addresses: list[str]) -> list[str]:
        new = [a.lower() for a in addresses if a.lower() not in self.addresses]
        self.addresses.update(new)
        if new:
            self.save()
        return new

    def remove(self, addresses: list[str]) -> list[str]:
        removed = [a.lower() for a in addresses if a.lower() in self.addresses]
        self.addresses.difference_update(removed)
        if removed:
            self.save()
        return removed


class HyperliquidClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.day_volume: dict[str, Decimal] = {}

    async def refresh_day_volumes(self) -> None:
        async with self.session.post(HL_INFO_URL, json={"type": "metaAndAssetCtxs"}) as r:
            r.raise_for_status()
            meta, contexts = await r.json()
        for asset, ctx in zip(meta.get("universe", []), contexts):
            self.day_volume[str(asset.get("name", "")).upper()] = dec(ctx.get("dayNtlVlm"))
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

    async def get_updates(self, token: str, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        url = TELEGRAM_API.format(token=token, method="getUpdates")
        async with self.session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=35)) as r:
            r.raise_for_status()
            body = await r.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {body}")
        return body.get("result", [])


class ErrorNotifier:
    def __init__(self, client: HyperliquidClient, token: str, chat_id: str, dry_run: bool):
        self.client, self.token, self.chat_id, self.dry_run = client, token, chat_id, dry_run
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
        try:
            await self.client.send_telegram(self.token, self.chat_id, f"Whale alert bot error\nType: {kind}{context}\nDetails: {str(exc)[:500]}")
            self.last_sent[kind] = now
        except Exception as notify_exc:
            LOG.error("Could not send Telegram error notification: %s", notify_exc, exc_info=True)


def format_alert(alert: Alert) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(alert.timestamp_ms / 1000))
    return (f"WHALE POSITION ALERT\n{alert.coin} — {alert.direction}\nNotional: ${alert.notional:,.0f}\nThreshold: ${alert.threshold:,.0f}\n"
            f"Entry: ${alert.px:,.8g} | Size: {alert.size:,.8g}\nWallet: {alert.wallet}\nTime: {ts}\nReview manually: {HYPERDASH_REVIEW_URL}")


def alert_keyboard(alert: Alert) -> dict[str, list[list[dict[str, str]]]]:
    chart_url = HL_CHART_URL_TEMPLATE.format(coin=quote(alert.coin, safe=":@._-"))
    return {"inline_keyboard": [[{"text": f"Open {alert.coin} chart", "url": chart_url}], [{"text": "Open Hyperdash (manual)", "url": HYPERDASH_REVIEW_URL}]]}


async def watch_wallet(client: HyperliquidClient, wallet: str, token: str, chat_id: str, seen: set[tuple[str, str]], dry_run: bool, notifier: ErrorNotifier) -> None:
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
        except Exception as exc:
            await notifier.report("wallet_watcher", exc, wallet)
            await asyncio.sleep(5)


async def telegram_watchlist_loop(client: HyperliquidClient, token: str, admin_chat_id: str, store: WatchlistStore, watchers: dict[str, asyncio.Task[Any]], seen: dict[str, set[tuple[str, str]]], dry_run: bool, notifier: ErrorNotifier) -> None:
    offset: int | None = None
    while True:
        try:
            for update in await client.get_updates(token, offset):
                offset = int(update["update_id"]) + 1
                message = update.get("message", {})
                chat_id = str(message.get("chat", {}).get("id", ""))
                if chat_id != str(admin_chat_id):
                    LOG.warning("Ignored watchlist message from unauthorized chat %s", chat_id)
                    continue
                text = str(message.get("text", "")).strip()
                if not text:
                    continue
                command, _, args = text.partition(" ")
                command = command.lower().split("@")[0]
                candidates = args.split() if command in {"/add", "/watch", "/remove", "/unwatch"} else text.split()
                addresses = [x.lower() for x in candidates if valid_address(x)]
                if command in {"/start", "/help"}:
                    reply = "Send a wallet address to watch it, or use /add 0x...\n/list — show watched wallets\n/remove 0x... — stop watching"
                elif command == "/list":
                    reply = "Watched wallets:\n" + ("\n".join(sorted(store.addresses)) if store.addresses else "(none)")
                elif command in {"/add", "/watch"} or (not command.startswith("/")):
                    added = store.add(addresses)
                    for wallet in added:
                        seen.setdefault(wallet, set())
                        watchers[wallet] = asyncio.create_task(watch_wallet(client, wallet, token, admin_chat_id, seen[wallet], dry_run, notifier))
                    reply = f"Added {len(added)} wallet(s)." if added else "No new valid wallet address found. Send a 42-character 0x... address."
                elif command in {"/remove", "/unwatch"}:
                    removed = store.remove(addresses)
                    for wallet in removed:
                        task = watchers.pop(wallet, None)
                        if task:
                            task.cancel()
                    reply = f"Removed {len(removed)} wallet(s)." if removed else "No matching watched wallet found."
                else:
                    reply = "Unknown command. Use /help."
                if not dry_run:
                    await client.send_telegram(token, admin_chat_id, reply)
                LOG.info("Processed Telegram watchlist message command=%s added_or_removed=%d", command, len(addresses))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await notifier.report("telegram_watchlist", exc)
            await asyncio.sleep(5)


def configure_logging() -> None:
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stream = logging.StreamHandler(); stream.setFormatter(formatter)
    file_handler = logging.handlers.RotatingFileHandler(os.getenv("LOG_FILE", "whale_alert_bot.log"), maxBytes=int(os.getenv("LOG_MAX_BYTES", "10485760")), backupCount=int(os.getenv("LOG_BACKUPS", "5")))
    file_handler.setFormatter(formatter)
    LOG.setLevel(level); LOG.addHandler(stream); LOG.addHandler(file_handler)


async def main() -> None:
    configure_logging()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    admin_chat_id = os.environ["TELEGRAM_CHAT_ID"]
    seed = parse_addresses(os.getenv("HL_WHALE_ADDRESSES", ""))
    store = WatchlistStore(os.getenv("WATCHLIST_FILE", "watchlist.json"), seed)
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
    async with aiohttp.ClientSession() as session:
        client = HyperliquidClient(session)
        notifier = ErrorNotifier(client, token, os.getenv("ERROR_NOTIFY_CHAT_ID", admin_chat_id), dry_run)
        await client.refresh_day_volumes()
        watchers: dict[str, asyncio.Task[Any]] = {}
        seen: dict[str, set[tuple[str, str]]] = {}
        for wallet in sorted(store.addresses):
            seen[wallet] = set()
            watchers[wallet] = asyncio.create_task(watch_wallet(client, wallet, token, admin_chat_id, seen[wallet], dry_run, notifier))
        await telegram_watchlist_loop(client, token, admin_chat_id, store, watchers, seen, dry_run, notifier)


if __name__ == "__main__":
    asyncio.run(main())
