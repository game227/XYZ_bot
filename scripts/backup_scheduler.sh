#!/usr/bin/env bash
# Backup scheduler, the command of the `backup` compose service (TZ §19):
# - a DB dump every day at BACKUP_AT (UTC, default 03:15), and right away on start when the newest
#   dump is older than 24 h (a home PC may be off at night);
# - a restore test on BACKUP_CHECK_WEEKDAY (1 = Monday … 7 = Sunday, default 7) after the dump.
# Output goes to `docker compose logs backup`; failures alert the superadmins (scripts/_pg.sh).
set -uo pipefail

cd "$(dirname "$0")/.."
AT="${BACKUP_AT:-03:15}"
CHECK_DAY="${BACKUP_CHECK_WEEKDAY:-7}"
BACKUP_DIR="${BACKUP_DIR:-backups}"

run() {
  scripts/backup.sh || true
  if [[ "$(date -u +%u)" == "$CHECK_DAY" ]]; then scripts/backup_restore_check.sh || true; fi
}

mkdir -p "$BACKUP_DIR/db"
if [[ -z "$(find "$BACKUP_DIR/db" -name 'xyz-*.dump' -mmin -1440 2>/dev/null | head -n1)" ]]; then
  echo "no dump from the last 24 h: backing up now"
  run
fi

while true; do
  now=$(date -u +%s)
  next=$(date -u -d "today $AT" +%s)
  (( next > now )) || next=$(date -u -d "tomorrow $AT" +%s)
  echo "next backup: $(date -u -d "@$next" '+%F %T') UTC"
  sleep $(( next - now ))
  run
done
