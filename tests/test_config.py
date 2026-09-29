"""Tests for service-account configuration and Secret Manager loading."""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ping_admin_agent import config as config_module

ENV_KEYS = [
    "PING_BASE_URL",
    "PING_REALM",
    "PING_SCOPES",
    "PING_ADMIN_SERVICE_ACCOUNT_ID",
    "PING_ADMIN_PRIVATE_JWK",
    "PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET",
    "PING_ADMIN_PRIVATE_JWK_SECRET",
    "PING_ADMIN_AUDIT_API_KEY",
    "PING_ADMIN_AUDIT_API_SECRET",
    "PING_ADMIN_AUDIT_API_KEY_SECRET",
    "PING_ADMIN_AUDIT_API_SECRET_SECRET",
    "PING_ADMIN_AUDIT_USERNAME_FIELD",
    "PING_TOKEN_URL",
]


def _b64(value: int) -> str:
    length = max(1, (value.bit_length() + 7) // 8)
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode()


@pytest.fixture
def private_jwk():
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
        "kid": "test-key",
    }


@pytest.fixture(autouse=True)
def _clear_ping_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _set_base(monkeypatch):
    monkeypatch.setenv("PING_BASE_URL", "https://tenant.example.com")


def test_env_service_account_and_jwk(monkeypatch, private_jwk):
    _set_base(monkeypatch)
    monkeypatch.setenv("PING_ADMIN_SERVICE_ACCOUNT_ID", "service-id")
    monkeypatch.setenv("PING_ADMIN_PRIVATE_JWK", json.dumps(private_jwk))
    cfg = config_module.get_config()
    assert cfg.service_account_id == "service-id"
    assert cfg.private_jwk["kid"] == "test-key"
    assert cfg.audit_api_key is None
    assert cfg.audit_api_secret is None
    assert cfg.base_url == "https://tenant.example.com"
    assert cfg.realm == "alpha"
    assert cfg.scopes == "fr:idm:* fr:am:*"
    assert cfg.token_url == "https://tenant.example.com:443/am/oauth2/access_token"
    assert "private_jwk" not in repr(cfg)
    assert json.dumps(private_jwk)[1:-1] not in repr(cfg)


def test_secret_manager_used_when_plaintext_missing(monkeypatch, private_jwk):
    _set_base(monkeypatch)
    monkeypatch.setenv(
        "PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET",
        "projects/p/secrets/id/versions/latest",
    )
    monkeypatch.setenv(
        "PING_ADMIN_PRIVATE_JWK_SECRET",
        "projects/p/secrets/jwk/versions/latest",
    )
    fetched: list[str] = []

    def fake_fetch(resource_name: str) -> str:
        fetched.append(resource_name)
        return "sm-id" if resource_name.endswith("id/versions/latest") else json.dumps(private_jwk)

    monkeypatch.setattr(config_module, "_fetch_secret", fake_fetch)
    cfg = config_module.get_config()
    assert cfg.service_account_id == "sm-id"
    assert cfg.private_jwk["kty"] == "RSA"
    assert fetched == [
        "projects/p/secrets/id/versions/latest",
        "projects/p/secrets/jwk/versions/latest",
    ]


def test_missing_auth_raises(monkeypatch):
    _set_base(monkeypatch)
    with pytest.raises(RuntimeError, match="service-account authentication"):
        config_module.get_config()


@pytest.mark.parametrize(
    "values",
    [
        {"PING_ADMIN_SERVICE_ACCOUNT_ID": "id"},
        {"PING_ADMIN_PRIVATE_JWK": "{}"},
        {"PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET": "ref"},
        {"PING_ADMIN_PRIVATE_JWK_SECRET": "ref"},
    ],
)
def test_partial_auth_is_rejected(monkeypatch, values):
    _set_base(monkeypatch)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match="required"):
        config_module.get_config()


def test_mixed_auth_is_rejected(monkeypatch, private_jwk):
    _set_base(monkeypatch)
    monkeypatch.setenv("PING_ADMIN_SERVICE_ACCOUNT_ID", "id")
    monkeypatch.setenv("PING_ADMIN_PRIVATE_JWK", json.dumps(private_jwk))
    monkeypatch.setenv("PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET", "ref")
    monkeypatch.setenv("PING_ADMIN_PRIVATE_JWK_SECRET", "ref")
    with pytest.raises(RuntimeError, match="mix"):
        config_module.get_config()


