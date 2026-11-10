"""Offline tests / dry simulation for the 2026-10-29 YoungBuffett rules. No network, no keys.
Run: python3 tests/test_rules.py"""
import json, os, sys, tempfile, time, logging, math, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.WARNING)
from btcbot import factors as F, live as L, kengine as E

CFG = E.load_cfg()
CFGP = json.loads(json.dumps(CFG)); CFGP["exit"].update(partial_tp=True, take_profit_delta=0.15)   # partial-TP code path (off in live config)

def run_checks():
    """Run the offline rule checks. Returns the number of assertions that passed."""
    ok = 0
    def check(cond, msg):
        nonlocal ok
        assert cond, msg
        ok += 1
        print("PASS", msg)

    e = CFG["entry"]
    # ---- 2. early entry ----
    check(not F.entry_timing(100, 800, 5, e)[0], "B at t+100s blocked (start is now t+120s)")
    check(F.entry_timing(150, 750, 5, e)[0], "B at t+150s (12.5m left) allowed")
    check(not F.entry_timing(300, 600, 6, e)[0], "B at t+300s (10m left) blocked")
    check(F.entry_timing(300, 600, 7, e)[0], "A at t+300s (10m left) allowed")
    check(not F.entry_timing(560, 340, 9, e)[0], "A+ with 5.7m left blocked")
    check(F.entry_timing(540, 360, 8, e)[0], "A+ with exactly 6m left allowed")
    check(not F.entry_timing(110, 790, 9, e)[0], "A+ before t+120s blocked")
    check(F.entry_deadline_sec(5, 900, e) == 240 and F.entry_deadline_sec(7, 900, e) == 540, "resting-order deadline B=240s, A/A+=540s")

    # ---- 1. chop / mixed ----
    def mk(closes, rng=5.0):
        return [{"t": i * 60, "o": c, "h": c + rng, "l": c - rng, "c": c, "v": 1} for i, c in enumerate(closes)]
    trend = mk([80000 + 20 * i for i in range(80)])
    votes_up = {"ribbon": 1, "ctx15m": 1, "mom1m": 1, "trigger10s": 1}
    sig = {"direction": "UP", "votes": votes_up}
    r = F.chop_check(sig, trend, trend[-1]["c"] + 20, CFG); check(r[0], f"clean uptrend passes ({r[2]})")
    r = F.chop_check({"direction": "UP", "votes": {**votes_up, "ribbon": 0}}, trend, trend[-1]["c"] + 20, CFG)
    check(not r[0] and r[1].startswith("11 mixed"), f"ribbon not aligned -> {r[1]}")
    r = F.chop_check({"direction": "UP", "votes": {**votes_up, "mom1m": -1}}, trend, trend[-1]["c"] + 20, CFG)
    check(not r[0] and "momentum" in r[1], f"1m momentum against -> {r[1]}")
    saw = mk([80000 + (15 if i % 2 else -15) for i in range(80)], rng=20)
    r = F.chop_check(sig, saw, 80001, CFG); check(not r[0] and r[1].startswith("11 chop"), f"sideways sawtooth -> {r[1]}")

    # ---- 4. partial TP size ----
    cost = 4.41 + 0.16
    k = F.partial_tp_qty(9, 0.64, cost)
    check(k == 8 and 8 * 0.64 - F.kalshi_fee(8, 0.64) >= cost and 7 * 0.64 - F.kalshi_fee(7, 0.64) < cost,
          f"9 @0.49, bid 0.64: sell {k} to recover ${cost:.2f}, ride 1")
    check(F.partial_tp_qty(1, 0.70, 0.55) == 1, "1 contract -> sell all")
    check(F.partial_tp_qty(4, 0.40, 1.60) == 4, "partial needing everything -> sell all")
    check(F.partial_tp_qty(20, 0.40, 5.0) == 14, f"20 @0.25 bid 0.40 -> {F.partial_tp_qty(20, 0.40, 5.0)} (rounded up)")

    # ---- 3. no stop-loss ----
    x = CFG["exit"]; p = {"entry_px": 0.49}
    check(E.exit_kind(p, 0.05, x) is None, "bid 0.05 on 0.49 entry: no SL, hold to settlement")
    check(E.exit_kind(p, 0.68, x) is None, "bid 0.68 (+0.19) -> no TP yet (TP is +0.20)")
    check(E.exit_kind(p, 0.69, x) == "TP", "bid 0.69 (+0.20) -> TP")
    check(CFG["entry"]["min_price"] == 0.50 and CFG["entry"]["max_price"] == 0.75 and CFG["entry"]["min_edge"] == 0.03, "price band 0.50-0.75, min edge 0.03")
    check(CFG["live"]["daily_loss_cap_usd"] == 100 and CFG["live"]["max_trades_per_day"] == 50 and CFG["live"]["min_score"] == 5
          and CFG["chop"]["enabled"] and not CFG["exit"]["stop_loss_enabled"] and CFG["exit"]["bid_ceiling"] == 0.92 and not CFG["exit"]["partial_tp"],
          "config: $100 loss cap, 50/day, min score 5, chop on, no SL, ceiling 0.92, partial TP off")
    check(E.exit_kind({**p, "tp_done": True}, 0.80, x) is None, "after TP partial, TP does not re-fire")
    check(E.exit_kind({**p, "tp_done": True}, 0.92, x) == "CEIL", "remainder sold at bid 0.92")

    # ---- loss cap uses full stake as worst case ----
    class Dummy(L.LiveTrader):
        def __init__(self): pass
    t = Dummy(); t.max_cost = min(CFG["live"]["max_cost_per_trade_usd"], L.HARD_MAX_COST_PER_TRADE); t.loss_cap = min(CFG["live"]["daily_loss_cap_usd"], L.HARD_DAILY_LOSS_CAP)
    t._d = {"pnl": 0.0}; t.day = lambda: t._d
    check(t.max_cost == 20.0 and L.HARD_MAX_COST_PER_TRADE == 20.0 and CFG["live"]["max_cost_per_trade_usd"] == 20.0, "per-trade hard max $20, config $20 (not clamped)")
    for g, usd in (("B", 10.0), ("A", 15.0), ("A+", 20.0), (None, 10.0), ("C", 10.0)): check(t.size_for({"grade": g}) == usd, f"grade size {g} = ${usd:.0f}")
    t.max_cost = 25.0; check(t.size_for({"grade": "A+"}) == 20.0, "size capped at $20 hard max even if config said more")
    t.max_cost = 12.0; check(t.size_for({"grade": "A+"}) == 12.0 and t.size_for({"grade": "B"}) == 10.0, "config can only lower the size"); t.max_cost = 20.0
    check(t.loss_cap == 100.0 and L.HARD_DAILY_LOSS_CAP == 100.0, "live loss cap = $100 (hard max 100)")
    def stake(pnl, g): t._d["pnl"] = pnl; return t.stake_for({"grade": g})[0]
    check(stake(-15.0, "B") == 10 and stake(-89.99, "B") == 10 and stake(-90.0, "B") == 0,
          "$10 worst case: allowed at -15 and -89.99, blocked at -90.00 (trading stops between -$90 and -$100)")
    check(stake(0, "A+") == 20 and stake(-79.99, "A+") == 20, "A+ $20 allowed while room > $20 (pnl -79.99)")
    check(stake(-80.0, "A+") == 15 and stake(-84.99, "A+") == 15, "A+ at -80.00: $20 refused (would reach -$100), stepped down to $15")
    check(stake(-85.0, "A+") == 10 and stake(-85.0, "A") == 10, "at -85.00: A+/A stepped down to $10")
    check(stake(-90.0, "A+") == 0 and stake(-95.0, "A") == 0, "at -90 or worse: every grade refused")
    t._d["pnl"] = -80.0; st_, note = t.stake_for({"grade": "A+"}); check("stepped down" in note, f"step-down is logged: {note}")
    t._d["pnl"] = -92.0; st_, note = t.stake_for({"grade": "A+"}); check(st_ == 0 and note.startswith("daily loss cap"), f"refusal reason: {note}")
    for pnl in (0, -50, -80, -84.99, -85, -89.99):
        for g in ("B", "A", "A+"):
            s_ = stake(pnl, g); assert s_ == 0 or pnl - s_ > -100
    check(True, "no stake can ever push the day to -$100 (grid check)")

    # ---- live simulation with a fake exchange ----
    class FakeClient:
        key_type = "ed25519"
        def __init__(self): self.orders = {}; self.pos = {}; self.n = 0; self.sent = []
        def build(self, *a): return "", {}, None
        def request(self, method, path, body=None, timeout=10):
            if method == "POST":
                self.n += 1; oid = f"o{self.n}"; cnt = float(body["count"]); self.sent.append(body)
                assert body["reduce_only"] and body["time_in_force"] == "immediate_or_cancel"
                yes_px = float(body["price"]); px = (yes_px if body["side"] == "bid" else 1 - yes_px)   # price in sold outcome... see fills
                self.orders[oid] = {"order_id": oid, "client_order_id": body["client_order_id"], "ticker": body["ticker"],
                                    "count": cnt, "yes_price": yes_px, "status": "executed"}
                t = body["ticker"]; self.pos[t] = self.pos.get(t, 0) - cnt
                return 201, {"order_id": oid, "fill_count": f"{cnt:.2f}"}
            if path.startswith("/portfolio/fills"):
                oid = path.split("order_id=")[1].split("&")[0]; o = self.orders[oid]; c = o["count"]
                return 200, {"fills": [{"count_fp": str(c), "fee_cost": str(F.kalshi_fee(c, o["yes_price"])),
                                        "yes_price_dollars": str(o["yes_price"]), "no_price_dollars": str(round(1 - o["yes_price"], 4))}]}
            if path.startswith("/portfolio/orders?ticker"):
                return 200, {"orders": list(self.orders.values())}
            if path.startswith("/portfolio/orders"): return 200, {"orders": []}
            if path.startswith("/portfolio/positions"):
                return 200, {"market_positions": [{"ticker": k, "position_fp": str(v)} for k, v in self.pos.items() if v]}
            return 200, {}

    d = tempfile.mkdtemp(); L.LSTATE = os.path.join(d, "ls.json"); L.LCSV = os.path.join(d, "lo.csv")
    TK = "KXBTC15M-TEST"
    def fresh():
        lt = Dummy(); lt.cfg = CFGP; lt.tz = __import__("zoneinfo").ZoneInfo("America/Los_Angeles"); lt.dry_run = False
        lt.enabled = True; lt.halted = None; lt.client = FakeClient(); lt.client.pos[TK] = 9
        lt.st = {"days": {}, "windows": {TK: {"entry_coid": "c", "exit_coids": []}}, "open": [
            {"ticker": TK, "outcome": "yes", "qty": 9.0, "cost": 4.41, "fees": 0.16, "entry_px": 0.49,
             "close_ts": time.time() + 600, "entry_qty": 9.0, "entry_total": 4.57, "recovered": 0.0, "realized": 0.0,
             "tp_done": False, "applied_exits": []}]}
        return lt
    now = time.time(); cur = {"ticker": TK, "close_ts": now + 600}
    book = lambda b: {"yes": {"bids": [(b, 100)], "asks": [(b + 0.01, 100)]}, "no": {"bids": [(round(1 - b - 0.01, 2), 100)], "asks": []}}

    lt = fresh(); lt.cfg = CFG
    lt.manage_exits(cur, time.time(), book(0.64))
    check(lt.client.sent == [], "live config: bid 0.64 (+0.15) no exit")
    lt.manage_exits(cur, time.time(), book(0.69))
    check(lt.st["open"] == [] and len(lt.client.sent) == 1 and lt.client.sent[0]["count"] == "9.00", "live config: +0.20 sells all 9 (partial TP off)")
    exp = 9 * 0.69 - F.kalshi_fee(9, 0.69) - 4.57
    check(abs(lt.day()["pnl"] - exp) < 1e-6, f"full TP P&L {lt.day()['pnl']:+.2f}")
    # --- partial-TP code path (CFGP) still correct if re-enabled ---
    lt = fresh()
    lt.manage_exits(cur, time.time(), book(0.64))
    p = lt.st["open"][0]
    check(p["qty"] == 1 and p["tp_done"] and p["recovered"] >= 4.57, f"live TP partial: sold 8, riding {p['qty']:.0f}, recovered ${p['recovered']:.2f}")
    pnl1 = lt.day()["pnl"]; exp1 = 8 * 0.64 - F.kalshi_fee(8, 0.64) - 4.57 * 8 / 9
    check(abs(pnl1 - exp1) < 1e-6, f"partial P&L booked {pnl1:+.4f} (expected {exp1:+.4f})")
    check(abs(p["cost"] + p["fees"] - 4.57 / 9) < 1e-6, "remaining basis = 1/9 of entry cost")
    lt.manage_exits(cur, time.time(), book(0.80))
    check(len(lt.client.sent) == 1, "bid 0.80 after partial: no further sale (no TP re-fire, no SL)")
    lt.manage_exits(cur, time.time(), book(0.93))
    check(lt.st["open"] == [] and len(lt.client.sent) == 2, "CEIL at 0.93 sold the last contract; position closed")
    tot = lt.day()["pnl"]; exp = 8 * 0.64 - F.kalshi_fee(8, 0.64) + 0.93 - F.kalshi_fee(1, 0.93) - 4.57
    check(abs(tot - exp) < 1e-6, f"live total P&L {tot:+.2f} = proceeds - fees - entry cost")
    rows = open(L.LCSV).read().strip().splitlines()
    check(any("EXIT_TP_PARTIAL" in r and "remaining=1" in r for r in rows) and any("EXIT_CEIL" in r for r in rows), "CSV has TP_PARTIAL (remaining=1) and CEIL rows")

    # remainder held to settlement and loses
    lt = fresh(); lt.manage_exits(cur, time.time(), book(0.64))
    lt.st["open"][0]["close_ts"] = time.time() - 1
    lt.settle(lambda t: {"result": "no"})
    tot = lt.day()["pnl"]; exp = 8 * 0.64 - F.kalshi_fee(8, 0.64) - 4.57
    check(lt.st["open"] == [] and abs(tot - exp) < 1e-6, f"remainder loses at settlement: trade total {tot:+.2f} (still >= 0)")

    # no stop-loss: big drop does nothing, held to settlement, stake is max loss
    lt = fresh(); lt.manage_exits(cur, time.time(), book(0.10))
    check(lt.client.sent == [], "bid 0.10: no exit order (no stop-loss)")
    lt.st["open"][0]["close_ts"] = time.time() - 1; lt.settle(lambda t: {"result": "no"})
    check(abs(lt.day()["pnl"] + 4.57) < 1e-6, "loser held to settlement: loss = full stake $4.57")

    # restart/reconcile: exit sent but process died before booking it
    lt = fresh(); fc = lt.client
    w = lt.st["windows"][TK]; cid = L.coid(TK, "exit0"); w["exit_coids"].append(cid)
    fc.request("POST", "/portfolio/events/orders", {"ticker": TK, "client_order_id": cid, "side": "ask", "count": "8.00",
               "price": "0.6400", "time_in_force": "immediate_or_cancel", "reduce_only": True})
    lt.reconcile_startup()
    p = lt.st["open"][0]
    check(p["qty"] == 1 and p["tp_done"] and cid in p["applied_exits"], "reconcile: unrecorded partial exit booked once, riding 1, tp_done")
    before = lt.day()["pnl"]; lt.reconcile_startup()
    check(lt.day()["pnl"] == before and lt.st["open"][0]["qty"] == 1, "second reconcile is idempotent")
    # exchange qty differs from tracked (e.g. manual sale) -> exchange wins
    lt = fresh(); lt.client.pos[TK] = 5; lt.reconcile_startup()
    check(lt.st["open"][0]["qty"] == 5 and abs(lt.st["open"][0]["cost"] - 4.41 * 5 / 9) < 1e-6, "reconcile: exchange qty 5 adopted, basis scaled")
    # legacy position (no new fields) gets normalised
    lt = fresh(); pp = lt.st["open"][0]
    for k in ("entry_qty", "entry_total", "recovered", "realized", "tp_done", "applied_exits"): pp.pop(k)
    lt.manage_exits(cur, time.time(), book(0.64))
    check(lt.st["open"][0]["qty"] == 1, "legacy open position handled by new partial-TP logic")

    # ---- paper ledger: same rules ----
    E.STATE = os.path.join(d, "st.json"); E.TRADES = os.path.join(d, "tr.csv"); E.WINDOWS = os.path.join(d, "w.jsonl")
    cfgp = json.loads(json.dumps(CFGP)); cfgp["live"]["enabled"] = False
    bot = E.KalshiBot(cfgp)
    pos = {"ticker": TK, "window_local": "x", "side": "yes", "contracts": 9, "cost": 4.41, "fee": 0.16, "entry_px": 0.49,
           "grade": "B", "score": 5, "model_p": 0.6}
    pc = {"ticker": TK, "position": pos, "phase": "in_position", "close_ts": time.time() + 600}
    cash0 = bot.state["cash"]; bk = {"v": 0.64}
    E.kalshi.book = lambda t: book(bk["v"])
    bot.check_exits(pc, 600)
    check(pos["contracts"] == 1 and pos.get("tp_done") and pc["phase"] == "in_position", "paper TP partial: sold 8, riding 1")
    bk["v"] = 0.10; bot.check_exits(pc, 500)
    check(pos["contracts"] == 1 and pc["phase"] == "in_position", "paper: no stop-loss at bid 0.10")
    bot.state["pending_settlement"].append(pos); E.kalshi.market = lambda t: {"result": "yes"}
    bot.settle_pending()
    tot = bot.state["realized_pnl"]
    exp = 8 * 0.64 - F.kalshi_fee(8, 0.64) + 1.0 - 4.57
    check(abs(tot - exp) < 1e-6 and abs(bot.state["cash"] - cash0 - (exp + 4.57)) < 1e-6 and bot.state["wins"] == 1 and bot.state["trades"] == 1,
          f"paper remainder settles YES: trade total {tot:+.2f}, counted as 1 winning trade")
    check(any("TP_PARTIAL" in r for r in open(E.TRADES)), "paper trades.csv has TP_PARTIAL row")
    cfgf = json.loads(json.dumps(CFG)); cfgf["live"]["enabled"] = False
    bot2 = E.KalshiBot(cfgf); pos2 = {**pos, "contracts": 9, "cost": 4.41, "fee": 0.16, "entry_px": 0.49}
    for k in ("tp_done", "recovered", "partial_pnl", "entry_total"): pos2.pop(k, None)
    pc2 = {"ticker": TK, "position": pos2, "phase": "in_position", "close_ts": time.time() + 600}
    bk["v"] = 0.64; bot2.check_exits(pc2, 600); check(pc2["phase"] == "in_position", "paper live-config: +0.15 no exit")
    bk["v"] = 0.69; bot2.check_exits(pc2, 600); check(pc2["phase"] == "closed" and pc2["result"]["exit_type"] == "TP", "paper live-config: +0.20 sells all")
    # gates: chop + timing reasons reach the skip list
    bot.m1 = saw; bot.feed.trades = {1: (time.time(), 80001.0, 1.0, True)}; bot.feed.last_ok = time.time()
    gcur = {"open_ts": time.time() - 300, "close_ts": time.time() + 600, "order": None, "position": None, "strike": 80000}
    okg, fails, _ = bot.gates(gcur, time.time(), {"score": 6, "direction": "UP", "votes": votes_up}, "yes",
                              {"bids": [(0.40, 10)], "asks": [(0.41, 10)]}, 0.60)
    check(not okg and any(x.startswith("5 late entry") for x in fails) and any(x.startswith("11 chop") for x in fails),
          f"gates report late-entry and chop: {fails}")

    # ---- 12. order-flow pressure gate (Binance 1m taker delta) ----
    from btcbot import orderflow as OF
    oc = CFG["entry"]["orderflow"]; now = time.time()
    check(oc["enabled"] and oc["block_opposing_usd"] == 1000000 and oc["lookback_sec"] == 60, "config: orderflow gate on, $1M, 60s")
    rd = lambda d, age=2, src="binance", sc=1.0: {"ts": now - age, "src": src, "d60": d, "d180": d * 2, "scale": sc}
    r = OF.check("yes", rd(-1_800_000), now, oc); check(not r[0] and r[1].startswith("12 orderflow"), f"LONG blocked at -$1.8M: {r[1]}")
    r = OF.check("yes", rd(-1_000_000), now, oc); check(not r[0], "LONG blocked at exactly -$1.0M")
    r = OF.check("no", rd(+1_200_000), now, oc); check(not r[0] and r[1].startswith("12 orderflow"), f"SHORT blocked at +$1.2M: {r[1]}")
    check(OF.check("yes", rd(+1_800_000), now, oc)[0], "LONG allowed with +$1.8M aligned buy pressure")
    check(OF.check("no", rd(-1_800_000), now, oc)[0], "SHORT allowed with -$1.8M aligned sell pressure")
    check(OF.check("yes", rd(-900_000), now, oc)[0] and OF.check("no", rd(900_000), now, oc)[0], "opposing $0.9M below threshold allowed")
    r = OF.check("yes", None, now, oc); check(r[0] and "fail open" in r[1], "missing data fails open")
    r = OF.check("no", rd(5_000_000, age=45), now, oc); check(r[0] and "fail open" in r[1], "stale (45s) data fails open")
    check(not OF.check("yes", rd(-600_000, src="coinbase", sc=0.5), now, oc)[0], "Coinbase fallback: -$0.6M blocks LONG (thr x0.5 = $500k)")
    check(OF.check("yes", rd(-5_000_000), now, {**oc, "enabled": False})[0], "gate disabled -> never blocks")
    N0 = 1793340000.0
    rows = [[int((N0 - k) * 1000), "0", "0", "0", "0", "0", 0, "100", 0, "0", "80" if k < 30 else "10", "0"] for k in range(1, 181)]
    dd = OF.deltas_from_klines(rows, N0)
    check(abs(dd[60] - (29 * 60 + 31 * -80)) < 1e-6 and abs(dd[180] - (29 * 60 + 151 * -80)) < 1e-6, f"kline delta math 60s={dd[60]:.0f} 180s={dd[180]:.0f}")
    # wired into gates(): opposing flow appears as gate 12 and counts as a market fail
    bot.of.reading = {"ts": time.time(), "src": "binance", "d60": -2_000_000, "d180": -3_000_000, "scale": 1.0}
    okg, fails, gd = bot.gates(gcur, time.time(), {"score": 6, "direction": "UP", "votes": votes_up}, "yes",
                               {"bids": [(0.40, 10)], "asks": [(0.41, 10)]}, 0.60)
    check(any(x.startswith("12 orderflow") for x in fails) and gd["of_d60"] == -2_000_000, f"gates() includes order-flow block: {fails[-1]}")
    bot.of.reading = None; bot.feed.last_ok = 0
    okg, fails, gd = bot.gates(gcur, time.time(), {"score": 6, "direction": "UP", "votes": votes_up}, "yes",
                               {"bids": [(0.40, 10)], "asks": [(0.41, 10)]}, 0.60)
    check(not any(x.startswith("12") for x in fails), "gates(): no flow data -> gate 12 does not block")

    # ---- grade sizing through LiveTrader.enter (order count / cost) ----
    def sized(grade, pnl=0.0, bal=500.0, limit=0.60):
        lt = fresh(); lt.st = {"days": {}, "windows": {}, "open": []}; lt.max_cost = 20.0; lt.max_trades = 50; lt.loss_cap = 100.0
        lt.day()["pnl"] = pnl; got = {}
        lt.can_enter = lambda cur, sig, now: (True, "ok")
        lt.api = lambda m, path, body=None: (200, {"balance": int(bal * 100)})
        def place(t, o, px, n, cid, tif, expiration=None, reduce_only=False, tag=""): got["n"] = n; return None
        lt.place = place; lt.dry_run = True
        tnow = time.time(); c = {"ticker": "KXBTC15M-SZ" + grade, "open_ts": tnow - 150, "close_ts": tnow + 750}
        lt.enter(c, "yes", limit, {"grade": grade, "score": {"B": 5, "A": 7, "A+": 8}[grade]}, 0.7, tnow)
        per = limit + 0.07 * limit * (1 - limit); return got.get("n", 0), got.get("n", 0) * per
    for g, usd in (("B", 10), ("A", 15), ("A+", 20)):
        n, cost = sized(g); check(usd - 0.62 < cost <= usd - 0.01 + 1e-9, f"enter() {g} @0.60: {n} contracts, cost ${cost:.2f} <= ${usd}")
    n, cost = sized("A+", pnl=-82.0); check(cost <= 15 and n == sized("A")[0], f"enter() A+ at day -82: stepped down to $15 stake ({n} contracts)")
    n, cost = sized("A+", bal=12.0); check(cost <= 12, f"enter() A+ with $12 cash: limited by balance (${cost:.2f})")

    # ---- 2026-10-30 mid-window entries (12:00-9:00 left): TP_MID at net +15%, BE with <=180s left ----
    xm = CFG["exit"]; mw = xm["mid_window"]
    check(mw["enabled"] and mw["entry_remaining_max_sec"] == 900 and mw["entry_remaining_min_sec"] == 0 and mw["tp_pct"] == 0.15
          and mw["breakeven_remaining_sec"] == 180, "config (2026-11-02): exit.mid_window enabled for every entry (900-0s left), tp 15%, breakeven <=180s")
    check(all(E.is_mid_window_entry(s_, xm) for s_ in (900, 780, 720, 600, 540, 400, 360, 0)), "every entry time (13:00 ... 6:00 left) -> tagged for TP_MID/BE")
    check(not E.is_mid_window_entry(None, xm), "unknown time left -> not tagged")
    xold = {**xm, "mid_window": {**mw, "entry_remaining_max_sec": 720, "entry_remaining_min_sec": 540}}
    check(E.is_mid_window_entry(720, xold) and E.is_mid_window_entry(540, xold) and not E.is_mid_window_entry(721, xold)
          and not E.is_mid_window_entry(539, xold), "range bounds still honoured if narrowed (old 12:00-9:00 config)")
    check(not E.is_mid_window_entry(600, {**xm, "mid_window": {**mw, "enabled": False}}), "disabled -> never mid-window")
    # 20 @0.50, entry fee ceil(0.07*20*.25)=0.35 -> all-in 0.5175/contract; +15% target 0.5951 net
    pm = {"entry_px": 0.50, "contracts": 20, "cost": 10.0, "fee": E.kalshi.fee(20, 0.50), "mid_window": True}
    q, b = E.position_basis(pm); check(q == 20 and abs(b - 0.5175) < 1e-9, f"paper basis incl. entry fee = {b:.4f}")
    check(E.exit_kind(pm, 0.61, xm, 500) is None, "mid: bid 0.61 nets < +15% -> hold")
    check(E.exit_kind(pm, 0.62, xm, 500) == "TP_MID", f"mid: bid 0.62 nets {E.net_exit_px(0.62, 20, 0.07):.4f} >= {b*1.15:.4f} -> TP_MID")
    check(E.exit_kind({**pm, "mid_window": False}, 0.62, xm, 500) is None, "same bid on untagged entry -> no exit (TP still +0.20)")
    check(E.exit_kind({**pm, "mid_window": False}, 0.70, xm, 500) == "TP", "untagged entry keeps TP +0.20")
    check(E.exit_kind(pm, 0.54, xm, 181) is None, "mid: breakeven bid with 181s left -> hold")
    check(E.exit_kind(pm, 0.54, xm, 180) == "BE", "mid: bid 0.54 nets >= all-in cost with 180s left -> BE")
    check(E.exit_kind(pm, 0.53, xm, 120) is None, "mid: bid 0.53 nets below all-in cost (fees) -> hold, no loss-taking")
    check(E.exit_kind(pm, 0.50, xm, 120) is None, "mid: bid = entry px is not a true breakeven after fees -> hold")
    check(E.exit_kind(pm, 0.10, xm, 90) is None, "mid: losing position held to settlement (no SL)")
    check(E.exit_kind(pm, 0.93, xm, 500) == "CEIL", "mid: ceiling still first")
    check(E.exit_kind(pm, 0.62, {**xm, "mid_window": {**mw, "enabled": False}}, 500) is None, "rule disabled -> tagged position uses old rules")
    # live-shaped position (qty/fees)
    pl = {"entry_px": 0.60, "qty": 16, "cost": 9.60, "fees": E.kalshi.fee(16, 0.60), "mid_window": True}
    _, bl = E.position_basis(pl)
    check(E.exit_kind(pl, 0.72, xm, 400) is None and E.exit_kind(pl, 0.73, xm, 400) == "TP_MID", f"live mid @0.60 (all-in {bl:.4f}): TP_MID at bid 0.73 not 0.72")
    check(E.exit_kind(pl, 0.64, xm, 170) == "BE" and E.exit_kind(pl, 0.63, xm, 170) is None, "live mid @0.60: BE at bid 0.64 with 170s left, not 0.63")

    # tag persists: LiveTrader.enter stores entry_left_sec/mid_window on the window, poll_entry copies to the position
    def tagged(left):
        lt = fresh(); lt.st = {"days": {}, "windows": {}, "open": []}; lt.max_cost = 20.0; lt.max_trades = 50; lt.loss_cap = 100.0
        lt.can_enter = lambda cur, sig, now: (True, "ok"); lt.dry_run = False; lt.save = lambda: None
        lt.api = lambda m, path, body=None: (200, {"balance": 50000})
        lt.place = lambda *a, **k: {"order_id": "OID"}
        lt.get_order = lambda oid: {"status": "executed", "remaining_count_fp": "0", "fill_count_fp": "16.00"}
        lt.fills = lambda oid, o: (16.0, 9.60, 0.17)
        tnow = time.time(); c = {"ticker": f"KXBTC15M-TAG{left}", "open_ts": tnow - (900 - left), "close_ts": tnow + left}
        lt.enter(c, "yes", 0.60, {"grade": "A", "score": 7}, 0.7, tnow)
        return lt.st["windows"][c["ticker"]], lt.st["open"][0]
    w, p = tagged(600); check(w["mid_window"] and p["mid_window"] and p["entry_left_sec"] == 600, "live entry with 10:00 left tagged mid_window on window + position")
    w, p = tagged(750); check(w["mid_window"] and p["mid_window"], "live entry with 12:30 left now tagged (rule applies to all entries)")
    w, p = tagged(400); check(w["mid_window"] and p["mid_window"] and p["entry_left_sec"] == 400, "live late entry with 6:40 left tagged")
    s_ = json.loads(json.dumps(p)); check("mid_window" in s_ and "entry_left_sec" in s_, "tag survives JSON round-trip (live_state.json)")

    # ---- 2026-11-02 tuning: TP_MID vs +0.20 TP (first wins), min price 0.50, 5% edge for 0.60-0.70 ----
    pe = {"entry_px": 0.50, "contracts": 20, "cost": 10.0, "fee": E.kalshi.fee(20, 0.50), "mid_window": True}
    check(E.exit_kind(pe, 0.62, xm, 700) == "TP_MID", "all-entries: early entry (700s left) exits at net +15% before +0.20")
    pt = {"entry_px": 0.50, "contracts": 20, "cost": 10.0, "fee": E.kalshi.fee(20, 0.50), "mid_window": True}
    xtp = {**xm, "mid_window": {**mw, "tp_pct": 0.60}}
    check(E.exit_kind(pt, 0.70, xtp, 500) == "TP", "if +0.20 comes first (TP_MID not yet), TP still fires")
    check(E.exit_kind(pe, 0.95, xm, 500) == "CEIL", "ceiling 0.92 still first")
    check(E.exit_kind(pe, 0.10, xm, 100) is None and not xm["stop_loss_enabled"], "still no stop-loss")
    check(xm["exit_buffer_sec"] == 60 and xm["take_profit_delta"] == 0.2 and xm["bid_ceiling"] == 0.92, "exit buffer 60s, TP +0.20, ceiling 0.92 unchanged")
    eb = CFG["entry"]
    check(eb["edge_bands"] == [{"min_price": 0.60, "max_price": 0.70, "min_edge": 0.05}], "config: edge band 0.60-0.70 needs 0.05")
    check(E.min_edge_for(0.59, eb) == 0.03 and E.min_edge_for(0.60, eb) == 0.05 and E.min_edge_for(0.65, eb) == 0.05
          and E.min_edge_for(0.70, eb) == 0.05 and E.min_edge_for(0.71, eb) == 0.03 and E.min_edge_for(0.50, eb) == 0.03, "min edge: 0.03 / 0.05 at 0.60, 0.65, 0.70 / 0.03 at 0.71")
    check(E.min_edge_for(0.65, {"min_edge": 0.03}) == 0.03, "no edge_bands -> flat min_edge")
    fee_ = lambda a: 0.07 * a * (1 - a)
    gb = lambda ask, ps: bot.gates(gcur, time.time(), {"score": 6, "direction": "UP", "votes": votes_up}, "yes",
                                   {"bids": [(round(ask - 0.01, 2), 10)], "asks": [(ask, 10)]}, ps)[1]
    f8 = lambda fl: [x for x in fl if x.startswith("8 edge")]
    f7 = lambda fl: [x for x in fl if x.startswith("7 price")]
    check(f7(gb(0.49, 0.9)) and not f7(gb(0.50, 0.9)) and not f7(gb(0.75, 0.95)) and f7(gb(0.76, 0.95)), "price band: 0.49 / 0.76 blocked, 0.50 / 0.75 ok")
    check(f8(gb(0.65, 0.65 + fee_(0.65) + 0.04)) and "< 0.05" in f8(gb(0.65, 0.65 + fee_(0.65) + 0.04))[0], "ask 0.65 with 4% edge blocked (needs 5%)")
    check(not f8(gb(0.65, 0.65 + fee_(0.65) + 0.051)), "ask 0.65 with 5.1% edge passes")
    check(not f8(gb(0.72, 0.72 + fee_(0.72) + 0.04)) and not f8(gb(0.55, 0.55 + fee_(0.55) + 0.04)), "ask 0.72 / 0.55 with 4% edge pass (3% rule)")
    check(f8(gb(0.60, 0.60 + fee_(0.60) + 0.045)) and f8(gb(0.70, 0.70 + fee_(0.70) + 0.045)), "edges 0.60 and 0.70 are inclusive (4.5% blocked)")
    lf = bot.limit_for
    check(lf(0.65 + fee_(0.65) + 0.05 + 1e-6) == 0.65, "limit_for: p 0.65+fee+5% -> limit 0.65")
    _bad = [ps / 1000 for ps in range(500, 1000) if (lambda L_: L_ > 0.50 and ps / 1000 - L_ - fee_(L_) < E.min_edge_for(L_, eb) - 1e-9)(lf(ps / 1000))]
    check(not _bad, "limit_for grid: chosen limit always has its required edge (5% inside 0.60-0.70)")
    check(lf(0.76) == 0.71, "limit_for: p 0.76 -> 0.71 (3% band), not 0.72")
    check(lf(0.58 + fee_(0.58) + 0.03 + 1e-6) == 0.58, "limit_for: p 0.58+fee+3% -> 0.58")
    check(lf(0.40) == 0.50, "limit_for floor is the new 0.50 min price")
    # sizing / caps / order-flow untouched
    check(L.GRADE_SIZE_USD == {"B": 10.0, "A": 15.0, "A+": 20.0} and CFG["risk"]["daily_loss_cap_usd"] == 100 and CFG["risk"]["max_trades_per_day"] == 50
          and CFG["entry"]["orderflow"]["enabled"] and CFG["entry"]["orderflow"]["block_opposing_usd"] == 1000000, "grade sizing, caps, order-flow unchanged")

    print(f"\nALL {ok} CHECKS PASSED")

    return ok


class YoungBuffettRulesTest(unittest.TestCase):
    def test_all_rules(self):
        n = run_checks()
        self.assertGreater(n, 50)


if __name__ == "__main__":
    unittest.main()
