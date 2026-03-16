"""
V3 SL Sweet Spot Test: Compare SL=$0.40 vs SL=$0.35 across direction modes.
Runs multiple configs simultaneously on live 15-min windows.
dry_mode = True always.

CRITICAL: Pre-window entry at T-2s BEFORE window opens!
"""
import time
import json
import requests
import sys
import functools
from datetime import datetime

# Force unbuffered output
print = functools.partial(print, flush=True)

BINANCE_BASE = "https://api.binance.com/api/v3"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

RESULTS_FILE = "v3_results.json"

# V3 SL Sweet Spot Configs
CONFIGS = [
    {"name": "Config1", "sl": 0.40, "tp": None, "dir": "momentum",   "flip": False, "label": "C1: momentum SL=$0.40"},
    {"name": "Config2", "sl": 0.35, "tp": None, "dir": "momentum",   "flip": False, "label": "C2: momentum SL=$0.35"},
    {"name": "Config3", "sl": 0.40, "tp": None, "dir": "always_up",  "flip": False, "label": "C3: always_up SL=$0.40"},
    {"name": "Config4", "sl": 0.35, "tp": None, "dir": "always_up",  "flip": False, "label": "C4: always_up SL=$0.35"},
    {"name": "Config5", "sl": 0.40, "tp": None, "dir": "always_down","flip": False, "label": "C5: always_down SL=$0.40"},
]

TRADE_SIZE = 5.0
WINDOW_MINUTES = 15


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print("  [%s] %s" % (ts, msg), flush=True)


# ============================================================
# API FUNCTIONS
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


def find_market_for_block(block_ts):
    """Find market for a specific block timestamp."""
    slug = "btc-updown-15m-%d" % block_ts
    m = get_market_by_slug(slug)
    if m and not m["closed"]:
        m["block_start"] = block_ts
        m["block_end"] = block_ts + 900
        return m
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
# DIRECTION DECISION
# ============================================================

def decide_direction(config, momentum_dir):
    d = config["dir"]
    if d == "always_up":
        return "UP"
    elif d == "always_down":
        return "DOWN"
    elif d == "momentum":
        return momentum_dir
    return "UP"


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
        f.flush()
    log("  [SAVE] v3_results.json guncellendi")


# ============================================================
# PRE-WINDOW ENTRY SEQUENCE
# ============================================================

