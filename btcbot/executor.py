"""Execution backends. Only PaperExecutor is implemented.

A future LiveExecutor would wrap py-clob-client (ClobClient.create_and_post_order,
cancel, etc.). It is intentionally NOT implemented and there is no key handling here.
"""
from .polymarket import taker_fee


class PaperExecutor:
    """Simulates fills against the live public CLOB order book."""
    name = "paper"

    def __init__(self, fee_rate=0.07):
        self.fee_rate = fee_rate

    def place_limit_buy(self, token, limit, shares, book):
        """Marketable part fills immediately as taker (walk asks <= limit, pay fee);
        remainder rests as a maker order (no fee) until best ask trades down to limit."""
        filled, cost, fee = 0.0, 0.0, 0.0
        for px, sz in book["asks"]:
            if px > limit or filled >= shares:
                break
            q = min(sz, shares - filled)
            filled += q; cost += q * px; fee += taker_fee(q, px, self.fee_rate)
        order = {"token": token, "limit": limit, "shares": shares,
                 "filled": filled, "cost": cost, "fee": fee, "status": "open"}
        if filled >= shares - 1e-9:
            order["status"] = "filled"
        return order

    def poll_resting(self, order, book):
        """Resting maker bid fills (at our limit, no fee) when someone sells into it:
        approximated as best ask <= limit (optimistic w.r.t. queue position)."""
        if order["status"] != "open" or not book["asks"]:
            return order
        best_ask, ask_sz = book["asks"][0]
        if best_ask <= order["limit"]:
            q = min(order["shares"] - order["filled"], ask_sz)
            order["filled"] += q; order["cost"] += q * order["limit"]
            if order["filled"] >= order["shares"] - 1e-9:
                order["status"] = "filled"
        return order

    def cancel(self, order):
        order["status"] = "filled" if order["filled"] > 0 else "cancelled"
        return order

    def sell_taker(self, shares, book):
        """Market sell by walking the bids. Returns (avg_px, proceeds, fee, sold)."""
        sold, proceeds, fee = 0.0, 0.0, 0.0
        for px, sz in book["bids"]:
            if sold >= shares: break
            q = min(sz, shares - sold)
            sold += q; proceeds += q * px; fee += taker_fee(q, px, self.fee_rate)
        avg = proceeds / sold if sold else 0.0
        return avg, proceeds, fee, sold


class LiveExecutor:  # placeholder for a future, explicitly-enabled live mode
    name = "live"

    def __init__(self, *a, **k):
        raise NotImplementedError("Live trading is disabled. Paper mode only.")
