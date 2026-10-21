# Zaxira nusxa va tiklash (TZ §19, qabul mezoni 24)

Botning ikki xil ma’lumoti zaxiralanadi:

| Nima | Qayerda | Qanday |
|---|---|---|
| PostgreSQL (`xyz`) | docker `postgres` | `pg_dump -Fc` → `backups/db/xyz-YYYYmmdd-HHMMSS.dump` + `.sha256` |
| Media (S3/MinIO bucket) | docker `minio`, bucket `S3_BUCKET` | inkremental ko‘chirma → `backups/media/<bucket>/` (originallar + HLS) |

Redis zaxiralanmaydi: unda faqat vaqtinchalik holatlar (FSM, navbat, login limiti, yuklash sessiyalari) bor.

Skriptlar (`scripts/`):

- `backup.sh` — DB dump + media ko‘chirmasi + eski dumplarni o‘chirish.
- `backup_restore_check.sh` — oxirgi dumpni vaqtinchalik `xyz_restore_check` bazasiga tiklab tekshiradi, keyin o‘chiradi.
- `backup_media.py` — bucket ↔ papka (zaxiralash va `--restore`).
- `backup_alert.py` — xatolikda superadminlarga Telegram xabari.

## Jadval (cron, server foydalanuvchisi `docker` guruhida bo‘lishi kerak)

```cron
# Har kuni 03:15 (UTC) — zaxira
15 3 * * *  cd /srv/xyz-telegram-bot && scripts/backup.sh >> backups/backup.log 2>&1
# Har yakshanba 04:30 — tiklash sinovi
30 4 * * 0  cd /srv/xyz-telegram-bot && COMPARE_SOURCE=1 scripts/backup_restore_check.sh >> backups/backup.log 2>&1
# Har kuni 05:00 — serverdan tashqariga nusxa (pastga qarang)
0 5 * * *   rclone sync /srv/xyz-telegram-bot/backups offsite-crypt:xyz-backups --log-file /var/log/rclone-xyz.log
```

## Saqlash muddati

- DB dumplari: `BACKUP_KEEP_DAYS` (standart 14 kun) serverda; serverdan tashqarida kamida 30 kunlik + oyning 1-kunidagi dumplar 12 oy.
- Media: inkremental ko‘chirma (o‘zgarmagan fayllar qayta yuklanmaydi). O‘chirilgan media zaxirada qoladi; kerak bo‘lsa vaqti-vaqti bilan qo‘lda tozalanadi.
- Tavsiya: bucket uchun lifecycle qoidasi — tugallanmagan multipart yuklashlarni 7 kundan keyin bekor qilish.

## Serverdan tashqari nusxa

Bir diskdagi zaxira — zaxira emas. `backups/` papkasini boshqa joyga shifrlangan holda yuboring:

- **rclone crypt** (S3, Backblaze B2, Google Drive va h.k.): `rclone config` → oddiy remote + uning ustida `crypt` remote (`offsite-crypt`), parolni server tashqarisida saqlang.
- Yoki `restic`/`borg` (shifrlash va versiyalash o‘zida bor).

Ruxsatlar: `backups/` `umask 077` bilan yaratiladi (faqat egasi o‘qiydi). Dumplarda foydalanuvchilarning shaxsiy ma’lumotlari bor: offsite kalitlarni faqat mas’ul administrator biladi.

## Xatolik haqida xabar

Skript xato bilan tugasa (`set -e` + `trap ERR`), `backup_alert.py` `SUPERADMIN_IDS` dagilarga Telegram xabar yuboradi. Chiqish kodi 0 dan farqli bo‘ladi (cron MAILTO ham ishlaydi). Sinov vaqtida xabarni o‘chirish uchun: `BACKUP_ALERTS=0`.

## Tiklash tartibi

### Avval: tiklash sinovi (ishlab turgan tizimga tegmaydi)

```bash
scripts/backup_restore_check.sh                        # oxirgi dump
scripts/backup_restore_check.sh backups/db/xyz-....dump  # aniq dump
COMPARE_SOURCE=1 scripts/backup_restore_check.sh       # + jonli baza bilan sxema va qatorlar sonini solishtirish
```

