"""
Multi-Config Parallel Test V3 — SL Sweet Spot Finder
=====================================================
Test different SL levels to find the sweet spot that avoids whipsaw
but still protects against big losses.

5 CONFIGS:
C1: momentum + SL=$0.40, TP=OFF
C2: momentum + SL=$0.35, TP=OFF
C3: always_up + SL=$0.40, TP=OFF
C4: always_up + SL=$0.35, TP=OFF
C5: always_down + SL=$0.40, TP=OFF

KEY QUESTION: Is SL=$0.35-$0.40 better than SL=$0.45 (too tight) or no SL?

EARLY ENTRY FIX:
- Start scanning for market 30s before block opens (was: wait 10s after)
- Poll every 3s until market found
- MAX_ENTRY_PRICE tightened to $0.55 (was $0.65)
- Goal: enter near $0.50 instead of $0.60+
"""

import time
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
HISTORY_FILE = "polymarket_history.json"
RESULT_FILE = "multi_config_results_v3.json"

TRADE_SIZE = 5.0
MAX_ENTRY_PRICE = 0.55   # tightened from 0.65 — reject bad risk/reward entries
MAX_AGE_SECS = 120
EARLY_ENTRY_SECS = 30    # start looking for market 30s before block opens

CONFIGS = [
    {
        "name": "C1: momentum SL=0.40",
        "direction_mode": "momentum",
        "momentum_lookback": 4,
        "use_stop_loss": True,
        "stop_loss": 0.40,
        "use_take_profit": False,
    },
    {
        "name": "C2: momentum SL=0.35",
        "direction_mode": "momentum",
        "momentum_lookback": 4,
        "use_stop_loss": True,
        "stop_loss": 0.35,
        "use_take_profit": False,
    },
    {
        "name": "C3: always_up SL=0.40",
        "direction_mode": "always_up",
        "momentum_lookback": 4,
        "use_stop_loss": True,
        "stop_loss": 0.40,
        "use_take_profit": False,
    },
    {
        "name": "C4: always_up SL=0.35",
        "direction_mode": "always_up",
        "momentum_lookback": 4,
        "use_stop_loss": True,
        "stop_loss": 0.35,
        "use_take_profit": False,
    },
    {
        "name": "C5: always_down SL=0.40",
        "direction_mode": "always_down",
        "momentum_lookback": 4,
        "use_stop_loss": True,
        "stop_loss": 0.40,
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
    mode = cfg["direction_mode"]
    lb = cfg.get("momentum_lookback", 4)

    closed = [w for w in history_windows
              if w.get("closed") and w.get("outcome") in ("UP", "DOWN")]

    if mode == "momentum":
        if len(closed) >= lb:
            recent = closed[-lb:]
            ups = sum(1 for r in recent if r["outcome"] == "UP")
            return "UP" if ups > lb / 2 else "DOWN"
        return "UP"

    elif mode == "always_up":
        return "UP"

    elif mode == "always_down":
        return "DOWN"

    return "UP"


def check_stop_loss(cfg, current_price, entry_price):
    """Check if SL is triggered. Returns True if stopped out."""
    if not cfg.get("use_stop_loss"):
        return False
    sl = cfg["stop_loss"]
    # SL safety: if SL >= entry (e.g. entry=$0.45, SL=$0.40 is fine)
    # But if entry is very low and SL is higher, cap it
    if sl >= entry_price:
        sl = entry_price * 0.60
    return current_price is not None and current_price <= sl


def run_one_window(window_num, total_windows, history_windows, all_results):
    print("\n" + "=" * 70, flush=True)
    log("=== WINDOW %d/%d ===" % (window_num, total_windows))
    print("=" * 70, flush=True)

    now_ts = int(time.time())
    current_block = (now_ts // 900) * 900
    next_block = current_block + 900
    wait_secs = next_block - now_ts

    tr_tz = timezone(timedelta(hours=3))
    next_time_tr = datetime.fromtimestamp(next_block, tz=tr_tz)
    now_tr = datetime.now(tr_tz)
    log("Su an: %s TR" % now_tr.strftime("%H:%M:%S"))
    log("Sonraki window: %s TR (in %dm %ds)" % (
        next_time_tr.strftime("%H:%M:%S"), wait_secs // 60, wait_secs % 60))

    # EARLY ENTRY: Wait until T-30s, then start polling for market
    early_wait = max(0, wait_secs - EARLY_ENTRY_SECS)
    if early_wait > 0:
        log("Waiting %ds (will start scanning at T-%ds)..." % (early_wait, EARLY_ENTRY_SECS))
        waited = 0
        while waited < early_wait:
            chunk = min(60, early_wait - waited)
            time.sleep(chunk)
            waited += chunk
            rem = early_wait - waited
            if rem > 0:
                log("  %dm %ds remaining" % (rem // 60, rem % 60))

    # Start aggressive polling for market (T-30s to T+30s)
    log("SCANNING for market (early entry mode)...")
    market = None
    scan_start = time.time()
    max_scan_secs = EARLY_ENTRY_SECS + 30  # scan up to 30s after block opens
    while (time.time() - scan_start) < max_scan_secs:
        # Try next block's slug directly (might exist before block opens)
        slug = "btc-updown-15m-%d" % next_block
        m = fetch_market_by_slug(slug)
        if m and not m["closed"] and m.get("token_ids") and len(m["token_ids"]) >= 2:
            entry_ts = time.time()
            offset_from_block = entry_ts - next_block
            log("MARKET FOUND! Entry at T%+.1fs (%.1fs %s block open)" % (
                offset_from_block,
                abs(offset_from_block),
                "before" if offset_from_block < 0 else "after"))
            market = {
                "slug": slug,
                "question": m["question"],
                "token_ids": m["token_ids"],
                "prices": m["prices"],
                "block_ts": next_block,
                "block_end": next_block + 900,
                "entry_offset": offset_from_block,
            }
            break
        time.sleep(3)  # poll every 3s (was 10s fixed wait)

    if not market:
        log("ERROR: No market found after %ds scanning! Skipping." % max_scan_secs)
        for cfg in CONFIGS:
            if cfg["name"] not in all_results:
                all_results[cfg["name"]] = []
            all_results[cfg["name"]].append({"pnl": 0.0, "exit_reason": "no_market"})
        return "UNKNOWN"

    log("Market: %s" % market["question"])

    if not market.get("token_ids") or len(market["token_ids"]) < 2:
        log("ERROR: No token IDs")
        return "UNKNOWN"

    up_token = market["token_ids"][0]
    down_token = market["token_ids"][1]

    up_price = get_clob_midpoint(up_token)
    down_price = get_clob_midpoint(down_token)
    log("CLOB: UP=$%.3f DOWN=$%.3f" % (up_price or 0, down_price or 0))

    positions = []
    for cfg in CONFIGS:
        direction = get_direction(cfg, history_windows)
        if direction == "UP":
            entry_price = up_price
            token_id = up_token
        else:
            entry_price = down_price
            token_id = down_token

        if entry_price is None or entry_price > MAX_ENTRY_PRICE:
            positions.append({
                "config": cfg,
                "direction": direction,
                "skipped": True,
                "reason": "price>$%.2f" % MAX_ENTRY_PRICE if entry_price else "no_price",
                "entry_price": entry_price or 0,
            })
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

        # Calculate effective SL
        eff_sl = cfg["stop_loss"]
        if eff_sl >= entry_price:
            eff_sl = entry_price * 0.60

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
            "eff_sl": eff_sl,
        })

    # Print entries
    print("\n  %-35s | %4s | %6s | %6s | Strategy" % (
        "Config", "Dir", "Entry", "SL"), flush=True)
    print("  " + "-" * 80, flush=True)
    for p in positions:
        if p["skipped"]:
            print("  %-35s | %4s | SKIP   | %6s | %s" % (
                p["config"]["name"], p["direction"], "---", p.get("reason", "")), flush=True)
        else:
            print("  %-35s | %4s | $%.3f | $%.3f | SL=$%.2f TP=OFF" % (
                p["config"]["name"], p["direction"],
                p["entry_price"], p["eff_sl"], p["config"]["stop_loss"]), flush=True)

    active_positions = [p for p in positions if not p["skipped"]]
    if not active_positions:
        log("All configs skipped!")
        for cfg in CONFIGS:
            if cfg["name"] not in all_results:
                all_results[cfg["name"]] = []
            all_results[cfg["name"]].append({"pnl": 0.0, "exit_reason": "skipped"})
        return "UNKNOWN"

    # Monitor with SL checks
    # Calculate remaining time until block closes (account for early entry)
    block_end = market["block_end"]
    remaining_secs = max(60, int(block_end - time.time()))
    log("Monitoring for %ds (until block closes)" % remaining_secs)
    check_interval = 5  # check every 5s for SL
    total_checks = remaining_secs // check_interval

    print("\n  %5s |" % "Time", end="", flush=True)
    for p in active_positions:
        short = p["config"]["name"][:8]
        print(" %8s |" % short, end="", flush=True)
    print("", flush=True)
    print("  " + "-" * (8 + len(active_positions) * 11), flush=True)

    sl_trigger_log = []

    for tick in range(1, total_checks + 1):
        time.sleep(check_interval)
        elapsed = tick * check_interval
        elapsed_min = elapsed / 60.0

        cur_up = get_clob_midpoint(up_token)
        cur_down = get_clob_midpoint(down_token)

        for p in active_positions:
            if p["exited"]:
                continue
            if p["direction"] == "UP":
                cur_price = cur_up
            else:
                cur_price = cur_down

            if cur_price:
                p["min_price"] = min(p["min_price"], cur_price)
                p["max_price"] = max(p["max_price"], cur_price)

            # Check SL
            if check_stop_loss(p["config"], cur_price, p["entry_price"]):
                p["exited"] = True
                p["exit_price"] = cur_price
                p["exit_reason"] = "SL"
                sl_trigger_log.append(
                    "  >> SL HIT: %s at $%.3f (entry=$%.3f, SL=$%.3f) @ %.1fm" % (
                        p["config"]["name"], cur_price, p["entry_price"],
                        p["eff_sl"], elapsed_min))

        # Print every 30 seconds
        if elapsed % 30 == 0:
            print("  %4.1fm |" % elapsed_min, end="", flush=True)
            for p in active_positions:
                if p["exited"]:
                    print("  SL-OUT |" % (), end="", flush=True) if p["exit_reason"] == "SL" else print(" %8s |" % "EXIT", end="", flush=True)
                else:
                    cp = cur_up if p["direction"] == "UP" else cur_down
                    if cp:
                        pnl = (cp - p["entry_price"]) * p["tokens"]
                        print(" $%+.2f  |" % pnl, end="", flush=True)
                    else:
                        print(" %8s |" % "N/A", end="", flush=True)
            print("", flush=True)

            # Print any SL triggers since last print
            for sl_log in sl_trigger_log:
                print(sl_log, flush=True)
            sl_trigger_log.clear()

    # Print any remaining SL triggers
    for sl_log in sl_trigger_log:
        print(sl_log, flush=True)

    # Expiry resolution
    log("Window ended. Waiting for resolution...")
    time.sleep(20)

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

        final_up = get_clob_midpoint(up_token)
        final_down = get_clob_midpoint(down_token)
        if final_up and final_down:
            if final_up > 0.85:
                actual = "UP"
                log("Resolved via CLOB: UP=$%.3f (attempt %d)" % (final_up, attempt + 1))
                break
            elif final_down > 0.85:
                actual = "DOWN"
                log("Resolved via CLOB: DOWN=$%.3f (attempt %d)" % (final_down, attempt + 1))
                break

        if attempt < 7:
            log("  Not resolved yet, retry in 10s (attempt %d/8)..." % (attempt + 1))
            time.sleep(10)

    log("Resolved: %s" % actual)

    # Calculate PnL
    print("\n  " + "=" * 90, flush=True)
    print("  WINDOW %d RESULTS (Actual: %s)" % (window_num, actual), flush=True)
    print("  " + "=" * 90, flush=True)
    print("  %-35s | %4s | %5s | %8s | %8s | %8s | %s" % (
        "Config", "Dir", "Corr?", "Exit$", "Reason", "PnL", "Price Range"), flush=True)
    print("  " + "-" * 100, flush=True)

    for p in positions:
        name = p["config"]["name"]
        if name not in all_results:
            all_results[name] = []

        if p["skipped"]:
            all_results[name].append({"pnl": 0.0, "exit_reason": "skipped"})
            print("  %-35s | %4s | SKIP  | %8s | %8s | $%+.2f  | ---" % (
                name, p["direction"], "---", "skip", 0.0), flush=True)
            continue

        if p["exited"] and p["exit_reason"] == "SL":
            # Stopped out
            exit_value = p["tokens"] * p["exit_price"]
            pnl = exit_value - TRADE_SIZE
        elif actual == "UNKNOWN":
            p["exit_price"] = p["entry_price"]
            p["exit_reason"] = "unknown"
            pnl = 0.0
        else:
            correct = p["direction"] == actual
            p["exit_price"] = 1.00 if correct else 0.00
            p["exit_reason"] = "expiry"
            exit_value = p["tokens"] * p["exit_price"]
            pnl = exit_value - TRADE_SIZE

        pnl = round(pnl, 4)
        all_results[name].append({"pnl": pnl, "exit_reason": p.get("exit_reason", "expiry")})

        correct = p["direction"] == actual if actual != "UNKNOWN" else "?"
        if p["exit_reason"] == "SL":
            icon = "SL"
        elif correct == True:
            icon = "WIN"
        elif correct == False:
            icon = "LOSS"
        else:
            icon = "?"

        price_range = "$%.2f-$%.2f" % (p["min_price"], p["max_price"])
        reason = p.get("exit_reason", "expiry")

        print("  %-35s | %4s | %5s | $%.3f   | %8s | $%+.2f  | %s" % (
            name, p["direction"], icon,
            p["exit_price"], reason, pnl, price_range), flush=True)

    print("  " + "=" * 90, flush=True)

    # SL analysis for this window
    sl_hits = [p for p in positions if not p["skipped"] and p.get("exit_reason") == "SL"]
    held = [p for p in positions if not p["skipped"] and p.get("exit_reason") != "SL"]
    if sl_hits:
        print("\n  SL ANALYSIS:", flush=True)
        for p in sl_hits:
            correct = p["direction"] == actual if actual != "UNKNOWN" else "?"
            would_have = ""
            if correct == True:
                tokens = TRADE_SIZE / p["entry_price"]
                would_pnl = tokens * 1.00 - TRADE_SIZE
                sl_pnl = p["tokens"] * p["exit_price"] - TRADE_SIZE
                missed = would_pnl - sl_pnl
                would_have = " >>> WOULD HAVE WON $%+.2f if held!" % missed
            elif correct == False:
                would_have = " (correct SL - saved from full loss)"
            print("    %s: SL@$%.3f dir=%s actual=%s%s" % (
                p["config"]["name"], p["exit_price"],
                p["direction"], actual, would_have), flush=True)
    print("", flush=True)

    return actual


def print_leaderboard(all_results, total_windows):
    print("\n" + "#" * 70, flush=True)
    print("  LEADERBOARD AFTER %d WINDOWS" % total_windows, flush=True)
    print("#" * 70, flush=True)

    scores = []
    for name, results in all_results.items():
        pnls = [r["pnl"] for r in results]
        total = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        sl_count = sum(1 for r in results if r["exit_reason"] == "SL")
        wr = wins / max(1, wins + losses) * 100
        scores.append((total, name, pnls, wins, losses, wr, sl_count))

    scores.sort(reverse=True)

    print("\n  %3s | %-35s | %3s | %3s | %5s | %3s | %8s | PnLs" % (
        "Rk", "Config", "W", "L", "WR%", "SL", "Total"), flush=True)
    print("  " + "-" * 105, flush=True)

    for rank, (total, name, pnls, wins, losses, wr, sl_count) in enumerate(scores, 1):
        pnl_str = " ".join(["$%+.2f" % p for p in pnls])
        print("  %3d | %-35s | %dW  | %dL  | %4.0f%% | %dx  | $%+.2f  | %s" % (
            rank, name, wins, losses, wr, sl_count, total, pnl_str), flush=True)

    print("  " + "=" * 105, flush=True)
    winner = scores[0]
    print("\n  LEADER: %s with $%+.2f total PnL (%d/%d wins, %d SL outs)" % (
        winner[1], winner[0], winner[3], winner[3] + winner[4], winner[6]), flush=True)

    if total_windows >= 3:
        avg = winner[0] / total_windows
        print("  Avg/window: $%+.2f | Projected 20w: $%+.2f (target: $20)" % (
            avg, avg * 20), flush=True)

    # SL impact analysis
    print("\n  SL IMPACT ANALYSIS:", flush=True)
    for total, name, pnls, wins, losses, wr, sl_count in scores:
        results = all_results[name]
        sl_losses = sum(r["pnl"] for r in results if r["exit_reason"] == "SL")
        expiry_pnl = sum(r["pnl"] for r in results if r["exit_reason"] == "expiry")
        print("    %s: SL loss=$%.2f, Expiry PnL=$%+.2f, SL rate=%d/%d" % (
            name, sl_losses, expiry_pnl, sl_count, len(results)), flush=True)
    print("", flush=True)


def main():
    total_windows = 5

    print("\n" + "#" * 70, flush=True)
    print("  MULTI-CONFIG TEST V3 -- SL SWEET SPOT FINDER", flush=True)
    print("  Question: Is SL=$0.35-$0.40 better than $0.45 or no SL?", flush=True)
    print("  5 configs x %d windows" % total_windows, flush=True)
    tr_tz = timezone(timedelta(hours=3))
    print("  %s TR" % datetime.now(tr_tz).strftime("%Y-%m-%d %H:%M:%S"), flush=True)
    print("#" * 70, flush=True)

    print("\n  CONFIGS:", flush=True)
    for cfg in CONFIGS:
        sl_str = "$%.2f" % cfg["stop_loss"] if cfg.get("use_stop_loss") else "OFF"
        tp_str = "OFF"
        print("    %s" % cfg["name"], flush=True)
        print("      direction=%s, SL=%s, TP=%s" % (
            cfg["direction_mode"], sl_str, tp_str), flush=True)

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
        log("No history file found")

    all_results = {}

    for w in range(1, total_windows + 1):
        actual = run_one_window(w, total_windows, history_windows, all_results)

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

    # Save results
    save_data = {}
    for name, results in all_results.items():
        save_data[name] = results
    with open(RESULT_FILE, "w") as f:
        json.dump(save_data, f, indent=2, default=str)
    log("Results saved to %s" % RESULT_FILE)

    # Final
    print("\n" + "#" * 70, flush=True)
    print("  FINAL RESULTS -- V3 SL SWEET SPOT TEST", flush=True)
    print("#" * 70, flush=True)
    print_leaderboard(all_results, total_windows)

    print("  CONCLUSION:", flush=True)
    print("  Compare SL=$0.40 vs SL=$0.35 vs previous data:", flush=True)
    print("    - V1 (SL=$0.45): SL killed winning trades constantly", flush=True)
    print("    - V2 (no SL): Hold to expiry, max PnL on wins but full loss on losses", flush=True)
    print("    - V3 (SL=$0.35-$0.40): Sweet spot test results above", flush=True)
    print("", flush=True)


if __name__ == "__main__":
    main()
