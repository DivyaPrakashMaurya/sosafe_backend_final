from __future__ import annotations

import asyncio
import json

import pytest

from app import cache as c
from tests.conftest import FakeRedis


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


TTL = {"search": 100, "coordinates": 100, "reverse": 100, "route": 100}


def make(redis: FakeRedis | None = None, clock: Clock | None = None, **kw) -> c.LookupCache:
    clock = clock or Clock()
    store = c.RedisStore(redis, clock=clock) if redis is not None else None
    return c.LookupCache(redis_store=store, memory_store=c.MemoryStore(clock=clock), ttl_seconds=kw.pop("ttl", TTL), **kw)


# ─── keys ───

def test_search_spelling_does_not_matter():
    assert c.search_key("  Hazratganj   Market ") == c.search_key("hazratganj market")
    assert c.search_key("   ") is None


def test_place_ids_are_case_sensitive_and_long_ones_hashed():
    assert c.coordinates_key("ChIJabc") != c.coordinates_key("chijabc")
    assert len(c.coordinates_key("x" * 500)) < 200


def test_spots_a_metre_apart_are_one_key_and_ten_metres_apart_two():
    assert c.reverse_key(26.846694, 80.946166) == c.reverse_key(26.8466941, 80.9461662)
    assert c.reverse_key(26.84669, 80.94616) != c.reverse_key(26.84679, 80.94616)
    assert c.reverse_key(-0.0, 1.0) == c.reverse_key(0.0, 1.0)


def test_route_keys_depend_on_mode_and_direction():
    k = c.route_key("driving", (26.1, 80.1), (26.2, 80.2))
    assert k != c.route_key("walking", (26.1, 80.1), (26.2, 80.2))
    assert k != c.route_key("driving", (26.2, 80.2), (26.1, 80.1))


# ─── get / put ───

@pytest.mark.anyio
async def test_put_then_get():
    cache = make()
    await cache.put("search", "k", [{"a": 1}])
    assert await cache.get("search", "k") == [{"a": 1}]


@pytest.mark.anyio
async def test_entries_expire():
    clock = Clock()
    cache = make(clock=clock)
    await cache.put("search", "k", [1])
    clock.now += 101
    assert await cache.get("search", "k") is None


@pytest.mark.anyio
async def test_a_kind_with_no_lifetime_is_never_kept():
    cache = make(ttl={**TTL, "route": 0})
    await cache.put("route", "k", {"x": 1})
    assert await cache.get("route", "k") is None


@pytest.mark.anyio
async def test_redis_is_used_with_the_kinds_lifetime():
    redis = FakeRedis()
    cache = make(redis)
    await cache.put("search", "k", [1])
    assert redis.ttl["k"] == 100
    assert await cache.get("search", "k") == [1]


@pytest.mark.anyio
async def test_a_damaged_entry_is_a_miss():
    redis = FakeRedis()
    redis.data["k"] = "{oops"
    assert await make(redis).get("search", "k") is None


@pytest.mark.anyio
async def test_redis_down_pauses_it_and_memory_takes_over():
    redis = FakeRedis()
    redis.fail = ConnectionError("down")
    clock = Clock()
    cache = make(redis, clock=clock)
    assert await cache.get("search", "k") is None
    calls = redis.calls
    await cache.put("search", "k", [1])
    assert await cache.get("search", "k") == [1], "kept in memory meanwhile"
    assert redis.calls == calls, "Redis not asked during the pause"
    redis.fail = None
    clock.now += 31
    await cache.put("search", "k2", [2])
    assert json.loads(redis.data["k2"]) == [2], "back on Redis after the pause"


@pytest.mark.anyio
async def test_nothing_redis_raises_reaches_the_caller():
    for error in (ConnectionError(), TimeoutError(), RuntimeError(), OSError()):
        redis = FakeRedis()
        redis.fail = error
        cache = make(redis)
        await cache.put("search", "k", [1])
        assert await cache.get("search", "k") == [1]


# ─── get_or_fetch ───

@pytest.mark.anyio
async def test_a_miss_fetches_and_keeps_a_good_answer():
    cache = make()
    calls = []

    async def fetch():
        calls.append(1)
        return [1]

    assert await cache.get_or_fetch("search", "k", fetch, keep=bool) == ([1], "miss")
    assert await cache.get_or_fetch("search", "k", fetch, keep=bool) == ([1], "hit")
    assert len(calls) == 1


@pytest.mark.anyio
async def test_an_answer_not_worth_keeping_is_asked_for_again():
    cache = make()
    calls = []

    async def fetch():
        calls.append(1)
        return []

    await cache.get_or_fetch("search", "k", fetch, keep=bool)
    await cache.get_or_fetch("search", "k", fetch, keep=bool)
    assert len(calls) == 2


@pytest.mark.anyio
async def test_a_hundred_requests_for_the_same_missing_answer_make_one_call():
    cache = make()
    calls = []

    async def fetch():
        calls.append(1)
        await asyncio.sleep(0.05)
        return [1]

    results = await asyncio.gather(*(cache.get_or_fetch("search", "k", fetch, keep=bool) for _ in range(100)))
    assert len(calls) == 1
    assert {value[0] for value, _ in results} == {1}
    assert sorted(how for _, how in results).count("miss") == 1


@pytest.mark.anyio
async def test_a_failure_reaches_everyone_waiting_and_is_not_kept():
    cache = make()

    async def fetch():
        await asyncio.sleep(0.01)
        raise ValueError("ola down")

    results = await asyncio.gather(*(cache.get_or_fetch("search", "k", fetch, keep=bool) for _ in range(5)), return_exceptions=True)
    assert all(isinstance(r, ValueError) for r in results)
    assert await cache.get("search", "k") is None


@pytest.mark.anyio
async def test_a_request_that_gives_up_does_not_cancel_the_answer_for_the_others():
    cache = make()

    async def fetch():
        await asyncio.sleep(0.05)
        return [7]

    first = asyncio.ensure_future(cache.get_or_fetch("search", "k", fetch, keep=bool))
    await asyncio.sleep(0.01)
    second = asyncio.ensure_future(cache.get_or_fetch("search", "k", fetch, keep=bool))
    await asyncio.sleep(0.01)
    first.cancel()  # that phone went away
    assert await second == ([7], "shared")
    assert await cache.get("search", "k") == [7], "and it was kept"


@pytest.mark.anyio
async def test_cache_off_still_answers():
    cache = make(enabled=False)

    async def fetch():
        return [1]

    assert await cache.get_or_fetch("search", "k", fetch, keep=bool) == ([1], "off")
