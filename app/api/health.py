"""Health checks for the load balancer and for people.

/healthz  the process is up (the load balancer's check: cheap, no outside calls)
/readyz   what it depends on: Redis, and whether Ola is currently being rested
/         kept from the old backend ("running")
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/")
async def root() -> dict:
    return {"status": "running"}


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict:
    state = request.app.state
    redis_status = "off"
    store = state.cache.redis
    if store is not None:
        try:
            await asyncio.wait_for(store.client.ping(), timeout=0.5)
            redis_status = "ok"
        except Exception:  # noqa: BLE001
            redis_status = "down"
    ola_state = state.ola.breaker.state
    # Redis being down is "degraded", never "not ready": the service works without
    # it, and taking every instance out of the load balancer would be far worse.
    degraded = redis_status == "down" or ola_state != "closed"
    return {
        "status": "degraded" if degraded else "ok",
        "redis": redis_status,
        "ola": ola_state,
        "auth": state.settings.auth_mode if state.verifier is not None else "off",
    }
