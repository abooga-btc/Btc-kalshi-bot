import csv, json, logging, os, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import polymarket as pm, prices, signal
from .executor import PaperExecutor, LiveExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
STATE = os.path.join(DATA, "state.json")
TRADES = os.path.join(DATA, "trades.csv")
WINDOWS = os.path.join(DATA, "windows.jsonl")
log = logging.getLogger("btc15m")

TRADE_FIELDS = ["window_start_utc", "window_local", "slug", "side", "entry_px", "shares", "entry_cost",
                "entry_fee", "exit_type", "exit_px", "exit_proceeds", "exit_fee", "pnl", "cash_after",
                "signal_detail"]


def load_cfg(path=os.path.join(ROOT, "config.json")):
    with open(path) as f:
        return json.load(f)


def new_state(cfg):
    return {"cash": cfg["starting_budget_usd"], "starting_budget": cfg["starting_budget_usd"],
            "realized_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0, "windows_seen": 0,
            "days": {}, "current": None, "pending_resolution": [], "last_update": None}


class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        if not cfg.get("paper", True):
            self.ex = LiveExecutor()  # raises: live disabled
        self.ex = PaperExecutor(cfg["fees"]["crypto_taker_rate"])
        self.tz = ZoneInfo(cfg.get("timezone", "America/Los_Angeles"))
        os.makedirs(DATA, exist_ok=True)
        self.state = json.load(open(STATE)) if os.path.exists(STATE) else new_state(cfg)
        self.last_signal_eval = 0

    # ---------- persistence ----------
    def save(self):
        self.state["last_update"] = datetime.now(self.tz).isoformat(timespec="seconds")
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, STATE)

    def local(self, ts):
        return datetime.fromtimestamp(ts, self.tz).strftime("%Y-%m-%d %H:%M %Z")

    def day(self, ts=None):
        k = datetime.fromtimestamp(ts or time.time(), self.tz).strftime("%Y-%m-%d")
        return self.state["days"].setdefault(k, {"trades": 0, "pnl": 0.0})

    def write_trade(self, pos, cur):
        new = not os.path.exists(TRADES)
        with open(TRADES, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
            if new: w.writeheader()
            w.writerow({"window_start_utc": pm.iso(pos["start"]), "window_local": self.local(pos["start"]),
                        "slug": pos["slug"], "side": pos["side"], "entry_px": round(pos["entry_px"], 4),
                        "shares": round(pos["shares"], 4), "entry_cost": round(pos["cost"], 4),
                        "entry_fee": round(pos["fee"], 4), "exit_type": cur["exit_type"],
                        "exit_px": round(cur["exit_px"], 4), "exit_proceeds": round(cur["proceeds"], 4),
                        "exit_fee": round(cur["exit_fee"], 4), "pnl": round(cur["pnl"], 4),
                        "cash_after": round(self.state["cash"], 4),
                        "signal_detail": json.dumps(pos.get("signal", {}))})

    def finish_window(self, cur):
        with open(WINDOWS, "a") as f:
            f.write(json.dumps(cur) + "\n")
        self.state["windows_seen"] += 1

    def close_position(self, pos, exit_type, exit_px, proceeds, exit_fee):
        pnl = proceeds - exit_fee - pos["cost"] - pos["fee"]
        st = self.state
        st["cash"] += proceeds - exit_fee
        st["realized_pnl"] += pnl
        st["trades"] += 1
        st["wins" if pnl > 0 else "losses"] += 1
        self.day(pos["start"])["pnl"] += pnl
        res = {"exit_type": exit_type, "exit_px": exit_px, "proceeds": proceeds, "exit_fee": exit_fee, "pnl": pnl}
        self.write_trade(pos, res)
        log.info("CLOSE %s %s %s @%.3f pnl=%+.2f cash=%.2f", pos["slug"], pos["side"], exit_type,
                 exit_px, pnl, st["cash"])
        return res

    # ---------- main loop ----------
    def run(self):
        log.info("Bot started (mode=%s). cash=%.2f", self.ex.name, self.state["cash"])
        while True:
            try:
                self.tick()
            except Exception as e:
                log.exception("tick error: %s", e)
            time.sleep(self.cfg["poll_interval_sec"])

    def tick(self):
        now = time.time()
        start = pm.window_start(now)
        self.settle_pending()
        cur = self.state["current"]
        if cur and cur["start"] != start:
            self.end_window(cur)
            cur = None
        if cur is None:
            cur = self.begin_window(start)
            if cur is None:
                return
        self.manage(cur, now)
        self.save()

    def begin_window(self, start):
        mkt = pm.get_market(start)
        if not mkt:
            log.warning("No market found for %s", pm.slug_for(start))
            return None
        ptb = (mkt.get("event_metadata") or {}).get("priceToBeat")  # official, when published
        try:
            ptb = ptb or pm.price_to_beat(start)[0]
        except Exception as e:
            log.warning("price_to_beat lookup failed: %s", e)
        cur = {"start": start, "window_local": self.local(start), "slug": mkt["slug"], "title": mkt["title"],
               "up_token": mkt["up_token"], "down_token": mkt["down_token"], "price_to_beat": ptb,
               "phase": "scanning", "decisions": [], "order": None, "position": None, "result": None}
        self.state["current"] = cur
        log.info("NEW WINDOW %s | %s | ptb=%s | up=%s.. down=%s..", mkt["slug"], mkt["title"], ptb,
                 mkt["up_token"][:10], mkt["down_token"][:10])
        return cur

    def can_trade(self):
        d = self.day()
        if d["trades"] >= self.cfg["max_trades_per_day"]:
            return False, "max trades/day reached"
        if d["pnl"] <= -abs(self.cfg["daily_loss_cap_usd"]):
            return False, "daily loss cap hit"
        if self.state["cash"] < 1.0:
            return False, "no budget"
        return True, ""

    def manage(self, cur, now):
        cfg = self.cfg
        t_in = now - cur["start"]
        if cur["price_to_beat"] is None:
            try:
                cur["price_to_beat"], _ = pm.price_to_beat(cur["start"])
            except Exception:
                pass
        if cur["phase"] == "scanning":
            if t_in < cfg["entry_window_start_sec"]:
                return
            if t_in > cfg["entry_window_end_sec"]:
                cur["phase"] = "no_trade"
                if not cur["decisions"]:
                    cur["decisions"].append({"t": int(t_in), "call": "WAIT", "why": "entry window passed"})
                log.info("Window %s: no entry (final call WAIT)", cur["slug"])
                return
            if now - self.last_signal_eval < cfg["signal_eval_interval_sec"]:
                return
            self.last_signal_eval = now
            ok, why = self.can_trade()
            if not ok:
                cur["decisions"].append({"t": int(t_in), "call": "SKIP", "why": why})
                cur["phase"] = "no_trade"; log.info("Window %s: skip (%s)", cur["slug"], why); return
            if not cur["price_to_beat"]:
                log.info("Waiting for price-to-beat"); return
            candles = prices.candles_1m(120)
            spot = prices.spot()
            # Compare like with like: Coinbase spot vs Coinbase open at window start
            # (Polymarket's price-to-beat is Chainlink; cross-source basis can be several bps).
            ref = next((c["o"] for c in candles if c["t"] == cur["start"]), None) or cur["price_to_beat"]
            call, why, det = signal.evaluate(candles, spot, ref, cfg)
            det["chainlink_price_to_beat"] = cur["price_to_beat"]
            cur["decisions"].append({"t": int(t_in), "call": call, "why": why, **det})
            log.info("SIGNAL %s t+%ds -> %s (%s) delta=%sbps rsi=%s votes=%s", cur["slug"], t_in, call, why,
                     det["delta_bps"], det["rsi"], det["votes"])
            if call in ("UP", "DOWN"):
                self.enter(cur, call, det)
        elif cur["phase"] == "order_open":
            o = cur["order"]
            bk = pm.book(o["token"])
            self.ex.poll_resting(o, bk)
            if o["status"] == "filled" or (t_in > cfg["entry_window_end_sec"] and o["filled"] > 0):
                self.ex.cancel(o); self.open_position(cur)
            elif t_in > cfg["entry_window_end_sec"]:
                self.ex.cancel(o); cur["phase"] = "no_fill"
                log.info("Window %s: limit %.2f not filled, cancelled", cur["slug"], o["limit"])
        if cur["phase"] == "in_position":
            self.check_exits(cur, t_in)

    def enter(self, cur, side, det):
        cfg = self.cfg
        token = cur["up_token"] if side == "UP" else cur["down_token"]
        bk = pm.book(token)
        best_ask = bk["asks"][0][0] if bk["asks"] else 1.0
        limit = cfg["entry_limit_price"]
        if best_ask < cfg["entry_min_price"]:
            cur["decisions"][-1]["why"] += f"; skipped: ask {best_ask} < min {cfg['entry_min_price']}"
            return
        usd = min(cfg["per_trade_usd"], self.state["cash"])
        shares = round(usd / limit, 2)
        if shares < 5:
            return
        o = self.ex.place_limit_buy(token, limit, shares, bk)
        o["side"] = side; o["signal"] = det
        cur["order"] = o; cur["phase"] = "order_open"
        self.day()["trades"] += 1
        log.info("PAPER LIMIT BUY %s %s %.2f sh @ <=%.2f (best ask %.2f) filled_now=%.2f",
                 cur["slug"], side, shares, limit, best_ask, o["filled"])
        if o["status"] == "filled":
            self.open_position(cur)

    def open_position(self, cur):
        o = cur["order"]
        self.state["cash"] -= o["cost"] + o["fee"]
        cur["position"] = {"start": cur["start"], "slug": cur["slug"], "side": o["side"], "token": o["token"],
                           "shares": o["filled"], "cost": o["cost"], "fee": o["fee"],
                           "entry_px": o["cost"] / o["filled"], "signal": o["signal"]}
        cur["phase"] = "in_position"
        log.info("FILLED %s %s %.2f sh avg %.3f fee %.3f", cur["slug"], o["side"], o["filled"],
                 cur["position"]["entry_px"], o["fee"])

    def check_exits(self, cur, t_in):
        cfg = self.cfg; p = cur["position"]
        if t_in > pm.WINDOW - cfg["no_exit_last_sec"]:
            return
        bk = pm.book(p["token"])
        if not bk["bids"]:
            return
        bid = bk["bids"][0][0]
        tp = p["entry_px"] + cfg["take_profit_price_delta"]
        sl = p["entry_px"] - cfg["stop_loss_price_delta"]
        kind = "TP" if bid >= tp else ("SL" if bid <= sl else None)
        if kind:
            avg, proceeds, fee, sold = self.ex.sell_taker(p["shares"], bk)
            if sold < p["shares"] - 1e-6:
                return  # not enough depth; try next tick
            cur["result"] = self.close_position(p, kind, avg, proceeds, fee)
            cur["phase"] = "closed"

    def end_window(self, cur):
        if cur["phase"] == "order_open":
            self.ex.cancel(cur["order"])
            if cur["order"]["filled"] > 0:
                self.open_position(cur)
            else:
                cur["phase"] = "no_fill"
        if cur["phase"] == "in_position":
            cur["phase"] = "held_to_resolution"
            self.state["pending_resolution"].append(cur["position"])
            log.info("Window %s ended, holding %s to resolution", cur["slug"], cur["position"]["side"])
        cur.pop("up_token", None); cur.pop("down_token", None)
        self.finish_window(cur)
        self.state["current"] = None

    def settle_pending(self):
        keep = []
        for p in self.state["pending_resolution"]:
            try:
                mkt = pm.get_market(p["start"])
                win = pm.resolved_winner(mkt)
            except Exception as e:
                log.warning("resolution check failed: %s", e); win = None
            if win is None:
                keep.append(p); continue
            payout = p["shares"] * (1.0 if win.upper() == p["side"] else 0.0)
            res = self.close_position(p, f"RESOLVED_{win.upper()}", 1.0 if payout else 0.0, payout, 0.0)
            with open(WINDOWS, "a") as f:
                f.write(json.dumps({"settlement": p["slug"], "winner": win,
                                    "meta": mkt.get("event_metadata"), **res}) + "\n")
        self.state["pending_resolution"] = keep
