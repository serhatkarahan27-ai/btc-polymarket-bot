"""
BTC Directional Trading Bot - Polymarket 15-Min Markets
=========================================================
Binance'den gercek BTC fiyat verisi cekilir, teknik indikatörlerle
yon tahmini yapilir, Polymarket 15-dk BTC Up/Down marketlerinde trade edilir.

API Endpoints:
  - Binance: https://api.binance.com/api/v3 (klines, ticker)
  - Gamma:   https://gamma-api.polymarket.com (market discovery by slug)
  - CLOB:    https://clob.polymarket.com (orderbook, prices)

Polymarket 15-min BTC slug pattern:
  btc-updown-15m-{unix_timestamp}
  Timestamp her 15 dakikada bir degisir (900 saniyelik bloklar)

Claude Code sadece CONFIG blogunu degistirerek deney yapar.
"""

import time
import json
import math
import requests
from datetime import datetime, timezone

# ============================================================
# CONFIG — Claude Code sadece bu blogu degistirir
# ============================================================
CONFIG = {
    "dry_mode": True,               # Her zaman True
    "trade_size_usd": 5.0,          # Trade basi USD
    "window_minutes": 15,           # Polymarket penceresi (15dk)
    "backtest_candles": 1000,       # Backtest icin kac 1m mum (~16 saat)
    "candle_interval": "1m",        # Binance mum araligi
    # --- Indikatör parametreleri ---
    "rsi_period": 9,                # Kisa RSI (15dk icin daha hassas)
    "rsi_overbought": 60,           # Daha dusuk asiri alim esigi
    "rsi_oversold": 40,             # Daha yuksek asiri satim esigi
    "fast_ma": 5,                   # 5-mum hizli MA
    "slow_ma": 20,                  # 20-mum yavas MA
    "momentum_period": 10,          # 10-mum momentum
    # --- Sinyal agirliklari ---
    "weight_rsi": 0.5,              # RSI biraz daha guclu
    "weight_ma": 1.5,               # MA hafifletildi
    "weight_momentum": 1.0,         # Momentum guclendirildi
    "signal_threshold": 0.15,       # Daha dusuk esik (daha fazla trade)
    "volume_filter": True,          # Volume filtresi ACIK
}
# ============================================================

BINANCE_BASE = "https://api.binance.com/api/v3"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


# ============================================================
# BINANCE VERI CEKME
# ============================================================

def fetch_binance_klines(symbol="BTCUSDT", interval=None, limit=None):
    """Binance'den mum verisi cek."""
    if interval is None:
        interval = CONFIG["candle_interval"]
    if limit is None:
        limit = CONFIG["backtest_candles"]
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
    return candles


def get_btc_price():
    """Anlik BTC/USDT fiyatini al."""
    resp = requests.get(
        f"{BINANCE_BASE}/ticker/price",
        params={"symbol": "BTCUSDT"},
        timeout=5,
    )
    return float(resp.json()["price"])


# ============================================================
# POLYMARKET MARKET DISCOVERY
# ============================================================

