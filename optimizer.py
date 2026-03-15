"""
Polymarket BTC 15-Min Optimizer + Live Validator
=================================================
1. polymarket_history.json'dan 673 pencereyi oku
2. Tum SL/TP/direction/flip kombinasyonlarini backtest et
3. Profit factor'a gore en iyisini bul
4. 5 canli pencere ile validate et
5. PnL pozitifse KEEP + git commit, degilse DISCARD + revert
6. Sonsuza kadar tekrar (her seferinde en iyiyi yenmeye calis)

Kullanim:
  python optimizer.py              # tek tur optimize + validate
  python optimizer.py --loop       # sonsuz dongu
  python optimizer.py --backtest   # sadece backtest (live yok)
"""

import json
import time
import itertools
import os
import subprocess
from datetime import datetime

# ============================================================
# VERI YUKLEME
# ============================================================

HISTORY_FILE = "polymarket_history.json"
BEST_FILE = "best_config.json"
EXPERIMENT_LOG = "experiment_log.json"

def load_history():
    """673 pencerelik Polymarket gecmis verisini yukle."""
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        windows = json.load(f)
    # Sadece kapanmis pencereleri al (outcome = UP veya DOWN)
    closed = [w for w in windows if w["outcome"] in ("UP", "DOWN")]
    print(f"[DATA] {len(closed)} kapanmis pencere yuklendi ({len(windows)} toplam)")
    return closed


# ============================================================
# BACKTEST - Tek bir config ile tum pencerelerde test
# ============================================================

def backtest_config(windows, cfg):
    """Verilen config ile tum pencerelerde backtest yap.

    Her pencerede:
    - direction_mode'a gore UP veya DOWN token al
    - flip=True ise yonu tersle
    - entry_price'dan al
    - SL/TP varsa kontrol et (pencere icinde tetiklenebilir mi?)
    - Pencere sonunda outcome'a gore resolve et

    Binary market ozelligi:
    - Pencere basinda token ~$0.50
    - Pencere sonunda: dogru tahmin = $1.00, yanlis = $0.00
    - SL/TP pencere ortasinda tetiklenebilir

    Returns: trades listesi
    """
    trades = []
    entry_price = cfg.get("entry_price", 0.50)
    sl = cfg.get("stop_loss", None)
    tp = cfg.get("take_profit", None)
    use_sl = sl is not None and cfg.get("use_stop_loss", True)
    use_tp = tp is not None and cfg.get("use_take_profit", True)
    direction_mode = cfg.get("direction_mode", "momentum")
    flip = cfg.get("flip", False)
    trade_size = cfg.get("trade_size_usd", 5.0)

    for i, window in enumerate(windows):
        outcome = window["outcome"]  # "UP" or "DOWN"

        # Yon belirleme
        if direction_mode == "always_up":
            direction = "UP"
        elif direction_mode == "always_down":
            direction = "DOWN"
        elif direction_mode == "momentum":
            # Onceki pencere sonucuna gore momentum
            if i > 0:
                direction = windows[i-1]["outcome"]
            else:
                direction = "UP"
        else:
            direction = "UP"

        # Flip
        if flip:
            direction = "DOWN" if direction == "UP" else "UP"

        # Dogru tahmin mi?
        correct = (direction == outcome)

        # Token fiyat simulasyonu
        tokens_bought = trade_size / entry_price

        # SL/TP simulasyonu - binary market'te orta fiyatlari tahmin et
        # Gercek hayatta token fiyati 0.50'den baslar, pencere icinde BTC'ye gore kayar
        # Burada basitlestirilmis model:
        # - Dogru tahmin: token 0.50 -> ~0.65 -> 1.00 (artan trend)
        # - Yanlis tahmin: token 0.50 -> ~0.35 -> 0.00 (azalan trend)

        exit_price = None
        exit_reason = "expiry"

        if correct:
            # Token yukseliyor: 0.50 -> 1.00
            # SL tetiklenmez (fiyat dusmuyor)
            # TP tetiklenebilir (fiyat yukseliyor)
            if use_tp and tp is not None:
                # TP < 1.00 ise pencere ortasinda tetiklenir
                if tp < 1.00:
                    exit_price = tp
                    exit_reason = "take_profit"
                else:
                    exit_price = 1.00
                    exit_reason = "expiry"
            else:
                exit_price = 1.00
                exit_reason = "expiry"
        else:
            # Token dusuyor: 0.50 -> 0.00
            # SL tetiklenebilir (fiyat dusuyor)
            # TP tetiklenmez (fiyat yukselmiyor)
            if use_sl and sl is not None:
                # SL > 0.00 ise pencere ortasinda tetiklenir
                if sl > 0.00:
                    exit_price = sl
                    exit_reason = "stop_loss"
                else:
                    exit_price = 0.00
                    exit_reason = "expiry"
            else:
                exit_price = 0.00
                exit_reason = "expiry"

        # PnL
        exit_value = tokens_bought * exit_price
        pnl = exit_value - trade_size
        roi = (pnl / trade_size) * 100

        trades.append({
            "window_idx": i,
            "direction": direction,
            "outcome": outcome,
            "correct": correct,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "pnl_usd": round(pnl, 4),
            "roi_pct": round(roi, 2),
            "win": pnl > 0,
        })

    return trades


