# -*- coding: utf-8 -*-
"""
SMART ORDER ROUTER (ALGO3 - RIT)
---------------------------------
Run directly in Spyder.
Steps:
1. Open your RIT Client and start the ALGO3 case.
2. Copy the API key shown at the top-right of the RIT window.
3. Paste it below into API_KEY.
4. Press Run ▶️ in Spyder.
"""

import requests
import time
import random
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Any


# =====================================================
# CONFIGURATION
# =====================================================
API_KEY = "PUT_YOUR_RIT_API_KEY_HERE"   # ← paste your key here
SYMBOL  = "THOR"
SIDE    = "BUY"        # BUY to hedge a client sell; SELL to hedge a client buy
QTY     = 35000
CAP     = 20.05        # Client price cap
MAX_ITERS = 3
# =====================================================


# =====================================================
# RIT API WRAPPER (REST ONLY)
# =====================================================
class ApiException(Exception):
    pass


class RITAPI:
    def __init__(self, api_key: str, base_url: str = "http://localhost:9999/v1") -> None:
        self.base = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({'X-API-Key': api_key})
        self._last_symbol: Optional[str] = None

    def _check(self, resp: requests.Response) -> None:
        if resp.status_code == 401:
            raise ApiException("401 Unauthorized. Ensure API key matches the RIT client.")
        if not resp.ok:
            raise ApiException(f"HTTP {resp.status_code}: {resp.text}")

    def now(self) -> float:
        return time.monotonic()

    def get_tick(self) -> int:
        resp = self.session.get(f"{self.base}/case")
        self._check(resp)
        return int(resp.json().get("tick", 0))

    def print_heartbeat(self) -> None:
        resp = self.session.get(f"{self.base}/case")
        self._check(resp)
        js = resp.json()
        print(f"Heartbeat | Case: {js.get('name')} | Status: {js.get('status')} | Tick: {js.get('tick')}")

    def _get_book_consolidated(self, symbol: str):
        resp = self.session.get(f"{self.base}/securities/book", params={'ticker': symbol})
        self._check(resp)
        js = resp.json()
        bids = js.get('bids', []); asks = js.get('asks', [])
        bids_alt = js.get('bids_alt', []); asks_alt = js.get('asks_alt', [])
        if (bids_alt or asks_alt):
            return {
                "MAIN": {"bids": [(float(x['price']), int(x['quantity'])) for x in bids],
                         "asks": [(float(x['price']), int(x['quantity'])) for x in asks]},
                "ALT":  {"bids": [(float(x['price']), int(x['quantity'])) for x in bids_alt],
                         "asks": [(float(x['price']), int(x['quantity'])) for x in asks_alt]},
            }
        return None

    def _get_book_per_venue(self, symbol: str) -> Dict[str, Dict[str, List[Tuple[float, int]]]]:
        out: Dict[str, Dict[str, List[Tuple[float, int]]]] = {}
        for venue in ("MAIN", "ALT"):
            resp = self.session.get(f"{self.base}/securities/book",
                                    params={'ticker': symbol, 'venue': venue})
            self._check(resp)
            js = resp.json()
            out[venue] = {
                "bids": [(float(x['price']), int(x['quantity'])) for x in js.get('bids', [])],
                "asks": [(float(x['price']), int(x['quantity'])) for x in js.get('asks', [])],
            }
        return out

    def get_books(self, symbol: str):
        self._last_symbol = symbol
        books = self._get_book_consolidated(symbol)
        return books if books is not None else self._get_book_per_venue(symbol)

    def send_order(self, venue: str, side: str, qty: int, price: float, tif: str = "IOC") -> Optional[str]:
        payload = {
            'ticker': self._last_symbol,
            'type': 'LIMIT',
            'quantity': int(qty),
            'action': side.upper(),
            'price': float(price),
            'tif': tif.upper(),
            'venue': venue.upper(),
        }
        resp = self.session.post(f"{self.base}/orders", data=payload)
        self._check(resp)
        js = resp.json()
        return str(js.get('order_id', '')) if isinstance(js, dict) else None

    def post_limit(self, venue: str, side: str, qty: int, price: float, tif: str = "DAY") -> str:
        payload = {
            'ticker': self._last_symbol,
            'type': 'LIMIT',
            'quantity': int(qty),
            'action': side.upper(),
            'price': float(price),
            'tif': tif.upper(),
            'venue': venue.upper(),
        }
        resp = self.session.post(f"{self.base}/orders", data=payload)
        self._check(resp)
        js = resp.json()
        return str(js.get('order_id', '')) if isinstance(js, dict) else ""

    def cancel(self, order_id: str) -> None:
        if not order_id:
            return
        resp = self.session.delete(f"{self.base}/orders/{order_id}")
        if resp.status_code not in (200, 204):
            self._check(resp)


