from app import app


def _client():
    return app.test_client()


def test_health():
    r = _client().get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "healthy"


def test_home_counts():
    r = _client().get("/")
    body = r.get_json()
    assert r.status_code == 200
    assert body["status"] == "ok"
    assert body["total_hits"] >= 1


def test_metrics_exposes_counter():
    r = _client().get("/metrics")
    assert r.status_code == 200
    assert b"app_requests_total" in r.data