#!/usr/bin/env bash
# PostgreSQL + media backup (TZ §19). Run from the project root on the Docker host, e.g. cron:
#   15 3 * * * cd /srv/xyz-telegram-bot && scripts/backup.sh >> backups/backup.log 2>&1
#
# - DB: pg_dump custom format → backups/db/xyz-YYYYmmdd-HHMMSS.dump (+ .sha256), kept BACKUP_KEEP_DAYS.
# - Media: incremental mirror of the private bucket → backups/media/<bucket>/ (originals + HLS).
# - On failure an alert goes to the superadmins (scripts/backup_alert.py) and the exit code is non-zero.
# Copy backups/ off the server (rsync/rclone) — a backup on the same disk is not a backup.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
BUCKET="${S3_BUCKET:-xyz-media}"
MC_IMAGE="${MC_IMAGE:-minio/mc}"  # pin a RELEASE tag in production
COMPOSE=(docker compose)
STAMP="$(date -u +%Y%m%d-%H%M%S)"

alert() {
  "${COMPOSE[@]}" run --rm --no-deps -T bot python scripts/backup_alert.py "$1" >/dev/null 2>&1 || true
}
trap 'alert "❌ Zaxira nusxa olinmadi (${STAMP}). Serverdagi backups/backup.log ni tekshiring."' ERR

umask 077
mkdir -p "$BACKUP_DIR/db" "$BACKUP_DIR/media"

echo "[$STAMP] database dump"
DUMP="$BACKUP_DIR/db/xyz-$STAMP.dump"
"${COMPOSE[@]}" exec -T postgres pg_dump -U xyz -d xyz -Fc --no-owner > "$DUMP.part"
mv "$DUMP.part" "$DUMP"
sha256sum "$DUMP" > "$DUMP.sha256"
test -s "$DUMP"

echo "[$STAMP] media mirror"
MINIO_CID="$("${COMPOSE[@]}" ps -q minio)"
NETWORK="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$MINIO_CID")"
MINIO_USER="$("${COMPOSE[@]}" exec -T minio printenv MINIO_ROOT_USER)"
MINIO_PASS="$("${COMPOSE[@]}" exec -T minio printenv MINIO_ROOT_PASSWORD)"
docker run --rm --network "$NETWORK" \
  -e "MC_HOST_src=http://${MINIO_USER}:${MINIO_PASS}@minio:9000" \
  -v "$(pwd)/$BACKUP_DIR/media:/backup" \
  "$MC_IMAGE" mirror --overwrite --preserve "src/$BUCKET" "/backup/$BUCKET" >/dev/null

echo "[$STAMP] retention: removing DB dumps older than $KEEP_DAYS days"
find "$BACKUP_DIR/db" -name 'xyz-*.dump*' -mtime "+$KEEP_DAYS" -delete

echo "[$STAMP] done: $DUMP ($(du -h "$DUMP" | cut -f1))"
