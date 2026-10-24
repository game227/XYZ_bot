#!/usr/bin/env bash
# PostgreSQL backup (TZ §19). Runs daily from the `backup` compose service (scripts/backup_scheduler.sh);
# can also be run by hand from the project root on the Docker host: scripts/backup.sh
#
# - DB: pg_dump custom format → backups/db/xyz-YYYYmmdd-HHMMSS.dump (+ .sha256), kept BACKUP_KEEP_DAYS.
# - Lesson files live on Telegram's servers (only their file_id is in the DB), so the DB dump is enough.
# - On failure an alert goes to the superadmins (scripts/backup_alert.py) and the exit code is non-zero.
# Copy backups/ off the server (rsync/rclone) — a backup on the same disk is not a backup.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
source scripts/_pg.sh
trap 'alert "❌ Zaxira nusxa olinmadi (${STAMP}). Serverdagi backups/backup.log ni tekshiring."' ERR

umask 077
mkdir -p "$BACKUP_DIR/db"

echo "[$STAMP] database dump"
DUMP="$BACKUP_DIR/db/xyz-$STAMP.dump"
pg pg_dump -U xyz -d xyz -Fc --no-owner > "$DUMP.part"
mv "$DUMP.part" "$DUMP"
sha256sum "$DUMP" > "$DUMP.sha256"
test -s "$DUMP"

echo "[$STAMP] retention: removing DB dumps older than $KEEP_DAYS days"
find "$BACKUP_DIR/db" -name 'xyz-*.dump*' -mtime "+$KEEP_DAYS" -delete

echo "[$STAMP] done: $DUMP ($(du -h "$DUMP" | cut -f1))"
