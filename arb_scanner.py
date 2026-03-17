"""
Polymarket BTC 15-min Arbitrage Scanner v2
==========================================
TRUE ARBITRAGE: Buy BOTH UP and DOWN when BOTH are below $0.50.
One side ALWAYS resolves to $1.00 → guaranteed profit.

Core Rule:
  up_price < $0.50 AND down_price < $0.50
  → Buy $10 UP + $10 DOWN = $20 cost
  → Winner pays 10/price * $1.00 > $20 GUARANTEED
  → Profit = min(10/up, 10/down) - 20

Example: UP=$0.49, DOWN=$0.49, total=$0.98
  UP tokens = 10/0.49 = 20.41,  DOWN tokens = 10/0.49 = 20.41
  If UP wins:  20.41 * $1 = $20.41 → profit $0.41
  If DOWN wins: 20.41 * $1 = $20.41 → profit $0.41
  Guaranteed profit = $0.41

Scan Speed:
  Phase 1: Pre-window (T-60s → T-0s)   every 0.5s    ~120 scans
  Phase 2: Post-open  (T+0s → T+30s)   every 0.2s    ~150 scans
  Phase 3: Full window (T+30s → expiry) every 1.0s    ~870 scans
  Total per window: ~1140 scans
  Parallel HTTP requests for UP and DOWN midpoints.

dry_mode = True ALWAYS.
"""
import time
import json
import requests
import os
import functools
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# Force unbuffered output
print = functools.partial(print, flush=True)

# ============================================================
# CONFIG
# ============================================================
TRADE_SIZE = 10.0             # $ per side ($20 total)
SCAN_INTERVAL_PRE = 0.5       # Phase 1: pre-window (every 0.5s)
SCAN_INTERVAL_FAST = 0.2      # Phase 2: post-open  (every 0.2s — FASTEST)
SCAN_INTERVAL_FULL = 1.0      # Phase 3: full window (every 1.0s)
PRE_WINDOW_SECS = 60          # start scanning 60s before window
POST_OPEN_FAST_SECS = 30      # fast-scan for 30s after open
WINDOW_DURATION = 900          # 15 minutes
DRY_MODE = True                # ALWAYS True

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
RESULTS_FILE = "arb_results.json"

# Reusable session for faster HTTP (connection pooling)
SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json"})

# Thread pool for parallel price fetches
POOL = ThreadPoolExecutor(max_workers=2)


# ============================================================
# LOGGING
# ============================================================
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print("  [%s] %s" % (ts, msg))


def log_banner(msg):
    print("\n" + "=" * 70)
    print("  %s" % msg)
    print("=" * 70)


# ============================================================
# API FUNCTIONS (FAST)
# ============================================================
def fetch_midpoint(token_id):
    """Fetch CLOB midpoint for one token. Used inside thread pool."""
    try:
        r = SESSION.get("%s/midpoint" % CLOB_BASE,
                        params={"token_id": token_id}, timeout=2)
        if r.status_code == 200:
            mid = float(r.json().get("mid", 0))
            if mid > 0:
                return mid
    except Exception:
        pass
    return None


def get_prices_parallel(token_ids):
    """Fetch UP and DOWN midpoints in PARALLEL. Returns (up, down) or (None, None)."""
    if not token_ids or len(token_ids) < 2:
        return None, None
    try:
        fut_up = POOL.submit(fetch_midpoint, token_ids[0])
        fut_down = POOL.submit(fetch_midpoint, token_ids[1])
        up = fut_up.result(timeout=3)
        down = fut_down.result(timeout=3)
        return up, down
    except Exception:
        return None, None


def get_market_by_slug(slug):
    try:
        r = SESSION.get("%s/markets/slug/%s" % (GAMMA_BASE, slug), timeout=5)
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


def find_market_for_block(block_ts):
    slug = "btc-updown-15m-%d" % block_ts
    m = get_market_by_slug(slug)
    if m and not m["closed"]:
        m["block_ts"] = block_ts
        return m
    return None


# ============================================================
# ARBITRAGE LOGIC
# ============================================================
def is_true_arb(up, down):
    """TRUE arb only when BOTH sides < $0.50."""
    return up < 0.50 and down < 0.50


