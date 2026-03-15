# Polymarket Auto-Research Program

## Görev
Sen bir otonom araştırma agentisin. Görevin Polymarket'te Bitcoin 5-dakikalık 
up/down piyasasında arbitraj stratejisini otomatik olarak geliştirmek.

## Nasıl Çalışırsın
Her deney döngüsünde şunları yaparsın:
1. `strategy.py` içindeki CONFIG parametrelerini değiştir
2. `python run_experiment.py` çalıştır (dry mode, 60 saniye)
3. `python evaluate.py` çalıştır, skoru al
4. Skor iyileştiyse git commit at ve sonraki deneye geç
5. Skor kötüleştiyse `git checkout strategy.py` ile geri al
6. Sonucu `history/` klasörüne yaz
7. Döngüyü tekrarla

## Deney Parametreleri (strategy.py CONFIG içinde)
Sadece şu parametreleri değiştir:
- `spread_threshold`: 0.005 ile 0.05 arası (arbitraj eşiği)
- `min_edge`: 0.003 ile 0.02 arası (minimum edge)
- `max_trades_per_window`: 1 ile 5 arası
- `price_check_interval`: 5 ile 30 saniye arası
- `require_both_sides`: True veya False

## Deney Hipotezleri (Sırayla Dene)
### Faz 1 - Spread Filtreleri
- spread_threshold değerini küçülterek daha seçici ol
- spread_threshold değerini büyülterek daha fazla fırsat yak

### Faz 2 - Edge Filtreleri  
- min_edge artır: sadece büyük edge'leri al
- min_edge azalt: küçük edge'leri de yakala

### Faz 3 - Asimetri Filtreleri
- up_bias: up tarafı daha ucuzsa öncelik ver
- down_bias: down tarafı daha ucuzsa öncelik ver

### Faz 4 - Zamanlama Filtreleri
- price_check_interval değiştir
- window başlangıcında mı sona yakın mı işlem yap

## Değerlendirme Metrikleri
```
fill_rate = dolan_trade / toplam_firsat
win_rate  = kazanılan / toplam_trade  (arbitrajda her zaman 1.0 olmalı)
avg_edge  = ortalama (1.0 - toplam_maliyet)
score     = fill_rate * avg_edge * 100
```

Yüksek score = iyi strateji.

## Karar Kuralları
- score > best_score → KEEP (git commit at)
- score <= best_score → DISCARD (git checkout strategy.py)
- score > best_score * 1.5 → CONFIRM (bir daha test et, sonra commit)

## Git Kuralları
Her commit mesajı şu formatta olsun:
```
exp_{N}: {parametre_degisikligi} → score={skor:.4f} ({KEEP/DISCARD})
```
Örnek: `exp_3: spread_threshold=0.015 → score=0.0234 (KEEP)`

## Deney Geçmişi
Her deney sonrası `history/exp_{N}.json` dosyası oluştur:
```json
{
  "experiment": 3,
  "params": {"spread_threshold": 0.015},
  "score": 0.0234,
  "fill_rate": 0.45,
  "avg_edge": 0.052,
  "trades": 4,
  "decision": "KEEP"
}
```

## Başlangıç
İlk mesajı alınca:
1. Mevcut best_score'u `history/` klasöründen oku (yoksa 0 kabul et)
2. Bir sonraki mantıklı hipotezi seç
3. strategy.py'yi güncelle
4. Deneyi başlat
