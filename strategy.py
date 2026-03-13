"""
Polymarket BTC Arbitrage Strategy
Claude Code bu dosyadaki CONFIG parametrelerini değiştirerek deney yapar.
"""

import time
import json
import requests
from datetime import datetime

# ============================================================
# CONFIG — Claude Code sadece bu bloğu değiştirir
# ============================================================
CONFIG = {
    "dry_mode": True,               # Her zaman True tut (gerçek para harcama)
    "trade_size_usd": 5.0,          # Her trade için USD miktarı
    "spread_threshold": 0.02,       # Arbitraj için max toplam maliyet (örn: 0.98 = 1¢ kâr)
    "min_edge": 0.008,              # Minimum edge (1.0 - toplam_maliyet)
    "max_trades_per_window": 2,     # Pencere başına max trade
    "price_check_interval": 10,     # Kaç saniyede bir fiyat kontrol et
    "require_both_sides": True,     # Her iki tarafı da aynı anda al
    "up_bias": False,               # Up tarafını öncelikle değerlendir
    "down_bias": False,             # Down tarafını öncelikle değerlendir
}
# ============================================================

# Polymarket BTC 5-dakikalık market ID'leri
# Her gün yenilenir — run_experiment.py bunları otomatik günceller
MARKET_CONFIG = {
    "base_url": "https://clob.polymarket.com",
    "gamma_url": "https://gamma-api.polymarket.com",
}


def fetch_btc_markets():
    """Aktif BTC 5-dakikalık up/down marketlerini çek."""
    try:
        response = requests.get(
            f"{MARKET_CONFIG['gamma_url']}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": 50,
            },
            timeout=10
        )
        markets = response.json()
        
        btc_markets = []
        for market in markets:
            title = market.get("question", "").lower()
            if "bitcoin" in title or "btc" in title:
                if "above" in title or "higher" in title or "up" in title:
                    btc_markets.append({
                        "id": market.get("id"),
                        "question": market.get("question"),
                        "condition_id": market.get("conditionId"),
                        "tokens": market.get("tokens", []),
                    })
        return btc_markets
    except Exception as e:
        print(f"Market fetch hatası: {e}")
        return []


def get_orderbook_prices(token_id):
    """Token için en iyi bid/ask fiyatlarını al."""
    try:
        response = requests.get(
            f"{MARKET_CONFIG['base_url']}/book",
            params={"token_id": token_id},
            timeout=5
        )
        book = response.json()
        
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        
        best_ask = float(asks[0]["price"]) if asks else None
        best_bid = float(bids[0]["price"]) if bids else None
        
        return best_bid, best_ask
    except Exception as e:
        return None, None


def find_arbitrage_opportunity(market):
    """Markette arbitraj fırsatı var mı kontrol et."""
    tokens = market.get("tokens", [])
    if len(tokens) < 2:
        return None
    
    up_token = tokens[0]
    down_token = tokens[1]
    
    _, up_ask = get_orderbook_prices(up_token.get("token_id", ""))
    _, down_ask = get_orderbook_prices(down_token.get("token_id", ""))
    
    if up_ask is None or down_ask is None:
        return None
    
    total_cost = up_ask + down_ask
    edge = 1.0 - total_cost
    
    # CONFIG filtrelerini uygula
    if edge < CONFIG["min_edge"]:
        return None
    
    if total_cost > (1.0 - CONFIG["spread_threshold"]):
        return None
    
    return {
        "market_id": market["id"],
        "question": market["question"],
        "up_ask": up_ask,
        "down_ask": down_ask,
        "total_cost": total_cost,
        "edge": edge,
        "timestamp": datetime.now().isoformat(),
    }