def pre_window_entry_sequence(next_block_ts):
    """
    CRITICAL PRE-WINDOW ENTRY SEQUENCE:
    T-30s: Check BTC momentum, decide direction
    T-10s: Fetch token IDs for next window via Gamma API
    T-2s:  Fire order immediately (catch ~$0.50 price!)
    T+0s:  Window opens, position already active

    Returns: (market, momentum_dir, mom_pct, btc_entry, entry_prices, entry_time)
    """
    now_ts = int(time.time())
    secs_to_open = next_block_ts - now_ts

    print("\n  " + "=" * 60)
    print("  PRE-WINDOW ENTRY SEQUENCE")
    print("  Window opens at: %s (in %ds)" % (
        datetime.fromtimestamp(next_block_ts).strftime("%H:%M:%S"), secs_to_open))
    print("  " + "=" * 60)

    market = None
    momentum_dir = "UP"
    mom_pct = 0.0
    btc_entry = 0.0

    # ---- T-30s: Check BTC momentum ----
    wait_until = next_block_ts - 30
    now_ts = int(time.time())
    if now_ts < wait_until:
        sleep_time = wait_until - now_ts
        log("T-30s'e kadar bekleniyor... (%ds)" % sleep_time)
        # Wait in chunks, showing countdown
        while True:
            now_ts = int(time.time())
            remaining = wait_until - now_ts
            if remaining <= 0:
                break
            chunk = min(30, remaining)
            time.sleep(chunk)
            remaining = wait_until - int(time.time())
            if remaining > 5:
                log("  T-30s'e kalan: %ds" % remaining)

    log("T-30s: BTC momentum kontrol ediliyor...")
    btc_entry = get_btc_price()
    momentum_dir, mom_pct = get_btc_momentum(10)
    log("T-30s: BTC=$%s | Momentum: %s (%+.4f%%)" % (
        "{:,.2f}".format(btc_entry), momentum_dir, mom_pct))

    # ---- T-10s: Fetch token IDs ----
    wait_until = next_block_ts - 10
    now_ts = int(time.time())
    if now_ts < wait_until:
        sleep_time = wait_until - now_ts
        log("T-10s'e kadar bekleniyor... (%ds)" % sleep_time)
        time.sleep(sleep_time)

    log("T-10s: Market ve token ID'leri aliniyor...")
    market = find_market_for_block(next_block_ts)
    if not market:
        # Try next block
        market = find_market_for_block(next_block_ts + 900)
    if not market:
        market = find_next_market()

    if market:
        log("T-10s: Market bulundu: %s" % market["question"])
        if market.get("token_ids"):
            log("T-10s: Token IDs: UP=%s... DOWN=%s..." % (
                market["token_ids"][0][:20] if len(market["token_ids"]) > 0 else "?",
                market["token_ids"][1][:20] if len(market["token_ids"]) > 1 else "?"))
    else:
        log("T-10s: UYARI - Market bulunamadi, window acilinca tekrar denenecek")

    # ---- T-2s: FIRE ORDER ----
    wait_until = next_block_ts - 2
    now_ts = int(time.time())
    if now_ts < wait_until:
        sleep_time = wait_until - now_ts
        log("T-2s'e kadar bekleniyor... (%ds)" % sleep_time)
        time.sleep(sleep_time)

    log("T-2s: >>> ORDER FIRE! <<<")

    # Get fresh prices right before entry
    up_price, down_price = None, None
    entry_time = datetime.now()

    if market:
        up_price, down_price, price_src = get_live_prices(market)
        if up_price is not None:
            log("T-2s: Giris fiyatlari (%s): UP=$%.4f DOWN=$%.4f" % (
                price_src, up_price, down_price))

            # Check pre-window price threshold
            if up_price > 0.55 and down_price > 0.55:
                log("T-2s: UYARI - Fiyatlar cok yuksek (>$0.55), SKIP!")
                return market, momentum_dir, mom_pct, btc_entry, None, entry_time
        else:
            log("T-2s: Fiyat alinamadi, window acilinca tekrar denenecek")

    # Update BTC price one more time
    try:
        btc_entry = get_btc_price()
    except:
        pass

    entry_prices = {
        "up": up_price if up_price else 0.50,
        "down": down_price if down_price else 0.50,
        "source": price_src if up_price else "estimated",
        "time": entry_time.isoformat(),
    }

    log("T-2s: Pre-window entry @ UP=$%.4f DOWN=$%.4f (T-%ds before open)" % (
        entry_prices["up"], entry_prices["down"],
        max(0, next_block_ts - int(time.time()))))

    return market, momentum_dir, mom_pct, btc_entry, entry_prices, entry_time


# ============================================================
# MAIN WINDOW EXECUTION WITH DETAILED LOGGING
# ============================================================

