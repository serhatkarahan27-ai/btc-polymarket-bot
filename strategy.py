"""
BTC Price Action Bot - Polymarket 15-Min Binary Markets
=========================================================
Teknik indikatör YOK. Sadece price management:
  - Entry: her 15dk pencere basinda pozisyon al
  - Stop-loss: token fiyati X'e duserse sat (zarar kes)
  - Take-profit: token fiyati Y'ye cikarse sat (kar al)
  - Pozisyon yonu: basit momentum (son N mumun yonu) veya her zaman UP

Polymarket binary token price modeli:
  - Pencere basi: UP token ~ entry_price (ornegin 0.50)
  - Pencere sonu: UP token = 1.00 (BTC yukseldi) veya 0.00 (BTC dustu)
  - Pencere ortasi: token fiyati BTC hareketine gore kayar
  - SL/TP bu ara fiyatlara gore tetiklenir

Claude Code sadece CONFIG blogunu degistirerek deney yapar.
"""

import time
import json
import math
import requests
import itertools
from datetime import datetime, timezone

# ============================================================
# CONFIG — Claude Code sadece bu blogu degistirir
# ============================================================
CONFIG = {
    "dry_mode": True,
    "trade_size_usd": 5.0,
    "window_minutes": 15,
    "backtest_candles": 300,
    "candle_interval": "1m",
    # --- Price Action parametreleri ---
    "direction_mode": "always_up",  # "momentum", "always_up", "always_down", "alternate"
    "momentum_lookback": 3,         # Kac mum geriye bakarak yon belirle
    "entry_price": 0.50,            # Token giris fiyati (genelde ~0.50)
    # --- Stop-Loss / Take-Profit ---
    "stop_loss": 0.30,              # Token fiyati buraya duserse sat (zarar kes)
    "take_profit": 0.72,            # Token fiyati buraya cikarse sat (kar al)
    "use_stop_loss": False,
    "use_take_profit": False,
    # --- Position sizing ---
    "sizing_mode": "fixed",         # "fixed", "kelly", "martingale"
    "kelly_fraction": 0.25,         # Kelly criterion orani
    "martingale_multiplier": 1.5,   # Kayiptan sonra carpan
    "max_position_usd": 20.0,       # Max pozisyon buyuklugu
}
# ============================================================

BINANCE_BASE = "https://api.binance.com/api/v3"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


# ============================================================
# API CACHE - ayni veriyi tekrar cekmemek icin
# ============================================================
_cache = {}
_cache_ttl = {}

def _cache_get(key, ttl_seconds=60):
    """Cache'den veri al. TTL dolmussa None dondur."""
    if key in _cache and key in _cache_ttl:
        if time.time() - _cache_ttl[key] < ttl_seconds:
            return _cache[key]
    return None

def _cache_set(key, value):
    """Cache'e veri yaz."""
    _cache[key] = value
    _cache_ttl[key] = time.time()

def clear_cache():
    """Tum cache'i temizle."""
    _cache.clear()
    _cache_ttl.clear()


# ============================================================
# BINANCE VERI
# ============================================================

