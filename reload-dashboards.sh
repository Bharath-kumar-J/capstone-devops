#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")"

URL="${GRAFANA_URL:-http://127.0.0.1:3000}"
USER="${GRAFANA_USER:-admin}"
PASSWORD="${GRAFANA_PASSWORD:-admin}"
COMPOSE_FILE="${COMPOSE_FILE:-monitoring/docker-compose.yml}"

reload_dashboards() {
  curl -fsS \
    -u "$USER:$PASSWORD" \
    -X POST \
    "$URL/api/admin/provisioning/dashboards/reload" >/dev/null
}

wait_for_grafana() {
  i=0
  while [ "$i" -lt 60 ]; do
    if curl -fsS --max-time 3 "$URL/api/health" >/dev/null 2>&1; then
      return 0
    fi
    i=$((i + 1))
    sleep 2
  done
  return 1
}

echo "Reloading Grafana dashboard provisioning at $URL ..."
if ! wait_for_grafana; then
  echo "Grafana is not answering yet. Starting it with Docker Compose..."
  docker compose -f "$COMPOSE_FILE" up -d grafana
  wait_for_grafana
fi

if ! reload_dashboards; then
  echo "Grafana rejected $USER/$PASSWORD. Resetting the local admin password to '$PASSWORD'..."
  docker compose -f "$COMPOSE_FILE" exec -T grafana grafana cli admin reset-admin-password "$PASSWORD" >/dev/null
  wait_for_grafana
  reload_dashboards
fi

echo "Dashboards reloaded. Open $URL/dashboards"
