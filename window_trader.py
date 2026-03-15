"""
Window Trader v2: Gamma API AMM fiyatlari ile limit order stratejisi
=====================================================================
- CLOB orderbook yerine Gamma API outcomePrices kullanir (gercek AMM fiyat)
- Pencere basinda $0.50 civarinda limit order koyar
- Fill olmazsa (fiyat yukseldiyse) skip eder
- Spread ve fiyat kalitesi kontrol eder
- 15 dakika boyunca SL/TP ile pozisyon yonetir
"""
import time
import json
import math
import requests
from datetime import datetime

BINANCE_BASE = "https://api.binance.com/api/v3"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Limit Order Strategy Config
CONFIG = {
    "entry_price": 0.50,           # hedef limit order fiyati
    "stop_loss": 0.01,             # disabled for data collection
    "take_profit": 0.99,           # disabled for data collection
    "direction_mode": "momentum",
    "momentum_lookback": 10,
    "trade_size_usd": 5.0,
    "window_minutes": 15,
    "early_entry_seconds": 3,
    "max_wait_minutes": 20,
    # Limit Order parametreleri
    "limit_price": 0.55,           # bu fiyat veya altinda al
    "max_fill_wait_secs": 300,     # max 5dk fill bekleme
    "fill_check_interval": 1,     # her 1sn'de fiyat kontrol
    "max_spread_pct": 15.0,        # max spread % (daha toleransli)
    "min_entry_price": 0.30,       # bunun altinda girilmez
    "max_entry_price": 0.70,       # bunun uzerinde girilmez
    "min_momentum_pct": 0.005,     # neredeyse her zaman trade (veri toplama)
}

TRADES_FILE = "window_trades.json"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print("  [%s] %s" % (ts, msg))


# ============================================================
# API FONKSIYONLARI - Gamma API (AMM fiyatlari)
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
        return "UP", 0.0, closes
    move = closes[-1] - closes[0]
    move_pct = move / closes[0] * 100
    direction = "UP" if move >= 0 else "DOWN"
    return direction, move_pct, closes


def get_market_by_slug(slug):
    """Gamma API'den market bilgisi + AMM fiyatlarini al."""
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
        # prices = ["0.52", "0.49"] -> [0.52, 0.49] (UP, DOWN)
        float_prices = [float(p) for p in prices] if prices else []
        return {
            "question": m.get("question", ""),
            "slug": slug,
            "token_ids": clob_ids,
            "outcomes": outcomes,
            "amm_prices": float_prices,  # [up_price, down_price]
            "closed": m.get("closed", True),
            "volume": m.get("volume", "0"),
            "raw": m,
        }
    except Exception as e:
        return None


def find_next_market():
    """Bir sonraki acilacak veya yeni acilmis marketi bul."""
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
    """CLOB /midpoint endpoint'inden gercek zamanli fiyat al.
    Returns: (up_price, down_price) veya (None, None)
    """
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
    """Oncelik: CLOB midpoint (gercek zamanli), fallback: Gamma AMM.
    Returns: (up_price, down_price, source)
    """
    # 1. CLOB midpoint (gercek zamanli)
    if market.get("token_ids") and len(market["token_ids"]) >= 2:
        up, down = get_midpoint_prices(market["token_ids"])
        if up is not None:
            return up, down, "CLOB_midpoint"

    # 2. Fallback: Gamma AMM (gecikmeli)
    if market.get("amm_prices") and len(market["amm_prices"]) >= 2:
        return market["amm_prices"][0], market["amm_prices"][1], "Gamma_AMM"

    return None, None, "none"


def check_price_quality(up_price, down_price):
    """AMM fiyat kalitesi kontrolu."""
    issues = []

    if up_price is None or down_price is None:
        return False, ["Fiyat alinamadi"]

    # Spread kontrolu: up + down idealde 1.0 olmali
    total = up_price + down_price
    spread = abs(total - 1.0)
    spread_pct = spread * 100

    if spread_pct > CONFIG["max_spread_pct"]:
        issues.append("Spread cok yuksek: %.1f%% (max %.1f%%)" % (spread_pct, CONFIG["max_spread_pct"]))

    # Fiyat araligi kontrolu
    if up_price < 0.10 or up_price > 0.90:
        issues.append("UP fiyat cok uc: $%.3f (beklenen ~$0.50)" % up_price)
    if down_price < 0.10 or down_price > 0.90:
        issues.append("DOWN fiyat cok uc: $%.3f (beklenen ~$0.50)" % down_price)

    return len(issues) == 0, issues


