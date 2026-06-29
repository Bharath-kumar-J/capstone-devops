#!/usr/bin/env python3
"""Capstone full-pipeline stress + validation test (standard library only).

Tests EVERYTHING the capstone is supposed to do:
  functional   : /health, /, /work, /metrics all respond correctly
  stateful tier: Redis counter increments and reports "redis": true
  observability: /metrics exposes the expected Prometheus series
  load         : concurrent traffic (mix of / and /work) with a live dashboard
  autoscaling  : the HPA scales pods up under load (and optionally back down)
  load balance : traffic served by >= 2 distinct pods
  SLOs         : success rate + p95 latency within budget, zero transport errors
  monitoring   : Prometheus 'capstone' target is UP and is recording traffic

Usage:
  python3 stresstest.py
  python3 stresstest.py --url http://localhost:30080 --duration 120 --concurrency 60
  python3 stresstest.py --wait-scaledown          # also prove it scales back down
  python3 stresstest.py --no-k8s --no-prometheus  # skip the infra checks
"""
import argparse
import json
import os
import random
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter as Tally


def paint(code, s):
    if sys.stdout.isatty() and os.getenv("NO_COLOR") is None:
        return "\033[%sm%s\033[0m" % (code, s)
    return s


G = lambda s: paint("32", s)
R = lambda s: paint("31", s)
Y = lambda s: paint("33", s)
C = lambda s: paint("36", s)
B = lambda s: paint("1", s)
D = lambda s: paint("2", s)


# ----------------------------- HTTP helpers -----------------------------------
def http_get(url, timeout):
    """Return (latency_s, status_code, body_bytes, content_type, error_name)."""
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
            ct = resp.headers.get("Content-Type", "")
            return time.perf_counter() - start, resp.getcode(), body, ct, None
    except urllib.error.HTTPError as e:
        return time.perf_counter() - start, e.code, b"", "", None
    except Exception as e:
        return time.perf_counter() - start, None, b"", "", type(e).__name__


def get_json(url, timeout):
    _, code, body, ct, err = http_get(url, timeout)
    if code == 200 and "application/json" in ct:
        try:
            return code, json.loads(body), err
        except Exception:
            return code, None, "bad-json"
    return code, None, err


# ----------------------------- live load stats --------------------------------
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.latencies = []
        self.status = Tally()
        self.errors = Tally()
        self.backends = set()
        self.ok = 0
        self.total = 0

    def record(self, dt, code=None, err=None, backend=None):
        with self.lock:
            self.total += 1
            self.latencies.append(dt)
            if err:
                self.errors[err] += 1
            else:
                self.status[code] += 1
                if 200 <= code < 400:
                    self.ok += 1
            if backend:
                self.backends.add(backend)

    def snapshot(self):
        with self.lock:
            return (self.total, self.ok, sum(self.errors.values()),
                    sorted(self.latencies), len(self.backends))


def percentile(values, p):
    if not values:
        return 0.0
    k = min(len(values) - 1, max(0, int(round((p / 100.0) * (len(values) - 1)))))
    return values[k]


def worker(stop, base, paths, weights, timeout, stats):
    while not stop.is_set():
        path = random.choices(paths, weights=weights, k=1)[0]
        dt, code, body, ct, err = http_get(base + path, timeout)
        backend = None
        if code and "application/json" in ct:
            try:
                backend = json.loads(body).get("served_by")
            except Exception:
                backend = None
        stats.record(dt, code, err, backend)


def ms(seconds):
    return "%6.0fms" % (seconds * 1000)


# ----------------------------- Kubernetes probes ------------------------------
def kube(args):
    try:
        return subprocess.run(["kubectl"] + args, capture_output=True,
                              text=True, timeout=8).stdout.strip()
    except Exception:
        return ""


def hpa_min_replicas():
    out = kube(["get", "hpa", "capstone", "-o",
                "jsonpath={.spec.minReplicas}"])
    try:
        return int(out)
    except Exception:
        return 2


def hpa_state():
    """Return (running_pods, desired_replicas, cpu_pct_str)."""
    pods = kube(["get", "pods", "-l", "app=capstone", "--no-headers"])
    running = sum(1 for ln in pods.splitlines()
                  if " Running " in (" " + ln + " "))
    desired = kube(["get", "hpa", "capstone", "-o",
                    "jsonpath={.status.desiredReplicas}"]) or "?"
    cpu = kube(["get", "hpa", "capstone", "-o",
                "jsonpath={.status.currentMetrics[0].resource.current.averageUtilization}"])
    return running, desired, (cpu + "%") if cpu else "--"


# ----------------------------- Prometheus probes ------------------------------
def prom_target_up(prom_url, timeout):
    code, data, _ = get_json(prom_url.rstrip("/") + "/api/v1/targets", timeout)
    if code != 200 or not data:
        return None
    for t in data.get("data", {}).get("activeTargets", []):
        if t.get("labels", {}).get("job", "").startswith("capstone"):
            return t.get("health") == "up"
    return False


