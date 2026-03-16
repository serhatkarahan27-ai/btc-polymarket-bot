"""
Multi-Config Parallel Test V2 — Improved
==========================================
5 IMPROVED configs based on analysis of 26 experiments.

KEY FINDINGS APPLIED:
1. No SL — SL kills winning trades (exp_025, exp_026 confirmed repeatedly)
2. No TP — TP caps upside in binary markets ($0→$1 at expiry)
3. Momentum is best direction mode (+$8.52/5w in exp_026)
4. Entry price filter — skip windows where token >$0.65 (bad risk/reward)
5. Whipsaw fix — no SL eliminates whipsaw entirely
6. Late entry filter — skip if window >2 min old (timing issue from exp_026)

5 CONFIGS:
C1: momentum (lb=4) + no SL/TP  — proven best combo
C2: momentum (lb=2) + no SL/TP  — faster reaction to trend changes
C3: momentum (lb=6) + no SL/TP  — smoother, less whipsaw
C4: streak_follow + no SL/TP    — if last 2 same direction, follow
C5: adaptive_momentum + no SL/TP — lb=2 if volatile, lb=6 if calm
"""

import time
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
HISTORY_FILE = "polymarket_history.json"
RESULT_FILE = "multi_config_results_v2.json"

TRADE_SIZE = 5.0
MAX_ENTRY_PRICE = 0.65   # skip if token costs more than this
MAX_AGE_SECS = 120       # skip windows older than 2 min

# ============================================================
# 5 IMPROVED CONFIGS
# ============================================================
CONFIGS = [
    {
        "name": "C1: momentum_lb4 + noSL",
        "direction_mode": "momentum",
        "momentum_lookback": 4,
        "use_stop_loss": False,
        "use_take_profit": False,
    },
    {
        "name": "C2: momentum_lb2 + noSL",
        "direction_mode": "momentum",
        "momentum_lookback": 2,
        "use_stop_loss": False,
        "use_take_profit": False,
    },
    {
        "name": "C3: momentum_lb6 + noSL",
        "direction_mode": "momentum",
        "momentum_lookback": 6,
        "use_stop_loss": False,
        "use_take_profit": False,
    },
    {
        "name": "C4: streak_follow + noSL",
        "direction_mode": "streak_follow",
        "momentum_lookback": 2,
        "use_stop_loss": False,
        "use_take_profit": False,
    },
    {
        "name": "C5: adaptive_mom + noSL",
        "direction_mode": "adaptive_momentum",
        "momentum_lookback": 4,
        "use_stop_loss": False,
        "use_take_profit": False,
    },
]


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
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
            "volume": float(m.get("volume", 0)),
        }
    except:
        return None


# Simple 3-second cache for CLOB midpoint prices
_midpoint_cache = {}  # {token_id: (timestamp, price)}
CACHE_TTL = 3  # seconds


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


