"""
Polymarket BTC binary market tarayici
- Farkli zaman dilimlerini tara (5m, 15m, 1h)
- Farkli slug pattern'lerini dene
- Orderbook spread ve likiditeyi raporla
- Tradeable market bul ($0.40-0.60 arasi ask olan)
"""
import time
import json
import requests
from datetime import datetime

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

now_ts = int(time.time())
print("=" * 70)
print("  POLYMARKET BTC MARKET TARAYICI")
print("  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 70)

# 1. Farkli slug pattern'lerini dene
patterns = []

# 15-dakika (bilinen)
for off in range(-2, 5):
    block = ((now_ts // 900) + off) * 900
    patterns.append(("15m", "btc-updown-15m-%d" % block, block))

# 5-dakika
for off in range(-2, 5):
    block = ((now_ts // 300) + off) * 300
    patterns.append(("5m", "btc-updown-5m-%d" % block, block))

# 1-saat
for off in range(-1, 3):
    block = ((now_ts // 3600) + off) * 3600
    patterns.append(("1h", "btc-updown-1h-%d" % block, block))

# 4-saat
for off in range(-1, 2):
    block = ((now_ts // 14400) + off) * 14400
    patterns.append(("4h", "btc-updown-4h-%d" % block, block))

# Daily
for off in range(0, 2):
    block = ((now_ts // 86400) + off) * 86400
    patterns.append(("1d", "btc-updown-daily-%d" % block, block))

print("\n[1/3] %d slug pattern test ediliyor...\n" % len(patterns))

found_markets = []
for interval, slug, block in patterns:
    try:
        r = requests.get(GAMMA_BASE + "/markets/slug/" + slug, timeout=5)
        if r.status_code == 200:
            m = r.json()
            closed = m.get("closed", True)
            status = "CLOSED" if closed else "ACTIVE"
            clob_ids = m.get("clobTokenIds", "[]")
            if isinstance(clob_ids, str):
                clob_ids = json.loads(clob_ids)
            prices = m.get("outcomePrices", "[]")
            if isinstance(prices, str):
                prices = json.loads(prices)
            print("  [%s] %s %s | %s" % (status, interval, slug[-15:], m.get("question", "")[:50]))
            if prices:
                print("         Mid prices: %s" % str(prices))
            if not closed and len(clob_ids) >= 2:
                found_markets.append({
                    "interval": interval,
                    "slug": slug,
                    "question": m.get("question", ""),
                    "token_ids": clob_ids,
                    "mid_prices": prices,
                    "closed": closed,
                })
    except Exception as e:
        pass

# 2. Gamma API ile genel arama
print("\n[2/3] Gamma API ile BTC binary market aranıyor...\n")
try:
    r = requests.get(GAMMA_BASE + "/events",
                     params={"active": "true", "closed": "false", "limit": 200},
                     timeout=10)
    if r.status_code == 200:
        events = r.json()
        for ev in events:
            title = ev.get("title", "").lower()
            if "bitcoin" in title or "btc" in title:
                print("  Event: %s" % ev.get("title", "")[:70])
                for m in ev.get("markets", [])[:10]:
                    q = m.get("question", "")
                    closed = m.get("closed", False)
                    if not closed:
                        clob_ids = m.get("clobTokenIds", "[]")
                        if isinstance(clob_ids, str):
                            clob_ids = json.loads(clob_ids)
                        prices = m.get("outcomePrices", "[]")
                        if isinstance(prices, str):
                            prices = json.loads(prices)
                        slug = m.get("slug", "")
                        print("    OPEN: %s | prices=%s" % (q[:60], str(prices)))
                        if len(clob_ids) >= 2:
                            found_markets.append({
                                "interval": "event",
                                "slug": slug,
                                "question": q,
                                "token_ids": clob_ids,
                                "mid_prices": prices,
                                "closed": False,
                            })
except Exception as e:
    print("  Events arama hatasi: %s" % e)

# Deduplicate
seen = set()
unique_markets = []
for m in found_markets:
    key = m["slug"] or m["question"]
    if key not in seen:
        seen.add(key)
        unique_markets.append(m)

# 3. Her aktif market icin orderbook cek
print("\n[3/3] %d aktif market icin orderbook analizi...\n" % len(unique_markets))
print("  %-6s | %-40s | %-10s | %-10s | %-8s" % ("Type", "Question", "UP Ask", "DOWN Ask", "Spread"))
print("  " + "-" * 85)

tradeable = []
for m in unique_markets:
    try:
        up_r = requests.get(CLOB_BASE + "/book",
                           params={"token_id": m["token_ids"][0]}, timeout=5)
        down_r = requests.get(CLOB_BASE + "/book",
                             params={"token_id": m["token_ids"][1]}, timeout=5)
        up_book = up_r.json()
        down_book = down_r.json()

        up_asks = up_book.get("asks", [])
        down_asks = down_book.get("asks", [])
        up_bids = up_book.get("bids", [])
        down_bids = down_book.get("bids", [])

        up_ask = float(up_asks[0]["price"]) if up_asks else None
        down_ask = float(down_asks[0]["price"]) if down_asks else None
        up_bid = float(up_bids[0]["price"]) if up_bids else None
        down_bid = float(down_bids[0]["price"]) if down_bids else None

        if up_ask and down_ask:
            spread = up_ask + down_ask - 1.0
            print("  %-6s | %-40s | $%-9.3f | $%-9.3f | $%.3f" % (
                m["interval"], m["question"][:40], up_ask, down_ask, spread))

            # Tradeable = ask $0.35-$0.65 arasi
            if (0.35 <= up_ask <= 0.65) or (0.35 <= down_ask <= 0.65):
                tradeable.append({
                    "market": m,
                    "up_ask": up_ask, "up_bid": up_bid,
                    "down_ask": down_ask, "down_bid": down_bid,
                    "spread": spread,
                })
                print("         *** TRADEABLE! ***")

            # Detayli book (eger ask < 0.95 ise)
            if up_ask < 0.95 or down_ask < 0.95:
                print("         UP  book: bid=$%.3f ask=$%.3f (size=%.0f)" % (
                    up_bid or 0, up_ask, float(up_asks[0]["size"]) if up_asks else 0))
                print("         DOWN book: bid=$%.3f ask=$%.3f (size=%.0f)" % (
                    down_bid or 0, down_ask, float(down_asks[0]["size"]) if down_asks else 0))
        else:
            print("  %-6s | %-40s | %-10s | %-10s | N/A" % (
                m["interval"], m["question"][:40], "no asks", "no asks"))

    except Exception as e:
        print("  %-6s | %-40s | ERROR: %s" % (m["interval"], m["question"][:40], str(e)))

print("\n" + "=" * 70)
print("  SONUC: %d aktif market, %d tradeable" % (len(unique_markets), len(tradeable)))
if tradeable:
    print("\n  TRADEABLE MARKETLER:")
    for t in tradeable:
        print("    - %s" % t["market"]["question"])
        print("      UP=$%.3f DOWN=$%.3f spread=$%.3f" % (t["up_ask"], t["down_ask"], t["spread"]))
else:
    print("\n  Hicbir markette $0.35-$0.65 arasi ask bulunamadi.")
    print("  Tum marketler bid=$0.01 ask=$0.99 ile calisiyor (spread=98%%)")
print("=" * 70)

with open("market_scan.json", "w", encoding="utf-8") as f:
    json.dump({
        "timestamp": datetime.now().isoformat(),
        "total_found": len(unique_markets),
        "tradeable": len(tradeable),
        "markets": [{"slug": m["slug"], "question": m["question"], "interval": m["interval"]}
                    for m in unique_markets],
        "tradeable_details": tradeable,
    }, f, indent=2, default=str)
print("\nSonuc market_scan.json'a kaydedildi.")
