import time, requests
from datetime import datetime

now_ts = int(time.time())
current_block = (now_ts // 900) * 900
next_block = current_block + 900
wait = next_block - now_ts
print("Now:", datetime.now().strftime("%H:%M:%S"))
print("Next window:", datetime.fromtimestamp(next_block).strftime("%H:%M:%S"))
print("Wait: %dm %ds" % (wait // 60, wait % 60))
print()

# Test Binance
try:
    r = requests.get("https://api.binance.com/api/v3/ticker/price",
                     params={"symbol": "BTCUSDT"}, timeout=5)
    print("Binance OK: $%.2f" % float(r.json()["price"]))
except Exception as e:
    print("Binance FAIL:", e)

# Test Gamma
try:
    slug = "btc-updown-15m-%d" % current_block
    r = requests.get("https://gamma-api.polymarket.com/markets/slug/" + slug, timeout=10)
    print("Gamma %s: status=%d" % (slug, r.status_code))
    if r.status_code == 200:
        m = r.json()
        print("  Question:", m.get("question", "?"))
        print("  Closed:", m.get("closed"))
except Exception as e:
    print("Gamma FAIL:", e)

# Next block
slug2 = "btc-updown-15m-%d" % next_block
try:
    r = requests.get("https://gamma-api.polymarket.com/markets/slug/" + slug2, timeout=10)
    print("Gamma next %s: status=%d" % (slug2, r.status_code))
except Exception as e:
    print("Gamma next FAIL:", e)
