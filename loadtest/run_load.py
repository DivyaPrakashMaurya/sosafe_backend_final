"""Many phones at once against a running server: requests per second and how
long they take.

    python -m loadtest.run_load --url http://127.0.0.1:8000 --concurrency 200 --seconds 20

The mix follows a trip: mostly searches, then a place, an address and a route.
Each search uses fresh text, so with the cache on it still measures real work.
Pass --token to send a sign-in token (AUTH_MODE=required).
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time
from collections import Counter

import httpx


async def phone(client: httpx.AsyncClient, stop_at: float, latencies: list[float], statuses: Counter, n: int) -> None:
    i = 0
    while time.perf_counter() < stop_at:
        i += 1
        pick = random.random()
        started = time.perf_counter()
        try:
            if pick < 0.6:
                r = await client.get("/api/v1/location/search", params={"query": f"street {n}-{i}"})
            elif pick < 0.75:
                r = await client.get("/api/v1/location/coordinates", params={"place_id": f"id-{n}-{i}"})
            elif pick < 0.9:
                r = await client.get("/api/v1/location/reverse", params={"lat": 26.8 + random.random() / 10, "lng": 80.9 + random.random() / 10})
            else:
                r = await client.post(
                    "/api/v1/route/calculate",
                    json={"start_lat": 26.8 + random.random() / 10, "start_lng": 80.9, "end_lat": 26.9, "end_lng": 81.0, "mode": "driving"},
                )
            statuses[r.status_code] += 1
        except httpx.HTTPError as error:
            statuses[type(error).__name__] += 1
        latencies.append((time.perf_counter() - started) * 1000)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--token", default="")
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    latencies: list[float] = []
    statuses: Counter = Counter()
    async with httpx.AsyncClient(base_url=args.url, headers=headers, limits=limits, timeout=30) as client:
        started = time.perf_counter()
        stop_at = started + args.seconds
        await asyncio.gather(*(phone(client, stop_at, latencies, statuses, n) for n in range(args.concurrency)))
        elapsed = time.perf_counter() - started

    latencies.sort()
    q = statistics.quantiles(latencies, n=100)
    print(f"phones at once: {args.concurrency}   time: {elapsed:.1f} s   requests: {len(latencies)}")
    print(f"requests/second: {len(latencies) / elapsed:.0f}")
    print(f"latency ms: p50 {q[49]:.0f}   p95 {q[94]:.0f}   p99 {q[98]:.0f}   max {latencies[-1]:.0f}")
    print(f"answers: {dict(statuses)}")


if __name__ == "__main__":
    asyncio.run(main())
