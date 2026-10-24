# Shared helpers for the backup scripts (sourced, not executed). PostgreSQL is reached either
# directly (inside the `backup` compose service, PGHOST/PGUSER/PGPASSWORD set) or through
# `docker compose exec` (when run on the Docker host, e.g. from cron).
COMPOSE=(docker compose)

if [[ -n "${PGHOST:-}" ]]; then
  pg() { command "$@"; }  # `command`: never recurse into a same-named shell function (psql)
else
  pg() { "${COMPOSE[@]}" exec -T postgres "$@"; }
fi

# App Python: the container's own, the project venv (dev / host install), else the app image.
run_py() {
  if [[ -n "${PGHOST:-}" ]]; then
    PYTHONPATH=. python "$@"
  elif [[ -x .venv/bin/python ]]; then
    PYTHONPATH=. .venv/bin/python "$@"
  else
    "${COMPOSE[@]}" run --rm --no-deps -T -e PYTHONPATH=. bot python "$@"
  fi
}

alert() { [[ "${BACKUP_ALERTS:-1}" == 0 ]] || run_py scripts/backup_alert.py "$1" >/dev/null 2>&1 || true; }