def calc_arb(up_price, down_price):
    """Calculate arb details. Entry only valid if both < $0.50."""
    total = up_price + down_price
    cost = TRADE_SIZE * 2  # $20

    tokens_up = TRADE_SIZE / up_price       # 10/0.49 = 20.41
    tokens_down = TRADE_SIZE / down_price   # 10/0.49 = 20.41

    # If UP wins:  profit = tokens_up - cost
    # If DOWN wins: profit = tokens_down - cost
    profit_if_up = tokens_up - cost
    profit_if_down = tokens_down - cost

    guaranteed = min(profit_if_up, profit_if_down)
    best = max(profit_if_up, profit_if_down)
    both_below_50 = up_price < 0.50 and down_price < 0.50

    return {
        "up_price": up_price,
        "down_price": down_price,
        "total": total,
        "cost": cost,
        "tokens_up": tokens_up,
        "tokens_down": tokens_down,
        "profit_if_up": profit_if_up,
        "profit_if_down": profit_if_down,
        "guaranteed_profit": guaranteed,
        "best_profit": best,
        "is_arb": both_below_50,
        "edge_pct": (1.0 - total) * 100 if total < 1.0 else 0,
    }


# ============================================================
# RESULTS STORAGE
# ============================================================
def load_results():
    defaults = {
        "total_windows": 0,
        "total_scans": 0,
        "total_arb_found": 0,
        "total_entries": 0,
        "total_pnl": 0.0,
        "opportunities": [],
        "trades": [],
    }
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE, "r") as f:
                data = json.load(f)
            # Ensure all keys exist (fixes KeyError for old files)
            for k, v in defaults.items():
                if k not in data:
                    data[k] = v
            return data
        except Exception:
            pass
    return defaults


def save_results(data):
    tmp = RESULTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, RESULTS_FILE)


# ============================================================
# CHECK RESOLUTION
# ============================================================
def check_resolved(token_ids):
    """Check if window resolved: UP=$1 or DOWN=$1. Returns 'UP', 'DOWN', or None."""
    up, down = get_prices_parallel(token_ids)
    if up is not None and up >= 0.95:
        return "UP"
    if down is not None and down >= 0.95:
        return "DOWN"
    return None


