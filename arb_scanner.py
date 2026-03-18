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
"""

import time
import json
import requests
from datetime import datetime, timezone, timedelta

# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════
BUDGET = 20.0          # $ per trade
SCAN_INTERVAL = 5      # seconds between scans
MIN_PROFIT = 0.01      # minimum profit to report ($)
DRY_MODE = True        # True = paper trade only

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Turkey timezone (UTC+3)
TR_TZ = timezone(timedelta(hours=3))


# ═══════════════════════════════════════════════════════════════════
# API FUNCTIONS
# ═══════════════════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════════════════
# ARB CALCULATION (CORRECT FORMULA)
# ═══════════════════════════════════════════════════════════════════
def calculate_arb(up_price, down_price, budget):
    """
    Calculate arbitrage opportunity.

    Returns dict with:
      - profitable: bool
      - sum: UP + DOWN price
      - tokens: number of token pairs to buy
      - up_cost: cost to buy UP tokens
      - down_cost: cost to buy DOWN tokens
      - total_cost: always = budget
      - payout: tokens * $1.00
      - profit: guaranteed profit
      - profit_pct: profit as % of budget
    """
    price_sum = up_price + down_price

    if price_sum <= 0 or price_sum >= 1.0:
        return {
            "profitable": False,
            "sum": price_sum,
            "tokens": 0,
            "up_cost": 0,
            "down_cost": 0,
            "total_cost": budget,
            "payout": 0,
            "profit": 0,
            "profit_pct": 0,
        }

    # Core formula: buy equal number of UP and DOWN tokens
    tokens = budget / price_sum
    up_cost = tokens * up_price
    down_cost = tokens * down_price
    payout = tokens * 1.0  # winner pays $1.00 per token
    profit = payout - budget  # = budget * (1/sum - 1)
    profit_pct = (profit / budget) * 100

    return {
        "profitable": profit >= MIN_PROFIT,
        "sum": price_sum,
        "tokens": tokens,
        "up_cost": up_cost,
        "down_cost": down_cost,
        "total_cost": budget,
        "payout": payout,
        "profit": profit,
        "profit_pct": profit_pct,
    }


# ═══════════════════════════════════════════════════════════════════
# SCANNER
# ═══════════════════════════════════════════════════════════════════
def scan_once():
    """Scan all active windows for arb opportunities."""
    windows = find_active_windows()
    if not windows:
        return []

    opportunities = []
    for w in windows:
        up_token = w["token_ids"][0]
        down_token = w["token_ids"][1]

        up_price = get_clob_midpoint(up_token)
        down_price = get_clob_midpoint(down_token)

        if up_price is None or down_price is None:
            continue

        arb = calculate_arb(up_price, down_price, BUDGET)

        result = {
            "window": w["question"],
            "slug": w["slug"],
            "up_price": up_price,
            "down_price": down_price,
            "secs_remaining": w["secs_remaining"],
            **arb,
        }
        opportunities.append(result)

    return opportunities


def main():
    print("=" * 70)
    print("  POLYMARKET BTC ARB SCANNER")
    print("  Budget: $%.2f | Min profit: $%.2f" % (BUDGET, MIN_PROFIT))
    print("  Mode: %s" % ("DRY RUN" if DRY_MODE else "LIVE"))
    print("  Entry rule: sum < $1.00 = always profitable")
    print("=" * 70)
    print()

    trades_log = []
    scan_count = 0

    while True:
        scan_count += 1
        try:
            opps = scan_once()
        except Exception as e:
            log("Scan error: %s" % e)
            time.sleep(SCAN_INTERVAL)
            continue

        if not opps:
            if scan_count % 12 == 0:  # every ~60s
                log("No active windows found")
            time.sleep(SCAN_INTERVAL)
            continue

        for opp in opps:
            price_sum = opp["sum"]
            up_p = opp["up_price"]
            down_p = opp["down_price"]
            mins_left = opp["secs_remaining"] / 60

            if opp["profitable"]:
                log("*** ARB FOUND ***")
                log("  %s" % opp["window"])
                log("  UP=$%.3f + DOWN=$%.3f = $%.4f (< $1.00!)" % (up_p, down_p, price_sum))
                log("  Tokens: %.2f pairs" % opp["tokens"])
                log("  UP cost: $%.2f | DOWN cost: $%.2f" % (opp["up_cost"], opp["down_cost"]))
                log("  Payout: $%.2f (either outcome)" % opp["payout"])
                log("  GUARANTEED PROFIT: $%.4f (%.2f%%)" % (opp["profit"], opp["profit_pct"]))
                log("  Time remaining: %.1f min" % mins_left)

                if DRY_MODE:
                    log("  [DRY MODE - not executing]")

                trades_log.append({
                    "time": datetime.now(TR_TZ).isoformat(),
                    "window": opp["window"],
                    "up_price": up_p,
                    "down_price": down_p,
                    "sum": price_sum,
                    "profit": opp["profit"],
                    "profit_pct": opp["profit_pct"],
                    "executed": not DRY_MODE,
                })

                # Save log
                with open("arb_scan_log.json", "w") as f:
                    json.dump(trades_log, f, indent=2)

                log("")
            else:
                # Not profitable — show status periodically
                if scan_count % 12 == 0:
                    gap = price_sum - 1.0
                    log("Scan #%d: UP=$%.3f DOWN=$%.3f sum=$%.4f (gap=+$%.4f) | %.0fm left" % (
                        scan_count, up_p, down_p, price_sum, gap, mins_left))

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