def fetch_binance_klines(symbol="BTCUSDT", interval=None, limit=None):
    if interval is None:
        interval = CONFIG["candle_interval"]
    if limit is None:
        limit = CONFIG["backtest_candles"]

    cache_key = f"klines_{symbol}_{interval}_{limit}"
    cached = _cache_get(cache_key, ttl_seconds=30)
    if cached is not None:
        return cached

    resp = requests.get(
        f"{BINANCE_BASE}/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    candles = []
    for k in resp.json():
        candles.append({
            "ts": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    _cache_set(cache_key, candles)
    return candles


# ============================================================
# POLYMARKET MARKET DISCOVERY
# ============================================================

def fetch_polymarket_market(slug):
    cache_key = f"market_{slug}"
    cached = _cache_get(cache_key, ttl_seconds=60)
    if cached is not None:
        return cached

    try:
        resp = requests.get(f"{GAMMA_BASE}/markets/slug/{slug}", timeout=10)
        if resp.status_code == 404:
            _cache_set(cache_key, None)
            return None
        resp.raise_for_status()
        m = resp.json()
        clob_ids = m.get("clobTokenIds", "[]")
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)
        outcomes = m.get("outcomes", "[]")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        prices = m.get("outcomePrices", "[]")
        if isinstance(prices, str):
            prices = json.loads(prices)
        result = {
            "id": m.get("id"),
            "question": m.get("question", ""),
            "slug": slug,
            "token_ids": clob_ids,
            "outcomes": outcomes,
            "prices": prices,
            "closed": m.get("closed", False),
        }
        _cache_set(cache_key, result)
        return result
    except Exception as e:
        return None


def fetch_orderbook(token_id):
    cache_key = f"book_{token_id}"
    cached = _cache_get(cache_key, ttl_seconds=10)
    if cached is not None:
        return cached

    try:
        resp = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=5)
        book = resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        result = {
            "best_bid": float(bids[0]["price"]) if bids else None,
            "best_ask": float(asks[0]["price"]) if asks else None,
        }
        _cache_set(cache_key, result)
        return result
    except:
        return {"best_bid": None, "best_ask": None}


def find_active_btc_15m_market():
    now_ts = int(time.time())
    for offset in [0, 1, -1, -2]:
        block = ((now_ts // 900) + offset) * 900
        slug = f"btc-updown-15m-{block}"
        market = fetch_polymarket_market(slug)
        if market and not market["closed"] and len(market["token_ids"]) >= 2:
            return market
    return None


# ============================================================
# TOKEN FIYAT MODELI
# ============================================================

def estimate_token_price(btc_move_pct, minutes_elapsed, window_minutes, entry_price=0.50):
    """15-dk pencere icinde UP token fiyatini tahmin et.

    Model:
    - BTC hareket yuzdesini, penceredeki kalan sureye gore olcekle
    - Sigmoid benzeri fonksiyonla 0-1 arasina map et
    - Pencere basinda belirsizlik yuksek → fiyat 0.50 yakin
    - Pencere sonunda kesinlik yuksek → fiyat 0 veya 1'e yakin

    Args:
        btc_move_pct: BTC'nin pencere basina gore % degisimi (pozitif=yukseldi)
        minutes_elapsed: pencere baslamasindan bu yana gecen dakika
        window_minutes: toplam pencere suresi
        entry_price: baslangic token fiyati
    """
    # Zaman faktoru: pencere sonuna yaklastikca kesinlik artar
    time_factor = minutes_elapsed / window_minutes  # 0.0 → 1.0
    # Kesinlik carpani: pencere basinda 2, sonunda 20
    certainty = 2.0 + time_factor * 18.0

    # BTC hareketini token fiyatina cevir
    # Tipik 15dk BTC volatilitesi ~0.15% civarinda
    # normalized_move: +1 = guclu yukselis, -1 = guclu dusus
    normalized_move = btc_move_pct / 0.15 * certainty

    # Sigmoid ile 0-1 arasina
    token_price = 1.0 / (1.0 + math.exp(-normalized_move * 0.05))

    # Clamp
    return max(0.01, min(0.99, token_price))


# ============================================================
# YON BELIRLEME (teknik indikatorsuz)
# ============================================================

def determine_direction(candles, index, cfg=None):
    """Basit price action ile yon belirle. Indikatorsuz.

    Modlar:
      momentum:   son N mumun net yonu
      always_up:  her zaman UP
      always_down: her zaman DOWN
      alternate:  UP/DOWN sirayla
    """
    if cfg is None:
        cfg = CONFIG
    mode = cfg.get("direction_mode", "momentum")

    if mode == "always_up":
        return "UP"
    elif mode == "always_down":
        return "DOWN"
    elif mode == "alternate":
        return "UP" if (index % 2 == 0) else "DOWN"
    else:  # momentum
        lookback = cfg.get("momentum_lookback", 5)
        if index < lookback:
            return "UP"  # Yetersiz veri, default UP
        start_price = candles[index - lookback]["close"]
        end_price = candles[index]["close"]
        return "UP" if end_price >= start_price else "DOWN"


# ============================================================
# POSITION SIZING
# ============================================================

def calculate_position_size(cfg, previous_trades=None):
    """Pozisyon buyuklugunu hesapla."""
    if previous_trades is None:
        previous_trades = []

    mode = cfg.get("sizing_mode", "fixed")
    base_size = cfg["trade_size_usd"]
    max_size = cfg.get("max_position_usd", 20.0)

    if mode == "fixed":
        return base_size

    elif mode == "kelly":
        # Kelly criterion: f = (bp - q) / b
        # b = odds (1:1 for binary), p = win_rate, q = 1-p
        if len(previous_trades) < 5:
            return base_size
        wins = sum(1 for t in previous_trades if t["pnl_usd"] > 0)
        p = wins / len(previous_trades)
        q = 1 - p
        b = 1.0  # Even odds
        kelly = (b * p - q) / b
        if kelly <= 0:
            return base_size * 0.5  # Minimum bet
        fraction = cfg.get("kelly_fraction", 0.25)
        return min(base_size * (1 + kelly * fraction * 10), max_size)

    elif mode == "martingale":
        # Son trade kayipsa pozisyonu buyut
        if previous_trades and previous_trades[-1]["pnl_usd"] < 0:
            mult = cfg.get("martingale_multiplier", 1.5)
            prev_size = previous_trades[-1]["position_size"]
            return min(prev_size * mult, max_size)
        return base_size

    return base_size


# ============================================================
# SINGLE TRADE SIMULASYONU
# ============================================================

def simulate_single_trade(candles, entry_idx, cfg=None):
    """Tek bir 15-dk pencerede trade simule et.

    1. Pencere basinda yon belirle (UP veya DOWN)
    2. Token al (entry_price'dan)
    3. Her dakika token fiyatini guncelle
    4. SL/TP tetiklenirse erken cik
    5. Pencere sonunda resolve (1.00 veya 0.00)

    Returns: trade dict with full P&L details
    """
    if cfg is None:
        cfg = CONFIG
    window = cfg["window_minutes"]

    if entry_idx + window >= len(candles):
        return None

    # Yon belirle
    direction = determine_direction(candles, entry_idx, cfg)

    # Entry
    entry_btc = candles[entry_idx]["close"]
    entry_token_price = cfg.get("entry_price", 0.50)

    # Dakika dakika token fiyatini simule et
    exit_price = None
    exit_reason = "expiry"  # "stop_loss", "take_profit", "expiry"
    exit_minute = window

    for m in range(1, window + 1):
        if entry_idx + m >= len(candles):
            break

        current_btc = candles[entry_idx + m]["close"]
        btc_move_pct = (current_btc - entry_btc) / entry_btc * 100

        # UP token fiyati
        up_token_price = estimate_token_price(btc_move_pct, m, window, entry_token_price)

        # Bizim token fiyatimiz (UP aldıysak up_token, DOWN aldıysak 1-up_token)
        if direction == "UP":
            our_token_price = up_token_price
        else:
            our_token_price = 1.0 - up_token_price

        # Stop-loss kontrolu
        if cfg.get("use_stop_loss", True) and our_token_price <= cfg.get("stop_loss", 0.30):
            exit_price = our_token_price
            exit_reason = "stop_loss"
            exit_minute = m
            break

        # Take-profit kontrolu
        if cfg.get("use_take_profit", True) and our_token_price >= cfg.get("take_profit", 0.72):
            exit_price = our_token_price
            exit_reason = "take_profit"
            exit_minute = m
            break

    # Eger SL/TP tetiklenmediyse, expiry'de resolve
    if exit_price is None:
        exit_btc = candles[entry_idx + window]["close"]
        actual_direction = "UP" if exit_btc >= entry_btc else "DOWN"
        if direction == actual_direction:
            exit_price = 1.00  # Kazandik
        else:
            exit_price = 0.00  # Kaybettik

    # P&L hesapla
    tokens_bought = cfg["trade_size_usd"] / entry_token_price
    exit_value = tokens_bought * exit_price
    pnl_usd = exit_value - cfg["trade_size_usd"]
    roi_pct = (pnl_usd / cfg["trade_size_usd"]) * 100

    # Gercek BTC hareketi
    exit_btc_final = candles[entry_idx + window]["close"]
    btc_move_final = (exit_btc_final - entry_btc) / entry_btc * 100
    actual_dir = "UP" if exit_btc_final >= entry_btc else "DOWN"
    win = pnl_usd > 0

    return {
        "entry_idx": entry_idx,
        "direction": direction,
        "actual_direction": actual_dir,
        "entry_btc": round(entry_btc, 2),
        "exit_btc": round(exit_btc_final, 2),
        "btc_move_pct": round(btc_move_final, 4),
        "entry_token_price": round(entry_token_price, 4),
        "exit_token_price": round(exit_price, 4),
        "exit_reason": exit_reason,
        "exit_minute": exit_minute,
        "tokens_bought": round(tokens_bought, 4),
        "position_size": round(cfg["trade_size_usd"], 2),
        "exit_value": round(exit_value, 4),
        "pnl_usd": round(pnl_usd, 4),
        "roi_pct": round(roi_pct, 2),
        "win": win,
        "timestamp": candles[entry_idx]["ts"],
    }


# ============================================================
# BACKTEST
# ============================================================

def backtest_strategy(candles, cfg=None):
    """Tum pencereler icin backtest yap."""
    if cfg is None:
        cfg = CONFIG
    window = cfg["window_minutes"]
    trades = []

    lookback = cfg.get("momentum_lookback", 5)
    start_idx = max(lookback + 1, 1)
    end_idx = len(candles) - window

    if end_idx <= start_idx:
        return trades

    for i in range(start_idx, end_idx, window):
        # Dynamic sizing
        pos_size = calculate_position_size(cfg, trades)
        trade_cfg = dict(cfg)
        trade_cfg["trade_size_usd"] = pos_size

        trade = simulate_single_trade(candles, i, trade_cfg)
        if trade:
            trades.append(trade)

    return trades


# ============================================================
# OTOMATIK OPTIMIZASYON
# ============================================================

def auto_optimize(candles):
    """Farkli parametre kombinasyonlarini dene, en iyisini bul.

    Grid search:
      - stop_loss: [0.20, 0.25, 0.30, 0.35, 0.40]
      - take_profit: [0.60, 0.65, 0.70, 0.75, 0.80, 0.85]
      - direction_mode: [momentum, always_up]
      - momentum_lookback: [3, 5, 8, 10]
      - entry_price: [0.48, 0.50, 0.52]
    """
    print("\n[OPT] Otomatik optimizasyon basliyor...")

    param_grid = {
        "stop_loss": [0.20, 0.25, 0.30, 0.35, 0.40],
        "take_profit": [0.60, 0.65, 0.70, 0.75, 0.80, 0.85],
        "direction_mode": ["momentum", "always_up"],
        "momentum_lookback": [3, 5, 8, 10],
        "entry_price": [0.48, 0.50, 0.52],
    }

    # Tum kombinasyonlar
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combos = list(itertools.product(*values))
    print(f"[OPT] {len(combos)} kombinasyon test edilecek...")

    best_score = -999
    best_config = None
    best_trades = None
    tested = 0

    for combo in combos:
        test_cfg = dict(CONFIG)
        for k, v in zip(keys, combo):
            test_cfg[k] = v

        # SL >= TP ise atla (mantıksız)
        if test_cfg["stop_loss"] >= test_cfg["take_profit"]:
            continue
        # SL >= entry ise atla
        if test_cfg["stop_loss"] >= test_cfg["entry_price"]:
            continue

        trades = backtest_strategy(candles, test_cfg)
        tested += 1

        if len(trades) < 5:
            continue

        wins = sum(1 for t in trades if t["win"])
        wr = wins / len(trades)
        total_pnl = sum(t["pnl_usd"] for t in trades)
        avg_pnl = total_pnl / len(trades)

        # Score = win_rate * 0.6 + profit_factor_normalized * 0.4
        gross_profit = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
        gross_loss = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 10.0
        pf_norm = min(profit_factor / 3.0, 1.0)  # Normalize PF to 0-1

        score = wr * 0.6 + pf_norm * 0.4

        if score > best_score:
            best_score = score
            best_config = dict(test_cfg)
            best_trades = trades
            print(f"  [OPT] Yeni best! WR={wr:.1%} PF={profit_factor:.2f} "
                  f"PnL=${total_pnl:+.2f} ({len(trades)} trades) "
                  f"| SL={test_cfg['stop_loss']} TP={test_cfg['take_profit']} "
                  f"dir={test_cfg['direction_mode']} lb={test_cfg['momentum_lookback']} "
                  f"entry={test_cfg['entry_price']}")

    print(f"\n[OPT] {tested} kombinasyon test edildi")
    return best_config, best_trades, best_score


# ============================================================
# SONUC RAPORLAMA
# ============================================================

def print_trade_summary(trades, label=""):
    """Trade listesini ozetini yazdir."""
    if not trades:
        print(f"  {label} Hicbir trade yok")
        return {}

    wins = sum(1 for t in trades if t["win"])
    losses = len(trades) - wins
    wr = wins / len(trades)
    total_pnl = sum(t["pnl_usd"] for t in trades)
    avg_pnl = total_pnl / len(trades)
    total_invested = sum(t["position_size"] for t in trades)
    roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0

    # Profit factor
    gross_profit = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    gross_loss = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    # Exit reason breakdown
    sl_count = sum(1 for t in trades if t["exit_reason"] == "stop_loss")
    tp_count = sum(1 for t in trades if t["exit_reason"] == "take_profit")
    exp_count = sum(1 for t in trades if t["exit_reason"] == "expiry")

    # Max drawdown
    running_pnl = 0
    peak = 0
    max_dd = 0
    for t in trades:
        running_pnl += t["pnl_usd"]
        peak = max(peak, running_pnl)
        dd = peak - running_pnl
        max_dd = max(max_dd, dd)

    print(f"\n  {'='*60}")
    print(f"  {label} TRADE OZETI")
    print(f"  {'='*60}")
    print(f"  Toplam trade:    {len(trades)}")
    print(f"  Kazanc/Kayip:    {wins}W / {losses}L")
    print(f"  WIN RATE:        {wr:.1%}")
    print(f"  Profit Factor:   {pf:.2f}")
    print(f"  {'-'*40}")
    print(f"  Toplam PnL:      ${total_pnl:+.2f}")
    print(f"  Ort. PnL/trade:  ${avg_pnl:+.4f}")
    print(f"  Toplam yatirim:  ${total_invested:.2f}")
    print(f"  ROI:             {roi:+.2f}%")
    print(f"  Max Drawdown:    ${max_dd:.2f}")
    print(f"  {'-'*40}")
    print(f"  Brut Kar:        ${gross_profit:.2f}")
    print(f"  Brut Zarar:      ${gross_loss:.2f}")
    print(f"  {'-'*40}")
    print(f"  Cikis nedenleri: SL={sl_count} TP={tp_count} Expiry={exp_count}")
    print(f"  {'='*60}")

    # Son 8 trade detayi
    print(f"\n  Son {min(8, len(trades))} trade:")
    for t in trades[-8:]:
        icon = "W" if t["win"] else "L"
        print(f"    [{icon}] {t['direction']:4s} | BTC ${t['entry_btc']:>10,.2f} -> ${t['exit_btc']:>10,.2f} ({t['btc_move_pct']:+.3f}%)"
              f" | Token {t['entry_token_price']:.2f}->{t['exit_token_price']:.2f}"
              f" | {t['exit_reason']:11s} @{t['exit_minute']:2d}m"
              f" | PnL ${t['pnl_usd']:+.4f} ({t['roi_pct']:+.1f}%)")

    return {
        "wins": wins, "losses": losses, "win_rate": round(wr, 4),
        "total_pnl_usd": round(total_pnl, 4), "avg_pnl_usd": round(avg_pnl, 4),
        "roi_pct": round(roi, 2), "profit_factor": round(pf, 4),
        "gross_profit": round(gross_profit, 4), "gross_loss": round(gross_loss, 4),
        "max_drawdown": round(max_dd, 4),
        "sl_exits": sl_count, "tp_exits": tp_count, "expiry_exits": exp_count,
    }


# ============================================================
# ANA STRATEJI
# ============================================================

def run_strategy(duration_seconds=60):
    print(f"\n{'='*65}")
    print(f"  BTC PRICE ACTION BOT - Polymarket 15-Min Markets")
    print(f"{'='*65}")
    print(f"  Mod:          {'DRY RUN' if CONFIG['dry_mode'] else '!!! CANLI !!!'}")
    print(f"  Pencere:      {CONFIG['window_minutes']}dk")
    print(f"  Yon modu:     {CONFIG['direction_mode']} (lookback={CONFIG.get('momentum_lookback', 5)})")
    print(f"  Entry price:  {CONFIG['entry_price']}")
    print(f"  Stop-loss:    {CONFIG['stop_loss']} ({'ACIK' if CONFIG['use_stop_loss'] else 'KAPALI'})")
    print(f"  Take-profit:  {CONFIG['take_profit']} ({'ACIK' if CONFIG['use_take_profit'] else 'KAPALI'})")
    print(f"  Sizing:       {CONFIG['sizing_mode']}")
    print(f"{'='*65}\n")

    start_time = time.time()

    # ---- ADIM 1: Binance verisi ----
    print("[1/4] Binance'den BTC/USDT kline verisi cekiliyor...")
    candles = fetch_binance_klines()
    btc_price = candles[-1]["close"]
    print(f"      {len(candles)} mum yuklendi | Son fiyat: ${btc_price:,.2f}")

    # ---- ADIM 2: Otomatik optimizasyon ----
    print(f"\n[2/4] Parametre optimizasyonu...")
    best_cfg, best_trades, best_opt_score = auto_optimize(candles)

    if best_cfg:
        print(f"\n      EN IYI CONFIG BULUNDU:")
        print(f"      SL={best_cfg['stop_loss']} TP={best_cfg['take_profit']}")
        print(f"      Yon={best_cfg['direction_mode']} Lookback={best_cfg['momentum_lookback']}")
        print(f"      Entry={best_cfg['entry_price']} Sizing={best_cfg['sizing_mode']}")
    else:
        best_cfg = CONFIG
        best_trades = backtest_strategy(candles)

    # ---- ADIM 3: Backtest sonuclari ----
    print(f"\n[3/4] Backtest sonuclari (optimized config)...")
    stats = print_trade_summary(best_trades, "BACKTEST")

    # ---- ADIM 4: Polymarket market ----
    print(f"\n[4/4] Polymarket 15-dk BTC marketi...")
    market = find_active_btc_15m_market()
    live_trade = None

    if market:
        print(f"      BULUNDU: {market['question']}")
        if len(market["token_ids"]) >= 2:
            up_ob = fetch_orderbook(market["token_ids"][0])
            down_ob = fetch_orderbook(market["token_ids"][1])
            print(f"      Up: bid={up_ob['best_bid']} ask={up_ob['best_ask']}")
            print(f"      Down: bid={down_ob['best_bid']} ask={down_ob['best_ask']}")

        # Canli sinyal
        direction = determine_direction(candles, len(candles) - 1, best_cfg)
        pos_size = calculate_position_size(best_cfg, best_trades)
        print(f"\n      [DRY] Sinyal: {direction} | Pozisyon: ${pos_size:.2f}")
        print(f"      [DRY] SL={best_cfg['stop_loss']} TP={best_cfg['take_profit']}")

        live_trade = {
            "direction": direction,
            "position_size": pos_size,
            "entry_price": best_cfg["entry_price"],
            "stop_loss": best_cfg["stop_loss"],
            "take_profit": best_cfg["take_profit"],
            "market": market["question"],
            "slug": market["slug"],
            "btc_price": btc_price,
        }
    else:
        print(f"      Aktif market bulunamadi")

    # ---- SONUCLAR ----
    results = {
        "config": best_cfg.copy(),
        "start_time": datetime.now().isoformat(),
        "duration_seconds": round(time.time() - start_time, 2),
        "btc_price": round(btc_price, 2),
        "total_candles": len(candles),
        # Trade stats
        "backtest_trades": len(best_trades) if best_trades else 0,
        "backtest_wins": stats.get("wins", 0),
        "backtest_losses": stats.get("losses", 0),
        "win_rate": stats.get("win_rate", 0),
        "score": stats.get("win_rate", 0),
        # Dollar P&L
        "total_pnl_usd": stats.get("total_pnl_usd", 0),
        "avg_pnl_usd": stats.get("avg_pnl_usd", 0),
        "roi_pct": stats.get("roi_pct", 0),
        "profit_factor": stats.get("profit_factor", 0),
        "gross_profit": stats.get("gross_profit", 0),
        "gross_loss": stats.get("gross_loss", 0),
        "max_drawdown": stats.get("max_drawdown", 0),
        # Exit reasons
        "sl_exits": stats.get("sl_exits", 0),
        "tp_exits": stats.get("tp_exits", 0),
        "expiry_exits": stats.get("expiry_exits", 0),
        # Polymarket
        "polymarket_found": market is not None,
        "live_trade": live_trade,
        # Trades detail
        "trades": best_trades if best_trades else [],
    }

    print(f"\n{'='*65}")
    print(f"  FINAL SONUC")
    print(f"{'='*65}")
    print(f"  Win Rate:        {stats.get('win_rate', 0):.1%}")
    print(f"  Profit Factor:   {stats.get('profit_factor', 0):.2f}")
    print(f"  Total PnL:       ${stats.get('total_pnl_usd', 0):+.2f}")
    print(f"  ROI:             {stats.get('roi_pct', 0):+.2f}%")
    print(f"  Max Drawdown:    ${stats.get('max_drawdown', 0):.2f}")
    print(f"  Trades:          {len(best_trades) if best_trades else 0}")
    print(f"  Polymarket:      {'AKTIF' if market else 'YOK'}")
    print(f"  Score:           {results['score']:.4f}")
    print(f"{'='*65}\n")

    with open("last_result.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("Sonuc last_result.json'a yazildi.")

    return results


if __name__ == "__main__":
    run_strategy()
