#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")"
CLUSTER=capstone
say(){ printf "\n\033[1;36m== %s ==\033[0m\n" "$1"; }

if [ ! -f Dockerfile ] || [ ! -f app.py ]; then
  echo "ERROR: run this from ~/devops-labs/capstone (app.py + Dockerfile must exist)."; exit 1
fi

# ---- (re)generate all infra + monitoring config (single source of truth) ----
cat > kind-config.yaml <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30080
        hostPort: 30080
        protocol: TCP
EOF

cat > k8s.yaml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata: { name: redis }
spec:
  replicas: 1
  selector: { matchLabels: { app: redis } }
  template:
    metadata: { labels: { app: redis } }
    spec:
      containers:
        - name: redis
          image: redis:7-alpine
          ports: [{ containerPort: 6379 }]
---
apiVersion: v1
kind: Service
metadata: { name: redis }
spec:
  selector: { app: redis }
  ports: [{ port: 6379, targetPort: 6379 }]
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: capstone }
spec:
  replicas: 2
  selector: { matchLabels: { app: capstone } }
  template:
    metadata: { labels: { app: capstone } }
    spec:
      containers:
        - name: capstone
          image: capstone:1.0
          imagePullPolicy: IfNotPresent
          ports: [{ containerPort: 5000 }]
          env:
            - { name: REDIS_HOST, value: redis }
          resources:
            requests: { cpu: 100m, memory: 64Mi }
            limits:   { cpu: 500m, memory: 128Mi }
          readinessProbe: { httpGet: { path: /health, port: 5000 }, initialDelaySeconds: 3 }
          livenessProbe:  { httpGet: { path: /health, port: 5000 }, initialDelaySeconds: 5 }
---
apiVersion: v1
kind: Service
metadata: { name: capstone }
spec:
  type: NodePort
  selector: { app: capstone }
  ports: [{ port: 80, targetPort: 5000, nodePort: 30080 }]
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: { name: capstone }
spec:
  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: capstone }
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource: { name: cpu, target: { type: Utilization, averageUtilization: 50 } }
EOF

mkdir -p monitoring/provisioning/datasources monitoring/provisioning/dashboards monitoring/dashboards

cat > monitoring/prometheus.yml <<'EOF'
global:
  scrape_interval: 5s
scrape_configs:
  - job_name: 'capstone'
    static_configs:
      - targets: ['host.docker.internal:30080']
EOF

cat > monitoring/docker-compose.yml <<'EOF'
services:
  prometheus:
    image: prom/prometheus:latest
    ports: ["9090:9090"]
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
    extra_hosts:
      - "host.docker.internal:host-gateway"
  grafana:
    image: grafana/grafana:latest
    ports: ["3000:3000"]
    environment:
      GF_SECURITY_ADMIN_PASSWORD: admin
      GF_AUTH_ANONYMOUS_ENABLED: "true"
      GF_ANALYTICS_CHECK_FOR_UPDATES: "false"
      GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES: "false"
      GF_PLUGINS_PLUGIN_ADMIN_ENABLED: "false"
      GF_LOG_LEVEL: warn
    volumes:
      - grafana-data:/var/lib/grafana
      - ./provisioning:/etc/grafana/provisioning
      - ./dashboards:/var/lib/grafana/dashboards
    healthcheck:
      test: ["CMD-SHELL", "wget -q -O- http://127.0.0.1:3000/api/health >/dev/null 2>&1 || exit 1"]
      interval: 5s
      timeout: 3s
      retries: 60
volumes:
  grafana-data:
EOF

cat > monitoring/provisioning/datasources/datasource.yml <<'EOF'
apiVersion: 1
datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
EOF

cat > monitoring/provisioning/dashboards/dashboards.yml <<'EOF'
apiVersion: 1
providers:
  - name: capstone
    orgId: 1
    folder: ""
    type: file
    disableDeletion: false
    editable: true
    updateIntervalSeconds: 5
    options:
      path: /var/lib/grafana/dashboards
EOF

