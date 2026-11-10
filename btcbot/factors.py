"""11-factor binary confluence score, modeled on Bucko's PUBLIC description
("Multi-TF confluence + 11-factor scoring", labels like 'LONG A 7/11', grades A+/A/B).
Bucko's real factor list is not public; names marked [public] are concepts Bucko names publicly,
the exact math of every factor here is our own approximation.

Each factor votes +1 (UP), -1 (DOWN) or 0 (neutral). Direction = sign of the vote sum.
Score = number of factors agreeing with that direction, out of 11.
"""
import math

FACTORS = [
    ("strike",     "Price vs price-to-beat (Kalshi target) [public: Oracle 'price to beat']"),
    ("trigger10s", "Short trigger: last 3x10s bars direction [public: '15s Trigger', 10s/15s signals]"),
    ("mom1m",      "1-minute momentum [public: '1m Momentum']"),
    ("struct3m",   "3-minute structure: higher highs+lows / lower highs+lows [public: '3m Structure']"),
    ("ctx15m",     "15-minute context: 15m close vs 15m EMA(8) [public: '15m Context']"),
    ("ribbon",     "Flow ribbon: EMA 8/13/21 stack on 1m [public: 'ribbon' flow read; EMA choice inferred]"),
    ("bos",        "Break of structure on 10s bars [public: 'BOS' label; rule inferred]"),
    ("volume",     "Volume confirmation: last 1m vol vs 20-bar avg, in candle direction [public: 'Vol', status box 'volume'; rule inferred]"),
    ("orderflow",  "Taker buy/sell imbalance last 60s on Coinbase [public: Belugo 'order flow'; rule inferred]"),
    ("location",   "Location in 60m range: near demand votes UP, near supply votes DOWN [public: Demand/Supply/Liquidity zones; rule inferred]"),
    ("rsi_rev",    "RSI(14) 1m momentum, flips when stretched (REV) [public: 'REV' label; RSI inferred]"),
]


def ema(vals, n):
    k = 2 / (n + 1); e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(vals, n=14):
    d = [vals[i] - vals[i - 1] for i in range(1, len(vals))][-n:]
    g = sum(x for x in d if x > 0); l = -sum(x for x in d if x < 0)
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def sgn(x, eps=0.0):
    return 1 if x > eps else (-1 if x < -eps else 0)


