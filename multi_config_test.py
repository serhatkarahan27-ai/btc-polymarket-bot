"""
Multi-Config Parallel Test
===========================
Run 5 different configs on the SAME window simultaneously.
Same price feed, different direction/SL/TP logic.
"""

import time
import json
import requests
from datetime import datetime
from pathlib import Path

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
HISTORY_FILE = "polymarket_history.json"
RESULT_FILE = "multi_config_results.json"

TRADE_SIZE = 5.0

# ============================================================
# 5 CONFIGS TO TEST
# ============================================================
CONFIGS = [
    {
        "name": "C1: always_up + SL=0.45",
        "direction_mode": "always_up",
        "stop_loss": 0.45,
        "use_stop_loss": True,
        "take_profit": 0.99,
        "use_take_profit": False,
    },
    {
        "name": "C2: momentum + SL=0.45",
        "direction_mode": "momentum",
        "stop_loss": 0.45,
        "use_stop_loss": True,
        "take_profit": 0.99,
        "use_take_profit": False,
    },
    {
        "name": "C3: mom+flip + SL=0.45",
        "direction_mode": "contrarian",
        "stop_loss": 0.45,
        "use_stop_loss": True,
        "take_profit": 0.99,
        "use_take_profit": False,
    },
    {
        "name": "C4: always_down + SL=0.45",
        "direction_mode": "always_down",
        "stop_loss": 0.45,
        "use_stop_loss": True,
        "take_profit": 0.99,
        "use_take_profit": False,
    },
    {
        "name": "C5: mom+flip + SL=0.45 + TP=0.80",
        "direction_mode": "contrarian",
        "stop_loss": 0.45,
        "use_stop_loss": True,
        "take_profit": 0.80,
        "use_take_profit": True,
    },
]

# Previous results from windows 1-2
PREV_RESULTS = {
    "C1: always_up + SL=0.45":          [0.00, 5.53],
    "C2: momentum + SL=0.45":           [0.00, 5.53],
    "C3: mom+flip + SL=0.45":           [4.01, -0.68],
    "C4: always_down + SL=0.45":        [4.01, -0.68],
    "C5: mom+flip + SL=0.45 + TP=0.80": [2.25, -0.68],
}


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print("  [%s] %s" % (ts, msg))


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


def get_clob_midpoint(token_id):
    try:
        r = requests.get("%s/midpoint" % CLOB_BASE,
                         params={"token_id": token_id}, timeout=5)
        if r.status_code == 200:
            return float(r.json().get("mid", 0))
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
            if age_secs > 120:
                continue
            return {
                "slug": slug,
                "question": m["question"],
                "token_ids": m["token_ids"],
                "prices": m["prices"],
                "block_ts": block,
                "block_end": block + 900,
                "secs_to_start": block - now_ts,
            }
    return None


def get_direction(cfg, history_windows):
    """Determine trade direction based on config."""
    mode = cfg["direction_mode"]
    if mode == "always_up":
        return "UP"
    elif mode == "always_down":
        return "DOWN"
    elif mode == "momentum":
        closed = [w for w in history_windows
                  if w["closed"] and w["outcome"] in ("UP", "DOWN")]
        lb = 4
        if len(closed) >= lb:
            recent = closed[-lb:]
            ups = sum(1 for r in recent if r["outcome"] == "UP")
            return "UP" if ups > lb / 2 else "DOWN"
        return "UP"
    elif mode == "contrarian":
        # momentum + flip = look at recent trend, then go opposite
        closed = [w for w in history_windows
                  if w["closed"] and w["outcome"] in ("UP", "DOWN")]
        lb = 4
        if len(closed) >= lb:
            recent = closed[-lb:]
            ups = sum(1 for r in recent if r["outcome"] == "UP")
            momentum_dir = "UP" if ups > lb / 2 else "DOWN"
            return "DOWN" if momentum_dir == "UP" else "UP"
        return "DOWN"
    return "UP"


