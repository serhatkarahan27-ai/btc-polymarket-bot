"""
Polymarket BTC 15-min Arbitrage Scanner
=======================================
Scans for mispricing: if UP + DOWN < $0.995, buy both for guaranteed profit.

Theory:
  UP resolves to $1 if BTC goes up, DOWN resolves to $1 if BTC goes down.
  One of them ALWAYS resolves to $1.00.
  If UP + DOWN < $1.00 → buy both → guaranteed profit = $1.00 - (UP + DOWN)

  Entry: $10 on UP + $10 on DOWN = $20 total
  Return: $10/UP_price * $1.00 (if UP wins) + $10/DOWN_price * $0.00 = $10/UP_price
    OR    $10/UP_price * $0.00 + $10/DOWN_price * $1.00 = $10/DOWN_price

  Actual profit formula:
    If UP wins:  tokens_up * $1.00 + tokens_down * $0.00 = tokens_up
    If DOWN wins: tokens_up * $0.00 + tokens_down * $1.00 = tokens_down
    tokens_up = $10 / UP_price
    tokens_down = $10 / DOWN_price
    Guaranteed return = min(tokens_up, tokens_down) ... NO!
    Actually we get ONE side paid out:
      If UP wins:  payout = 10/UP_price * 1.00 = 10/UP_price
      If DOWN wins: payout = 10/DOWN_price * 1.00 = 10/DOWN_price
    Worst case = min(10/UP, 10/DOWN) - already profited if min > 10
    Best case = max(10/UP, 10/DOWN)

  Simple case: UP=$0.49, DOWN=$0.49, total=$0.98
    Buy 10/0.49=20.41 UP tokens, 10/0.49=20.41 DOWN tokens. Cost=$20
    If UP wins: 20.41 * $1 = $20.41. Profit = $0.41
    If DOWN wins: 20.41 * $1 = $20.41. Profit = $0.41
    Guaranteed profit = $0.41

dry_mode = True always.
"""
import time
import json
import requests
import sys
import os
import functools
from datetime import datetime, timezone

# Force unbuffered output
print = functools.partial(print, flush=True)

# ============================================================
# CONFIG
# ============================================================
TRADE_SIZE = 10.0            # $ per side
ARB_THRESHOLD = 0.995        # Enter if UP + DOWN < this
SCAN_INTERVAL_PRE = 1.0      # seconds between checks (pre-window)
SCAN_INTERVAL_FAST = 0.5     # seconds between checks (post-open, first 30s)
PRE_WINDOW_SECS = 60         # start scanning 60s before window
POST_OPEN_FAST_SECS = 30     # fast-scan for 30s after open
DRY_MODE = True              # ALWAYS True

BINANCE_BASE = "https://api.binance.com/api/v3"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
RESULTS_FILE = "arb_results.json"

# ============================================================
# LOGGING
# ============================================================
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print("  [%s] %s" % (ts, msg))


def log_banner(msg):
    print("\n" + "=" * 64)
    print("  %s" % msg)
    print("=" * 64)


# ============================================================
# API FUNCTIONS
# ============================================================
def get_btc_price():
    try:
        r = requests.get("%s/ticker/price" % BINANCE_BASE,
                         params={"symbol": "BTCUSDT"}, timeout=3)
        return float(r.json()["price"])
    except Exception:
        return None


def get_market_by_slug(slug):
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
        return {
            "question": m.get("question", ""),
            "slug": slug,
            "token_ids": clob_ids,
            "outcomes": outcomes,
            "closed": m.get("closed", True),
        }
    except Exception:
        return None


def get_midpoint(token_id):
    """Get CLOB midpoint for a single token. Returns float or None."""
    try:
        r = requests.get("%s/midpoint" % CLOB_BASE,
                         params={"token_id": token_id}, timeout=3)
        if r.status_code == 200:
            mid = float(r.json().get("mid", 0))
            if mid > 0:
                return mid
    except Exception:
        pass
    return None


def get_both_midpoints(token_ids):
    """Get UP and DOWN midpoints. Returns (up, down) or (None, None)."""
    if not token_ids or len(token_ids) < 2:
        return None, None
    up = get_midpoint(token_ids[0])
    down = get_midpoint(token_ids[1])
    return up, down