def load_trades():
    try:
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_trades(trades):
    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2, default=str)


# ============================================================
# PENCERE BASLANGICI BEKLEME
# ============================================================

def wait_for_window_start():
    """Bir sonraki 15-dk pencere basini bekle."""
    print("\n" + "=" * 65)
    print("  WINDOW TRADER v3 - CLOB Midpoint (Real-Time)")
    print("  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 65)

    now_ts = int(time.time())
    current_block = (now_ts // 900) * 900
    next_block = current_block + 900
    wait_secs = next_block - now_ts

    next_time = datetime.fromtimestamp(next_block)
    log("Simdiki zaman:     %s" % datetime.now().strftime("%H:%M:%S"))
    log("Sonraki pencere:   %s" % next_time.strftime("%H:%M:%S"))
    log("Bekleme suresi:    %ddk %dsn" % (wait_secs // 60, wait_secs % 60))

    # Simdiki marketi kontrol et - AMM fiyatlarini goster
    log("Market kontrol ediliyor...")
    market = find_next_market()
    if market:
        log("Market: %s" % market["question"])
        up, down, src = get_live_prices(market)
        if up is not None:
            log("Fiyatlar (%s): UP=$%.3f DOWN=$%.3f" % (src, up, down))

    # Bekleme
    early = CONFIG["early_entry_seconds"]
    target_wait = max(0, wait_secs - early)
    if target_wait > CONFIG["max_wait_minutes"] * 60:
        target_wait = CONFIG["max_wait_minutes"] * 60

    log("Bekleniyor... (%dsn)" % target_wait)

    waited = 0
    while waited < target_wait:
        sleep_chunk = min(30, target_wait - waited)
        time.sleep(sleep_chunk)
        waited += sleep_chunk
        remaining = target_wait - waited
        if remaining > 0:
            log("  Kalan: %ddk %dsn" % (remaining // 60, remaining % 60))

    log("PENCERE ACILIYOR!")
    return next_block


# ============================================================
# LIMIT ORDER - AMM FIYAT ILE FILL BEKLEME
# ============================================================

def wait_for_limit_fill(market, direction, limit_price, max_wait_secs):
    """CLOB midpoint fiyatini izleyerek limit fill simule et.

    Her fill_check_interval saniyede gercek zamanli midpoint kontrol eder.
    Hedef token fiyati <= limit_price olunca 'fill' olur.

    Returns: (filled, fill_price, up_price, down_price, elapsed_secs)
    """
    token_name = direction
    log("LIMIT ORDER: %s token @ $%.3f (max %ds bekleme)" % (
        token_name, limit_price, max_wait_secs))

    interval = CONFIG["fill_check_interval"]
    elapsed = 0

    while elapsed < max_wait_secs:
        try:
            up_price, down_price, src = get_live_prices(market)

            if up_price is None:
                log("  [%ds] Fiyat alinamadi, bekleniyor..." % elapsed)
                time.sleep(interval)
                elapsed += interval
                continue

            our_price = up_price if direction == "UP" else down_price
            spread = abs((up_price + down_price) - 1.0) * 100

            # Her 10 saniyede veya fill olunca logla
            if elapsed % 10 == 0 or our_price <= limit_price:
                log("  [%ds] UP=$%.3f DOWN=$%.3f | %s=$%.3f | src=%s" % (
                    elapsed, up_price, down_price, token_name, our_price, src))

            # Limit fill kontrolu
            if our_price <= limit_price:
                log("  FILL! %s=$%.3f <= limit=$%.3f (%ds'de) [%s]" % (
                    token_name, our_price, limit_price, elapsed, src))
                return True, our_price, up_price, down_price, elapsed

            # Fiyat cok yukseldiyse erken cik
            if our_price > 0.80 and elapsed > 60:
                log("  %s=$%.3f cok yuksek, erken skip" % (token_name, our_price))
                return False, our_price, up_price, down_price, elapsed

        except Exception as e:
            log("  [%ds] Kontrol hatasi: %s" % (elapsed, e))

        time.sleep(interval)
        elapsed += interval

    log("  TIMEOUT: %ds'de fill olmadi" % max_wait_secs)
    return False, None, None, None, elapsed


# ============================================================
# TRADE CALISTIRMA
# ============================================================

def execute_window_trade(block_ts):
    """Pencere acildiginda trade gir ve 15dk boyunca izle."""

    print("\n" + "=" * 65)
    print("  TRADE GIRIS - %s" % datetime.now().strftime("%H:%M:%S"))
    print("=" * 65)

    # 1. BTC fiyati ve momentum
    log("[1/6] BTC fiyat + momentum...")
    btc_entry = get_btc_price()
    direction, mom_pct, closes = get_btc_momentum(CONFIG["momentum_lookback"])
    log("  BTC:       $%s" % "{:,.2f}".format(btc_entry))
    log("  Momentum:  %+.4f%% -> %s" % (mom_pct, direction))

    # Momentum threshold kontrolu
    min_mom = CONFIG["min_momentum_pct"]
    if abs(mom_pct) < min_mom:
        log("SKIP: Momentum cok zayif (%.4f%% < %.2f%%)" % (abs(mom_pct), min_mom))
        log("  Yeterli momentum olmadan trade girilmiyor")
        skip_result = {
            "timestamp": datetime.now().isoformat(),
            "block_ts": block_ts,
            "market": "N/A",
            "slug": "N/A",
            "direction": direction,
            "btc_entry": round(btc_entry, 2),
            "amm_price": None,
            "limit_price": CONFIG["limit_price"],
            "exit_reason": "weak_momentum",
            "momentum_pct": round(mom_pct, 4),
            "pnl_usd": 0,
            "win": False,
            "skipped": True,
            "config": CONFIG.copy(),
        }
        trades = load_trades()
        trades.append(skip_result)
        save_trades(trades)
        log("Skip kaydedildi (weak_momentum)")
        return skip_result

    # 2. Market bul
    log("[2/6] Polymarket marketi araniyor...")
    market = find_next_market()
    if not market:
        log("HATA: Market bulunamadi!")
        return None
    log("  Market: %s" % market["question"])
    log("  Slug:   %s" % market["slug"])

    # 3. Gercek zamanli fiyat kontrolu (CLOB midpoint)
    log("[3/6] Fiyat kontrolu...")
    up_price, down_price, price_src = get_live_prices(market)
    if up_price is None:
        log("HATA: Fiyat alinamadi!")
        return None

    log("  UP:   $%.3f (%s)" % (up_price, price_src))
    log("  DOWN: $%.3f" % down_price)
    spread = abs((up_price + down_price) - 1.0) * 100
    log("  Spread: %.1f%%" % spread)

    # Kalite kontrolu
    quality_ok, issues = check_price_quality(up_price, down_price)
    if not quality_ok:
        log("FIYAT KALITESIZ:")
        for iss in issues:
            log("  x %s" % iss)

    # Hangi token alacagiz?
    our_price = up_price if direction == "UP" else down_price
    log("  Hedef: %s token @ $%.3f" % (direction, our_price))

    # Fiyat araligi kontrolu
    if our_price < CONFIG["min_entry_price"]:
        log("SKIP: Fiyat cok dusuk ($%.3f < $%.3f)" % (our_price, CONFIG["min_entry_price"]))
        return _save_skip(block_ts, market, direction, btc_entry, mom_pct, our_price, "price_too_low")
    if our_price > CONFIG["max_entry_price"]:
        log("SKIP: Fiyat cok yuksek ($%.3f > $%.3f)" % (our_price, CONFIG["max_entry_price"]))
        return _save_skip(block_ts, market, direction, btc_entry, mom_pct, our_price, "price_too_high")

    # Spread kontrolu
    if spread > CONFIG["max_spread_pct"]:
        log("SKIP: Spread cok yuksek (%.1f%% > %.1f%%)" % (spread, CONFIG["max_spread_pct"]))
        return _save_skip(block_ts, market, direction, btc_entry, mom_pct, our_price, "spread_too_high")

    # 4. LIMIT ORDER: hedef fiyattan fill bekleme
    log("[4/6] LIMIT ORDER - $%.3f'dan fill bekleniyor..." % CONFIG["limit_price"])

    # Eger anlik fiyat zaten limit'in altindaysa hemen fill
    if our_price <= CONFIG["limit_price"]:
        log("  ANLIK FILL! %s=$%.3f <= limit=$%.3f" % (direction, our_price, CONFIG["limit_price"]))
        entry_price_actual = our_price
        wait_time = 0
    else:
        filled, fill_price, up_p, down_p, wait_time = wait_for_limit_fill(
            market, direction, CONFIG["limit_price"], CONFIG["max_fill_wait_secs"]
        )
        if not filled:
            log("SKIP: Limit order fill olmadi, bu pencere atlaniyor")
            return _save_skip(block_ts, market, direction, btc_entry, mom_pct, fill_price, "no_fill")
        entry_price_actual = fill_price

    trade_size = CONFIG["trade_size_usd"]
    tokens_bought = trade_size / entry_price_actual
    sl = CONFIG["stop_loss"]
    tp = CONFIG["take_profit"]

    risk = entry_price_actual - sl
    reward = tp - entry_price_actual
    rr_ratio = reward / risk if risk > 0 else 0

    log("  Token:     %s" % direction)
    log("  Entry:     $%.3f (AMM)" % entry_price_actual)
    log("  Size:      $%.2f (%.2f token)" % (trade_size, tokens_bought))
    log("  SL:        $%.2f" % sl)
    log("  TP:        $%.2f" % tp)
    log("  R/R:       1:%.2f" % rr_ratio)

    # 5. Pozisyon izleme - AMM fiyatlarini periyodik kontrol
    remaining_secs = CONFIG["window_minutes"] * 60 - wait_time
    remaining_minutes = max(3, remaining_secs // 60)

    log("[5/6] Pozisyon izleniyor (%ddk kaldi)..." % remaining_minutes)
    print("\n  %4s | %12s | %8s | %6s | %8s | Status" % ("Min", "BTC", "Move%", "Token", "PnL"))
    print("  " + "-" * 65)

    exit_price = None
    exit_reason = "expiry"
    exit_minute = remaining_minutes
    min_token_price = entry_price_actual
    max_token_price = entry_price_actual

    check_interval = 1  # her 1 saniyede fiyat kontrol
    total_checks = remaining_secs // check_interval
    last_print_sec = 0

    for tick in range(1, total_checks + 1):
        time.sleep(check_interval)
        elapsed_secs_pos = tick * check_interval
        elapsed_min = elapsed_secs_pos / 60.0

        try:
            # CLOB midpoint'ten gercek zamanli fiyat al
            cur_up, cur_down, cur_src = get_live_prices(market)

            if cur_up is not None:
                current_token = cur_up if direction == "UP" else cur_down
            else:
                current_btc = get_btc_price()
                btc_move = (current_btc - btc_entry) / btc_entry * 100
                current_token = estimate_token_price(btc_move, elapsed_min)

            min_token_price = min(min_token_price, current_token)
            max_token_price = max(max_token_price, current_token)
            current_pnl = (current_token - entry_price_actual) * tokens_bought
            status = "HOLD"

            # SL kontrolu
            if current_token <= sl:
                current_btc = get_btc_price()
                btc_move = (current_btc - btc_entry) / btc_entry * 100
                exit_price = current_token
                exit_reason = "stop_loss"
                exit_minute = int(elapsed_min) + 1
                status = "!! STOP-LOSS !!"
                print("  %4.0f | $%10s | %+7.3f%% | $%.3f | $%+7.2f | %s" % (
                    elapsed_min, "{:,.2f}".format(current_btc), btc_move, current_token, current_pnl, status))
                log("STOP-LOSS! Token=$%.3f (%dsn'de)" % (current_token, elapsed_secs_pos))
                break

            # TP kontrolu
            if current_token >= tp:
                current_btc = get_btc_price()
                btc_move = (current_btc - btc_entry) / btc_entry * 100
                exit_price = current_token
                exit_reason = "take_profit"
                exit_minute = int(elapsed_min) + 1
                status = "** TAKE-PROFIT **"
                print("  %4.0f | $%10s | %+7.3f%% | $%.3f | $%+7.2f | %s" % (
                    elapsed_min, "{:,.2f}".format(current_btc), btc_move, current_token, current_pnl, status))
                log("TAKE-PROFIT! Token=$%.3f (%dsn'de)" % (current_token, elapsed_secs_pos))
                break

            # Her 15 saniyede ekrana yaz (cogu tick sessiz)
            if elapsed_secs_pos - last_print_sec >= 15:
                current_btc = get_btc_price()
                btc_move = (current_btc - btc_entry) / btc_entry * 100
                print("  %4.0f | $%10s | %+7.3f%% | $%.3f | $%+7.2f | %s" % (
                    elapsed_min, "{:,.2f}".format(current_btc), btc_move, current_token, current_pnl, status))
                last_print_sec = elapsed_secs_pos

        except Exception as e:
            log("  Tick %d hata: %s" % (tick, e))

    # Expiry
    if exit_price is None:
        try:
            final_btc = get_btc_price()
            btc_move_final = (final_btc - btc_entry) / btc_entry * 100
            actual_direction = "UP" if final_btc >= btc_entry else "DOWN"

            if direction == actual_direction:
                exit_price = 1.00
            else:
                exit_price = 0.00

            log("EXPIRY: BTC $%s -> $%s (%+.3f%%)" % (
                "{:,.2f}".format(btc_entry), "{:,.2f}".format(final_btc), btc_move_final))
            log("  Gercek yon: %s | Tahmin: %s | %s" % (
                actual_direction, direction,
                "DOGRU" if direction == actual_direction else "YANLIS"))
        except:
            exit_price = entry_price_actual

    # PnL hesapla
    exit_value = tokens_bought * exit_price
    pnl = exit_value - trade_size
    roi = (pnl / trade_size) * 100

    print("\n  " + "=" * 65)
    print("  TRADE SONUCU")
    print("  " + "=" * 65)
    print("  Order:       LIMIT @ $%.3f (%s)" % (entry_price_actual, price_src))
    print("  Yon:         %s token" % direction)
    print("  Entry:       $%.3f" % entry_price_actual)
    print("  Exit:        $%.3f (%s @%ddk)" % (exit_price, exit_reason, exit_minute))
    print("  Min/Max:     $%.3f / $%.3f" % (min_token_price, max_token_price))
    print("  R/R:         1:%.2f" % rr_ratio)
    print("  Tokens:      %.2f" % tokens_bought)
    print("  PnL:         $%+.2f (ROI: %+.1f%%)" % (pnl, roi))
    print("  Sonuc:       %s" % ("WIN" if pnl > 0 else "LOSS"))
    print("  Fill suresi: %dsn" % wait_time)
    print("  " + "=" * 65)

    # 6. Trade kaydet
    log("[6/6] Trade kaydediliyor...")
    trade_result = {
        "timestamp": datetime.now().isoformat(),
        "block_ts": block_ts,
        "market": market["question"],
        "slug": market["slug"],
        "direction": direction,
        "btc_entry": round(btc_entry, 2),
        "btc_exit": round(get_btc_price(), 2),
        "entry_price_actual": entry_price_actual,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "exit_minute": exit_minute,
        "tokens_bought": round(tokens_bought, 4),
        "trade_size_usd": trade_size,
        "pnl_usd": round(pnl, 4),
        "roi_pct": round(roi, 2),
        "win": pnl > 0,
        "skipped": False,
        "min_token": min_token_price,
        "max_token": max_token_price,
        "momentum_pct": round(mom_pct, 4),
        "up_price": up_price,
        "down_price": down_price,
        "price_source": price_src,
        "spread_pct": round(spread, 2),
        "order_type": "LIMIT_MIDPOINT",
        "limit_price": CONFIG["limit_price"],
        "fill_wait_secs": wait_time,
        "risk_reward": round(rr_ratio, 2),
        "config": CONFIG.copy(),
    }

    trades = load_trades()
    trades.append(trade_result)
    save_trades(trades)
    log("Trade kaydedildi -> %s (%d toplam)" % (TRADES_FILE, len(trades)))

    with open("forward_test.json", "w", encoding="utf-8") as f:
        json.dump(trade_result, f, indent=2, default=str)
    log("forward_test.json guncellendi")

    return trade_result


def _save_skip(block_ts, market, direction, btc_entry, mom_pct, price, reason):
    """Skip edilen trade'i kaydet."""
    skip_result = {
        "timestamp": datetime.now().isoformat(),
        "block_ts": block_ts,
        "market": market["question"],
        "slug": market["slug"],
        "direction": direction,
        "btc_entry": round(btc_entry, 2),
        "amm_price": price,
        "limit_price": CONFIG["limit_price"],
        "exit_reason": reason,
        "pnl_usd": 0,
        "win": False,
        "skipped": True,
        "momentum_pct": round(mom_pct, 4),
        "config": CONFIG.copy(),
    }
    trades = load_trades()
    trades.append(skip_result)
    save_trades(trades)
    log("Skip kaydedildi (%s) -> %s" % (reason, TRADES_FILE))
    return skip_result


def estimate_token_price(btc_move_pct, minutes_elapsed):
    """Fallback: AMM fiyat alinamazsa sigmoid model."""
    window = CONFIG["window_minutes"]
    time_factor = minutes_elapsed / window
    certainty = 2.0 + time_factor * 18.0
    normalized = btc_move_pct / 0.15 * certainty
    price = 1.0 / (1.0 + math.exp(-normalized * 0.05))
    return max(0.01, min(0.99, price))


# ============================================================
# ANA DONGU
# ============================================================

def run_single_window():
    """Tek bir pencere bekle, trade yap."""
    block_ts = wait_for_window_start()
    return execute_window_trade(block_ts)


def run_continuous(num_windows=None):
    """Surekli pencere baslarini yakala ve trade yap."""
    print("\n" + "#" * 65)
    print("  WINDOW TRADER v3 - CLOB Midpoint Real-Time")
    print("  Config: SL=$%.2f TP=$%.2f Limit=$%.2f" % (
        CONFIG["stop_loss"], CONFIG["take_profit"], CONFIG["limit_price"]))
    print("  Direction: %s (lookback=%d)" % (CONFIG["direction_mode"], CONFIG["momentum_lookback"]))
    print("  Trade size: $%.2f" % CONFIG["trade_size_usd"])
    print("  Max spread: %.1f%% | Price range: $%.2f-$%.2f" % (
        CONFIG["max_spread_pct"], CONFIG["min_entry_price"], CONFIG["max_entry_price"]))
    if num_windows:
        print("  Hedef: %d pencere" % num_windows)
    else:
        print("  Hedef: Sonsuz dongu (Ctrl+C ile dur)")
    print("#" * 65)

    trades_done = 0
    total_pnl = 0.0

    try:
        while True:
            if num_windows and trades_done >= num_windows:
                break

            result = run_single_window()
            if result:
                trades_done += 1
                if not result.get("skipped"):
                    total_pnl += result["pnl_usd"]

                all_trades = [t for t in load_trades() if not t.get("skipped")]
                wins = sum(1 for t in all_trades if t.get("win"))
                total = len(all_trades)
                wr = wins / total * 100 if total > 0 else 0
                skips = sum(1 for t in load_trades() if t.get("skipped"))

                print("\n  --- OZET: %d trade (%d skip) | WR=%.1f%% | PnL=$%+.2f ---\n" % (
                    total, skips, wr, total_pnl))

            time.sleep(5)

    except KeyboardInterrupt:
        print("\n\n  Durduruluyor...")
        trades = load_trades()
        real_trades = [t for t in trades if not t.get("skipped")]
        if real_trades:
            wins = sum(1 for t in real_trades if t.get("win"))
            total_pnl = sum(t["pnl_usd"] for t in real_trades)
            skips = len(trades) - len(real_trades)
            print("  Toplam: %d trade (%d skip) | %dW/%dL | PnL=$%+.2f" % (
                len(real_trades), skips, wins, len(real_trades) - wins, total_pnl))
        print("  Sonuclar %s dosyasinda." % TRADES_FILE)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--continuous":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else None
        run_continuous(n)
    else:
        run_single_window()
