import csv, json, logging, os, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import kalshi, factors, orderflow
from .feed import Feed, candles
from .kexecutor import KalshiPaperExecutor
from .live import LiveTrader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data_kalshi")
STATE = os.path.join(DATA, "state.json")
TRADES = os.path.join(DATA, "trades.csv")
WINDOWS = os.path.join(DATA, "windows.jsonl")
DECISIONS = os.path.join(DATA, "decisions.jsonl")
HEARTBEAT = os.path.join(ROOT, "logs", "kalshi_heartbeat.json")
GAP_SEC = 60          # wall-clock gap between loops that means the process/box was paused
STALE_TICK_SEC = 20   # a tick whose data fetch took longer than this is discarded
log = logging.getLogger("kalshi-btc15m")
TF = ["window_local", "ticker", "side", "grade", "score", "entry_px", "contracts", "entry_cost", "entry_fee",
      "exit_type", "exit_px", "exit_proceeds", "exit_fee", "pnl", "cash_after", "model_p", "signal"]


def load_cfg(path=os.path.join(ROOT, "config_kalshi.json")):
    return json.load(open(path))


def is_mid_window_entry(left_at_entry, x):
    """True if an entry decided with `left_at_entry` seconds remaining falls in the exit.mid_window band
    (inclusive; 2026-11-02 config 0..900s = every entry). Disabled/missing config -> False."""
    m = (x or {}).get("mid_window") or {}
    if not m.get("enabled") or left_at_entry is None: return False
    return m.get("entry_remaining_min_sec", 540) <= left_at_entry <= m.get("entry_remaining_max_sec", 720)


def min_edge_for(price, e):
    """Minimum edge required at an entry price: entry.edge_bands [{min_price, max_price, min_edge}] (inclusive,
    first match wins; 2026-11-02: 0.05 for 0.60-0.70), else entry.min_edge."""
    for b in e.get("edge_bands") or []:
        if b["min_price"] - 1e-9 <= price <= b["max_price"] + 1e-9: return b["min_edge"]
    return e["min_edge"]


def position_basis(p):
    """(remaining contracts, all-in entry cost per contract incl. entry fee) for paper or live position dicts."""
    q = p.get("qty", p.get("contracts")) or 0
    fees = p.get("fees", p.get("fee", 0.0))
    if q <= 0: return 0, p["entry_px"]
    return q, (p.get("cost", p["entry_px"] * q) + fees) / q


def net_exit_px(bid, qty, rate):
    """Per-contract sale proceeds after the taker fee (Kalshi fee rounded up to the cent per order)."""
    from .kalshi import fee
    return bid - (fee(qty, bid, rate) / qty if qty else rate * bid * (1 - bid))


def exit_kind(p, bid, x, left=None, rate=0.07):
    """Exit rule (2026-10-29): no stop-loss (losers ride to settlement; stake = max loss).
    TP (+take_profit_delta) once -> sells the position (partial_tp=false); CEIL (bid >= bid_ceiling) sells the rest.
    stop_loss_enabled=true in config restores the old SL.
    2026-10-30 mid-window rule (exit.mid_window, positions tagged mid_window=True = entered with 12:00-9:00 left):
      TP_MID: sell all once the sale would net >= +tp_pct on the all-in entry cost (both fees counted);
      BE: if not yet, with <= breakeven_remaining_sec left, sell all as soon as the sale nets >= all-in entry cost.
      Otherwise held to settlement (no stop-loss). CEIL still applies first."""
    if bid >= x["bid_ceiling"]: return "CEIL"
    m = x.get("mid_window") or {}
    if p.get("mid_window") and m.get("enabled"):
        q, basis = position_basis(p)
        net = net_exit_px(bid, q, rate)
        if net >= basis * (1 + m.get("tp_pct", 0.15)) - 1e-9: return "TP_MID"
        if left is not None and left <= m.get("breakeven_remaining_sec", 180) and net >= basis - 1e-9: return "BE"
    if not p.get("tp_done") and bid >= p["entry_px"] + x["take_profit_delta"]: return "TP"
    if x.get("stop_loss_enabled", False) and bid <= p["entry_px"] - x["stop_loss_delta"]: return "SL"
    return None


