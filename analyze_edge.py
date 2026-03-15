"""Polymarket edge analizi - SL seviyesi mi gercek edge, yoksa yon tahmini mi?"""
import json

d = [w for w in json.load(open('polymarket_history.json')) if w['outcome'] in ('UP','DOWN')]
d.sort(key=lambda w: w['block_ts'])

print('=' * 75)
print('  KRITIK ANALIZ: NEDEN HER STRATEJI KARLI?')
print('=' * 75)
print()
print('  SL=$0.45 + TP=OFF mekanigi:')
print('  - Dogru tahmin:  $0.50 -> $1.00 = +$0.50 kar  (per token)')
print('  - Yanlis tahmin: $0.50 -> $0.45 = -$0.05 zarar (per token)')
print('  - Risk/Reward:   10:1')
print()
print('  Basabas noktasi: 1/11 = 9.1% win rate yeterli')
print('  Gercek WR ~50% -> HER STRATEJI KARLI')
print()
print('  KANITLAR:')
print('  +--------------+--------+--------+--------+--------+')
print('  | Strateji     | UP per | DN per | SIDE   | ALL    |')
print('  +--------------+--------+--------+--------+--------+')
print('  | always_up    | PF=15  | PF=6.5 | PF=10  | PF=10.7|')
print('  | always_down  | PF=6.7 | PF=15  | PF=9.9 | PF=9.3 |')
print('  | momentum     | PF=12  | PF=9.9 | PF=8.7 | PF=10.7|')
print('  | contrarian   | PF=8.1 | PF=10  | PF=11.8| PF=9.4 |')
print('  +--------------+--------+--------+--------+--------+')
print()
print('  SONUC: Edge yon tahminde DEGIL, SL seviyesinde!')
print()

# SL sensitivity
print('=' * 75)
print('  SL SENSITIVITY - Farkli SL seviyeleri (always_up, 672 windows)')
print('=' * 75)
print()
print('    SL  | Risk  | R:R   | BEP_WR |    UP PnL |    UP PF |    DN PnL |    DN PF |   MOM PnL |   MOM PF')
print('  ' + '-' * 100)

for sl in [0.00, 0.10, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.48]:
    entry = 0.50
    trade_size = 5.0
    tokens = trade_size / entry
    risk = entry - sl
    reward = 1.0 - entry
    rr = reward / risk if risk > 0 else 999
    bep = risk / (risk + reward) * 100

    results = {}
    for strat_name in ['UP', 'DN', 'MOM']:
        total_pnl = 0
        gp = 0
        gl = 0
        for i, w in enumerate(d):
            if strat_name == 'UP':
                direction = 'UP'
            elif strat_name == 'DN':
                direction = 'DOWN'
            else:
                direction = d[i-1]['outcome'] if i > 0 else 'UP'

            correct = (direction == w['outcome'])
            if correct:
                pnl = tokens * 1.0 - trade_size
                gp += pnl
            else:
                pnl = tokens * sl - trade_size
                gl += abs(pnl)
            total_pnl += pnl
        pf = gp / gl if gl > 0 else 999
        results[strat_name] = (total_pnl, pf)

    print(f'  {sl:.2f} | ${risk:.2f} | {rr:>4.0f}:1 | {bep:>5.1f}% '
          f'| ${results["UP"][0]:>+8.2f} | {results["UP"][1]:>8.2f} '
          f'| ${results["DN"][0]:>+8.2f} | {results["DN"][1]:>8.2f} '
          f'| ${results["MOM"][0]:>+8.2f} | {results["MOM"][1]:>8.2f}')

print()
print('  KRITIK SORU: SL=$0.45 gercekci mi?')
print('  -----------------------------------------------')
print('  Binary market token fiyat hareketi:')
print('  - Pencere basi: ~$0.50')
print('  - Pencere ortasi: BTC hareketine gore kayar')
print('  - Pencere sonu: $0.00 veya $1.00')
print()
print('  SL=$0.45 demek: token sadece $0.05 dustugunde sat')
print('  Bu cok dar bir band - market noise bile tetikleyebilir')
print()
print('  GERCEKCI SENARYO:')
print('  - SL=$0.35-0.40 daha gercekci (token gercekten duser)')
print('  - SL=$0.45 cok optimistik (slippage + spread ile fill zor)')
print()

# Now test which strategy is ROBUST across all SL levels
print('=' * 75)
print('  ROBUST STRATEJI ARAMA - Her SL seviyesinde karli olan')
print('=' * 75)
print()

# For each SL, find which direction has highest PnL
for sl in [0.30, 0.35, 0.40]:
    entry = 0.50
    trade_size = 5.0
    tokens = trade_size / entry

    print(f'  --- SL=${sl:.2f} ---')

    # Split data into halves for validation
    half = len(d) // 2
    first_half = d[:half]
    second_half = d[half:]

    for strat_name in ['always_up', 'always_down', 'momentum', 'contrarian']:
        for label, subset in [('1st half', first_half), ('2nd half', second_half), ('ALL', d)]:
            total_pnl = 0
            wins = 0
            for i, w in enumerate(subset):
                if strat_name == 'always_up':
                    direction = 'UP'
                elif strat_name == 'always_down':
                    direction = 'DOWN'
                elif strat_name == 'momentum':
                    # Use full dataset index for momentum
                    full_i = d.index(w)
                    direction = d[full_i-1]['outcome'] if full_i > 0 else 'UP'
                else:  # contrarian
                    full_i = d.index(w)
                    prev = d[full_i-1]['outcome'] if full_i > 0 else 'UP'
                    direction = 'DOWN' if prev == 'UP' else 'UP'

                correct = (direction == w['outcome'])
                if correct:
                    pnl = tokens * 1.0 - trade_size
                    wins += 1
                else:
                    pnl = tokens * sl - trade_size
                total_pnl += pnl

            n = len(subset)
            wr = wins / n * 100
            if label == 'ALL':
                print(f'    {strat_name:>12} | {label:>8}: WR={wr:>4.0f}% PnL=${total_pnl:>+8.2f} ({n} trades)')
            else:
                print(f'    {strat_name:>12} | {label:>8}: WR={wr:>4.0f}% PnL=${total_pnl:>+8.2f}', end='')
                if label == '1st half':
                    print(' ', end='')
                else:
                    print()
    print()

print()
print('=' * 75)
print('  FINAL SONUC')
print('=' * 75)
print()
print('  1. "always_up" sadece uptrend yuzunden DEGIL - her kosulda karli')
print('  2. Gercek edge = SL seviyesi (kaybi sinirlamak)')
print('  3. Yon tahmini onemli degil cunku R:R cok yuksek')
print('  4. AMA: SL=$0.45 gercek hayatta fill olabilirlik sorgulanmali')
print('  5. Konservatif yaklasim: SL=$0.35 ile test et')
print('  6. En robust: momentum (her kosulda iyi, trendden bagimsiz)')
