# Yuklama sinovi (TZ §18, qabul mezoni 25)

Sana: 21.10.2026 · Muallif: AUTH sessiyasi · Harness: `tests/load/`

## 1. Usul

- **Haqiqiy bot kodi**: `app.bot.create_dispatcher()` — barcha routerlar va middleware'lar (throttling, DB session, user upsert, menu reset). FSM `MemoryStorage`da. Yangilanishlar `dp.feed_update()` orqali beriladi.
- **Telegram simulyatsiyasi**: `FakeTelegramSession` (aiogram `BaseSession`).
  - Har bir Bot API chaqiruviga **30–80 ms** sun'iy kechikish qo'shiladi.
  - Javob lokal qaytariladi, chaqiruv yozib olinadi.
  - Token soxta (`123456789:LOAD-…`). Haqiqiy API'ga **hech qanday so'rov ketmaydi**: skript real token bilan ishga tushishni rad etadi.
- **Alohida baza**: `xyz_load` (docker postgres 17), sxema modellardan yaratiladi; Redis db 14.
- **Ma'lumotlar (seed)**:
  - **Katalog**: 3 fan → 6 kurs → 12 modul → 24 mavzu → **96 dars**. Shundan 12 tasi bepul.
  - **Testlar**: **48 ta**, har birida 5 ta bitta javobli savol.
  - **Foydalanuvchilar**: **10 000 ta** ro'yxatdan o'tgan. Ularning 30 foizida faol obuna bor.
- **Virtual o'quvchi ssenariysi** (har yangi iteratsiyada yangi foydalanuvchi):
  - `/start` → «📚 Fanlar va kurslar» → fan → kurs → modul → mavzu.
  - Darsni ochish; bepul bo'lsa tasdiqlash.
  - Ruxsat bo'lsa: test kartasi → boshlash → 5 ta javob (tugmalar botning haqiqiy klaviaturasidan olinadi).
  - Oxirida «👤 Profilim».
  - Obunasiz o'quvchilarning 30 foizi ataylab pullik darsga uriladi (kutilgan `SubscriptionRequired`).
- **Fikrlash vaqti**: harakatlar orasida 0,5–1,5 s (real foydalanuvchiga yaqin). Throttling yoqilgan (8 ta update / 4 s).
- **Bosqichlar**: bir vaqtda 50 / 100 / 200 faol foydalanuvchi, har biri 25 s.
- **«Saturatsiya»**: 100 foydalanuvchi, fikrlash vaqtisiz, throttling o'chirilgan — jarayonning maksimal o'tkazuvchanligi.
- **O'lchanadi**:
  - throughput (update/s);
  - `feed_update` kechikishi p50/p95/p99 — to'liq va «net» (sun'iy Telegram kutishlarisiz);
  - connection pool'dan ulanish kutish vaqti (`QueuePool._do_get`), bir vaqtda band ulanishlar soni;
  - qayta ishlanmagan xatolar, jarayon RSS, CPU ulushi;
  - mashinadagi bo'sh xotira. 400 MiB dan pastga tushsa sinov o'zini to'xtatadi.

Ishga tushirish (bir vaqtda faqat bitta):

```bash
.venv/bin/python -m tests.load.run_load --steps 50,100,200 --duration 25 --saturation 100
# pool hajmini sinash:
DB_POOL_SIZE=20 DB_MAX_OVERFLOW=30 .venv/bin/python -m tests.load.run_load --steps 200 --saturation 100
```

Natijalar `tests/load/results.json` ga yoziladi (oxirgi yugurish).

## 2. Mashina (ochiq aytamiz: kuchsiz va band)

| | |
|---|---|
| CPU | Intel Core i3-1115G4 (2 yadro / 4 oqim), noutbuk |
| RAM | **3,6 GiB**, swap 1,8 GiB — sinov paytida swap deyarli to'la edi |
| Bir vaqtda ishlagan | PostgreSQL, Redis, MinIO (docker), production bot polling, 4 ta Claude sessiyasi, IDE/brauzer |
| Bo'sh xotira sinov paytida | 520–590 MiB (bosqichlar orasida 860 MiB) |