### Haqiqiy halokatdan keyin: PostgreSQL

1. Ilovani to‘xtating (DB ga yozmasin): `docker compose stop bot web worker`.
2. Dumpning butunligini tekshiring: `sha256sum -c backups/db/xyz-....dump.sha256`.
3. Yangi bazaga tiklang (eski bazani darhol o‘chirmang):
   ```bash
   docker compose exec -T postgres createdb -U xyz xyz_restored
   docker compose exec -T postgres pg_restore -U xyz -d xyz_restored --no-owner --exit-on-error < backups/db/xyz-....dump
   ```
4. Tekshiring: `docker compose exec -T postgres psql -U xyz -d xyz_restored -c 'SELECT count(*) FROM users'`.
5. Almashtiring:
   ```bash
   docker compose exec -T postgres psql -U xyz -d postgres \
     -c 'ALTER DATABASE xyz RENAME TO xyz_broken' -c 'ALTER DATABASE xyz_restored RENAME TO xyz'
   ```
6. Migratsiyalar: `docker compose run --rm migrate` (dump eskiroq versiyada bo‘lsa, `alembic upgrade head`).
7. Ilovani yoqing: `docker compose start bot web worker`. Hammasi joyida bo‘lgach, `xyz_broken` ni o‘chiring.

### Media

```bash
# backups/media/<bucket>/ → bucket (yo‘q yoki hajmi farq qiladigan fayllar yuklanadi)
PYTHONPATH=. .venv/bin/python scripts/backup_media.py --restore backups/media/xyz-media
# boshqa bucketga: --bucket NOM
```

HLS kalitlari (`media_files.hls_aes_key`) bazada saqlanadi, segmentlar esa bucketda. Videolar ijro etilishi uchun DB va media bir-biriga mos davrdan tiklanishi kerak. Media inkremental, shuning uchun oxirgi ko‘chirma har doim eng yangi DB dumpini qamraydi.

### Tiklash maqsadlari

- RPO (yo‘qotilishi mumkin bo‘lgan ma’lumot): kundalik zaxirada ≤ 24 soat. Kerak bo‘lsa cron tez-tez qo‘yiladi yoki WAL arxivlash (pgBackRest) qo‘shiladi.
- RTO (tiklash vaqti): joriy hajmda bir necha daqiqa; vaqt asosan media hajmiga bog‘liq.

## Sinov natijalari (drill)

**2026-10-21, dev muhit** (docker: `postgres:17-alpine`, `bitnamilegacy/minio:2025.7.23-debian-12-r5`)

| Qadam | Natija |
|---|---|
| `BACKUP_ALERTS=0 scripts/backup.sh` | ✅ `backups/db/xyz-20261021-092452.dump` (116 KB) + sha256; media: 1 obyekt ko‘chirildi |
| Takroriy media ko‘chirma | ✅ 0 ko‘chirildi, 1 o‘zgarmagan (inkremental) |
| `COMPARE_SOURCE=1 scripts/backup_restore_check.sh` | ✅ sha256 mos; `xyz_restore_check` ga tiklandi; alembic `0001`, 31 jadval; jadvallar, ustunlar va alembic manba bilan bir xil; 31 jadvalning hammasida qatorlar soni mos (`users`=1, `user_settings`=1, `alembic_version`=1, qolganlari 0) |
| Vaqtinchalik baza o‘chirildi | ✅ `xyz_restore_check` qolmadi |
| `backup_media.py --restore` → vaqtinchalik `xyz-restore-check` bucket | ✅ 1/1 obyekt, kalitlar va hajmlar bir xil; bucket o‘chirildi |

Sinov paytida `xyz` bazasi faqat o‘qildi (`pg_dump` va `SELECT count(*)`).

Topilgan va tuzatilgan muammo: `ORDER BY 1 COLLATE "C"` PostgreSQL’da ustun raqami emas, ifoda sifatida o‘qiladi. Ustun nomi bilan tuzatildi.

Keyingi drill: prod serverga joylashtirilgandan keyin, haqiqiy hajmda, offsite nusxadan tiklash bilan.
