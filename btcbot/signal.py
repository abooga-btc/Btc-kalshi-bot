"""Transparent confluence signal: UP / DOWN / WAIT."""

def ema(vals, n):
    k = 2 / (n + 1); e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e

def rsi(vals, n=14):
    gains = losses = 0.0
    d = [vals[i] - vals[i - 1] for i in range(1, len(vals))][-n:]
    for x in d:
        if x > 0: gains += x
        else: losses -= x
    if losses == 0: return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)

def evaluate(candles, spot_px, ptb, cfg):
    s = cfg["signal"]
    closes = [c["c"] for c in candles] + [spot_px]
    ef, es = ema(closes, s["ema_fast"]), ema(closes, s["ema_slow"])
    r = rsi(closes, s["rsi_period"])
    m_short = spot_px - closes[-1 - s["momentum_short_min"]]
    m_long = spot_px - closes[-1 - s["momentum_long_min"]]
    delta_bps = (spot_px - ptb) / ptb * 1e4 if ptb else 0.0
    sgn = lambda x: 1 if x > 0 else (-1 if x < 0 else 0)
    votes = {
        "vs_price_to_beat": sgn(delta_bps) if abs(delta_bps) >= s["min_delta_bps"] else 0,
        "momentum_short": sgn(m_short),
        "momentum_long": sgn(m_long),
        "ema_cross": sgn(ef - es),
    }
    total = sum(votes.values())
    detail = {"spot": round(spot_px, 2), "ref_open_coinbase": round(ptb, 2) if ptb else None,
              "delta_bps": round(delta_bps, 2), "ema_fast": round(ef, 2), "ema_slow": round(es, 2),
              "rsi": round(r, 1), "mom_short": round(m_short, 2), "mom_long": round(m_long, 2),
              "votes": votes, "score": total}
    need = s["min_votes"]
    if total >= need: call = "UP"
    elif total <= -need: call = "DOWN"
    else: return "WAIT", "no confluence", detail
    if abs(delta_bps) > s["max_delta_bps"]:
        return "WAIT", "move already priced in (delta too large)", detail
    if call == "UP" and r >= s["rsi_overbought"]:
        return "WAIT", "RSI overbought filter", detail
    if call == "DOWN" and r <= s["rsi_oversold"]:
        return "WAIT", "RSI oversold filter", detail
    return call, "confluence", detail
