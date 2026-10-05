from __future__ import annotations

import gzip

from tests.conftest import body, client_for, make_settings


def test_every_response_has_a_request_id(ola):
    with client_for(ola) as client:
        a = client.get("/healthz")
        b = client.get("/healthz")
    assert len(a.headers["X-Request-ID"]) == 32
    assert a.headers["X-Request-ID"] != b.headers["X-Request-ID"]
    assert a.headers["X-Content-Type-Options"] == "nosniff"


def test_a_sensible_incoming_request_id_is_kept_and_a_strange_one_replaced(ola):
    with client_for(ola) as client:
        kept = client.get("/healthz", headers={"X-Request-ID": "app-abc12345"})
        replaced = client.get("/healthz", headers={"X-Request-ID": "<script>alert(1)</script>"})
    assert kept.headers["X-Request-ID"] == "app-abc12345"
    assert "<" not in replaced.headers["X-Request-ID"]


def test_errors_carry_the_request_id(ola):
    with client_for(ola) as client:
        r = client.get("/api/v1/location/coordinates", params={"place_id": "ghost"})
    assert body(r)["request_id"] == r.headers["X-Request-ID"]


def test_an_oversized_body_is_413_without_being_read(ola):
    with client_for(ola, make_settings(max_body_bytes=1024)) as client:
        r = client.post("/api/v1/route/calculate", content=b"{" + b" " * 5000 + b"}", headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert body(r)["error"]["code"] == "body_too_large"
    assert ola.calls == []


def test_an_oversized_body_without_a_length_is_413_too(ola):
    def chunks():
        for _ in range(10):
            yield b" " * 500

    with client_for(ola, make_settings(max_body_bytes=1024)) as client:
        r = client.post("/api/v1/route/calculate", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_an_unexpected_failure_is_a_clean_500_with_the_request_id(ola):
    def explode(request):
        raise RuntimeError("something internal with a secret path C:/x")

    ola.override = explode
    with client_for(ola) as client:
        r = client.get("/api/v1/location/search", params={"query": "hazratganj"})
    assert r.status_code == 500
    assert body(r)["error"]["code"] == "internal_error"
    assert body(r)["request_id"] == r.headers["X-Request-ID"]
    assert "secret" not in r.text


def test_unknown_address_is_404_in_the_error_shape(ola):
    with client_for(ola) as client:
        r = client.get("/api/v1/nothing-here")
    assert r.status_code == 404
    assert body(r)["error"]["code"] == "not_found"


def test_wrong_method_is_405_in_the_error_shape(ola):
    with client_for(ola) as client:
        r = client.get("/api/v1/route/calculate")
    assert r.status_code == 405
    assert body(r)["error"]["code"] == "method_not_allowed"


def test_large_answers_are_compressed_for_phones_that_accept_it(ola):
    ola.route = {"legs": [{"duration": 1200, "distance": 5400}], "overview_polyline": "x" * 5000}
    with client_for(ola) as client:
        r = client.post(
            "/api/v1/route/calculate",
            json={"start_lat": 1, "start_lng": 2, "end_lat": 3, "end_lng": 4},
            headers={"Accept-Encoding": "gzip"},
        )
    assert r.headers.get("content-encoding") == "gzip"
    assert body(r)["data"]["overview_polyline"] == "x" * 5000  # httpx undoes the gzip


def test_the_access_log_has_the_path_but_never_the_query(ola, capsys):
    with client_for(ola) as client:
        client.get("/api/v1/location/search", params={"query": "my-home-street"})
        client.get("/healthz")
    out = capsys.readouterr().out
    assert '"path": "/api/v1/location/search"' in out
    assert "my-home-street" not in out
    assert '"path": "/healthz"' not in out, "the load balancer's checks are not logged"


def test_no_server_header_leaks_versions(ola):
    with client_for(ola) as client:
        r = client.get("/healthz")
    assert "uvicorn" not in r.headers.get("server", "")
    _ = gzip  # (module used above via httpx)
