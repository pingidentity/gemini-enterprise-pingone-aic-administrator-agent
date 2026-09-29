"""PingOne AIC service-account JWT bearer authentication helpers.

The private JWK is parsed and validated at configuration load time, then used
only to create short-lived JWT bearer assertions.  Callers should never log
or include the assertion or private key in an exception message.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from typing import Any

import jwt
from jwt.algorithms import RSAAlgorithm

_REQUIRED_RSA_PRIVATE_FIELDS = ("n", "e", "d")


def _validate_jwk_object(value: object) -> dict[str, Any]:
    """Validate and copy a private RSA JWK without exposing its contents."""
    if not isinstance(value, dict):
        raise ValueError("Private JWK must be a JSON object.")  # noqa: TRY004 -- One ValueError contract for callers.

    if value.get("kty") != "RSA":
        raise ValueError("Private JWK must have kty=RSA.")

    for field in _REQUIRED_RSA_PRIVATE_FIELDS:
        field_value = value.get(field)
        if not isinstance(field_value, str) or not field_value.strip():
            raise ValueError("Private JWK is missing required RSA private material.")
        try:
            # Ensure each required member is valid base64url integer material;
            # RSAAlgorithm performs the full key construction below.
            import base64

            base64.urlsafe_b64decode(field_value + "=" * (-len(field_value) % 4))
        except Exception as exc:
            raise ValueError("Private JWK contains invalid RSA key material.") from exc

    if "alg" in value and value["alg"] != "RS256":
        raise ValueError("Private JWK algorithm must be RS256.")
    if "kid" in value and (
        not isinstance(value["kid"], str) or not value["kid"].strip()
    ):
        raise ValueError("Private JWK kid must be a non-empty string.")

    # PyJWT/cryptography validates the RSA parameters and ensures that the
    # supplied key is actually private.  A public-only JWK therefore fails
    # here even though n and e are present.
    copied = dict(value)
    try:
        RSAAlgorithm.from_jwk(json.dumps(copied, separators=(",", ":")))
    except Exception as exc:
        raise ValueError("Private JWK contains invalid RSA key material.") from exc
    return copied


def parse_private_jwk(raw_value: str) -> dict[str, Any]:
    """Parse and validate a JSON-encoded private RSA JWK.

    Errors intentionally contain no raw JSON, key material, or assertion.
    """
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError("Private JWK must be a non-empty JSON object.")
    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Private JWK must contain valid JSON.") from exc
    return _validate_jwk_object(parsed)


def create_client_assertion(
    service_account_id: str,
    private_jwk: Mapping[str, Any],
    token_url: str,
    *,
    now: float | None = None,
) -> str:
    """Create the documented PingOne AIC JWT bearer assertion.

    The grant uses the service-account ID as both ``iss`` and ``sub`` and the
    exact token URL (including an explicit HTTPS ``:443`` when applicable) as
    ``aud``.  Assertions expire 899 seconds after their integer issue time.
    """
    if not isinstance(service_account_id, str) or not service_account_id.strip():
        raise ValueError("Service-account ID must be non-empty.")
    if not isinstance(token_url, str) or not token_url.strip():
        raise ValueError("Token URL must be non-empty.")

    jwk = _validate_jwk_object(dict(private_jwk))
    issued_at = int(time.time() if now is None else now)
    claims = {
        "iss": service_account_id,
        "sub": service_account_id,
        "aud": token_url,
        "exp": issued_at + 899,
        "jti": uuid.uuid4().hex,
    }
    headers = {"alg": "RS256", "typ": "JWT"}
    if "kid" in jwk:
        headers["kid"] = jwk["kid"]

    try:
        signing_key = RSAAlgorithm.from_jwk(
            json.dumps(jwk, separators=(",", ":"))
        )
        return jwt.encode(claims, signing_key, algorithm="RS256", headers=headers)
    except Exception as exc:
        raise ValueError("Could not sign service-account JWT assertion.") from exc
