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
SCAN_INTERVAL_FULL = 5.0     # seconds between checks (full window, after 30s)
PRE_WINDOW_SECS = 60         # start scanning 60s before window
POST_OPEN_FAST_SECS = 30     # fast-scan for 30s after open
WINDOW_DURATION = 900        # 15 minutes
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
def record_arb(results, up, down, block_ts, window_time, phase, timing_val, entry_made):
    """Record an arb opportunity and optionally enter a trade. Returns (arb_dict, entry_made, trade_or_None)."""
    arb = calc_arb(up, down)
    total = up + down
    results["total_arb_found"] += 1

    timing_key = "secs_to_open" if phase == "pre-window" else "secs_after_open"
    log("*** %s ARB FOUND! UP=$%.4f DOWN=$%.4f total=$%.4f edge=%.2f%% ***" %
        (phase.upper().replace("-", "_"), up, down, total, arb["edge_pct"]))
    log("    Guaranteed profit: $%.4f | Best case: $%.4f" %
        (arb["guaranteed_profit"], arb["best_profit"]))

    opp = {
        "time": datetime.now().isoformat(),
        "window": window_time,
        "block_ts": block_ts,
        "phase": phase,
        timing_key: timing_val,
        "up_price": up,
        "down_price": down,
        "total": total,
        "edge_pct": arb["edge_pct"],
        "guaranteed_profit": arb["guaranteed_profit"],
        "entered": False,
    }

    trade = None
    if not entry_made:
        log(">>> ENTERING ARB TRADE (dry_mode=%s) <<<" % DRY_MODE)
        log("    Buy %.2f UP tokens @ $%.4f ($%.2f)" % (arb["tokens_up"], up, TRADE_SIZE))
        log("    Buy %.2f DOWN tokens @ $%.4f ($%.2f)" % (arb["tokens_down"], down, TRADE_SIZE))
        log("    Total cost: $%.2f | Guaranteed return: $%.4f" %
            (arb["total_cost"], arb["total_cost"] + arb["guaranteed_profit"]))
        opp["entered"] = True
        entry_made = True

        trade = {
            "time": datetime.now().isoformat(),
            "window": window_time,
            "block_ts": block_ts,
            "phase": phase,
            timing_key: timing_val,
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
    return arb, entry_made


def scan_window(block_ts, results):
    """
    Scan a single 15-min window for arbitrage across 3 phases:
      Phase 1: Pre-window  (T-60s to T-0s)  — every 1.0s
      Phase 2: Fast scan   (T+0s to T+30s)  — every 0.5s
      Phase 3: Full window (T+30s to T+900s) — every 5.0s
    Always scans the ENTIRE window. Never stops early.
    """
    window_time = datetime.fromtimestamp(block_ts).strftime("%H:%M:%S")
    window_end = block_ts + WINDOW_DURATION
    log_banner("SCANNING WINDOW: %s (block=%d)" % (window_time, block_ts))

    # ── Find market and tokens ──
    now = int(time.time())
    secs_to_open = block_ts - now
    if secs_to_open > PRE_WINDOW_SECS + 10:
        wait_secs = secs_to_open - PRE_WINDOW_SECS - 5
        log("Window in %ds, waiting %ds until T-%ds..." % (secs_to_open, wait_secs, PRE_WINDOW_SECS + 5))
        time.sleep(wait_secs)

    log("Fetching market for block %d..." % block_ts)
    token_ids = None
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
        return False

    scans = {"pre": 0, "fast": 0, "full": 0}
    arb_found = False
    entry_made = False
    best_opportunity = None
    lowest_total = 2.0  # track closest-to-arb price

    # ── PHASE 1: Pre-window (T-60s → T-0s) — every 1.0s ──
    now = int(time.time())
    secs_to_open = block_ts - now
    if secs_to_open > 0:
        log("")
        log("PHASE 1: Pre-window scan (%ds to open, every %.1fs)" % (secs_to_open, SCAN_INTERVAL_PRE))
        log("-" * 60)
        while True:
            now = int(time.time())
            secs_left = block_ts - now
            if secs_left <= 0:
                break
            up, down = get_both_midpoints(token_ids)
            scans["pre"] += 1
            results["total_scans"] += 1
            if up is not None and down is not None:
                total = up + down
                lowest_total = min(lowest_total, total)
                arb = calc_arb(up, down)
                if arb["is_arb"]:
                    arb_obj, entry_made = record_arb(
                        results, up, down, block_ts, window_time,
                        "pre-window", secs_left, entry_made)
                    arb_found = True
                    if best_opportunity is None or arb_obj["edge_pct"] > best_opportunity["edge_pct"]:
                        best_opportunity = arb_obj
                else:
                    if scans["pre"] % 5 == 0 or scans["pre"] == 1:
                        log("  T-%3ds | UP=$%.4f DOWN=$%.4f | sum=$%.4f | gap=%+.3f%%" %
                            (secs_left, up, down, total, (total - 1.0) * 100))
            else:
                if scans["pre"] % 10 == 0:
                    log("  T-%3ds | prices unavailable" % secs_left)
            time.sleep(SCAN_INTERVAL_PRE)

    # ── PHASE 2: Fast scan (T+0s → T+30s) — every 0.5s ──
    log("")
    log("PHASE 2: Post-open FAST scan (every %.1fs for %ds)" % (SCAN_INTERVAL_FAST, POST_OPEN_FAST_SECS))
    log("-" * 60)
    phase2_start = time.time()
    while True:
        elapsed = time.time() - phase2_start
        if elapsed >= POST_OPEN_FAST_SECS:
            break
        up, down = get_both_midpoints(token_ids)
        scans["fast"] += 1
        results["total_scans"] += 1
        if up is not None and down is not None:
            total = up + down
            lowest_total = min(lowest_total, total)
            arb = calc_arb(up, down)
            if arb["is_arb"]:
                arb_obj, entry_made = record_arb(
                    results, up, down, block_ts, window_time,
                    "post-open-fast", round(elapsed, 1), entry_made)
                arb_found = True
                if best_opportunity is None or arb_obj["edge_pct"] > best_opportunity["edge_pct"]:
                    best_opportunity = arb_obj
            else:
                if scans["fast"] % 4 == 0 or scans["fast"] == 1:
                    log("  T+%5.1fs | UP=$%.4f DOWN=$%.4f | sum=$%.4f | gap=%+.3f%%" %
                        (elapsed, up, down, total, (total - 1.0) * 100))
        time.sleep(SCAN_INTERVAL_FAST)

    # ── PHASE 3: Full window scan (T+30s → T+900s) — every 5s ──
    log("")
    now_ts = int(time.time())
    remaining = window_end - now_ts
    log("PHASE 3: Full window scan (every %.0fs for ~%ds until expiry)" % (SCAN_INTERVAL_FULL, remaining))
    log("-" * 60)

    while True:
        now_ts = int(time.time())
        remaining = window_end - now_ts
        if remaining <= 0:
            break

        elapsed_in_window = now_ts - block_ts
        up, down = get_both_midpoints(token_ids)
        scans["full"] += 1
        results["total_scans"] += 1

        if up is not None and down is not None:
            total = up + down
            lowest_total = min(lowest_total, total)
            arb = calc_arb(up, down)

            if arb["is_arb"]:
                arb_obj, entry_made = record_arb(
                    results, up, down, block_ts, window_time,
                    "mid-window", elapsed_in_window, entry_made)
                arb_found = True
                if best_opportunity is None or arb_obj["edge_pct"] > best_opportunity["edge_pct"]:
                    best_opportunity = arb_obj
            else:
                # Print every scan (every 5s = ~170 lines per window, manageable)
                min_elapsed = elapsed_in_window // 60
                sec_elapsed = elapsed_in_window % 60
                log("  %2dm%02ds | UP=$%.4f DOWN=$%.4f | sum=$%.4f | gap=%+.3f%% | low=$%.4f" %
                    (min_elapsed, sec_elapsed, up, down, total,
                     (total - 1.0) * 100, lowest_total))
        else:
            if scans["full"] % 6 == 0:
                log("  %dm%02ds | prices unavailable" % (elapsed_in_window // 60, elapsed_in_window % 60))

        time.sleep(min(SCAN_INTERVAL_FULL, remaining))

    # ── RESOLVE TRADE if we entered ──
    if entry_made:
        log("")
        log("Window expired — resolving trade...")
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
            payout = trade["cost"]

        pnl = payout - trade["cost"]
        trade["status"] = "closed"
        trade["outcome"] = outcome
        trade["payout"] = round(payout, 4)
        trade["pnl"] = round(pnl, 4)
        results["total_pnl"] += pnl

        log("=" * 50)
        log("TRADE RESULT: %s won" % outcome)
        log("  Payout: $%.2f | Cost: $%.2f | PnL: $%+.2f" % (payout, trade["cost"], pnl))
        log("  Running total PnL: $%+.2f" % results["total_pnl"])
        save_results(results)

    # ── Window Summary ──
    total_scans = scans["pre"] + scans["fast"] + scans["full"]
    log("")
    log("Window %s COMPLETE:" % window_time)
    log("  Total scans: %d (pre=%d, fast=%d, full=%d)" %
        (total_scans, scans["pre"], scans["fast"], scans["full"]))
    log("  Lowest UP+DOWN seen: $%.4f" % lowest_total)
    log("  Arb found: %s | Entry made: %s" %
        ("YES" if arb_found else "NO", "YES" if entry_made else "NO"))
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
    print("    Phase 1:       Pre-window T-%ds (every %.1fs)" % (PRE_WINDOW_SECS, SCAN_INTERVAL_PRE))
    print("    Phase 2:       Post-open %ds fast (every %.1fs)" % (POST_OPEN_FAST_SECS, SCAN_INTERVAL_FAST))
    print("    Phase 3:       Full window until expiry (every %.1fs)" % SCAN_INTERVAL_FULL)
    print("    Scans/window:  ~%d (60x1s + 60x0.5s + 174x5s)" %
          (PRE_WINDOW_SECS + POST_OPEN_FAST_SECS * 2 + (WINDOW_DURATION - POST_OPEN_FAST_SECS) // 5))
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
            # Find next window — jump into current if still scannable
            now = int(time.time())
            current_block = (now // 900) * 900
            elapsed_in_current = now - current_block
            window_end_current = current_block + WINDOW_DURATION

            # If current window hasn't expired, scan it (we scan entire window)
            if now < window_end_current - 10:
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