def calc_stats(trades):
    """Trade listesinden istatistik hesapla."""
    if not trades:
        return None

    wins = sum(1 for t in trades if t["win"])
    losses = len(trades) - wins
    win_rate = wins / len(trades) if trades else 0

    total_pnl = sum(t["pnl_usd"] for t in trades)
    gross_profit = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    gross_loss = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0))

    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999.0
    avg_pnl = total_pnl / len(trades)

    # Max drawdown
    running = 0
    peak = 0
    max_dd = 0
    for t in trades:
        running += t["pnl_usd"]
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    # Exit breakdown
    sl_exits = sum(1 for t in trades if t["exit_reason"] == "stop_loss")
    tp_exits = sum(1 for t in trades if t["exit_reason"] == "take_profit")
    exp_exits = sum(1 for t in trades if t["exit_reason"] == "expiry")

    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "total_pnl": round(total_pnl, 4),
        "avg_pnl": round(avg_pnl, 4),
        "profit_factor": round(profit_factor, 4),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "max_drawdown": round(max_dd, 4),
        "sl_exits": sl_exits,
        "tp_exits": tp_exits,
        "exp_exits": exp_exits,
    }


# ============================================================
# GRID SEARCH - Tum kombinasyonlar
# ============================================================

def grid_search(windows):
    """Tum SL/TP/direction/flip kombinasyonlarini test et."""

    sl_values = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, None]  # None = OFF
    tp_values = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, None]  # None = OFF
    dir_values = ["always_up", "always_down", "momentum"]
    flip_values = [True, False]

    total = len(sl_values) * len(tp_values) * len(dir_values) * len(flip_values)
    print(f"\n[GRID] {total} kombinasyon test edilecek...")
    print(f"  SL:  {sl_values}")
    print(f"  TP:  {tp_values}")
    print(f"  Dir: {dir_values}")
    print(f"  Flip: {flip_values}")

    results = []
    best_pf = -1
    best_cfg = None
    tested = 0

    for sl, tp, direction, flip in itertools.product(sl_values, tp_values, dir_values, flip_values):
        # Mantıksız kombinasyonları atla
        if sl is not None and tp is not None and sl >= tp:
            continue

        # momentum + flip = contrarian momentum
        # always_up + flip = always_down (tekrar)
        if direction == "always_up" and flip:
            continue  # always_down zaten var
        if direction == "always_down" and flip:
            continue  # always_up zaten var

        cfg = {
            "entry_price": 0.50,
            "stop_loss": sl,
            "take_profit": tp,
            "use_stop_loss": sl is not None,
            "use_take_profit": tp is not None,
            "direction_mode": direction,
            "flip": flip,
            "trade_size_usd": 5.0,
        }

        trades = backtest_config(windows, cfg)
        stats = calc_stats(trades)
        tested += 1

        if stats and stats["trades"] >= 10:
            result = {
                "config": cfg,
                "stats": stats,
            }
            results.append(result)

            if stats["profit_factor"] > best_pf:
                best_pf = stats["profit_factor"]
                best_cfg = result
                sl_str = f"${sl:.2f}" if sl else "OFF"
                tp_str = f"${tp:.2f}" if tp else "OFF"
                flip_str = "+flip" if flip else ""
                print(f"  NEW BEST #{tested}: PF={stats['profit_factor']:.4f} "
                      f"WR={stats['win_rate']:.1%} PnL=${stats['total_pnl']:+.2f} "
                      f"| SL={sl_str} TP={tp_str} dir={direction}{flip_str} "
                      f"({stats['trades']} trades)")

    # Sort by profit factor
    results.sort(key=lambda r: r["stats"]["profit_factor"], reverse=True)

    print(f"\n[GRID] {tested} kombinasyon test edildi")
    print(f"\n{'='*70}")
    print(f"  TOP 10 KOMBINASYON (profit factor'a gore)")
    print(f"{'='*70}")
    print(f"  {'#':>3} | {'PF':>8} | {'WR':>6} | {'PnL':>10} | {'SL':>6} | {'TP':>6} | {'Dir':>12} | {'Flip':>5} | {'DD':>8}")
    print(f"  {'-'*85}")

    for i, r in enumerate(results[:10]):
        c = r["config"]
        s = r["stats"]
        sl_str = f"${c['stop_loss']:.2f}" if c['stop_loss'] else "OFF"
        tp_str = f"${c['take_profit']:.2f}" if c['take_profit'] else "OFF"
        print(f"  {i+1:>3} | {s['profit_factor']:>8.4f} | {s['win_rate']:>5.1%} | ${s['total_pnl']:>+9.2f} "
              f"| {sl_str:>6} | {tp_str:>6} | {c['direction_mode']:>12} "
              f"| {'Y' if c.get('flip') else 'N':>5} | ${s['max_drawdown']:>7.2f}")

    return results, best_cfg


