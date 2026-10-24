#!/usr/bin/env bash
# Restore test (TZ §19: backups must be proven restorable). Restores the newest (or given) dump
# into a throwaway database on the same PostgreSQL, sanity-checks it, then drops it.
#   scripts/backup_restore_check.sh [backups/db/xyz-....dump]
# Runs weekly from the `backup` compose service (scripts/backup_scheduler.sh).
set -Eeuo pipefail

cd "$(dirname "$0")/.."
source scripts/_pg.sh
CHECK_DB="xyz_restore_check"
DUMP="${1:-$(ls -1t backups/db/xyz-*.dump 2>/dev/null | head -n1)}"

cleanup() { pg dropdb -U xyz --if-exists "$CHECK_DB" >/dev/null 2>&1 || true; }
trap 'alert "❌ Zaxirani tiklash sinovi muvaffaqiyatsiz: ${DUMP:-dump topilmadi}"; cleanup' ERR
trap cleanup EXIT

[[ -n "$DUMP" && -s "$DUMP" ]] || { echo "no dump found"; false; }
if [[ -f "$DUMP.sha256" ]]; then sha256sum -c --quiet "$DUMP.sha256"; fi

echo "restoring $DUMP into $CHECK_DB"
cleanup
pg createdb -U xyz "$CHECK_DB"
pg pg_restore -U xyz -d "$CHECK_DB" --no-owner --exit-on-error < "$DUMP"

psql() { pg psql -U xyz -d "$1" -v ON_ERROR_STOP=1 -tAc "$2"; }
TABLES_SQL="SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name COLLATE \"C\""
COLUMNS_SQL="SELECT md5(string_agg(table_name||'.'||column_name||':'||data_type||':'||is_nullable, ',' ORDER BY table_name, column_name)) FROM information_schema.columns WHERE table_schema='public'"
counts_sql() {
  local sql="" t
  for t in $1; do sql+="${sql:+ UNION ALL }SELECT '$t', count(*) FROM \"$t\""; done
  echo "SELECT * FROM ($sql) q(t, n) ORDER BY t COLLATE \"C\""
}

version="$(psql "$CHECK_DB" 'SELECT version_num FROM alembic_version')"
[[ -n "$version" ]]
tables="$(psql "$CHECK_DB" "$TABLES_SQL")"
echo "restored: alembic=$version, $(wc -w <<<"$tables") tables"

# Optional comparison with the live source DB (read-only SELECTs). Row counts can drift
# slightly on a live system between the dump and this check; schema must match exactly.
if [[ "${COMPARE_SOURCE:-0}" == 1 ]]; then
  [[ "$(psql xyz "$TABLES_SQL")" == "$tables" ]] || { echo "table list differs from source"; false; }
  [[ "$(psql xyz "$COLUMNS_SQL")" == "$(psql "$CHECK_DB" "$COLUMNS_SQL")" ]] || { echo "columns differ from source"; false; }
  [[ "$(psql xyz 'SELECT version_num FROM alembic_version')" == "$version" ]] || { echo "alembic version differs"; false; }
  echo "schema identical to source (tables, columns, alembic)"
  LC_ALL=C join -t'|' <(psql xyz "$(counts_sql "$tables")") <(psql "$CHECK_DB" "$(counts_sql "$tables")") \
    | awk -F'|' '{ printf "  %-32s source=%-8s restored=%-8s %s\n", $1, $2, $3, ($2==$3 ? "ok" : "DIFF") }'
else
  psql "$CHECK_DB" "$(counts_sql "$tables")" | awk -F'|' '{ printf "  %-32s %s\n", $1, $2 }'
fi
echo "restore check OK"
