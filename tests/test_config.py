from __future__ import annotations

import pytest

from app.config import ConfigError, Settings


def test_defaults():
    s = Settings.from_env({"OLA_API_KEY": "k"})
    assert s.auth_mode == "optional"
    assert s.cache_ttl_seconds["route"] == 120
    assert s.is_production
    assert s.check() == [
        "FIREBASE_PROJECT_ID is not set: sign-in is not checked (everyone is anonymous)",
        "REDIS_URL is not set: cache and rate limits are per process only",
    ]


def test_everything_can_be_set():
    s = Settings.from_env(
        {
            "OLA_API_KEY": "k",
            "AUTH_MODE": "required",
            "FIREBASE_PROJECT_ID": "p",
            "REDIS_URL": "redis://x",
            "CACHE_TTL_ROUTE_SECONDS": "0",
            "RATE_LIMIT_USER_PER_MINUTE": "50",
            "CORS_ORIGINS": "https://a.example, https://b.example",
            "ENVIRONMENT": "Staging",
        }
    )
    assert (s.auth_mode, s.cache_ttl_seconds["route"], s.rate_limit_user_per_minute) == ("required", 0, 50)
    assert s.cors_origins == ("https://a.example", "https://b.example")
    assert not s.is_production
    assert s.check() == []


@pytest.mark.parametrize(
    "env",
    [
        {"AUTH_MODE": "maybe"},
        {"CACHE_TTL_SEARCH_SECONDS": "a day"},
        {"CACHE_TTL_ROUTE_SECONDS": "100000"},
        {"RATE_LIMIT_ENABLED": "perhaps"},
        {"LOG_LEVEL": "LOUD"},
        {"OLA_RETRIES": "-1"},
    ],
)
def test_a_wrong_setting_stops_the_start(env):
    with pytest.raises(ConfigError):
        Settings.from_env({"OLA_API_KEY": "k", **env})


def test_missing_ola_key_or_project_for_required_auth_are_fatal():
    with pytest.raises(ConfigError):
        Settings.from_env({}).check()
    with pytest.raises(ConfigError):
        Settings.from_env({"OLA_API_KEY": "k", "AUTH_MODE": "required"}).check()


def test_env_file_fills_only_what_is_not_set():
    from pathlib import Path

    from app.config import load_env_file

    f = Path(__file__).parent / "_test.env"
    f.write_text('# comment\nOLA_API_KEY="from-file"\nexport AUTH_MODE=off  # note\nLOG_LEVEL=DEBUG\nnot a line\n', encoding="utf-8")
    try:
        env = {"LOG_LEVEL": "INFO"}
        load_env_file(str(f), env)
        assert env == {"LOG_LEVEL": "INFO", "OLA_API_KEY": "from-file", "AUTH_MODE": "off"}
        load_env_file(str(f.parent / "no-such-file"), env)  # no file: nothing happens
    finally:
        f.unlink()
