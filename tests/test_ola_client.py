"""The Ola client on its own: shapes, retries, the breaker, the connection limit."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.clients.ola import CircuitBreaker, OlaClient, OlaUnavailable
from tests.conftest import FakeOla, make_settings


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def no_sleep(_: float) -> None:
    return None


def client(ola: FakeOla, **settings) -> OlaClient:
    http = httpx.AsyncClient(base_url="https://ola.test", transport=ola.transport())
    return OlaClient(http, make_settings(**settings), sleep=no_sleep)


@pytest.mark.anyio
async def test_autocomplete_skips_broken_entries():
    ola = FakeOla()
    ola.places["x"] = [{"description": "Good", "place_id": "g"}, {"description": "", "place_id": "e"}, {"place_id": "n"}, "junk"]
    assert await client(ola).autocomplete("x") == [{"place_name": "Good", "place_id": "g"}]


@pytest.mark.anyio
async def test_place_ids_ola_rejects_are_not_found_not_errors():
    ola = FakeOla()
    ola.override = lambda r: httpx.Response(400, json={"error": "invalid place id"})
    assert await client(ola).place_location("bad") is None
    assert await client(ola).reverse_geocode(1, 2) is None
    assert await client(ola).directions((1, 2), (3, 4), "walking") is None


@pytest.mark.anyio
async def test_route_minutes_and_kilometres_are_rounded_as_before():
    ola = FakeOla()
    ola.route = {"legs": [{"duration": 1290, "distance": 5432}], "overview_polyline": "p"}
    assert await client(ola).directions((1, 2), (3, 4), "driving") == {"ola_raw_mins": 22, "distance_km": 5.43, "overview_polyline": "p"}


@pytest.mark.anyio
async def test_a_fast_failure_is_tried_once_more_and_can_succeed():
    ola = FakeOla()
    answers = iter([httpx.Response(503), None])
    ola.override = lambda r: next(answers)
    assert await client(ola).autocomplete("hazratganj")
    assert ola.count("autocomplete") == 2


@pytest.mark.anyio
async def test_a_timeout_is_not_retried():
    ola = FakeOla()
    ola.override = lambda r: httpx.ReadTimeout("slow")
    with pytest.raises(OlaUnavailable) as caught:
        await client(ola).autocomplete("hazratganj")
    assert caught.value.reason == "timeout"
    assert ola.count("autocomplete") == 1


@pytest.mark.anyio
async def test_the_error_never_carries_the_url():
    ola = FakeOla()
    ola.override = lambda r: httpx.ConnectError(f"failed {r.url}")
    with pytest.raises(OlaUnavailable) as caught:
        await client(ola).autocomplete("hazratganj")
    assert "api_key" not in str(caught.value) and "olamaps" not in str(caught.value)


@pytest.mark.anyio
async def test_at_most_n_calls_to_ola_at_once():
    ola = FakeOla()
    ola.delay = 0.05
    in_flight = 0
    most = 0
    original = ola.handle

    async def counting(request):
        nonlocal in_flight, most
        in_flight += 1
        most = max(most, in_flight)
        try:
            return await original(request)
        finally:
            in_flight -= 1

    ola.handle = counting  # type: ignore[method-assign]
    c = client(ola, ola_max_connections=3)
    await asyncio.gather(*(c.autocomplete("hazratganj") for _ in range(12)))
    assert most == 3


def test_breaker_opens_after_n_failures_then_lets_one_trial_through():
    clock = Clock()
    breaker = CircuitBreaker(failures=3, cooldown=10, clock=clock)
    for _ in range(3):
        assert breaker.allow()
        breaker.failure()
    assert breaker.state == "open" and not breaker.allow()
    clock.now += 11
    assert breaker.allow(), "the trial"
    assert not breaker.allow(), "only one trial at a time"
    breaker.success()
    assert breaker.state == "closed" and breaker.allow()


def test_a_failed_trial_opens_it_again():
    clock = Clock()
    breaker = CircuitBreaker(failures=1, cooldown=10, clock=clock)
    breaker.failure()
    clock.now += 11
    assert breaker.allow()
    breaker.failure()
    assert breaker.state == "open"


@pytest.mark.anyio
async def test_a_cancelled_trial_does_not_leave_it_shut_for_ever():
    ola = FakeOla()
    ola.delay = 1.0
    c = client(ola, breaker_failures=1)
    c.breaker.failure()  # open
    c.breaker._open_until = 0  # cooldown over: half open
    task = asyncio.ensure_future(c.autocomplete("hazratganj"))  # the trial
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert c.breaker.allow(), "another trial may go"