# ============================================================
# ARBITRAGE MATH
# ============================================================
def calc_arb(up_price, down_price, trade_size=TRADE_SIZE):
    """
    Calculate arbitrage opportunity.
    Buy $trade_size of UP tokens + $trade_size of DOWN tokens.
    One side resolves to $1.00, other to $0.00.

    Returns dict with arb details.
    """
    total_price = up_price + down_price
    total_cost = trade_size * 2  # $10 UP + $10 DOWN = $20

    tokens_up = trade_size / up_price      # e.g. 10/0.49 = 20.41
    tokens_down = trade_size / down_price   # e.g. 10/0.49 = 20.41

    # If UP wins: payout = tokens_up * $1.00
    payout_if_up = tokens_up * 1.00
    # If DOWN wins: payout = tokens_down * $1.00
    payout_if_down = tokens_down * 1.00

    profit_if_up = payout_if_up - total_cost
    profit_if_down = payout_if_down - total_cost

    guaranteed_profit = min(profit_if_up, profit_if_down)
    best_profit = max(profit_if_up, profit_if_down)

    return {
        "up_price": up_price,
        "down_price": down_price,
        "total_price": total_price,
        "total_cost": total_cost,
        "tokens_up": tokens_up,
        "tokens_down": tokens_down,
        "payout_if_up": payout_if_up,
        "payout_if_down": payout_if_down,
        "profit_if_up": profit_if_up,
        "profit_if_down": profit_if_down,
        "guaranteed_profit": guaranteed_profit,
        "best_profit": best_profit,
        "is_arb": total_price < ARB_THRESHOLD,
        "edge_pct": (1.0 - total_price) * 100 if total_price < 1.0 else 0,
    }


# ============================================================
# RESULTS STORAGE
# ============================================================
def load_results():
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "total_scans": 0,
        "total_arb_found": 0,
        "total_entries": 0,
        "total_pnl": 0.0,
        "opportunities": [],
        "trades": [],
    }


