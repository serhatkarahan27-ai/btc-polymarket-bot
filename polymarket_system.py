"""
Polymarket-Only Trading System
==============================
3-Phase autonomous system using ONLY Polymarket data.

Phase 1: Collect historical 15-min BTC window outcomes from Gamma API
Phase 2: Backtest direction strategies on real Polymarket outcomes
Phase 3: Live validation with CLOB midpoint prices

NO Binance data. Score = real dollar PnL.
"""

import time
import json
import math
import requests
import itertools
import sys
from datetime import datetime, timezone
from pathlib import Path

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
BINANCE_BASE = "https://api.binance.com/api/v3"

HISTORY_FILE = "polymarket_history.json"
LIVE_FILE = "live_results.json"
SYSTEM_HISTORY_DIR = Path("history")

# ============================================================
# CONFIG — tuning parameters
# ============================================================
CONFIG = {
    "dry_mode": True,
    "trade_size_usd": 5.0,
    "window_minutes": 15,
    "collect_days": 7,
    # Direction
    "direction_mode": "always_up",
    "momentum_lookback": 4,       # how many previous windows to look back
    # Entry
    "entry_price": 0.50,
    # SL/TP — exp_025 showed SL=$0.45 killed 2 winning trades
    # Hypothesis: disable SL, hold to expiry. Asymmetric payoff means
    # 1 win ($5+) covers 2 losses ($5 each). SL just adds false exits.
    "stop_loss": 0.15,
    "take_profit": 0.70,
    "use_stop_loss": False,
    "use_take_profit": False,
    # Live
    "live_windows": 3,
    "fill_check_interval": 5,     # seconds between price checks
    "limit_price": 0.65,          # accept UP token up to $0.65
    "max_spread_pct": 15.0,
}


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print("  [%s] %s" % (ts, msg))


# ============================================================
# PHASE 1: HISTORICAL DATA COLLECTION
# ============================================================

def fetch_market_by_slug(slug):
    """Fetch single market from Gamma API."""
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
    except Exception as e:
        return None


def get_clob_midpoint(token_id):
    """Get current CLOB midpoint price for a token."""
    try:
        r = requests.get("%s/midpoint" % CLOB_BASE,
                         params={"token_id": token_id}, timeout=5)
        if r.status_code == 200:
            return float(r.json().get("mid", 0))
    except:
        pass
    return None