def test_secret_manager_failure_is_redacted(monkeypatch):
    _set_base(monkeypatch)
    monkeypatch.setenv("PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET", "secret-id-ref")
    monkeypatch.setenv("PING_ADMIN_PRIVATE_JWK_SECRET", "secret-jwk-ref")

    def fake_fetch(_resource_name: str) -> str:
        raise RuntimeError("private-material-should-not-leak")

    monkeypatch.setattr(config_module, "_fetch_secret", fake_fetch)
    with pytest.raises(RuntimeError, match="Failed to load") as exc_info:
        config_module.get_config()
    assert "private-material-should-not-leak" not in str(exc_info.value)
    assert "secret-jwk-ref" not in str(exc_info.value)


def test_invalid_public_only_or_malformed_jwk_is_rejected(monkeypatch):
    _set_base(monkeypatch)
    monkeypatch.setenv("PING_ADMIN_SERVICE_ACCOUNT_ID", "id")
    for raw in ("not-json", json.dumps({"kty": "RSA", "n": "abc", "e": "AQAB"})):
        monkeypatch.setenv("PING_ADMIN_PRIVATE_JWK", raw)
        with pytest.raises(RuntimeError, match="Invalid"):
            config_module.get_config.cache_clear()
            config_module.get_config()


def test_audit_secret_refs_are_loaded_and_redacted(monkeypatch, private_jwk):
    _set_base(monkeypatch)
    monkeypatch.setenv("PING_ADMIN_SERVICE_ACCOUNT_ID", "id")
    monkeypatch.setenv("PING_ADMIN_PRIVATE_JWK", json.dumps(private_jwk))
    monkeypatch.setenv("PING_ADMIN_AUDIT_API_KEY_SECRET", "projects/p/secrets/key/versions/latest")
    monkeypatch.setenv("PING_ADMIN_AUDIT_API_SECRET_SECRET", "projects/p/secrets/secret/versions/latest")
    monkeypatch.setenv("PING_ADMIN_AUDIT_USERNAME_FIELD", "payload.userName")
    values = {
        "projects/p/secrets/key/versions/latest": "audit-key",
        "projects/p/secrets/secret/versions/latest": "audit-secret",
    }
    monkeypatch.setattr(config_module, "_fetch_secret", values.__getitem__)
    cfg = config_module.get_config()
    assert (cfg.audit_api_key, cfg.audit_api_secret) == ("audit-key", "audit-secret")
    assert cfg.audit_username_field == "payload.userName"
    assert "audit-key" not in repr(cfg)
    assert "audit-secret" not in repr(cfg)


def test_custom_token_url_override(monkeypatch, private_jwk):
    _set_base(monkeypatch)
    monkeypatch.setenv("PING_ADMIN_SERVICE_ACCOUNT_ID", "id")
    monkeypatch.setenv("PING_ADMIN_PRIVATE_JWK", json.dumps(private_jwk))
    monkeypatch.setenv("PING_TOKEN_URL", "https://custom.example/token")
    assert config_module.get_config().token_url == "https://custom.example/token"


def test_missing_base_url_raises(monkeypatch, private_jwk):
    monkeypatch.setenv("PING_ADMIN_SERVICE_ACCOUNT_ID", "id")
    monkeypatch.setenv("PING_ADMIN_PRIVATE_JWK", json.dumps(private_jwk))
    with pytest.raises(RuntimeError, match="PING_BASE_URL"):
        config_module.get_config()


def test_managed_object_paths(monkeypatch, private_jwk):
    _set_base(monkeypatch)
    monkeypatch.setenv("PING_ADMIN_SERVICE_ACCOUNT_ID", "x")
    monkeypatch.setenv("PING_ADMIN_PRIVATE_JWK", json.dumps(private_jwk))
    monkeypatch.setenv("PING_REALM", "bravo")
    cfg = config_module.get_config()
    assert cfg.managed_user_path == "managed/bravo_user"
    assert cfg.managed_group_path == "managed/bravo_group"
    assert cfg.managed_role_path == "managed/bravo_role"
    assert cfg.managed_assignment_path == "managed/bravo_assignment"
    assert cfg.managed_application_path == "managed/bravo_application"
    assert cfg.am_realm_base.endswith("/realms/bravo")