def save_results(data):
    with open(RESULTS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    # Force flush to disk
    f = open(RESULTS_FILE, "r")
    f.close()


# ============================================================
# WINDOW MANAGEMENT
# ============================================================
def get_next_block_ts():
    """Get the next 15-min block timestamp."""
    now = int(time.time())
    current_block = (now // 900) * 900
    elapsed = now - current_block
    # If we're within the pre-window scan range, use current block
    if elapsed <= 5:
        return current_block
    return current_block + 900


def find_market_for_block(block_ts):
    slug = "btc-updown-15m-%d" % block_ts
    m = get_market_by_slug(slug)
    if m and not m["closed"]:
        m["block_ts"] = block_ts
        return m
    return None


# ============================================================
# MAIN SCAN LOOP
# ============================================================
def scan_window(block_ts, results):
    """
    Scan a single 15-min window for arbitrage.
    Phase 1: Pre-window (T-60s to T-0s) — check every 1s
    Phase 2: Post-open (T+0s to T+30s) — check every 0.5s
    """
    window_time = datetime.fromtimestamp(block_ts).strftime("%H:%M:%S")
    log_banner("SCANNING WINDOW: %s (block=%d)" % (window_time, block_ts))

    # Find market and tokens
    market = None
    token_ids = None

    # Try to find market starting from T-60s
    now = int(time.time())
    secs_to_open = block_ts - now

    if secs_to_open > PRE_WINDOW_SECS + 10:
        wait_secs = secs_to_open - PRE_WINDOW_SECS - 5
        log("Window in %ds, waiting %ds until T-%ds..." % (secs_to_open, wait_secs, PRE_WINDOW_SECS + 5))
        time.sleep(wait_secs)

    # Fetch market info
    log("Fetching market for block %d..." % block_ts)
    for attempt in range(5):
        market = find_market_for_block(block_ts)
        if market and market.get("token_ids") and len(market["token_ids"]) >= 2:
            token_ids = market["token_ids"]
            log("Market found: %s" % market["question"])
            log("UP token:   %s..." % token_ids[0][:20])
            log("DOWN token: %s..." % token_ids[1][:20])
            break
        log("Market not found yet (attempt %d/5), retrying in 3s..." % (attempt + 1))
        time.sleep(3)

    if not token_ids:
        log("ERROR: Could not find market. Skipping window.")
        return

    scan_count = 0
    arb_found = False
    entry_made = False
    best_opportunity = None

    # ── PHASE 1: Pre-window scan (T-60s to T-0s) ──
    now = int(time.time())
    secs_to_open = block_ts - now

    if secs_to_open > 0:
        log("")
        log("PHASE 1: Pre-window scan (%ds to open, checking every %.1fs)" % (secs_to_open, SCAN_INTERVAL_PRE))
        log("-" * 50)

        while True:
            now = int(time.time())
            secs_left = block_ts - now
            if secs_left <= 0:
                break

            up, down = get_both_midpoints(token_ids)
            scan_count += 1

            if up is not None and down is not None:
                total = up + down
                arb = calc_arb(up, down)
                results["total_scans"] += 1

                if arb["is_arb"]:
                    log("*** PRE-WINDOW ARB FOUND! UP=$%.4f DOWN=$%.4f total=$%.4f edge=%.2f%% ***" %
                        (up, down, total, arb["edge_pct"]))
                    log("    Guaranteed profit: $%.4f | Best case: $%.4f" %
                        (arb["guaranteed_profit"], arb["best_profit"]))
                    arb_found = True
                    results["total_arb_found"] += 1

                    opp = {
                        "time": datetime.now().isoformat(),
                        "window": window_time,
                        "block_ts": block_ts,
                        "phase": "pre-window",
                        "secs_to_open": secs_left,
                        "up_price": up,
                        "down_price": down,
                        "total": total,
                        "edge_pct": arb["edge_pct"],
                        "guaranteed_profit": arb["guaranteed_profit"],
                        "entered": False,
                    }

                    if not entry_made:
                        # ENTER THE TRADE
                        log(">>> ENTERING ARB TRADE (dry_mode=%s) <<<" % DRY_MODE)
                        log("    Buy %.2f UP tokens @ $%.4f ($%.2f)" %
                            (arb["tokens_up"], up, TRADE_SIZE))
                        log("    Buy %.2f DOWN tokens @ $%.4f ($%.2f)" %
                            (arb["tokens_down"], down, TRADE_SIZE))
                        log("    Total cost: $%.2f" % arb["total_cost"])
                        log("    Guaranteed return: $%.4f" %
                            (arb["total_cost"] + arb["guaranteed_profit"]))

                        opp["entered"] = True
                        entry_made = True
                        best_opportunity = arb

                        trade = {
                            "time": datetime.now().isoformat(),
                            "window": window_time,
                            "block_ts": block_ts,
                            "phase": "pre-window",
                            "entry_up": up,
                            "entry_down": down,
                            "total_entry": total,
                            "tokens_up": arb["tokens_up"],
                            "tokens_down": arb["tokens_down"],
                            "cost": arb["total_cost"],
                            "guaranteed_profit": arb["guaranteed_profit"],
                            "status": "open",
                            "pnl": None,
                        }
                        results["trades"].append(trade)
                        results["total_entries"] += 1

                    results["opportunities"].append(opp)
                    save_results(results)
                else:
                    # No arb — show price
                    if scan_count % 5 == 0 or scan_count == 1:
                        log("  T-%3ds | UP=$%.4f DOWN=$%.4f | total=$%.4f | gap=%.3f%%" %
                            (secs_left, up, down, total, (total - 1.0) * 100))
            else:
                if scan_count % 10 == 0:
                    log("  T-%3ds | prices unavailable" % secs_left)

            time.sleep(SCAN_INTERVAL_PRE)

    # ── PHASE 2: Post-open fast scan (T+0s to T+30s) ──
    log("")
    log("PHASE 2: Post-open FAST scan (0.5s intervals for %ds)" % POST_OPEN_FAST_SECS)
    log("-" * 50)

    phase2_start = time.time()
    phase2_scans = 0

    while True:
        elapsed = time.time() - phase2_start
        if elapsed >= POST_OPEN_FAST_SECS:
            break

        up, down = get_both_midpoints(token_ids)
        phase2_scans += 1
        scan_count += 1

        if up is not None and down is not None:
            total = up + down
            arb = calc_arb(up, down)
            results["total_scans"] += 1

            if arb["is_arb"]:
                log("*** POST-OPEN ARB FOUND! UP=$%.4f DOWN=$%.4f total=$%.4f edge=%.2f%% ***" %
                    (up, down, total, arb["edge_pct"]))
                log("    Guaranteed profit: $%.4f | Best case: $%.4f" %
                    (arb["guaranteed_profit"], arb["best_profit"]))
                arb_found = True
                results["total_arb_found"] += 1

                opp = {
                    "time": datetime.now().isoformat(),
                    "window": window_time,
                    "block_ts": block_ts,
                    "phase": "post-open",
                    "secs_after_open": round(elapsed, 1),
                    "up_price": up,
                    "down_price": down,
                    "total": total,
                    "edge_pct": arb["edge_pct"],
                    "guaranteed_profit": arb["guaranteed_profit"],
                    "entered": False,
                }

                if not entry_made:
                    log(">>> ENTERING ARB TRADE (dry_mode=%s) <<<" % DRY_MODE)
                    log("    Buy %.2f UP tokens @ $%.4f ($%.2f)" %
                        (arb["tokens_up"], up, TRADE_SIZE))
                    log("    Buy %.2f DOWN tokens @ $%.4f ($%.2f)" %
                        (arb["tokens_down"], down, TRADE_SIZE))
                    log("    Total cost: $%.2f" % arb["total_cost"])

                    opp["entered"] = True
                    entry_made = True
                    best_opportunity = arb

                    trade = {
                        "time": datetime.now().isoformat(),
                        "window": window_time,
                        "block_ts": block_ts,
                        "phase": "post-open",
                        "secs_after_open": round(elapsed, 1),
                        "entry_up": up,
                        "entry_down": down,
                        "total_entry": total,
                        "tokens_up": arb["tokens_up"],
                        "tokens_down": arb["tokens_down"],
                        "cost": arb["total_cost"],
                        "guaranteed_profit": arb["guaranteed_profit"],
                        "status": "open",
                        "pnl": None,
                    }
                    results["trades"].append(trade)
                    results["total_entries"] += 1

                results["opportunities"].append(opp)
                save_results(results)
            else:
                if phase2_scans % 4 == 0 or phase2_scans == 1:
                    log("  T+%4.1fs | UP=$%.4f DOWN=$%.4f | total=$%.4f | gap=%+.3f%%" %
                        (elapsed, up, down, total, (total - 1.0) * 100))

        time.sleep(SCAN_INTERVAL_FAST)

    # ── PHASE 2b: Continue monitoring until window end (every 10s) ──
    if entry_made:
        log("")
        log("TRADE OPEN — monitoring until window expiry...")
        window_end = block_ts + 900
        while True:
            now = int(time.time())
            remaining = window_end - now
            if remaining <= 0:
                break

            up, down = get_both_midpoints(token_ids)
            if up is not None and down is not None:
                total = up + down
                elapsed_in_window = now - block_ts
                log("  [%3dm%02ds] UP=$%.4f DOWN=$%.4f total=$%.4f" %
                    (elapsed_in_window // 60, elapsed_in_window % 60, up, down, total))

            sleep_time = min(30, remaining)
            time.sleep(sleep_time)

        # Window expired — resolve trade
        log("")
        btc_now = get_btc_price()
        # Determine outcome based on which token is worth more
        up_final, down_final = get_both_midpoints(token_ids)
        if up_final is not None and up_final > 0.7:
            outcome = "UP"
        elif down_final is not None and down_final > 0.7:
            outcome = "DOWN"
        else:
            outcome = "UNKNOWN"

        trade = results["trades"][-1]
        if outcome == "UP":
            payout = trade["tokens_up"] * 1.0
        elif outcome == "DOWN":
            payout = trade["tokens_down"] * 1.0
        else:
            payout = trade["cost"]  # neutral if unknown

        pnl = payout - trade["cost"]
        trade["status"] = "closed"
        trade["outcome"] = outcome
        trade["payout"] = payout
        trade["pnl"] = pnl
        results["total_pnl"] += pnl

        log("=" * 50)
        log("TRADE RESULT: %s won" % outcome)
        log("  Payout: $%.2f | Cost: $%.2f | PnL: $%+.2f" %
            (payout, trade["cost"], pnl))
        log("  Running total PnL: $%+.2f" % results["total_pnl"])
        save_results(results)

    # ── Summary ──
    log("")
    log("Window %s scan complete:" % window_time)
    log("  Scans: %d (pre: %d, post: %d)" %
        (scan_count, scan_count - phase2_scans, phase2_scans))
    log("  Arb found: %s" % ("YES" if arb_found else "NO"))
    log("  Entry made: %s" % ("YES" if entry_made else "NO"))
    if best_opportunity:
        log("  Best edge: %.3f%% (guaranteed $%.4f profit)" %
            (best_opportunity["edge_pct"], best_opportunity["guaranteed_profit"]))
    log("")
    save_results(results)
    return arb_found


# ============================================================
# MAIN LOOP — NEVER STOP
# ============================================================
def main():
    log_banner("POLYMARKET BTC ARBITRAGE SCANNER")
    print("  Config:")
    print("    Trade size:    $%.0f per side ($%.0f total)" % (TRADE_SIZE, TRADE_SIZE * 2))
    print("    Arb threshold: UP+DOWN < $%.4f" % ARB_THRESHOLD)
    print("    Pre-window:    T-%ds (every %.1fs)" % (PRE_WINDOW_SECS, SCAN_INTERVAL_PRE))
    print("    Post-open:     %ds fast scan (every %.1fs)" % (POST_OPEN_FAST_SECS, SCAN_INTERVAL_FAST))
    print("    dry_mode:      %s (ALWAYS)" % DRY_MODE)
    print("    Results:       %s" % RESULTS_FILE)
    print("")

    results = load_results()
    log("Loaded %d previous opportunities, %d trades, PnL: $%+.2f" %
        (len(results["opportunities"]), len(results["trades"]), results["total_pnl"]))

    windows_scanned = 0
    arbs_found = 0

    while True:
        try:
            # Find next window
            now = int(time.time())
            current_block = (now // 900) * 900
            elapsed_in_current = now - current_block

            # If we're in the first 60s of a window, scan the current one
            if elapsed_in_current <= POST_OPEN_FAST_SECS:
                block_ts = current_block
            else:
                block_ts = current_block + 900

            secs_to_scan_start = block_ts - PRE_WINDOW_SECS - now

            if secs_to_scan_start > 5:
                next_time = datetime.fromtimestamp(block_ts).strftime("%H:%M:%S")
                scan_start = datetime.fromtimestamp(block_ts - PRE_WINDOW_SECS).strftime("%H:%M:%S")
                log("Next window: %s | Scan starts: %s (in %ds)" %
                    (next_time, scan_start, secs_to_scan_start))
                log("Sleeping %ds..." % secs_to_scan_start)
                time.sleep(secs_to_scan_start)

            found = scan_window(block_ts, results)
            windows_scanned += 1
            if found:
                arbs_found += 1

            # Show running stats
            log_banner("RUNNING STATS")
            print("  Windows scanned:  %d" % windows_scanned)
            print("  Arbs found:       %d" % arbs_found)
            print("  Total entries:    %d" % results["total_entries"])
            print("  Total PnL:        $%+.2f" % results["total_pnl"])
            print("  Opportunities:    %d" % results["total_arb_found"])
            if windows_scanned > 0:
                print("  Arb rate:         %.1f%%" % (arbs_found / windows_scanned * 100))
            print("")

            # Wait a few seconds before next cycle
            time.sleep(3)

        except KeyboardInterrupt:
            log("Shutting down...")
            save_results(results)
            break
        except Exception as e:
            log("ERROR: %s" % str(e))
            log("Retrying in 10s...")
            time.sleep(10)


if __name__ == "__main__":
    main()
