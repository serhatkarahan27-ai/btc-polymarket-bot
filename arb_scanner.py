"""
Polymarket BTC 15-min Arbitrage Scanner
========================================
Scans for arbitrage when UP + DOWN < $1.00.

CORRECT FORMULA:
  tokens = budget / (up_price + down_price)
  up_cost = tokens * up_price
  down_cost = tokens * down_price
  total_cost = budget (always)
  payout = tokens * $1.00 (either outcome wins)
  profit = tokens - budget = budget * (1/sum - 1)

ENTRY RULE:
  Enter when sum < $1.00 (any amount!)
  Profit is always positive when sum < 1.00

Example sum=$0.99, budget=$20:
  tokens = 20 / 0.99 = 20.20
  profit = +$0.20 guaranteed!

Example sum=$0.98, budget=$20:
  tokens = 20 / 0.98 = 20.41
  profit = +$0.41 guaranteed!

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
SCAN_INTERVAL = 5      # seconds between scans
MIN_PROFIT = 0.00      # $0 min — any sum < 1.00 is an arb
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
    """Load or initialize arb_results.json."""
    if Path(RESULTS_FILE).exists():
        try:
            with open(RESULTS_FILE) as f:
                data = json.load(f)
            # Ensure all required keys exist
            data.setdefault("total_windows", 0)
            data.setdefault("total_arbs", 0)
            data.setdefault("total_trades", 0)
            data.setdefault("total_pnl", 0.0)
            data.setdefault("opportunities", [])
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
    """Save arb_results.json with correct format."""
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)


# ===================================================================
# API FUNCTIONS
# ===================================================================
_midpoint_cache = {}
CACHE_TTL = 3


def log(msg):
    ts = datetime.now(TR_TZ).strftime("%H:%M:%S")
    print("  [%s] %s" % (ts, msg), flush=True)


def fetch_market_by_slug(slug):
    try:
        r = requests.get("%s/markets/slug/%s" % (GAMMA_BASE, slug), timeout=8)
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


def get_clob_midpoint(token_id):
    now = time.time()
    cached = _midpoint_cache.get(token_id)
    if cached and (now - cached[0]) < CACHE_TTL:
        return cached[1]
    try:
        r = requests.get("%s/midpoint" % CLOB_BASE,
                         params={"token_id": token_id}, timeout=5)
        if r.status_code == 200:
            price = float(r.json().get("mid", 0))
            _midpoint_cache[token_id] = (now, price)
            return price
    except:
        pass
    return None


def find_active_windows():
    """Find all active 15-min BTC windows (current + next)."""
    now_ts = int(time.time())
    windows = []
    for offset in range(0, 3):
        block = ((now_ts // 900) + offset) * 900
        slug = "btc-updown-15m-%d" % block
        m = fetch_market_by_slug(slug)
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
# ARB CALCULATION (CORRECT FORMULA)
# ===================================================================
def calculate_arb(up_price, down_price, budget):
    """
    Calculate arbitrage opportunity.
    sum < 1.00 = ALWAYS profitable, enter immediately.
    """
    price_sum = up_price + down_price

    if price_sum <= 0 or price_sum >= 1.0:
        return {
            "is_arb": False,
            "sum": price_sum,
            "tokens": 0,
            "up_cost": 0,
            "down_cost": 0,
            "total_cost": budget,
            "payout": 0,
            "profit": 0,
            "profit_pct": 0,
        }

    # sum < 1.00 — THIS IS AN ARB! Always profitable!
    tokens = budget / price_sum
    up_cost = tokens * up_price
    down_cost = tokens * down_price
    payout = tokens * 1.0  # winner pays $1.00 per token
    profit = payout - budget  # = budget * (1/sum - 1)
    profit_pct = (profit / budget) * 100

    return {
        "is_arb": True,  # sum < 1.00 = always an arb
        "sum": price_sum,
        "tokens": tokens,
        "up_cost": up_cost,
        "down_cost": down_cost,
        "total_cost": budget,
        "payout": payout,
        "profit": profit,
        "profit_pct": profit_pct,
    }


# ===================================================================
# SCANNER
# ===================================================================
def scan_once(results):
    """Scan all active windows for arb opportunities.
    When sum < 1.00: immediately log and enter trade."""
    windows = find_active_windows()
    results["total_windows"] += len(windows)

    if not windows:
        return 0

    arbs_found = 0

    for w in windows:
        up_token = w["token_ids"][0]
        down_token = w["token_ids"][1]

        up_price = get_clob_midpoint(up_token)
        down_price = get_clob_midpoint(down_token)

        if up_price is None or down_price is None:
            continue

        arb = calculate_arb(up_price, down_price, BUDGET)
        price_sum = arb["sum"]
        mins_left = w["secs_remaining"] / 60

        if arb["is_arb"]:
            # *** SUM < 1.00 DETECTED — THIS IS AN ARB! ***
            arbs_found += 1
            results["total_arbs"] += 1

            log("*** ARB DETECTED! sum=$%.4f < $1.00 ***" % price_sum)
            log("  Window: %s" % w["question"])
            log("  UP=$%.4f + DOWN=$%.4f = $%.4f" % (up_price, down_price, price_sum))
            log("  Tokens: %.4f pairs @ $%.4f each" % (arb["tokens"], price_sum))
            log("  UP cost: $%.4f | DOWN cost: $%.4f" % (arb["up_cost"], arb["down_cost"]))
            log("  Total cost: $%.2f (= budget)" % arb["total_cost"])
            log("  Payout: $%.4f (either UP or DOWN wins)" % arb["payout"])
            log("  GUARANTEED PROFIT: $%.4f (%.3f%%)" % (arb["profit"], arb["profit_pct"]))
            log("  Time remaining: %.1f min" % mins_left)

            # Record opportunity
            opp = {
                "time": datetime.now(TR_TZ).isoformat(),
                "window": w["question"],
                "slug": w["slug"],
                "up_price": round(up_price, 6),
                "down_price": round(down_price, 6),
                "sum": round(price_sum, 6),
                "tokens": round(arb["tokens"], 4),
                "up_cost": round(arb["up_cost"], 4),
                "down_cost": round(arb["down_cost"], 4),
                "payout": round(arb["payout"], 4),
                "profit": round(arb["profit"], 4),
                "profit_pct": round(arb["profit_pct"], 4),
                "secs_remaining": w["secs_remaining"],
                "executed": False,
            }

            if DRY_MODE:
                log("  >> DRY MODE — not executing trade")
                opp["executed"] = False
            else:
                # LIVE MODE: execute trade here
                log("  >> ENTERING TRADE!")
                opp["executed"] = True
                results["total_trades"] += 1
                results["total_pnl"] += arb["profit"]

            results["opportunities"].append(opp)
            save_results(results)
            log("")

    return arbs_found


def main():
    print("=" * 70)
    print("  POLYMARKET BTC ARB SCANNER")
    print("  Budget: $%.2f per trade" % BUDGET)
    print("  Mode: %s" % ("DRY RUN" if DRY_MODE else "LIVE TRADING"))
    print("  Rule: sum < $1.00 = arb = always enter!")
    print("  Output: %s" % RESULTS_FILE)
    print("=" * 70)
    print()

    results = load_results()
    log("Loaded %d previous opportunities" % len(results["opportunities"]))
    scan_count = 0
    last_status_time = 0

    while True:
        scan_count += 1
        try:
            arbs = scan_once(results)
        except Exception as e:
            log("Scan error: %s" % e)
            time.sleep(SCAN_INTERVAL)
            continue

        # Print status every 60s even if no arb found
        now = time.time()
        if now - last_status_time >= 60:
            last_status_time = now
            # Quick check current prices
            windows = find_active_windows()
            for w in windows:
                up_p = get_clob_midpoint(w["token_ids"][0])
                down_p = get_clob_midpoint(w["token_ids"][1])
                if up_p and down_p:
                    s = up_p + down_p
                    gap = s - 1.0
                    mins = w["secs_remaining"] / 60
                    status = "ARB!" if s < 1.0 else "no arb"
                    log("Scan #%d: UP=$%.4f DOWN=$%.4f sum=$%.4f (gap=%+.4f) %.0fm left [%s]" % (
                        scan_count, up_p, down_p, s, gap, mins, status))

            # Summary
            log("  Totals: %d arbs found, %d trades, PnL=$%.4f" % (
                results["total_arbs"], results["total_trades"], results["total_pnl"]))

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
