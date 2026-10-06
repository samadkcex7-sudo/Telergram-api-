from decimal import Decimal
from whale_alert_bot import WatchlistStore, alert_keyboard, classify_fill, parse_addresses


def test_btc_fixed_threshold():
    fill = {"coin": "BTC", "px": "100000", "sz": "50", "dir": "Open Long", "tid": 1}
    assert classify_fill("0xabc", fill, Decimal("999999999")) is not None


def test_btc_below_fixed_threshold():
    fill = {"coin": "BTC", "px": "100000", "sz": "49", "dir": "Open Short", "tid": 2}
    assert classify_fill("0xabc", fill, Decimal("999999999")) is None


def test_other_asset_uses_one_percent_volume():
    fill = {"coin": "HYPE", "px": "20", "sz": "60000", "dir": "Open Long", "tid": 3}
    assert classify_fill("0xabc", fill, Decimal("1000000")) is not None


def test_close_is_not_alerted():
    fill = {"coin": "ETH", "px": "3000", "sz": "1000", "dir": "Close Long", "tid": 4}
    assert classify_fill("0xabc", fill, Decimal("999999999")) is None


def test_keyboard_opens_asset_chart():
    fill = {"coin": "BTC", "px": "100000", "sz": "50", "dir": "Open Long", "tid": 5}
    alert = classify_fill("0xabc", fill, Decimal("999999999"))
    keyboard = alert_keyboard(alert)
    assert keyboard["inline_keyboard"][0][0]["url"] == "https://app.hyperliquid.xyz/trade/BTC"
    assert keyboard["inline_keyboard"][1][0]["url"] == "https://hyperdash.com/"


def test_watchlist_accepts_valid_addresses_and_persists(tmp_path):
    address = "0x0123456789012345678901234567890123456789"
    store = WatchlistStore(str(tmp_path / "watchlist.json"), parse_addresses(address))
    assert address in store.addresses
    assert store.add([address]) == []
    assert store.remove([address]) == [address]
    assert address not in store.addresses
