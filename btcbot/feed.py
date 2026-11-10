"""BTC spot from Coinbase Exchange public API: trades -> 10s bars, plus 1m / 15m candles.
Note: Coinbase 'side' is the MAKER side, so side=='sell' means an aggressive (taker) buy."""
import time
from datetime import datetime
from .http import get_json

CB = "https://api.exchange.coinbase.com/products/BTC-USD"


def _t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class Feed:
    def __init__(self, keep_sec=900):
        self.trades = {}  # id -> (ts, price, size, taker_buy)
        self.keep = keep_sec
        self.last_ok = 0

    def _ingest(self, rows):
        for r in rows:
            self.trades[r["trade_id"]] = (_t(r["time"]), float(r["price"]), float(r["size"]), r["side"] == "sell")

    def backfill(self, pages=4):
        after = None
        for _ in range(pages):
            url = f"{CB}/trades?limit=1000" + (f"&after={after}" if after else "")
            rows = get_json(url)
            if not rows: break
            self._ingest(rows)
            after = min(r["trade_id"] for r in rows)
        self.last_ok = time.time()

    def poll(self):
        self._ingest(get_json(f"{CB}/trades?limit=1000"))
        cutoff = time.time() - self.keep
        self.trades = {k: v for k, v in self.trades.items() if v[0] >= cutoff}
        self.last_ok = time.time()

    def spot(self):
        if not self.trades: return None
        return max(self.trades.values())[1]

    def bars(self, sec=10):
        """OHLCV bars of `sec` seconds built from trades (closed + current partial)."""
        b = {}
        for t, p, s, _ in sorted(self.trades.values()):
            k = int(t // sec * sec)
            if k not in b: b[k] = {"t": k, "o": p, "h": p, "l": p, "c": p, "v": 0.0}
            x = b[k]; x["h"] = max(x["h"], p); x["l"] = min(x["l"], p); x["c"] = p; x["v"] += s
        return [b[k] for k in sorted(b)]

    def flow(self, window_sec):
        cut = time.time() - window_sec
        buy = sum(s for t, p, s, tb in self.trades.values() if t >= cut and tb)
        sell = sum(s for t, p, s, tb in self.trades.values() if t >= cut and not tb)
        return buy, sell

    def vwap_window(self, t0, t1):
        xs = [(p, s) for t, p, s, _ in self.trades.values() if t0 <= t < t1]
        v = sum(s for _, s in xs)
        return sum(p * s for p, s in xs) / v if v else None


def candles(granularity=60):
    rows = get_json(f"{CB}/candles?granularity={granularity}")
    rows = sorted(rows, key=lambda r: r[0])
    return [{"t": r[0], "o": r[3], "h": r[2], "l": r[1], "c": r[4], "v": r[5]} for r in rows]
