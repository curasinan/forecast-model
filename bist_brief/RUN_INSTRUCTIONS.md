# BIST 100 Bülteni — Otomatik Görev Talimatı

## Çalıştırma
```bash
BD=$(ls -d /sessions/*/mnt/new_work/bist_brief | head -1)
cd $BD
python3 -c "import yfinance,matplotlib,PIL" 2>/dev/null || \
  pip install yfinance matplotlib pillow --break-system-packages -q
python3 report.py <MODE> $BD    # MODE = preopen | postclose
```

## Üretilen dosyalar (hepsi bist_brief/ içine)
| Dosya | İçerik |
|---|---|
| `email.html` | Gmail taslağı için htmlBody |
| `email.txt` | Düz metin fallback (body) |
| `bist_panel.html` | Sıralanabilir/filtrelenebilir interaktif panel |
| `bist_data.csv` | 100 hissenin tüm metrikleri |
| `meta.json` | Konu satırı, sinyal listeleri, özet |

## Sonraki adım
1. `meta.json` içindeki `subject` alanını e-posta konusu yap.
2. `email.html` içeriğini `htmlBody`, `email.txt` içeriğini `body` olarak
   Gmail `create_draft` çağrısına ver. Alıcı: curasinan9@gmail.com
3. **Taslak oluştur, GÖNDERME.**
4. Kısa bir onay notu yaz: tarih, XU100 kapanışı ve değişimi, genişlik,
   tetiklenen sinyal sayıları, taslak ID.

## Kurallar
- Fiyat/hacim/volatilite verisi YALNIZCA yfinance OHLCV'den gelir. Haber
  sitelerinden fiyat alma. WebSearch sadece haber/katalizör içindir, sayı için değil.
- Script çökerse hatayı bildir; uydurma sayı ile devam etme.
- Veri tarihi (`last_bar`) bugünden eskiyse bunu açıkça belirt (tatil/veri gecikmesi).
