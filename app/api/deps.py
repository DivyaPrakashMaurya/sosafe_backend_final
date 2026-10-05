"""Who is calling, and whether they may call again this minute."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import Request

from ..auth import AuthUnavailable, InvalidToken
from ..errors import ApiError
from ..rate_limit import bucket_id

logger = logging.getLogger("app.auth")


@dataclass(frozen=True)
class Caller:
    uid: Optional[str]  # None: not signed in (allowed while AUTH_MODE is off or optional)
    ip: str


def client_ip(request: Request, trusted_hops: int) -> str:
    """The phone's address. Behind `trusted_hops` proxies (the load balancer), each
    of which appends the address it saw to X-Forwarded-For, the entry that many
    from the right is the one our own proxy wrote: anything to its left was
    sent by the caller and could be made up."""
    peer = request.client.host if request.client else "unknown"
    if trusted_hops <= 0:
        return peer
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
    if len(forwarded) >= trusted_hops:
        return forwarded[-trusted_hops]
    return peer


def _bearer(request: Request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


_SIGN_IN_AGAIN = {"WWW-Authenticate": "Bearer"}


async def get_caller(request: Request) -> Caller:
    state = request.app.state
    settings = state.settings
    ip = client_ip(request, settings.trusted_proxy_hops)
    mode = settings.auth_mode
    verifier = state.verifier

    if mode == "off" or verifier is None:
        return Caller(None, ip)

    token = _bearer(request)
    if token is None:
        if mode == "required":
            raise ApiError(401, "unauthenticated", "Sign in again.", _SIGN_IN_AGAIN)
        return Caller(None, ip)

    try:
        uid = await verifier.verify(token)
    except InvalidToken as error:
        logger.warning("sign-in token refused (%s)", error, extra={"event": "token_refused", "auth": mode})
        if mode == "required":
            raise ApiError(401, "unauthenticated", "Sign in again.", _SIGN_IN_AGAIN) from None
        return Caller(None, ip)
    except AuthUnavailable:
        if mode == "required":
            raise ApiError(503, "auth_unavailable", "Sign-in cannot be checked right now. Try again.", {"Retry-After": "5"}) from None
        return Caller(None, ip)
    return Caller(uid, ip)


async def enforce_rate_limit(request: Request, caller: Caller, route: bool = False) -> None:
    """429 with Retry-After when over the limit. Signed-in people are counted by
    who they are; others by network address (with a generous limit)."""
    state = request.app.state
    settings = state.settings
    limiter = state.limiter
    if not limiter.enabled:
        return

    if caller.uid is not None:
        identity, limit = f"user:{caller.uid}", settings.rate_limit_user_per_minute
    else:
        identity, limit = f"ip:{caller.ip}", settings.rate_limit_anonymous_per_minute
    checks = [(bucket_id("all", identity), limit)]
    if route:
        checks.append((bucket_id("route", identity), settings.rate_limit_route_per_minute))

    for bucket, bucket_limit in checks:
        decision = await limiter.hit(bucket, bucket_limit)
        if not decision.allowed:
            raise ApiError(
                429,
                "rate_limited",
                "Too many requests. Wait a moment and try again.",
                {"Retry-After": str(decision.retry_after)},
            )