cat > monitoring/dashboards/capstone.json <<'EOF'
{
  "id": null,
  "uid": "capstone",
  "title": "Capstone Pipeline",
  "tags": ["capstone"],
  "timezone": "browser",
  "schemaVersion": 39,
  "version": 1,
  "refresh": "5s",
  "time": { "from": "now-15m", "to": "now" },
  "panels": [
    { "id": 1, "type": "timeseries", "title": "Requests / sec (all routes)",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 0 },
      "targets": [ { "refId": "A", "expr": "sum(rate(app_requests_total[1m]))" } ] },
    { "id": 2, "type": "timeseries", "title": "p95 latency (seconds)",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 0 },
      "targets": [ { "refId": "A", "expr": "histogram_quantile(0.95, sum(rate(app_request_latency_seconds_bucket[1m])) by (le))" } ] },
    { "id": 3, "type": "timeseries", "title": "In-flight requests",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 8 },
      "targets": [ { "refId": "A", "expr": "app_requests_in_progress" } ] },
    { "id": 4, "type": "timeseries", "title": "Requests / sec per route",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 8 },
      "targets": [ { "refId": "A", "expr": "sum by (endpoint) (rate(app_requests_total[1m]))", "legendFormat": "{{endpoint}}" } ] }
  ]
}
EOF

# ---- bring everything up ----
say "1/6  Docker running?"
docker info >/dev/null 2>&1 || { sudo systemctl start docker; sleep 3; }

say "2/6  kind cluster"
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind create cluster --name "$CLUSTER" --config kind-config.yaml
else
  echo "cluster '$CLUSTER' already exists"
  if [ "$(docker inspect -f '{{.State.Running}}' "${CLUSTER}-control-plane" 2>/dev/null || true)" != "true" ]; then
    echo "cluster '$CLUSTER' exists but its Docker node is not running; recreating it"
    kind delete cluster --name "$CLUSTER"
    kind create cluster --name "$CLUSTER" --config kind-config.yaml
  else
    kind export kubeconfig --name "$CLUSTER" >/dev/null
  fi
  if ! kubectl cluster-info --request-timeout=8s >/dev/null 2>&1; then
    echo "cluster '$CLUSTER' exists but its API is not reachable; recreating it"
    kind delete cluster --name "$CLUSTER"
    kind create cluster --name "$CLUSTER" --config kind-config.yaml
  fi
fi
kind export kubeconfig --name "$CLUSTER" >/dev/null

say "3/6  metrics-server (feeds the autoscaler)"
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
if ! kubectl -n kube-system get deploy metrics-server -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null | grep -q kubelet-insecure-tls; then
  kubectl -n kube-system patch deployment metrics-server --type=json \
    -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
fi
kubectl -n kube-system rollout status deployment metrics-server --timeout=120s || true

say "4/6  build + load image"
docker build -t capstone:1.0 .
kind load docker-image capstone:1.0 --name "$CLUSTER"

say "5/6  deploy app + redis + HPA"
DEPLOYMENT_EXISTS=0
if kubectl get deployment/capstone >/dev/null 2>&1; then DEPLOYMENT_EXISTS=1; fi
kubectl apply -f k8s.yaml
if [ "$DEPLOYMENT_EXISTS" = 1 ]; then
  kubectl rollout restart deployment/capstone >/dev/null 2>&1 || true
fi
kubectl rollout status deployment/capstone --timeout=180s

say "6/6  monitoring (Prometheus + Grafana)"
( cd monitoring && docker compose up -d )

printf "waiting for Grafana to answer"
GRAFANA_OK=0
for i in $(seq 1 90); do
  if curl -fs --max-time 2 http://127.0.0.1:3000/api/health >/dev/null 2>&1; then GRAFANA_OK=1; break; fi
  printf "."; sleep 2
done; echo

IP=$(hostname -I | awk '{print $1}')
echo
echo "==================== CAPSTONE IS UP ===================="
kubectl get pods,svc,hpa
echo
printf "App        : http://localhost:30080      (Windows fallback: http://%s:30080)\n" "$IP"
printf "Prometheus : http://localhost:9090       (Windows fallback: http://%s:9090)\n" "$IP"
printf "Grafana    : http://localhost:3000       (Windows fallback: http://%s:3000)   admin/admin\n" "$IP"
echo
if [ "$GRAFANA_OK" = 1 ]; then
  sh reload-dashboards.sh >/dev/null 2>&1 || true
  printf "Grafana    : \033[32mup\033[0m  -> Dashboards -> http://localhost:3000/dashboards (admin/admin)\n"
else
  printf "Grafana    : \033[31mnot answering yet\033[0m -> docker compose -f monitoring/docker-compose.yml logs grafana\n"
fi
echo
echo "If localhost won't open in Windows: use the IP links above, or run 'wsl --shutdown' in PowerShell then reopen."
echo
echo "STRESS TEST (2nd terminal):"
echo "  python3 stresstest.py --url http://localhost:30080 --duration 120 --concurrency 60 --k8s"