def simulate_trade(opportunity):
    """Dry mode'da trade simüle et."""
    if not CONFIG["dry_mode"]:
        raise Exception("CANLI MOD AÇIK! Önce dry_mode=True yap.")
    
    size = CONFIG["trade_size_usd"]
    expected_payout = size / opportunity["total_cost"]
    expected_profit = expected_payout - size
    
    print(f"  [DRY] Trade simüle edildi:")
    print(f"        Up:   {opportunity['up_ask']:.4f}")
    print(f"        Down: {opportunity['down_ask']:.4f}")
    print(f"        Toplam maliyet: {opportunity['total_cost']:.4f}")
    print(f"        Edge: {opportunity['edge']:.4f}")
    print(f"        Beklenen kâr: ${expected_profit:.4f}")
    
    return {
        "status": "simulated",
        "size": size,
        "total_cost": opportunity["total_cost"],
        "edge": opportunity["edge"],
        "expected_profit": expected_profit,
        "win": True,  # Arbitrajda her zaman kazanılır
    }


def run_strategy(duration_seconds=60):
    """
    Ana strateji döngüsü.
    duration_seconds: kaç saniye çalışacak
    """
    print(f"\n{'='*50}")
    print(f"Polymarket Arbitraj Stratejisi Başlatıldı")
    print(f"Mod: {'DRY RUN' if CONFIG['dry_mode'] else 'CANLI'}")
    print(f"Süre: {duration_seconds} saniye")
    print(f"Config: {json.dumps(CONFIG, indent=2)}")
    print(f"{'='*50}\n")
    
    start_time = time.time()
    results = {
        "trades": [],
        "opportunities_found": 0,
        "trades_executed": 0,
        "config": CONFIG.copy(),
        "start_time": datetime.now().isoformat(),
    }
    
    trades_this_window = 0
    
    while time.time() - start_time < duration_seconds:
        elapsed = time.time() - start_time
        remaining = duration_seconds - elapsed
        print(f"\n[{elapsed:.0f}s / {duration_seconds}s] Market taranıyor...")
        
        # Market bul
        markets = fetch_btc_markets()
        
        if not markets:
            print("  BTC marketi bulunamadı, bekleniyor...")
            time.sleep(CONFIG["price_check_interval"])
            continue
        
        # Her markette fırsat ara
        for market in markets[:3]:  # İlk 3 markete bak
            opp = find_arbitrage_opportunity(market)
            
            if opp:
                results["opportunities_found"] += 1
                print(f"  ✓ Fırsat bulundu! Edge: {opp['edge']:.4f}")
                
                # Trade limiti kontrolü
                if trades_this_window >= CONFIG["max_trades_per_window"]:
                    print(f"  ⚠ Window limiti doldu ({CONFIG['max_trades_per_window']} trade)")
                    continue
                
                # Trade yap
                trade_result = simulate_trade(opp)
                trade_result["opportunity"] = opp
                results["trades"].append(trade_result)
                results["trades_executed"] += 1
                trades_this_window += 1
            else:
                print(f"  - Fırsat yok (market: {market['question'][:50]}...)")
        
        time.sleep(CONFIG["price_check_interval"])
    
    # Sonuçları hesapla
    results["end_time"] = datetime.now().isoformat()
    results["duration_seconds"] = duration_seconds
    
    if results["trades_executed"] > 0:
        results["fill_rate"] = results["trades_executed"] / max(results["opportunities_found"], 1)
        results["avg_edge"] = sum(t["edge"] for t in results["trades"]) / len(results["trades"])
        results["win_rate"] = sum(1 for t in results["trades"] if t["win"]) / len(results["trades"])
        results["score"] = results["fill_rate"] * results["avg_edge"] * 100
    else:
        results["fill_rate"] = 0.0
        results["avg_edge"] = 0.0
        results["win_rate"] = 0.0
        results["score"] = 0.0
    
    print(f"\n{'='*50}")
    print(f"Deney Tamamlandı")
    print(f"Fırsatlar: {results['opportunities_found']}")
    print(f"Tradeler: {results['trades_executed']}")
    print(f"Fill Rate: {results['fill_rate']:.4f}")
    print(f"Avg Edge: {results['avg_edge']:.4f}")
    print(f"Score: {results['score']:.4f}")
    print(f"{'='*50}\n")
    
    # Sonucu dosyaya yaz
    with open("last_result.json", "w") as f:
        json.dump(results, f, indent=2)
    
    return results


if __name__ == "__main__":
    run_strategy(duration_seconds=60)