def wait_for_window():
    """Wait for next window, returning block_ts.
    Uses pre-window entry: arrives 30s early.
    If we're within first 120s of current window, enter it directly.
    """
    now_ts = int(time.time())
    current_block = (now_ts // 900) * 900
    elapsed_in_current = now_ts - current_block
    next_block = current_block + 900

    # If we're within first 2 minutes of current window, use it (no pre-entry possible)
    if elapsed_in_current <= 120:
        log("Mevcut pencereye giriliyor! (gecen: %dsn)" % elapsed_in_current)
        return current_block, False  # False = no pre-entry possible

    # Otherwise wait for next window with pre-entry
    wait_secs = next_block - now_ts
    next_time = datetime.fromtimestamp(next_block)
    log("Sonraki pencere: %s (%ddk %dsn)" % (
        next_time.strftime("%H:%M:%S"), wait_secs // 60, wait_secs % 60))

    # Wait until T-35s before next window (give 5s buffer before T-30 sequence)
    target_arrival = next_block - 35
    now_ts = int(time.time())
    wait_to_arrival = max(0, target_arrival - now_ts)

    if wait_to_arrival > 0:
        log("Pre-window beklemesi basladi (T-35s'e %ds)" % wait_to_arrival)
        waited = 0
        while waited < wait_to_arrival:
            sleep_chunk = min(30, wait_to_arrival - waited)
            if sleep_chunk <= 0:
                break
            time.sleep(sleep_chunk)
            waited += sleep_chunk
            remaining = wait_to_arrival - waited
            if remaining > 30:
                log("  Pre-window'a kalan: %ddk %dsn" % (remaining // 60, remaining % 60))

    return next_block, True  # True = pre-entry sequence will run


def run_window_for_all_configs(block_ts, results_data, pre_entry=True):
    window_num = len(results_data["windows"]) + 1

    print("\n" + "=" * 70)
    print("  V3 SL SWEET SPOT TEST - Window #%d" % window_num)
    print("  %s | block_ts=%d" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), block_ts))
    print("=" * 70)

    market = None
    momentum_dir = "UP"
    mom_pct = 0.0
    btc_entry = 0.0
    entry_prices = None
    entry_time = datetime.now()

    # ============ PRE-WINDOW ENTRY or DIRECT ENTRY ============
    if pre_entry:
        market, momentum_dir, mom_pct, btc_entry, entry_prices, entry_time = \
            pre_window_entry_sequence(block_ts)

        if entry_prices is None:
            log("Pre-window entry basarisiz, window acilinca girilecek")
            # Wait for window to open
            now_ts = int(time.time())
            wait = max(0, block_ts - now_ts)
            if wait > 0:
                time.sleep(wait)
    else:
        # Direct entry (already in window)
        log("DIRECT ENTRY - Pencere zaten acik")
        btc_entry = get_btc_price()
        momentum_dir, mom_pct = get_btc_momentum(10)
        log("BTC: $%s | Momentum: %+.4f%% -> %s" % (
            "{:,.2f}".format(btc_entry), mom_pct, momentum_dir))

    # If we don't have market yet, find it now
    if not market:
        log("Market araniyor...")
        market = find_next_market()
        if not market:
            log("HATA: Market bulunamadi! Skip.")
            window_result = {
                "window": window_num, "block_ts": block_ts,
                "timestamp": datetime.now().isoformat(),
                "btc_entry": round(btc_entry, 2),
                "momentum": momentum_dir, "momentum_pct": round(mom_pct, 4),
                "status": "no_market", "trades": {},
            }
            results_data["windows"].append(window_result)
            save_results(results_data)
            return

    log("Market: %s" % market["question"])

    # If we don't have entry prices yet, get them now
    if not entry_prices:
        up_price, down_price, price_src = get_live_prices(market)
        if up_price is None:
            log("HATA: Fiyat alinamadi!")
            window_result = {
                "window": window_num, "block_ts": block_ts,
                "timestamp": datetime.now().isoformat(),
                "btc_entry": round(btc_entry, 2),
                "momentum": momentum_dir, "momentum_pct": round(mom_pct, 4),
                "status": "no_price", "trades": {},
            }
            results_data["windows"].append(window_result)
            save_results(results_data)
            return
        entry_prices = {
            "up": up_price, "down": down_price,
            "source": price_src, "time": datetime.now().isoformat(),
        }
        entry_time = datetime.now()
        log("Giris fiyatlari (%s): UP=$%.4f DOWN=$%.4f" % (price_src, up_price, down_price))

    up_price = entry_prices["up"]
    down_price = entry_prices["down"]
    price_src = entry_prices["source"]

    # ============ SETUP TRADES FOR EACH CONFIG ============
    config_trades = {}
    print("\n  === TRADE ENTRIES ===")
    for cfg in CONFIGS:
        direction = decide_direction(cfg, momentum_dir)
        e_price = up_price if direction == "UP" else down_price
        tokens = TRADE_SIZE / e_price if e_price > 0 else 0

        config_trades[cfg["name"]] = {
            "direction": direction,
            "entry_price": round(e_price, 4),
            "entry_time": entry_time.isoformat(),
            "tokens": round(tokens, 4),
            "sl": cfg["sl"],
            "tp": cfg["tp"],
            "exit_price": None,
            "exit_reason": None,
            "exit_time": None,
            "pnl": None,
            "exited": False,
            "price_log": [],  # detailed price every 2 min
            "would_have_won": None,  # if SL hit, what would expiry have been
        }
        log("  %s: %s @ $%.4f | %d tokens | SL=$%.2f" % (
            cfg["label"], direction, e_price, tokens, cfg["sl"]))

    # ============ MONITOR UNTIL WINDOW END ============
    now_ts = int(time.time())
    window_end = block_ts + 900
    monitor_secs = max(60, window_end - now_ts)
    log("\nPozisyon izleniyor (%dsn / pencere sonu %s)..." % (
        monitor_secs, datetime.fromtimestamp(window_end).strftime("%H:%M:%S")))

    # Create in-progress window result for continuous saving
    live_window = {
        "window": window_num,
        "block_ts": block_ts,
        "timestamp": datetime.now().isoformat(),
        "btc_entry": round(btc_entry, 2),
        "btc_exit": 0,
        "actual_direction": "PENDING",
        "momentum": momentum_dir,
        "momentum_pct": round(mom_pct, 4),
        "market": market["question"] if market else "unknown",
        "slug": market["slug"] if market else "unknown",
        "price_source": price_src,
        "entry_time": entry_time.isoformat(),
        "pre_window_entry": False,
        "secs_before_open": 0,
        "status": "monitoring",
        "trades": config_trades,
    }
    # Add to results_data immediately so continuous saves include it
    results_data["windows"].append(live_window)
    results_data["last_update"] = datetime.now().isoformat()
    save_results(results_data)

    print("\n  %4s | %12s | %8s | %8s | " % ("Sec", "BTC", "UP", "DOWN") +
          " | ".join("%-12s" % c["name"] for c in CONFIGS))
    print("  " + "-" * (45 + len(CONFIGS) * 15))

    last_print = 0
    last_2min_log = 0  # for 2-minute detailed logging
    last_save = 0  # for 30s backup saves
    last_prices = {"up": up_price, "down": down_price}  # track last known prices
    need_save = False  # flag for event-driven saves

    for tick in range(1, monitor_secs + 1):
        time.sleep(1)

        try:
            cur_up, cur_down, cur_src = get_live_prices(market)
            if cur_up is None:
                continue

            last_prices = {"up": cur_up, "down": cur_down}

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
                    ct["exit_time"] = datetime.now().isoformat()
                    ct["pnl"] = round(ct["tokens"] * cur_token - TRADE_SIZE, 4)
                    ct["exited"] = True
                    log("  >>> %s STOP LOSS HIT @ $%.4f | PnL: $%+.2f | %ds into window" % (
                        cfg["label"], cur_token, ct["pnl"], tick))
                    need_save = True  # SAVE IMMEDIATELY on SL

                # TP check
                if ct["tp"] is not None and cur_token >= ct["tp"]:
                    ct["exit_price"] = round(cur_token, 4)
                    ct["exit_reason"] = "take_profit"
                    ct["exit_time"] = datetime.now().isoformat()
                    ct["pnl"] = round(ct["tokens"] * cur_token - TRADE_SIZE, 4)
                    ct["exited"] = True
                    log("  >>> %s TAKE PROFIT @ $%.4f | PnL: $%+.2f" % (
                        cfg["label"], cur_token, ct["pnl"]))
                    need_save = True  # SAVE IMMEDIATELY on TP

            # ---- CONTINUOUS SAVE: after SL/TP events or every 30s ----
            if need_save or (tick - last_save >= 30):
                results_data["last_update"] = datetime.now().isoformat()
                save_results(results_data)
                last_save = tick
                need_save = False

            # ---- Detailed price log every 2 minutes ----
            if tick - last_2min_log >= 120 or tick == 1:
                for cfg in CONFIGS:
                    ct = config_trades[cfg["name"]]
                    cur_token = cur_up if ct["direction"] == "UP" else cur_down
                    ct["price_log"].append({
                        "tick": tick,
                        "time": datetime.now().isoformat(),
                        "token_price": round(cur_token, 4),
                        "up_price": round(cur_up, 4),
                        "down_price": round(cur_down, 4),
                    })
                last_2min_log = tick

            # ---- Print every 30 seconds ----
            if tick - last_print >= 30:
                cur_btc = get_btc_price()
                statuses = []
                for cfg in CONFIGS:
                    ct = config_trades[cfg["name"]]
                    if ct["exited"]:
                        statuses.append("%-12s" % ct["exit_reason"][:12])
                    else:
                        cur_token = cur_up if ct["direction"] == "UP" else cur_down
                        pnl = ct["tokens"] * cur_token - TRADE_SIZE
                        statuses.append("$%+.2f      " % pnl)

                print("  %4d | $%10s | $%.4f | $%.4f | %s" % (
                    tick, "{:,.2f}".format(cur_btc), cur_up, cur_down,
                    " | ".join(statuses)))
                last_print = tick

        except Exception as e:
            if tick % 60 == 0:
                log("Tick %d hata: %s" % (tick, e))

    # ============ EXPIRY ============
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

    # Process expiry for all configs
    for cfg in CONFIGS:
        ct = config_trades[cfg["name"]]

        # Calculate "would have won" for SL-exited trades
        if ct["exited"] and ct["exit_reason"] == "stop_loss":
            if ct["direction"] == actual_dir:
                # Would have won if held to expiry
                would_pnl = round(ct["tokens"] * 1.00 - TRADE_SIZE, 4)
                ct["would_have_won"] = would_pnl
                log("  %s: SL'de cikildi ($%+.2f) ama EXPIRY'de KAZANIRDI ($%+.2f) | Kayip fark: $%.2f" % (
                    cfg["label"], ct["pnl"], would_pnl, would_pnl - ct["pnl"]))
            else:
                # Would have lost anyway
                would_pnl = round(ct["tokens"] * 0.00 - TRADE_SIZE, 4)
                ct["would_have_won"] = would_pnl
                log("  %s: SL'de cikildi ($%+.2f), expiry'de de KAYBEDERDI ($%+.2f) | SL KORUDU: $%.2f" % (
                    cfg["label"], ct["pnl"], would_pnl, ct["pnl"] - would_pnl))

        if not ct["exited"]:
            ct["exit_time"] = datetime.now().isoformat()
            if ct["direction"] == actual_dir:
                ct["exit_price"] = 1.00
                ct["exit_reason"] = "expiry_win"
            else:
                ct["exit_price"] = 0.00
                ct["exit_reason"] = "expiry_loss"
            ct["pnl"] = round(ct["tokens"] * ct["exit_price"] - TRADE_SIZE, 4)
            ct["exited"] = True

    # ============ DETAILED RESULTS TABLE ============
    print("\n  " + "=" * 80)
    print("  WINDOW #%d DETAILED RESULTS" % window_num)
    print("  Actual BTC direction: %s" % actual_dir)
    print("  " + "-" * 80)
    print("  %-22s | %4s | %6s | %7s | %7s | %12s | %8s | %s" % (
        "Config", "Dir", "Entry", "SL", "Exit", "Reason", "PnL", "Would-Have"))
    print("  " + "-" * 80)

    for cfg in CONFIGS:
        ct = config_trades[cfg["name"]]
        win_marker = " WIN" if ct["pnl"] > 0 else " LOSS"
        would_str = ""
        if ct["would_have_won"] is not None:
            would_str = "$%+.2f" % ct["would_have_won"]

        print("  %-22s | %4s | $%.3f | $%.2f | $%.3f | %12s | $%+.2f%s | %s" % (
            cfg["label"], ct["direction"], ct["entry_price"], cfg["sl"],
            ct["exit_price"], ct["exit_reason"], ct["pnl"], win_marker, would_str))

    # Entry timing info
    print("\n  Entry timing: %s (source: %s)" % (
        entry_time.strftime("%H:%M:%S.%f")[:12], price_src))
    secs_before_open = block_ts - int(entry_time.timestamp())
    if secs_before_open > 0:
        print("  >>> PRE-WINDOW ENTRY: %ds BEFORE window opened! <<<" % secs_before_open)
    else:
        print("  >>> LATE ENTRY: %ds AFTER window opened <<<" % abs(secs_before_open))

    print("  " + "=" * 80)

    # ============ SAVE RESULTS (update live_window in-place) ============
    live_window["btc_exit"] = round(final_btc, 2)
    live_window["actual_direction"] = actual_dir
    live_window["pre_window_entry"] = secs_before_open > 0
    live_window["secs_before_open"] = secs_before_open
    live_window["status"] = "completed"
    live_window["completed_at"] = datetime.now().isoformat()
    # trades dict is already updated in-place via config_trades reference

    # Update cumulative stats
    for cfg in CONFIGS:
        cfg_name = cfg["name"]
        cfg_summary = None
        for cs in results_data["configs"]:
            if cs["name"] == cfg_name:
                cfg_summary = cs
                break
        if cfg_summary is None:
            cfg_summary = {
                "name": cfg_name, "label": cfg["label"],
                "sl": cfg["sl"], "tp": cfg["tp"],
                "dir": cfg["dir"], "flip": cfg.get("flip", False),
                "total_trades": 0, "wins": 0,
                "total_pnl": 0, "pnl_history": [],
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
    print("\n" + "#" * 70)
    print("  V3 SL SWEET SPOT TEST")
    print("  %d configs x %d windows = %d total trades" % (
        len(CONFIGS), num_windows, len(CONFIGS) * num_windows))
    print("  dry_mode = True (ALWAYS)")
    print("  PRE-WINDOW ENTRY: T-30s momentum | T-10s tokens | T-2s FIRE!")
    print("#" * 70)

    for cfg in CONFIGS:
        print("  - %s" % cfg["label"])
    print()

    # RESUME or fresh start
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
            "configs": [], "windows": [],
            "status": "starting",
            "started": datetime.now().isoformat(),
            "target_windows": num_windows,
            "last_update": datetime.now().isoformat(),
        }
        done = 0
        save_results(results_data)

    for i in range(done, num_windows):
        print("\n>>> Window %d/%d bekliyor..." % (i + 1, num_windows))
        block_ts, can_pre_enter = wait_for_window()
        run_window_for_all_configs(block_ts, results_data, pre_entry=can_pre_enter)

        # Print standings
        print("\n  === CUMULATIVE STANDINGS (after %d windows) ===" % (i + 1))
        print("  %-25s | %5s | %5s | %8s" % ("Config", "W/L", "WR%", "PnL"))
        print("  " + "-" * 55)

        sorted_configs = sorted(results_data["configs"],
                                key=lambda c: c["total_pnl"], reverse=True)
        for j, cs in enumerate(sorted_configs):
            marker = " <-- LEADER" if j == 0 else ""
            print("  %-25s | %d/%d   | %5.1f | $%+.2f%s" % (
                cs["label"],
                cs["wins"], cs["total_trades"] - cs["wins"],
                cs["wr"], cs["total_pnl"], marker))

        time.sleep(5)

    # Final
    results_data["status"] = "completed"
    results_data["completed"] = datetime.now().isoformat()
    save_results(results_data)

    print("\n" + "=" * 70)
    print("  V3 TEST COMPLETE - %d windows" % num_windows)
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
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    do_resume = "--resume" in sys.argv
    run_validation(n, resume=do_resume)
