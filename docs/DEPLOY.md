# Ishga tushirish (deploy)

Bot to‘liq Telegram ichida ishlaydi: veb sayt, Mini App, domen, HTTPS va ochiq port **kerak emas**.
Bot Telegram serveriga o‘zi ulanadi (long polling), shuning uchun u istalgan kompyuterda ishlaydi —
hozircha shu kompyuterda, keyinchalik VPS'da xuddi shu buyruqlar bilan.

Xizmatlar (`docker-compose.yml`, profil `app`):

| Xizmat | Vazifasi |
|---|---|
| `postgres` | ma’lumotlar bazasi |
| `redis` | FSM holatlari, navbat, login limiti |
| `migrate` | `alembic upgrade head` (har ishga tushishda, keyin to‘xtaydi) |
| `bot` | Telegram bot |
| `worker` | eslatmalar, ommaviy xabarlar, jonli dars eslatmalari |

Dars fayllari (video 2000 MB gacha, PDF va hujjatlar 200 MB gacha) Telegram serverlarida saqlanadi;
bazada faqat ularning `file_id` si bor.

## 1. Talablar

- Linux, Docker va Docker Compose (`sudo pacman -S docker docker-compose` yoki `curl -fsSL https://get.docker.com | sh`).
- Kamida 2 GB bo‘sh RAM (VPS uchun: 2 vCPU / 4 GB, yuklama testi — [`LOAD_TEST.md`](LOAD_TEST.md)).
- Internet (chiquvchi ulanish `api.telegram.org` ga).

## 2. `.env`

```bash
cp .env.example .env && chmod 600 .env
```

| O‘zgaruvchi | Qiymat |
|---|---|
| `ENVIRONMENT` | `prod` |
| `BOT_TOKEN` | @BotFather tokeni (ishga chiqarishdan oldin yangisini oling) |
| `SUPERADMIN_IDS` | `7564700491` — yagona superadmin |
| `POSTGRES_PASSWORD` | kuchli parol (VPS'da albatta) |

## 3. Ishga tushirish

```bash
docker compose --profile app up -d --build
docker compose --profile app ps        # bot, worker: running; migrate: exited (0)
docker compose logs -f bot             # "Start polling"
```

Konteynerlar `restart: unless-stopped` bilan ishlaydi: kompyuter qayta yonganda Docker ularni
avtomatik ko‘taradi (`sudo systemctl enable docker`).

Muhim: bitta token bilan bir vaqtda faqat **bitta** bot jarayoni ishlashi mumkin. Botni venv'dan
(`python -m app.main`) ishga tushirgan bo‘lsangiz, avval uni to‘xtating.

## 4. Yangilash

```bash
git pull
docker compose --profile app up -d --build   # yangi migratsiyalar avtomatik qo‘llanadi
```

Oldingi versiyaga qaytish: `git checkout <commit> && docker compose --profile app up -d --build`
(migratsiyani orqaga qaytarishdan oldin zaxira oling: `scripts/backup.sh`).

## 5. Zaxira

`backup` konteyneri har kuni zaxira oladi va haftada bir tiklashni sinaydi — tafsilotlar [`BACKUP.md`](BACKUP.md) da.

## 6. Monitoring

- Botda kutilmagan xato bo‘lsa, superadminga qisqa Telegram xabari keladi (bir turdagi xato haqida
  5 daqiqada ko‘pi bilan bir marta; tafsilotlar faqat loglarda).
- Zaxira olish xato bersa, superadminga xabar keladi.
- Bot va worker Redis'da heartbeat yangilab turadi: `docker compose ps` da `healthy`/`unhealthy`
  ko‘rinadi, bot to‘xtab qolsa worker superadminlarga bir marta xabar beradi (qayta ishlaganda ham).
- «📊 Statistika» → «🖥 Tizim holati»: bot/worker holati, soatlik va sutkalik so‘rovlar, xatolar,
  o‘rtacha va sekin javoblar.
- Ixtiyoriy: `.env` ga `SENTRY_DSN` yozilsa, xatolar Sentry'ga ham boradi (shaxsiy ma’lumotsiz).
- Loglar: `docker compose logs --since 1h bot worker`.
- Resurslar: `docker stats`, `df -h`.

## 7. Xavfsizlik ro‘yxati

- [ ] Ishga chiqarishdan oldin @BotFather'da yangi token olingan (eski token chatda ulashilgan edi).
- [ ] `.env` faqat egasiga o‘qiladi (`chmod 600`), git'ga kirmaydi.
- [ ] `ENVIRONMENT=prod`, kuchli `POSTGRES_PASSWORD`.
- [ ] PostgreSQL va Redis faqat `127.0.0.1` da tinglaydi (compose shunday sozlangan).
- [ ] `backup` konteyneri ishlayapti (`docker compose logs backup`) va tiklash sinovi o‘tkazilgan.
