"""
Live Validator: Run multiple configs simultaneously on live windows.
Each config makes its own direction decision but uses same real-time prices.
Tracks results per-config, saves to validation_results.json.
dry_mode = True always.
"""
import time
import json
import math
import requests
from datetime import datetime

BINANCE_BASE = "https://api.binance.com/api/v3"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

RESULTS_FILE = "validation_results.json"

# 5 configs from optimizer walk-forward results
CONFIGS = [
    {"name": "Config1", "sl": 0.45, "tp": None,  "dir": "always_up",  "flip": False, "label": "SL=$0.45 TP=OFF always_up"},
    {"name": "Config2", "sl": 0.45, "tp": None,  "dir": "momentum",   "flip": False, "label": "SL=$0.45 TP=OFF momentum"},
    {"name": "Config3", "sl": 0.45, "tp": None,  "dir": "momentum",   "flip": True,  "label": "SL=$0.45 TP=OFF momentum+flip"},
    {"name": "Config4", "sl": 0.45, "tp": None,  "dir": "always_down", "flip": False, "label": "SL=$0.45 TP=OFF always_down"},
    {"name": "Config5", "sl": 0.45, "tp": 0.80,  "dir": "momentum",   "flip": True,  "label": "SL=$0.45 TP=$0.80 momentum+flip"},
]

TRADE_SIZE = 5.0
ENTRY_PRICE = 0.50
WINDOW_MINUTES = 15


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print("  [%s] %s" % (ts, msg))


# ============================================================
# API FUNCTIONS (reused from window_trader.py)
# ============================================================

def get_btc_price():
    r = requests.get("%s/ticker/price" % BINANCE_BASE,
                     params={"symbol": "BTCUSDT"}, timeout=5)
    return float(r.json()["price"])


def get_btc_momentum(lookback=10):
    r = requests.get("%s/klines" % BINANCE_BASE,
                     params={"symbol": "BTCUSDT", "interval": "1m",
                             "limit": lookback + 1}, timeout=5)
    klines = r.json()
    closes = [float(k[4]) for k in klines]
    if len(closes) < 2:
        return "UP", 0.0
    move = closes[-1] - closes[0]
    move_pct = move / closes[0] * 100
    direction = "UP" if move >= 0 else "DOWN"
    return direction, move_pct


def get_market_by_slug(slug):
    try:
        r = requests.get("%s/markets/slug/%s" % (GAMMA_BASE, slug), timeout=10)
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
            "amm_prices": float_prices,
            "closed": m.get("closed", True),
        }
    except Exception:
        return None


