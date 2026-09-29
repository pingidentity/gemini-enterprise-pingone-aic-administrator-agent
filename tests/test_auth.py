"""Tests for PingOne AIC JWT bearer assertion creation."""

from __future__ import annotations

import base64
import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from ping_admin_agent.auth import create_client_assertion, parse_private_jwk


def _b64(value: int) -> str:
    size = max(1, (value.bit_length() + 7) // 8)
    return base64.urlsafe_b64encode(value.to_bytes(size, "big")).rstrip(b"=").decode()


@pytest.fixture
def jwk():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.private_numbers()
    public = numbers.public_numbers
    return {
        "kty": "RSA",
        "n": _b64(public.n),
        "e": _b64(public.e),
        "d": _b64(numbers.d),
        "p": _b64(numbers.p),
        "q": _b64(numbers.q),
        "dp": _b64(numbers.dmp1),
        "dq": _b64(numbers.dmq1),
        "qi": _b64(numbers.iqmp),
        "kid": "rotation-key-1",
    }


def test_assertion_claims_header_and_signature(jwk):
    token_url = "https://tenant.example.com:443/am/oauth2/access_token"
    token = create_client_assertion("sa-123", jwk, token_url, now=1_700_000_000)
    header = jwt.get_unverified_header(token)
    public_jwk = {key: jwk[key] for key in ("kty", "n", "e")}
    claims = jwt.decode(token, RSAAlgorithm.from_jwk(json.dumps(public_jwk)), algorithms=["RS256"], audience=token_url, options={"verify_exp": False})
    assert header["alg"] == "RS256"
    assert header["typ"] == "JWT"
    assert header["kid"] == "rotation-key-1"
    assert claims["iss"] == "sa-123"
    assert claims["sub"] == "sa-123"
    assert claims["aud"] == token_url
    assert claims["exp"] == 1_700_000_899
    assert claims["jti"]


def test_assertion_jti_is_unique(jwk):
    first = create_client_assertion("sa-123", jwk, "https://tenant:443/am/oauth2/access_token")
    second = create_client_assertion("sa-123", jwk, "https://tenant:443/am/oauth2/access_token")
    assert jwt.decode(first, options={"verify_signature": False})["jti"] != jwt.decode(second, options={"verify_signature": False})["jti"]


@pytest.mark.parametrize(
    "raw",
    ["not-json", "[]", json.dumps({"kty": "EC", "n": "n", "e": "e", "d": "d"})],
)
def test_malformed_jwk_is_rejected(raw):
    with pytest.raises(ValueError, match="JWK"):
        parse_private_jwk(raw)


def test_public_only_jwk_is_rejected(jwk):
    public = {key: jwk[key] for key in ("kty", "n", "e")}
    with pytest.raises(ValueError, match="private"):
        parse_private_jwk(json.dumps(public))


def test_jwk_errors_do_not_include_secret_material():
    raw = '{"kty":"RSA","n":"PRIVATE-N","e":"AQAB","d":"PRIVATE-D"}'
    with pytest.raises(ValueError) as exc_info:
        parse_private_jwk(raw)
    assert "PRIVATE-N" not in str(exc_info.value)
    assert "PRIVATE-D" not in str(exc_info.value)
