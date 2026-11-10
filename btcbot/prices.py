"""BTC spot candles from Coinbase Exchange public API (Binance.com is geo-blocked from this box)."""
from .http import get_json

def candles_1m(limit=120):
    rows = get_json("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60")
    rows = sorted(rows, key=lambda r: r[0])[-limit:]  # [time, low, high, open, close, volume]
    return [{"t": r[0], "o": r[3], "h": r[2], "l": r[1], "c": r[4], "v": r[5]} for r in rows]

def spot():
    d = get_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
    return float(d["price"])