def find_next_window():
    now_ts = int(time.time())
    for offset in range(0, 4):
        block = ((now_ts // 900) + offset) * 900
        slug = "btc-updown-15m-%d" % block
        m = fetch_market_by_slug(slug)
        if m and not m["closed"]:
            age_secs = now_ts - block
            if age_secs > MAX_AGE_SECS:
                continue
            return {
                "slug": slug,
                "question": m["question"],
                "token_ids": m["token_ids"],
                "prices": m["prices"],
                "block_ts": block,
                "block_end": block + 900,
                "secs_to_start": block - now_ts,
                "age_secs": max(0, age_secs),
            }
    return None


def get_direction(cfg, history_windows):
    """Determine trade direction based on config mode."""
    mode = cfg["direction_mode"]
    lb = cfg.get("momentum_lookback", 4)

    closed = [w for w in history_windows
              if w.get("closed") and w.get("outcome") in ("UP", "DOWN")]

    if mode == "momentum":
        if len(closed) >= lb:
            recent = closed[-lb:]
            ups = sum(1 for r in recent if r["outcome"] == "UP")
            return "UP" if ups > lb / 2 else "DOWN"
        return "UP"  # default when not enough data

    elif mode == "streak_follow":
        # If last 2 windows are same direction, follow. Otherwise UP default.
        if len(closed) >= 2:
            last_two = [closed[-1]["outcome"], closed[-2]["outcome"]]
            if last_two[0] == last_two[1]:
                return last_two[0]  # follow the streak
        return "UP"

    elif mode == "adaptive_momentum":
        # Check volatility: if recent windows alternate a lot, use short lookback
        # If they're streaky, use long lookback
        if len(closed) >= 6:
            recent6 = closed[-6:]
            changes = sum(1 for i in range(1, 6)
                          if recent6[i]["outcome"] != recent6[i-1]["outcome"])
            # changes: 0-1 = very streaky (use lb=6), 4-5 = very choppy (use lb=2)
            if changes >= 3:
                adaptive_lb = 2  # choppy: react fast
            else:
                adaptive_lb = 6  # streaky: smooth
            recent = closed[-adaptive_lb:]
            ups = sum(1 for r in recent if r["outcome"] == "UP")
            return "UP" if ups > adaptive_lb / 2 else "DOWN"
        # Fallback to lb=4
        if len(closed) >= 4:
            recent = closed[-4:]
            ups = sum(1 for r in recent if r["outcome"] == "UP")
            return "UP" if ups > 2 else "DOWN"
        return "UP"

    return "UP"


def run_one_window(window_num, total_windows, history_windows, all_results):
    """Run one window with all configs simultaneously."""
    print("\n" + "=" * 70, flush=True)
    log("=== WINDOW %d/%d ===" % (window_num, total_windows))
    print("=" * 70, flush=True)

    # Wait for next 15-min block
    now_ts = int(time.time())
    current_block = (now_ts // 900) * 900
    next_block = current_block + 900
    wait_secs = next_block - now_ts

    # Turkey timezone display
    tr_tz = timezone(timedelta(hours=3))
    next_time_tr = datetime.fromtimestamp(next_block, tz=tr_tz)
    now_tr = datetime.now(tr_tz)
    log("Şu an: %s TR" % now_tr.strftime("%H:%M:%S"))
    log("Sonraki window: %s TR (in %dm %ds)" % (
        next_time_tr.strftime("%H:%M:%S"), wait_secs // 60, wait_secs % 60))

    target_wait = max(0, wait_secs - 3)
    if target_wait > 0:
        log("Waiting %ds..." % target_wait)
        waited = 0
        while waited < target_wait:
            chunk = min(60, target_wait - waited)
            time.sleep(chunk)
            waited += chunk
            rem = target_wait - waited
            if rem > 0:
                log("  %dm %ds remaining" % (rem // 60, rem % 60))

    log("WINDOW OPEN! Waiting 10s for market to appear...")
    time.sleep(10)

    market = find_next_window()
    if not market:
        log("No fresh market. Retrying in 30s...")
        time.sleep(30)
        market = find_next_window()
    if not market:
        log("ERROR: No market found! Skipping window.")
        for cfg in CONFIGS:
            if cfg["name"] not in all_results:
                all_results[cfg["name"]] = []
            all_results[cfg["name"]].append(0.0)
        return "UNKNOWN"

    log("Market: %s" % market["question"])

    # Get token IDs
    if not market.get("token_ids") or len(market["token_ids"]) < 2:
        log("ERROR: No token IDs")
        return "UNKNOWN"

    up_token = market["token_ids"][0]
    down_token = market["token_ids"][1]

    # Get initial prices for both sides
    up_price = get_clob_midpoint(up_token)
    down_price = get_clob_midpoint(down_token)
    log("CLOB: UP=$%.3f DOWN=$%.3f" % (up_price or 0, down_price or 0))

    # Setup positions for each config
    positions = []
    for cfg in CONFIGS:
        direction = get_direction(cfg, history_windows)
        if direction == "UP":
            entry_price = up_price
            token_id = up_token
        else:
            entry_price = down_price
            token_id = down_token

        # Entry filter: skip if price too high (bad risk/reward)
        if entry_price is None or entry_price > MAX_ENTRY_PRICE:
            positions.append({
                "config": cfg,
                "direction": direction,
                "skipped": True,
                "reason": "price>$%.2f" % MAX_ENTRY_PRICE if entry_price else "no_price",
                "entry_price": entry_price or 0,
            })
            log("SKIP %s: %s token=$%.3f > max $%.2f" % (
                cfg["name"], direction, entry_price or 0, MAX_ENTRY_PRICE))
            continue

        if entry_price < 0.15:
            positions.append({
                "config": cfg,
                "direction": direction,
                "skipped": True,
                "reason": "price_too_low",
                "entry_price": entry_price,
            })
            continue

        tokens = TRADE_SIZE / entry_price

        positions.append({
            "config": cfg,
            "direction": direction,
            "token_id": token_id,
            "entry_price": entry_price,
            "tokens": tokens,
            "exited": False,
            "exit_price": None,
            "exit_reason": None,
            "skipped": False,
            "min_price": entry_price,
            "max_price": entry_price,
        })

    # Print entries
    print("\n  %-35s | %4s | %6s | Strategy Notes" % ("Config", "Dir", "Entry"), flush=True)
    print("  " + "-" * 75, flush=True)
    for p in positions:
        if p["skipped"]:
            print("  %-35s | %4s | SKIP   | %s" % (
                p["config"]["name"], p["direction"], p.get("reason", "")), flush=True)
        else:
            print("  %-35s | %4s | $%.3f | Hold to expiry (no SL/TP)" % (
                p["config"]["name"], p["direction"], p["entry_price"]), flush=True)

    active_positions = [p for p in positions if not p["skipped"]]
    if not active_positions:
        log("All configs skipped! No positions.")
        for cfg in CONFIGS:
            if cfg["name"] not in all_results:
                all_results[cfg["name"]] = []
            all_results[cfg["name"]].append(0.0)
        return "UNKNOWN"

    # Monitor — since no SL/TP, we just track prices and wait for expiry
    window_secs = 15 * 60
    check_interval = 10  # less frequent since no SL/TP to trigger
    total_checks = window_secs // check_interval

    print("\n  %5s |" % "Time", end="", flush=True)
    for p in active_positions:
        short = p["config"]["name"][:8]
        print(" %8s |" % short, end="", flush=True)
    print("", flush=True)
    print("  " + "-" * (8 + len(active_positions) * 11), flush=True)

    for tick in range(1, total_checks + 1):
        time.sleep(check_interval)
        elapsed = tick * check_interval
        elapsed_min = elapsed / 60.0

        # Get current prices
        cur_up = get_clob_midpoint(up_token)
        cur_down = get_clob_midpoint(down_token)

        # Track min/max
        for p in active_positions:
            if p["direction"] == "UP":
                cur_price = cur_up
            else:
                cur_price = cur_down
            if cur_price:
                p["min_price"] = min(p["min_price"], cur_price)
                p["max_price"] = max(p["max_price"], cur_price)

        # Print every 60 seconds
        if elapsed % 60 == 0:
            print("  %4.1fm |" % elapsed_min, end="", flush=True)
            for p in active_positions:
                cp = cur_up if p["direction"] == "UP" else cur_down
                if cp:
                    pnl = (cp - p["entry_price"]) * p["tokens"]
                    print(" $%+.2f  |" % pnl, end="", flush=True)
                else:
                    print(" %8s |" % "N/A", end="", flush=True)
            print("", flush=True)

    # Expiry resolution — wait longer for market to close
    log("Window ended. Waiting for resolution...")
    time.sleep(20)

    # Try multiple times to get resolution (up to ~90 seconds)
    actual = "UNKNOWN"
    for attempt in range(8):
        m = fetch_market_by_slug(market["slug"])
        if m and m["closed"] and len(m["prices"]) >= 2:
            if m["prices"][0] > 0.5:
                actual = "UP"
            else:
                actual = "DOWN"
            log("Resolved via Gamma API (attempt %d)" % (attempt + 1))
            break

        # Fallback: check CLOB prices for strong signal
        final_up = get_clob_midpoint(up_token)
        final_down = get_clob_midpoint(down_token)
        if final_up and final_down:
            if final_up > 0.85:
                actual = "UP"
                log("Resolved via CLOB midpoint: UP=$%.3f (attempt %d)" % (final_up, attempt + 1))
                break
            elif final_down > 0.85:
                actual = "DOWN"
                log("Resolved via CLOB midpoint: DOWN=$%.3f (attempt %d)" % (final_down, attempt + 1))
                break

        if attempt < 7:
            log("  Not resolved yet, retry in 10s (attempt %d/8)..." % (attempt + 1))
            time.sleep(10)

    log("Resolved: %s" % actual)

    # Calculate PnL for each position
    print("\n  " + "=" * 70, flush=True)
    print("  WINDOW %d RESULTS (Actual: %s)" % (window_num, actual), flush=True)
    print("  " + "=" * 70, flush=True)
    print("  %-35s | %4s | %6s | %8s | %8s | %s" % (
        "Config", "Dir", "Corr?", "Exit$", "PnL", "Price Range"), flush=True)
    print("  " + "-" * 90, flush=True)

    for p in positions:
        name = p["config"]["name"]
        if name not in all_results:
            all_results[name] = []

        if p["skipped"]:
            pnl = 0.0
            all_results[name].append(pnl)
            print("  %-35s | %4s | SKIP  | %8s | $%+.2f  | —" % (
                name, p["direction"], "—", pnl), flush=True)
            continue

        # All positions held to expiry (no SL/TP)
        if actual == "UNKNOWN":
            p["exit_price"] = p["entry_price"]
            p["exit_reason"] = "unknown"
        else:
            correct = p["direction"] == actual
            p["exit_price"] = 1.00 if correct else 0.00
            p["exit_reason"] = "expiry"

        exit_value = p["tokens"] * p["exit_price"]
        pnl = exit_value - TRADE_SIZE
        pnl = round(pnl, 4)
        all_results[name].append(pnl)

        correct = p["direction"] == actual
        icon = "WIN" if correct else "LOSS"
        price_range = "$%.2f-$%.2f" % (p["min_price"], p["max_price"])

        print("  %-35s | %4s | %4s  | $%.3f   | $%+.2f  | %s" % (
            name, p["direction"], icon,
            p["exit_price"], pnl, price_range), flush=True)

    print("  " + "=" * 70, flush=True)
    return actual


def print_leaderboard(all_results, total_windows):
    """Print cumulative leaderboard."""
    print("\n" + "#" * 70, flush=True)
    print("  LEADERBOARD AFTER %d WINDOWS" % total_windows, flush=True)
    print("#" * 70, flush=True)

    scores = []
    for name, pnls in all_results.items():
        total = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        draws = sum(1 for p in pnls if p == 0)
        wr = wins / max(1, wins + losses) * 100
        scores.append((total, name, pnls, wins, losses, draws, wr))

    scores.sort(reverse=True)

    print("\n  %3s | %-35s | %3s | %3s | %5s | %8s | PnLs" % (
        "Rk", "Config", "W", "L", "WR%", "Total"), flush=True)
    print("  " + "-" * 95, flush=True)

    medals = ["", "1st", "2nd", "3rd", "4th", "5th"]
    for rank, (total, name, pnls, wins, losses, draws, wr) in enumerate(scores, 1):
        medal = medals[rank] if rank < len(medals) else str(rank)
        pnl_str = " ".join(["$%+.2f" % p for p in pnls])
        print("  %3s | %-35s | %dW  | %dL  | %4.0f%% | $%+.2f  | %s" % (
            medal, name, wins, losses, wr, total, pnl_str), flush=True)

    print("  " + "=" * 95, flush=True)
    winner = scores[0]
    print("\n  LEADER: %s with $%+.2f total PnL (%d/%d wins)" % (
        winner[1], winner[0], winner[3], winner[3] + winner[4]), flush=True)

    # Key insight
    if total_windows >= 3:
        avg_per_window = winner[0] / total_windows
        projected_20w = avg_per_window * 20
        print("  Avg/window: $%+.2f | Projected 20w: $%+.2f (target: $20)" % (
            avg_per_window, projected_20w), flush=True)
    print("", flush=True)


def main():
    total_windows = 5

    print("\n" + "#" * 70, flush=True)
    print("  MULTI-CONFIG TEST V2 — IMPROVED", flush=True)
    print("  Based on analysis of 26 experiments", flush=True)
    print("  KEY CHANGES: No SL, No TP, Hold to expiry", flush=True)
    print("  5 configs x %d windows" % total_windows, flush=True)
    tr_tz = timezone(timedelta(hours=3))
    print("  %s TR" % datetime.now(tr_tz).strftime("%Y-%m-%d %H:%M:%S"), flush=True)
    print("#" * 70, flush=True)

    print("\n  CONFIGS:", flush=True)
    for cfg in CONFIGS:
        print("    %s" % cfg["name"], flush=True)
        print("      direction=%s, lb=%d, SL=OFF, TP=OFF" % (
            cfg["direction_mode"], cfg.get("momentum_lookback", 4)), flush=True)

    # Load history for momentum calc
    try:
        with open(HISTORY_FILE) as f:
            history_windows = json.load(f)
        log("Loaded %d history windows" % len(history_windows))
        closed = [w for w in history_windows
                  if w.get("closed") and w.get("outcome") in ("UP", "DOWN")]
        if closed:
            recent = closed[-6:]
            recent_str = " ".join([w["outcome"][0] for w in recent])
            log("Recent outcomes: %s" % recent_str)
    except:
        history_windows = []
        log("No history file found, starting fresh")

    all_results = {}

    # Run windows
    for w in range(1, total_windows + 1):
        actual = run_one_window(w, total_windows, history_windows, all_results)

        # Update history with result for momentum calc
        if actual in ("UP", "DOWN"):
            now_ts = int(time.time())
            block_ts = (now_ts // 900) * 900
            history_windows.append({
                "block_ts": block_ts,
                "slug": "btc-updown-15m-%d" % block_ts,
                "closed": True,
                "outcome": actual,
            })

        print_leaderboard(all_results, w)
        time.sleep(3)

    # Save final results
    with open(RESULT_FILE, "w") as f:
        json.dump(all_results, f, indent=2)
    log("Results saved to %s" % RESULT_FILE)

    # Final summary
    print("\n" + "#" * 70, flush=True)
    print("  FINAL RESULTS -- %d WINDOWS COMPLETE" % total_windows, flush=True)
    print("#" * 70, flush=True)
    print_leaderboard(all_results, total_windows)

    # Recommendations
    scores = []
    for name, pnls in all_results.items():
        total = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        scores.append((total, name, wins, losses))
    scores.sort(reverse=True)
    winner = scores[0]
    print("  RECOMMENDATION:", flush=True)
    print("  Best config: %s ($%+.2f)" % (winner[1], winner[0]), flush=True)
    print("  Next step: Run 20-window validation with this config", flush=True)
    print("", flush=True)


if __name__ == "__main__":
    main()