def session_of(ts):
    h = datetime.fromtimestamp(ts, timezone.utc).hour
    if 13 <= h < 21: return "ny"        # ~9:00-17:00 New York (EDT)
    if 7 <= h < 13: return "london"
    if 0 <= h < 7: return "asia"
    return "off"


class KalshiBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg["timezone"])
        self.ex = KalshiPaperExecutor(cfg["fees"]["taker_rate"], cfg["fees"]["maker_rate"])
        os.makedirs(DATA, exist_ok=True)
        self.state = json.load(open(STATE)) if os.path.exists(STATE) else {
            "cash": cfg["starting_budget_usd"], "starting_budget": cfg["starting_budget_usd"],
            "realized_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0, "windows_seen": 0,
            "loss_streak": 0, "cooldown_until_window": 0, "days": {}, "current": None,
            "pending_settlement": [], "last_update": None}
        self.feed = Feed(keep_sec=1200)
        self.m1 = []; self.m15 = []; self.c_ts = 0
        self.last_eval = 0
        self.live = LiveTrader(cfg)
        self.kill_logged = False
        self.of = orderflow.OrderFlow(cfg, self.feed); self.of_warn = 0

    # ---- helpers ----
    def local(self, t): return datetime.fromtimestamp(t, self.tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    def day(self, t=None):
        k = datetime.fromtimestamp(t or time.time(), self.tz).strftime("%Y-%m-%d")
        return self.state["days"].setdefault(k, {"trades": 0, "pnl": 0.0})
    def save(self):
        self.state["last_update"] = datetime.now(self.tz).isoformat(timespec="seconds")
        tmp = STATE + ".tmp"; json.dump(self.state, open(tmp, "w"), indent=2); os.replace(tmp, STATE)
    def jl(self, path, obj):
        with open(path, "a") as f: f.write(json.dumps(obj) + "\n")

    def refresh_data(self, now):
        self.feed.poll()
        if now - self.c_ts > 20:
            self.m1 = candles(60); self.m15 = candles(900); self.c_ts = now

    # ---- loop ----
    def run(self):
        log.info("Kalshi paper bot started. cash=%.2f series=%s", self.state["cash"], self.cfg["series_ticker"])
        e, x, c = self.cfg["entry"], self.cfg["exit"], self.cfg.get("chop", {})
        log.info("RULES: entry t+%ds..%ds any grade>=B; later only score>=%d with >=%ds left (hard end t+%ds) | "
                 "chop filter=%s (trend+momentum must agree, ER>=%.2f, %dm range>=%.1fxATR) | stop-loss=%s | "
                 "TP +%.2f partial=%s (recover entry cost, ride rest) | ceiling %.2f | applies to live + paper",
                 e["entry_window_start_sec"], e.get("early_entry_end_sec", 240), e.get("late_entry_min_score", 7),
                 e.get("late_entry_min_left_sec", 360), e["entry_window_end_sec"], c.get("enabled", True), c.get("er_min", 0.25),
                 c.get("lookback_min", 10), c.get("range_atr_min", 1.8), "ON" if x.get("stop_loss_enabled") else "OFF",
                 x["take_profit_delta"], x.get("partial_tp", True), x["bid_ceiling"])
        mw = x.get("mid_window") or {}
        log.info("RULES: entry price band %.2f-%.2f, min edge %.2f%s", e["min_price"], e["max_price"], e["min_edge"],
                 "".join(f", {b['min_edge']:.2f} for {b['min_price']:.2f}-{b['max_price']:.2f}" for b in e.get("edge_bands") or []))
        log.info("RULES: mid-window exit=%s: entries with %d-%ds left sell all at net +%.0f%% on all-in cost (fees counted), "
                 "else with <=%ds left sell all at net breakeven; TP +0.20 / CEIL also apply, first wins; otherwise hold (no SL; no exits in last %ds)",
                 "ON" if mw.get("enabled") else "OFF", mw.get("entry_remaining_max_sec", 720), mw.get("entry_remaining_min_sec", 540),
                 100 * mw.get("tp_pct", 0.15), mw.get("breakeven_remaining_sec", 180), x["exit_buffer_sec"])
        oc = e.get("orderflow", {})
        log.info("RULES: order-flow filter=%s: block LONG if Binance BTCUSDT %ds taker delta <= -$%.2fM, SHORT if >= +$%.2fM "
                 "(Coinbase fallback x%.2f; fails open if data missing/stale > %ds)", "ON" if oc.get("enabled") else "OFF",
                 oc.get("lookback_sec", 60), oc.get("block_opposing_usd", 0) / 1e6, oc.get("block_opposing_usd", 0) / 1e6,
                 oc.get("coinbase_scale", 0.5), oc.get("max_age_sec", 30))
        self.of.start()
        from . import live as _L
        log.info("RULES: live sizing by grade: B $%.0f / A $%.0f / A+ $%.0f max cost per trade (hard max $%.0f); stake steps down "
                 "20->15->10 or is refused if the full stake could reach the -$%.0f daily loss cap",
                 *(self.live.size_for({"grade": g}) for g in ("B", "A", "A+")), _L.HARD_MAX_COST_PER_TRADE, self.live.loss_cap)
        try:
            s = kalshi.series(self.cfg["series_ticker"])
            log.info("Series %s: fee_type=%s multiplier=%s settlement=%s", s["ticker"], s["fee_type"],
                     s["fee_multiplier"], [x["name"] for x in s["settlement_sources"]])
        except Exception as e:
            log.warning("series lookup failed: %s", e)
        self.feed.backfill(pages=6)
        last = time.time(); last_hb_log = 0
        while True:
            t0 = time.time()
            if t0 - last > GAP_SEC:
                log.warning("RESUMED after a %.0fs gap in the loop (process or box was paused); refreshing data", t0 - last)
                try: self.feed.backfill(pages=6)
                except Exception as e: log.warning("backfill after gap failed: %s", e)
            try:
                self.tick()
            except Exception as e:
                log.exception("tick error: %s", e)
            last = time.time()
            self.heartbeat(last, t0)
            if last - last_hb_log >= 60:
                last_hb_log = last
                cur = self.state.get("current") or {}
                log.info("HEARTBEAT live_active=%s halted=%s window=%s tick=%.1fs", self.live.active,
                         self.live.halted, cur.get("ticker"), last - t0)
            time.sleep(self.cfg["poll_interval_sec"])

    def heartbeat(self, now, t0):
        try:
            cur = self.state.get("current") or {}
            hb = {"ts": round(now, 1), "local": self.local(now), "pid": os.getpid(), "tick_sec": round(now - t0, 2),
                  "live_active": self.live.active, "halted": self.live.halted, "window": cur.get("ticker")}
            os.makedirs(os.path.dirname(HEARTBEAT), exist_ok=True); tmp = HEARTBEAT + ".tmp"; json.dump(hb, open(tmp, "w")); os.replace(tmp, HEARTBEAT)
        except Exception as e:
            log.warning("heartbeat write failed: %s", e)

    def tick(self):
        now = time.time()
        if self.live.kill_check():
            if not self.kill_logged: log.error("KILL file present: live orders cancelled, bot halted (remove KILL and run './kalshi_bot.py resume-live' to resume)")
            self.kill_logged = True; self.save(); return
        self.kill_logged = False
        self.live.try_recover_auth()
        self.refresh_data(now)
        if time.time() - now > STALE_TICK_SEC:
            log.warning("tick discarded: data refresh took %.0fs (pause or slow network)", time.time() - now); return
        self.settle_pending()
        if self.live.enabled: self.live.settle(kalshi.market)
        cur = self.state["current"]
        if cur and now >= cur["close_ts"]:
            self.end_window(cur); cur = None
        if cur is None:
            m = kalshi.current_market(self.cfg["series_ticker"], now)
            if not m:
                log.warning("no open KXBTC15M market right now"); self.save(); return
            cur = self.begin_window(m)
        self.manage(cur, now)
        self.save()

    def begin_window(self, m):
        o, c = kalshi.ts(m["open_time"]), kalshi.ts(m["close_time"])
        cur = {"ticker": m["ticker"], "open_ts": o, "close_ts": c, "window_local": self.local(o),
               "strike": m.get("floor_strike"), "strike_cb": None, "phase": "scanning",
               "decisions": [], "order": None, "position": None, "result": None}
        self.state["current"] = cur
        log.info("NEW WINDOW %s %s -> %s strike=%s", m["ticker"], self.local(o), self.local(c), cur["strike"])
        return cur

    def ensure_strike(self, cur):
        if cur["strike"] is None:
            cur["strike"] = kalshi.market(cur["ticker"]).get("floor_strike")
        if cur["strike"] and cur["strike_cb"] is None:
            # Kalshi strike = 60s avg of CF BRTI before open. Use Coinbase's 60s VWAP over the same span
            # as the comparable reference (removes cross-source basis); fall back to the strike itself.
            vw = self.feed.vwap_window(cur["open_ts"] - 60, cur["open_ts"])
            cur["strike_cb"] = vw or cur["strike"]
            cur["basis"] = round((vw - cur["strike"]), 2) if vw else None

    def gates(self, cur, now, sig, side, sb, p_side):
        """10-gate risk filter. Returns (ok, list_of_failures, details)."""
        r, e = self.cfg["risk"], self.cfg["entry"]; d = self.day(); fails = []
        t_in = now - cur["open_ts"]
        if d["pnl"] <= -abs(r["daily_loss_cap_usd"]): fails.append("1 daily loss cap")
        if r["daily_profit_cap_usd"] and d["pnl"] >= r["daily_profit_cap_usd"]: fails.append("2 daily profit cap")
        if d["trades"] >= r["max_trades_per_day"]: fails.append("3 max trades/day")
        if cur["order"] or cur["position"]: fails.append("4 one entry per window")
        t_ok, t_why = factors.entry_timing(t_in, cur["close_ts"] - now, sig["score"], e)
        if not t_ok: fails.append(t_why)
        bid = sb["bids"][0][0] if sb["bids"] else 0.0
        ask = sb["asks"][0][0] if sb["asks"] else 1.0
        spread = ask - bid
        if spread > r["max_spread"]: fails.append(f"6 spread {spread:.3f}")
        if not (e["min_price"] <= ask <= e["max_price"]): fails.append(f"7 price band (ask {ask})")
        fee_pc = self.cfg["fees"]["taker_rate"] * ask * (1 - ask)
        edge = p_side - ask - fee_pc
        need = min_edge_for(ask, e)
        if edge < need: fails.append(f"8 edge {edge:+.3f} < {need}")
        if now - self.feed.last_ok > r["max_data_age_sec"] or not cur["strike"]: fails.append("9 stale data / no strike")
        widx = int(cur["open_ts"] // 900)
        if widx < self.state.get("cooldown_until_window", 0): fails.append("10 loss-streak cooldown")
        if session_of(now) not in r["allowed_sessions"]: fails.append("10b session not allowed")
        if self.state["cash"] < 1: fails.append("no budget")
        c_ok, c_why, cm = factors.chop_check(sig, self.m1, self.feed.spot(), self.cfg)
        if not c_ok: fails.append(c_why)
        o_ok, o_why, om = orderflow.check(side, self.of.get(now), now, e.get("orderflow", {}))
        if not o_ok: fails.append(o_why)
        elif o_why.startswith("orderflow unavailable") and now - self.of_warn >= 60:
            self.of_warn = now; log.warning("orderflow data unavailable/stale: gate failing OPEN (not blocking)")
        return (not fails), fails, {"bid": bid, "ask": ask, "spread": round(spread, 4), "edge": round(edge, 4), **cm, **om}

    def manage(self, cur, now):
        t_in = now - cur["open_ts"]; left = cur["close_ts"] - now
        e = self.cfg["entry"]
        if self.live.active:
            self.live.poll_entry(cur, now)
            if any(p["ticker"] == cur["ticker"] for p in self.live.st["open"]):
                self.live.manage_exits(cur, now, kalshi.book(cur["ticker"]))
        if cur["phase"] == "scanning" and t_in >= e["entry_window_start_sec"]:
            if t_in > self.last_entry_sec(cur):
                cur["phase"] = "no_trade"
                log.info("%s: entry window closed with no trade", cur["ticker"]); return
            if now - self.last_eval < self.cfg["signal_eval_interval_sec"]: return
            self.last_eval = now
            self.ensure_strike(cur)
            if not cur["strike"] or len(self.m1) < 70: return
            spot = self.feed.spot()
            bars10 = self.feed.bars(10)
            sig = factors.compute(spot, cur["strike_cb"], bars10, self.m1, self.m15,
                                  self.feed.flow(self.cfg["signal"]["orderflow_window_sec"]), self.cfg)
            sigma = factors.realized_sigma(self.m1, self.cfg["model"]["vol_lookback_min"], self.cfg["model"]["min_sigma_per_sec"])
            pu = factors.prob_up(spot, cur["strike_cb"], sigma, left, sig["net"], self.cfg["model"]["score_tilt_k"])
            bk = kalshi.book(cur["ticker"])
            dec = {"t_local": self.local(now), "t_in": int(t_in), "spot": round(spot, 2),
                   "strike": cur["strike"], "strike_cb": round(cur["strike_cb"], 2), "p_up": round(pu, 3),
                   "yes_bid": bk["yes"]["bids"][0][0] if bk["yes"]["bids"] else None,
                   "yes_ask": bk["yes"]["asks"][0][0] if bk["yes"]["asks"] else None,
                   "session": session_of(now), **sig}
            ofr = self.of.get(now)
            dec["of_src"] = ofr["src"] if ofr else None
            dec["of_d60"] = round(ofr["d60"]) if ofr else None
            dec["of_d180"] = round(ofr["d180"]) if ofr else None
            label = f"{'LONG' if sig['direction']=='UP' else 'SHORT' if sig['direction'] else 'FLAT'} {sig['grade'] or '-'} {sig['score']}/11"
            dec["label"] = label
            min_g = self.cfg["signal"]["min_grade"]
            if not sig["direction"] or factors.GRADE_RANK[sig["grade"]] < factors.GRADE_RANK[min_g]:
                dec["action"] = "WAIT"; dec["why"] = f"grade below {min_g}"
            else:
                side = "yes" if sig["direction"] == "UP" else "no"
                p_side = pu if side == "yes" else 1 - pu
                ok, fails, gd = self.gates(cur, now, sig, side, bk[side], p_side)
                dec.update(gd)
                market_fails = [x for x in fails if x[0] in "56789" or x.startswith("10") or x.startswith("11") or x.startswith("12")]
                sig["entry_deadline_ts"] = cur["open_ts"] + factors.entry_deadline_sec(
                    sig["score"], cur["close_ts"] - cur["open_ts"], e)
                if ok:
                    dec["action"] = f"BUY {side.upper()}"; dec["why"] = "grade+gates pass"
                    self.enter(cur, side, bk[side], p_side, sig, dec)
                else:
                    dec["action"] = "WAIT"; dec["why"] = "; ".join(fails)
                if self.live.enabled and not market_fails:
                    dec["live"] = self.live.enter(cur, side, self.limit_for(p_side), sig, p_side, now)
            cur["decisions"].append(dec); self.jl(DECISIONS, {"ticker": cur["ticker"], **dec})
            ofs = (f"{dec['of_d60']/1e6:+.2f}M/3m {dec['of_d180']/1e6:+.2f}M {dec['of_src']}" if dec["of_d60"] is not None else "n/a")
            log.info("SIGNAL %s t+%ds %s | p_up=%.3f yes %s/%s | %s (%s) Δ=%sbps | flow1m=%s", cur["ticker"], t_in, label, pu,
                     dec["yes_bid"], dec["yes_ask"], dec["action"], dec["why"], sig["delta_bps"], ofs)
        elif cur["phase"] == "order_open":
            o = cur["order"]; sb = kalshi.book(cur["ticker"])[o["side"]]
            self.ex.poll_resting(o, sb)
            dl = o.get("deadline_sec", e["entry_window_end_sec"])
            if o["status"] == "filled" or (t_in > dl and o["filled"] > 0):
                self.ex.cancel(o); self.open_position(cur)
            elif t_in > dl:
                self.ex.cancel(o); cur["phase"] = "no_fill"
                log.info("%s: resting %s @%.2f not filled, cancelled", cur["ticker"], o["side"], o["limit"])
        if cur["phase"] == "in_position":
            self.check_exits(cur, left)

    def enter(self, cur, side, sb, p_side, sig, dec):
        lim = self.limit_for(p_side)
        usd = min(self.cfg["per_trade_usd"], self.state["cash"])
        n = int(usd // lim)
        if n < 1: return
        o = self.ex.place_limit_buy(side, lim, n, sb)
        left_now = int(round(cur["close_ts"] - cur["open_ts"] - dec["t_in"])) if "t_in" in dec else int(round(cur["close_ts"] - time.time()))
        o.update({"grade": sig["grade"], "score": sig["score"], "model_p": round(p_side, 3), "signal": dec,
                  "entry_left_sec": left_now, "mid_window": is_mid_window_entry(left_now, self.cfg["exit"]),
                  "deadline_sec": factors.entry_deadline_sec(sig["score"], cur["close_ts"] - cur["open_ts"], self.cfg["entry"])})
        cur["order"] = o; cur["phase"] = "order_open"; self.day()["trades"] += 1
        log.info("PAPER BUY %s %s x%d limit %.2f (filled now %d, fee $%.2f)", cur["ticker"], side.upper(), n, lim, o["filled"], o["fee"])
        if o["status"] == "filled": self.open_position(cur)

    def last_entry_sec(self, cur):
        """Latest t_in at which any entry is still allowed (A/A+ late entries need late_entry_min_left_sec left)."""
        e = self.cfg["entry"]
        return min(e["entry_window_end_sec"], (cur["close_ts"] - cur["open_ts"]) - e.get("late_entry_min_left_sec", 360))

    def limit_for(self, p_side):
        """Highest whole-cent price that still leaves the required edge (min_edge_for: edge_bands, else min_edge) after taker fee, within [min_price, max_price]."""
        e = self.cfg["entry"]; lim = e["min_price"]
        for c in range(int(round(e["max_price"] * 100)), int(round(e["min_price"] * 100)) - 1, -1):
            p = c / 100
            if p_side - p - self.cfg["fees"]["taker_rate"] * p * (1 - p) >= min_edge_for(p, e):
                lim = p; break
        return lim

    def open_position(self, cur):
        o = cur["order"]
        self.state["cash"] -= o["cost"] + o["fee"]
        cur["position"] = {"ticker": cur["ticker"], "window_local": cur["window_local"], "side": o["side"],
                           "contracts": o["filled"], "cost": o["cost"], "fee": o["fee"],
                           "entry_px": o["cost"] / o["filled"], "grade": o["grade"], "score": o["score"],
                           "model_p": o["model_p"], "entry_left_sec": o.get("entry_left_sec"),
                           "mid_window": bool(o.get("mid_window"))}
        cur["phase"] = "in_position"
        log.info("FILLED %s %s x%d avg %.3f fee $%.2f%s", cur["ticker"], o["side"].upper(), o["filled"], cur["position"]["entry_px"], o["fee"],
                 f" [MID-WINDOW entry, {o.get('entry_left_sec')}s left: TP_MID/BE rules]" if o.get("mid_window") else "")

    def check_exits(self, cur, left):
        x = self.cfg["exit"]; p = cur["position"]
        if left <= x["exit_buffer_sec"]: return  # hold to settlement in the final buffer
        sb = kalshi.book(cur["ticker"])[p["side"]]
        if not sb["bids"]: return
        bid = sb["bids"][0][0]
        p.setdefault("entry_total", p["cost"] + p["fee"]); p.setdefault("recovered", 0.0)
        kind = exit_kind(p, bid, x, left, self.cfg["fees"]["taker_rate"])
        if not kind: return
        qty = p["contracts"]
        if kind == "TP" and x.get("partial_tp", True):
            qty = factors.partial_tp_qty(p["contracts"], bid, p["entry_total"] - p["recovered"], self.cfg["fees"]["taker_rate"])
            if qty <= 0: p["tp_done"] = True; return
        avg, proceeds, f, sold = self.ex.sell_taker(qty, sb)
        if sold <= 0: return
        if sold < p["contracts"]:
            self.partial_close(p, "TP_PARTIAL" if kind == "TP" else kind, avg, proceeds, f, sold)
            if kind == "TP" and p["recovered"] >= p["entry_total"] - 1e-9: p["tp_done"] = True
            return
        cur["result"] = self.close(p, kind, avg, proceeds, f); cur["phase"] = "closed"

    def partial_close(self, p, kind, px, proceeds, f, sold):
        """Sell part of a paper position: realize P&L on the sold fraction, keep the rest riding."""
        frac = sold / p["contracts"]; st = self.state
        basis_c, basis_f = p["cost"] * frac, p["fee"] * frac
        pnl = proceeds - f - basis_c - basis_f
        st["cash"] += proceeds - f; st["realized_pnl"] += pnl; self.day()["pnl"] += pnl
        p["recovered"] = p.get("recovered", 0.0) + proceeds - f
        p["partial_pnl"] = p.get("partial_pnl", 0.0) + pnl
        p["contracts"] -= sold; p["cost"] -= basis_c; p["fee"] -= basis_f
        self.write_trade_row(p, kind, px, proceeds, f, pnl, sold)
        log.info("PARTIAL %s %s %s sold %d @%.3f pnl=%+.2f (recovered $%.2f of $%.2f) riding %d cash=%.2f",
                 p["ticker"], p["side"].upper(), kind, sold, px, pnl, p["recovered"], p["entry_total"], p["contracts"], st["cash"])

    def close(self, p, kind, px, proceeds, f):
        pnl = proceeds - f - p["cost"] - p["fee"]; st = self.state
        st["cash"] += proceeds - f; st["realized_pnl"] += pnl; st["trades"] += 1
        total = pnl + p.get("partial_pnl", 0.0)   # whole-trade result (partial TP + remainder)
        if total > 0: st["wins"] += 1; st["loss_streak"] = 0
        else:
            st["losses"] += 1; st["loss_streak"] += 1
            r = self.cfg["risk"]
            if st["loss_streak"] >= r["cooldown_after_losses"]:
                st["cooldown_until_window"] = int(time.time() // 900) + 1 + r["cooldown_windows"]; st["loss_streak"] = 0
        self.day()["pnl"] += pnl
        self.write_trade_row(p, kind, px, proceeds, f, pnl, p["contracts"])
        log.info("CLOSE %s %s %s @%.3f pnl=%+.2f%s cash=%.2f", p["ticker"], p["side"].upper(), kind, px, pnl,
                 f" (trade total {total:+.2f} incl. partial)" if p.get("partial_pnl") else "", st["cash"])
        return {"exit_type": kind, "exit_px": px, "proceeds": proceeds, "exit_fee": f, "pnl": pnl, "trade_total_pnl": total}

    def write_trade_row(self, p, kind, px, proceeds, f, pnl, contracts):
        new = not os.path.exists(TRADES)
        with open(TRADES, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=TF)
            if new: w.writeheader()
            w.writerow({"window_local": p["window_local"], "ticker": p["ticker"], "side": p["side"], "grade": p["grade"],
                        "score": p["score"], "entry_px": round(p["entry_px"], 4), "contracts": contracts,
                        "entry_cost": round(p["cost"], 4), "entry_fee": round(p["fee"], 4), "exit_type": kind,
                        "exit_px": round(px, 4), "exit_proceeds": round(proceeds, 4), "exit_fee": f,
                        "pnl": round(pnl, 4), "cash_after": round(self.state["cash"], 4), "model_p": p["model_p"], "signal": ""})

    def end_window(self, cur):
        if self.live.active: self.live.end_window(cur)
        if cur["phase"] == "order_open":
            self.ex.cancel(cur["order"])
            if cur["order"]["filled"] > 0: self.open_position(cur)
            else: cur["phase"] = "no_fill"
        if cur["phase"] == "in_position":
            cur["phase"] = "held_to_settlement"; self.state["pending_settlement"].append(cur["position"])
        summary = {k: cur[k] for k in ("ticker", "window_local", "strike", "strike_cb", "phase", "result")}
        summary["n_decisions"] = len(cur["decisions"])
        summary["last_decision"] = {k: cur["decisions"][-1].get(k) for k in ("t_local", "label", "action", "why", "p_up")} if cur["decisions"] else None
        if cur.get("order"): summary["order"] = {k: cur["order"][k] for k in ("side", "limit", "contracts", "filled", "status")}
        self.jl(WINDOWS, summary); self.state["windows_seen"] += 1; self.state["current"] = None
        log.info("WINDOW END %s phase=%s", cur["ticker"], summary["phase"])

    def settle_pending(self):
        keep = []
        for p in self.state["pending_settlement"]:
            try: m = kalshi.market(p["ticker"])
            except Exception: keep.append(p); continue
            if m.get("result") not in ("yes", "no"): keep.append(p); continue
            won = m["result"] == p["side"]
            res = self.close(p, f"SETTLED_{m['result'].upper()}", 1.0 if won else 0.0, p["contracts"] * (1.0 if won else 0.0), 0.0)
            self.jl(WINDOWS, {"settlement": p["ticker"], "result": m["result"], "expiration_value": m.get("expiration_value"),
                              "strike": m.get("floor_strike"), **res})
        self.state["pending_settlement"] = keep
