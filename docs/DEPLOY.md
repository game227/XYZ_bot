# VPS'ga joylashtirish (deploy)

Bu qo‘llanma botni bitta Linux VPS'da Docker Compose orqali ishga tushirishni tushuntiradi.
Zaxira nusxalar: [`BACKUP.md`](BACKUP.md). Yuklama testi natijalari: [`LOAD_TEST.md`](LOAD_TEST.md).

## 1. Server

| | Minimal | Tavsiya |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU (video ffmpeg bilan shu serverda o‘giriladi) |
| RAM | 4 GB | 8 GB |
| Disk | 40 GB SSD | 80 GB NVMe |
| OS | Ubuntu 24.04 LTS yoki Debian 12 | |

Yuklama testi 3.6 GB RAM'li noutbukda 200 ta bir vaqtdagi faol foydalanuvchida 112 update/s ni
xatosiz ko‘tardi. 10 000 ro‘yxatdan o‘tgan foydalanuvchi uchun kutilgan cho‘qqi 15–50 update/s.

Domen: `bot.example.uz` kabi subdomen yarating va uning **A yozuvini** VPS IP manziliga yo‘naltiring.
Mini App va video player faqat HTTPS bilan ishlaydi; sertifikatni Caddy avtomatik oladi.

## 2. Serverni tayyorlash

```bash
# Faqat SSH, HTTP va HTTPS ochiq bo‘ladi (PostgreSQL/Redis/MinIO faqat 127.0.0.1 da tinglaydi)
sudo apt update && sudo apt -y upgrade
sudo apt -y install ufw git
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable

# Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER   # so‘ng qayta login qiling
```

SSH'ga faqat kalit bilan kirishni yoqing (`PasswordAuthentication no`).

## 3. Kod

Serverda faqat o‘qish huquqli GitHub deploy key yarating va repo'ga qo‘shing
(Settings → Deploy keys, **write access belgilanmasin**):

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/xyz_bot_deploy -C "xyz-bot@server"
cat ~/.ssh/xyz_bot_deploy.pub    # GitHub'ga qo‘shing
GIT_SSH_COMMAND="ssh -i ~/.ssh/xyz_bot_deploy" git clone git@github.com:game227/XYZ_bot.git xyz-bot
cd xyz-bot && git config core.sshCommand "ssh -i ~/.ssh/xyz_bot_deploy"
```

## 4. `.env` (production)

```bash
cp .env.example .env && chmod 600 .env && nano .env
```

| O‘zgaruvchi | Qiymat |
|---|---|
| `ENVIRONMENT` | `prod` (API hujjatlari sahifasi o‘chadi) |
| `BOT_TOKEN` | @BotFather'dan **yangi** token (chatda ulashilgan eski tokenni Revoke qiling) |
| `SUPERADMIN_IDS` | `7564700491` (vergul bilan bir nechta) |
| `SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `POSTGRES_PASSWORD` | kuchli tasodifiy parol |
| `DOMAIN` | `bot.example.uz` |
| `WEB_BASE_URL` | `https://bot.example.uz` |
| `S3_*` | 5-bo‘limga qarang |

`DATABASE_URL`, `REDIS_URL` ni yozish shart emas: konteynerlar uchun compose o‘zi beradi.

## 5. Media saqlash (S3)

MinIO bepul Docker image'larni tarqatishni to‘xtatgan; `docker-compose.yml` dagi `minio` faqat
ishlab chiqish uchun (muzlatilgan Bitnami nusxasi, xavfsizlik yangilanishlari yo‘q).
Production uchun boshqariladigan S3-mos xizmatdan foydalaning (masalan Cloudflare R2 — video trafigi
uchun chiqish to‘lovi yo‘q, yoki Backblaze B2, Hetzner Object Storage, AWS S3):

```dotenv
S3_ENDPOINT_URL_DOCKER=https://<account-id>.r2.cloudflarestorage.com
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_BUCKET=xyz-media
S3_REGION=auto
MEDIA_SEGMENT_MODE=redirect   # video segmentlari 60 soniyalik imzolangan S3 havolalari orqali
```

Bucket sozlamalari:
- **Private** bo‘lsin (ommaviy o‘qish yo‘q).
- Lifecycle qoidasi: tugallanmagan multipart yuklamalarni **7 kundan keyin bekor qilish**.
- Versiyalash yoki alohida zaxira — `BACKUP.md` ga qarang.

O‘z serveringizdagi MinIO bilan ishlasangiz, `S3_ENDPOINT_URL_DOCKER` ni bo‘sh qoldiring va
`MEDIA_SEGMENT_MODE=proxy` qiling (segmentlar web servis orqali uzatiladi).

## 6. Ishga tushirish

```bash
# Tashqi S3 bilan (MinIO konteynerisiz):
docker compose --profile app up -d --build --scale minio=0
# Ichki MinIO bilan:
docker compose --profile app up -d --build
```

Tartib avtomatik: `postgres`/`redis` sog‘lom bo‘lgach `migrate` (`alembic upgrade head`) ishlaydi,
u muvaffaqiyatli tugagach `bot`, `web`, `worker` ko‘tariladi, so‘ng `caddy` HTTPS sertifikatini oladi.

Tekshirish:

```bash
docker compose --profile app ps          # hammasi "running"/"healthy"
curl -fs https://bot.example.uz/healthz  # {"status":"ok"}
docker compose logs -f bot               # "Bot started (long polling)"
```

So‘ng Telegram'da botga `/start` yozing — superadmin sifatida admin panel ochiladi.

## 7. Yangilash

```bash
git pull
docker compose --profile app up -d --build   # yangi migratsiyalar avtomatik qo‘llanadi
```

Muammo bo‘lsa, oldingi versiyaga qaytish: `git checkout <oldingi-commit> && docker compose --profile app up -d --build`.
Migratsiyani orqaga qaytarishdan oldin albatta zaxira nusxa oling (`scripts/backup.sh`).

## 8. Monitoring

- **Xatolar**: botda kutilmagan xato bo‘lsa, superadminlarga qisqa Telegram xabari keladi (bir turdagi xato
  haqida 5 daqiqada ko‘pi bilan bir marta; tafsilotlar faqat loglarda).
- **Zaxira**: zaxira olish xato bersa, superadminlarga xabar keladi (`BACKUP.md`).
- **Loglar**: `docker compose logs --since 1h bot web worker`.
- **Mavjudlik**: tashqi monitoring xizmatida (masalan UptimeRobot) `https://<domen>/healthz` ni kuzatib boring.
- **Resurslar**: `docker stats`, `df -h` (disk 80% dan oshsa — eski zaxiralar/loglarni tozalang).

## 9. Xavfsizlik ro‘yxati

- [ ] Yangi `BOT_TOKEN` olingan, eski token Revoke qilingan.
- [ ] `.env` faqat egasiga o‘qiladi (`chmod 600`), git'ga kirmaydi.
- [ ] `ENVIRONMENT=prod`.
- [ ] UFW: faqat 22/80/443; SSH faqat kalit bilan.
- [ ] S3 bucket private, lifecycle qoidasi bor.
- [ ] Zaxira cron'i ishlayapti va tiklash sinovi o‘tkazilgan (`BACKUP.md`).
