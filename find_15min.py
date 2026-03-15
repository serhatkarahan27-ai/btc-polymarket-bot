"""Find the 15-min BTC Up/Down market on Polymarket."""
import requests, json

# Try different search approaches
print("=== METHOD 1: Search by slug ===")
for slug in ["bitcoin-up-or-down-15-min", "15-min-crypto", "bitcoin-15-min"]:
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets?slug={slug}", timeout=10)
        data = r.json()
        if data:
            print(f"  FOUND via slug '{slug}': {json.dumps(data[0] if isinstance(data, list) else data, indent=2)[:500]}")
    except Exception as e:
        print(f"  slug '{slug}': {e}")

print("\n=== METHOD 2: Search all markets for '15 min' ===")
for offset in range(0, 500, 100):
    r = requests.get("https://gamma-api.polymarket.com/markets",
        params={"active": "true", "closed": "false", "limit": 100, "offset": offset},
        timeout=10)
    markets = r.json()
    if not markets:
        break
    for m in markets:
        q = m.get("question", "").lower()
        if "15 min" in q or "15-min" in q or "up or down" in q:
            clob_ids = m.get("clobTokenIds", "[]")
            if isinstance(clob_ids, str):
                clob_ids = json.loads(clob_ids)
            outcomes = m.get("outcomes", "[]")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            print(f"\n  FOUND: {m['question']}")
            print(f"    ID: {m.get('id')}")
            print(f"    Slug: {m.get('slug')}")
            print(f"    Condition ID: {m.get('conditionId')}")
            print(f"    CLOB Token IDs: {clob_ids}")
            print(f"    Outcomes: {outcomes}")
            print(f"    Prices: {m.get('outcomePrices')}")
            print(f"    Closed: {m.get('closed')}")
            print(f"    Active: {m.get('active')}")

print("\n=== METHOD 3: Search events for '15 min' or 'up or down' ===")
r = requests.get("https://gamma-api.polymarket.com/events",
    params={"active": "true", "closed": "false", "limit": 200}, timeout=10)
events = r.json()
for ev in events:
    title = ev.get("title", "").lower()
    if "15 min" in title or "up or down" in title or "5 min" in title:
        print(f"\n  Event: {ev['title']}")
        for m in ev.get("markets", []):
            q = m.get("question", "")
            clob_ids = m.get("clobTokenIds", "[]")
            if isinstance(clob_ids, str):
                clob_ids = json.loads(clob_ids)
            print(f"    Market: {q} | tokens={len(clob_ids)} | closed={m.get('closed')}")

print("\n=== METHOD 4: CLOB API markets search ===")
try:
    r = requests.get("https://clob.polymarket.com/markets", timeout=10)
    clob_markets = r.json()
    if isinstance(clob_markets, dict):
        clob_markets = clob_markets.get("data", clob_markets.get("markets", []))
    found = 0
    for m in clob_markets:
        q = str(m.get("question", m.get("description", ""))).lower()
        if "15 min" in q or "up or down" in q or "bitcoin" in q:
            print(f"  CLOB: {m}")
            found += 1
            if found > 5:
                break
except Exception as e:
    print(f"  CLOB search error: {e}")

print("\n=== METHOD 5: Direct URL-based search ===")
for tag in ["crypto", "15M", "crypto/15M"]:
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events",
            params={"active": "true", "closed": "false", "tag": tag, "limit": 20}, timeout=10)
        data = r.json()
        if data:
            print(f"\n  Tag '{tag}': {len(data)} events")
            for ev in data[:5]:
                print(f"    {ev.get('title', 'no title')}")
    except:
        pass
