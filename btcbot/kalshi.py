"""Public, read-only Kalshi trade-api v2 (no auth, no keys)."""
import math
from datetime import datetime
from .http import get_json

BASE = "https://api.elections.kalshi.com/trade-api/v2"


def ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def series(ticker):
    return get_json(f"{BASE}/series/{ticker}")["series"]


def current_market(series_ticker, now):
    ms = get_json(f"{BASE}/markets?series_ticker={series_ticker}&status=open&limit=20")["markets"]
    for m in ms:
        if ts(m["open_time"]) <= now < ts(m["close_time"]):
            return m
    return None


def market(ticker):
    return get_json(f"{BASE}/markets/{ticker}")["market"]


def book(ticker):
    """Kalshi returns only bids for YES and NO. A YES ask at p == a NO bid at 1-p.
    Returns {'yes': {'bids': [(p,sz) desc], 'asks': [(p,sz) asc]}, 'no': {...}}"""
    ob = get_json(f"{BASE}/markets/{ticker}/orderbook")["orderbook_fp"]
    yb = sorted(((float(p), float(s)) for p, s in (ob.get("yes_dollars") or [])), reverse=True)
    nb = sorted(((float(p), float(s)) for p, s in (ob.get("no_dollars") or [])), reverse=True)
    ya = [(round(1 - p, 4), s) for p, s in nb]  # asc because nb desc
    na = [(round(1 - p, 4), s) for p, s in yb]
    return {"yes": {"bids": yb, "asks": ya}, "no": {"bids": nb, "asks": na}}


def fee(contracts, price, rate=0.07):
    """Kalshi general trading fee: round up(M*rate*C*P*(1-P)); rounded up to the cent here (conservative)."""
    raw = rate * contracts * price * (1 - price)
    return math.ceil(raw * 100 - 1e-9) / 100 if raw > 0 else 0.0
