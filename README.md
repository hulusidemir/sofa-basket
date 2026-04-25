# SofaScore Basket Analiz Paneli

Canlı basketbol maçlarını SofaScore'dan çekip, profesyonel bir analistin baktığı
kriterlerle (tempo, şut kalitesi, faul/FT baskısı, top kaybı, ribaund, maç
scripti, oyuncu yükü) değerlendiren ve AiScore'dan gelen **ALT/ÜST** sinyalini
doğrulamak için kullanılan bir karar **destek** panelidir.

> Bu araç bahis oynatmaz, oran çekmez, kupon yapmaz. Sadece canlı maç verisini
> analiz eder ve "bu sinyal istatistiklerle tutarlı mı?" sorusuna cevap vermeye
> çalışır.

## Özellikler

- Canlı basket maçı listesi (takım / lig filtreleme)
- Maç detayı: skor, periyot, tempo, projeksiyon
- Şut, ribaund, faul, top kaybı, asist, blok, serbest atış detayları
- **Tempo / script / şut / faul / ribaund / top kaybı** için özet kartlar
- Canlı barem girince **projeksiyon vs barem** farkı ve değer uyarısı
- Lineups (varsa): faul problemi ve tek oyuncuya bağımlılık uyarıları
- Otomatik yenileme (30s) opsiyonu
- Mobil uyumlu tek sayfalık arayüz

## Kurulum

```bash
cd sofa-basket
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Çalıştırma

```bash
python app.py
```

Varsayılan olarak `http://127.0.0.1:5050` adresinde çalışır.

Ortam değişkenleri (opsiyonel):

| Değişken      | Varsayılan | Açıklama                  |
| ------------- | ---------- | ------------------------- |
| `HOST`        | 127.0.0.1  | Flask host                |
| `PORT`        | 5050       | Flask port                |
| `FLASK_DEBUG` | 1          | Debug modu (0/1)          |
| `LOG_LEVEL`   | INFO       | Python logging seviyesi   |

## API Uçları

- `GET /` – Tek sayfa dashboard
- `GET /api/live-events` – Canlı basket maçları listesi
- `GET /api/event/<id>/analysis?line=168.5` – Seçilen maç için tam analiz;
  opsiyonel `line` parametresi canlı baremle karşılaştırma yapar.

## Analiz mantığı (kısa)

1. **Lig ve süre**: Tournament / takım isminden NBA (48 dk), NCAA Erkek (40 dk,
   2 yarı), WNBA / FIBA (40 dk) ayrımı yapılır. Tespit yetersizse uyarı verilir.
2. **Tempo ve projeksiyon**: `raw_pace = mevcut_sayı / geçen_dk`. Kalan süre için
   periyoda göre regresyon faktörü (Q1 erken 0.86 → Q4 1.00) uygulanır.
   **Süre okunamazsa projeksiyon yapılmaz**, sahte sayı üretilmez.
3. **Şut kalitesi**: Saha içi yüzdesi çok yüksekse regresyon riski (ALT yönüne
   puan). Yüzde düşük ama hacim yüksekse toparlama payı (ÜST yönüne puan).
   3P% çok yüksekse sıcak şut uyarısı.
4. **Faul / FT**: Dakika başına faul ve FT denemesi hesaplanır; yüksekse ÜST
   yönüne puan eklenir.
5. **Top kaybı**: Dakika başına top kaybı yüksekse hücum verimsizliği -> ALT.
6. **Ribaund**: Hücum ribaund oranı yüksekse ekstra pozisyon -> ÜST. Baskın
   savunma ribaundu -> tek atışta biten hücumlar -> ALT.
7. **Maç scripti**: Fark 15+ ise tempo düşme riski, garbage time uyarısı. Son
   periyot + yakın skor -> faul oyunu ihtimali -> ÜST.
8. **Oyuncu notları**: Faul problemi ve tek oyuncuya bağımlılık sürdürülebilirlik
   uyarıları olarak görünür.
9. **Karar**: ÜST ve ALT puanları toplanır, net farka göre `ÜST destekli /
   ALT destekli / Karışık / Pas / Veri yetersiz` kararı üretilir. Güven skoru
   0-100 aralığındadır.

## Canlı barem

Maç detay panelinde **Canlı barem** alanına AiScore'daki ÜST/ALT barem çizgisi
yazılırsa:

- Projeksiyon - barem farkı gösterilir.
- **+6** üstü: ÜST değerli olabilir (karara puan ekler).
- **-6** altı: ALT değerli olabilir.
- **-3 ile +3** arası: net avantaj yok.

Barem girilmezse yalnızca istatistik-tabanlı tempo analizi yapılır.

## Uyarılar

- **SofaScore API'leri resmi, belgelenmiş public API değildir.** Uç noktalar
  değişebilir, 403 dönebilir, rate limit uygulanabilir. Bu sebeple uygulama
  hataya karşı sağlam yazılmıştır: veri yoksa "Veri yok" yazar, uygulama çökmez.
- İstekler **in-memory cache** ile 15-30 saniye tutulur. Gereksiz sık
  yenilemekten kaçın; otomatik yenileme 30 saniyeden daha sık çağrılmaz.
- Bu uygulama bir **karar destek aracıdır, bahis tavsiyesi değildir.** Son karar
  her zaman kullanıcıya aittir.

## Dosya yapısı

```
sofa-basket/
├── app.py                 # Flask uygulaması ve route'lar
├── sofascore_client.py    # SofaScore HTTP istemcisi + cache
├── analyzer.py            # Analiz motoru (tempo, puanlama, karar)
├── templates/
│   └── index.html         # Tek sayfa dashboard (HTML/CSS/JS)
├── requirements.txt
└── README.md
```

## İleride opsiyonel

- Takım adı fuzzy search
- AiScore maç adı yapıştırınca SofaScore adayları önerme
- Favori maç listesi
- Lig ön filtresi
- Geçmiş maç verisi karşılaştırması (H2H + takım ortalamaları)
# sofa-basket