def compute(spot, strike_cb, bars10, m1, m15, flow, cfg):
    s = cfg["signal"]; v = {}; info = {}
    # 1 strike
    d_bps = (spot - strike_cb) / strike_cb * 1e4
    v["strike"] = sgn(d_bps, s["strike_min_bps"]); info["delta_bps"] = round(d_bps, 2)
    # 2 trigger 10s
    n = s["trigger_bars_10s"]
    if len(bars10) > n:
        v["trigger10s"] = sgn(spot - bars10[-1 - n]["c"])
    else:
        v["trigger10s"] = 0
    # 3 1m momentum
    c1 = [c["c"] for c in m1]
    mom = (spot - c1[-2]) / c1[-2] * 1e4
    v["mom1m"] = sgn(mom, s["momentum_1m_bps"]); info["mom1m_bps"] = round(mom, 2)
    # 4 3m structure: build 3m bars from the last 6 1m candles
    b3 = [m1[i:i + 3] for i in range(len(m1) - 6, len(m1), 3)]
    h = [max(x["h"] for x in b) for b in b3]; l = [min(x["l"] for x in b) for b in b3]
    v["struct3m"] = 1 if (h[1] > h[0] and l[1] > l[0]) else (-1 if (h[1] < h[0] and l[1] < l[0]) else 0)
    # 5 15m context
    c15 = [c["c"] for c in m15[-30:]]
    e15 = ema(c15, 8)
    v["ctx15m"] = sgn(spot - e15); info["ema15m_8"] = round(e15, 2)
    # 6 ribbon
    cl = c1[-60:] + [spot]
    ef, em, es = ema(cl, s["ribbon_fast"]), ema(cl, s["ribbon_mid"]), ema(cl, s["ribbon_slow"])
    v["ribbon"] = 1 if ef > em > es else (-1 if ef < em < es else 0)
    # 7 BOS on 10s bars: close beyond prior swing (excluding the last 3 bars)
    lb = s["bos_lookback_10s_bars"]
    if len(bars10) > lb + 3:
        prior = bars10[-(lb + 3):-3]
        hi, lo = max(b["h"] for b in prior), min(b["l"] for b in prior)
        v["bos"] = 1 if spot > hi else (-1 if spot < lo else 0)
    else:
        v["bos"] = 0
    # 8 volume
    vols = [c["v"] for c in m1[-21:-1]]; last = m1[-1]
    ratio = last["v"] / (sum(vols) / len(vols)) if vols and sum(vols) else 0
    v["volume"] = sgn(last["c"] - last["o"]) if ratio >= s["volume_ratio_min"] else 0
    info["vol_ratio"] = round(ratio, 2)
    # 9 order flow
    buy, sell = flow
    imb = (buy - sell) / (buy + sell) if buy + sell else 0
    v["orderflow"] = sgn(imb, s["orderflow_imbalance_min"]); info["flow_imb"] = round(imb, 3)
    # 10 location in 60m range
    rng = m1[-s["location_lookback_min"]:]
    H, L = max(x["h"] for x in rng), min(x["l"] for x in rng)
    pos = (spot - L) / (H - L) if H > L else 0.5
    f = s["location_edge_frac"]
    v["location"] = 1 if pos <= f else (-1 if pos >= 1 - f else 0); info["range_pos"] = round(pos, 2)
    # 11 RSI / REV
    r = rsi(cl, s["rsi_period"]); info["rsi"] = round(r, 1)
    if r >= s["rsi_stretched_hi"]: v["rsi_rev"] = -1
    elif r <= s["rsi_stretched_lo"]: v["rsi_rev"] = 1
    elif r >= s["rsi_bull"]: v["rsi_rev"] = 1
    elif r <= s["rsi_bear"]: v["rsi_rev"] = -1
    else: v["rsi_rev"] = 0

    net = sum(v.values())
    direction = "UP" if net > 0 else ("DOWN" if net < 0 else None)
    score = sum(1 for x in v.values() if direction and x == (1 if direction == "UP" else -1))
    th = s["grade_thresholds"]
    grade = "A+" if score >= th["A+"] else "A" if score >= th["A"] else "B" if score >= th["B"] else None
    return {"direction": direction, "score": score, "net": net, "grade": grade, "votes": v, **info}


GRADE_RANK = {"A+": 3, "A": 2, "B": 1, None: 0}


def prob_up(spot, strike, sigma_per_sec, secs_left, net_votes, k):
    """Driftless lognormal fair value with a small score tilt:
    p = Phi( (ln(S/K) + k*(net/11)*sigma*sqrt(t)) / (sigma*sqrt(t)) ).
    Settlement is a 60s average, so we shorten the horizon by ~30s (floor 10s)."""
    t = max(secs_left - 30, 10)
    sd = sigma_per_sec * math.sqrt(t)
    z = (math.log(spot / strike) + k * (net_votes / 11) * sd) / sd
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def realized_sigma(m1, n=60, floor=2e-5):
    c = [x["c"] for x in m1[-(n + 1):]]
    r = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    if len(r) < 5: return floor
    mu = sum(r) / len(r)
    var = sum((x - mu) ** 2 for x in r) / (len(r) - 1)
    return max(math.sqrt(var / 60), floor)


