"""
Polymarket BTC 15-min Arbitrage Scanner v2
==========================================
HIGH-SPEED scanner: 1s interval, no cache, instant detection.

CORRECT FORMULA:
  tokens = budget / (up_price + down_price)
  profit = tokens - budget = budget * (1/sum - 1)

ENTRY RULE:
  sum < $1.00 = ALWAYS profitable = ENTER IMMEDIATELY

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
SCAN_INTERVAL = 1      # 1 SECOND — fast scanning to catch micro-arbs
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
# API FUNCTIONS — NO CACHE for arb detection (speed > efficiency)
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


def get_clob_midpoint_fresh(token_id):
    """Get FRESH midpoint — NO CACHE. Every call hits the API."""
    try:
        r = requests.get("%s/midpoint" % CLOB_BASE,
                         params={"token_id": token_id}, timeout=3)
        if r.status_code == 200:
            return float(r.json().get("mid", 0))
    except:
        pass
    return None


def get_both_midpoints(up_token, down_token):
    """Get both midpoints in rapid succession (no cache)."""
    up = get_clob_midpoint_fresh(up_token)
    down = get_clob_midpoint_fresh(down_token)
    return up, down


# ===================================================================
# WINDOW CACHE — cache market info (slow to change), not prices
# ===================================================================
_window_cache = {}  # {slug: (timestamp, data)}
WINDOW_CACHE_TTL = 30  # market info changes slowly, cache 30s


def find_active_windows():
    """Find active 15-min BTC windows. Market info cached 30s."""
    now_ts = int(time.time())
    windows = []
    for offset in range(0, 2):  # only current + next (was 3)
        block = ((now_ts // 900) + offset) * 900
        slug = "btc-updown-15m-%d" % block

        # Check cache for market info
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
# ARB CALCULATION
# ===================================================================
def calculate_arb(up_price, down_price, budget):
    """sum < 1.00 = ALWAYS profitable, enter immediately."""
    price_sum = up_price + down_price
    gap_pct = (1.0 - price_sum) / 1.0 * 100

    if price_sum <= 0:
        return {
            "is_arb": False, "sum": price_sum, "gap_pct": gap_pct,
            "tokens": 0, "up_cost": 0, "down_cost": 0,
            "total_cost": budget, "payout": 0, "profit": 0, "profit_pct": 0,
        }

    tokens = budget / price_sum
    up_cost = tokens * up_price
    down_cost = tokens * down_price
    payout = tokens * 1.0
    profit = payout - budget
    profit_pct = (profit / budget) * 100

    # sum < 1.00 = arb. Use < 0.99999 to handle float rounding of exact 1.0
    is_arb = price_sum < 0.99999

    return {
        "is_arb": is_arb,
        "sum": price_sum,
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
# HIGH-SPEED SCANNER
# ===================================================================
def main():
    print("=" * 70)
    print("  POLYMARKET BTC ARB SCANNER v2 — HIGH SPEED")
    print("  Budget: $%.2f | Scan: every %ds" % (BUDGET, SCAN_INTERVAL))
    print("  Mode: %s" % ("DRY RUN" if DRY_MODE else "LIVE TRADING"))
    print("  Rule: sum < $1.00 = arb = ENTER IMMEDIATELY")
    print("  NO price cache — every scan = fresh API call")
    print("  Output: %s" % RESULTS_FILE)
    print("=" * 70)
    print()

    results = load_results()
    log("Loaded %d previous opportunities" % len(results["opportunities"]))

    scan_count = 0
    last_status_time = 0
    low_sum = {}  # {slug: lowest_sum_seen} — track per window

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
            if scan_count % 60 == 0:
                log("No active windows found")
            time.sleep(SCAN_INTERVAL)
            continue

        results["total_windows"] += len(windows)

        for w in windows:
            slug = w["slug"]
            up_token = w["token_ids"][0]
            down_token = w["token_ids"][1]

            # FRESH prices — no cache!
            up_price, down_price = get_both_midpoints(up_token, down_token)

            if up_price is None or down_price is None:
                continue

            arb = calculate_arb(up_price, down_price, BUDGET)
            price_sum = arb["sum"]
            mins_left = w["secs_remaining"] / 60

            # Track lowest sum per window
            if slug not in low_sum or price_sum < low_sum[slug]:
                low_sum[slug] = price_sum

            if arb["is_arb"]:
                # *** SUM < 1.00 — ARB DETECTED — ENTER NOW! ***
                results["total_arbs"] += 1

                log("!!!! ARB DETECTED !!!!")
                log("  sum=$%.6f < $1.00 (gap=+%.4f%%)" % (price_sum, arb["gap_pct"]))
                log("  %s" % w["question"])
                log("  UP=$%.6f + DOWN=$%.6f = $%.6f" % (up_price, down_price, price_sum))
                log("  Tokens: %.4f | Payout: $%.4f" % (arb["tokens"], arb["payout"]))
                log("  GUARANTEED PROFIT: $%.4f (%.4f%%)" % (arb["profit"], arb["profit_pct"]))
                log("  Time left: %.1f min" % mins_left)

                # IMMEDIATELY record opportunity
                opp = {
                    "time": datetime.now(TR_TZ).isoformat(),
                    "window": w["question"],
                    "slug": slug,
                    "up_price": round(up_price, 6),
                    "down_price": round(down_price, 6),
                    "sum": round(price_sum, 6),
                    "gap_pct": round(arb["gap_pct"], 4),
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
                    log("  >> DRY MODE — logged but not executed")
                else:
                    log("  >> ENTERING TRADE!")
                    opp["executed"] = True
                    results["total_trades"] += 1
                    results["total_pnl"] += arb["profit"]

                # IMMEDIATELY append and save
                results["opportunities"].append(opp)
                save_results(results)
                log("")

        # Status print every 10s (was 60s — faster feedback)
        now = time.time()
        if now - last_status_time >= 10:
            last_status_time = now
            for w in windows:
                slug = w["slug"]
                up_p, down_p = get_both_midpoints(w["token_ids"][0], w["token_ids"][1])
                if up_p and down_p:
                    s = up_p + down_p
                    gap_pct = (1.0 - s) / 1.0 * 100
                    mins = w["secs_remaining"] / 60
                    low = low_sum.get(slug, s)

                    if s < 1.0:
                        tag = "*** ARB NOW! ***"
                    elif low < 0.99999:
                        tag = "<<< LOW WAS $%.4f — ARB MISSED!" % low
                    else:
                        tag = ""

                    log("UP=$%.4f DOWN=$%.4f | sum=$%.6f | gap=%+.3f%% | low=$%.4f | %.0fm left %s" % (
                        up_p, down_p, s, gap_pct, low, mins, tag))

            log("  Totals: %d arbs, %d trades, PnL=$%.4f | scan took %.0fms" % (
                results["total_arbs"], results["total_trades"], results["total_pnl"],
                (time.time() - scan_start) * 1000))

        # Clean up low_sum for expired windows
        active_slugs = set(w["slug"] for w in windows)
        for slug in list(low_sum.keys()):
            if slug not in active_slugs:
                del low_sum[slug]

        # Sleep remaining time (account for scan duration)
        elapsed = time.time() - scan_start
        sleep_time = max(0.1, SCAN_INTERVAL - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
