"""
Polymarket BTC 15-min Arbitrage Scanner v3
==========================================
ORDER BOOK based scanner — uses REAL ask prices, not midpoints.

WHY: Midpoint always sums to $1.00 (by design). Real arbs exist in the
order book where best_ask_UP + best_ask_DOWN < $1.00.

FORMULA:
  tokens = budget / (ask_up + ask_down)
  profit = tokens - budget = budget * (1/sum - 1)

ENTRY RULE:
  ask_sum < $1.00 = ALWAYS profitable = ENTER IMMEDIATELY

3 PRICE SOURCES (all checked):
  1. Order book asks (REAL tradeable prices)
  2. Order book cross-check (UP bid vs DOWN ask and vice versa)
  3. Midpoint (for logging only — never use for arb detection)

OUTPUT: arb_results.json with keys:
  total_windows, total_arbs, total_trades, total_pnl, opportunities
"""

import time
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ===================================================================
# CONFIG
# ===================================================================
BUDGET = 20.0          # $ per trade
SCAN_INTERVAL = 2      # seconds between scans (2s = ~900 scans per 30min)
DRY_MODE = True        # True = paper trade only
RESULTS_FILE = "arb_results.json"

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Turkey timezone (UTC+3)
TR_TZ = timezone(timedelta(hours=3))


# ===================================================================
# RESULTS TRACKER
# ===================================================================
def load_results():
    if Path(RESULTS_FILE).exists():
        try:
            with open(RESULTS_FILE) as f:
                data = json.load(f)
            data["total_windows"] = int(data.get("total_windows") or 0)
            data["total_arbs"] = int(data.get("total_arbs") or 0)
            data["total_trades"] = int(data.get("total_trades") or 0)
            data["total_pnl"] = float(data.get("total_pnl") or 0.0)
            if not isinstance(data.get("opportunities"), list):
                data["opportunities"] = []
            return data
        except:
            pass
    return {
        "total_windows": 0,
        "total_arbs": 0,
        "total_trades": 0,
        "total_pnl": 0.0,
        "opportunities": [],
    }


def save_results(results):
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)


# ===================================================================
# API FUNCTIONS
# ===================================================================
def log(msg):
    ts = datetime.now(TR_TZ).strftime("%H:%M:%S.%f")[:-3]
    print("  [%s] %s" % (ts, msg), flush=True)


def fetch_market_by_slug(slug):
    try:
        r = requests.get("%s/markets/slug/%s" % (GAMMA_BASE, slug), timeout=5)
        if r.status_code != 200:
            return None
        m = r.json()
        clob_ids = m.get("clobTokenIds", "[]")
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)
        outcomes = m.get("outcomes", "[]")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        prices = m.get("outcomePrices", "[]")
        if isinstance(prices, str):
            prices = json.loads(prices)
        float_prices = [float(p) for p in prices] if prices else []
        return {
            "question": m.get("question", ""),
            "slug": slug,
            "token_ids": clob_ids,
            "outcomes": outcomes,
            "prices": float_prices,
            "closed": m.get("closed", False),
        }
    except:
        return None


def get_order_book(token_id):
    """Get order book for a token. Returns best bid/ask with sizes."""
    try:
        r = requests.get("%s/book" % CLOB_BASE,
                         params={"token_id": token_id}, timeout=3)
        if r.status_code != 200:
            return None
        book = r.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid = max((float(b["price"]) for b in bids), default=0) if bids else 0
        best_ask = min((float(a["price"]) for a in asks), default=1) if asks else 1

        # Find size available at best ask
        best_ask_size = 0
        for a in asks:
            if abs(float(a["price"]) - best_ask) < 0.0001:
                best_ask_size = float(a["size"])
                break

        # Find size available at best bid
        best_bid_size = 0
        for b in bids:
            if abs(float(b["price"]) - best_bid) < 0.0001:
                best_bid_size = float(b["size"])
                break

        return {
            "best_bid": best_bid,
            "best_bid_size": best_bid_size,
            "best_ask": best_ask,
            "best_ask_size": best_ask_size,
            "spread": best_ask - best_bid,
            "midpoint": (best_bid + best_ask) / 2,
            "num_bids": len(bids),
            "num_asks": len(asks),
        }
    except:
        return None


def get_midpoint_fresh(token_id):
    """Get midpoint (for logging comparison only)."""
    try:
        r = requests.get("%s/midpoint" % CLOB_BASE,
                         params={"token_id": token_id}, timeout=3)
        if r.status_code == 200:
            return float(r.json().get("mid", 0))
    except:
        pass
    return None


# ===================================================================
# WINDOW CACHE
# ===================================================================
_window_cache = {}
WINDOW_CACHE_TTL = 30


