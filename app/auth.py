"""Who is asking: a Firebase ID token, checked the way Firebase documents for
any JWT library (https://firebase.google.com/docs/auth/admin/verify-id-tokens).

No service-account key is needed: the token is checked against Google's public
signing keys, fetched over HTTPS and kept for as long as Google says they are
valid.

Checked: signed with RS256 by one of those keys; `aud` is our project;
`iss` is https://securetoken.google.com/<project>; not expired; issued and
signed-in in the past (30 s of clock difference allowed); `sub` (the user id)
present.

A checked token is remembered (by its hash) until it expires, so the same
token on the next request costs nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

import httpx
import jwt
from cryptography.x509 import load_pem_x509_certificate

logger = logging.getLogger("app.auth")

GOOGLE_CERTS_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"


class InvalidToken(Exception):
    """The token is not a valid sign-in for this project."""


class AuthUnavailable(Exception):
    """Google's keys could not be fetched and none are on hand: tokens cannot be checked right now."""


class FirebaseTokenVerifier:
    def __init__(
        self,
        project_id: str,
        http: httpx.AsyncClient,
        certs_url: str = GOOGLE_CERTS_URL,
        leeway_seconds: int = 30,
        clock: Callable[[], float] = time.time,
        remember: int = 20_000,
    ):
        self._project = project_id
        self._issuer = f"https://securetoken.google.com/{project_id}"
        self._http = http
        self._certs_url = certs_url
        self._leeway = leeway_seconds
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._keys_valid_until = 0.0
        self._keys_fetched_at = 0.0
        self._lock = asyncio.Lock()
        self._verified: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
        self._remember = remember

    async def verify(self, token: str) -> str:
        """The user id (uid) of a valid token; InvalidToken otherwise."""
        if not token or len(token) > 4096:
            raise InvalidToken("no token")
        fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = self._clock()
        remembered = self._verified.get(fingerprint)
        if remembered is not None:
            uid, expires = remembered
            if now < expires:
                self._verified.move_to_end(fingerprint)
                return uid
            del self._verified[fingerprint]

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise InvalidToken("not a token") from None
        if header.get("alg") != "RS256":
            raise InvalidToken("wrong algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidToken("no key id")

        key = await self._key(kid)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self._project,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            )
        except jwt.PyJWTError as error:
            raise InvalidToken(type(error).__name__) from None

        uid = claims.get("sub")
        if not isinstance(uid, str) or not uid or len(uid) > 128:
            raise InvalidToken("no user")
        auth_time = claims.get("auth_time")
        if not isinstance(auth_time, (int, float)) or auth_time > now + self._leeway:
            raise InvalidToken("bad auth_time")

        self._verified[fingerprint] = (uid, float(claims["exp"]))
        while len(self._verified) > self._remember:
            self._verified.popitem(last=False)
        return uid

    async def _key(self, kid: str) -> Any:
        keys = await self._current_keys()
        if kid in keys:
            return keys[kid]
        # Google rotates keys: an unknown id may be a new key. Fetch again, but
        # not more than once a minute however many bad tokens arrive.
        if self._clock() - self._keys_fetched_at > 60:
            keys = await self._current_keys(force=True)
            if kid in keys:
                return keys[kid]
        raise InvalidToken("unknown key")

    async def _current_keys(self, force: bool = False) -> dict[str, Any]:
        if not force and self._keys and self._clock() < self._keys_valid_until:
            return self._keys
        async with self._lock:
            if not force and self._keys and self._clock() < self._keys_valid_until:
                return self._keys  # another request fetched them while this one waited
            try:
                response = await self._http.get(self._certs_url, timeout=5.0)
                if response.status_code != 200:
                    raise ValueError(f"status {response.status_code}")
                certs = response.json()
                keys = {kid: load_pem_x509_certificate(pem.encode("ascii")).public_key() for kid, pem in certs.items()}
                if not keys:
                    raise ValueError("no keys")
            except Exception as error:  # noqa: BLE001
                self._keys_fetched_at = self._clock()
                if self._keys:
                    # Keep using the keys on hand rather than lock everyone out.
                    logger.warning("could not refresh Google's signing keys (%s)", type(error).__name__)
                    self._keys_valid_until = self._clock() + 300
                    return self._keys
                logger.error("could not fetch Google's signing keys (%s)", type(error).__name__)
                raise AuthUnavailable() from None
            self._keys = keys
            self._keys_fetched_at = self._clock()
            self._keys_valid_until = self._clock() + _max_age(response.headers.get("cache-control", ""))
            return keys


def _max_age(cache_control: str) -> float:
    """Seconds from Cache-Control max-age, kept between 5 minutes and 1 day."""
    match = re.search(r"max-age=(\d+)", cache_control)
    seconds = int(match.group(1)) if match else 3600
    return float(min(max(seconds, 300), 86400))
