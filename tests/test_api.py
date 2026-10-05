"""The API end to end, as the app calls it, against a pretend Ola Maps."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import TEST_KEY, body, client_for, make_settings


# ─── the answers the app has always read ───

def test_search_answer_has_the_shape_the_app_reads(ola):
    with client_for(ola) as client:
        r = client.get("/api/v1/location/search", params={"query": "Hazratganj"})
    assert r.status_code == 200
    assert body(r) == {
        "status": "success",
        "results": [
            {"place_name": "Hazratganj, Lucknow", "place_id": "p1"},
            {"place_name": "Hazratganj Market, Lucknow", "place_id": "p2"},
        ],
    }


def test_coordinates_answer_has_the_shape_the_app_reads(ola):
    with client_for(ola) as client:
        r = client.get("/api/v1/location/coordinates", params={"place_id": "p1"})
    assert r.status_code == 200
    assert body(r) == {"status": "success", "latitude": 26.85, "longitude": 80.94}


def test_reverse_answer_has_the_shape_the_app_reads(ola):
    with client_for(ola) as client:
        r = client.get("/api/v1/location/reverse", params={"lat": 26.8467, "lng": 80.9462})
    assert r.status_code == 200
    assert body(r) == {"status": "success", "address": "1 Mall Road, Lucknow"}


def test_route_answer_has_the_shape_and_numbers_the_app_reads(ola):
    with client_for(ola) as client:
        r = client.post(
            "/api/v1/route/calculate",
            json={"start_lat": 26.80, "start_lng": 80.90, "end_lat": 26.85, "end_lng": 80.95, "mode": "driving",
                  "added_buffer_mins": 10, "checkpoint_count": 3},
        )
    assert r.status_code == 200
    assert body(r) == {
        "status": "success",
        "data": {
            "mode": "driving",
            "ola_raw_mins": 20,
            "total_duration_mins": 30,
            "distance_km": 5.4,
            "checkpoint_count": 3,
            "safety_interval_mins": 7.5,
            "trigger_schedule": [7.5, 15.0, 22.5],
            "overview_polyline": "abc123",
        },
    }


def test_route_request_from_the_walk_screen_has_no_mode_and_means_walking(ola):
    with client_for(ola) as client:
        r = client.post("/api/v1/route/calculate", json={"start_lat": 26.8, "start_lng": 80.9, "end_lat": 26.85, "end_lng": 80.95,
                                                         "added_buffer_mins": 0, "checkpoint_count": 2})
    assert r.status_code == 200
    assert body(r)["data"]["mode"] == "walking"
    assert ola.calls[-1].url.params["mode"] == "walking"


def test_ride_is_routed_as_driving_and_says_ride(ola):
    with client_for(ola) as client:
        r = client.post("/api/v1/route/calculate", json={"start_lat": 26.8, "start_lng": 80.9, "end_lat": 26.85, "end_lng": 80.95, "mode": "ride"})
    assert body(r)["data"]["mode"] == "ride"
    assert ola.calls[-1].url.params["mode"] == "driving"


def test_the_old_health_address_still_answers(ola):
    with client_for(ola) as client:
        assert body(client.get("/")) == {"status": "running"}
        assert body(client.get("/healthz")) == {"status": "ok"}


# ─── the cache ───

def test_the_same_search_asks_ola_once(ola):
    with client_for(ola) as client:
        first = client.get("/api/v1/location/search", params={"query": "Hazratganj"})
        second = client.get("/api/v1/location/search", params={"query": "  hazratganj "})
    assert ola.count("autocomplete") == 1
    assert first.headers["X-Cache"] == "MISS"
    assert second.headers["X-Cache"] == "HIT"
    assert body(first) == body(second)


def test_changing_buffer_or_checkpoints_does_not_ask_ola_again_but_the_schedule_is_new(ola):
    trip = {"start_lat": 26.8, "start_lng": 80.9, "end_lat": 26.85, "end_lng": 80.95, "mode": "driving"}
    with client_for(ola) as client:
        a = body(client.post("/api/v1/route/calculate", json={**trip, "added_buffer_mins": 0, "checkpoint_count": 2}))
        b = body(client.post("/api/v1/route/calculate", json={**trip, "added_buffer_mins": 10, "checkpoint_count": 3}))
    assert ola.count("directions") == 1
    assert a["data"]["total_duration_mins"] == 20 and b["data"]["total_duration_mins"] == 30
    assert b["data"]["trigger_schedule"] == [7.5, 15.0, 22.5]


def test_the_cache_is_shared_through_redis(ola, fake_redis):
    # Two separate apps (two instances behind the load balancer), one Redis.
    with client_for(ola, redis=fake_redis) as one:
        one.get("/api/v1/location/coordinates", params={"place_id": "p1"})
    with client_for(ola, redis=fake_redis) as two:
        r = two.get("/api/v1/location/coordinates", params={"place_id": "p1"})
    assert ola.count("details") == 1
    assert r.headers["X-Cache"] == "HIT"


def test_redis_down_changes_nothing_for_the_caller(ola, fake_redis):
    fake_redis.fail = ConnectionError("refused")
    with client_for(ola, redis=fake_redis) as client:
        assert client.get("/api/v1/location/search", params={"query": "hazratganj"}).status_code == 200
        assert client.get("/api/v1/location/coordinates", params={"place_id": "p1"}).status_code == 200
        assert client.post("/api/v1/route/calculate", json={"start_lat": 1, "start_lng": 2, "end_lat": 3, "end_lng": 4}).status_code == 200
        ready = body(client.get("/readyz"))
    assert ready["status"] == "degraded" and ready["redis"] == "down"


def test_with_the_cache_off_everything_is_asked_every_time(ola):
    with client_for(ola, make_settings(cache_enabled=False)) as client:
        client.get("/api/v1/location/coordinates", params={"place_id": "p1"})
        r = client.get("/api/v1/location/coordinates", params={"place_id": "p1"})
    assert ola.count("details") == 2
    assert r.headers["X-Cache"] == "OFF"


# ─── what is never kept, and not-found answers ───

def test_a_place_ola_cannot_locate_is_404_and_not_kept(ola):
    with client_for(ola) as client:
        r = client.get("/api/v1/location/coordinates", params={"place_id": "ghost"})
        assert r.status_code == 404
        assert body(r)["error"]["code"] == "place_not_found"
        ola.locations["ghost"] = (26.9, 80.9)
        r = client.get("/api/v1/location/coordinates", params={"place_id": "ghost"})
    assert r.status_code == 200
    assert ola.count("details") == 2


def test_zero_zero_is_never_a_place(ola):
    ola.locations["zero"] = (0.0, 0.0)
    with client_for(ola) as client:
        assert client.get("/api/v1/location/coordinates", params={"place_id": "zero"}).status_code == 404


def test_a_spot_with_no_address_is_404_and_not_kept(ola):
    ola.address = None
    with client_for(ola) as client:
        r = client.get("/api/v1/location/reverse", params={"lat": 1, "lng": 2})
        assert r.status_code == 404
        ola.address = "Real Road"
        assert body(client.get("/api/v1/location/reverse", params={"lat": 1, "lng": 2}))["address"] == "Real Road"


def test_an_empty_search_is_200_empty_and_asked_again(ola):
    with client_for(ola) as client:
        assert body(client.get("/api/v1/location/search", params={"query": "zzzz"}))["results"] == []
        client.get("/api/v1/location/search", params={"query": "zzzz"})
    assert ola.count("autocomplete") == 2


def test_a_blank_search_asks_nobody(ola):
    with client_for(ola) as client:
        r = client.get("/api/v1/location/search", params={"query": "   "})
    assert body(r) == {"status": "success", "results": []}
    assert ola.calls == []


def test_no_route_is_404(ola):
    ola.route = None
    with client_for(ola) as client:
        r = client.post("/api/v1/route/calculate", json={"start_lat": 1, "start_lng": 2, "end_lat": 3, "end_lng": 4})
    assert r.status_code == 404
    assert body(r)["error"]["code"] == "no_route"


def test_a_route_without_a_line_is_404_and_not_kept(ola):
    ola.route = {"legs": [{"duration": 60, "distance": 100}], "overview_polyline": ""}
    with client_for(ola) as client:
        for _ in range(2):
            assert client.post("/api/v1/route/calculate", json={"start_lat": 1, "start_lng": 2, "end_lat": 3, "end_lng": 4}).status_code == 404
    assert ola.count("directions") == 2


# ─── bad requests are refused before Ola is asked ───

@pytest.mark.parametrize(
    "change",
    [
        {"checkpoint_count": 10_000_000},
        {"checkpoint_count": -1},
        {"added_buffer_mins": -5},
        {"added_buffer_mins": 100_000},
        {"start_lat": 91},
        {"end_lng": -181},
        {"mode": "flying"},
        {"start_lat": "north"},
    ],
)
def test_bad_route_requests_are_422_and_cost_nothing(ola, change):
    trip = {"start_lat": 26.8, "start_lng": 80.9, "end_lat": 26.85, "end_lng": 80.95}
    with client_for(ola) as client:
        r = client.post("/api/v1/route/calculate", json={**trip, **change})
    assert r.status_code == 422
    assert body(r)["error"]["code"] == "invalid_request"
    assert ola.calls == []


def test_checkpoint_count_zero_is_allowed(ola):
    with client_for(ola) as client:
        r = client.post("/api/v1/route/calculate", json={"start_lat": 1, "start_lng": 2, "end_lat": 3, "end_lng": 4, "checkpoint_count": 0})
    assert r.status_code == 200
    assert body(r)["data"]["trigger_schedule"] == []


@pytest.mark.parametrize(
    "path,params",
    [
        ("/api/v1/location/search", {"query": "x" * 201}),
        ("/api/v1/location/search", {}),
        ("/api/v1/location/coordinates", {"place_id": "p" * 257}),
        ("/api/v1/location/reverse", {"lat": 95, "lng": 1}),
        ("/api/v1/location/reverse", {"lat": "nan", "lng": 1}),
        ("/api/v1/location/reverse", {"lat": 1}),
    ],
)
def test_bad_lookups_are_422_and_cost_nothing(ola, path, params):
    with client_for(ola) as client:
        r = client.get(path, params=params)
    assert r.status_code == 422
    assert ola.calls == []


def test_a_422_never_echoes_what_was_sent(ola):
    with client_for(ola) as client:
        r = client.post("/api/v1/route/calculate", json={"start_lat": "secret-looking-text", "start_lng": 1, "end_lat": 1, "end_lng": 1})
    assert "secret-looking-text" not in r.text


# ─── when Ola fails ───

def test_ola_timeout_is_504(ola):
    ola.override = lambda request: httpx.ReadTimeout("slow")
    with client_for(ola) as client:
        r = client.get("/api/v1/location/search", params={"query": "hazratganj"})
    assert r.status_code == 504
    assert body(r)["error"]["code"] == "maps_timeout"


def test_ola_error_is_502_and_tried_twice(ola):
    ola.override = lambda request: httpx.Response(502, json={})
    with client_for(ola) as client:
        r = client.get("/api/v1/location/search", params={"query": "hazratganj"})
    assert r.status_code == 502
    assert ola.count("autocomplete") == 2


def test_a_wrong_or_exhausted_ola_key_is_503_and_not_retried(ola):
    ola.override = lambda request: httpx.Response(403, json={})
    with client_for(ola) as client:
        r = client.get("/api/v1/location/search", params={"query": "hazratganj"})
    assert r.status_code == 503
    assert ola.count("autocomplete") == 1


def test_after_repeated_failures_ola_is_rested_and_requests_fail_at_once(ola):
    ola.override = lambda request: httpx.Response(500, json={})
    with client_for(ola, make_settings(breaker_failures=3, ola_retries=0)) as client:
        for _ in range(3):
            assert client.get("/api/v1/location/search", params={"query": "hazratganj"}).status_code == 502
        r = client.get("/api/v1/location/search", params={"query": "hazratganj"})
        ready = body(client.get("/readyz"))
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "15"
    assert ola.count("autocomplete") == 3, "the fourth was not sent"
    assert ready["ola"] == "open"


def test_an_ola_answer_that_is_not_json_is_502(ola):
    ola.override = lambda request: httpx.Response(200, content=b"<html>oops</html>")
    with client_for(ola) as client:
        assert client.get("/api/v1/location/search", params={"query": "hazratganj"}).status_code == 502


def test_a_route_answer_in_a_strange_shape_is_502_not_500(ola):
    ola.route = {"legs": "nope", "overview_polyline": "abc"}
    with client_for(ola) as client:
        r = client.post("/api/v1/route/calculate", json={"start_lat": 1, "start_lng": 2, "end_lat": 3, "end_lng": 4})
    assert r.status_code == 502


def test_the_ola_key_never_appears_in_any_response(ola):
    failures = [
        lambda request: httpx.ConnectError(f"cannot connect to {request.url}"),
        lambda request: httpx.Response(400, text=f"bad request for {request.url}"),
        lambda request: httpx.Response(500, text=f"boom {request.url}"),
        lambda request: httpx.ReadTimeout(f"timeout {request.url}"),
    ]
    for failure in failures:
        ola.override = failure
        with client_for(ola) as client:
            for path, params in [
                ("/api/v1/location/search", {"query": "abc"}),
                ("/api/v1/location/coordinates", {"place_id": "p1"}),
                ("/api/v1/location/reverse", {"lat": 1, "lng": 2}),
            ]:
                r = client.get(path, params=params)
                assert TEST_KEY not in r.text
                assert "olamaps" not in r.text


def test_the_ola_key_never_appears_in_the_logs(ola, capsys):
    # The JSON log goes to stdout (app/log.py).
    ola.override = lambda request: httpx.ConnectError(f"cannot connect to {request.url}")
    with client_for(ola, make_settings(log_level="DEBUG")) as client:
        client.get("/api/v1/location/search", params={"query": "hazratganj"})
    out = capsys.readouterr().out
    assert '"Ola call failed"' in out, "the failure was logged"
    assert TEST_KEY not in out
    assert "hazratganj" not in out, "nor what was searched"
    assert "olamaps" not in out


def test_the_key_is_sent_to_ola(ola):
    with client_for(ola) as client:
        client.get("/api/v1/location/search", params={"query": "hazratganj"})
    assert ola.calls[0].url.params["api_key"] == TEST_KEY
    assert ola.calls[0].headers["X-Request-Id"], "Ola gets our request id, to trace a call with them"


# ─── health ───

def test_readyz_when_everything_is_fine(ola, fake_redis):
    with client_for(ola, redis=fake_redis) as client:
        assert body(client.get("/readyz")) == {"status": "ok", "redis": "ok", "ola": "closed", "auth": "off"}


def test_the_app_refuses_to_start_without_an_ola_key(ola):
    from app.config import ConfigError

    with pytest.raises(ConfigError):
        with client_for(ola, make_settings(ola_api_key="")):
            pass


def test_api_docs_are_not_published_in_production(ola):
    with client_for(ola, make_settings(environment="production")) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