Raqamlar **pastki chegara** sifatida qaralsin. Alohida VPS'da ular sezilarli yaxshi bo'ladi.

## 3. Natijalar

### 3.1. Asosiy yugurish (pool: `pool_size=10`, `max_overflow=20` → 30 ulanish)

| Bosqich | Update/s | p50 | p95 | p99 | net p95 | Pool kutish p95 | Band ulanish max | Xato | RSS max |
|---|---|---|---|---|---|---|---|---|---|
| 50 bir vaqtda | 35,0 | 132 ms | 213 ms | 736 ms | 95 ms | 0,02 ms | 30 | **0** | 232 MiB |
| 100 bir vaqtda | 66,9 | 146 ms | 365 ms | 551 ms | 287 ms | 43 ms | 30 | **0** | 242 MiB |
| 200 bir vaqtda | 111,6 | 280 ms | 1 075 ms | 1 568 ms | 972 ms | 624 ms | 30 | **0** | 260 MiB |
| Saturatsiya (100, kutishsiz) | **134,4** | 587 ms | 1 426 ms | 2 044 ms | 1 321 ms | 876 ms | 30 | **0** | 271 MiB |

- **Xatolar**: hech bir bosqichda qayta ishlanmagan xato ham, `feed_update` istisnosi ham bo'lmadi.
- **Ko'lam**: jami ~13 300 update ishlandi, shundan ~1 900 tasi test javoblari.

### 3.2. Pool kattalashtirilganda (20 + 30 = 50 ulanish)

| Bosqich | Update/s | p50 | p95 | p99 | Pool kutish p95 | Band ulanish max | CPU (1 yadro) |
|---|---|---|---|---|---|---|---|
| 200 bir vaqtda | 96,8 | 470 ms | 1 254 ms | 1 629 ms | 622 ms | 50 | 74 % |

- **Natija**: pool'ni 30 dan 50 ga oshirish **yaxshilamadi**, aksincha biroz yomonlashtirdi.
- **To'xtash**: bu yugurish bo'sh xotira 398 MiB ga tushgani uchun xavfsizlik chegarasida o'zini to'xtatdi. Saturatsiya bosqichi bajarilmadi.

### 3.3. Bepul dars limiti parallel so'rovlarda

5 ta obunasiz foydalanuvchi sinaldi. Har biri bir vaqtning o'zida **30 ta** «ochish» so'rovini yubordi; so'rovlar 12 ta bepul darsga taqsimlandi va throttling o'chirilgan edi.

| | |
|---|---|
| Har bir foydalanuvchida sarflangan bepul slotlar | **2, 2, 2, 2, 2** |
| Kutilgan | 2 (`free_lesson_limit`) |
| Natija | ✅ limit hech qachon oshmadi. `users` qatorini `FOR UPDATE` bilan qulflash ishlayapti |

## 4. Tor joylar (bottleneck)

1. **Bitta bot jarayonining CPU'si.**
   - Bot yagona asyncio event loop'da ishlaydi.
   - Saturatsiyada ~134 update/s olindi. Pool kattalashtirilganda bu oshmadi, CPU esa ~74 % edi (bitta yadro, swap va boshqa jarayonlar bilan raqobatda).
   - Throughput chegarasini Python jarayonining o'zi belgilaydi. Asosiy CPU sarfi aiogram filtrlari, pydantic serializatsiyasi va SQLAlchemy ORM'ga ketadi.
