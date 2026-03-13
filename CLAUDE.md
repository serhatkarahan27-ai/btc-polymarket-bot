# Polymarket Auto-Research Bot

## Proje Nedir
Polymarket BTC 5-dakikalık up/down piyasasında arbitraj stratejisi geliştiren
otonom araştırma sistemi. Karpathy'nin autoresearch loop mantığından ilham alındı.

## Dosyalar
- `strategy.py` → Ana arbitraj botu. **Sadece CONFIG bloğunu değiştir.**
- `evaluate.py` → Deney sonucunu değerlendirir, history/ klasörüne yazar.
- `program.md` → Deney talimatları ve hipotezler. **Okumak zorunlu.**
- `last_result.json` → Son deney sonucu (otomatik oluşur)
- `history/` → Tüm deney geçmişi

## Çalıştırma
```bash
# Bir deney çalıştır
python strategy.py

# Değerlendir
python evaluate.py
```

## Kurallar
1. `dry_mode` her zaman `True` olmalı
2. Sadece `strategy.py` içindeki CONFIG bloğunu değiştir
3. Her deney sonrası git commit at (KEEP ise) veya revert et (DISCARD ise)
4. `program.md` talimatlarını takip et

## Git Commit Formatı
```
exp_{N}: {değişiklik} → score={skor:.4f} ({KEEP/DISCARD})
```