def collect_historical_data():
    """Phase 1: Collect all resolved 15-min BTC windows from past N days."""
    days = CONFIG["collect_days"]
    now_ts = int(time.time())
    blocks_per_day = 96  # 24*4
    total_blocks = blocks_per_day * days

    print("\n" + "=" * 65)
    print("  PHASE 1: HISTORICAL DATA COLLECTION")
    print("  Scanning %d days (%d blocks)..." % (days, total_blocks))
    print("=" * 65)

    windows = []
    found = 0
    errors = 0
    start_block_offset = -total_blocks

    for offset in range(start_block_offset, 1):
        block_ts = ((now_ts // 900) + offset) * 900
        slug = "btc-updown-15m-%d" % block_ts
        m = fetch_market_by_slug(slug)

        if m is None:
            errors += 1
            continue

        # Determine outcome
        if m["closed"] and len(m["prices"]) >= 2:
            up_price = m["prices"][0]
            down_price = m["prices"][1]
            if up_price > 0.5:
                outcome = "UP"
            elif down_price > 0.5:
                outcome = "DOWN"
            else:
                outcome = "UNKNOWN"
        elif not m["closed"]:
            outcome = "OPEN"
        else:
            outcome = "UNKNOWN"

        window = {
            "block_ts": block_ts,
            "slug": slug,
            "question": m["question"],
            "closed": m["closed"],
            "outcome": outcome,
            "up_price": m["prices"][0] if len(m["prices"]) >= 2 else None,
            "down_price": m["prices"][1] if len(m["prices"]) >= 2 else None,
            "volume": m["volume"],
            "token_ids": m["token_ids"],
        }
        windows.append(window)
        found += 1

        # Progress every 50 blocks
        if found % 50 == 0:
            log("  %d/%d blocks scanned, %d found..." % (
                offset - start_block_offset, total_blocks, found))

        # Rate limit - be gentle to the API
        if found % 10 == 0:
            time.sleep(0.3)

    # Stats
    closed_windows = [w for w in windows if w["closed"]]
    up_wins = sum(1 for w in closed_windows if w["outcome"] == "UP")
    down_wins = sum(1 for w in closed_windows if w["outcome"] == "DOWN")

    print("\n  COLLECTION RESULTS:")
    print("  Total found:    %d" % found)
    print("  Closed/resolved:%d" % len(closed_windows))
    print("  UP wins:        %d (%.1f%%)" % (up_wins, up_wins / max(1, len(closed_windows)) * 100))
    print("  DOWN wins:      %d (%.1f%%)" % (down_wins, down_wins / max(1, len(closed_windows)) * 100))
    print("  API errors:     %d" % errors)

    # Save
    with open(HISTORY_FILE, "w") as f:
        json.dump(windows, f, indent=2)
    log("Saved to %s" % HISTORY_FILE)

    return windows


# ============================================================
# PHASE 2: BACKTEST ON REAL POLYMARKET DATA
# ============================================================

def load_history():
    """Load historical Polymarket window data."""
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except:
        return []


def backtest_direction(windows, cfg):
    """Backtest a direction strategy on resolved Polymarket windows.

    Each window is a binary outcome: UP or DOWN.
    We simulate buying the predicted token at entry_price,
    and it resolves to $1.00 (correct) or $0.00 (wrong).

    For SL/TP: We model token price path using a simplified
    time-decay model. As the window progresses:
    - Correct prediction: token price rises toward 1.00
    - Wrong prediction: token price falls toward 0.00

    Returns list of trade results.
    """
    mode = cfg.get("direction_mode", "always_up")
    lookback = cfg.get("momentum_lookback", 4)
    entry_price = cfg.get("entry_price", 0.50)
    sl = cfg.get("stop_loss", 0.35)
    tp = cfg.get("take_profit", 0.70)
    use_sl = cfg.get("use_stop_loss", True)
    use_tp = cfg.get("use_take_profit", True)
    trade_size = cfg.get("trade_size_usd", 5.0)
    window_min = cfg.get("window_minutes", 15)

    closed = [w for w in windows if w["closed"] and w["outcome"] in ("UP", "DOWN")]
    if len(closed) < 10:
        return []

    trades = []
    for i, w in enumerate(closed):
        # Determine our prediction
        if mode == "always_up":
            prediction = "UP"
        elif mode == "always_down":
            prediction = "DOWN"
        elif mode == "follow_previous":
            if i == 0:
                prediction = "UP"
            else:
                prediction = closed[i - 1]["outcome"]
        elif mode == "contrarian":
            if i == 0:
                prediction = "UP"
            else:
                prediction = "DOWN" if closed[i - 1]["outcome"] == "UP" else "UP"
        elif mode == "momentum":
            # Look at last N windows, go with majority direction
            if i < lookback:
                prediction = "UP"
            else:
                recent = closed[i - lookback:i]
                ups = sum(1 for r in recent if r["outcome"] == "UP")
                prediction = "UP" if ups > lookback / 2 else "DOWN"
        elif mode == "streak":
            # If last N were all same direction, follow it; else contrarian
            if i < lookback:
                prediction = "UP"
            else:
                recent = [r["outcome"] for r in closed[i - lookback:i]]
                if all(r == recent[0] for r in recent):
                    prediction = recent[0]  # streak continues
                else:
                    # mixed: go with majority
                    ups = sum(1 for r in recent if r == "UP")
                    prediction = "UP" if ups > lookback / 2 else "DOWN"
        elif mode == "volume_bias":
            # Higher volume windows tend to have stronger moves
            # Use previous window volume as signal
            if i == 0:
                prediction = "UP"
            else:
                prev_vol = closed[i - 1]["volume"]
                median_vol = sorted([c["volume"] for c in closed[:i]])[len(closed[:i]) // 2]
                # High volume previous window = trend continues
                if prev_vol > median_vol:
                    prediction = closed[i - 1]["outcome"]
                else:
                    prediction = "DOWN" if closed[i - 1]["outcome"] == "UP" else "UP"
        else:
            prediction = "UP"

        correct = prediction == w["outcome"]

        # Simulate token price path for SL/TP
        # Model: token moves linearly toward 1.0 (correct) or 0.0 (wrong)
        # with some noise. We check at each minute.
        exit_price = None
        exit_reason = "expiry"
        exit_minute = window_min

        for m in range(1, window_min + 1):
            time_frac = m / window_min  # 0.0 → 1.0
            if correct:
                # Token gradually moves toward 1.0
                # But not linearly — it accelerates near the end
                base_price = entry_price + (1.0 - entry_price) * (time_frac ** 0.7)
            else:
                # Token gradually moves toward 0.0
                base_price = entry_price * (1.0 - time_frac ** 0.7)

            # Add some realistic variance (±10% of the move)
            # Use deterministic "noise" based on block_ts + minute
            noise_seed = (w["block_ts"] + m * 7) % 100
            noise = (noise_seed - 50) / 500.0  # ±0.10
            token_price = max(0.01, min(0.99, base_price + noise))

            # SL check
            if use_sl and token_price <= sl:
                exit_price = sl
                exit_reason = "stop_loss"
                exit_minute = m
                break

            # TP check
            if use_tp and token_price >= tp:
                exit_price = tp
                exit_reason = "take_profit"
                exit_minute = m
                break

        # Expiry resolution
        if exit_price is None:
            exit_price = 1.00 if correct else 0.00

        tokens = trade_size / entry_price
        exit_value = tokens * exit_price
        pnl = exit_value - trade_size
        roi = (pnl / trade_size) * 100

        trades.append({
            "block_ts": w["block_ts"],
            "prediction": prediction,
            "actual": w["outcome"],
            "correct": correct,
            "entry_price": entry_price,
            "exit_price": round(exit_price, 4),
            "exit_reason": exit_reason,
            "exit_minute": exit_minute,
            "pnl_usd": round(pnl, 4),
            "roi_pct": round(roi, 2),
            "win": pnl > 0,
            "volume": w["volume"],
        })

    return trades


def auto_optimize_polymarket(windows):
    """Grid search over direction strategies and SL/TP on real Polymarket data."""
    print("\n" + "=" * 65)
    print("  PHASE 2: AUTO-OPTIMIZATION ON POLYMARKET DATA")
    print("=" * 65)

    closed = [w for w in windows if w["closed"] and w["outcome"] in ("UP", "DOWN")]
    print("  Resolved windows: %d" % len(closed))

    if len(closed) < 50:
        print("  WARNING: Only %d windows (need 50+). Results may be unreliable." % len(closed))
        if len(closed) < 10:
            print("  ERROR: Too few windows to optimize. Collect more data first.")
            return None, None, None

    param_grid = {
        "direction_mode": ["always_up", "always_down", "momentum",
                           "follow_previous", "contrarian", "streak",
                           "volume_bias"],
        "momentum_lookback": [2, 3, 4, 6, 8],
        "entry_price": [0.48, 0.50, 0.52, 0.55, 0.58, 0.60],
        "stop_loss": [0.20, 0.25, 0.30, 0.35, 0.40, 0.45],
        "take_profit": [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85],
        "use_stop_loss": [True, False],
        "use_take_profit": [True, False],
    }

    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combos = list(itertools.product(*values))
    print("  Testing %d parameter combinations..." % len(combos))

    best_pnl = -9999
    best_config = None
    best_trades = None
    tested = 0

    for combo in combos:
        test_cfg = dict(CONFIG)
        for k, v in zip(keys, combo):
            test_cfg[k] = v

        # Skip invalid: SL >= TP
        if test_cfg["use_stop_loss"] and test_cfg["use_take_profit"]:
            if test_cfg["stop_loss"] >= test_cfg["take_profit"]:
                continue
        # SL >= entry
        if test_cfg["use_stop_loss"] and test_cfg["stop_loss"] >= test_cfg["entry_price"]:
            continue

        trades = backtest_direction(closed, test_cfg)
        tested += 1

        if len(trades) < 10:
            continue

        total_pnl = sum(t["pnl_usd"] for t in trades)
        wins = sum(1 for t in trades if t["win"])
        wr = wins / len(trades)
        gross_profit = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
        gross_loss = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else 99.0

        # Score = total PnL (dollar-based, as requested)
        if total_pnl > best_pnl:
            best_pnl = total_pnl
            best_config = dict(test_cfg)
            best_trades = trades
            print("  [NEW BEST] PnL=$%+.2f WR=%.1f%% PF=%.2f (%d trades)"
                  " | dir=%s lb=%s entry=$%.2f SL=%s TP=%s" % (
                      total_pnl, wr * 100, pf, len(trades),
                      test_cfg["direction_mode"],
                      test_cfg["momentum_lookback"],
                      test_cfg["entry_price"],
                      "OFF" if not test_cfg["use_stop_loss"] else str(test_cfg["stop_loss"]),
                      "OFF" if not test_cfg["use_take_profit"] else str(test_cfg["take_profit"]),
                  ))

    print("\n  Tested %d valid combinations" % tested)
    return best_config, best_trades, best_pnl


def print_backtest_results(trades, cfg, label=""):
    """Print detailed backtest summary."""
    if not trades:
        print("  No trades to report.")
        return {}

    wins = sum(1 for t in trades if t["win"])
    losses = len(trades) - wins
    wr = wins / len(trades)
    total_pnl = sum(t["pnl_usd"] for t in trades)
    avg_pnl = total_pnl / len(trades)
    gross_profit = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    gross_loss = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Max drawdown
    running = 0
    peak = 0
    max_dd = 0
    for t in trades:
        running += t["pnl_usd"]
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    # Exit reasons
    sl_n = sum(1 for t in trades if t["exit_reason"] == "stop_loss")
    tp_n = sum(1 for t in trades if t["exit_reason"] == "take_profit")
    exp_n = sum(1 for t in trades if t["exit_reason"] == "expiry")

    # Direction accuracy
    correct = sum(1 for t in trades if t["correct"])

    print("\n  " + "=" * 60)
    print("  %s RESULTS" % label)
    print("  " + "=" * 60)
    print("  Direction:       %s (lookback=%s)" % (
        cfg.get("direction_mode", "?"), cfg.get("momentum_lookback", "?")))
    print("  Entry:           $%.2f" % cfg.get("entry_price", 0.50))
    print("  SL:              %s" % ("OFF" if not cfg.get("use_stop_loss") else "$%.2f" % cfg["stop_loss"]))
    print("  TP:              %s" % ("OFF" if not cfg.get("use_take_profit") else "$%.2f" % cfg["take_profit"]))
    print("  " + "-" * 40)
    print("  Trades:          %d (%dW / %dL)" % (len(trades), wins, losses))
    print("  Win Rate:        %.1f%%" % (wr * 100))
    print("  Direction Acc:   %.1f%% (%d/%d correct)" % (
        correct / len(trades) * 100, correct, len(trades)))
    print("  Profit Factor:   %.2f" % pf)
    print("  " + "-" * 40)
    print("  Total PnL:       $%+.2f" % total_pnl)
    print("  Avg PnL/trade:   $%+.4f" % avg_pnl)
    print("  Max Drawdown:    $%.2f" % max_dd)
    print("  " + "-" * 40)
    print("  Gross Profit:    $%.2f" % gross_profit)
    print("  Gross Loss:      $%.2f" % gross_loss)
    print("  Exits:           SL=%d TP=%d Expiry=%d" % (sl_n, tp_n, exp_n))
    print("  " + "=" * 60)

    # Last 10 trades
    print("\n  Last 10 trades:")
    for t in trades[-10:]:
        icon = "W" if t["win"] else "L"
        chk = "OK" if t["correct"] else "XX"
        print("    [%s] pred=%s actual=%s [%s] | exit=$%.2f (%s @%dm)"
              " | PnL $%+.2f" % (
                  icon, t["prediction"], t["actual"], chk,
                  t["exit_price"], t["exit_reason"], t["exit_minute"],
                  t["pnl_usd"]))

    return {
        "wins": wins, "losses": losses, "win_rate": round(wr, 4),
        "total_pnl_usd": round(total_pnl, 4),
        "profit_factor": round(pf, 4),
        "max_drawdown": round(max_dd, 4),
        "direction_accuracy": round(correct / len(trades), 4),
        "sl_exits": sl_n, "tp_exits": tp_n, "expiry_exits": exp_n,
    }


# ============================================================
# PHASE 3: LIVE VALIDATION
# ============================================================

def get_btc_momentum_poly(windows, lookback=4):
    """Get BTC momentum from recent Polymarket window outcomes.
    Pure Polymarket data - no Binance.
    """
    closed = [w for w in windows if w["closed"] and w["outcome"] in ("UP", "DOWN")]
    if len(closed) < lookback:
        return "UP", 0.0
    recent = closed[-lookback:]
    ups = sum(1 for r in recent if r["outcome"] == "UP")
    downs = lookback - ups
    direction = "UP" if ups >= downs else "DOWN"
    strength = abs(ups - downs) / lookback
    return direction, strength


def find_next_window():
    """Find the next open 15-min BTC window that just started (< 2 min old)."""
    now_ts = int(time.time())
    for offset in range(0, 4):
        block = ((now_ts // 900) + offset) * 900
        slug = "btc-updown-15m-%d" % block
        m = fetch_market_by_slug(slug)
        if m and not m["closed"]:
            age_secs = now_ts - block
            # Only accept windows in their first 120 seconds
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


def get_live_price(market, direction):
    """Get real-time CLOB midpoint price for our token."""
    if not market.get("token_ids") or len(market["token_ids"]) < 2:
        return None, None, None

    idx = 0 if direction == "UP" else 1
    token_id = market["token_ids"][idx]
    mid = get_clob_midpoint(token_id)

    # Also get the other side
    other_idx = 1 - idx
    other_mid = get_clob_midpoint(market["token_ids"][other_idx])

    return mid, other_mid, "CLOB_midpoint"


def run_live_window(cfg, windows_history):
    """Execute one live window trade with CLOB midpoint prices."""
    print("\n  " + "-" * 50)
    log("Waiting for next window...")

    # Wait for next 15-min block
    now_ts = int(time.time())
    current_block = (now_ts // 900) * 900
    next_block = current_block + 900
    wait_secs = next_block - now_ts

    next_time = datetime.fromtimestamp(next_block)
    log("Next window: %s (in %dm %ds)" % (
        next_time.strftime("%H:%M:%S"), wait_secs // 60, wait_secs % 60))

    # Wait (cap at 20 min)
    target_wait = min(wait_secs - cfg.get("early_entry_seconds", 3),
                      20 * 60)
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
    time.sleep(10)  # Give Polymarket time to open the new window

    # Find market (only fresh windows < 2 min old)
    market = find_next_window()
    if not market:
        log("No fresh market found. Waiting 30s and retrying...")
        time.sleep(30)
        market = find_next_window()
    if not market:
        log("ERROR: No market found after retry!")
        return None

    log("Market: %s" % market["question"])

    # Determine direction from cfg
    mode = cfg.get("direction_mode", "always_up")
    if mode == "always_up":
        direction = "UP"
    elif mode == "always_down":
        direction = "DOWN"
    elif mode in ("momentum", "follow_previous", "streak"):
        # Use recent Polymarket window outcomes
        closed = [w for w in windows_history
                  if w["closed"] and w["outcome"] in ("UP", "DOWN")]
        lb = cfg.get("momentum_lookback", 4)
        if len(closed) >= lb:
            recent = closed[-lb:]
            ups = sum(1 for r in recent if r["outcome"] == "UP")
            if mode == "contrarian":
                direction = "DOWN" if ups > lb / 2 else "UP"
            else:
                direction = "UP" if ups > lb / 2 else "DOWN"
        else:
            direction = "UP"
    elif mode == "contrarian":
        closed = [w for w in windows_history
                  if w["closed"] and w["outcome"] in ("UP", "DOWN")]
        if closed:
            direction = "DOWN" if closed[-1]["outcome"] == "UP" else "UP"
        else:
            direction = "UP"
    else:
        direction = "UP"

    log("Direction: %s (mode=%s)" % (direction, mode))

    # Get initial CLOB price
    our_price, other_price, src = get_live_price(market, direction)
    if our_price is None or our_price <= 0:
        log("ERROR: Could not get CLOB price")
        return _save_live_skip(market, direction, "no_price", cfg)

    log("CLOB midpoint: %s=$%.3f other=$%.3f [%s]" % (
        direction, our_price, other_price or 0, src))

    # Check if price is acceptable for entry
    if our_price > cfg.get("limit_price", 0.65):
        log("SKIP: Price $%.3f > limit $%.3f" % (our_price, cfg["limit_price"]))
        return _save_live_skip(market, direction, "price_too_high", cfg)

    # Sanity check: reject extreme prices (market already decided)
    if our_price < 0.20:
        log("SKIP: Price $%.3f too low (market already resolved)" % our_price)
        return _save_live_skip(market, direction, "price_extreme_low", cfg)
    if our_price > 0.80:
        log("SKIP: Price $%.3f too high (overpaying)" % our_price)
        return _save_live_skip(market, direction, "price_extreme_high", cfg)

    # Entry
    entry_price = our_price
    trade_size = cfg["trade_size_usd"]
    tokens = trade_size / entry_price
    # SL/TP: only valid if below/above entry price
    sl = cfg.get("stop_loss", 0.35)
    tp = cfg.get("take_profit", 0.70)
    if sl >= entry_price:
        sl = entry_price * 0.60  # 40% below entry as fallback
    if tp <= entry_price:
        tp = entry_price * 1.40  # 40% above entry as fallback

    log("ENTRY: %s @ $%.3f | size=$%.2f | tokens=%.2f" % (
        direction, entry_price, trade_size, tokens))
    log("  SL=$%.3f TP=$%.3f" % (sl, tp))

    # Monitor position
    window_secs = cfg["window_minutes"] * 60
    check_interval = cfg.get("fill_check_interval", 5)
    total_checks = window_secs // check_interval

    exit_price = None
    exit_reason = "expiry"
    exit_minute = cfg["window_minutes"]
    min_price = entry_price
    max_price = entry_price
    price_path = [{"sec": 0, "price": entry_price}]

    print("\n  %5s | %8s | %8s | Status" % ("Time", "Price", "PnL"))
    print("  " + "-" * 45)

    for tick in range(1, total_checks + 1):
        time.sleep(check_interval)
        elapsed = tick * check_interval
        elapsed_min = elapsed / 60.0

        try:
            cur_price, _, _ = get_live_price(market, direction)
            if cur_price is None:
                continue

            min_price = min(min_price, cur_price)
            max_price = max(max_price, cur_price)
            cur_pnl = (cur_price - entry_price) * tokens

            price_path.append({"sec": elapsed, "price": round(cur_price, 4)})

            # Print every 15 seconds
            if elapsed % 15 == 0:
                print("  %4.1fm | $%.4f | $%+.4f | HOLD" % (
                    elapsed_min, cur_price, cur_pnl))

            # SL check
            if cfg.get("use_stop_loss") and cur_price <= sl:
                exit_price = cur_price
                exit_reason = "stop_loss"
                exit_minute = int(elapsed_min) + 1
                print("  %4.1fm | $%.4f | $%+.4f | !! STOP-LOSS !!" % (
                    elapsed_min, cur_price, cur_pnl))
                break

            # TP check
            if cfg.get("use_take_profit") and cur_price >= tp:
                exit_price = cur_price
                exit_reason = "take_profit"
                exit_minute = int(elapsed_min) + 1
                print("  %4.1fm | $%.4f | $%+.4f | ** TAKE-PROFIT **" % (
                    elapsed_min, cur_price, cur_pnl))
                break

        except Exception as e:
            pass

    # Expiry resolution
    if exit_price is None:
        # Wait a moment for resolution
        log("Window ended. Checking resolution...")
        time.sleep(10)
        # Refetch market to see if resolved
        m = fetch_market_by_slug(market["slug"])
        if m and m["closed"] and len(m["prices"]) >= 2:
            if m["prices"][0] > 0.5:
                actual = "UP"
            else:
                actual = "DOWN"
            exit_price = 1.00 if direction == actual else 0.00
            log("Resolved: %s -> %s = %s" % (
                direction, actual, "WIN" if exit_price > 0 else "LOSS"))
        else:
            # Not resolved yet - check final CLOB price
            final_price, _, _ = get_live_price(market, direction)
            if final_price is not None:
                exit_price = final_price
                log("Using final CLOB price: $%.3f" % exit_price)
            else:
                exit_price = entry_price  # fallback
                log("WARNING: Could not determine resolution, using entry price")

    # Calculate PnL
    exit_value = tokens * exit_price
    pnl = exit_value - trade_size
    roi = (pnl / trade_size) * 100

    print("\n  " + "=" * 50)
    print("  TRADE RESULT")
    print("  " + "=" * 50)
    print("  Direction:   %s" % direction)
    print("  Entry:       $%.4f" % entry_price)
    print("  Exit:        $%.4f (%s @%dm)" % (exit_price, exit_reason, exit_minute))
    print("  Min/Max:     $%.4f / $%.4f" % (min_price, max_price))
    print("  PnL:         $%+.4f (ROI: %+.1f%%)" % (pnl, roi))
    print("  Result:      %s" % ("WIN" if pnl > 0 else "LOSS"))
    print("  " + "=" * 50)

    result = {
        "timestamp": datetime.now().isoformat(),
        "block_ts": market["block_ts"],
        "market": market["question"],
        "slug": market["slug"],
        "direction": direction,
        "direction_mode": mode,
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "exit_reason": exit_reason,
        "exit_minute": exit_minute,
        "tokens": round(tokens, 4),
        "trade_size": trade_size,
        "pnl_usd": round(pnl, 4),
        "roi_pct": round(roi, 2),
        "win": pnl > 0,
        "min_price": round(min_price, 4),
        "max_price": round(max_price, 4),
        "price_path": price_path,
        "config": {k: v for k, v in cfg.items() if k != "dry_mode"},
    }

    # Save to live results
    live = _load_live()
    live.append(result)
    _save_live(live)
    log("Saved to %s (%d total)" % (LIVE_FILE, len(live)))

    return result


def _save_live_skip(market, direction, reason, cfg):
    result = {
        "timestamp": datetime.now().isoformat(),
        "block_ts": market["block_ts"],
        "market": market["question"],
        "slug": market["slug"],
        "direction": direction,
        "exit_reason": reason,
        "pnl_usd": 0,
        "win": False,
        "skipped": True,
    }
    live = _load_live()
    live.append(result)
    _save_live(live)
    return result


def _load_live():
    try:
        with open(LIVE_FILE) as f:
            return json.load(f)
    except:
        return []


def _save_live(data):
    with open(LIVE_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ============================================================
# PHASE 3: VALIDATION + DECISION
# ============================================================

def run_live_validation(cfg, windows_history, num_windows=3):
    """Run N live windows and validate the strategy."""
    print("\n" + "=" * 65)
    print("  PHASE 3: LIVE VALIDATION (%d windows)" % num_windows)
    print("  Direction: %s | SL=%s | TP=%s" % (
        cfg.get("direction_mode"),
        "OFF" if not cfg.get("use_stop_loss") else "$%.2f" % cfg["stop_loss"],
        "OFF" if not cfg.get("use_take_profit") else "$%.2f" % cfg["take_profit"],
    ))
    print("=" * 65)

    results = []
    for i in range(num_windows):
        log("=== Window %d/%d ===" % (i + 1, num_windows))
        result = run_live_window(cfg, windows_history)
        if result and not result.get("skipped"):
            results.append(result)
        elif result and result.get("skipped"):
            log("Skipped - trying next window")
            # Don't count skips against the target
            num_windows += 1  # bad practice in loop but bounded
            if num_windows > 10:
                break
        time.sleep(5)

    # Summary
    if not results:
        print("\n  No valid trades executed!")
        return results, 0

    wins = sum(1 for r in results if r["win"])
    total_pnl = sum(r["pnl_usd"] for r in results)
    wr = wins / len(results) * 100

    print("\n  " + "=" * 50)
    print("  LIVE VALIDATION SUMMARY")
    print("  " + "=" * 50)
    print("  Trades:    %d (%dW / %dL)" % (len(results), wins, len(results) - wins))
    print("  Win Rate:  %.1f%%" % wr)
    print("  Total PnL: $%+.4f" % total_pnl)
    for r in results:
        icon = "W" if r["win"] else "L"
        print("    [%s] %s @ $%.3f -> $%.3f | %s | $%+.4f" % (
            icon, r["direction"], r["entry_price"], r["exit_price"],
            r["exit_reason"], r["pnl_usd"]))
    print("  " + "=" * 50)

    return results, total_pnl


# ============================================================
# MAIN LOOP
# ============================================================

def save_experiment(exp_num, cfg, backtest_stats, live_results, live_pnl, decision):
    """Save experiment to history/."""
    SYSTEM_HISTORY_DIR.mkdir(exist_ok=True)
    exp = {
        "experiment": exp_num,
        "timestamp": datetime.now().isoformat(),
        "system": "polymarket_only",
        "config": {k: v for k, v in cfg.items()},
        "backtest": backtest_stats,
        "live_trades": len(live_results),
        "live_pnl_usd": round(live_pnl, 4),
        "live_wins": sum(1 for r in live_results if r["win"]),
        "score": round(live_pnl, 4),
        "decision": decision,
    }
    path = SYSTEM_HISTORY_DIR / ("exp_%03d.json" % exp_num)
    with open(path, "w") as f:
        json.dump(exp, f, indent=2)
    log("Experiment saved to %s" % path)
    return exp


def get_next_exp_num():
    """Get next experiment number from history/."""
    SYSTEM_HISTORY_DIR.mkdir(exist_ok=True)
    existing = list(SYSTEM_HISTORY_DIR.glob("exp_*.json"))
    if not existing:
        return 1
    nums = []
    for f in existing:
        try:
            nums.append(int(f.stem.split("_")[1]))
        except:
            pass
    return max(nums) + 1 if nums else 1


def get_best_pnl():
    """Get best live PnL from history."""
    SYSTEM_HISTORY_DIR.mkdir(exist_ok=True)
    best = -9999
    for f in SYSTEM_HISTORY_DIR.glob("exp_*.json"):
        try:
            with open(f) as fp:
                exp = json.load(fp)
                if exp.get("decision") == "KEEP":
                    pnl = exp.get("score", exp.get("live_pnl_usd", -9999))
                    best = max(best, pnl)
        except:
            pass
    return best if best > -9999 else 0


def run_full_cycle():
    """Run one complete research cycle: collect → optimize → validate."""
    exp_num = get_next_exp_num()
    best_pnl = get_best_pnl()

    print("\n" + "#" * 65)
    print("  POLYMARKET-ONLY TRADING SYSTEM")
    print("  Experiment #%d | Best PnL: $%+.4f" % (exp_num, best_pnl))
    print("  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("#" * 65)

    # Phase 1: Collect data
    windows = collect_historical_data()

    # Phase 2: Optimize
    best_cfg, best_trades, best_bt_pnl = auto_optimize_polymarket(windows)
    if best_cfg is None:
        print("\n  ABORT: Could not find valid configuration.")
        return

    bt_stats = print_backtest_results(best_trades, best_cfg, "BACKTEST")

    # Phase 3: Live validation
    live_results, live_pnl = run_live_validation(
        best_cfg, windows, CONFIG["live_windows"])

    # Decision: KEEP if live PnL > 0 AND > best
    if live_pnl > 0 and live_pnl > best_pnl:
        decision = "KEEP"
        print("\n  >>> KEEP: Live PnL $%+.4f > best $%+.4f <<<" % (
            live_pnl, best_pnl))
    else:
        decision = "DISCARD"
        print("\n  >>> DISCARD: Live PnL $%+.4f (best=$%+.4f) <<<" % (
            live_pnl, best_pnl))

    # Save experiment
    save_experiment(exp_num, best_cfg, bt_stats, live_results, live_pnl, decision)

    return decision, live_pnl


def run_forever():
    """Autonomous loop: keep running cycles forever."""
    print("\n  Starting autonomous research loop...")
    print("  Press Ctrl+C to stop\n")

    cycle = 0
    try:
        while True:
            cycle += 1
            print("\n\n" + "=" * 65)
            print("  CYCLE %d" % cycle)
            print("=" * 65)

            result = run_full_cycle()
            if result:
                decision, pnl = result
                log("Cycle %d complete: %s (PnL=$%+.4f)" % (cycle, decision, pnl))

            # Brief pause between cycles
            log("Next cycle in 30 seconds...")
            time.sleep(30)

    except KeyboardInterrupt:
        print("\n\n  Loop stopped by user.")
        print("  Check history/ for all experiment results.")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "--collect":
            collect_historical_data()
        elif cmd == "--optimize":
            windows = load_history()
            if windows:
                best_cfg, best_trades, best_pnl = auto_optimize_polymarket(windows)
                if best_cfg:
                    print_backtest_results(best_trades, best_cfg, "BEST")
            else:
                print("No history data. Run --collect first.")
        elif cmd == "--live":
            n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
            windows = load_history()
            best_cfg, best_trades, _ = auto_optimize_polymarket(windows)
            if best_cfg:
                run_live_validation(best_cfg, windows, n)
        elif cmd == "--loop":
            run_forever()
        else:
            print("Usage: polymarket_system.py [--collect|--optimize|--live N|--loop]")
    else:
        # Default: one full cycle
        run_full_cycle()
