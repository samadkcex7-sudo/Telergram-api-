from decimal import Decimal
from whale_alert_bot import classify_fill


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