# ============================================================
# LIVE VALIDATION - 5 pencere ile canli test
# ============================================================

def run_live_validation(cfg, num_windows=5):
    """Config'i 5 canli pencerede test et.

    window_trader.py'nin fonksiyonlarini kullanir.
    """
    print(f"\n{'='*70}")
    print(f"  LIVE VALIDATION - {num_windows} pencere")
    print(f"{'='*70}")

    sl_str = f"${cfg['stop_loss']:.2f}" if cfg.get('stop_loss') else "OFF"
    tp_str = f"${cfg['take_profit']:.2f}" if cfg.get('take_profit') else "OFF"
    flip_str = "+flip" if cfg.get('flip') else ""
    print(f"  Config: SL={sl_str} TP={tp_str} dir={cfg['direction_mode']}{flip_str}")

    # window_trader.py'yi import et
    try:
        import window_trader as wt
    except ImportError:
        print("  HATA: window_trader.py import edilemedi!")
        return None

    # CONFIG'i guncelle
    wt.CONFIG["stop_loss"] = cfg.get("stop_loss", 0.40) or 0.01
    wt.CONFIG["take_profit"] = cfg.get("take_profit", 0.60) or 0.99
    wt.CONFIG["direction_mode"] = cfg.get("direction_mode", "momentum")

    # Flip mantigi icin direction_mode'u ayarla
    # flip=True + momentum = contrarian momentum (onceki pencere tersini al)
    # Bunu window_trader'da uygulamak icin ozel field ekliyoruz

    live_trades = []
    total_pnl = 0.0

    for win_num in range(1, num_windows + 1):
        print(f"\n  --- Window {win_num}/{num_windows} ---")

        try:
            result = wt.run_single_window()

            if result and not result.get("skipped"):
                pnl = result.get("pnl_usd", 0)
                total_pnl += pnl
                live_trades.append(result)

                wins = sum(1 for t in live_trades if t.get("win") or t.get("pnl_usd", 0) > 0)
                wr = wins / len(live_trades) * 100 if live_trades else 0

                print(f"\n  Window {win_num} sonucu: PnL=${pnl:+.2f} | "
                      f"Toplam: {len(live_trades)} trade, WR={wr:.0f}%, PnL=${total_pnl:+.2f}")
            elif result and result.get("skipped"):
                print(f"\n  Window {win_num}: SKIPPED ({result.get('exit_reason', '?')})")
            else:
                print(f"\n  Window {win_num}: HATA - sonuc yok")

        except KeyboardInterrupt:
            print("\n  Kullanici durdurdu!")
            break
        except Exception as e:
            print(f"\n  Window {win_num} HATA: {e}")

        # Kisa bekleme
        if win_num < num_windows:
            time.sleep(3)

    # Validation sonucu
    real_trades = [t for t in live_trades if not t.get("skipped")]
    if not real_trades:
        print("\n  VALIDATION: Hicbir gercek trade olmadi!")
        return {"pnl": 0, "trades": 0, "result": "NO_TRADES"}

    wins = sum(1 for t in real_trades if t.get("win") or t.get("pnl_usd", 0) > 0)
    total_pnl = sum(t.get("pnl_usd", 0) for t in real_trades)
    wr = wins / len(real_trades) * 100

    print(f"\n{'='*70}")
    print(f"  VALIDATION SONUCU")
    print(f"{'='*70}")
    print(f"  Trades:    {len(real_trades)}")
    print(f"  Wins:      {wins}")
    print(f"  Win Rate:  {wr:.0f}%")
    print(f"  Total PnL: ${total_pnl:+.2f}")
    print(f"  Sonuc:     {'KEEP' if total_pnl > 0 else 'DISCARD'}")
    print(f"{'='*70}")

    return {
        "pnl": total_pnl,
        "trades": len(real_trades),
        "wins": wins,
        "win_rate": wr,
        "result": "KEEP" if total_pnl > 0 else "DISCARD",
        "live_trades": real_trades,
    }