def find_next_market():
    now_ts = int(time.time())
    for offset in range(0, 4):
        block = ((now_ts // 900) + offset) * 900
        slug = "btc-updown-15m-%d" % block
        m = get_market_by_slug(slug)
        if m and not m["closed"]:
            m["block_start"] = block
            m["block_end"] = block + 900
            m["secs_to_start"] = block - now_ts
            return m
    return None


def get_midpoint_prices(token_ids):
    try:
        r1 = requests.get("%s/midpoint" % CLOB_BASE,
                         params={"token_id": token_ids[0]}, timeout=5)
        r2 = requests.get("%s/midpoint" % CLOB_BASE,
                         params={"token_id": token_ids[1]}, timeout=5)
        if r1.status_code == 200 and r2.status_code == 200:
            up = float(r1.json().get("mid", 0))
            down = float(r2.json().get("mid", 0))
            if up > 0 and down > 0:
                return up, down
    except Exception:
        pass
    return None, None


def get_live_prices(market):
    if market.get("token_ids") and len(market["token_ids"]) >= 2:
        up, down = get_midpoint_prices(market["token_ids"])
        if up is not None:
            return up, down, "CLOB_midpoint"
    if market.get("amm_prices") and len(market["amm_prices"]) >= 2:
        return market["amm_prices"][0], market["amm_prices"][1], "Gamma_AMM"
    return None, None, "none"


# ============================================================
# DIRECTION DECISION PER CONFIG
# ============================================================

def decide_direction(config, momentum_dir):
    """Determine trade direction based on config."""
    d = config["dir"]
    if d == "always_up":
        direction = "UP"
    elif d == "always_down":
        direction = "DOWN"
    elif d == "momentum":
        direction = momentum_dir
    else:
        direction = "UP"

    if config["flip"]:
        direction = "DOWN" if direction == "UP" else "UP"

    return direction


# ============================================================
# RESULTS I/O
# ============================================================

def load_results():
    try:
        with open(RESULTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"configs": [], "windows": [], "status": "idle"}


def save_results(data):
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# ============================================================
# MAIN VALIDATION LOOP
# ============================================================

def wait_for_window():
    """Wait for next 15-min window start."""
    now_ts = int(time.time())
    current_block = (now_ts // 900) * 900
    next_block = current_block + 900
    wait_secs = next_block - now_ts

    next_time = datetime.fromtimestamp(next_block)
    log("Sonraki pencere: %s (%ddk %dsn)" % (
        next_time.strftime("%H:%M:%S"), wait_secs // 60, wait_secs % 60))

    # Wait with periodic updates
    waited = 0
    while waited < max(0, wait_secs - 3):
        sleep_chunk = min(30, wait_secs - 3 - waited)
        if sleep_chunk <= 0:
            break
        time.sleep(sleep_chunk)
        waited += sleep_chunk
        remaining = wait_secs - 3 - waited
        if remaining > 30:
            log("  Kalan: %ddk %dsn" % (remaining // 60, remaining % 60))

    # Final wait
    now_ts = int(time.time())
    final_wait = max(0, next_block - now_ts)
    if final_wait > 0:
        time.sleep(final_wait)

    return next_block


def run_window_for_all_configs(block_ts, results_data):
    """Run a single window for all 5 configs simultaneously."""
    window_num = len(results_data["windows"]) + 1

    print("\n" + "=" * 70)
    print("  LIVE VALIDATION - Window #%d" % window_num)
    print("  %s | block_ts=%d" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), block_ts))
    print("=" * 70)

    # 1. Get BTC momentum (shared across all configs)
    log("BTC momentum kontrol...")
    btc_entry = get_btc_price()
    momentum_dir, mom_pct = get_btc_momentum(10)
    log("BTC: $%s | Momentum: %+.4f%% -> %s" % (
        "{:,.2f}".format(btc_entry), mom_pct, momentum_dir))

    # 2. Find market
    log("Market araniyor...")
    market = find_next_market()
    if not market:
        log("HATA: Market bulunamadi! Skip.")
        window_result = {
            "window": window_num,
            "block_ts": block_ts,
            "timestamp": datetime.now().isoformat(),
            "btc_entry": round(btc_entry, 2),
            "momentum": momentum_dir,
            "momentum_pct": round(mom_pct, 4),
            "status": "no_market",
            "trades": {},
        }
        results_data["windows"].append(window_result)
        save_results(results_data)
        return

    log("Market: %s" % market["question"])

    # 3. Get initial prices
    up_price, down_price, price_src = get_live_prices(market)
    if up_price is None:
        log("HATA: Fiyat alinamadi!")
        window_result = {
            "window": window_num,
            "block_ts": block_ts,
            "timestamp": datetime.now().isoformat(),
            "btc_entry": round(btc_entry, 2),
            "momentum": momentum_dir,
            "momentum_pct": round(mom_pct, 4),
            "status": "no_price",
            "trades": {},
        }
        results_data["windows"].append(window_result)
        save_results(results_data)
        return

    log("Fiyatlar (%s): UP=$%.3f DOWN=$%.3f" % (price_src, up_price, down_price))

    # 4. Determine direction for each config
    config_trades = {}
    for cfg in CONFIGS:
        direction = decide_direction(cfg, momentum_dir)
        entry_price = up_price if direction == "UP" else down_price
        tokens = TRADE_SIZE / entry_price if entry_price > 0 else 0

        config_trades[cfg["name"]] = {
            "direction": direction,
            "entry_price": round(entry_price, 4),
            "tokens": round(tokens, 4),
            "sl": cfg["sl"],
            "tp": cfg["tp"],
            "exit_price": None,
            "exit_reason": None,
            "pnl": None,
            "exited": False,
        }
        log("  %s: %s token @ $%.3f (SL=$%.2f TP=%s)" % (
            cfg["label"], direction, entry_price,
            cfg["sl"], "$%.2f" % cfg["tp"] if cfg["tp"] else "OFF"))

    # 5. Monitor position for 15 minutes
    monitor_secs = WINDOW_MINUTES * 60
    log("Pozisyon izleniyor (%d dk)..." % WINDOW_MINUTES)

    print("\n  %4s | %12s | %8s | %8s | " % ("Sec", "BTC", "UP", "DOWN") +
          " | ".join("%-8s" % c["name"] for c in CONFIGS))
    print("  " + "-" * (40 + len(CONFIGS) * 11))

    last_print = 0

    for tick in range(1, monitor_secs + 1):
        time.sleep(1)

        try:
            cur_up, cur_down, cur_src = get_live_prices(market)
            if cur_up is None:
                continue

            # Check SL/TP for each config
            for cfg in CONFIGS:
                ct = config_trades[cfg["name"]]
                if ct["exited"]:
                    continue

                cur_token = cur_up if ct["direction"] == "UP" else cur_down

                # SL check
                if ct["sl"] is not None and cur_token <= ct["sl"]:
                    ct["exit_price"] = round(cur_token, 4)
                    ct["exit_reason"] = "stop_loss"
                    ct["pnl"] = round(ct["tokens"] * cur_token - TRADE_SIZE, 4)
                    ct["exited"] = True

                # TP check
                if ct["tp"] is not None and cur_token >= ct["tp"]:
                    ct["exit_price"] = round(cur_token, 4)
                    ct["exit_reason"] = "take_profit"
                    ct["pnl"] = round(ct["tokens"] * cur_token - TRADE_SIZE, 4)
                    ct["exited"] = True

            # Print every 30 seconds
            if tick - last_print >= 30:
                cur_btc = get_btc_price()
                statuses = []
                for cfg in CONFIGS:
                    ct = config_trades[cfg["name"]]
                    if ct["exited"]:
                        statuses.append("%-8s" % ct["exit_reason"][:8])
                    else:
                        cur_token = cur_up if ct["direction"] == "UP" else cur_down
                        pnl = ct["tokens"] * cur_token - TRADE_SIZE
                        statuses.append("$%+.2f  " % pnl)

                print("  %4d | $%10s | $%.4f | $%.4f | %s" % (
                    tick, "{:,.2f}".format(cur_btc), cur_up, cur_down,
                    " | ".join(statuses)))
                last_print = tick

        except Exception as e:
            if tick % 60 == 0:
                log("Tick %d hata: %s" % (tick, e))

    # 6. Expiry - resolve remaining configs
    log("EXPIRY - Pencere kapandi")
    try:
        final_btc = get_btc_price()
        btc_move = (final_btc - btc_entry) / btc_entry * 100
        actual_dir = "UP" if final_btc >= btc_entry else "DOWN"
        log("BTC: $%s -> $%s (%+.3f%%) = %s" % (
            "{:,.2f}".format(btc_entry), "{:,.2f}".format(final_btc),
            btc_move, actual_dir))
    except:
        actual_dir = momentum_dir
        final_btc = btc_entry

    for cfg in CONFIGS:
        ct = config_trades[cfg["name"]]
        if not ct["exited"]:
            # Resolve at expiry
            if ct["direction"] == actual_dir:
                ct["exit_price"] = 1.00
                ct["exit_reason"] = "expiry_win"
            else:
                ct["exit_price"] = 0.00
                ct["exit_reason"] = "expiry_loss"
            ct["pnl"] = round(ct["tokens"] * ct["exit_price"] - TRADE_SIZE, 4)
            ct["exited"] = True

    # 7. Print results
    print("\n  " + "=" * 70)
    print("  WINDOW #%d RESULTS" % window_num)
    print("  " + "-" * 70)
    print("  %-35s | %8s | %10s | %8s" % ("Config", "Dir", "Exit", "PnL"))
    print("  " + "-" * 70)

    for cfg in CONFIGS:
        ct = config_trades[cfg["name"]]
        print("  %-35s | %8s | %10s | $%+.2f" % (
            cfg["label"], ct["direction"], ct["exit_reason"], ct["pnl"]))

    print("  " + "=" * 70)

    # 8. Save window result
    window_result = {
        "window": window_num,
        "block_ts": block_ts,
        "timestamp": datetime.now().isoformat(),
        "btc_entry": round(btc_entry, 2),
        "btc_exit": round(final_btc, 2),
        "actual_direction": actual_dir,
        "momentum": momentum_dir,
        "momentum_pct": round(mom_pct, 4),
        "market": market["question"],
        "slug": market["slug"],
        "price_source": price_src,
        "status": "completed",
        "trades": config_trades,
    }
    results_data["windows"].append(window_result)

    # Update cumulative stats per config
    for cfg in CONFIGS:
        cfg_name = cfg["name"]
        # Find or create config summary
        cfg_summary = None
        for cs in results_data["configs"]:
            if cs["name"] == cfg_name:
                cfg_summary = cs
                break
        if cfg_summary is None:
            cfg_summary = {
                "name": cfg_name,
                "label": cfg["label"],
                "sl": cfg["sl"],
                "tp": cfg["tp"],
                "dir": cfg["dir"],
                "flip": cfg["flip"],
                "total_trades": 0,
                "wins": 0,
                "total_pnl": 0,
                "pnl_history": [],
            }
            results_data["configs"].append(cfg_summary)

        ct = config_trades[cfg_name]
        cfg_summary["total_trades"] += 1
        if ct["pnl"] > 0:
            cfg_summary["wins"] += 1
        cfg_summary["total_pnl"] = round(cfg_summary["total_pnl"] + ct["pnl"], 4)
        cfg_summary["pnl_history"].append(ct["pnl"])
        cfg_summary["wr"] = round(cfg_summary["wins"] / cfg_summary["total_trades"] * 100, 1)

    results_data["status"] = "running"
    results_data["last_update"] = datetime.now().isoformat()
    save_results(results_data)
    log("Sonuclar kaydedildi -> %s" % RESULTS_FILE)


def run_validation(num_windows=5, resume=False):
    """Run validation for N windows."""
    print("\n" + "#" * 70)
    print("  LIVE MULTI-CONFIG VALIDATOR")
    print("  %d configs x %d windows = %d total trades" % (
        len(CONFIGS), num_windows, len(CONFIGS) * num_windows))
    print("  dry_mode = True (ALWAYS)")
    print("#" * 70)

    for cfg in CONFIGS:
        print("  - %s" % cfg["label"])
    print()

    # Initialize or RESUME existing results
    existing = load_results()
    if existing.get("configs") and len(existing.get("windows", [])) > 0 and resume:
        results_data = existing
        results_data["target_windows"] = num_windows
        done = len(results_data["windows"])
        print("  RESUMING from window %d (existing %d windows)" % (done + 1, done))
        print("  Current standings:")
        for cs in sorted(results_data["configs"], key=lambda c: c["total_pnl"], reverse=True):
            print("    %s: $%+.2f (%dW/%dL)" % (cs["label"], cs["total_pnl"], cs["wins"], cs["total_trades"] - cs["wins"]))
        print()
    else:
        results_data = {
            "configs": [],
            "windows": [],
            "status": "starting",
            "started": datetime.now().isoformat(),
            "target_windows": num_windows,
            "last_update": datetime.now().isoformat(),
        }
        done = 0
        save_results(results_data)

    for i in range(done, num_windows):
        print("\n>>> Window %d/%d bekliyor..." % (i + 1, num_windows))
        block_ts = wait_for_window()
        run_window_for_all_configs(block_ts, results_data)

        # Print cumulative standings
        print("\n  === CUMULATIVE STANDINGS ===")
        print("  %-35s | %5s | %5s | %8s" % ("Config", "W/L", "WR%", "PnL"))
        print("  " + "-" * 60)

        sorted_configs = sorted(results_data["configs"],
                                key=lambda c: c["total_pnl"], reverse=True)
        for j, cs in enumerate(sorted_configs):
            marker = " <-- LEADER" if j == 0 else ""
            print("  %-35s | %d/%d   | %5.1f | $%+.2f%s" % (
                cs["label"],
                cs["wins"], cs["total_trades"] - cs["wins"],
                cs["wr"], cs["total_pnl"], marker))

        time.sleep(5)

    # Final results
    results_data["status"] = "completed"
    results_data["completed"] = datetime.now().isoformat()
    save_results(results_data)

    print("\n" + "=" * 70)
    print("  VALIDATION COMPLETE - %d windows" % num_windows)
    print("=" * 70)

    sorted_configs = sorted(results_data["configs"],
                            key=lambda c: c["total_pnl"], reverse=True)
    winner = sorted_configs[0]
    print("\n  WINNER: %s" % winner["label"])
    print("  PnL: $%+.2f | WR: %.1f%% | %dW/%dL" % (
        winner["total_pnl"], winner["wr"],
        winner["wins"], winner["total_trades"] - winner["wins"]))
    print("\n  Sonuclar: %s" % RESULTS_FILE)


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    do_resume = "--resume" in sys.argv
    run_validation(n, resume=do_resume)
