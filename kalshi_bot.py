#!/usr/bin/env python3
"""Kalshi KXBTC15M PAPER bot (Bucko-style 11-factor score).  Usage: kalshi_bot.py run | status"""
import json, logging, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btcbot import kengine as E


def status():
    if not os.path.exists(E.STATE): print("No state yet."); return
    s = json.load(open(E.STATE)); wr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
    pend = sum(p["cost"] + p["fee"] for p in s["pending_settlement"])
    print(f"KALSHI KXBTC15M | PAPER | last update {s['last_update']}")
    print(f"Cash ${s['cash']:.2f} (start ${s['starting_budget']:.2f}) | awaiting settlement (cost) ${pend:.2f}")
    print(f"Realized P&L ${s['realized_pnl']:+.2f} | trades {s['trades']} | W/L {s['wins']}/{s['losses']} | win rate {wr:.1f}% | windows {s['windows_seen']}")
    for d, v in sorted(s["days"].items())[-5:]: print(f"  {d}: trades {v['trades']} pnl ${v['pnl']:+.2f}")
    c = s.get("current")
    if c:
        print(f"Current: {c['ticker']} opened {c['window_local']} strike {c['strike']} phase {c['phase']}")
        for d in c["decisions"][-3:]:
            print(f"  {d['t_local']} {d['label']} p_up={d['p_up']} yes {d['yes_bid']}/{d['yes_ask']} -> {d['action']} ({d['why']})")
        if c.get("order"): o = c["order"]; print(f"  order {o['side']} x{o['contracts']} @<= {o['limit']} filled {o['filled']} {o['status']}")
    if os.path.exists(E.WINDOWS):
        print("Recent windows:")
        for l in open(E.WINDOWS).read().strip().splitlines()[-6:]:
            w = json.loads(l)
            if "settlement" in w: print(f"  settled {w['settlement']} result={w['result']} pnl ${w['pnl']:+.2f}")
            else:
                ld = w.get("last_decision") or {}
                print(f"  {w['window_local']} {w['phase']} last: {ld.get('label')} {ld.get('action')} ({ld.get('why')})"
                      + (f" pnl ${w['result']['pnl']:+.2f}" if w.get("result") else ""))


def live_status():
    from btcbot import live as L
    if not os.path.exists(L.LSTATE): print("LIVE: no live state yet"); return
    s = json.load(open(L.LSTATE)); cfg = E.load_cfg()["live"]
    print(f"LIVE: enabled={cfg['enabled']} dry_run={cfg['dry_run']} halted={s.get('halted_reason')} KILL file={'present' if os.path.exists(L.KILL) else 'absent'}")
    for d, v in sorted(s["days"].items())[-3:]: print(f"  {d}: live entries {v['entries']} realized pnl ${v['pnl']:+.2f}")
    for p in s["open"]: print(f"  OPEN {p['ticker']} {p['outcome'].upper()} x{p['qty']:.0f} avg {p['entry_px']:.3f}")
    if os.path.exists(L.LCSV):
        print("  recent live orders:")
        for l in open(L.LCSV).read().strip().splitlines()[-6:]: print("   ", l)


def resume_live():
    from btcbot import live as L
    if os.path.exists(L.KILL): print("Remove the KILL file first."); return
    if os.path.exists(L.LSTATE):
        s = json.load(open(L.LSTATE)); s["halted_reason"] = None; s.pop("kill_handled", None)
        json.dump(s, open(L.LSTATE, "w"), indent=2)
    print("live halt cleared; restart the bot (./stop_kalshi.sh is not needed if it is not running) with ./start_kalshi.sh")


if __name__ == "__main__":
    cmd = (sys.argv[1:] or ["status"])[0]
    if cmd == "resume-live": resume_live(); sys.exit()
    if cmd == "status": status(); live_status(); sys.exit()
    if cmd == "run":
        os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"), exist_ok=True)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                            handlers=[logging.FileHandler(os.path.join(E.ROOT, "logs", "kalshi_bot.log"))])
        E.KalshiBot(E.load_cfg()).run()

