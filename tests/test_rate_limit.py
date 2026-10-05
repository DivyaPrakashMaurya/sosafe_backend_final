from __future__ import annotations

import pytest

from app.cache import RedisStore
from app.rate_limit import RateLimiter, bucket_id
from tests.conftest import FakeRedis, body, client_for, make_settings
from tests.test_auth import PROJECT, Google, token


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.mark.anyio
async def test_within_the_limit_then_refused_then_a_new_minute():
    clock = Clock(1_000_040.0)  # 20 s into a minute (minutes start at multiples of 60)
    limiter = RateLimiter(clock=clock)
    decisions = [await limiter.hit("b", 3) for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[-1].retry_after == 40
    clock.now += 41
    assert (await limiter.hit("b", 3)).allowed


@pytest.mark.anyio
async def test_buckets_are_separate():
    limiter = RateLimiter(clock=Clock())
    for _ in range(3):
        await limiter.hit("a", 3)
    assert (await limiter.hit("b", 3)).allowed


@pytest.mark.anyio
async def test_counted_in_redis_so_every_instance_shares_the_count():
    redis = FakeRedis()
    one = RateLimiter(RedisStore(redis), clock=Clock())
    two = RateLimiter(RedisStore(redis), clock=Clock())
    await one.hit("b", 2)
    await two.hit("b", 2)
    assert not (await one.hit("b", 2)).allowed
    key = next(k for k in redis.data if ":rl:" in k)
    assert redis.ttl[key] == 65, "the count goes away a little after its minute"


@pytest.mark.anyio
async def test_redis_down_counts_in_memory_and_never_blocks():
    redis = FakeRedis()
    redis.fail = ConnectionError("down")
    limiter = RateLimiter(RedisStore(redis), clock=Clock())
    assert (await limiter.hit("b", 1)).allowed
    assert not (await limiter.hit("b", 1)).allowed


@pytest.mark.anyio
async def test_switched_off_allows_everything():
    limiter = RateLimiter(enabled=False)
    for _ in range(100):
        assert (await limiter.hit("b", 1)).allowed


def test_ids_are_never_stored_as_they_are():
    assert "user-123" not in bucket_id("all", "user:user-123")
    assert bucket_id("all", "user:a") != bucket_id("all", "user:b")


# ─── through the API ───

def test_too_many_requests_is_429_with_retry_after(ola):
    with client_for(ola, make_settings(rate_limit_anonymous_per_minute=3)) as client:
        codes = [client.get("/api/v1/location/search", params={"query": "hazratganj"}).status_code for _ in range(4)]
        r = client.get("/api/v1/location/search", params={"query": "hazratganj"})
    assert codes == [200, 200, 200, 429]
    assert body(r)["error"]["code"] == "rate_limited"
    assert 1 <= int(r.headers["Retry-After"]) <= 60


def test_route_has_its_own_tighter_limit(ola):
    trip = {"start_lat": 1, "start_lng": 2, "end_lat": 3, "end_lng": 4}
    with client_for(ola, make_settings(rate_limit_route_per_minute=2)) as client:
        codes = [client.post("/api/v1/route/calculate", json=trip).status_code for _ in range(3)]
        search = client.get("/api/v1/location/search", params={"query": "hazratganj"})
    assert codes == [200, 200, 429]
    assert search.status_code == 200, "other lookups are not held back by it"


def test_signed_in_people_are_counted_each_on_their_own(ola):
    settings = make_settings(auth_mode="required", firebase_project_id=PROJECT, rate_limit_user_per_minute=2)
    with client_for(ola, settings, google=Google().transport()) as client:
        alice = {"Authorization": f"Bearer {token('alice')}"}
        bob = {"Authorization": f"Bearer {token('bob')}"}
        a = [client.get("/api/v1/location/search", params={"query": "x"}, headers=alice).status_code for _ in range(3)]
        b = client.get("/api/v1/location/search", params={"query": "x"}, headers=bob).status_code
    assert a == [200, 200, 429]
    assert b == 200, "bob is not held back by alice"


def test_behind_the_load_balancer_the_real_address_is_counted(ola):
    # Render appends the address it saw; whatever the caller wrote before that is ignored.
    with client_for(ola, make_settings(rate_limit_anonymous_per_minute=2, trusted_proxy_hops=1)) as client:
        def ask(real: str, spoofed: str):
            return client.get(
                "/api/v1/location/search", params={"query": "x"}, headers={"X-Forwarded-For": f"{spoofed}, {real}"}
            ).status_code

        codes = [ask("1.1.1.1", f"9.9.9.{i}") for i in range(3)]  # a new made-up address each time
        other = ask("2.2.2.2", "9.9.9.9")
    assert codes == [200, 200, 429], "made-up addresses do not get around the limit"
    assert other == 200
