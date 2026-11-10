"""Paper execution for Kalshi binary contracts. No live trading, no keys.
A future live executor would call Kalshi's authenticated POST /portfolio/orders;
it is intentionally not implemented."""
from .kalshi import fee


class KalshiPaperExecutor:
    name = "paper"

    def __init__(self, taker_rate=0.07, maker_rate=0.0):
        self.tr, self.mr = taker_rate, maker_rate

    def place_limit_buy(self, side, limit, contracts, sidebook):
        """Marketable part takes asks <= limit (taker fee); remainder rests (maker fee = 0 on 'quadratic' series)."""
        filled, cost = 0, 0.0
        for px, sz in sidebook["asks"]:
            if px > limit + 1e-9 or filled >= contracts: break
            q = int(min(sz, contracts - filled))
            filled += q; cost += q * px
        tfee = fee(filled, cost / filled, self.tr) if filled else 0.0
        o = {"side": side, "limit": limit, "contracts": contracts, "filled": filled, "cost": cost,
             "fee": tfee, "taker_filled": filled, "status": "filled" if filled >= contracts else "open"}
        return o

    def poll_resting(self, o, sidebook):
        """Resting bid fills at our limit when the best ask trades down to it (optimistic on queue)."""
        if o["status"] != "open" or not sidebook["asks"]: return o
        ask, sz = sidebook["asks"][0]
        if ask <= o["limit"] + 1e-9:
            q = int(min(sz, o["contracts"] - o["filled"]))
            o["filled"] += q; o["cost"] += q * o["limit"]
            if self.mr: o["fee"] += fee(q, o["limit"], self.mr)
            if o["filled"] >= o["contracts"]: o["status"] = "filled"
        return o

    def cancel(self, o):
        o["status"] = "filled" if o["filled"] > 0 else "cancelled"
        return o

    def sell_taker(self, contracts, sidebook):
        sold, proceeds = 0, 0.0
        for px, sz in sidebook["bids"]:
            if sold >= contracts: break
            q = int(min(sz, contracts - sold))
            sold += q; proceeds += q * px
        avg = proceeds / sold if sold else 0.0
        return avg, proceeds, (fee(sold, avg, self.tr) if sold else 0.0), sold