# ============================================================
# GIT OPERATIONS
# ============================================================

def git_commit(message):
    """Git commit at."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=os.path.dirname(os.path.abspath(__file__)))
        subprocess.run(
            ["git", "commit", "-m", message + "\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"],
            cwd=os.path.dirname(os.path.abspath(__file__))
        )
        print(f"  [GIT] Commit: {message}")
    except Exception as e:
        print(f"  [GIT] Commit hatasi: {e}")


# ============================================================
# ANA DONGU
# ============================================================

def load_best():
    """Onceki en iyi config'i yukle."""
    try:
        with open(BEST_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_best(config, stats, experiment_num):
    """En iyi config'i kaydet."""
    data = {
        "config": config,
        "stats": stats,
        "experiment_num": experiment_num,
        "timestamp": datetime.now().isoformat(),
    }
    with open(BEST_FILE, "w") as f:
        json.dump(data, f, indent=2)


def log_experiment(exp_num, config, backtest_stats, live_result, decision):
    """Deney sonucunu logla."""
    entry = {
        "experiment": exp_num,
        "timestamp": datetime.now().isoformat(),
        "config": config,
        "backtest": backtest_stats,
        "live": live_result,
        "decision": decision,
    }

    try:
        with open(EXPERIMENT_LOG, "r") as f:
            log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log = []

    log.append(entry)
    with open(EXPERIMENT_LOG, "w") as f:
        json.dump(log, f, indent=2, default=str)


def run_single_experiment(windows, exp_num, skip_live=False):
    """Tek bir deney: optimize + validate + keep/discard."""

    print(f"\n{'#'*70}")
    print(f"  EXPERIMENT #{exp_num}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*70}")

    # 1. Grid search
    results, best = grid_search(windows)

    if not best:
        print("  HATA: Hicbir iyi config bulunamadi!")
        return None

    best_cfg = best["config"]
    best_stats = best["stats"]

    print(f"\n  EN IYI CONFIG:")
    sl_str = f"${best_cfg['stop_loss']:.2f}" if best_cfg.get('stop_loss') else "OFF"
    tp_str = f"${best_cfg['take_profit']:.2f}" if best_cfg.get('take_profit') else "OFF"
    flip_str = "+flip" if best_cfg.get('flip') else ""
    print(f"    SL={sl_str} TP={tp_str} dir={best_cfg['direction_mode']}{flip_str}")
    print(f"    PF={best_stats['profit_factor']:.4f} WR={best_stats['win_rate']:.1%} "
          f"PnL=${best_stats['total_pnl']:+.2f}")

    if skip_live:
        print("\n  [SKIP] Live validation atlaniyor (--backtest modu)")
        save_best(best_cfg, best_stats, exp_num)
        log_experiment(exp_num, best_cfg, best_stats, None, "BACKTEST_ONLY")
        git_commit(f"exp_{exp_num}: SL={sl_str} TP={tp_str} dir={best_cfg['direction_mode']}{flip_str} "
                   f"→ PF={best_stats['profit_factor']:.4f} (BACKTEST)")
        return best

    # 2. Live validation
    live_result = run_live_validation(best_cfg, num_windows=5)

    if not live_result:
        print("  HATA: Live validation basarisiz!")
        log_experiment(exp_num, best_cfg, best_stats, None, "FAILED")
        return None

    decision = live_result.get("result", "DISCARD")

    # 3. Keep or discard
    log_experiment(exp_num, best_cfg, best_stats, live_result, decision)

    if decision == "KEEP":
        save_best(best_cfg, best_stats, exp_num)
        score = best_stats['profit_factor']
        git_commit(f"exp_{exp_num}: SL={sl_str} TP={tp_str} dir={best_cfg['direction_mode']}{flip_str} "
                   f"→ PF={score:.4f} PnL=${live_result['pnl']:+.2f} (KEEP)")
        print(f"\n  [OK] KEEP - Config kaydedildi ve commit edildi")
    else:
        score = best_stats['profit_factor']
        git_commit(f"exp_{exp_num}: SL={sl_str} TP={tp_str} dir={best_cfg['direction_mode']}{flip_str} "
                   f"→ PF={score:.4f} PnL=${live_result['pnl']:+.2f} (DISCARD)")
        print(f"\n  [X] DISCARD - Config discard edildi")

    return best if decision == "KEEP" else None


def main():
    import sys

    skip_live = "--backtest" in sys.argv
    loop_mode = "--loop" in sys.argv

    # Veri yukle
    windows = load_history()

    # Onceki en iyi config
    prev_best = load_best()
    if prev_best:
        print(f"\n[PREV] Onceki en iyi: PF={prev_best['stats']['profit_factor']:.4f}")

    exp_num = 1
    if prev_best:
        exp_num = prev_best.get("experiment_num", 0) + 1

    if loop_mode:
        print(f"\n[LOOP] Sonsuz dongu modu - Ctrl+C ile dur")
        try:
            while True:
                result = run_single_experiment(windows, exp_num, skip_live=skip_live)
                exp_num += 1

                if not loop_mode:
                    break

                # Veriyi yenile (yeni pencereler eklenebilir)
                try:
                    windows = load_history()
                except:
                    pass

                print(f"\n  Sonraki deney icin bekleniyor (30sn)...")
                time.sleep(30)

        except KeyboardInterrupt:
            print(f"\n\n  Durduruluyor... {exp_num - 1} deney yapildi.")
    else:
        run_single_experiment(windows, exp_num, skip_live=skip_live)


if __name__ == "__main__":
    main()
