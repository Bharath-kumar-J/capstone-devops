import hashlib
import os
import socket
import time

from flask import Flask, Response, g, jsonify, request
from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Gauge, Histogram,
                               generate_latest)

app = Flask(__name__)
HOSTNAME = socket.gethostname()   # the pod name once it runs in Kubernetes

# --- Prometheus metrics (scraped at /metrics) ---
REQS = Counter("app_requests_total", "Total HTTP requests",
               ["method", "endpoint", "http_status"])
LATENCY = Histogram("app_request_latency_seconds", "Request latency in seconds",
                    ["endpoint"])
INPROGRESS = Gauge("app_requests_in_progress", "In-flight requests right now")

# --- Optional Redis-backed counter (the stateful tier) ---
try:
    import redis
    _r = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379,
                     socket_connect_timeout=1, socket_timeout=1)
    _r.ping()
    REDIS_OK = True
except Exception:
    _r = None
    REDIS_OK = False

_mem = {"hits": 0}


def bump():
    """Increment the hit counter in Redis, falling back to memory."""
    if _r is not None:
        try:
            return _r.incr("hits")
        except Exception:
            pass
    _mem["hits"] += 1
    return _mem["hits"]


@app.before_request
def _start_timer():
    g.t0 = time.perf_counter()
    INPROGRESS.inc()


@app.after_request
def _record(resp):
    INPROGRESS.dec()
    endpoint = request.endpoint or "unknown"
    LATENCY.labels(endpoint).observe(time.perf_counter() - getattr(g, "t0", time.perf_counter()))
    REQS.labels(request.method, endpoint, resp.status_code).inc()
    return resp


@app.route("/")
def home():
    return jsonify(message="Capstone DevOps app", status="ok",
                   served_by=HOSTNAME, total_hits=bump(), redis=REDIS_OK)


@app.route("/health")
def health():
    return jsonify(status="healthy", served_by=HOSTNAME)


@app.route("/work")
def work():
    # Deliberate CPU burn so load drives CPU up and the autoscaler reacts.
    iterations = int(request.args.get("iters", 20000))
    digest = b"capstone-seed"
    for _ in range(iterations):
        digest = hashlib.sha256(digest).digest()
    return jsonify(status="done", iterations=iterations, served_by=HOSTNAME)


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)