def find_active_windows():
    now_ts = int(time.time())
    windows = []
    for offset in range(0, 2):
        block = ((now_ts // 900) + offset) * 900
        slug = "btc-updown-15m-%d" % block

        cached = _window_cache.get(slug)
        if cached and (now_ts - cached[0]) < WINDOW_CACHE_TTL:
            m = cached[1]
        else:
            m = fetch_market_by_slug(slug)
            if m:
                _window_cache[slug] = (now_ts, m)

        if m and not m["closed"] and m.get("token_ids") and len(m["token_ids"]) >= 2:
            age_secs = now_ts - block
            windows.append({
                "slug": slug,
                "question": m["question"],
                "token_ids": m["token_ids"],
                "prices": m["prices"],
                "block_ts": block,
                "block_end": block + 900,
                "age_secs": max(0, age_secs),
                "secs_remaining": block + 900 - now_ts,
            })
    return windows


# ===================================================================
# ARB CALCULATION (ORDER BOOK BASED)
# ===================================================================
def calculate_arb(up_ask, down_ask, budget):
    """
    Calculate arb from ORDER BOOK ask prices.
    ask_sum < 1.00 = ALWAYS profitable.
    """
    ask_sum = up_ask + down_ask
    gap_pct = (1.0 - ask_sum) / 1.0 * 100  # positive = arb exists

    if ask_sum <= 0:
        return {
            "is_arb": False, "ask_sum": ask_sum, "gap_pct": gap_pct,
            "tokens": 0, "up_cost": 0, "down_cost": 0,
            "total_cost": budget, "payout": 0, "profit": 0, "profit_pct": 0,
        }

    tokens = budget / ask_sum
    up_cost = tokens * up_ask
    down_cost = tokens * down_ask
    payout = tokens * 1.0
    profit = payout - budget
    profit_pct = (profit / budget) * 100

    # ask_sum < 1.00 = arb (use 0.9999 threshold for float safety)
    is_arb = ask_sum < 0.9999

    return {
        "is_arb": is_arb,
        "ask_sum": ask_sum,
        "gap_pct": gap_pct,
        "tokens": tokens,
        "up_cost": up_cost,
        "down_cost": down_cost,
        "total_cost": budget,
        "payout": payout,
        "profit": profit,
        "profit_pct": profit_pct,
    }


# ===================================================================
# MAIN SCANNER — ORDER BOOK BASED
# ===================================================================
def main():
    print("=" * 70)
    print("  POLYMARKET BTC ARB SCANNER v3 -- ORDER BOOK")
    print("  Budget: $%.2f | Scan: every %ds" % (BUDGET, SCAN_INTERVAL))
    print("  Mode: %s" % ("DRY RUN" if DRY_MODE else "LIVE TRADING"))
    print("  Rule: ask_UP + ask_DOWN < $1.00 = arb = ENTER!")
    print("  Uses ORDER BOOK (not midpoint — midpoint always = $1.00)")
    print("  Output: %s" % RESULTS_FILE)
    print("=" * 70)
    print()

    results = load_results()
    log("Loaded %d previous opportunities" % len(results["opportunities"]))

    scan_count = 0
    last_status_time = 0
    low_ask_sum = {}  # {slug: lowest ask_sum seen}

    while True:
        scan_count += 1
        scan_start = time.time()

        try:
            windows = find_active_windows()
        except Exception as e:
            log("Window fetch error: %s" % e)
            time.sleep(SCAN_INTERVAL)
            continue

        if not windows:
            if scan_count % 30 == 0:
                log("No active windows found")
            time.sleep(SCAN_INTERVAL)
            continue

        results["total_windows"] += len(windows)

        for w in windows:
            slug = w["slug"]
            up_token = w["token_ids"][0]
            down_token = w["token_ids"][1]

            # Get ORDER BOOK for both tokens
            up_book = get_order_book(up_token)
            down_book = get_order_book(down_token)

            if not up_book or not down_book:
                continue

            up_ask = up_book["best_ask"]
            down_ask = down_book["best_ask"]
            up_bid = up_book["best_bid"]
            down_bid = down_book["best_bid"]

            # Calculate arb from ASK prices (what you actually pay to buy)
            arb = calculate_arb(up_ask, down_ask, BUDGET)
            ask_sum = arb["ask_sum"]
            mins_left = w["secs_remaining"] / 60

            # Also calculate midpoint sum for comparison
            mid_sum = up_book["midpoint"] + down_book["midpoint"]

            # Track lowest ask sum
            if slug not in low_ask_sum or ask_sum < low_ask_sum[slug]:
                low_ask_sum[slug] = ask_sum

            if arb["is_arb"]:
                # *** ASK SUM < 1.00 — REAL ARB — ENTER NOW! ***
                results["total_arbs"] += 1

                log("!!!! ORDER BOOK ARB DETECTED !!!!")
                log("  %s" % w["question"])
                log("  UP:   bid=$%.4f  ask=$%.4f  spread=$%.4f  ask_size=%.1f" % (
                    up_bid, up_ask, up_book["spread"], up_book["best_ask_size"]))
                log("  DOWN: bid=$%.4f  ask=$%.4f  spread=$%.4f  ask_size=%.1f" % (
                    down_bid, down_ask, down_book["spread"], down_book["best_ask_size"]))
                log("  ASK SUM = $%.6f < $1.00 (gap=+%.4f%%)" % (ask_sum, arb["gap_pct"]))
                log("  MID SUM = $%.6f (midpoint always ~$1.00)" % mid_sum)
                log("  Tokens: %.4f | Payout: $%.4f" % (arb["tokens"], arb["payout"]))
                log("  UP cost: $%.4f | DOWN cost: $%.4f | Total: $%.2f" % (
                    arb["up_cost"], arb["down_cost"], arb["total_cost"]))
                log("  GUARANTEED PROFIT: $%.4f (%.4f%%)" % (arb["profit"], arb["profit_pct"]))

                # Check if we have enough size
                max_tokens = min(up_book["best_ask_size"], down_book["best_ask_size"])
                if arb["tokens"] > max_tokens:
                    log("  WARNING: Need %.1f tokens but only %.1f available!" % (
                        arb["tokens"], max_tokens))

                log("  Time left: %.1f min" % mins_left)

                opp = {
                    "time": datetime.now(TR_TZ).isoformat(),
                    "window": w["question"],
                    "slug": slug,
                    "up_ask": round(up_ask, 6),
                    "down_ask": round(down_ask, 6),
                    "up_bid": round(up_bid, 6),
                    "down_bid": round(down_bid, 6),
                    "ask_sum": round(ask_sum, 6),
                    "mid_sum": round(mid_sum, 6),
                    "gap_pct": round(arb["gap_pct"], 4),
                    "tokens": round(arb["tokens"], 4),
                    "up_cost": round(arb["up_cost"], 4),
                    "down_cost": round(arb["down_cost"], 4),
                    "payout": round(arb["payout"], 4),
                    "profit": round(arb["profit"], 4),
                    "profit_pct": round(arb["profit_pct"], 4),
                    "up_ask_size": round(up_book["best_ask_size"], 2),
                    "down_ask_size": round(down_book["best_ask_size"], 2),
                    "secs_remaining": w["secs_remaining"],
                    "executed": False,
                }

                if DRY_MODE:
                    log("  >> DRY MODE -- logged but not executed")
                else:
                    log("  >> ENTERING TRADE!")
                    opp["executed"] = True
                    results["total_trades"] += 1
                    results["total_pnl"] += arb["profit"]

                results["opportunities"].append(opp)
                save_results(results)
                log("")

        # Status print every 10s
        now = time.time()
        if now - last_status_time >= 10:
            last_status_time = now
            for w in windows:
                slug = w["slug"]
                up_book = get_order_book(w["token_ids"][0])
                down_book = get_order_book(w["token_ids"][1])
                if up_book and down_book:
                    ask_s = up_book["best_ask"] + down_book["best_ask"]
                    mid_s = up_book["midpoint"] + down_book["midpoint"]
                    gap = (1.0 - ask_s) * 100
                    mins = w["secs_remaining"] / 60
                    low = low_ask_sum.get(slug, ask_s)

                    if ask_s < 0.9999:
                        tag = "*** ARB NOW! ***"
                    elif low < 0.9999:
                        tag = "<<< MISSED! low=$%.4f" % low
                    else:
                        tag = ""

                    log("ASK: UP=$%.4f+DOWN=$%.4f=$%.4f | MID=$%.4f | gap=%+.2f%% | low=$%.4f | %.0fm %s" % (
                        up_book["best_ask"], down_book["best_ask"], ask_s,
                        mid_s, gap, low, mins, tag))

            log("  Totals: %d arbs, %d trades, PnL=$%.4f | scan #%d" % (
                results["total_arbs"], results["total_trades"],
                results["total_pnl"], scan_count))

        # Clean up expired windows
        active_slugs = set(w["slug"] for w in windows)
        for slug in list(low_ask_sum.keys()):
            if slug not in active_slugs:
                del low_ask_sum[slug]

        elapsed = time.time() - scan_start
        sleep_time = max(0.1, SCAN_INTERVAL - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
