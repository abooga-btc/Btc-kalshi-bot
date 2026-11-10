"""LIVE Kalshi trading for KXBTC15M, small size, with HARD limits enforced in code.
Config values may only make these stricter, never looser."""
import csv, json, logging, math, os, time, uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .kauth import KalshiAuthClient, AuthError
from . import factors

log = logging.getLogger("kalshi-btc15m.live")

# ---- HARD LIMITS (code-level; config can only lower them) ----
HARD_MAX_COST_PER_TRADE = 20.00     # USD incl. fees (A+ size; 2026-10-30)
HARD_MAX_TRADES_PER_DAY = 50        # live entries per Pacific day
HARD_DAILY_LOSS_CAP = 100.00        # USD realized; trading stops for the Pacific day
HARD_MAX_OPEN_POSITIONS = 1
HARD_MIN_SCORE = 5                  # grade B or better (score >= 5/11)
GRADE_SIZE_USD = {"B": 10.00, "A": 15.00, "A+": 20.00}   # max cost per trade by grade (capped by HARD_MAX_COST_PER_TRADE); other grades $10
SIZE_LADDER = (20.00, 15.00, 10.00)  # step-down stakes when the daily loss-cap room is too small for the grade size
SERIES_PREFIX = "KXBTC15M-"
CANCEL_BEFORE_CLOSE_SEC = 60        # no resting order survives into the last minute
COID_NS = uuid.UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data_kalshi")
LSTATE = os.path.join(DATA, "live_state.json")
LCSV = os.path.join(DATA, "live_orders.csv")
KILL = os.path.join(ROOT, "KILL")
LF = ["time_local", "event", "ticker", "outcome_side", "book_side", "yes_price", "side_price", "count",
      "order_id", "client_order_id", "status", "filled", "fill_cost", "fees", "pnl", "note"]


def coid(ticker, tag):
    return str(uuid.uuid5(COID_NS, f"{ticker}|{tag}"))


def f(x):
    try: return float(x)
    except (TypeError, ValueError): return 0.0


