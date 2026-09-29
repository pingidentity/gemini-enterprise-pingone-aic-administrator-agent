"""Configuration for the PingAIC admin/help desk agent.

Authentication is the documented PingOne AIC service-account JWT bearer grant.
Local development may provide the service-account ID and private JWK directly;
Agent Runtime provides Secret Manager resource names and loads those values
with its runtime service account.
The private JWK is never included in the configuration representation or
exception text.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .auth import parse_private_jwk


@dataclass(frozen=True)
class PingAdminConfig:
    """Connection + OAuth2 settings for a single PingAIC tenant."""

    # Tenant base, e.g. https://openam-<tenant>.forgeblocks.com (no trailing slash).
    base_url: str

    # Realm the managed objects live in. Almost always "alpha".
    realm: str

    # PingOne AIC service-account identity and validated private signing key.
    service_account_id: str
    private_jwk: dict[str, Any] = field(repr=False, compare=False)

    # Tenant-root token endpoint (or explicit PING_TOKEN_URL override).
    token_url: str

    # Monitoring API credentials are independent from the service-account JWT.
    # They are only attached to /monitoring/logs requests.
    audit_api_key: str | None = field(default=None, repr=False, compare=False)
    audit_api_secret: str | None = field(default=None, repr=False, compare=False)

    # Documented payload path used for username filtering. This is intentionally
    # unset until the tenant's audit schema is confirmed; no field is guessed.
    audit_username_field: str | None = None

    # Space-separated scopes. Admin/help desk needs IDM + AM.
    scopes: str = "fr:idm:* fr:am:*"

    # Seconds to subtract from a token's expiry before treating it as stale.
    token_leeway_seconds: int = 60

    # Per-request timeout in seconds.
    request_timeout_seconds: float = 30.0

    @property
    def idm_base(self) -> str:
        """Base for /openidm/managed and /openidm/config."""
        return f"{self.base_url}/openidm"

    @property
    def am_realm_base(self) -> str:
        """Base for AM realm endpoints (sessions, users/{id}/devices, etc.)."""
        return f"{self.base_url}/am/json/realms/root/realms/{self.realm}"

    @property
    def monitoring_base(self) -> str:
        """Base for /monitoring/logs (audit events)."""
        return f"{self.base_url}/monitoring"

    @property
    def managed_user_path(self) -> str:
        return f"managed/{self.realm}_user"

    @property
    def managed_group_path(self) -> str:
        return f"managed/{self.realm}_group"

    @property
    def managed_role_path(self) -> str:
        return f"managed/{self.realm}_role"

    @property
    def managed_assignment_path(self) -> str:
        return f"managed/{self.realm}_assignment"

    @property
    def managed_application_path(self) -> str:
        return f"managed/{self.realm}_application"


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            "Copy .env.example to .env for local dev, or set it on the service."
        )
    return value


def _fetch_secret(resource_name: str) -> str:
    """Read the latest value of a Secret Manager secret via ADC."""
    # Imported lazily so tests that stub credential resolution do not need the
    # Google client library installed just to import this module.
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(request={"name": resource_name})
    return response.payload.data.decode("utf-8")


def _resolve_secret_pair(
    *,
    plaintext_names: tuple[str, str],
    secret_names: tuple[str, str],
    label: str,
    optional: bool = False,
) -> tuple[str, str] | None:
    """Resolve an optional credential pair without leaking payloads."""
    plain = tuple(os.environ.get(name, "").strip() for name in plaintext_names)
    refs = tuple(os.environ.get(name, "").strip() for name in secret_names)
    plain_any = any(plain)
    refs_any = any(refs)
    if plain_any and refs_any:
        raise RuntimeError(f"Do not mix plaintext {label} values with Secret Manager refs.")
    if not plain_any and not refs_any:
        if optional:
            return None
        raise RuntimeError(f"Both {plaintext_names[0]} and {plaintext_names[1]} are required.")
    if plain_any:
        if not all(plain):
            raise RuntimeError(f"Both {plaintext_names[0]} and {plaintext_names[1]} are required.")
        return plain
    if not all(refs):
        raise RuntimeError(f"Both {secret_names[0]} and {secret_names[1]} are required.")
    try:
        return _fetch_secret(refs[0]).strip(), _fetch_secret(refs[1]).strip()
    except Exception as exc:  # Keep secret payloads redacted.
        raise RuntimeError(f"Failed to load {label} from Secret Manager.") from exc


def _resolve_audit_auth() -> tuple[str, str] | None:
    return _resolve_secret_pair(
        plaintext_names=("PING_ADMIN_AUDIT_API_KEY", "PING_ADMIN_AUDIT_API_SECRET"),
        secret_names=(
            "PING_ADMIN_AUDIT_API_KEY_SECRET",
            "PING_ADMIN_AUDIT_API_SECRET_SECRET",
        ),
        label="audit API credentials",
        optional=True,
    )


def _resolve_service_account_auth() -> tuple[str, dict[str, Any]]:
    """Resolve service-account ID and private JWK from env or Secret Manager.

    Plaintext values and Secret Manager refs are each an all-or-nothing pair;
    mixed or partial configuration is rejected instead of silently falling
    back between authentication modes.
    """
    env_id = os.environ.get("PING_ADMIN_SERVICE_ACCOUNT_ID", "").strip()
    env_jwk = os.environ.get("PING_ADMIN_PRIVATE_JWK", "").strip()
    ref_id = os.environ.get("PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET", "").strip()
    ref_jwk = os.environ.get("PING_ADMIN_PRIVATE_JWK_SECRET", "").strip()

    plaintext_any = bool(env_id or env_jwk)
    secret_any = bool(ref_id or ref_jwk)
    if plaintext_any and secret_any:
        raise RuntimeError(
            "Do not mix plaintext service-account values with Secret Manager refs. "
            "Configure both values from one source."
        )
    if plaintext_any:
        if not (env_id and env_jwk):
            raise RuntimeError(
                "Both PING_ADMIN_SERVICE_ACCOUNT_ID and PING_ADMIN_PRIVATE_JWK "
                "are required for plaintext local authentication."
            )
        raw_id, raw_jwk = env_id, env_jwk
    elif secret_any:
        if not (ref_id and ref_jwk):
            raise RuntimeError(
                "Both PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET and "
                "PING_ADMIN_PRIVATE_JWK_SECRET are required for Secret Manager "
                "authentication."
            )
        try:
            raw_id = _fetch_secret(ref_id)
            raw_jwk = _fetch_secret(ref_jwk)
        except Exception as exc:  # Keep secret payloads redacted.
            raise RuntimeError(
                "Failed to load PingAIC service-account authentication from "
                "Secret Manager."
            ) from exc
    else:
        raise RuntimeError(
            "PingAIC service-account authentication is not configured. Set "
            "PING_ADMIN_SERVICE_ACCOUNT_ID and PING_ADMIN_PRIVATE_JWK for local "
            "dev, or PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET and "
            "PING_ADMIN_PRIVATE_JWK_SECRET for deployed services."
        )

    service_account_id = raw_id.strip()
    if not service_account_id:
        raise RuntimeError("PingAIC service-account ID cannot be empty.")
    try:
        private_jwk = parse_private_jwk(raw_jwk)
    except ValueError as exc:
        raise RuntimeError("Invalid PingAIC private JWK configuration.") from exc
    return service_account_id, private_jwk


def _token_url(base_url: str) -> str:
    override = os.environ.get("PING_TOKEN_URL", "").strip()
    if override:
        return override
    # The provider contract requires the tenant-root endpoint and an explicit
    # :443 for HTTPS when no port is supplied.
    if base_url.startswith("https://") and base_url.count(":") == 1:
        return f"{base_url}:443/am/oauth2/access_token"
    return f"{base_url}/am/oauth2/access_token"


@lru_cache(maxsize=1)
def get_config() -> PingAdminConfig:
    """Build the config once from the environment."""
    base_url = _require("PING_BASE_URL").rstrip("/")
    realm = (os.environ.get("PING_REALM", "").strip() or "alpha").lower()
    service_account_id, private_jwk = _resolve_service_account_auth()
    audit_auth = _resolve_audit_auth()
    audit_username_field = os.environ.get("PING_ADMIN_AUDIT_USERNAME_FIELD", "").strip() or None
    return PingAdminConfig(
        base_url=base_url,
        realm=realm,
        service_account_id=service_account_id,
        private_jwk=private_jwk,
        token_url=_token_url(base_url),
        audit_api_key=audit_auth[0] if audit_auth else None,
        audit_api_secret=audit_auth[1] if audit_auth else None,
        audit_username_field=audit_username_field,
        scopes=os.environ.get("PING_SCOPES", "").strip() or "fr:idm:* fr:am:*",
    )
