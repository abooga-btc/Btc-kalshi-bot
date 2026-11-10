"""1-minute aggregate taker delta (Bucko 'bookmeter' style), added 2026-10-29.
delta = taker-buy USD volume - taker-sell USD volume over the last `lookback_sec` seconds.

Primary source: Binance spot BTCUSDT via data-api.binance.vision (public market-data mirror;
api.binance.com returns 451 from this box). We pull 1-second klines (limit 180) in ONE request:
field 7 = quote (USDT) volume, field 10 = taker-buy quote volume, so per second
delta = 2*takerbuy - total. 60s and 180s deltas come from the same response.
Fallback: Coinbase BTC-USD trades already held in memory by Feed (no extra network), with the
threshold scaled by coinbase_scale (Coinbase spot ~0.5x Binance spot USD volume per minute).
binance.us was measured at ~0.08% of Binance volume -> too thin to be meaningful, not used.

Runs in a daemon thread (refresh every refresh_sec, 3s HTTP timeout) so the tick loop never waits on it.
"""
import json, logging, threading, time, urllib.request

log = logging.getLogger("kalshi-btc15m")
BINANCE_1S = "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1s&limit=180"
UA = {"User-Agent": "btc15m-bot/1.0", "Accept": "application/json"}


def deltas_from_klines(rows, now, lookbacks=(60, 180)):
    """rows: Binance 1s klines. Returns {lookback: delta_usd} using bars whose open time is within lookback of now."""
    out = {}
    for lb in lookbacks:
        cut = (now - lb) * 1000
        out[lb] = sum(2 * float(r[10]) - float(r[7]) for r in rows if r[0] >= cut)
    return out


class OrderFlow:
    def __init__(self, cfg, feed=None):
        self.c = cfg.get("entry", {}).get("orderflow", {})
        self.feed = feed
        self.lock = threading.Lock()
        self.reading = None          # {"ts", "src", "d60", "d180", "scale"}
        self.fail_n = 0; self.last_warn = 0
        self._stop = False

    def fetch_binance(self):
        req = urllib.request.Request(BINANCE_1S, headers=UA)
        with urllib.request.urlopen(req, timeout=self.c.get("http_timeout_sec", 3)) as r:
            rows = json.loads(r.read().decode())
        if not rows: raise RuntimeError("empty klines")
        last_bar = rows[-1][0] / 1000
        now = time.time()
        if now - last_bar > 30: raise RuntimeError(f"binance data {now - last_bar:.0f}s old")
        d = deltas_from_klines(rows, now, (self.c.get("lookback_sec", 60), 180))
        return {"ts": now, "src": "binance", "d60": d[self.c.get("lookback_sec", 60)], "d180": d[180], "scale": 1.0}

    def from_coinbase(self):
        if not self.feed or not self.feed.trades: return None
        now = time.time()
        if now - self.feed.last_ok > 30: return None
        def dl(sec):
            cut = now - sec
            return sum((p * s) * (1 if tb else -1) for t, p, s, tb in list(self.feed.trades.values()) if t >= cut)
        return {"ts": self.feed.last_ok, "src": "coinbase", "d60": dl(self.c.get("lookback_sec", 60)), "d180": dl(180),
                "scale": self.c.get("coinbase_scale", 0.5)}

    def refresh(self):
        try:
            rd = self.fetch_binance(); self.fail_n = 0
        except Exception as e:
            self.fail_n += 1
            rd = None
            if time.time() - self.last_warn >= 60:
                self.last_warn = time.time()
                log.warning("orderflow: Binance fetch failed (%s); using Coinbase fallback if fresh", e)
        if rd:
            with self.lock: self.reading = rd

    def _loop(self):
        while not self._stop:
            self.refresh()
            time.sleep(self.c.get("refresh_sec", 5))

    def start(self):
        if not self.c.get("enabled", False): return
        threading.Thread(target=self._loop, name="orderflow", daemon=True).start()

    def get(self, now=None):
        """Latest reading, or Coinbase fallback when the Binance reading is missing/stale. None if nothing usable."""
        now = now or time.time()
        with self.lock: rd = self.reading
        if rd and now - rd["ts"] <= self.c.get("max_age_sec", 30): return rd
        return self.from_coinbase()


def check(side, rd, now, c):
    """Entry gate. side 'yes' (LONG/UP) or 'no' (SHORT/DOWN). Fails OPEN on missing/stale data.
    Returns (ok, reason, info)."""
    if not c.get("enabled", False): return True, "orderflow off", {}
    if not rd or now - rd["ts"] > c.get("max_age_sec", 30):
        return True, "orderflow unavailable (fail open)", {"of_src": None, "of_d60": None, "of_d180": None}
    thr = c.get("block_opposing_usd", 1_000_000) * rd.get("scale", 1.0)
    d = rd["d60"]
    info = {"of_src": rd["src"], "of_d60": round(d), "of_d180": round(rd["d180"]) if rd.get("d180") is not None else None,
            "of_thr": round(thr)}
    if side == "yes" and d <= -thr:
        return False, f"12 orderflow: 1m delta ${d/1e6:+.2f}M sell pressure vs LONG (thr ${thr/1e6:.2f}M {rd['src']})", info
    if side == "no" and d >= thr:
        return False, f"12 orderflow: 1m delta ${d/1e6:+.2f}M buy pressure vs SHORT (thr ${thr/1e6:.2f}M {rd['src']})", info
    return True, "orderflow ok", info
