#!/usr/bin/env python3
"""BTC 15m Up/Down PAPER trading bot for Polymarket.  Usage: bot.py run | status"""
import json, logging, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btcbot import engine

def status():
    if not os.path.exists(engine.STATE):
        print("No state yet."); return
    s = json.load(open(engine.STATE))
    eq_pending = sum(p["cost"] + p["fee"] for p in s["pending_resolution"])
    wr = (s["wins"] / s["trades"] * 100) if s["trades"] else 0
    print(f"Mode: PAPER | last update: {s['last_update']}")
    print(f"Cash: ${s['cash']:.2f} (start ${s['starting_budget']:.2f}) | open/pending cost basis: ${eq_pending:.2f}")
    print(f"Realized P&L: ${s['realized_pnl']:+.2f} | trades: {s['trades']} | W/L {s['wins']}/{s['losses']} | win rate {wr:.1f}%")
    print(f"Windows logged: {s['windows_seen']}")
    for d, v in sorted(s["days"].items())[-5:]:
        print(f"  {d}: trades {v['trades']}, pnl ${v['pnl']:+.2f}")
    c = s.get("current")
    if c:
        last = c["decisions"][-1] if c["decisions"] else None
        print(f"Current window: {c['window_local']} {c['slug']} phase={c['phase']} ptb={c['price_to_beat']}")
        if last: print(f"  last decision: {last['call']} ({last['why']}) delta={last.get('delta_bps')}bps rsi={last.get('rsi')}")
        if c.get("order"): print(f"  order: {c['order']['side']} limit {c['order']['limit']} filled {c['order']['filled']}/{c['order']['shares']} {c['order']['status']}")
    if os.path.exists(engine.WINDOWS):
        lines = open(engine.WINDOWS).read().strip().splitlines()[-6:]
        print("Recent windows:")
        for l in lines:
            w = json.loads(l)
            if "settlement" in w:
                print(f"  settle {w['settlement']} winner={w['winner']} pnl ${w['pnl']:+.2f}")
            else:
                last = w["decisions"][-1] if w["decisions"] else {}
                r = w.get("result") or {}
                print(f"  {w['window_local']} phase={w['phase']} call={last.get('call')} ({last.get('why')})"
                      + (f" pnl ${r['pnl']:+.2f}" if r else ""))

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "run":
        os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"), exist_ok=True)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                            handlers=[logging.FileHandler(os.path.join(engine.ROOT, "logs", "bot.log")),
                                      logging.StreamHandler()])
        engine.Bot(engine.load_cfg()).run()
    else:
        status()
