"""Public, read-only Polymarket data (no auth, no keys)."""
import json
from datetime import datetime, timezone
from .http import get_json

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
CRYPTO_PRICE = "https://polymarket.com/api/crypto/crypto-price"
WINDOW = 900

def window_start(ts):
    return int(ts) - int(ts) % WINDOW

def slug_for(start_ts):
    return f"btc-updown-15m-{start_ts}"

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def get_market(start_ts):
    """Return dict with slug, condition id, token ids for Up/Down, fee schedule, status."""
    slug = slug_for(start_ts)
    evs = get_json(f"{GAMMA}/events?slug={slug}")
    if not evs:
        return None
    ev = evs[0]
    m = ev["markets"][0]
    outcomes = json.loads(m["outcomes"])
    tokens = json.loads(m["clobTokenIds"])
    prices = json.loads(m.get("outcomePrices") or "[]")
    tok = dict(zip(outcomes, tokens))
    return {
        "slug": slug,
        "title": ev.get("title"),
        "condition_id": m["conditionId"],
        "up_token": tok["Up"],
        "down_token": tok["Down"],
        "outcomes": outcomes,
        "outcome_prices": prices,
        "closed": bool(m.get("closed")),
        "accepting_orders": bool(m.get("acceptingOrders")),
        "fee_schedule": m.get("feeSchedule"),
        "fees_enabled": m.get("feesEnabled", False),
        "min_size": float(m.get("orderMinSize") or 5),
        "tick": float(m.get("orderPriceMinTickSize") or 0.01),
        "event_metadata": ev.get("eventMetadata") or {},
    }

def resolved_winner(mkt):
    """'Up'/'Down' once the market is closed with a 1/0 outcome, else None."""
    if not mkt or not mkt["closed"]:
        return None
    for o, p in zip(mkt["outcomes"], mkt["outcome_prices"]):
        if float(p) >= 0.99:
            return o
    return None

def price_to_beat(start_ts):
    """Polymarket's own open price (Chainlink) for the window."""
    url = (f"{CRYPTO_PRICE}?symbol=BTC&eventStartTime={iso(start_ts)}"
           f"&variant=fifteen&endDate={iso(start_ts + WINDOW)}")
    d = get_json(url)
    return d.get("openPrice"), d.get("closePrice")

def book(token_id):
    """Order book normalised: bids desc, asks asc, as (price, size) floats."""
    b = get_json(f"{CLOB}/book?token_id={token_id}")
    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks", []))
    return {"bids": bids, "asks": asks}

def taker_fee(shares, price, rate=0.07, exponent=1):
    """Docs: fee = C * feeRate * p * (1-p)   (exponent 1 for crypto_fees_v2)."""
    return shares * rate * (price * (1 - price)) ** exponent