class LiveTrader:
    def __init__(self, cfg):
        lc = cfg.get("live", {})
        self.enabled = bool(lc.get("enabled", False))
        self.dry_run = bool(lc.get("dry_run", False))
        self.max_cost = min(f(lc.get("max_cost_per_trade_usd", HARD_MAX_COST_PER_TRADE)), HARD_MAX_COST_PER_TRADE)
        self.max_trades = min(int(lc.get("max_trades_per_day", HARD_MAX_TRADES_PER_DAY)), HARD_MAX_TRADES_PER_DAY)
        self.loss_cap = min(f(lc.get("daily_loss_cap_usd", HARD_DAILY_LOSS_CAP)), HARD_DAILY_LOSS_CAP)
        self.min_score = max(int(lc.get("min_score", HARD_MIN_SCORE)), HARD_MIN_SCORE)
        self.tz = ZoneInfo(cfg["timezone"]); self.cfg = cfg
        self.halted = None; self.client = None
        self.st = json.load(open(LSTATE)) if os.path.exists(LSTATE) else {
            "days": {}, "windows": {}, "open": [], "halted_reason": None}
        if self.st.get("halted_reason"):
            self.halted = self.st["halted_reason"]
        if not self.enabled:
            log.info("LIVE disabled in config (paper only)."); return
        try:
            self.client = KalshiAuthClient()
            code, j = self.client.request("GET", "/portfolio/balance")
            if code != 200: raise AuthError(f"balance check HTTP {code} {j.get('code')}")
            log.info("LIVE auth OK (%s key). balance=$%.2f portfolio=$%.2f | limits: by grade B $%.0f / A $%.0f / A+ $%.0f per trade (max $%.2f), %d trades/day, "
                     "loss cap $%.2f, max open %d, min score %d/11%s", self.client.key_type, j["balance"] / 100,
                     j.get("portfolio_value", 0) / 100, *(self.size_for({"grade": g}) for g in ("B", "A", "A+")), self.max_cost, self.max_trades, self.loss_cap,
                     HARD_MAX_OPEN_POSITIONS, self.min_score, " [DRY RUN]" if self.dry_run else "")
            self.reconcile_startup()
        except AuthError as e:
            self.halt(f"auth: {e}")

    # ---------- utils ----------
    def now_local(self): return datetime.now(self.tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    def day(self):
        k = datetime.now(self.tz).strftime("%Y-%m-%d")
        return self.st["days"].setdefault(k, {"entries": 0, "pnl": 0.0})
    def save(self):
        tmp = LSTATE + ".tmp"; json.dump(self.st, open(tmp, "w"), indent=2); os.replace(tmp, LSTATE)
    def csvlog(self, **row):
        new = not os.path.exists(LCSV)
        with open(LCSV, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=LF)
            if new: w.writeheader()
            w.writerow({"time_local": self.now_local(), **{k: row.get(k, "") for k in LF if k != "time_local"}})
    def halt(self, why):
        self.halted = why; self.st["halted_reason"] = why; self.save()
        log.error("LIVE HALTED: %s (paper continues)", why)
    AUTH_RETRY_SEC = 60
    def try_recover_auth(self):
        """Auto-clear a halt caused by HTTP 401/403 or an auth error once a signed balance check
        succeeds again. Never clears a KILL halt. Re-runs the startup reconcile before resuming."""
        if not self.enabled or not self.halted or os.path.exists(KILL): return
        if not (self.halted.startswith("HTTP 401") or self.halted.startswith("HTTP 403") or self.halted.startswith("auth:")): return
        now = time.time()
        if now - getattr(self, "_last_auth_try", 0) < self.AUTH_RETRY_SEC: return
        self._last_auth_try = now
        try:
            if self.client is None: self.client = KalshiAuthClient()
            code, j = self.client.request("GET", "/portfolio/balance")
        except Exception as e:
            log.warning("LIVE auth recovery attempt failed: %s", type(e).__name__); return
        if code != 200:
            log.warning("LIVE auth recovery: balance check HTTP %s; still halted", code); return
        prev = self.halted
        self.halted = None; self.st["halted_reason"] = None; self.save()
        log.warning("LIVE RESUMED after auth recovery (was: %s). balance=$%.2f | limits: B/A/A+ $%.0f/$%.0f/$%.0f per trade, %d trades/day, "
                    "loss cap $%.2f, max open %d, min score %d/11", prev, j["balance"] / 100, *(self.size_for({"grade": g}) for g in ("B", "A", "A+")),
                    self.max_trades, self.loss_cap, HARD_MAX_OPEN_POSITIONS, self.min_score)
        self.reconcile_startup()
        if self.halted: log.error("LIVE re-halted during reconcile: %s", self.halted)

    @property
    def active(self): return self.enabled and self.client is not None and not self.halted

    def api(self, method, path, body=None):
        code, j = self.client.request(method, path, body)
        if code in (401, 403):
            self.halt(f"HTTP {code} on {method} {path.split('?')[0]}: {j.get('code')} {j.get('message')}")
        return code, j

    # ---------- reconciliation ----------
    def remote_positions(self):
        code, j = self.api("GET", "/portfolio/positions?count_filter=position&limit=200")
        if code != 200: return None
        return {p["ticker"]: f(p["position_fp"]) for p in j.get("market_positions", [])
                if p["ticker"].startswith(SERIES_PREFIX) and f(p["position_fp"]) != 0}

    def remote_resting(self):
        code, j = self.api("GET", "/portfolio/orders?status=resting&limit=200")
        if code != 200: return None
        return [o for o in j.get("orders", []) if o["ticker"].startswith(SERIES_PREFIX)]

    def reconcile_startup(self):
        mine = {w.get("entry_coid") for w in self.st["windows"].values()} | \
               {x for w in self.st["windows"].values() for x in w.get("exit_coids", [])}
        rest = self.remote_resting() or []
        for o in rest:
            if o.get("client_order_id") in mine:
                log.warning("startup: cancelling leftover bot order %s on %s", o["order_id"], o["ticker"])
                self.cancel(o["order_id"], o["ticker"], "startup cleanup")
            else:
                log.warning("startup: foreign resting KXBTC15M order %s on %s (not touched)", o["order_id"], o["ticker"])
        pos_raw = self.remote_positions(); pos = pos_raw or {}
        tracked = {p["ticker"] for p in self.st["open"]}
        for t, q in pos.items():
            if t not in tracked:
                log.warning("startup: untracked KXBTC15M position %s qty %s -> counts toward max-open, no live entries until it clears", t, q)
        for p in list(self.st["open"]):
            self._norm(p)
            # book any exit order that was sent but not recorded (e.g. crash between send and save)
            w = self.st["windows"].get(p["ticker"], {})
            for cid in w.get("exit_coids", []):
                if cid in p["applied_exits"]: continue
                o = self.find_by_coid(p["ticker"], cid)
                if not o:
                    p["applied_exits"].append(cid); continue   # never reached the exchange
                fr = self.fills(o["order_id"], p["outcome"])
                if fr is not None:
                    log.warning("startup: applying unrecorded exit %s on %s (%.0f contracts)", cid, p["ticker"], fr[0])
                    self._apply_exit(p, o["order_id"], cid, "TP_PARTIAL" if fr[0] < p["qty"] else "RECONCILED", 0.0, fr)
            if p not in self.st["open"]: continue
            if pos_raw is None: continue   # positions API unavailable: do not guess
            if p["ticker"] not in pos:
                log.info("startup: tracked position %s no longer open remotely (settled/closed); settling via market result", p["ticker"])
                continue
            rq = abs(pos[p["ticker"]])
            if abs(rq - p["qty"]) > 1e-6:
                log.warning("startup: %s tracked qty %.0f != exchange qty %.0f; using exchange qty (cost basis scaled)",
                            p["ticker"], p["qty"], rq)
                if p["qty"] > 0:
                    k = rq / p["qty"]; p["cost"] *= k; p["fees"] *= k
                p["qty"] = rq
            if p["qty"] < p.get("entry_qty", p["qty"]):
                log.info("startup: %s partially exited: riding %.0f of %.0f, tp_done=%s, recovered $%.2f", p["ticker"],
                         p["qty"], p["entry_qty"], p["tp_done"], p["recovered"])
        self.save()

    # ---------- orders ----------
    def place(self, ticker, outcome, side_price, count, cid, tif, expiration=None, reduce_only=False, tag=""):
        """Limit order only. outcome 'yes' -> book side bid at yes price; 'no' -> ask at yes price 1-p."""
        yes_px = side_price if outcome == "yes" else round(1 - side_price, 4)
        body = {"ticker": ticker, "client_order_id": cid, "side": "bid" if outcome == "yes" else "ask",
                "count": f"{count:.2f}", "price": f"{yes_px:.4f}", "time_in_force": tif,
                "self_trade_prevention_type": "taker_at_cross", "post_only": False,
                "cancel_order_on_pause": True, "reduce_only": reduce_only}
        if expiration: body["expiration_time"] = int(expiration)
        if self.dry_run:
            url, h, _ = self.client.build("POST", "/portfolio/events/orders", body)
            log.info("DRY RUN (not sent) POST %s %s headers=%s", url, json.dumps(body), self.client.redacted(h))
            self.csvlog(event=f"DRYRUN_{tag}", ticker=ticker, outcome_side=outcome, book_side=body["side"],
                        yes_price=yes_px, side_price=side_price, count=count, client_order_id=cid, status="not_sent")
            return None
        code, j = self.api("POST", "/portfolio/events/orders", body)
        if code == 409 or (code == 400 and "client_order_id" in json.dumps(j).lower()):
            # duplicate client_order_id: already placed earlier (restart) -> look it up instead of re-sending
            o = self.find_by_coid(ticker, cid)
            log.warning("order %s already exists (%s); adopted", cid, o and o["order_id"])
            return o and {"order_id": o["order_id"]}
        if code not in (200, 201):
            log.error("order rejected HTTP %s %s", code, j)
            self.csvlog(event=f"REJECT_{tag}", ticker=ticker, outcome_side=outcome, book_side=body["side"], yes_price=yes_px,
                        side_price=side_price, count=count, client_order_id=cid, status=f"http_{code}", note=str(j)[:200])
            return None
        self.csvlog(event=f"PLACE_{tag}", ticker=ticker, outcome_side=outcome, book_side=body["side"], yes_price=yes_px,
                    side_price=side_price, count=count, order_id=j.get("order_id"), client_order_id=cid, status="placed",
                    filled=j.get("fill_count"), note=f"avg_fill={j.get('average_fill_price')} avg_fee={j.get('average_fee_paid')}")
        log.info("LIVE %s %s %s x%d @%.2f -> order %s filled %s remaining %s", tag, ticker, outcome.upper(), count,
                 side_price, j.get("order_id"), j.get("fill_count"), j.get("remaining_count"))
        return j

    def find_by_coid(self, ticker, cid):
        code, j = self.api("GET", f"/portfolio/orders?ticker={ticker}&limit=200")
        for o in (j.get("orders", []) if code == 200 else []):
            if o.get("client_order_id") == cid: return o
        return None

    def get_order(self, oid):
        code, j = self.api("GET", f"/portfolio/orders/{oid}")
        return j.get("order") if code == 200 else None

    def cancel(self, oid, ticker, why):
        code, j = self.api("DELETE", f"/portfolio/events/orders/{oid}?market_ticker={ticker}")
        log.info("LIVE cancel %s (%s) -> HTTP %s", oid, why, code)
        self.csvlog(event="CANCEL", ticker=ticker, order_id=oid, status=f"http_{code}", note=why)
        return code in (200, 404)

    def fills(self, oid, our_outcome):
        """Reconcile from GET /portfolio/fills?order_id=: returns (contracts, value in OUR outcome's price, fees)."""
        code, j = self.api("GET", f"/portfolio/fills?order_id={oid}&limit=200")
        if code != 200: return None
        n = v = fee = 0.0
        for x in j.get("fills", []):
            c = f(x["count_fp"]); n += c; fee += f(x.get("fee_cost"))
            v += c * f(x["yes_price_dollars"] if our_outcome == "yes" else x["no_price_dollars"])
        return n, v, fee

    # ---------- engine hooks ----------
    def kill_check(self):
        if not os.path.exists(KILL): return False
        if self.enabled and self.client and not self.st.get("kill_handled"):
            for w_t, w in self.st["windows"].items():
                if w.get("entry_oid") and w.get("entry_status") == "resting":
                    self.cancel(w["entry_oid"], w_t, "KILL switch"); w["entry_status"] = "cancelled"
            for o in (self.remote_resting() or []):
                if o.get("client_order_id", "").count("-") == 4 and self._is_mine(o["client_order_id"]):
                    self.cancel(o["order_id"], o["ticker"], "KILL switch")
            self.st["kill_handled"] = True
            self.halt("KILL file present")
        return True

    def _is_mine(self, cid):
        return any(w.get("entry_coid") == cid or cid in w.get("exit_coids", []) for w in self.st["windows"].values())

    def size_for(self, sig):
        return min(self.max_cost, GRADE_SIZE_USD.get(sig.get("grade"), 10.00), HARD_MAX_COST_PER_TRADE)

    def stake_for(self, sig):
        """Grade stake, stepped down (20 -> 15 -> 10) so the full stake (worst case) can never push the day past
        -loss_cap. Returns (stake, note); stake 0 means refuse."""
        want = self.size_for(sig); pnl = self.day()["pnl"]
        for s in [want] + [x for x in SIZE_LADDER if x < want]:
            if not (pnl - s <= -self.loss_cap):
                return s, ("" if s == want else f"stepped down ${want:.0f}->${s:.0f} (day pnl {pnl:+.2f}, cap -{self.loss_cap:.0f})")
        return 0.0, f"daily loss cap (day pnl {pnl:+.2f}, worst case next trade -{min(want, SIZE_LADDER[-1]):.2f})"

    def can_enter(self, cur, sig, now):
        if not self.active: return False, "live inactive" + (f" ({self.halted})" if self.halted else "")
        t = cur["ticker"]; d = self.day()
        if not t.startswith(SERIES_PREFIX): return False, "not KXBTC15M"
        if not (cur["open_ts"] <= now < cur["close_ts"] - CANCEL_BEFORE_CLOSE_SEC - 30): return False, "outside current window"
        if sig["score"] < self.min_score: return False, f"score {sig['score']} < {self.min_score}"
        if t in self.st["windows"]: return False, "already traded this window (no averaging down)"
        if d["entries"] >= self.max_trades: return False, "live max trades/day"
        stake, note = self.stake_for(sig)
        if stake <= 0: return False, note
        if len(self.st["open"]) >= HARD_MAX_OPEN_POSITIONS: return False, "max open positions (tracked)"
        pos = self.remote_positions()
        if pos is None: return False, "positions API unavailable"
        if len(pos) >= HARD_MAX_OPEN_POSITIONS: return False, f"max open positions (account has {list(pos)})"
        rest = self.remote_resting()
        if rest is None: return False, "orders API unavailable"
        if any(o["ticker"] == t for o in rest): return False, "foreign resting order in this market"
        return True, "ok"

    STALE_TICK_SEC = 20
    def enter(self, cur, outcome, limit, sig, p_side, now):
        if time.time() - now > self.STALE_TICK_SEC:   # tick started long ago (box paused mid-tick): never act on it
            log.warning("LIVE skip %s: stale tick (%.0fs old)", cur["ticker"], time.time() - now); return "stale tick"
        ok, why = self.can_enter(cur, sig, now)
        if not ok:
            log.info("LIVE skip %s: %s", cur["ticker"], why); return why
        code, b = self.api("GET", "/portfolio/balance")
        bal = b.get("balance", 0) / 100 if code == 200 else 0
        per = limit + 0.07 * limit * (1 - limit)
        stake, note = self.stake_for(sig)
        if note: log.info("LIVE size %s: %s", cur["ticker"], note)
        n = int(math.floor((min(stake, bal) - 0.01) / per))
        if n < 1: log.info("LIVE skip: size < 1 (balance %.2f, stake %.2f)", bal, stake); return "size<1"
        assert n * per + 0.01 <= min(stake, HARD_MAX_COST_PER_TRADE) + 1e-9
        log.info("LIVE size %s: grade %s score %s -> stake $%.2f (%d @ %.2f, balance $%.2f)", cur["ticker"], sig.get("grade"),
                 sig.get("score"), stake, n, limit, bal)
        t = cur["ticker"]; cid = coid(t, "entry")
        w = {"entry_coid": cid, "outcome": outcome, "limit": limit, "count": n, "score": sig["score"],
             "grade": sig["grade"], "p_side": round(p_side, 3), "placed_local": self.now_local(), "exit_coids": []}
        from .kengine import is_mid_window_entry
        w["entry_left_sec"] = int(round(cur["close_ts"] - now)); w["mid_window"] = is_mid_window_entry(w["entry_left_sec"], self.cfg["exit"])
        self.st["windows"][t] = w; self.day()["entries"] += 1; self.save()   # persist BEFORE sending (idempotent restarts)
        dl = sig.get("entry_deadline_ts") or (cur["open_ts"] + self.cfg["entry"]["entry_window_end_sec"])
        exp = min(dl, cur["close_ts"] - CANCEL_BEFORE_CLOSE_SEC)
        w["deadline_ts"] = exp; self.save()
        r = self.place(t, outcome, limit, n, cid, "good_till_canceled", expiration=exp, tag="ENTRY")
        if not r:
            w["entry_status"] = "dry_run" if self.dry_run else "failed"
            if not self.dry_run: self.day()["entries"] -= 1
            self.save(); return w["entry_status"]
        w["entry_oid"] = r["order_id"]; w["entry_status"] = "resting"; self.save()
        self.poll_entry(cur, now)
        return "placed"

    def poll_entry(self, cur, now):
        t = cur["ticker"]; w = self.st["windows"].get(t)
        if not w or w.get("entry_status") != "resting": return
        o = self.get_order(w["entry_oid"])
        if not o: return
        done = o["status"] in ("executed", "canceled") or f(o.get("remaining_count_fp")) == 0
        deadline = now >= (w.get("deadline_ts") or
                           min(cur["open_ts"] + self.cfg["entry"]["entry_window_end_sec"], cur["close_ts"] - CANCEL_BEFORE_CLOSE_SEC))
        if not done and deadline:
            self.cancel(w["entry_oid"], t, "entry deadline / pre-close"); o = self.get_order(w["entry_oid"]) or o
            done = True
        if done:
            fr = self.fills(w["entry_oid"], w["outcome"])
            if fr is None: return
            filled, cost, fees = fr
            if abs(filled - f(o.get("fill_count_fp"))) > 1e-6:
                log.warning("fill mismatch order=%s fills=%s; retry next tick", o.get("fill_count_fp"), filled); return
            w["entry_status"] = "filled" if filled > 0 else "unfilled"
            w.update(filled=filled, cost=cost, fees=fees)
            self.csvlog(event="ENTRY_RESULT", ticker=t, outcome_side=w["outcome"], side_price=w["limit"], count=w["count"],
                        order_id=w["entry_oid"], client_order_id=w["entry_coid"], status=o["status"], filled=filled,
                        fill_cost=round(cost, 4), fees=round(fees, 4))
            if filled > 0:
                self.st["open"].append({"ticker": t, "outcome": w["outcome"], "qty": filled, "cost": cost, "fees": fees,
                                        "entry_px": cost / filled, "close_ts": cur["close_ts"], "entry_qty": filled,
                                        "entry_total": cost + fees, "recovered": 0.0, "realized": 0.0,
                                        "tp_done": False, "applied_exits": [],
                                        "entry_left_sec": w.get("entry_left_sec"), "mid_window": bool(w.get("mid_window"))})
                log.info("LIVE FILLED %s %s x%.0f avg %.3f fees $%.2f%s", t, w["outcome"].upper(), filled, cost / filled, fees,
                         f" [MID-WINDOW entry, {w.get('entry_left_sec')}s left: TP_MID/BE rules]" if w.get("mid_window") else "")
            else:
                self.day()["entries"] -= 1  # nothing traded; frees the slot (window still blocked)
            self.save()

    def manage_exits(self, cur, now, book):
        if time.time() - now > self.STALE_TICK_SEC: return   # stale tick; re-evaluate next loop
        xcfg = self.cfg["exit"]
        for p in list(self.st["open"]):
            if p["ticker"] != cur["ticker"]: continue
            if cur["close_ts"] - now <= xcfg["exit_buffer_sec"]: continue
            sb = book[p["outcome"]]
            if not sb["bids"]: continue
            bid = sb["bids"][0][0]
            self._norm(p)
            from .kengine import exit_kind
            kind = exit_kind(p, bid, xcfg, cur["close_ts"] - now, self.cfg["fees"]["taker_rate"])
            if not kind: continue
            qty = int(p["qty"])
            if kind == "TP" and xcfg.get("partial_tp", True):
                qty = factors.partial_tp_qty(p["qty"], bid, p["entry_total"] - p["recovered"], self.cfg["fees"]["taker_rate"])
                if qty <= 0: p["tp_done"] = True; self.save(); continue
            w = self.st["windows"][p["ticker"]]
            if len(w["exit_coids"]) >= 5: continue  # bounded retries
            cid = coid(p["ticker"], f"exit{len(w['exit_coids'])}")
            w["exit_coids"].append(cid); self.save()
            tag = "TP_PARTIAL" if (kind == "TP" and qty < p["qty"]) else kind
            self._sell(p, bid, cid, tag, qty)

    @staticmethod
    def _norm(p):
        """Backfill partial-exit bookkeeping for positions opened before 2026-10-29 rules."""
        p.setdefault("entry_qty", p["qty"]); p.setdefault("entry_total", p["cost"] + p["fees"])
        p.setdefault("recovered", 0.0); p.setdefault("realized", 0.0)
        p.setdefault("tp_done", False); p.setdefault("applied_exits", [])

    def _sell(self, p, bid, cid, kind, qty=None):
        """Sell `qty` (default all) of a long YES/NO position with an IOC + reduce_only limit at the current best bid.
        Selling YES at bid b = book ask at yes price b => place(outcome='no', side_price=1-b);
        selling NO at bid b = book bid at yes price 1-b => place(outcome='yes', side_price=1-b)."""
        qty = int(p["qty"] if qty is None else qty)
        outcome_order = "no" if p["outcome"] == "yes" else "yes"
        r = self.place(p["ticker"], outcome_order, round(1 - bid, 4), qty, cid, "immediate_or_cancel",
                       reduce_only=True, tag=f"EXIT_{kind}")
        if not r: return None
        want = f(r.get("fill_count"))
        fr = None
        for _ in range(4):   # fills can lag the order response briefly
            fr = self.fills(r["order_id"], p["outcome"])
            if fr and fr[0] + 1e-6 >= want: break
            time.sleep(1)
        if fr is None: return r
        self._apply_exit(p, r["order_id"], cid, kind, bid, fr)
        return r

    def _apply_exit(self, p, oid, cid, kind, bid, fr):
        """Book the fills of one exit order exactly once (keyed by client_order_id)."""
        self._norm(p)
        if cid in p["applied_exits"]: return
        sold, proceeds, fees = fr   # value priced in OUR outcome = sale proceeds
        p["applied_exits"].append(cid)
        if sold <= 0: self.save(); return
        sold = min(sold, p["qty"]); frac = sold / p["qty"]
        pnl = proceeds - fees - (p["cost"] + p["fees"]) * frac
        self.day()["pnl"] += pnl
        p["recovered"] += proceeds - fees; p["realized"] += pnl
        p["qty"] -= sold; p["cost"] *= (1 - frac); p["fees"] *= (1 - frac)
        if kind.startswith("TP") and p["recovered"] >= p["entry_total"] - 1e-9: p["tp_done"] = True
        self.csvlog(event=f"EXIT_{kind}", ticker=p["ticker"], outcome_side=p["outcome"], side_price=bid, count=sold,
                    order_id=oid, client_order_id=cid, status="ioc", filled=sold,
                    fill_cost=round(proceeds, 4), fees=round(fees, 4), pnl=round(pnl, 4),
                    note=f"remaining={p['qty']:.0f} recovered={p['recovered']:.2f}/{p['entry_total']:.2f}")
        log.info("LIVE EXIT %s %s %s x%.0f @~%.2f pnl %+.2f | riding %.0f (recovered $%.2f of $%.2f)", kind, p["ticker"],
                 p["outcome"].upper(), sold, bid, pnl, p["qty"], p["recovered"], p["entry_total"])
        if p["qty"] <= 1e-9: self.st["open"].remove(p)
        self.save()

    def end_window(self, cur):
        w = self.st["windows"].get(cur["ticker"])
        if w and w.get("entry_status") == "resting":
            self.poll_entry(cur, cur["close_ts"])

    def settle(self, market_fn):
        for p in list(self.st["open"]):
            if time.time() < p["close_ts"]: continue
            try: m = market_fn(p["ticker"])
            except Exception: continue
            if m.get("result") not in ("yes", "no"): continue
            won = m["result"] == p["outcome"]
            pnl = (p["qty"] if won else 0.0) - p["cost"] - p["fees"]
            self.day()["pnl"] += pnl
            if p.get("realized"): log.info("LIVE trade total %s %+.2f (partial exits %+.2f + settlement %+.2f)",
                                           p["ticker"], p["realized"] + pnl, p["realized"], pnl)
            self.csvlog(event=f"SETTLED_{m['result'].upper()}", ticker=p["ticker"], outcome_side=p["outcome"],
                        count=p["qty"], fill_cost=round(p["qty"] if won else 0, 4), pnl=round(pnl, 4),
                        note=f"expiration_value={m.get('expiration_value')} strike={m.get('floor_strike')}")
            log.info("LIVE SETTLED %s result=%s our=%s pnl %+.2f", p["ticker"], m["result"], p["outcome"], pnl)
            self.st["open"].remove(p); self.save()