# ============================================================
# SCAN A SINGLE PHASE
# ============================================================
def run_phase(phase_name, token_ids, block_ts, window_time, interval, duration_fn,
              results, state):
    """
    Generic scan phase.
    duration_fn(now) → remaining seconds in this phase. Returns 0 or negative to end.
    state = {"entry_made": bool, "arb_found": bool, "best": dict|None,
             "lowest_total": float, scans dict}
    """
    phase_scans = 0

    while True:
        now = time.time()
        remaining = duration_fn(now)
        if remaining <= 0:
            break

        up, down = get_prices_parallel(token_ids)
        phase_scans += 1
        results["total_scans"] += 1

        if up is not None and down is not None:
            total = up + down
            state["lowest_total"] = min(state["lowest_total"], total)
            arb = calc_arb(up, down)

            # Check for resolution (UP or DOWN hit $1.00)
            if up >= 0.95 or down >= 0.95:
                outcome = "UP" if up >= 0.95 else "DOWN"
                log("  RESOLVED: %s wins! (UP=$%.4f DOWN=$%.4f)" % (outcome, up, down))
                state["resolved"] = outcome
                state["scans"][phase_name] = phase_scans
                return

            if arb["is_arb"]:
                # TRUE ARB: both < $0.50
                state["arb_found"] = True
                results["total_arb_found"] += 1

                elapsed = int(time.time()) - block_ts
                log("")
                log("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                log("!!!  TRUE ARB FOUND in %s  !!!" % phase_name)
                log("!!!  UP=$%.4f  DOWN=$%.4f  total=$%.4f         !!!" % (up, down, total))
                log("!!!  Edge: %.2f%%  Guaranteed: $%.4f           !!!" %
                    (arb["edge_pct"], arb["guaranteed_profit"]))
                log("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                log("")

                opp = {
                    "time": datetime.now().isoformat(),
                    "window": window_time,
                    "block_ts": block_ts,
                    "phase": phase_name,
                    "elapsed_secs": elapsed,
                    "up_price": up,
                    "down_price": down,
                    "total": total,
                    "edge_pct": arb["edge_pct"],
                    "guaranteed_profit": arb["guaranteed_profit"],
                    "entered": False,
                }

                if not state["entry_made"]:
                    log(">>> ENTERING ARB TRADE (dry_mode=%s) <<<" % DRY_MODE)
                    log("    Buy %.2f UP tokens   @ $%.4f  ($%.2f)" %
                        (arb["tokens_up"], up, TRADE_SIZE))
                    log("    Buy %.2f DOWN tokens @ $%.4f  ($%.2f)" %
                        (arb["tokens_down"], down, TRADE_SIZE))
                    log("    Total cost: $%.2f" % arb["cost"])
                    log("    If UP wins:   $%.2f profit" % arb["profit_if_up"])
                    log("    If DOWN wins: $%.2f profit" % arb["profit_if_down"])
                    log("    GUARANTEED MIN PROFIT: $%.4f" % arb["guaranteed_profit"])

                    opp["entered"] = True
                    state["entry_made"] = True

                    trade = {
                        "time": datetime.now().isoformat(),
                        "window": window_time,
                        "block_ts": block_ts,
                        "phase": phase_name,
                        "elapsed_secs": elapsed,
                        "entry_up": up,
                        "entry_down": down,
                        "total_entry": total,
                        "tokens_up": arb["tokens_up"],
                        "tokens_down": arb["tokens_down"],
                        "cost": arb["cost"],
                        "guaranteed_profit": arb["guaranteed_profit"],
                        "profit_if_up": arb["profit_if_up"],
                        "profit_if_down": arb["profit_if_down"],
                        "status": "open",
                        "pnl": None,
                    }
                    results["trades"].append(trade)
                    results["total_entries"] += 1

                if state["best"] is None or arb["edge_pct"] > state["best"]["edge_pct"]:
                    state["best"] = arb

                results["opportunities"].append(opp)
                save_results(results)
            else:
                # Not arb — periodic price log
                elapsed = int(time.time()) - block_ts
                if elapsed < 0:
                    label = "T-%3ds" % abs(elapsed)
                else:
                    label = "%2dm%02ds" % (elapsed // 60, elapsed % 60)

                # Log frequency: every 2s in fast phase, every 5s otherwise
                log_every = max(1, int(2.0 / interval)) if interval < 0.5 else max(1, int(5.0 / interval))
                if phase_scans % log_every == 0 or phase_scans == 1:
                    arb_flag = " <<< CLOSE!" if total < 1.01 else ""
                    log("  %s | UP=$%.4f DOWN=$%.4f | sum=$%.4f | gap=%+.3f%% | low=$%.4f%s" %
                        (label, up, down, total, (total - 1.0) * 100,
                         state["lowest_total"], arb_flag))
        else:
            if phase_scans % 20 == 0:
                log("  prices unavailable (scan #%d)" % phase_scans)

        sleep_time = min(interval, max(0.05, remaining))
        time.sleep(sleep_time)

    state["scans"][phase_name] = phase_scans


# ============================================================
# SCAN A FULL WINDOW
# ============================================================
def scan_window(block_ts, results):
    """
    Scan one 15-min window for true arbitrage across 3 phases.
    Scans until window RESOLVES (UP or DOWN hits $1.00) or expires.
    """
    window_time = datetime.fromtimestamp(block_ts).strftime("%H:%M:%S")
    window_end = block_ts + WINDOW_DURATION
    log_banner("SCANNING WINDOW: %s (block=%d)" % (window_time, block_ts))

    # ── Find market ──
    now = int(time.time())
    secs_to_open = block_ts - now
    if secs_to_open > PRE_WINDOW_SECS + 10:
        wait_secs = secs_to_open - PRE_WINDOW_SECS - 5
        log("Window in %ds, waiting %ds..." % (secs_to_open, wait_secs))
        time.sleep(wait_secs)

    log("Fetching market...")
    token_ids = None
    for attempt in range(5):
        market = find_market_for_block(block_ts)
        if market and market.get("token_ids") and len(market["token_ids"]) >= 2:
            token_ids = market["token_ids"]
            log("Market: %s" % market["question"])
            log("UP:   %s..." % token_ids[0][:25])
            log("DOWN: %s..." % token_ids[1][:25])
            break
        log("Not found (attempt %d/5)..." % (attempt + 1))
        time.sleep(3)
    if not token_ids:
        log("ERROR: Market not found. Skipping.")
        return False

    state = {
        "entry_made": False,
        "arb_found": False,
        "best": None,
        "lowest_total": 2.0,
        "resolved": None,
        "scans": {"pre": 0, "fast": 0, "full": 0},
    }

    # ── PHASE 1: Pre-window (T-60s → T-0s) — every 0.5s ──
    now_ts = int(time.time())
    secs_to_open = block_ts - now_ts
    if secs_to_open > 0:
        log("")
        log("PHASE 1  Pre-window  %ds to open  every %.1fs" % (secs_to_open, SCAN_INTERVAL_PRE))
        log("-" * 65)
        run_phase("pre", token_ids, block_ts, window_time, SCAN_INTERVAL_PRE,
                  lambda now: block_ts - now, results, state)

    if state["resolved"]:
        return _finalize(state, results, window_time)

    # ── PHASE 2: Fast scan (T+0s → T+30s) — every 0.2s ──
    log("")
    fast_end = block_ts + POST_OPEN_FAST_SECS
    log("PHASE 2  Post-open FAST  every %.1fs for %ds" % (SCAN_INTERVAL_FAST, POST_OPEN_FAST_SECS))
    log("-" * 65)
    run_phase("fast", token_ids, block_ts, window_time, SCAN_INTERVAL_FAST,
              lambda now: fast_end - now, results, state)

    if state["resolved"]:
        return _finalize(state, results, window_time)

    # ── PHASE 3: Full window (T+30s → expiry) — every 1.0s ──
    log("")
    now_ts = int(time.time())
    remaining = window_end - now_ts
    log("PHASE 3  Full window scan  every %.1fs  ~%ds remaining" % (SCAN_INTERVAL_FULL, remaining))
    log("-" * 65)
    run_phase("full", token_ids, block_ts, window_time, SCAN_INTERVAL_FULL,
              lambda now: window_end - now, results, state)

    # ── Final resolution check if not already resolved ──
    if not state["resolved"]:
        log("Window expiry reached. Checking final resolution...")
        for _ in range(5):
            outcome = check_resolved(token_ids)
            if outcome:
                state["resolved"] = outcome
                break
            time.sleep(1)
        if not state["resolved"]:
            state["resolved"] = "UNKNOWN"

    return _finalize(state, results, window_time)


def _finalize(state, results, window_time):
    """Resolve trade if entered, print summary."""
    outcome = state["resolved"] or "UNKNOWN"

    # Resolve open trade
    if state["entry_made"] and results["trades"]:
        trade = results["trades"][-1]
        if trade["status"] == "open":
            if outcome == "UP":
                payout = trade["tokens_up"] * 1.0
            elif outcome == "DOWN":
                payout = trade["tokens_down"] * 1.0
            else:
                payout = trade["cost"]  # neutral

            pnl = payout - trade["cost"]
            trade["status"] = "closed"
            trade["outcome"] = outcome
            trade["payout"] = round(payout, 4)
            trade["pnl"] = round(pnl, 4)
            results["total_pnl"] += pnl

            log("")
            log("=" * 50)
            log("TRADE CLOSED: %s won" % outcome)
            log("  Payout: $%.2f | Cost: $%.2f | PnL: $%+.2f" %
                (payout, trade["cost"], pnl))
            log("  Total PnL: $%+.2f (%d trades)" %
                (results["total_pnl"], results["total_entries"]))
            log("=" * 50)

    # Window summary
    s = state["scans"]
    total_scans = s.get("pre", 0) + s.get("fast", 0) + s.get("full", 0)
    results["total_windows"] += 1

    log("")
    log("WINDOW %s DONE  |  Result: %s" % (window_time, outcome))
    log("  Scans: %d (pre=%d fast=%d full=%d)" %
        (total_scans, s.get("pre", 0), s.get("fast", 0), s.get("full", 0)))
    log("  Lowest UP+DOWN: $%.4f" % state["lowest_total"])
    log("  Arb found: %s  |  Entry: %s" %
        ("YES" if state["arb_found"] else "NO",
         "YES" if state["entry_made"] else "NO"))
    if state["best"]:
        log("  Best edge: %.3f%%  guaranteed $%.4f" %
            (state["best"]["edge_pct"], state["best"]["guaranteed_profit"]))
    log("")
    save_results(results)
    return state["arb_found"]


# ============================================================
# MAIN LOOP — RUNS FOREVER
# ============================================================
def main():
    log_banner("POLYMARKET BTC ARBITRAGE SCANNER v2")
    print("  ┌─────────────────────────────────────────────────┐")
    print("  │  TRUE ARB RULE: Enter ONLY if BOTH < $0.50     │")
    print("  │  UP < $0.50 AND DOWN < $0.50 = guaranteed win  │")
    print("  └─────────────────────────────────────────────────┘")
    print("")
    print("  Trade size:     $%.0f per side ($%.0f total)" % (TRADE_SIZE, TRADE_SIZE * 2))
    print("  Phase 1:        Pre-window T-%ds  (every %.1fs)  ~%d scans" %
          (PRE_WINDOW_SECS, SCAN_INTERVAL_PRE, PRE_WINDOW_SECS / SCAN_INTERVAL_PRE))
    print("  Phase 2:        Post-open %ds    (every %.1fs)  ~%d scans" %
          (POST_OPEN_FAST_SECS, SCAN_INTERVAL_FAST, POST_OPEN_FAST_SECS / SCAN_INTERVAL_FAST))
    print("  Phase 3:        Full window       (every %.1fs)  ~%d scans" %
          (SCAN_INTERVAL_FULL, (WINDOW_DURATION - POST_OPEN_FAST_SECS) / SCAN_INTERVAL_FULL))
    est_total = int(PRE_WINDOW_SECS / SCAN_INTERVAL_PRE +
                    POST_OPEN_FAST_SECS / SCAN_INTERVAL_FAST +
                    (WINDOW_DURATION - POST_OPEN_FAST_SECS) / SCAN_INTERVAL_FULL)
    print("  Total/window:   ~%d scans" % est_total)
    print("  Parallel HTTP:  YES (ThreadPoolExecutor)")
    print("  dry_mode:       %s (ALWAYS)" % DRY_MODE)
    print("  Results:        %s" % RESULTS_FILE)
    print("  Resolution:     Scans until UP=$1 or DOWN=$1 (early exit)")
    print("")

    results = load_results()
    log("Loaded: %d windows, %d opportunities, %d trades, PnL: $%+.2f" %
        (results["total_windows"], len(results["opportunities"]),
         len(results["trades"]), results["total_pnl"]))

    last_scanned_block = 0  # Track last scanned block to prevent re-scanning

    while True:
        try:
            # Determine which window to scan
            now = int(time.time())
            current_block = (now // 900) * 900
            window_end_current = current_block + WINDOW_DURATION

            # If current window still has >10s left AND we haven't scanned it yet
            if now < window_end_current - 10 and current_block != last_scanned_block:
                block_ts = current_block
            else:
                block_ts = current_block + 900

            # Skip if we already scanned this block (prevents infinite loop)
            if block_ts == last_scanned_block:
                block_ts = last_scanned_block + 900

            secs_to_scan_start = block_ts - PRE_WINDOW_SECS - now

            if secs_to_scan_start > 5:
                next_time = datetime.fromtimestamp(block_ts).strftime("%H:%M:%S")
                scan_start = datetime.fromtimestamp(block_ts - PRE_WINDOW_SECS).strftime("%H:%M:%S")
                log("Next window: %s | Scan starts: %s (in %ds)" %
                    (next_time, scan_start, secs_to_scan_start))
                time.sleep(secs_to_scan_start)

            scan_window(block_ts, results)
            last_scanned_block = block_ts  # Mark as scanned

            # Running stats
            tw = results.get("total_windows", 0)
            log_banner("RUNNING STATS  (%d windows)" % tw)
            print("  Windows scanned:  %d" % tw)
            print("  Total scans:      %d" % results["total_scans"])
            print("  Arb opportunities: %d" % results["total_arb_found"])
            print("  Trades entered:   %d" % results["total_entries"])
            print("  Total PnL:        $%+.2f" % results["total_pnl"])
            if tw > 0:
                arb_windows = len(set(o["block_ts"] for o in results["opportunities"]))
                print("  Arb rate:         %d/%d windows (%.1f%%)" %
                      (arb_windows, tw, arb_windows / tw * 100))
                if results["total_entries"] > 0:
                    print("  Avg PnL/trade:    $%+.2f" %
                          (results["total_pnl"] / results["total_entries"]))
            print("")

            time.sleep(2)

        except KeyboardInterrupt:
            log("Shutting down gracefully...")
            save_results(results)
            POOL.shutdown(wait=False)
            break
        except Exception as e:
            log("ERROR: %s" % str(e))
            log("Retrying in 10s...")
            time.sleep(10)


if __name__ == "__main__":
    main()
