"""Shared test fixtures."""

from __future__ import annotations

import pytest
import vertexai
from google.auth.credentials import AnonymousCredentials

from ping_admin_agent import config as config_module
from ping_admin_agent import ping_client as ping_client_module
from ping_admin_agent.config import PingAdminConfig

TEST_PRIVATE_JWK = {
    "kty": "RSA",
    "n": "sXchQ2Z5QW5jQXJjRkV5YlM4bV9xVjFhV1J1dFh2eE9qQmV4",
    "e": "AQAB",
    "d": "not-a-real-test-key",
}

# PingClient tests replace signing with a fake assertion; config tests use a
# real test key generated in test_config.py.  Keep this fixture's key-shaped
# value out of production and use it only with the stubbed config.
DEFAULT_TEST_CONFIG = PingAdminConfig(
    base_url="https://openam-test-tenant.forgeblocks.com",
    realm="alpha",
    service_account_id="test-service-account",
    private_jwk=TEST_PRIVATE_JWK,
    token_url=(
        "https://openam-test-tenant.forgeblocks.com:443/am/oauth2/access_token"
    ),
    audit_api_key="test-audit-key",
    audit_api_secret="test-audit-secret",
    audit_username_field="payload.userName",
)


@pytest.fixture(autouse=True)
def _reset_config_cache():
    config_module.get_config.cache_clear()
    yield
    config_module.get_config.cache_clear()


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    ping_client_module._client = None
    yield
    ping_client_module._client = None


@pytest.fixture
def stub_config(monkeypatch):
    monkeypatch.setattr(config_module, "get_config", lambda: DEFAULT_TEST_CONFIG)
    return DEFAULT_TEST_CONFIG


@pytest.fixture
def managed_runtime_env(monkeypatch, tmp_path):
    """Mimic the managed Agent Runtime process without Google Cloud access."""
    vertexai.init(
        project="customer-project",
        location="us-central1",
        credentials=AnonymousCredentials(),
    )
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "123")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "customer-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    # Worker-shared state goes to a per-test directory, never the real temp dir.
    monkeypatch.setenv("PING_ADMIN_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("PING_ADMIN_STATE_STORE", raising=False)