def get_current_15m_slug():
    """Suanki aktif 15-dakikalik BTC market slug'ini hesapla.

    Slug pattern: btc-updown-15m-{timestamp}
    Timestamp = floor(now / 900) * 900 (15dk blok baslangici)
    """
    now_ts = int(time.time())
    block_start = (now_ts // 900) * 900
    return f"btc-updown-15m-{block_start}"


def get_next_15m_slug():
    """Sonraki 15-dakikalik blok slug'i."""
    now_ts = int(time.time())
    next_block = ((now_ts // 900) + 1) * 900
    return f"btc-updown-15m-{next_block}"


def fetch_polymarket_market(slug):
    """Gamma API'den slug ile market verisini cek.

    Returns: {
        id, question, conditionId, clobTokenIds (Up, Down),
        outcomes, outcomePrices, closed, active, endDate
    } or None
    """
    try:
        resp = requests.get(
            f"{GAMMA_BASE}/markets/slug/{slug}",
            timeout=10,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        m = resp.json()

        # Parse JSON string fields
        clob_ids = m.get("clobTokenIds", "[]")
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)
        outcomes = m.get("outcomes", "[]")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        prices = m.get("outcomePrices", "[]")
        if isinstance(prices, str):
            prices = json.loads(prices)

        return {
            "id": m.get("id"),
            "question": m.get("question", ""),
            "slug": slug,
            "conditionId": m.get("conditionId"),
            "token_ids": clob_ids,      # [Up_token, Down_token]
            "outcomes": outcomes,         # ["Up", "Down"]
            "prices": prices,            # mid-prices from Gamma
            "closed": m.get("closed", False),
            "active": m.get("active", True),
            "endDate": m.get("endDate"),
            "volume": m.get("volume", "0"),
            "negRisk": m.get("negRisk", False),
        }
    except Exception as e:
        print(f"  [!] Market fetch hatasi ({slug}): {e}")
        return None


def fetch_orderbook(token_id):
    """CLOB API'den token orderbook'u cek.

    Returns: {best_bid, best_ask, bid_depth, ask_depth, spread, last_trade_price}
    """
    try:
        resp = requests.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=5,
        )
        book = resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        spread = (best_ask - best_bid) if (best_ask and best_bid) else None

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_depth": len(bids),
            "ask_depth": len(asks),
            "spread": spread,
            "last_trade_price": book.get("last_trade_price"),
            "tick_size": book.get("tick_size", "0.01"),
        }
    except Exception as e:
        return {"best_bid": None, "best_ask": None, "spread": None,
                "bid_depth": 0, "ask_depth": 0, "last_trade_price": None}


def find_active_btc_15m_market():
    """Aktif bir 15-dk BTC Up/Down marketi bul.

    Sirasi ile dener:
    1. Suanki 15-dk blok (acik mi?)
    2. Sonraki 15-dk blok
    3. Ozel slug pattern'lari
    """
    slugs_to_try = [
        get_current_15m_slug(),
        get_next_15m_slug(),
    ]

    # Son 2 blok da dene (gecikmeli kapanmis olabilir)
    now_ts = int(time.time())
    for offset in [-1, -2]:
        block = ((now_ts // 900) + offset) * 900
        slugs_to_try.append(f"btc-updown-15m-{block}")

    for slug in slugs_to_try:
        market = fetch_polymarket_market(slug)
        if market and not market["closed"] and len(market["token_ids"]) >= 2:
            return market

    return None


# ============================================================
# TEKNIK INDIKATÖRLER
# ============================================================

def compute_rsi(closes, period=None):
    """RSI hesapla (Wilder's smoothing)."""
    if period is None:
        period = CONFIG["rsi_period"]
    if len(closes) < period + 1:
        return [50.0] * len(closes)

    rsi_values = [50.0] * period
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100.0 - (100.0 / (1.0 + rs)))
    return rsi_values


def compute_sma(closes, period):
    """Basit hareketli ortalama."""
    sma = []
    for i in range(len(closes)):
        if i < period - 1:
            sma.append(None)
        else:
            sma.append(sum(closes[i - period + 1:i + 1]) / period)
    return sma


def compute_momentum(closes, period=None):
    """Momentum = close[i] - close[i-period]."""
    if period is None:
        period = CONFIG["momentum_period"]
    mom = []
    for i in range(len(closes)):
        if i < period:
            mom.append(0.0)
        else:
            mom.append(closes[i] - closes[i - period])
    return mom


def generate_signal(candles, index):
    """Verilen index'teki mum icin birlesik sinyal uret.

    Returns: (direction, strength)
      direction: "UP" veya "DOWN"
      strength: 0.0 - 1.0
    """
    closes = [c["close"] for c in candles[:index + 1]]
    if len(closes) < CONFIG["slow_ma"] + 1:
        return "HOLD", 0.0

    # RSI
    rsi_all = compute_rsi(closes, CONFIG["rsi_period"])
    rsi = rsi_all[-1]
    if rsi < CONFIG["rsi_oversold"]:
        rsi_signal = 1.0
    elif rsi > CONFIG["rsi_overbought"]:
        rsi_signal = -1.0
    else:
        rsi_signal = (50 - rsi) / 50.0

    # MA crossover
    fast_ma = compute_sma(closes, CONFIG["fast_ma"])
    slow_ma = compute_sma(closes, CONFIG["slow_ma"])
    ma_signal = 0.0
    if fast_ma[-1] is not None and slow_ma[-1] is not None:
        diff = fast_ma[-1] - slow_ma[-1]
        ma_signal = diff / closes[-1] * 100
        ma_signal = max(-1.0, min(1.0, ma_signal * 10))

    # Momentum
    mom = compute_momentum(closes, CONFIG["momentum_period"])
    mom_signal = 0.0
    if mom[-1] != 0:
        mom_signal = mom[-1] / closes[-1] * 100
        mom_signal = max(-1.0, min(1.0, mom_signal * 5))

    # Agirlikli birlesim
    total_weight = CONFIG["weight_rsi"] + CONFIG["weight_ma"] + CONFIG["weight_momentum"]
    if total_weight == 0:
        return "HOLD", 0.0

    combined = (
        rsi_signal * CONFIG["weight_rsi"]
        + ma_signal * CONFIG["weight_ma"]
        + mom_signal * CONFIG["weight_momentum"]
    ) / total_weight

    direction = "UP" if combined > 0 else "DOWN"
    strength = abs(combined)
    return direction, strength


# ============================================================
# POLYMARKET TRADE SIMULASYONU (DRY MODE)
# ============================================================

def simulate_polymarket_trade(market, direction, strength, btc_price):
    """Polymarket 15-dk marketinde dry-mode trade simule et.

    Polymarket'te Up veya Down token'i ALINIR:
    - Eger tahmin UP ise → Up token'ini al (ask fiyatindan)
    - Eger tahmin DOWN ise → Down token'ini al (ask fiyatindan)
    - Token 1.00'a resolve olursa kar, 0.00'a resolve olursa zarar
    """
    if not CONFIG["dry_mode"]:
        raise Exception("CANLI MOD ACIK! dry_mode=True yap.")

    # Token indexleri: 0=Up, 1=Down
    token_idx = 0 if direction == "UP" else 1
    token_id = market["token_ids"][token_idx]

    # Orderbook'tan fiyat al
    ob = fetch_orderbook(token_id)
    ask_price = ob["best_ask"]

    if ask_price is None or ask_price <= 0 or ask_price >= 1.0:
        # Gamma mid-price'i kullan
        try:
            ask_price = float(market["prices"][token_idx])
        except (IndexError, ValueError, TypeError):
            ask_price = 0.50  # Fallback

    # Kac token alabiliriz
    tokens_bought = CONFIG["trade_size_usd"] / ask_price if ask_price > 0 else 0

    # Max kazanc: tokens_bought * (1.0 - ask_price) (token 1.0'a resolve olursa)
    # Max kayip: trade_size_usd (token 0.0'a resolve olursa)
    potential_profit = tokens_bought * (1.0 - ask_price)
    potential_loss = CONFIG["trade_size_usd"]

    return {
        "status": "simulated",
        "direction": direction,
        "strength": round(strength, 4),
        "token_idx": token_idx,
        "ask_price": round(ask_price, 4),
        "tokens_bought": round(tokens_bought, 4),
        "trade_size_usd": CONFIG["trade_size_usd"],
        "potential_profit": round(potential_profit, 4),
        "potential_loss": round(potential_loss, 4),
        "btc_price_at_entry": round(btc_price, 2),
        "market_question": market["question"],
        "market_slug": market["slug"],
    }


# ============================================================
# BACKTEST (Binance verileriyle gecmis dogrulama)
# ============================================================

def backtest_strategy(candles):
    """Binance mumlari uzerinde backtest yap.

    Her 15-dakikalik pencerede:
    1. Indikatörlere göre sinyal uret
    2. 15 dakika sonrasindaki gercek fiyatla karsilastir
    3. Win/loss kaydet
    """
    window = CONFIG["window_minutes"]
    threshold = CONFIG["signal_threshold"]
    trades = []

    start_idx = max(CONFIG["slow_ma"], CONFIG["rsi_period"], CONFIG["momentum_period"]) + 5
    end_idx = len(candles) - window

    if end_idx <= start_idx:
        print(f"  Yetersiz veri: {len(candles)} mum")
        return trades

    for i in range(start_idx, end_idx, window):
        direction, strength = generate_signal(candles, i)

        if strength < threshold:
            continue

        if CONFIG.get("volume_filter", False):
            vol_period = 20
            if i >= vol_period:
                volumes = [c["volume"] for c in candles[i - vol_period:i]]
                avg_vol = sum(volumes) / len(volumes)
                if candles[i]["volume"] < avg_vol:
                    continue

        entry_price = candles[i]["close"]
        exit_price = candles[i + window]["close"]
        actual_move = exit_price - entry_price
        actual_direction = "UP" if actual_move > 0 else "DOWN"

        win = direction == actual_direction
        pnl_pct = abs(actual_move) / entry_price * 100
        pnl_usd = CONFIG["trade_size_usd"] * (pnl_pct / 100) * (1 if win else -1)

        trades.append({
            "index": i,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "prediction": direction,
            "actual": actual_direction,
            "strength": round(strength, 4),
            "win": win,
            "pnl_pct": round(pnl_pct, 4),
            "pnl_usd": round(pnl_usd, 4),
            "timestamp": candles[i]["ts"],
        })

    return trades


# ============================================================
# ANA STRATEJI
# ============================================================

def run_strategy(duration_seconds=60):
    """Ana strateji dongusu.

    1. Binance'den BTC kline verisi cek
    2. Backtest yap (gecmis dogrulama)
    3. Aktif Polymarket 15-dk marketi bul
    4. Canli sinyal uret ve dry-mode trade simule et
    """
    print(f"\n{'='*65}")
    print(f"  BTC DIRECTIONAL BOT - Polymarket 15-Min Markets")
    print(f"{'='*65}")
    print(f"  Mod:        {'DRY RUN' if CONFIG['dry_mode'] else '!!! CANLI !!!'}")
    print(f"  Pencere:    {CONFIG['window_minutes']}dk")
    print(f"  RSI({CONFIG['rsi_period']})    OB={CONFIG['rsi_overbought']} OS={CONFIG['rsi_oversold']}")
    print(f"  MA:         fast={CONFIG['fast_ma']} slow={CONFIG['slow_ma']}")
    print(f"  Momentum:   {CONFIG['momentum_period']}")
    print(f"  Agirliklar: RSI={CONFIG['weight_rsi']} MA={CONFIG['weight_ma']} MOM={CONFIG['weight_momentum']}")
    print(f"  Esik:       {CONFIG['signal_threshold']}")
    print(f"{'='*65}\n")

    start_time = time.time()

    # ---- ADIM 1: Binance verisi ----
    print("[1/4] Binance'den BTC/USDT kline verisi cekiliyor...")
    candles = fetch_binance_klines()
    btc_price = candles[-1]["close"]
    print(f"      {len(candles)} mum yuklendi | Son fiyat: ${btc_price:,.2f}")

    # ---- ADIM 2: Backtest ----
    print(f"\n[2/4] Backtest ({CONFIG['window_minutes']}dk pencere, {len(candles)} mum)...")
    trades = backtest_strategy(candles)

    bt_wins = sum(1 for t in trades if t["win"])
    bt_losses = len(trades) - bt_wins
    bt_winrate = bt_wins / len(trades) if trades else 0
    bt_pnl = sum(t["pnl_usd"] for t in trades)

    print(f"      {len(trades)} trade | {bt_wins}W/{bt_losses}L | WR={bt_winrate:.1%} | PnL=${bt_pnl:+.2f}")

    # ---- ADIM 3: Polymarket marketi ----
    print(f"\n[3/4] Polymarket 15-dk BTC marketi araniyor...")
    market = find_active_btc_15m_market()

    live_trade = None
    if market:
        print(f"      BULUNDU: {market['question']}")
        print(f"      Slug: {market['slug']}")
        print(f"      Up mid={market['prices'][0] if market['prices'] else '?'} "
              f"| Down mid={market['prices'][1] if len(market['prices']) > 1 else '?'}")

        # Orderbook detay
        if len(market["token_ids"]) >= 2:
            up_ob = fetch_orderbook(market["token_ids"][0])
            down_ob = fetch_orderbook(market["token_ids"][1])
            print(f"      Up OB:   bid={up_ob['best_bid']} ask={up_ob['best_ask']} spread={up_ob['spread']}")
            print(f"      Down OB: bid={down_ob['best_bid']} ask={down_ob['best_ask']} spread={down_ob['spread']}")

        # ---- ADIM 4: Canli sinyal & trade ----
        print(f"\n[4/4] Canli sinyal uretiliyor...")
        direction, strength = generate_signal(candles, len(candles) - 1)
        print(f"      Sinyal: {direction} (guc={strength:.4f}, esik={CONFIG['signal_threshold']})")

        if strength >= CONFIG["signal_threshold"]:
            live_trade = simulate_polymarket_trade(market, direction, strength, btc_price)
            print(f"\n      {'='*50}")
            print(f"      [DRY] TRADE SIMULASYONU")
            print(f"      {'='*50}")
            print(f"      Yon:           {live_trade['direction']}")
            print(f"      Ask fiyati:    {live_trade['ask_price']:.4f}")
            print(f"      Token adedi:   {live_trade['tokens_bought']:.2f}")
            print(f"      Yatirim:       ${live_trade['trade_size_usd']:.2f}")
            print(f"      Max kar:       ${live_trade['potential_profit']:.2f}")
            print(f"      Max zarar:     ${live_trade['potential_loss']:.2f}")
            print(f"      BTC fiyati:    ${live_trade['btc_price_at_entry']:,.2f}")
            print(f"      Market:        {live_trade['market_question']}")
            print(f"      {'='*50}")
        else:
            print(f"      Sinyal cok zayif ({strength:.4f} < {CONFIG['signal_threshold']}), trade yok")
    else:
        print(f"      [!] Aktif 15-dk BTC marketi bulunamadi")
        print(f"      [4/4] Canli trade atlanacak (sadece backtest sonuclari)")

    # ---- SONUCLAR ----
    results = {
        "config": CONFIG.copy(),
        "start_time": datetime.now().isoformat(),
        "end_time": datetime.now().isoformat(),
        "duration_seconds": round(time.time() - start_time, 2),
        "btc_price": round(btc_price, 2),
        "total_candles": len(candles),
        # Backtest
        "backtest_trades": len(trades),
        "backtest_wins": bt_wins,
        "backtest_losses": bt_losses,
        "backtest_win_rate": round(bt_winrate, 4),
        "backtest_pnl_usd": round(bt_pnl, 4),
        # Ana metrik
        "win_rate": round(bt_winrate, 4),
        "score": round(bt_winrate, 4),
        # Canli market
        "polymarket_found": market is not None,
        "polymarket_slug": market["slug"] if market else None,
        "live_trade": live_trade,
        # Detaylar
        "trades": trades,
    }

    print(f"\n{'='*65}")
    print(f"  SONUC OZETI")
    print(f"{'='*65}")
    print(f"  Backtest WR:      {bt_winrate:.2%} ({bt_wins}W/{bt_losses}L)")
    print(f"  Backtest PnL:     ${bt_pnl:+.2f}")
    print(f"  Polymarket:       {'AKTIF' if market else 'YOK'}")
    print(f"  Canli trade:      {'EVET' if live_trade else 'HAYIR'}")
    print(f"  Score (WR):       {results['score']:.4f}")
    print(f"{'='*65}\n")

    with open("last_result.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Sonuc last_result.json'a yazildi.")

    return results


if __name__ == "__main__":
    run_strategy()