def prom_query(prom_url, q, timeout):
    url = prom_url.rstrip("/") + "/api/v1/query?query=" + urllib.parse.quote(q)
    code, data, _ = get_json(url, timeout)
    if code != 200 or not data:
        return None
    result = data.get("data", {}).get("result", [])
    if not result:
        return 0.0
    try:
        return float(result[0]["value"][1])
    except Exception:
        return None


# ----------------------------- functional suite -------------------------------
def functional_checks(base, timeout):
    """Hit every route and the stateful tier BEFORE we pour on load."""
    checks = []
    redis_backed = False

    # /health
    code, body, err = get_json(base + "/health", timeout)
    ok = code == 200 and body and body.get("status") == "healthy"
    checks.append(("route /health", ok, "status=%s err=%s" % (code, err)))

    # /  -> JSON shape
    code, body, err = get_json(base + "/", timeout)
    ok = code == 200 and body and body.get("status") == "ok" and "total_hits" in body
    if body:
        redis_backed = bool(body.get("redis"))
    checks.append(("route /", ok, "status=%s total_hits=%s"
                   % (code, body.get("total_hits") if body else None)))

    # /work
    code, body, err = get_json(base + "/work?iters=2000", timeout)
    ok = code == 200 and body and body.get("status") == "done"
    checks.append(("route /work", ok, "status=%s err=%s" % (code, err)))

    # /metrics exposes the three series we built
    _, code, mbody, _, err = http_get(base + "/metrics", timeout)
    want = [b"app_requests_total", b"app_request_latency_seconds",
            b"app_requests_in_progress"]
    missing = [w.decode() for w in want if w not in mbody]
    ok = code == 200 and not missing
    checks.append(("route /metrics", ok,
                   "missing=%s" % (missing or "none")))

    # stateful tier: counter must strictly increase across calls
    seen = []
    for _ in range(5):
        code, body, err = get_json(base + "/", timeout)
        if body and "total_hits" in body:
            seen.append(body["total_hits"])
    increasing = len(seen) >= 2 and all(b > a for a, b in zip(seen, seen[1:]))
    checks.append(("counter increments", increasing, "samples=%s" % seen))
    checks.append(("redis-backed counter", redis_backed,
                   "redis=%s (false = in-memory fallback)" % redis_backed))
    return checks