# =====================================================
# SMART ORDER ROUTER LOGIC
# =====================================================
@dataclass
class SORConfig:
    main_take: float = 0.01
    alt_take: float = 0.005
    alt_limit_rebate: float = 0.0025
    max_child: int = 25000
    ttl_seconds: float = 1.5
    refresh_delay: float = 0.15
    jitter_low: float = 0.02
    jitter_high: float = 0.07
    prate_cap: float = 0.25
    follow_ticks: int = 1
    max_passive_loops: int = 2


class SmartSOR:
    def __init__(self, api, cfg: SORConfig = SORConfig()):
        self.api = api
        self.cfg = cfg

    def _nbbo(self, books):
        best_bid = 0.0
        best_ask = float("inf")
        for b in books.values():
            if b.get("bids"):
                best_bid = max(best_bid, b["bids"][0][0])
            if b.get("asks"):
                best_ask = min(best_ask, b["asks"][0][0])
        return best_bid, best_ask

    def _global_ladder(self, side, books):
        ladder = []
        for venue, b in books.items():
            fee = self.cfg.main_take if venue == "MAIN" else self.cfg.alt_take
            if side == "BUY":
                for px, vol in b.get("asks", []):
                    ladder.append((px + fee, vol, venue, px))
            else:
                for px, vol in b.get("bids", []):
                    ladder.append((px - fee, vol, venue, px))
        ladder.sort(key=lambda x: x[0] if side == "BUY" else -x[0])
        return ladder

    def estimate_vwap(self, side, qty, books):
        ladder = self._global_ladder(side, books)
        got, cost = 0, 0
        for eff, vol, _, _ in ladder:
            if got >= qty:
                break
            take = min(qty - got, vol)
            got += take
            cost += eff * take
        return cost / got if got else float("inf")

    def _plan(self, side, qty, books):
        ladder = self._global_ladder(side, books)
        plan, remaining = [], qty
        for eff, vol, venue, raw in ladder:
            if remaining <= 0:
                break
            child = min(vol, remaining, self.cfg.max_child)
            plan.append({"venue": venue, "qty": child, "raw_px": raw})
            remaining -= child
        return plan

    def _send_ioc(self, side, plan):
        oids = []
        random.shuffle(plan)
        for i, leg in enumerate(plan):
            oid = self.api.send_order(leg["venue"], side, leg["qty"], leg["raw_px"], "IOC")
            if oid:
                oids.append(oid)
            if i < len(plan) - 1:
                time.sleep(random.uniform(self.cfg.jitter_low, self.cfg.jitter_high))
        return oids

    def route(self, symbol, side, qty_target, client_price_cap, max_iters=3):
        side = side.upper()
        qty_left = qty_target
        report = {"accepted": None, "legs": [], "residual": qty_left}
        books = self.api.get_books(symbol)
        est = self.estimate_vwap(side, qty_left, books)
        report["est_vwap"] = est
        if side == "BUY" and est > client_price_cap:
            report["accepted"] = False
            report["reason"] = "VWAP exceeds client cap"
            return report

        report["accepted"] = True
        for _ in range(max_iters):
            if qty_left <= 0:
                break
            books = self.api.get_books(symbol)
            plan = self._plan(side, qty_left, books)
            oids = self._send_ioc(side, plan)
            filled = sum(p["qty"] for p in plan)
            qty_left -= filled
            report["legs"].append({"plan": plan, "oids": oids})
            time.sleep(self.cfg.refresh_delay)

        report["residual"] = qty_left
        return report


# =====================================================
# MAIN EXECUTION
# =====================================================
def main():
    if API_KEY == "PUT_YOUR_RIT_API_KEY_HERE":
        print("❌ Please paste your RIT API key at the top of this script.")
        return

    api = RITAPI(API_KEY)
    sor = SmartSOR(api, SORConfig())

    api.print_heartbeat()
    print("\nStarting Smart Order Router...\n")

    report = sor.route(SYMBOL, SIDE, QTY, CAP, MAX_ITERS)

    print("\n=== SMART SOR REPORT ===")
    for k, v in report.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
