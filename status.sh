#!/usr/bin/env bash
cd "$(dirname "$0")"
CLUSTER=capstone
IP=$(hostname -I | awk '{print $1}')
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind export kubeconfig --name "$CLUSTER" >/dev/null 2>&1 || true
fi
echo "== Kubernetes =="
if ! kubectl get pods,svc,hpa; then
  echo "Kubernetes API is not reachable. Run ./start.sh to recreate the local kind cluster."
fi
echo; echo "== Monitoring containers =="; docker compose -f monitoring/docker-compose.yml ps 2>/dev/null
chk(){ if curl -fs --max-time 3 -o /dev/null "$2"; then printf "  %-11s \033[32mUP\033[0m   %s\n" "$1" "$2"; else printf "  %-11s \033[31mDOWN\033[0m %s\n" "$1" "$2"; fi; }
echo; echo "== Endpoints (tested from inside WSL) =="
chk "App"        http://localhost:30080/health
chk "Prometheus" http://localhost:9090/-/healthy
chk "Grafana"    http://localhost:3000/api/health
echo; printf "Windows browser fallback IP: %s\n" "$IP"
