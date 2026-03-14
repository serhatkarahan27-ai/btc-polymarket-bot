"""
Forward Test: Live CLOB API + Binance ile tek trade simulasyonu
Best config: SL=0.40, TP=0.60, momentum, entry=0.48
"""
import time
import json
import math
import requests
from datetime import datetime, timezone

BINANCE_BASE = "https://api.binance.com/api/v3"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Best config from experiments
BEST_CONFIG = {
    "entry_price": 0.48,
    "stop_loss": 0.40,
    "take_profit": 0.60,
    "direction_mode": "momentum",
    "momentum_lookback": 10,
    "trade_size_usd": 5.0,
}


def get_btc_price():
    """Binance'den anlik BTC fiyati."""
    r = requests.get(f"{BINANCE_BASE}/ticker/price",
                     params={"symbol": "BTCUSDT"}, timeout=5)
    return float(r.json()["price"])


def get_btc_momentum(lookback=10):
    """Son N mumun momentum yonu."""
    r = requests.get(f"{BINANCE_BASE}/klines",
                     params={"symbol": "BTCUSDT", "interval": "1m", "limit": lookback + 1},
                     timeout=5)
    klines = r.json()
    closes = [float(k[4]) for k in klines]
    if len(closes) < 2:
        return "UP", 0.0, closes
    move = closes[-1] - closes[0]
    move_pct = move / closes[0] * 100
    direction = "UP" if move >= 0 else "DOWN"
    return direction, move_pct, closes


