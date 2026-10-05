"""The app measured on its own, with no network in the way: requests go straight
into the app (ASGI) and Ola is a stand-in that answers after a delay.

    python -m loadtest.in_process --concurrency 500 --seconds 15 --ola-ms 200

What it shows:
  * whether the app serves other requests while it waits for Ola (it must:
    with 500 phones and Ola taking 200 ms, close to 2500 requests a second)
  * the CPU one request costs, which is what decides how many requests a
    second one worker process can take: about 1 / (CPU per request)

It also runs the old backend's pattern (a blocking call inside an `async def`
endpoint, as main.py did with `requests`) for comparison, with --old.
Real-network numbers need a run against the deployed server (run_load.py).
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time
from dataclasses import replace

import httpx
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app


def fake_ola(delay: float) -> httpx.MockTransport:
    async def handle(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(delay)
        path = request.url.path
        if path.endswith("/autocomplete"):
            q = request.url.params["input"]
            return httpx.Response(200, json={"predictions": [{"description": f"{q} Road", "place_id": f"id-{q}"}]})
        if path.endswith("/details"):
            return httpx.Response(200, json={"result": {"geometry": {"location": {"lat": 26.85, "lng": 80.94}}}})
        if path.endswith("/reverse-geocode"):
            return httpx.Response(200, json={"results": [{"formatted_address": "1 Mall Road"}]})
        return httpx.Response(200, json={"routes": [{"legs": [{"duration": 1200, "distance": 5400}], "overview_polyline": "a" * 800}]})

    return httpx.MockTransport(handle)


def old_style_app(delay: float) -> FastAPI:
    """The old main.py's shape: `async def` with a blocking call inside."""
    app = FastAPI()

    @app.get("/api/v1/location/search")
    async def search(query: str):
        time.sleep(delay)  # what requests.get(...) did: holds the whole process
        return {"status": "success", "results": [{"place_name": query, "place_id": "x"}]}

    return app


async def phone(client: httpx.AsyncClient, stop_at: float, latencies: list[float], failures: list[int], n: int, only_search: bool) -> None:
    i = 0
    while time.perf_counter() < stop_at:
        i += 1
        pick = 0.0 if only_search else random.random()
        started = time.perf_counter()
        if pick < 0.6:
            r = await client.get("/api/v1/location/search", params={"query": f"street {n}-{i}"})
        elif pick < 0.75:
            r = await client.get("/api/v1/location/coordinates", params={"place_id": f"id-{n}-{i}"})
        elif pick < 0.9:
            r = await client.get("/api/v1/location/reverse", params={"lat": 26.8 + random.random() / 10, "lng": 80.9})
        else:
            r = await client.post("/api/v1/route/calculate", json={"start_lat": 26.8 + random.random() / 10, "start_lng": 80.9, "end_lat": 26.9, "end_lng": 81.0, "mode": "driving"})
        if r.status_code != 200:
            failures.append(r.status_code)
        latencies.append((time.perf_counter() - started) * 1000)


async def run(app: FastAPI, concurrency: int, seconds: float, only_search: bool) -> None:
    latencies: list[float] = []
    failures: list[int] = []
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://app", timeout=60) as client:
            cpu0, t0 = time.process_time(), time.perf_counter()
            stop_at = t0 + seconds
            await asyncio.gather(*(phone(client, stop_at, latencies, failures, n, only_search) for n in range(concurrency)))
            cpu, elapsed = time.process_time() - cpu0, time.perf_counter() - t0
    q = statistics.quantiles(latencies, n=100)
    print(f"  phones at once {concurrency}, {len(latencies)} requests in {elapsed:.1f} s")
    print(f"  requests/second {len(latencies) / elapsed:.0f}   latency ms p50 {q[49]:.0f}  p95 {q[94]:.0f}  p99 {q[98]:.0f}")
    print(f"  CPU per request {cpu / len(latencies) * 1000:.2f} ms (this process: the app, the stand-in Ola and the test client together)")
    print(f"  failures {len(failures)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=500)
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--ola-ms", type=float, default=200)
    parser.add_argument("--old", action="store_true", help="also run the old blocking pattern")
    args = parser.parse_args()
    delay = args.ola_ms / 1000

    settings = replace(
        Settings(), environment="loadtest", log_level="WARNING", ola_api_key="loadtest", auth_mode="off",
        cache_enabled=False, rate_limit_enabled=False, trusted_proxy_hops=0, ola_max_connections=10_000,
    )
    print(f"NEW backend (cache off: every request goes to Ola, {args.ola_ms:.0f} ms each)")
    asyncio.run(run(create_app(settings, ola_transport=fake_ola(delay)), args.concurrency, args.seconds, only_search=False))

    if args.old:
        print(f"OLD pattern (blocking call in async def, {args.ola_ms:.0f} ms), searches only")
        asyncio.run(run(old_style_app(delay), min(args.concurrency, 50), min(args.seconds, 10), only_search=True))


if __name__ == "__main__":
    main()
