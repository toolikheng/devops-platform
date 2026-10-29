import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import event

from app.main import app, engine
from app.metrics import registry

# The `client` fixture lives in conftest.py.


def sample(name: str, **labels: str) -> float:
    return registry.get_sample_value(name, labels) or 0.0


def route_labels() -> set[str]:
    return {
        s.labels["route"]
        for metric in registry.collect()
        for s in metric.samples
        if "route" in s.labels
    }


def test_root(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "DevOps Platform API"}


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "dependencies": {"database": "connected"}}


def test_metrics_endpoint(client: TestClient) -> None:
    # Make a request to generate metrics
    client.get("/")

    response = client.get("/metrics")
    assert response.status_code == 200
    assert b"http_requests_total" in response.content
    assert b"http_request_duration_seconds" in response.content
    # Verify histogram has recorded duration (bucket values present)
    assert b"_bucket" in response.content or b"_sum" in response.content


def test_create_item(client: TestClient) -> None:
    item_data = {"title": "Test Item", "description": "A test item"}
    response = client.post("/items", json=item_data)
    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Test Item"
    assert data["description"] == "A test item"
    assert "id" in data


def test_list_items(client: TestClient) -> None:
    client.post("/items", json={"title": "Item 1", "description": "First"})
    client.post("/items", json={"title": "Item 2", "description": "Second"})
    response = client.get("/items")
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 2  # exact: each test starts with an empty database


def test_get_item(client: TestClient) -> None:
    create_response = client.post("/items", json={"title": "Test", "description": "Desc"})
    item_id = create_response.json()["id"]
    response = client.get(f"/items/{item_id}")
    assert response.status_code == 200
    assert response.json()["title"] == "Test"


def test_update_item(client: TestClient) -> None:
    create_response = client.post("/items", json={"title": "Original", "description": "Old"})
    item_id = create_response.json()["id"]
    update_data = {"title": "Updated", "description": "New"}
    response = client.put(f"/items/{item_id}", json=update_data)
    assert response.status_code == 200
    assert response.json()["title"] == "Updated"


def test_delete_item(client: TestClient) -> None:
    create_response = client.post("/items", json={"title": "To Delete", "description": "X"})
    item_id = create_response.json()["id"]
    response = client.delete(f"/items/{item_id}")
    assert response.status_code == 204
    get_response = client.get(f"/items/{item_id}")
    assert get_response.status_code == 404


def test_tests_use_a_throwaway_database() -> None:
    db_path = Path(engine.url.database or "").resolve()
    assert db_path != (Path.cwd() / "app.db").resolve()
    assert Path(tempfile.gettempdir()).resolve() in db_path.parents


def test_db_endpoints_run_concurrently(client: TestClient) -> None:
    """Slow queries must not block the event loop: n requests' queries must overlap.

    Each items query waits at a barrier that only opens once all n queries are in flight
    at the same time, so the test proves overlap without depending on timing. If handlers
    block the event loop, the first query holds it and the barrier times out.
    """
    n = 4
    barrier = threading.Barrier(n, timeout=10)
    spans: list[tuple[float, float]] = []
    lock = threading.Lock()

    def hold_query(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "items" in statement:
            start = time.monotonic()
            barrier.wait()
            with lock:
                spans.append((start, time.monotonic()))

    event.listen(engine, "before_cursor_execute", hold_query)
    try:
        with ThreadPoolExecutor(n) as pool:
            responses = list(pool.map(lambda _: client.get("/items"), range(n)))
    finally:
        event.remove(engine, "before_cursor_execute", hold_query)

    assert [r.status_code for r in responses] == [200] * n
    assert len(spans) == n
    assert max(start for start, _ in spans) < min(end for _, end in spans)


def test_server_errors_are_counted_as_500(client: TestClient) -> None:
    def fail_insert(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().upper().startswith("INSERT"):
            raise RuntimeError("simulated database failure")

    labels = {"method": "POST", "route": "/items"}
    before_500 = sample("http_requests_total", **labels, status="500")
    before_201 = sample("http_requests_total", **labels, status="201")

    event.listen(engine, "before_cursor_execute", fail_insert)
    try:
        response = TestClient(app, raise_server_exceptions=False).post(
            "/items", json={"title": "x", "description": "y"}
        )
    finally:
        event.remove(engine, "before_cursor_execute", fail_insert)

    assert response.status_code == 500
    assert sample("http_requests_total", **labels, status="500") == before_500 + 1
    assert sample("http_requests_total", **labels, status="201") == before_201


def test_metric_labels_use_route_patterns(client: TestClient) -> None:
    ids = [
        client.post("/items", json={"title": "t", "description": "d"}).json()["id"]
        for _ in range(2)
    ]
    for item_id in ids:
        client.get(f"/items/{item_id}")
    client.get("/no-such-page-12345")

    labels = route_labels()
    assert "/items/{item_id}" in labels
    assert "unmatched" in labels
    assert not {f"/items/{item_id}" for item_id in ids} & labels
    assert "/no-such-page-12345" not in labels
