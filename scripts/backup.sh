#!/usr/bin/env bash
# PostgreSQL backup (TZ §19). Run from the project root on the Docker host, e.g. cron:
#   15 3 * * * cd /srv/xyz-telegram-bot && scripts/backup.sh >> backups/backup.log 2>&1
#
# - DB: pg_dump custom format → backups/db/xyz-YYYYmmdd-HHMMSS.dump (+ .sha256), kept BACKUP_KEEP_DAYS.
# - Lesson files live on Telegram's servers (only their file_id is in the DB), so the DB dump is enough.
# - On failure an alert goes to the superadmins (scripts/backup_alert.py) and the exit code is non-zero.
# Copy backups/ off the server (rsync/rclone) — a backup on the same disk is not a backup.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
COMPOSE=(docker compose)
STAMP="$(date -u +%Y%m%d-%H%M%S)"

# App Python: the project venv when present (dev / host install), else the app image.
run_py() {
  if [[ -x .venv/bin/python ]]; then
    PYTHONPATH=. .venv/bin/python "$@"
  else
    "${COMPOSE[@]}" run --rm --no-deps -T -v "$(pwd)/$BACKUP_DIR:/srv/$BACKUP_DIR" -e PYTHONPATH=. bot python "$@"
  fi
}
alert() { [[ "${BACKUP_ALERTS:-1}" == 0 ]] || run_py scripts/backup_alert.py "$1" >/dev/null 2>&1 || true; }
trap 'alert "❌ Zaxira nusxa olinmadi (${STAMP}). Serverdagi backups/backup.log ni tekshiring."' ERR

umask 077
mkdir -p "$BACKUP_DIR/db"

echo "[$STAMP] database dump"
DUMP="$BACKUP_DIR/db/xyz-$STAMP.dump"
"${COMPOSE[@]}" exec -T postgres pg_dump -U xyz -d xyz -Fc --no-owner > "$DUMP.part"
mv "$DUMP.part" "$DUMP"
sha256sum "$DUMP" > "$DUMP.sha256"
test -s "$DUMP"

echo "[$STAMP] retention: removing DB dumps older than $KEEP_DAYS days"
find "$BACKUP_DIR/db" -name 'xyz-*.dump*' -mtime "+$KEEP_DAYS" -delete

echo "[$STAMP] done: $DUMP ($(du -h "$DUMP" | cut -f1))"
