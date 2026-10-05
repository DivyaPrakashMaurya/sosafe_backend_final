"""Every setting, read once from the environment (see .env.example).

A setting that is present but wrong (a word where a number belongs, an unknown
auth mode) stops the app at start-up with a clear message: a typo must not
quietly run production on a default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, Mapping, Optional

AuthMode = Literal["off", "optional", "required"]
_AUTH_MODES = ("off", "optional", "required")


class ConfigError(ValueError):
    """A setting is missing or not usable."""


def _text(env: Mapping[str, str], name: str, default: str = "") -> str:
    return (env.get(name) or default).strip()


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false, not {raw!r}")


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, not {raw!r}") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, not {value}")
    return value


def _float(env: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, not {raw!r}") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, not {value}")
    return value


def load_env_file(path: str = ".env", env: Optional[dict] = None) -> None:
    """Local runs: KEY=VALUE lines from `path` into the environment, for keys not
    already set (a real environment always wins). No file, nothing happens."""
    env = os.environ if env is None else env
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        if key and key not in env:
            env[key] = value


@dataclass(frozen=True)
class Settings:
    # ── general ──
    environment: str = "production"
    log_level: str = "INFO"

    # ── Ola Maps ──
    ola_api_key: str = ""
    ola_base_url: str = "https://api.olamaps.io"
    ola_connect_timeout: float = 3.0
    # Seconds one attempt may take. With the one retry these stay inside the
    # app's own waits (10 s for lookups, 20 s for a route).
    ola_lookup_timeout: float = 4.0
    ola_route_timeout: float = 8.0
    ola_retries: int = 1
    ola_max_connections: int = 100  # per process
    breaker_failures: int = 5  # in a row, before Ola is given a rest
    breaker_cooldown_seconds: float = 15.0

    # ── sign-in (Firebase ID tokens) ──
    auth_mode: AuthMode = "optional"
    firebase_project_id: str = ""

    # ── cache ──
    redis_url: str = ""
    cache_enabled: bool = True
    cache_ttl_seconds: Mapping[str, int] = field(
        default_factory=lambda: {
            "search": 24 * 3600,
            "coordinates": 30 * 24 * 3600,
            "reverse": 30 * 24 * 3600,
            "route": 120,
        }
    )
    cache_memory_max_entries: int = 5000

    # ── rate limits (requests per minute) ──
    rate_limit_enabled: bool = True
    rate_limit_user_per_minute: int = 300  # a signed-in person, all lookups together
    rate_limit_route_per_minute: int = 30  # route calculations, per person (or per address when anonymous)
    # Per network address when not signed in. Generous: on Indian mobile networks
    # thousands of phones can share one public address.
    rate_limit_anonymous_per_minute: int = 1200
    # How many proxies sit in front of the app and append to X-Forwarded-For
    # (Render: 1). 0 = use the connecting address.
    trusted_proxy_hops: int = 1

    # ── HTTP ──
    max_body_bytes: int = 16 * 1024
    cors_origins: tuple[str, ...] = ()

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env

        auth_mode = _text(env, "AUTH_MODE", "optional").lower()
        if auth_mode not in _AUTH_MODES:
            raise ConfigError(f"AUTH_MODE must be one of {', '.join(_AUTH_MODES)}, not {auth_mode!r}")

        log_level = _text(env, "LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ConfigError(f"LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR, not {log_level!r}")

        day = 24 * 3600
        ttl = {
            "search": _int(env, "CACHE_TTL_SEARCH_SECONDS", day, 0, 30 * day),
            "coordinates": _int(env, "CACHE_TTL_COORDINATES_SECONDS", 30 * day, 0, 90 * day),
            "reverse": _int(env, "CACHE_TTL_REVERSE_SECONDS", 30 * day, 0, 90 * day),
            "route": _int(env, "CACHE_TTL_ROUTE_SECONDS", 120, 0, 900),
        }

        origins = tuple(o.strip() for o in _text(env, "CORS_ORIGINS").split(",") if o.strip())

        return cls(
            environment=_text(env, "ENVIRONMENT", "production").lower(),
            log_level=log_level,
            ola_api_key=_text(env, "OLA_API_KEY"),
            ola_base_url=_text(env, "OLA_BASE_URL", "https://api.olamaps.io").rstrip("/"),
            ola_connect_timeout=_float(env, "OLA_CONNECT_TIMEOUT_SECONDS", 3.0, 0.5, 30.0),
            ola_lookup_timeout=_float(env, "OLA_LOOKUP_TIMEOUT_SECONDS", 4.0, 0.5, 30.0),
            ola_route_timeout=_float(env, "OLA_ROUTE_TIMEOUT_SECONDS", 8.0, 1.0, 60.0),
            ola_retries=_int(env, "OLA_RETRIES", 1, 0, 3),
            ola_max_connections=_int(env, "OLA_MAX_CONNECTIONS", 100, 1, 1000),
            breaker_failures=_int(env, "OLA_BREAKER_FAILURES", 5, 1, 100),
            breaker_cooldown_seconds=_float(env, "OLA_BREAKER_COOLDOWN_SECONDS", 15.0, 1.0, 600.0),
            auth_mode=auth_mode,  # type: ignore[arg-type]
            firebase_project_id=_text(env, "FIREBASE_PROJECT_ID"),
            redis_url=_text(env, "REDIS_URL"),
            cache_enabled=not _bool(env, "CACHE_DISABLED", False),
            cache_ttl_seconds=ttl,
            cache_memory_max_entries=_int(env, "CACHE_MEMORY_MAX_ENTRIES", 5000, 1, 1_000_000),
            rate_limit_enabled=_bool(env, "RATE_LIMIT_ENABLED", True),
            rate_limit_user_per_minute=_int(env, "RATE_LIMIT_USER_PER_MINUTE", 300, 1, 100_000),
            rate_limit_route_per_minute=_int(env, "RATE_LIMIT_ROUTE_PER_MINUTE", 30, 1, 10_000),
            rate_limit_anonymous_per_minute=_int(env, "RATE_LIMIT_ANONYMOUS_PER_MINUTE", 1200, 1, 1_000_000),
            trusted_proxy_hops=_int(env, "TRUSTED_PROXY_HOPS", 1, 0, 5),
            max_body_bytes=_int(env, "MAX_BODY_BYTES", 16 * 1024, 1024, 1024 * 1024),
            cors_origins=origins,
        )

    def check(self) -> list[str]:
        """Problems that stop the app from serving correctly. Raised at start-up.
        Returns the warnings that do not."""
        if not self.ola_api_key:
            raise ConfigError("OLA_API_KEY is not set")
        if self.auth_mode == "required" and not self.firebase_project_id:
            raise ConfigError("AUTH_MODE=required needs FIREBASE_PROJECT_ID")
        warnings = []
        if self.auth_mode == "optional" and not self.firebase_project_id:
            warnings.append("FIREBASE_PROJECT_ID is not set: sign-in is not checked (everyone is anonymous)")
        if self.auth_mode == "off":
            warnings.append("AUTH_MODE=off: anyone with the address can use this API")
        if not self.redis_url:
            warnings.append("REDIS_URL is not set: cache and rate limits are per process only")
        return warnings
