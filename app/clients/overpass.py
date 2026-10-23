"""OpenStreetMap's Overpass API: where the police stations, hospitals and the like near a spot are.

It is a free public service, and the public servers are often overloaded (measured: of eight mirrors, one answered;
the best known ones rate-limited us or hung). So the manners and the resilience both matter:

  * the questions are about a ~1 km square (the same answer serves everyone in it, and is kept), so there are few;
  * the servers are tried in order, but a server that has not answered after `stagger` seconds is joined by the
    next one: the first good answer wins and the others are dropped. A healthy first server is never joined;
  * the whole lookup stops after `timeout` seconds, because the app gives up at 10;
  * after a run of failures it is left alone for a while (the same breaker as Ola).

Nothing about who asked, and no exact position, is ever sent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

import httpx

from ..config import Settings
from ..context import request_id_var
from ..domain.nearby import build_group_query, parse_elements
from .ola import CircuitBreaker

logger = logging.getLogger("app.overpass")


class PlacesUnavailable(Exception):
    """OpenStreetMap could not answer. `reason`: timeout, error, status, bad_response or circuit_open.
    Safe to show: it never carries a URL."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Attempt(Exception):
    """One server's try did not work (never leaves this module)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class OverpassClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: Settings,
        breaker: Optional[CircuitBreaker] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._http = http
        self._urls = tuple(settings.overpass_urls)
        self._deadline_seconds = settings.overpass_timeout
        self._stagger = settings.overpass_stagger
        self._connect_timeout = settings.ola_connect_timeout
        self.breaker = breaker or CircuitBreaker(settings.breaker_failures, settings.breaker_cooldown_seconds, clock)

    async def places(self, group: str, cell_lat: float, cell_lng: float, radius_m: int) -> dict[str, list[dict[str, Any]]]:
        """Every kind of place in this group around the centre of a cell, by kind, as the app reads them (no
        distances yet). One question for the whole group."""
        if not self._urls:
            raise PlacesUnavailable("error")
        if not self.breaker.allow():
            raise PlacesUnavailable("circuit_open")
        try:
            return await self._ask(group, build_group_query(group, cell_lat, cell_lng, radius_m))
        finally:
            self.breaker.release()

    async def _ask(self, group: str, query: str) -> dict[str, list[dict[str, Any]]]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._deadline_seconds
        running: dict[asyncio.Task, str] = {}
        reasons: set[str] = set()
        started = 0
        next_start = loop.time()
        try:
            while True:
                now = loop.time()
                remaining = deadline - now
                if remaining <= 0:
                    reasons.add("timeout")
                    break
                # The next server joins when nothing is running (the others failed), or when the running ones have
                # had `stagger` seconds and not answered.
                if started < len(self._urls) and (not running or now >= next_start):
                    url = self._urls[started]
                    started += 1
                    running[asyncio.ensure_future(self._attempt(url, query, remaining))] = url
                    next_start = loop.time() + self._stagger
                    continue
                if not running:
                    break
                wait = remaining if started >= len(self._urls) else min(remaining, max(next_start - now, 0.0))
                done, _ = await asyncio.wait(running, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
                answer: Optional[list[Any]] = None
                for task in done:  # every finished try is collected, even if another has already won
                    running.pop(task)
                    try:
                        elements = task.result()
                    except _Attempt as failed:
                        reasons.add(failed.reason)
                        continue
                    if answer is None:
                        answer = elements
                if answer is not None:
                    self.breaker.success()
                    return parse_elements(answer, group)
        finally:
            # The slower servers are dropped, and waited for, so nothing is left running behind the answer.
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)

        self.breaker.failure()
        # What the person is told: slowness if any server was slow, else what the last ones said.
        for reason in ("timeout", "status", "bad_response", "error"):
            if reason in reasons:
                raise PlacesUnavailable(reason)
        raise PlacesUnavailable("error")

    async def _attempt(self, url: str, query: str, budget: float) -> list[Any]:
        limits = httpx.Timeout(budget, connect=min(self._connect_timeout, budget))
        headers = {"X-Request-Id": request_id_var.get() or ""}
        try:
            response = await self._http.post(url, data={"data": query}, headers=headers, timeout=limits)
        except httpx.TimeoutException:
            raise _Attempt("timeout") from None
        except httpx.TransportError:
            raise _Attempt("error") from None

        if response.status_code != 200:
            # 429 (too busy), 406 (turned away), 5xx: another server may do better.
            logger.warning("Overpass call failed", extra={"upstream": "overpass", "status": response.status_code})
            raise _Attempt("status")

        data = _json_object(response)
        if data is None or not isinstance(data.get("elements"), list):
            raise _Attempt("bad_response")
        remark = data.get("remark")
        if isinstance(remark, str) and "runtime error" in remark.lower():
            # Overpass says 200 even when it ran out of time or memory, with the answer cut short.
            logger.warning("Overpass gave a partial answer", extra={"upstream": "overpass"})
            raise _Attempt("status")
        return data["elements"]


def _json_object(response: httpx.Response) -> Optional[dict[str, Any]]:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