# ----------------------------- main -------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Capstone full-pipeline stress test")
    ap.add_argument("--url", default="http://localhost:30080")
    ap.add_argument("--prometheus-url", default="http://localhost:9090")
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--concurrency", type=int, default=60)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--work-ratio", type=float, default=0.25)
    ap.add_argument("--max-p95-ms", type=float, default=1500.0)
    ap.add_argument("--min-success", type=float, default=99.0)
    ap.add_argument("--no-k8s", action="store_true", help="skip HPA/pod checks")
    ap.add_argument("--no-prometheus", action="store_true",
                    help="skip Prometheus scrape checks")
    ap.add_argument("--wait-scaledown", action="store_true",
                    help="after the test, wait and assert the HPA scales back down")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    use_k8s = not args.no_k8s
    use_prom = not args.no_prometheus

    print(B("\n=== Capstone FULL pipeline stress + validation ==="))
    print("target       : %s" % C(base))
    print("concurrency  : %d workers" % args.concurrency)
    print("duration     : %ds" % args.duration)
    print("traffic mix  : %d%% /    %d%% /work"
          % (round((1 - args.work_ratio) * 100), round(args.work_ratio * 100)))
    print("checks       : functional + load + slo%s%s\n"
          % (" + k8s-autoscale" if use_k8s else "",
             " + prometheus" if use_prom else ""))

    # --- Phase 0: functional correctness -------------------------------------
    print(B("--- Phase 0: functional checks (every route + state) ---"))
    func = functional_checks(base, args.timeout)
    for name, passed, detail in func:
        tag = G(" PASS ") if passed else R(" FAIL ")
        print("  [%s] %-22s %s" % (tag, name, D(detail)))
    if not func[0][1]:  # /health failed -> nothing else will work
        print(R("\n/health is down. Is the deployment up and :30080 mapped? Aborting."))
        sys.exit(2)

    min_replicas = hpa_min_replicas() if use_k8s else 0

    # --- Phase 1: load --------------------------------------------------------
    print(B("\n--- Phase 1: load test ---"))
    stats = Stats()
    stop = threading.Event()
    threads = [threading.Thread(
        target=worker,
        args=(stop, base, ["/", "/work"],
              [1.0 - args.work_ratio, args.work_ratio], args.timeout, stats),
        daemon=True) for _ in range(args.concurrency)]
    started = time.perf_counter()
    for t in threads:
        t.start()

    max_replicas = min_replicas
    print(B("  elapsed      req      rps    ok%    err     p50     p95     p99  backends"))
    print(D("  " + "-" * 76))
    last_total, last_t = 0, started
    try:
        while True:
            time.sleep(2)
            now = time.perf_counter()
            total, ok, errs, lat, backends = stats.snapshot()
            window = lat[-8000:]
            rps = (total - last_total) / max(1e-9, now - last_t)
            last_total, last_t = total, now
            succ = 100.0 * ok / max(1, total)
            succ_s = (G if succ >= args.min_success else R)("%5.1f" % succ)
            line = ("  %6.1fs %8d %8.0f  %s  %5d  %s %s %s   %4d"
                    % (now - started, total, rps, succ_s, errs,
                       ms(percentile(window, 50)), ms(percentile(window, 95)),
                       ms(percentile(window, 99)), backends))
            if use_k8s:
                running, desired, cpu = hpa_state()
                try:
                    max_replicas = max(max_replicas, running, int(desired))
                except Exception:
                    max_replicas = max(max_replicas, running)
                line += "  %s pods=%d desired=%s cpu=%s" % (C("k8s"), running, desired, cpu)
            print(line)
            if now - started >= args.duration:
                break
    except KeyboardInterrupt:
        print(Y("\ninterrupted -- wrapping up..."))
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2)

    total, ok, errs, lat, backends = stats.snapshot()
    elapsed = time.perf_counter() - started
    rps = total / max(1e-9, elapsed)
    succ = 100.0 * ok / max(1, total)
    p50, p95, p99 = percentile(lat, 50), percentile(lat, 95), percentile(lat, 99)

    # --- Phase 2: Prometheus checks ------------------------------------------
    prom_up = prom_traffic = None
    if use_prom:
        print(B("\n--- Phase 2: monitoring checks (Prometheus) ---"))
        prom_up = prom_target_up(args.prometheus_url, args.timeout)
        rate = prom_query(args.prometheus_url,
                          "sum(rate(app_requests_total[1m]))", args.timeout)
        prom_traffic = (rate is not None and rate > 0)
        print("  target 'capstone' UP : %s" % (G("yes") if prom_up else R(str(prom_up))))
        print("  recording traffic    : %s (rate=%s req/s)"
              % (G("yes") if prom_traffic else R("no"),
                 ("%.1f" % rate) if rate is not None else "n/a"))

    # --- Phase 3: optional scale-down proof ----------------------------------
    scaled_down = None
    if use_k8s and args.wait_scaledown:
        print(B("\n--- Phase 3: waiting for HPA to scale back down (up to 6 min) ---"))
        deadline = time.time() + 360
        while time.time() < deadline:
            running, desired, cpu = hpa_state()
            print("  pods=%d desired=%s cpu=%s" % (running, desired, cpu))
            try:
                if int(desired) <= min_replicas and running <= min_replicas:
                    scaled_down = True
                    break
            except Exception:
                pass
            time.sleep(15)
        scaled_down = bool(scaled_down)

    # --- Final report ---------------------------------------------------------
    print(B("\n=== Final report ==="))
    print("duration            : %.1fs" % elapsed)
    print("total requests      : %d" % total)
    print("throughput          : %.0f req/s" % rps)
    print("success rate        : %.2f%%  (%d/%d)" % (succ, ok, total))
    print("errors              : %d  %s" % (errs, dict(stats.errors) if errs else ""))
    print("status codes        : %s" % dict(stats.status))
    print("latency p50/p95/p99 : %.0f / %.0f / %.0f ms"
          % (p50 * 1000, p95 * 1000, p99 * 1000))
    print("distinct backends   : %d pod(s)" % backends)
    if use_k8s:
        print("max replicas seen   : %d (min was %d)" % (max_replicas, min_replicas))

    checks = [(n, p) for n, p, _ in func]
    checks += [
        ("availability", succ >= args.min_success),
        ("latency p95", p95 * 1000 <= args.max_p95_ms),
        ("no transport errors", errs == 0),
        ("load balanced (>=2 pods)", backends >= 2),
    ]
    if use_k8s:
        checks.append(("autoscaled up", max_replicas > min_replicas))
        if args.wait_scaledown:
            checks.append(("scaled back down", scaled_down))
    if use_prom:
        checks.append(("prometheus target up", bool(prom_up)))
        checks.append(("prometheus recording", bool(prom_traffic)))

    print(B("\n=== Pipeline health checks ==="))
    all_ok = True
    for name, passed in checks:
        all_ok = all_ok and passed
        print("  [%s] %s" % (G(" PASS ") if passed else R(" FAIL "), name))

    print()
    if all_ok:
        print(G(B(">>> PIPELINE HEALTHY -- every check passed. <<<")))
        sys.exit(0)
    print(R(B(">>> PIPELINE DEGRADED -- see the failed checks above. <<<")))
    sys.exit(1)


if __name__ == "__main__":
    main()