def run_one_window(window_num, history_windows, all_results):
    """Run one window with all 5 configs simultaneously."""
    print("\n" + "=" * 70)
    log("=== WINDOW %d/5 ===" % window_num)
    print("=" * 70)

    # Wait for next 15-min block
    now_ts = int(time.time())
    current_block = (now_ts // 900) * 900
    next_block = current_block + 900
    wait_secs = next_block - now_ts

    next_time = datetime.fromtimestamp(next_block)
    log("Next window: %s (in %dm %ds)" % (
        next_time.strftime("%H:%M:%S"), wait_secs // 60, wait_secs % 60))

    target_wait = min(wait_secs - 3, 20 * 60)
    if target_wait > 0:
        log("Waiting %ds..." % target_wait)
        waited = 0
        while waited < target_wait:
            chunk = min(30, target_wait - waited)
            time.sleep(chunk)
            waited += chunk
            rem = target_wait - waited
            if rem > 0 and rem % 60 < 30:
                log("  %dm %ds remaining" % (rem // 60, rem % 60))

    log("WINDOW OPEN! Waiting 10s for market to appear...")
    time.sleep(10)

    market = find_next_window()
    if not market:
        log("No fresh market. Retrying in 30s...")
        time.sleep(30)
        market = find_next_window()
    if not market:
        log("ERROR: No market found!")
        return

    log("Market: %s" % market["question"])

    # Get token IDs
    if not market.get("token_ids") or len(market["token_ids"]) < 2:
        log("ERROR: No token IDs")
        return

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

        # Sanity checks
        if entry_price is None or entry_price < 0.20 or entry_price > 0.80:
            positions.append({
                "config": cfg,
                "direction": direction,
                "skipped": True,
                "reason": "price_out_of_range",
                "entry_price": entry_price or 0,
            })
            continue

        # SL/TP adjustment
        sl = cfg["stop_loss"]
        tp = cfg["take_profit"]
        if sl >= entry_price:
            sl = entry_price * 0.60
        if tp <= entry_price:
            tp = entry_price * 1.40

        tokens = TRADE_SIZE / entry_price

        positions.append({
            "config": cfg,
            "direction": direction,
            "token_id": token_id,
            "entry_price": entry_price,
            "sl": sl,
            "tp": tp,
            "tokens": tokens,
            "exited": False,
            "exit_price": None,
            "exit_reason": None,
            "exit_minute": 15,
            "skipped": False,
            "min_price": entry_price,
            "max_price": entry_price,
        })

    # Print entries
    print("\n  %-35s | %4s | %6s | %6s | %6s" % ("Config", "Dir", "Entry", "SL", "TP"))
    print("  " + "-" * 75)
    for p in positions:
        if p["skipped"]:
            print("  %-35s | %4s | SKIPPED (%s)" % (
                p["config"]["name"], p["direction"], p["reason"]))
        else:
            tp_str = "$%.2f" % p["tp"] if p["config"]["use_take_profit"] else "OFF"
            print("  %-35s | %4s | $%.3f | $%.2f | %s" % (
                p["config"]["name"], p["direction"], p["entry_price"],
                p["sl"], tp_str))

    # Monitor all positions simultaneously
    window_secs = 15 * 60
    check_interval = 5
    total_checks = window_secs // check_interval

    print("\n  %5s |" % "Time", end="")
    for p in positions:
        if not p["skipped"]:
            print(" %8s |" % p["config"]["name"][:8], end="")
    print("")
    print("  " + "-" * (8 + len([p for p in positions if not p["skipped"]]) * 11))

    for tick in range(1, total_checks + 1):
        time.sleep(check_interval)
        elapsed = tick * check_interval
        elapsed_min = elapsed / 60.0

        # Get current prices for both tokens
        cur_up = get_clob_midpoint(up_token)
        cur_down = get_clob_midpoint(down_token)

        # Check each position
        for p in positions:
            if p["skipped"] or p["exited"]:
                continue

            if p["direction"] == "UP":
                cur_price = cur_up
            else:
                cur_price = cur_down

            if cur_price is None:
                continue

            p["min_price"] = min(p["min_price"], cur_price)
            p["max_price"] = max(p["max_price"], cur_price)

            # SL check
            if p["config"]["use_stop_loss"] and cur_price <= p["sl"]:
                p["exited"] = True
                p["exit_price"] = cur_price
                p["exit_reason"] = "stop_loss"
                p["exit_minute"] = int(elapsed_min) + 1

            # TP check
            if p["config"]["use_take_profit"] and cur_price >= p["tp"]:
                p["exited"] = True
                p["exit_price"] = cur_price
                p["exit_reason"] = "take_profit"
                p["exit_minute"] = int(elapsed_min) + 1

        # Print every 30 seconds
        if elapsed % 30 == 0:
            print("  %4.1fm |" % elapsed_min, end="")
            for p in positions:
                if p["skipped"]:
                    continue
                if p["exited"]:
                    print(" %8s |" % "EXITED", end="")
                else:
                    if p["direction"] == "UP":
                        cp = cur_up
                    else:
                        cp = cur_down
                    if cp:
                        pnl = (cp - p["entry_price"]) * p["tokens"]
                        print(" $%+.2f  |" % pnl, end="")
                    else:
                        print(" %8s |" % "N/A", end="")
            print("")

    # Expiry resolution
    log("Window ended. Checking resolution...")
    time.sleep(10)
    m = fetch_market_by_slug(market["slug"])
    if m and m["closed"] and len(m["prices"]) >= 2:
        if m["prices"][0] > 0.5:
            actual = "UP"
        else:
            actual = "DOWN"
    else:
        # Try final CLOB prices
        final_up = get_clob_midpoint(up_token)
        final_down = get_clob_midpoint(down_token)
        if final_up and final_down:
            actual = "UP" if final_up > final_down else "DOWN"
        else:
            actual = "UNKNOWN"

    log("Resolved: %s" % actual)

    # Calculate PnL for each position
    print("\n  " + "=" * 70)
    print("  WINDOW %d RESULTS (Actual: %s)" % (window_num, actual))
    print("  " + "=" * 70)
    print("  %-35s | %4s | %6s | %8s | %8s" % (
        "Config", "Dir", "Corr?", "Exit", "PnL"))
    print("  " + "-" * 75)

    for p in positions:
        if p["skipped"]:
            pnl = 0.0
            p["pnl"] = 0.0
            print("  %-35s | %4s | SKIP  | %8s | $%+.2f" % (
                p["config"]["name"], p["direction"], "—", pnl))
            continue

        if not p["exited"]:
            # Resolve at expiry
            if actual == "UNKNOWN":
                p["exit_price"] = p["entry_price"]
                p["exit_reason"] = "unknown"
            else:
                correct = p["direction"] == actual
                p["exit_price"] = 1.00 if correct else 0.00
                p["exit_reason"] = "expiry"

        exit_value = p["tokens"] * p["exit_price"]
        pnl = exit_value - TRADE_SIZE
        p["pnl"] = round(pnl, 4)
        correct = p["direction"] == actual
        icon = "YES" if correct else "NO"

        print("  %-35s | %4s | %5s | $%.3f %s | $%+.2f" % (
            p["config"]["name"], p["direction"], icon,
            p["exit_price"],
            "(%s)" % p["exit_reason"][:3] if p["exit_reason"] else "",
            pnl))

    print("  " + "=" * 70)

    # Store results
    for p in positions:
        name = p["config"]["name"]
        if name not in all_results:
            all_results[name] = PREV_RESULTS.get(name, [])
        all_results[name].append(p.get("pnl", 0.0))

    return actual


def print_leaderboard(all_results, total_windows):
    """Print cumulative leaderboard."""
    print("\n" + "#" * 70)
    print("  LEADERBOARD AFTER %d WINDOWS" % total_windows)
    print("#" * 70)

    scores = []
    for name, pnls in all_results.items():
        total = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        draws = sum(1 for p in pnls if p == 0)
        scores.append((total, name, pnls, wins, losses, draws))

    scores.sort(reverse=True)

    print("\n  %3s | %-35s | %4s | %4s | %8s | Window PnLs" % (
        "Rk", "Config", "W", "L", "Total"))
    print("  " + "-" * 90)

    for rank, (total, name, pnls, wins, losses, draws) in enumerate(scores, 1):
        medal = ["", "1st", "2nd", "3rd", "4th", "5th"][rank]
        pnl_str = " ".join(["$%+.2f" % p for p in pnls])
        print("  %3s | %-35s | %dW  | %dL  | $%+.2f  | %s" % (
            medal, name, wins, losses, total, pnl_str))

    print("  " + "=" * 90)
    winner = scores[0]
    print("\n  LEADER: %s with $%+.2f total PnL" % (winner[1], winner[0]))
    print()


def main():
    print("\n" + "#" * 70)
    print("  MULTI-CONFIG PARALLEL TEST")
    print("  5 configs x 3 windows (continuing from W1-W2)")
    print("  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("#" * 70)

    # Load history for momentum calc
    try:
        with open(HISTORY_FILE) as f:
            history_windows = json.load(f)
    except:
        history_windows = []

    all_results = dict(PREV_RESULTS)  # Start with previous W1-W2 results

    # Print starting leaderboard
    print_leaderboard(all_results, 2)

    # Run 3 more windows (W3, W4, W5)
    for w in range(3, 6):
        actual = run_one_window(w, history_windows, all_results)

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
        time.sleep(5)

    # Save final results
    with open(RESULT_FILE, "w") as f:
        json.dump(all_results, f, indent=2)
    log("Results saved to %s" % RESULT_FILE)

    # Final summary
    print("\n" + "#" * 70)
    print("  FINAL RESULTS — 5 WINDOWS COMPLETE")
    print("#" * 70)
    print_leaderboard(all_results, 5)


if __name__ == "__main__":
    main()
