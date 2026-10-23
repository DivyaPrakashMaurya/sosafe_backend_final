"""The application: settings, shared clients, middleware and routes, put together once.

Run (production):  uvicorn app.main:create_app --factory --host 0.0.0.0 --port $PORT --workers 2
Run (local):       uvicorn app.main:create_app --factory --reload

Everything a request needs is created once per process in `lifespan` and shared:
one pool of connections to Ola, one to Google (sign-in keys), one Redis client.
Nothing is kept per instance that another instance would need, so any number
of copies can run behind the load balancer.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from .api import health
from .api.v1 import location, places, route
from .auth import FirebaseTokenVerifier
from .cache import make_cache
from .clients.ola import OlaClient
from .clients.overpass import OverpassClient
from .config import Settings, load_env_file
from .errors import register_error_handlers
from .log import setup_logging
from .middleware import RequestContextMiddleware
from .rate_limit import RateLimiter
from .services.nearby import NearbyService
from .services.places import PlacesService

logger = logging.getLogger("app")

VERSION = "2.0.0"


def create_app(
    settings: Optional[Settings] = None,
    *,
    ola_transport: Optional[httpx.AsyncBaseTransport] = None,
    google_transport: Optional[httpx.AsyncBaseTransport] = None,
    overpass_transport: Optional[httpx.AsyncBaseTransport] = None,
    redis_client: Any = None,
) -> FastAPI:
    """`ola_transport`, `google_transport`, `overpass_transport` and `redis_client` are for tests (pretend
    services); in production they are left out and the real ones are used."""
    if settings is None:
        load_env_file()  # .env for local runs; on a server the environment is set directly
        settings = Settings.from_env()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for warning in settings.check():  # raises (and the app does not start) on a fatal problem
            logger.warning(warning, extra={"event": "config_warning"})

        ola_http = httpx.AsyncClient(
            base_url=settings.ola_base_url,
            transport=ola_transport,
            limits=httpx.Limits(
                max_connections=settings.ola_max_connections,
                # All of them kept open: a connection closed under load is a new
                # TLS handshake with Ola for the next call (found in the load test).
                max_keepalive_connections=settings.ola_max_connections,
                keepalive_expiry=30.0,
            ),
            headers={"User-Agent": f"sosafe-backend/{VERSION}"},
        )
        google_http = httpx.AsyncClient(transport=google_transport, timeout=5.0)
        overpass_http = httpx.AsyncClient(
            transport=overpass_transport,
            headers={"User-Agent": f"sosafe-backend/{VERSION} (nearby safe places)"},
        )

        redis = redis_client
        owns_redis = False
        if redis is None and settings.redis_url and settings.cache_enabled:
            import redis.asyncio as redis_asyncio  # only needed when Redis is configured

            redis = redis_asyncio.Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
                health_check_interval=30,
                decode_responses=True,
                max_connections=100,
            )
            owns_redis = True

        cache = make_cache(settings, redis)
        app.state.settings = settings
        app.state.cache = cache
        app.state.limiter = RateLimiter(cache.redis, enabled=settings.rate_limit_enabled)
        app.state.ola = OlaClient(ola_http, settings)
        app.state.places = PlacesService(app.state.ola, cache)
        app.state.nearby = NearbyService(OverpassClient(overpass_http, settings), cache, fresh_seconds=settings.nearby_fresh_seconds)
        app.state.verifier = (
            FirebaseTokenVerifier(settings.firebase_project_id, google_http)
            if settings.auth_mode != "off" and settings.firebase_project_id
            else None
        )
        logger.info(
            "started",
            extra={"event": "startup", "auth": settings.auth_mode, "cache": "redis" if cache.redis else "memory"},
        )
        try:
            yield
        finally:
            await ola_http.aclose()
            await google_http.aclose()
            await overpass_http.aclose()
            if owns_redis:
                await redis.aclose()

    app = FastAPI(
        title="SoSafe API",
        version=VERSION,
        lifespan=lifespan,
        # The API description is for development; a production server does not publish it.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(location.router)
    app.include_router(route.router)
    app.include_router(places.router)

    # Added last = runs first: the request id is set before anything else.
    if settings.cors_origins:  # a phone app needs none; only for a web client, if one is ever made
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])
    app.add_middleware(GZipMiddleware, minimum_size=1024)  # route lines shrink a lot on mobile data
    app.add_middleware(RequestContextMiddleware, max_body_bytes=settings.max_body_bytes)
    return app
