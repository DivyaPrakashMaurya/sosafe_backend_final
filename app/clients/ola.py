"""Ola Maps, asked over one shared pool of connections, without ever blocking.

What this guards against:
  * a slow Ola: every call has a time limit; a call that failed fast (a reset
    connection, a 502) is tried once more, a call that used up its time is not
  * an Ola that is down: after several failures in a row it is not asked at all
    for a short while (the circuit breaker), so requests fail at once instead
    of each waiting out a timeout, and Ola is not hammered while it recovers
  * a flood: at most `ola_max_connections` calls to Ola at once per process
  * a leak: the key is a URL parameter, so no URL, and no exception text that
    could carry one, ever leaves this module (logs, errors, responses)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from typing import Any, Callable, Optional

import httpx

from ..config import Settings
from ..context import request_id_var

logger = logging.getLogger("app.ola")


class OlaUnavailable(Exception):
    """Ola could not answer. `reason`: timeout, error, status, bad_response,
    rejected_key or circuit_open. Safe to show: it never carries a URL."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class OlaRejected(Exception):
    """Ola refused the question itself (400/404): e.g. a place id it does not know."""


class CircuitBreaker:
    """Closed: calls go through. After `failures` in a row: open for `cooldown`
    seconds (calls refused at once). Then one trial call is let through: success
    closes it, failure opens it again."""

    def __init__(self, failures: int, cooldown: float, clock: Callable[[], float] = time.monotonic):
        self._limit = failures
        self._cooldown = cooldown
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0
        self._trial_running = False

    @property
    def state(self) -> str:
        if self._failures < self._limit:
            return "closed"
        return "open" if self._clock() < self._open_until else "half_open"

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "open" or self._trial_running:
            return False
        self._trial_running = True  # half open: this one call is the trial
        return True

    def success(self) -> None:
        self._failures = 0
        self._trial_running = False

    def release(self) -> None:
        """The trial call ended without a verdict (it was cancelled): let another try."""
        self._trial_running = False

    def failure(self) -> None:
        self._trial_running = False
        self._failures += 1
        if self._failures >= self._limit:
            self._open_until = self._clock() + self._cooldown


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OlaClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: Settings,
        breaker: Optional[CircuitBreaker] = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._http = http
        self._key = settings.ola_api_key
        self._connect_timeout = settings.ola_connect_timeout
        self._lookup_timeout = settings.ola_lookup_timeout
        self._route_timeout = settings.ola_route_timeout
        self._retries = settings.ola_retries
        self._slots = asyncio.Semaphore(settings.ola_max_connections)
        self.breaker = breaker or CircuitBreaker(settings.breaker_failures, settings.breaker_cooldown_seconds, clock)
        self._sleep = sleep
        self._clock = clock

    # ─── the four questions ───

    async def autocomplete(self, query: str) -> list[dict[str, str]]:
        """Suggestions for typed text; an empty list when Ola has none."""
        data = await self._call("GET", "/places/v1/autocomplete", {"input": query}, self._lookup_timeout, "autocomplete")
        predictions = data.get("predictions")
        if not isinstance(predictions, list):
            logger.warning("autocomplete answer without predictions", extra={"upstream": "autocomplete", "failure": "shape"})
            return []
        results = []
        for item in predictions:
            if not isinstance(item, dict):
                continue
            name, place_id = item.get("description"), item.get("place_id")
            if isinstance(name, str) and isinstance(place_id, str) and name and place_id:
                results.append({"place_name": name, "place_id": place_id})
        return results

    async def place_location(self, place_id: str) -> Optional[dict[str, float]]:
        """{'lat', 'lng'} of a place, or None when Ola does not know where it is."""
        try:
            data = await self._call("GET", "/places/v1/details", {"place_id": place_id}, self._lookup_timeout, "details")
        except OlaRejected:
            return None
        location = (((data.get("result") or {}).get("geometry") or {}).get("location")) or {}
        lat, lng = location.get("lat"), location.get("lng")
        if not _is_number(lat) or not _is_number(lng):
            return None
        if lat == 0 and lng == 0:  # "nowhere": never a real answer
            return None
        return {"lat": float(lat), "lng": float(lng)}

    async def reverse_geocode(self, lat: float, lng: float) -> Optional[str]:
        """The address of a spot, or None when Ola has none."""
        try:
            data = await self._call(
                "GET", "/places/v1/reverse-geocode", {"latlng": f"{lat},{lng}"}, self._lookup_timeout, "reverse"
            )
        except OlaRejected:
            return None
        results = data.get("results")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return None
        address = results[0].get("formatted_address")
        return address.strip() if isinstance(address, str) and address.strip() else None

    async def directions(
        self, start: tuple[float, float], end: tuple[float, float], mode: str
    ) -> Optional[dict[str, Any]]:
        """Ola's part of a route: {'ola_raw_mins', 'distance_km', 'overview_polyline'},
        or None when there is no route between the two points."""
        params = {"origin": f"{start[0]},{start[1]}", "destination": f"{end[0]},{end[1]}", "mode": mode}
        try:
            data = await self._call("POST", "/routing/v1/directions", params, self._route_timeout, "directions")
        except OlaRejected:
            return None
        routes = data.get("routes")
        if not isinstance(routes, list) or not routes:
            return None
        route = routes[0]
        try:
            leg = route["legs"][0]
            duration_seconds = leg.get("duration", 0)
            distance_meters = leg.get("distance", 0)
            if not _is_number(duration_seconds) or not _is_number(distance_meters):
                raise TypeError
        except (KeyError, IndexError, TypeError, AttributeError):
            logger.warning("directions answer in an unexpected shape", extra={"upstream": "directions", "failure": "shape"})
            raise OlaUnavailable("bad_response") from None
        polyline = route.get("overview_polyline")
        if not isinstance(polyline, str) or not polyline:
            return None  # no line to draw: not a usable route
        return {
            "ola_raw_mins": round(duration_seconds / 60),
            "distance_km": round(distance_meters / 1000, 2),
            "overview_polyline": polyline,
        }

    # ─── the plumbing ───

    async def _call(self, method: str, path: str, params: dict[str, str], timeout: float, what: str) -> dict[str, Any]:
        if not self.breaker.allow():
            raise OlaUnavailable("circuit_open")
        try:
            return await self._attempts(method, path, params, timeout, what)
        finally:
            self.breaker.release()

    async def _attempts(self, method: str, path: str, params: dict[str, str], timeout: float, what: str) -> dict[str, Any]:

        query = {**params, "api_key": self._key}
        headers = {"X-Request-Id": request_id_var.get() or uuid.uuid4().hex}
        limits = httpx.Timeout(timeout, connect=min(self._connect_timeout, timeout))
        failure = "error"
        for attempt in range(1, self._retries + 2):
            started = self._clock()
            retry_worth_it = False
            status: Optional[int] = None
            try:
                async with self._slots:
                    response = await self._http.request(method, path, params=query, headers=headers, timeout=limits)
            except httpx.TimeoutException:
                failure = "timeout"  # the whole time is used up: no second try
            except httpx.TransportError:
                failure = "error"
                retry_worth_it = self._clock() - started < timeout / 2  # failed fast: worth one more go
            else:
                status = response.status_code
                if status < 400:
                    data = _json_object(response)
                    if data is not None:
                        self.breaker.success()
                        return data
                    failure = "bad_response"
                elif status in (400, 404):
                    self.breaker.success()  # Ola is fine; the question was not
                    raise OlaRejected()
                elif status in (401, 403):
                    failure = "rejected_key"  # wrong, revoked or out of quota: a retry fixes nothing
                else:
                    failure = "status"
                    retry_worth_it = status in _RETRYABLE_STATUS

            logger.log(
                logging.ERROR if failure == "rejected_key" else logging.WARNING,
                "Ola call failed",
                extra={"upstream": what, "status": status, "attempt": attempt, "failure": failure},
            )
            if retry_worth_it and attempt <= self._retries:
                await self._sleep(random.uniform(0.1, 0.3))
                continue
            break

        self.breaker.failure()
        raise OlaUnavailable(failure)


def _json_object(response: httpx.Response) -> Optional[dict[str, Any]]:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value  # not NaN
