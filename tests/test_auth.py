"""Sign-in: Firebase ID tokens, signed with a real RSA key in the test and checked
the way production checks them (keys served by a pretend Google)."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.auth import AuthUnavailable, FirebaseTokenVerifier, InvalidToken
from tests.conftest import body, client_for, make_settings

PROJECT = "sosafe-test-project"


def _key_and_cert(name: str = "test") -> tuple[Any, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


KEY_A, CERT_A = _key_and_cert("a")
KEY_B, CERT_B = _key_and_cert("b")


class Google:
    """Serves the signing keys like Google does."""

    def __init__(self) -> None:
        self.certs = {"kid-a": CERT_A}
        self.fetches = 0
        self.fail = False
        self.max_age = 3600

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        if self.fail:
            return httpx.Response(500)
        return httpx.Response(200, json=self.certs, headers={"Cache-Control": f"public, max-age={self.max_age}"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def token(uid: str = "user-1", key: Any = KEY_A, kid: str = "kid-a", **changes: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "aud": PROJECT,
        "sub": uid,
        "iat": now - 10,
        "exp": now + 3600,
        "auth_time": now - 100,
    }
    claims.update(changes)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def verifier(google: Google) -> FirebaseTokenVerifier:
    return FirebaseTokenVerifier(PROJECT, httpx.AsyncClient(transport=google.transport()))


# ─── the verifier ───

@pytest.mark.anyio
async def test_a_valid_token_gives_the_user_id():
    assert await verifier(Google()).verify(token("abc")) == "abc"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(lambda: token(exp=int(time.time()) - 120), id="expired"),
        pytest.param(lambda: token(aud="someone-elses-project"), id="other project"),
        pytest.param(lambda: token(iss="https://securetoken.google.com/someone-else"), id="wrong issuer"),
        pytest.param(lambda: token(iat=int(time.time()) + 600), id="issued in the future"),
        pytest.param(lambda: token(auth_time=int(time.time()) + 600), id="signed in in the future"),
        pytest.param(lambda: token(auth_time=None), id="no auth_time"),
        pytest.param(lambda: token(sub=""), id="no user"),
        pytest.param(lambda: token(key=KEY_B), id="signed by a key that is not Google's"),
        pytest.param(lambda: token(kid="kid-unknown"), id="unknown key id"),
        pytest.param(lambda: jwt.encode({"sub": "x", "aud": PROJECT}, None, algorithm="none"), id="alg none"),
        pytest.param(lambda: jwt.encode({"sub": "x"}, "secret", algorithm="HS256", headers={"kid": "kid-a"}), id="HS256"),
        pytest.param(lambda: token()[:-8] + "AAAAAAAA", id="tampered signature"),
        pytest.param(lambda: "not.a.token", id="garbage"),
        pytest.param(lambda: "x" * 5000, id="huge"),
    ],
)
async def test_bad_tokens_are_refused(bad):
    with pytest.raises(InvalidToken):
        await verifier(Google()).verify(bad())


@pytest.mark.anyio
async def test_keys_are_fetched_once_and_kept():
    google = Google()
    v = verifier(google)
    for uid in ("a", "b", "c"):
        await v.verify(token(uid))
    assert google.fetches == 1


@pytest.mark.anyio
async def test_a_token_checked_once_is_not_checked_again():
    google = Google()
    v = verifier(google)
    t = token()
    await v.verify(t)
    google.certs = {}  # even if the keys were gone, the remembered token stands until it expires
    google.fail = True
    assert await v.verify(t) == "user-1"


@pytest.mark.anyio
async def test_a_new_google_key_is_picked_up():
    google = Google()
    v = verifier(google)
    await v.verify(token())
    v._keys_fetched_at -= 120  # a while later
    google.certs = {"kid-a": CERT_A, "kid-b": CERT_B}  # Google rotated in a new key
    assert await v.verify(token("u2", key=KEY_B, kid="kid-b")) == "u2"


@pytest.mark.anyio
async def test_bad_key_ids_cannot_make_it_fetch_over_and_over():
    google = Google()
    v = verifier(google)
    await v.verify(token())
    for _ in range(20):
        with pytest.raises(InvalidToken):
            await v.verify(token(kid="made-up"))
    assert google.fetches == 1, "unknown ids refetch at most once a minute"


@pytest.mark.anyio
async def test_google_unreachable_with_no_keys_on_hand_is_unavailable():
    google = Google()
    google.fail = True
    with pytest.raises(AuthUnavailable):
        await verifier(google).verify(token())


@pytest.mark.anyio
async def test_google_unreachable_later_keeps_using_the_keys_on_hand():
    google = Google()
    google.max_age = 0  # expire at once (kept at least 5 minutes anyway)
    v = verifier(google)
    await v.verify(token("a"))
    v._keys_valid_until = 0  # time to refresh
    google.fail = True
    assert await v.verify(token("b")) == "b"


# ─── through the API ───

def google_client(ola, mode: str, google: Google):
    return client_for(ola, make_settings(auth_mode=mode, firebase_project_id=PROJECT), google=google.transport())


def search(client, **kwargs):
    return client.get("/api/v1/location/search", params={"query": "hazratganj"}, **kwargs)


def test_required_without_a_token_is_401(ola):
    with google_client(ola, "required", Google()) as client:
        r = search(client)
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"
    assert body(r)["error"]["code"] == "unauthenticated"
    assert ola.calls == [], "Ola was not asked"


def test_required_with_a_bad_token_is_401(ola):
    with google_client(ola, "required", Google()) as client:
        r = search(client, headers={"Authorization": f"Bearer {token(key=KEY_B)}"})
    assert r.status_code == 401


def test_required_with_a_good_token_works(ola):
    with google_client(ola, "required", Google()) as client:
        r = search(client, headers={"Authorization": f"Bearer {token()}"})
    assert r.status_code == 200


def test_required_and_google_unreachable_is_503_not_401(ola):
    google = Google()
    google.fail = True
    with google_client(ola, "required", google) as client:
        r = search(client, headers={"Authorization": f"Bearer {token()}"})
    assert r.status_code == 503
    assert body(r)["error"]["code"] == "auth_unavailable"


def test_optional_lets_the_old_app_in_without_a_token(ola):
    with google_client(ola, "optional", Google()) as client:
        assert search(client).status_code == 200


def test_optional_with_a_bad_token_is_treated_as_not_signed_in(ola):
    with google_client(ola, "optional", Google()) as client:
        assert search(client, headers={"Authorization": "Bearer junk"}).status_code == 200


def test_health_needs_no_sign_in(ola):
    with google_client(ola, "required", Google()) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200


def test_required_without_a_project_id_does_not_start(ola):
    from app.config import ConfigError

    with pytest.raises(ConfigError):
        with client_for(ola, make_settings(auth_mode="required", firebase_project_id="")):
            pass
