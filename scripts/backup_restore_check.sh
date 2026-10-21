#!/usr/bin/env bash
# Restore test (TZ §19: backups must be proven restorable). Restores the newest (or given) dump
# into a throwaway database on the same PostgreSQL, sanity-checks it, then drops it.
#   scripts/backup_restore_check.sh [backups/db/xyz-....dump]
# Weekly cron:  30 4 * * 0 cd /srv/xyz-telegram-bot && scripts/backup_restore_check.sh >> backups/backup.log 2>&1
set -Eeuo pipefail

cd "$(dirname "$0")/.."
COMPOSE=(docker compose)
CHECK_DB="xyz_restore_check"
DUMP="${1:-$(ls -1t backups/db/xyz-*.dump 2>/dev/null | head -n1)}"

alert() {
  "${COMPOSE[@]}" run --rm --no-deps -T bot python scripts/backup_alert.py "$1" >/dev/null 2>&1 || true
}
cleanup() { "${COMPOSE[@]}" exec -T postgres dropdb -U xyz --if-exists "$CHECK_DB" >/dev/null 2>&1 || true; }
trap 'alert "❌ Zaxirani tiklash sinovi muvaffaqiyatsiz: ${DUMP:-dump topilmadi}"; cleanup' ERR
trap cleanup EXIT

[[ -n "$DUMP" && -s "$DUMP" ]] || { echo "no dump found"; false; }
if [[ -f "$DUMP.sha256" ]]; then sha256sum -c --quiet "$DUMP.sha256"; fi

echo "restoring $DUMP into $CHECK_DB"
cleanup
"${COMPOSE[@]}" exec -T postgres createdb -U xyz "$CHECK_DB"
"${COMPOSE[@]}" exec -T postgres pg_restore -U xyz -d "$CHECK_DB" --no-owner --exit-on-error < "$DUMP"

psql() { "${COMPOSE[@]}" exec -T postgres psql -U xyz -d "$CHECK_DB" -v ON_ERROR_STOP=1 -tAc "$1"; }
version="$(psql 'SELECT version_num FROM alembic_version')"
users="$(psql 'SELECT count(*) FROM users')"
subs="$(psql 'SELECT count(*) FROM subscriptions')"
attempts="$(psql 'SELECT count(*) FROM quiz_attempts')"
[[ -n "$version" ]]
echo "ok: alembic=$version users=$users subscriptions=$subs quiz_attempts=$attempts"
