"""Pretend services (Ola Maps, Redis, Google's signing keys) and an app wired to them.

Nothing here touches the network: Ola and Google answer through httpx's
MockTransport, Redis is an in-memory stand-in.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace
from typing import Any, Callable, Optional

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TEST_KEY = "test-ola-key-not-real-0123456789"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_settings(**overrides: Any) -> Settings:
    base = Settings(
        environment="test",
        ola_api_key=TEST_KEY,
        auth_mode="off",
        trusted_proxy_hops=0,
    )
    return replace(base, **overrides)


class FakeOla:
    """Answers like Ola Maps. Records every call (path + params)."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.places: dict[str, list[dict[str, str]]] = {
            "hazratganj": [
                {"description": "Hazratganj, Lucknow", "place_id": "p1"},
                {"description": "Hazratganj Market, Lucknow", "place_id": "p2"},
            ]
        }
        self.locations: dict[str, tuple[float, float]] = {"p1": (26.85, 80.94), "p2": (26.86, 80.95)}
        self.address = "1 Mall Road, Lucknow"
        self.route = {"legs": [{"duration": 1200, "distance": 5400}], "overview_polyline": "abc123"}
        # A function (request) -> httpx.Response | Exception | None, consulted first.
        self.override: Optional[Callable[[httpx.Request], Any]] = None
        self.delay = 0.0

    def count(self, part: str) -> int:
        return sum(1 for r in self.calls if part in r.url.path)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.override is not None:
            result = self.override(request)
            if isinstance(result, Exception):
                raise result
            if result is not None:
                return result
        path, params = request.url.path, request.url.params
        if path.endswith("/autocomplete"):
            query = params["input"].strip().lower()
            return httpx.Response(200, json={"predictions": self.places.get(query, [])})
        if path.endswith("/details"):
            found = self.locations.get(params["place_id"])
            if found is None:
                return httpx.Response(200, json={"result": {}})
            return httpx.Response(200, json={"result": {"geometry": {"location": {"lat": found[0], "lng": found[1]}}}})
        if path.endswith("/reverse-geocode"):
            if self.address is None:
                return httpx.Response(200, json={"results": []})
            return httpx.Response(200, json={"results": [{"formatted_address": self.address}]})
        if path.endswith("/directions"):
            return httpx.Response(200, json={"routes": [self.route] if self.route else []})
        return httpx.Response(404, json={})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


class FakeOverpass:
    """Answers like OpenStreetMap's Overpass API. Records every question (url + the query text)."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        # What an answer holds: Overpass "elements".
        self.elements: list[dict[str, Any]] = []
        # A function (request) -> httpx.Response | Exception | None, consulted first.
        self.override: Optional[Callable[[httpx.Request], Any]] = None

    def queries(self) -> list[str]:
        out = []
        for call in self.calls:
            raw = call.content.decode("utf-8")
            out.append(httpx.QueryParams(raw).get("data", ""))
        return out

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.override is not None:
            result = self.override(request)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, Exception):
                raise result
            if result is not None:
                return result
        return httpx.Response(200, json={"version": 0.6, "elements": self.elements})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


class FakePipeline:
    def __init__(self, redis: "FakeRedis"):
        self._redis = redis
        self._ops: list[tuple[str, tuple]] = []

    def incr(self, key: str):
        self._ops.append(("incr", (key,)))
        return self

    def expire(self, key: str, seconds: int):
        self._ops.append(("expire", (key, seconds)))
        return self

    async def execute(self):
        self._redis._check()
        out = []
        for op, args in self._ops:
            out.append(await getattr(self._redis, op)(*args))
        return out


class FakeRedis:
    """Just enough of redis.asyncio.Redis for the cache and the rate limiter."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.fail: Optional[Exception] = None
        self.calls = 0

    def _check(self) -> None:
        self.calls += 1
        if self.fail is not None:
            raise self.fail

    async def get(self, key: str):
        self._check()
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: Optional[int] = None):
        self._check()
        self.data[key] = value
        if ex is not None:
            self.ttl[key] = ex
        return True

    async def incr(self, key: str):
        self.data[key] = str(int(self.data.get(key, "0")) + 1)
        return int(self.data[key])

    async def expire(self, key: str, seconds: int):
        self.ttl[key] = seconds
        return True

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    async def ping(self):
        self._check()
        return True

    async def aclose(self):
        return None


@pytest.fixture
def ola() -> FakeOla:
    return FakeOla()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def overpass() -> FakeOverpass:
    return FakeOverpass()


def client_for(
    ola: FakeOla,
    settings: Optional[Settings] = None,
    redis: Any = None,
    google: Optional[httpx.MockTransport] = None,
    overpass: Optional[FakeOverpass] = None,
) -> TestClient:
    app = create_app(
        settings or make_settings(),
        ola_transport=ola.transport(),
        google_transport=google,
        overpass_transport=overpass.transport() if overpass is not None else None,
        redis_client=redis,
    )
    return TestClient(app, raise_server_exceptions=False)


def body(response: httpx.Response) -> Any:
    return json.loads(response.content)