def find_live_market():
    """Aktif 15-dk BTC marketini bul (birden fazla slug dene)."""
    now_ts = int(time.time())
    for offset in range(0, 5):
        block = ((now_ts // 900) + offset) * 900
        slug = f"btc-updown-15m-{block}"
        try:
            r = requests.get(f"{GAMMA_BASE}/markets/slug/{slug}", timeout=10)
            if r.status_code == 200:
                m = r.json()
                if not m.get("closed", True):
                    clob_ids = m.get("clobTokenIds", "[]")
                    if isinstance(clob_ids, str):
                        clob_ids = json.loads(clob_ids)
                    outcomes = m.get("outcomes", "[]")
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)
                    prices = m.get("outcomePrices", "[]")
                    if isinstance(prices, str):
                        prices = json.loads(prices)
                    secs_to_end = (block + 900) - now_ts
                    return {
                        "question": m.get("question", ""),
                        "slug": slug,
                        "token_ids": clob_ids,
                        "outcomes": outcomes,
                        "mid_prices": prices,
                        "end_block": block + 900,
                        "secs_left": secs_to_end,
                    }
        except Exception as e:
            print(f"  slug {slug} hata: {e}")
    return None


def get_full_orderbook(token_id):
    """CLOB API'den tam orderbook cek."""
    try:
        r = requests.get(f"{CLOB_BASE}/book",
                         params={"token_id": token_id}, timeout=5)
        book = r.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        return {
            "bids": [{"price": float(b["price"]), "size": float(b["size"])} for b in bids[:5]],
            "asks": [{"price": float(a["price"]), "size": float(a["size"])} for a in asks[:5]],
            "best_bid": float(bids[0]["price"]) if bids else None,
            "best_bid_size": float(bids[0]["size"]) if bids else None,
            "best_ask": float(asks[0]["price"]) if asks else None,
            "best_ask_size": float(asks[0]["size"]) if asks else None,
            "last_trade": book.get("last_trade_price"),
            "spread": round(float(asks[0]["price"]) - float(bids[0]["price"]), 4) if bids and asks else None,
            "bid_depth": len(bids),
            "ask_depth": len(asks),
        }
    except Exception as e:
        return {"error": str(e)}


def run_forward_test():
    print(f"\n{'='*65}")
    print(f"  FORWARD TEST - Live CLOB API + Binance")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*65}")

    # 1. BTC Price + Momentum
    print(f"\n[1/4] Binance BTC/USDT...")
    btc_price = get_btc_price()
    direction, mom_pct, closes = get_btc_momentum(BEST_CONFIG["momentum_lookback"])
    print(f"  BTC Price:     ${btc_price:,.2f}")
    print(f"  10-bar Mom:    {mom_pct:+.4f}%")
    print(f"  Son 5 close:   {[f'${c:,.2f}' for c in closes[-5:]]}")
    print(f"  SINYAL:        {direction}")

    # 2. Polymarket Market
    print(f"\n[2/4] Polymarket 15-dk BTC marketi araniyor...")
    market = find_live_market()
    if not market:
        print("  HATA: Aktif market bulunamadi!")
        print("  Slug paterni: btc-updown-15m-{block}")
        # Try listing what's there
        now_ts = int(time.time())
        print(f"  Denenen bloklar:")
        for off in range(0, 5):
            block = ((now_ts // 900) + off) * 900
            slug = f"btc-updown-15m-{block}"
            print(f"    {slug} (starts in {(block - now_ts)//60}m {(block-now_ts)%60}s)")
        return

    print(f"  BULUNDU: {market['question']}")
    print(f"  Slug:    {market['slug']}")
    print(f"  Kalan:   {market['secs_left']//60}dk {market['secs_left']%60}s")
    print(f"  Mid:     UP={market['mid_prices'][0] if len(market['mid_prices'])>0 else '?'}"
          f"  DOWN={market['mid_prices'][1] if len(market['mid_prices'])>1 else '?'}")

    # 3. CLOB Orderbooks
    print(f"\n[3/4] CLOB API Orderbook...")
    if len(market["token_ids"]) < 2:
        print("  HATA: Token ID'ler eksik!")
        return

    up_book = get_full_orderbook(market["token_ids"][0])
    down_book = get_full_orderbook(market["token_ids"][1])

    if "error" in up_book or "error" in down_book:
        print(f"  UP error: {up_book.get('error','ok')}")
        print(f"  DOWN error: {down_book.get('error','ok')}")
        return

    print(f"\n  {'UP TOKEN ORDERBOOK':^40}")
    print(f"  {'-'*40}")
    print(f"  {'BIDS (buy)':^20} | {'ASKS (sell)':^20}")
    for i in range(max(len(up_book['bids']), len(up_book['asks']))):
        bid_str = f"${up_book['bids'][i]['price']:.3f} x {up_book['bids'][i]['size']:.0f}" if i < len(up_book['bids']) else ""
        ask_str = f"${up_book['asks'][i]['price']:.3f} x {up_book['asks'][i]['size']:.0f}" if i < len(up_book['asks']) else ""
        print(f"  {bid_str:>20} | {ask_str:<20}")
    print(f"  Spread: ${up_book['spread']:.3f}" if up_book['spread'] else "  Spread: N/A")
    print(f"  Last trade: {up_book['last_trade']}")

    print(f"\n  {'DOWN TOKEN ORDERBOOK':^40}")
    print(f"  {'-'*40}")
    print(f"  {'BIDS (buy)':^20} | {'ASKS (sell)':^20}")
    for i in range(max(len(down_book['bids']), len(down_book['asks']))):
        bid_str = f"${down_book['bids'][i]['price']:.3f} x {down_book['bids'][i]['size']:.0f}" if i < len(down_book['bids']) else ""
        ask_str = f"${down_book['asks'][i]['price']:.3f} x {down_book['asks'][i]['size']:.0f}" if i < len(down_book['asks']) else ""
        print(f"  {bid_str:>20} | {ask_str:<20}")
    print(f"  Spread: ${down_book['spread']:.3f}" if down_book['spread'] else "  Spread: N/A")
    print(f"  Last trade: {down_book['last_trade']}")

    # 4. Simulated Trade
    print(f"\n[4/4] DRY MODE TRADE SIMULASYONU")
    print(f"  {'='*50}")

    cfg = BEST_CONFIG
    trade_size = cfg["trade_size_usd"]

    # Hangi token'i aliyoruz?
    if direction == "UP":
        our_token = "UP"
        entry_ask = up_book["best_ask"]
        entry_ask_size = up_book["best_ask_size"]
        our_book = up_book
    else:
        our_token = "DOWN"
        entry_ask = down_book["best_ask"]
        entry_ask_size = down_book["best_ask_size"]
        our_book = down_book

    if entry_ask is None:
        print("  HATA: Ask fiyati yok, trade yapilamaz!")
        return

    # Entry analizi
    tokens_to_buy = trade_size / entry_ask
    sl_price = cfg["stop_loss"]
    tp_price = cfg["take_profit"]

    # P&L senaryolari
    sl_loss = (sl_price - entry_ask) * tokens_to_buy
    tp_profit = (tp_price - entry_ask) * tokens_to_buy
    win_payout = (1.00 - entry_ask) * tokens_to_buy  # Token 1.00'a resolve olursa
    lose_payout = (0.00 - entry_ask) * tokens_to_buy  # Token 0.00'a resolve olursa

    print(f"\n  TRADE DETAYI:")
    print(f"  ---------------------------------------------")
    print(f"  Sinyal:          {direction} (momentum {mom_pct:+.4f}%)")
    print(f"  Token:           {our_token}")
    print(f"  Market:          {market['question']}")
    print(f"  Kalan sure:      {market['secs_left']//60}dk {market['secs_left']%60}s")
    print(f"  ---------------------------------------------")
    print(f"  Entry Ask:       ${entry_ask:.3f}")
    print(f"  Ask Likidite:    {entry_ask_size:.0f} token")
    print(f"  Trade Size:      ${trade_size:.2f}")
    print(f"  Tokens:          {tokens_to_buy:.2f}")
    print(f"  ---------------------------------------------")
    print(f"  STOP-LOSS:       ${sl_price:.2f}  ->  PnL = ${sl_loss:+.2f} ({sl_loss/trade_size*100:+.1f}%)")
    print(f"  TAKE-PROFIT:     ${tp_price:.2f}  ->  PnL = ${tp_profit:+.2f} ({tp_profit/trade_size*100:+.1f}%)")
    print(f"  WIN (resolve 1): $1.00  ->  PnL = ${win_payout:+.2f} ({win_payout/trade_size*100:+.1f}%)")
    print(f"  LOSE (resolve 0):$0.00  ->  PnL = ${lose_payout:+.2f} ({lose_payout/trade_size*100:+.1f}%)")
    print(f"  ---------------------------------------------")

    # Risk/Reward
    risk = abs(entry_ask - sl_price)
    reward = abs(tp_price - entry_ask)
    rr = reward / risk if risk > 0 else float('inf')
    print(f"  Risk:            ${risk:.3f} per token")
    print(f"  Reward:          ${reward:.3f} per token")
    print(f"  Risk/Reward:     1:{rr:.2f}")

    # Breakeven win rate needed
    be_wr = risk / (risk + reward) if (risk + reward) > 0 else 0.5
    print(f"  Breakeven WR:    {be_wr:.1%}")
    print(f"  Our backtest WR: 63.1%")
    print(f"  Edge:            {0.631 - be_wr:.1%}")
    print(f"  {'='*50}")

    # Save forward test result
    result = {
        "timestamp": datetime.now().isoformat(),
        "btc_price": btc_price,
        "momentum_pct": round(mom_pct, 4),
        "direction": direction,
        "market": market["question"],
        "slug": market["slug"],
        "secs_left": market["secs_left"],
        "entry_ask": entry_ask,
        "entry_ask_size": entry_ask_size,
        "token": our_token,
        "trade_size_usd": trade_size,
        "tokens_to_buy": round(tokens_to_buy, 4),
        "stop_loss": sl_price,
        "take_profit": tp_price,
        "sl_pnl": round(sl_loss, 4),
        "tp_pnl": round(tp_profit, 4),
        "win_pnl": round(win_payout, 4),
        "lose_pnl": round(lose_payout, 4),
        "risk_reward": round(rr, 4),
        "breakeven_wr": round(be_wr, 4),
        "up_orderbook": up_book,
        "down_orderbook": down_book,
        "config": cfg,
    }

    with open("forward_test.json", "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  Sonuc forward_test.json'a kaydedildi.")

    return result


if __name__ == "__main__":
    run_forward_test()
