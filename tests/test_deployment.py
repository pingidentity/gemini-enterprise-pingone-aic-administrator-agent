"""Offline tests for the Agent Runtime deployment configuration."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

import deployment
from deployment import deploy
from ping_admin_agent import branding
from ping_admin_agent.a2a import build_agent_card, gemini_agent_card

RESOURCE = "projects/123456789012/locations/us-central1/reasoningEngines/123"


def _set_secret_runtime(monkeypatch):
    monkeypatch.setenv("PING_BASE_URL", "https://tenant.example.com")
    monkeypatch.setenv(
        "PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET",
        "projects/p/secrets/id/versions/latest",
    )
    monkeypatch.setenv(
        "PING_ADMIN_PRIVATE_JWK_SECRET",
        "projects/p/secrets/jwk/versions/latest",
    )
    monkeypatch.setenv(
        "PING_ADMIN_AUDIT_API_KEY_SECRET",
        "projects/p/secrets/audit-key/versions/latest",
    )
    monkeypatch.setenv(
        "PING_ADMIN_AUDIT_API_SECRET_SECRET",
        "projects/p/secrets/audit-secret/versions/latest",
    )
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")


def _serve_card(monkeypatch, card, seen):
    """Stub ADC and the authenticated discovery request with a fixed card."""
    monkeypatch.setattr(
        deploy.google.auth, "default", lambda **kwargs: ("test-credentials", None)
    )

    @contextmanager
    def authorized_session(credentials):
        assert credentials == "test-credentials"

        def get(endpoint, **kwargs):
            seen.append(endpoint)
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: card)

        yield SimpleNamespace(get=get)

    monkeypatch.setattr(deploy, "AuthorizedSession", authorized_session)


def test_runtime_env_forwards_secret_refs_not_plaintext(monkeypatch):
    _set_secret_runtime(monkeypatch)
    monkeypatch.setenv(
        "PING_ADMIN_SERVICE_ACCOUNT", "runtime@project.iam.gserviceaccount.com"
    )

    env = deploy._runtime_env()

    assert env["PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET"].endswith("id/versions/latest")
    assert env["PING_ADMIN_PRIVATE_JWK_SECRET"].endswith("jwk/versions/latest")
    assert "PING_ADMIN_SERVICE_ACCOUNT_ID" not in env
    assert "PING_ADMIN_PRIVATE_JWK" not in env
    assert "PING_ADMIN_SERVICE_ACCOUNT" not in env


def test_runtime_env_rejects_plaintext_auth(monkeypatch):
    _set_secret_runtime(monkeypatch)
    monkeypatch.setenv("PING_ADMIN_PRIVATE_JWK", '{"private":"material"}')

    with pytest.raises(SystemExit, match="plaintext"):
        deploy._runtime_env()


def test_runtime_env_rejects_plaintext_audit_auth(monkeypatch):
    _set_secret_runtime(monkeypatch)
    monkeypatch.setenv("PING_ADMIN_AUDIT_API_SECRET", "local-secret")
    with pytest.raises(SystemExit, match="plaintext"):
        deploy._runtime_env()


def test_runtime_env_requires_both_secret_refs(monkeypatch):
    monkeypatch.setenv("PING_BASE_URL", "https://tenant.example.com")
    monkeypatch.setenv(
        "PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET", "projects/p/secrets/id/versions/latest"
    )

    with pytest.raises(SystemExit, match="PING_ADMIN_PRIVATE_JWK_SECRET"):
        deploy._runtime_env()


def test_runtime_env_requires_audit_secret_refs(monkeypatch):
    _set_secret_runtime(monkeypatch)
    monkeypatch.delenv("PING_ADMIN_AUDIT_API_SECRET_SECRET")
    with pytest.raises(SystemExit, match="PING_ADMIN_AUDIT_API_SECRET_SECRET"):
        deploy._runtime_env()


def test_engine_kwargs_selects_runtime_identity_and_package(monkeypatch):
    _set_secret_runtime(monkeypatch)
    monkeypatch.setenv(
        "PING_ADMIN_SERVICE_ACCOUNT", "agent-runtime@project.iam.gserviceaccount.com"
    )

    kwargs = deploy._engine_kwargs()

    assert kwargs["extra_packages"] == ["ping_admin_agent"]
    assert kwargs["service_account"] == "agent-runtime@project.iam.gserviceaccount.com"
    assert "PING_ADMIN_PRIVATE_JWK" not in kwargs["env_vars"]


def test_create_passes_root_agent_and_runtime_options(monkeypatch):
    _set_secret_runtime(monkeypatch)
    monkeypatch.setenv("STAGING_BUCKET", "gs://bucket")
    monkeypatch.setattr(deploy, "_confirm", lambda action, resource=None: None)
    monkeypatch.setattr(deploy, "_init", lambda: None)
    monkeypatch.setattr(deploy, "_wrapped_app", lambda: "wrapped-app")
    captured = {}

    def fake_create(agent, **kwargs):
        captured["agent"] = agent
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            resource_name="projects/p/locations/r/reasoningEngines/1"
        )

    monkeypatch.setattr(deploy.agent_engines, "create", fake_create)
    monkeypatch.setattr(deploy, "export_agent_card", lambda *args: None)
    deploy.create()

    assert captured["agent"] == "wrapped-app"
    assert captured["kwargs"]["extra_packages"] == ["ping_admin_agent"]
    assert (
        captured["kwargs"]["env_vars"]["PING_BASE_URL"] == "https://tenant.example.com"
    )


def test_info_reads_engine_without_staging_bucket(monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project")
    monkeypatch.setattr(deploy, "_init", lambda **kwargs: None)
    monkeypatch.setattr(
        deploy.agent_engines,
        "get",
        lambda resource_name: SimpleNamespace(
            resource_name=resource_name,
            display_name="Admin agent",
            description="Help desk",
        ),
    )

    deploy.info("projects/p/locations/r/reasoningEngines/1")

    output = capsys.readouterr().out
    assert "display_name: Admin agent" in output
    assert "description: Help desk" in output


def test_deployment_uses_the_tested_dependencies_and_a2a_wrapper(monkeypatch):
    import vertexai
    from google.auth.credentials import AnonymousCredentials

    from ping_admin_agent.runtime import PingAdminA2aAgent

    vertexai.init(
        project="customer", location="us-central1", credentials=AnonymousCredentials()
    )
    assert isinstance(deploy._wrapped_app(), PingAdminA2aAgent)
    assert "a2a-sdk[http-server]==1.1.2" in deploy.REQUIREMENTS
    assert "google-adk[a2a]==2.8.0" in deploy.REQUIREMENTS
    assert not any(line.startswith("uvicorn") for line in deploy.REQUIREMENTS)
    _set_secret_runtime(monkeypatch)
    monkeypatch.setenv("PING_ADMIN_A2UI_ENABLED", "false")
    monkeypatch.setenv("PING_ADMIN_MODEL", "customer-selected-model")
    monkeypatch.setenv("PING_ADMIN_MODEL_LOCATION", "eu")
    monkeypatch.setenv("PING_ADMIN_LOGO_URL", "https://assets.customer.example/ping.svg")
    env = deploy._runtime_env()
    assert env["PING_ADMIN_A2UI_ENABLED"] == "false"
    assert env["PING_ADMIN_MODEL"] == "customer-selected-model"
    assert env["PING_ADMIN_MODEL_LOCATION"] == "eu"
    assert env["PING_ADMIN_LOGO_URL"] == "https://assets.customer.example/ping.svg"
    assert env["PING_ADMIN_STATE_STORE"] == "sqlite"
    assert deploy._resolve_location() == "us-central1"


def test_memory_state_store_is_refused_for_managed_deployments(monkeypatch):
    _set_secret_runtime(monkeypatch)
    monkeypatch.setenv("PING_ADMIN_STATE_STORE", "memory")
    with pytest.raises(SystemExit, match="sqlite"):
        deploy._runtime_env()


def test_malformed_secret_ref_rejected_before_deployment(monkeypatch):
    _set_secret_runtime(monkeypatch)
    monkeypatch.setenv("PING_ADMIN_PRIVATE_JWK_SECRET", "jwk-ref")
    with pytest.raises(SystemExit, match="full Secret Manager"):
        deploy._runtime_env()


def test_update_uses_resource_destination_for_runtime_setup_and_exports_card(
    monkeypatch, tmp_path
):
    _set_secret_runtime(monkeypatch)
    monkeypatch.setenv("STAGING_BUCKET", "gs://customer-bucket")
    resource = "projects/123456/locations/europe-west4/reasoningEngines/123"
    initialized = []
    exported = []
    monkeypatch.setattr(
        deploy.vertexai, "init", lambda **kwargs: initialized.append(kwargs)
    )
    monkeypatch.setattr(deploy, "_confirm", lambda *args: None)
    monkeypatch.setattr(deploy, "_wrapped_app", lambda: "a2a-app")

    def update(**kwargs):
        assert initialized[0]["project"] == "123456"
        assert initialized[0]["location"] == "europe-west4"
        assert kwargs["resource_name"] == resource
        assert kwargs["agent_engine"] == "a2a-app"
        assert kwargs["max_instances"] == 1
        return SimpleNamespace(resource_name=resource)

    monkeypatch.setattr(deploy.agent_engines, "update", update)
    monkeypatch.setattr(
        deploy, "export_agent_card", lambda *args: exported.append(args)
    )
    deploy.update(resource, tmp_path)
    assert exported == [(resource, tmp_path)]


@pytest.mark.parametrize(
    "resource",
    [
        "https://attacker.example/card",
        "projects/p/locations/r/reasoningEngines/1/../2",
        "projects/p/locations/r/reasoningEngines/not-an-id",
    ],
)
def test_runtime_url_rejects_non_resource_input(resource):
    with pytest.raises(ValueError):
        deploy.runtime_url(resource)


def test_same_runtime_endpoint_normalizes_only_the_project_segment():
    expected = deploy.runtime_url(RESOURCE)
    by_id = expected.replace("/projects/123456789012/", "/projects/customer-project/")
    assert deploy.same_runtime_endpoint(by_id + "/", expected)
    assert deploy.same_runtime_endpoint(expected, expected)
    assert not deploy.same_runtime_endpoint(expected.replace("/a2a", "/a2a/v1"), expected)
    assert not deploy.same_runtime_endpoint(expected.replace("v1beta1", "v1"), expected)
    assert not deploy.same_runtime_endpoint(
        expected.replace("/reasoningEngines/123/", "/reasoningEngines/1234/"), expected
    )


@pytest.mark.parametrize("served_project", ["123456789012", "customer-project"])
def test_export_fetches_live_card_with_adc_and_accepts_project_id_or_number(
    monkeypatch, tmp_path, capsys, served_project
):
    url = deploy.runtime_url(RESOURCE)
    # The managed process names the project as it was configured at packaging
    # time, usually the project ID; resource names carry the project number.
    served = url.replace("/projects/123456789012/", f"/projects/{served_project}/")
    card = gemini_agent_card(build_agent_card(served))
    seen = []
    _serve_card(monkeypatch, card, seen)
    # A customer's output directory can contain unrelated credentials. The
    # registration archive must include only its explicitly selected artifacts.
    (tmp_path / "deployment.env").write_text("SECRET=test-secret-sentinel\n")
    deploy.export_agent_card(RESOURCE, tmp_path)
    assert seen == [f"{url}/v1/card"]
    exported = json.loads((tmp_path / "agent-card.json").read_text())
    assert exported == card
    assert exported["url"] == served
    assert exported["security"] == [{"google-oauth2": [deploy.CLOUD_PLATFORM_SCOPE]}]
    assert (
        json.loads((tmp_path / "runtime.json").read_text())["resource_name"] == RESOURCE
    )
    with ZipFile(tmp_path / "manual-registration.zip") as bundle:
        assert "deployment.env" not in bundle.namelist()
        assert json.loads(bundle.read("agent-card.json")) == card
        png = bundle.read("assets/ping-identity-logo.png")
        assert png == branding.LOGO_PNG.read_bytes()
        assert bundle.read("assets/ping-identity-logo.svg") == branding.LOGO_SVG.read_bytes()
        icon_patch = json.loads(bundle.read("gemini-enterprise-icon.json"))
        assert set(icon_patch) == {"icon"}
        # Gemini Enterprise renders the avatar from icon.uri; the inline
        # content form is stored but not rendered by the UI (verified live
        # 2026-09-25). The uri is the card's own public iconUrl.
        assert icon_patch["icon"] == {"uri": card["iconUrl"]}
        manifest = json.loads(bundle.read("branding.json"))
        assert manifest["a2aIconUrl"] == card["iconUrl"]
        # The card carries the square mark; the manifest records where the
        # bundled horizontal files came from.
        assert card["iconUrl"] == branding.OFFICIAL_LOGO_URL
        assert card["iconUrl"].endswith("/topnav-json-configs/Ping-Logo.svg")
        assert manifest["logoSourceUri"] == branding.BUNDLED_LOGO_SOURCE_URL
        assert manifest["logoSourceUri"] != card["iconUrl"]
        assert manifest["assets"]["assets/ping-identity-logo.png"]["sha256"] == hashlib.sha256(png).hexdigest()
        square_png = bundle.read("assets/ping-logo-square-1251.png")
        assert square_png == branding.SQUARE_LOGO_PNG.read_bytes()
        assert manifest["assets"]["assets/ping-logo-square-1251.png"]["sha256"] == hashlib.sha256(square_png).hexdigest()
        instructions = bundle.read("REGISTRATION.md").decode()
        assert "updateMask=icon" in instructions
        assert "Custom agent via A2A" in instructions
        assert "agentAuthorization" in instructions
        assert "IdP Admins" in instructions
    output = capsys.readouterr().out
    assert "Custom agent via A2A" in output
    assert "agentAuthorization" in output
    assert "IdP Admins" in output
    assert "manual-registration.zip" in output
    assert "Google OAuth2 access token" in output


@pytest.mark.parametrize("missing", ["security", "securitySchemes"])
def test_export_rejects_a_card_that_does_not_require_an_access_token(
    monkeypatch, tmp_path, missing
):
    card = gemini_agent_card(build_agent_card(deploy.runtime_url(RESOURCE)))
    del card[missing]
    _serve_card(monkeypatch, card, [])
    with pytest.raises(SystemExit, match="Card export failed"):
        deploy.export_agent_card(RESOURCE, tmp_path)
    assert not (tmp_path / "agent-card.json").exists()


@pytest.mark.parametrize(
    "card_url",
    [
        "http://localhost:8080/a2a",
        "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/123456789012/locations/us-central1/reasoningEngines/999/a2a",
        "https://europe-west4-aiplatform.googleapis.com/v1beta1/projects/123456789012/locations/europe-west4/reasoningEngines/123/a2a",
    ],
)
def test_export_rejects_a_card_for_another_runtime(monkeypatch, tmp_path, card_url):
    card = gemini_agent_card(build_agent_card(card_url))
    _serve_card(monkeypatch, card, [])
    with pytest.raises(SystemExit, match="Card export failed"):
        deploy.export_agent_card(RESOURCE, tmp_path)
    assert not (tmp_path / "agent-card.json").exists()


def test_failed_card_export_does_not_claim_registration_or_delete_runtime(
    monkeypatch, tmp_path
):
    def unavailable(**kwargs):
        raise RuntimeError("simulated ADC failure")

    monkeypatch.setattr(deploy.google.auth, "default", unavailable)
    with pytest.raises(SystemExit, match="retry --agent-card"):
        deploy.export_agent_card(RESOURCE, tmp_path)
    assert not (tmp_path / "agent-card.json").exists()


def test_customer_env_file_is_explicit_and_shell_takes_precedence(
    monkeypatch, tmp_path
):
    env_file = tmp_path / "deployment.env"
    env_file.write_text("GOOGLE_CLOUD_PROJECT=file-project\nPING_REALM=bravo\n")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "shell-project")
    # Track this key even if initially absent, so dotenv's write is undone.
    monkeypatch.setenv("PING_REALM", "temporary")
    monkeypatch.delenv("PING_REALM", raising=False)
    monkeypatch.setattr("sys.argv", ["deploy", "--create", "--env-file", str(env_file)])
    seen = []
    monkeypatch.setattr(
        deploy,
        "create",
        lambda output_dir: seen.append(
            (deploy._resolve_project(), deploy.os.environ["PING_REALM"], output_dir)
        ),
    )
    deploy.main()
    assert seen == [("shell-project", "bravo", Path("deployment-output"))]


def test_deployer_refuses_python_older_than_3_11():
    with pytest.raises(SystemExit, match="3.11"):
        deployment.require_supported_python((3, 10, 12, "final", 0))
    deployment.require_supported_python((3, 11, 0, "final", 0))
    deployment.require_supported_python((3, 14, 3, "final", 0))