# ---------------- YoungBuffett rules (2026-10-29) ----------------
def entry_timing(t_in, secs_left, score, e):
    """Early-entry rule. Returns (ok, reason). Early window: entry_window_start_sec..early_entry_end_sec
    (about 11+ minutes left). Later entries only for score >= late_entry_min_score with
    >= late_entry_min_left_sec left."""
    early_end = e.get("early_entry_end_sec", 240)
    late_score = e.get("late_entry_min_score", 7)
    late_left = e.get("late_entry_min_left_sec", 360)
    if t_in < e["entry_window_start_sec"]: return False, "5 too early in window"
    if t_in <= early_end: return True, "early"
    if score >= late_score and secs_left >= late_left: return True, "late A+/A"
    return False, (f"5 late entry (t+{int(t_in)}s, {secs_left / 60:.1f}m left; late needs score>={late_score} "
                   f"and >={late_left / 60:.0f}m left)")


def entry_deadline_sec(score, dur, e):
    """Seconds into the window after which an entry order may no longer rest/fill for this score."""
    early_end = e.get("early_entry_end_sec", 240)
    if score >= e.get("late_entry_min_score", 7):
        return max(early_end, dur - e.get("late_entry_min_left_sec", 360))
    return early_end


def atr(m1, n=14):
    tr = []
    for i in range(1, len(m1)):
        h, l, pc = m1[i]["h"], m1[i]["l"], m1[i - 1]["c"]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    tr = tr[-n:]
    return sum(tr) / len(tr) if tr else 0.0


def chop_check(sig, m1, spot, cfg):
    """'MIXED/WAIT means skip'. Returns (ok, reason, metrics).
    mixed: longer-term trend (ribbon EMA 8/13/21 stack must agree, 15m context must not oppose) and
           short-horizon momentum (1m momentum must agree, 10s trigger must not oppose) must both point the trade's way.
    chop:  efficiency ratio of the last N 1m closes below er_min, or the N-minute range smaller than
           range_atr_min x ATR(14) (sideways / compressed)."""
    c = cfg.get("chop", {})
    if not c.get("enabled", True): return True, "chop filter off", {}
    d = 1 if sig["direction"] == "UP" else -1
    v = sig["votes"]
    trend_ok = v.get("ribbon") == d and v.get("ctx15m") != -d
    mom_ok = v.get("mom1m") == d and v.get("trigger10s") != -d
    n = c.get("lookback_min", 10)
    closes = [x["c"] for x in m1[-n:]] + [spot]
    net = abs(closes[-1] - closes[0]); path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    er = net / path if path else 0.0
    rng = m1[-n:]
    rr = (max(x["h"] for x in rng) - min(x["l"] for x in rng)) if rng else 0.0
    a = atr(m1, c.get("atr_period", 14))
    range_atr = rr / a if a else 0.0
    m = {"er": round(er, 3), "range_atr": round(range_atr, 2), "trend_ok": trend_ok, "mom_ok": mom_ok}
    if not (trend_ok and mom_ok):
        parts = []
        if not trend_ok: parts.append(f"trend ribbon={v.get('ribbon')} ctx15m={v.get('ctx15m')}")
        if not mom_ok: parts.append(f"momentum mom1m={v.get('mom1m')} trigger10s={v.get('trigger10s')}")
        return False, "11 mixed: " + ", ".join(parts), m
    if er < c.get("er_min", 0.25): return False, f"11 chop: efficiency {er:.2f} < {c.get('er_min', 0.25)}", m
    if range_atr < c.get("range_atr_min", 1.8):
        return False, f"11 chop: {n}m range {range_atr:.2f}x ATR < {c.get('range_atr_min', 1.8)}", m
    return True, "trend", m


def kalshi_fee(contracts, price, rate=0.07):
    raw = rate * contracts * price * (1 - price)
    return math.ceil(raw * 100 - 1e-9) / 100 if raw > 0 else 0.0


def partial_tp_qty(qty, bid, to_recover, rate=0.07):
    """Smallest whole number of contracts whose sale at `bid` (after taker fee) recovers `to_recover` dollars.
    Returns qty (sell all) when qty <= 1 or when the partial would need everything."""
    qty = int(qty)
    if qty <= 1: return qty
    if to_recover <= 0: return 0
    for k in range(1, qty + 1):
        if k * bid - kalshi_fee(k, bid, rate) >= to_recover - 1e-9:
            return qty if k >= qty else k
    return qty