2. **DB ulanishi butun handler davomida band turadi.**
   - `DbSessionMiddleware` sessiyani handler boshidan oxirigacha ushlab turadi, shu jumladan Telegram API chaqiruvlari (`answer`, `edit_text`) vaqtida ham.
   - Yuklama ostida 30 ta ulanish to'liq band bo'ldi va kutish paydo bo'ldi (200 da p95 0,6 s).
   - Bu handlerlar ichida «Telegram'ga yuborishdan oldin commit» qoidasiga amal qilinganda kamayadi. Qoida CONTRACTS §5 da, `lessons.open_lesson` va chat'da qo'llangan.
   - Ulanish band turishi pool'ni kattalashtirish bilan hal bo'lmaydi: CPU baribir tor joy bo'lib qoladi.
3. **`/start` eng og'ir xabar.** U har safar user upsert va settings upsert'ni bajaradi. Ammo 50 bosqichdagi p95 = 762 ms ning bir qismi sovuq start/JIT effekti.

## 5. Real yuklama bilan solishtirish va tavsiyalar

- **Kutilgan real yuklama.** 10 000 ro'yxatdan o'tgan foydalanuvchi bir vaqtda faol degani emas (TZ §18).
  - Taxminan 10 % kunlik faol, cho'qqi daqiqada esa 3–5 % onlayn bo'lsa, bu **300–500 kishi** bo'ladi.
  - Har biri ~10–20 s da bitta tugma bossa, natija **~15–50 update/s** bo'ladi.
- **Shu kuchsiz, band noutbukdagi natija:**
  - 200 faol foydalanuvchida **112 update/s**, p95 ≈ 1,1 s;
  - 100 faol foydalanuvchida p95 ≈ 0,37 s;
  - **xatolarsiz**.
- **Xulosa.** Real cho'qqidan **2–7 baravar zaxira** bor. Alohida VPS'da bu zaxira katta bo'ladi.

**Pool sozlamasi**:
- Bot jarayoni uchun `DB_POOL_SIZE=10`, `DB_MAX_OVERFLOW=20` (hozirgi default) yetarli. Oshirish foyda bermadi.
- Web (Mini App/player) va worker uchun ham shunga o'xshash (10/10).
- Jami ulanishlar PostgreSQL `max_connections` (default 100) dan kamida 20 ta kam bo'lsin.
- Ko'p jarayon bo'lsa, PgBouncer'ni (transaction mode) ko'rib chiqing.

**VPS** (10 000 ro'yxatdan o'tgan foydalanuvchi):

| Variant | Resurs | Izoh |
|---|---|---|
| Minimal | 2 vCPU, 4 GB RAM, 40 GB NVMe | bot + web + worker + PostgreSQL + Redis bitta serverda; video S3/MinIO alohida yoki tashqi |
| **Tavsiya** | **4 vCPU, 8 GB RAM, 80 GB NVMe** | ffmpeg HLS transkodlash ham shu serverda bo'lsa (u CPU'ni to'liq yeydi); PostgreSQL `shared_buffers≈2GB` |
| Kengaytirish | + alohida worker/transkod server | yuklama oshsa: webhook rejimi va bir nechta bot jarayoni (Redis FSM allaqachon bor), video uchun CDN |

**Xotira**: bot jarayoni yuklama ostida ~230–270 MiB RSS. Web va worker har biri ~150–250 MiB.

## 6. Cheklovlar va keyingi qadamlar

- **Chiqmagan ta'sirlar.** Telegram'ning o'z limitlari simulyatsiya qilinmadi: bitta chatga ~1 xabar/s va bot bo'yicha ~30 xabar/s. Ommaviy xabarlar uchun worker rate-limit (COMMERCE) shu sababli bor. Polling'ning tarmoq kechikishi ham kiritilmagan.
- **Qamrov.** Web (Mini App, video player) va worker yuklamasi bu harness'ga kirmadi. Ularni deploy'dan keyin VPS'da `locust` bilan alohida o'lchash tavsiya etiladi.
- **Takrorlash.** Raqamlarni yakuniy VPS'da qayta olish kerak. Buyruq yuqorida, bitta yugurish ~2 daqiqa.
