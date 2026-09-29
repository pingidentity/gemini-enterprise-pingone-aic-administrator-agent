"""Deploy the PingAIC agent to the customer's Agent Runtime with A2A + A2UI.

This is the primary deployment path. Agent Runtime (formerly Agent Engine)
runs ``ping_admin_agent.runtime.PingAdminA2aAgent``. The deployer forwards
only PingAIC configuration and Secret Manager *resource names* to the managed
runtime; it never forwards the private JWK or the resolved PingAIC
service-account ID.

Usage::

    gcloud auth application-default login
    python -m deployment.deploy --create
    python -m deployment.deploy --update RESOURCE_NAME
    python -m deployment.deploy --list
    python -m deployment.deploy --info RESOURCE_NAME
    python -m deployment.deploy --agent-card RESOURCE_NAME
    python -m deployment.deploy --delete RESOURCE_NAME

Required settings for create/update are ``GOOGLE_CLOUD_PROJECT`` (or the
active gcloud project), ``STAGING_BUCKET``, ``PING_BASE_URL``, the service-account
and audit Secret Manager resource-name pairs, and (recommended)
``PING_ADMIN_SERVICE_ACCOUNT`` to select the Agent Engine runtime identity. The
runtime identity needs ``roles/secretmanager.secretAccessor`` on exactly those
four secrets.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
from contextlib import chdir
from pathlib import Path
from typing import Any

import google.auth
import vertexai
from a2a.compat.v0_3.types import AgentCard
from dotenv import load_dotenv
from google.auth.transport.requests import AuthorizedSession
from vertexai import agent_engines

from ping_admin_agent.a2a import (
    CLOUD_PLATFORM_SCOPE,
    DESCRIPTION,
    DISPLAY_NAME,
    requires_access_token,
)
from ping_admin_agent.a2ui import EXTENSION_URI
from ping_admin_agent.runtime import PingAdminA2aAgent

from .registration import write_registration_bundle

# These are the only application variables allowed into the Agent Engine
# runtime. In particular, the plaintext auth variables are intentionally absent.
RUNTIME_ENV_KEYS = [
    "PING_BASE_URL",
    "PING_REALM",
    "PING_SCOPES",
    "PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET",
    "PING_ADMIN_PRIVATE_JWK_SECRET",
    "PING_ADMIN_AUDIT_API_KEY_SECRET",
    "PING_ADMIN_AUDIT_API_SECRET_SECRET",
    "PING_ADMIN_AUDIT_USERNAME_FIELD",
    "PING_TOKEN_URL",
    "PING_ADMIN_MODEL",
    "PING_ADMIN_MODEL_LOCATION",
    "PING_ADMIN_LOGO_URL",
    "PING_ADMIN_CANVAS_LOGO",
    "PING_ADMIN_A2UI_ENABLED",
    "PING_ADMIN_A2UI_PANEL",
    "PING_ADMIN_A2UI_SURFACE",
    "PING_ADMIN_STATE_STORE",
    "PING_ADMIN_STATE_DIR",
    "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
# One tested dependency set for local checks, containers, and managed builds.
REQUIREMENTS = [
    line.strip()
    for line in (REPO_ROOT / "requirements.txt").read_text().splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]


def _gcloud_config(prop: str) -> str:
    """Read a gcloud config value, returning an empty string when unavailable."""
    if not shutil.which("gcloud"):
        return ""
    try:
        result = subprocess.run(
            ["gcloud", "config", "get-value", prop],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    value = result.stdout.strip()
    return "" if value in ("", "(unset)") else value


def _resolve_project() -> str:
    return os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip() or _gcloud_config(
        "project"
    )


def _resolve_location() -> str:
    return (
        os.environ.get("GOOGLE_CLOUD_LOCATION", "").strip()
        or _gcloud_config("compute/region")
        or "us-central1"
    )


def _runtime_service_account() -> str:
    """Return the Agent Engine runtime identity, if one was explicitly set."""
    return os.environ.get("PING_ADMIN_SERVICE_ACCOUNT", "").strip()


def _init(
    *, require_staging_bucket: bool = True, resource_name: str | None = None
) -> None:
    """Initialize Vertex AI without creating or changing any cloud resources."""
    project = _resolve_project()
    location = _resolve_location()
    if resource_name:
        runtime_url(resource_name)
        project, location = resource_name.split("/")[1:4:2]
    staging_bucket = os.environ.get("STAGING_BUCKET", "").strip()
    missing: list[str] = []
    if not project:
        missing.append("GOOGLE_CLOUD_PROJECT (or `gcloud config set project ...`)")
    if require_staging_bucket and not staging_bucket:
        missing.append("STAGING_BUCKET")
    if missing:
        raise SystemExit(f"Missing required setting(s): {missing}.")
    vertexai.init(
        project=project,
        location=location,
        **({"staging_bucket": staging_bucket} if staging_bucket else {}),
    )


def _runtime_env() -> dict[str, str]:
    """Validate and return only safe application variables for Agent Engine.

    Secret Manager refs are values such as
    ``projects/p/secrets/name/versions/latest``. Agent Engine receives those
    strings and ``ping_admin_agent.config`` resolves their payloads at runtime
    using ADC. Plaintext values are rejected rather than silently ignored so a
    mistaken deploy cannot publish a private JWK in environment metadata.
    """
    plaintext_keys = {
        "PING_ADMIN_SERVICE_ACCOUNT_ID",
        "PING_ADMIN_PRIVATE_JWK",
        "PING_ADMIN_AUDIT_API_KEY",
        "PING_ADMIN_AUDIT_API_SECRET",
    }
    present_plaintext = sorted(
        k for k in plaintext_keys if os.environ.get(k, "").strip()
    )
    if present_plaintext:
        raise SystemExit(
            "Refusing Agent Engine deployment with plaintext PingAIC auth values: "
            f"{present_plaintext}. Use Secret Manager resource-name variables only."
        )

    env = {key: os.environ[key] for key in RUNTIME_ENV_KEYS if os.environ.get(key)}
    required = {
        "PING_BASE_URL",
        "PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET",
        "PING_ADMIN_PRIVATE_JWK_SECRET",
        "PING_ADMIN_AUDIT_API_KEY_SECRET",
        "PING_ADMIN_AUDIT_API_SECRET_SECRET",
    }
    missing = sorted(required - env.keys())
    if missing:
        raise SystemExit(f"Missing required runtime env: {missing}")
    for key in required:
        if key.endswith("_SECRET") and not re.fullmatch(
            r"projects/[a-zA-Z0-9_-]+/secrets/[a-zA-Z0-9_-]+/versions/(?:latest|[0-9]+)",
            env[key],
        ):
            raise SystemExit(
                f"{key} must be a full Secret Manager version resource name"
            )
    # Agent Engine's console Settings/Observability panel only becomes
    # editable when telemetry enablement is signaled through this env var
    # rather than through the legacy AdkApp(enable_tracing=...) parameter.
    # Deployments made via the API (as opposed to the Cloud Console) must
    # set it explicitly, or the console reports observability as unavailable.
    env.setdefault("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "true")
    # Agent Runtime serves an instance from several worker processes; the UI
    # controls, approvals and A2A tasks must be shared between them.
    env.setdefault("PING_ADMIN_STATE_STORE", "sqlite")
    if env["PING_ADMIN_STATE_STORE"] != "sqlite":
        raise SystemExit("Managed deployments require PING_ADMIN_STATE_STORE=sqlite")
    return env


def _mask_resource(value: str) -> str:
    """Mask a value in the confirmation display while retaining its shape."""
    if not value:
        return "<unset>"
    if len(value) <= 8:
        return "****"
    return f"{value[:6]}...{value[-6:]}"


def _confirm(action: str, resource: str | None = None) -> None:
    """Print a redacted plan and require explicit confirmation for mutations."""
    env = _runtime_env() if action in {"create", "update"} else {}
    project = _resolve_project()
    location = _resolve_location()
    if resource:
        runtime_url(resource)
        project, location = resource.split("/")[1:4:2]
    service_account = _runtime_service_account() or "<Vertex default runtime identity>"

    print("\n=== Agent Runtime A2A deployment plan ===")
    print(f"  action:                {action}")
    if resource:
        print(f"  resource:              {resource}")
    print(f"  GOOGLE_CLOUD_PROJECT:  {project or '<UNSET>'}")
    print(f"  GOOGLE_CLOUD_LOCATION: {location}")
    print(f"  STAGING_BUCKET:        {os.environ.get('STAGING_BUCKET', '<UNSET>')}")
    print(f"  runtime service acct:  {service_account}")
    print("  Gemini registration:  Custom agent via A2A + cloud-platform OAuth authorization")
    print("  operator access:      IdP Admins / Agent User + runtime IAM; one-time Google consent")
    print("  instances:            min=0, max=1 (UI/task state shared by the workers in SQLite)")
    print("\n  -- Runtime env forwarded to Agent Engine --")
    for key in RUNTIME_ENV_KEYS:
        value = env.get(key)
        if key.endswith("_SECRET") and value:
            value = _mask_resource(value)
        print(f"  {key + ':':<40} {value or '<unset>'}")
    print("====================================")
    try:
        answer = input(f"Proceed with {action}? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        raise SystemExit("Aborted, no changes made.")


def _wrapped_app():
    # Call only after _init: A2aAgent captures the configured project/location.
    # Never call set_up locally; the managed process creates its own executor.
    return PingAdminA2aAgent()


def _engine_kwargs() -> dict[str, Any]:
    """Return common Agent Engine package/runtime settings."""
    kwargs: dict[str, Any] = {
        "requirements": REQUIREMENTS,
        # SDK 2.1.0 preserves each supplied path inside dependencies.tar.gz.
        # Keep the package at the archive root; create/update anchor the cwd.
        "extra_packages": ["ping_admin_agent"],
        "env_vars": _runtime_env(),
        "min_instances": 0,
        "max_instances": 1,
    }
    service_account = _runtime_service_account()
    if service_account:
        kwargs["service_account"] = service_account
    return kwargs


def create(output_dir: Path = Path("deployment-output")) -> None:
    _init()
    _confirm("create")
    with chdir(REPO_ROOT):
        remote = agent_engines.create(
            _wrapped_app(),
            display_name=DISPLAY_NAME,
            description=DESCRIPTION,
            **_engine_kwargs(),
        )
    print("Created Agent Runtime A2A agent:")
    print(f"  resource_name: {remote.resource_name}")
    export_agent_card(remote.resource_name, output_dir)


def update(resource_name: str, output_dir: Path = Path("deployment-output")) -> None:
    _init(resource_name=resource_name)
    _confirm("update", resource_name)
    with chdir(REPO_ROOT):
        remote = agent_engines.update(
            resource_name=resource_name,
            agent_engine=_wrapped_app(),
            **_engine_kwargs(),
        )
    print(f"Updated: {remote.resource_name}")
    export_agent_card(remote.resource_name, output_dir)


def runtime_url(resource_name: str) -> str:
    """Build the documented API URL from a resource name, never an arbitrary URL."""
    match = re.fullmatch(
        r"projects/([a-zA-Z0-9_-]+)/locations/([a-z0-9-]+)/reasoningEngines/(\d+)",
        resource_name,
    )
    if not match:
        raise ValueError(
            "Expected projects/PROJECT/locations/REGION/reasoningEngines/ID"
        )
    return f"https://{match[2]}-aiplatform.googleapis.com/v1beta1/{resource_name}/a2a"


_PROJECT_SEGMENT = re.compile(r"/projects/[a-zA-Z0-9_-]+/")


def same_runtime_endpoint(card_url: str, expected_url: str) -> bool:
    """Compare host, API version, location, engine ID and the /a2a path.

    The managed process builds its card URL from the project as configured at
    packaging time, usually the project ID, while the API reports resource
    names with the project number. Either spelling names the same runtime.
    """

    def normalize(value: str) -> str:
        return _PROJECT_SEGMENT.sub("/projects/-/", value.rstrip("/"), count=1)

    return normalize(card_url) == normalize(expected_url)


def export_agent_card(
    resource_name: str, output_dir: Path = Path("deployment-output")
) -> None:
    """Fetch the running service's 0.3 card with ADC for manual GE registration.

    Do not use the legacy remote.handle_authenticated_agent_card SDK helper:
    its wrapper rejects streaming-enabled cards. Use the HTTP discovery API.
    """
    url = runtime_url(resource_name)
    try:
        credentials, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
        with AuthorizedSession(credentials) as session:
            response = session.get(f"{url}/v1/card", timeout=60)
            response.raise_for_status()
            card = AgentCard.model_validate(response.json())
        if card.protocol_version not in {"0.3", "0.3.0"}:
            raise ValueError("The deployed agent did not return an A2A 0.3 card")
        if card.preferred_transport != "HTTP+JSON" or not card.capabilities.streaming:
            raise ValueError(
                "The deployed agent must advertise HTTP+JSON and streaming"
            )
        if not same_runtime_endpoint(card.url, url):
            raise ValueError(
                "The deployed card does not point to this Agent Runtime resource"
            )
        if EXTENSION_URI not in {ext.uri for ext in card.capabilities.extensions or []}:
            raise ValueError("The deployed card does not advertise A2UI v0.9")
        card_json = card.model_dump(by_alias=True, exclude_none=True)
        if not requires_access_token(card_json):
            raise ValueError(
                "The deployed card does not require a Google OAuth2 access token"
            )
        if not card.icon_url:
            raise SystemExit(
                f"Agent Runtime resource: {resource_name}. Its live card has no "
                "branding icon. Update the runtime with this version before "
                "exporting a manual registration bundle."
            )
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        card_path = output_dir / "agent-card.json"
        bundle_path = write_registration_bundle(
            output_dir,
            card_json,
            {"resource_name": resource_name, "a2a_url": url},
        )
    except Exception as exc:
        raise SystemExit(
            f"Agent Runtime resource: {resource_name}. Card export failed "
            f"({type(exc).__name__}). The runtime was not deleted. Check ADC, "
            "IAM and runtime logs, then retry --agent-card with this resource name."
        ) from exc
    print(f"Agent card: {card_path.resolve()}")
    print(f"Manual registration bundle: {bundle_path.resolve()}")
    print("Set the Gemini Enterprise icon using gemini-enterprise-icon.json;")
    print("the official logo files and instructions are included in the bundle.")
    print("Every call needs a Google OAuth2 access token with the cloud-platform")
    print("scope; the card declares it and Agent Runtime rejects other requests.")
    print("Next: register in Gemini Enterprise as Custom agent via A2A with a")
    print("cloud-platform OAuth authorization attached (agentAuthorization), grant")
    print("the IdP Admins group Vertex AI User on the runtime project, then grant")
    print("Agent User to IdP Admins. Operators consent once. No Agent Gateway.")
    print("This deployer does not create the authorization, registration or permissions.")
    print("See README.md for setup and live access/A2UI verification.")


def _print_engine(engine: Any) -> None:
    name = getattr(engine, "resource_name", "")
    display_name = getattr(engine, "display_name", "")
    print(f"{name}\t{display_name}" if display_name else name)


def list_engines() -> None:
    _init(require_staging_bucket=False)
    for engine in agent_engines.list():
        _print_engine(engine)


def info(resource_name: str) -> None:
    _init(require_staging_bucket=False, resource_name=resource_name)
    engine = agent_engines.get(resource_name)
    print(f"resource_name: {getattr(engine, 'resource_name', resource_name)}")
    for field in ("display_name", "description", "create_time", "update_time"):
        value = getattr(engine, field, None)
        if value:
            print(f"{field}: {value}")


def delete(resource_name: str) -> None:
    _confirm("delete", resource_name)
    _init(require_staging_bucket=False, resource_name=resource_name)
    agent_engines.delete(resource_name=resource_name, force=True)
    print(f"Deleted: {resource_name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true", help="create a deployment")
    group.add_argument("--update", metavar="RESOURCE_NAME", help="update a deployment")
    group.add_argument("--list", action="store_true", help="list deployments")
    group.add_argument("--info", metavar="RESOURCE_NAME", help="show deployment info")
    group.add_argument("--delete", metavar="RESOURCE_NAME", help="delete a deployment")
    group.add_argument(
        "--agent-card", metavar="RESOURCE_NAME", help="export the live card and registration bundle"
    )
    parser.add_argument(
        "--env-file", type=Path, help="explicit deployment settings file"
    )
    parser.add_argument("--output-dir", type=Path, default=Path("deployment-output"))
    args = parser.parse_args()
    if args.env_file:
        if not args.env_file.is_file():
            parser.error(f"Environment file not found: {args.env_file}")
        load_dotenv(args.env_file, override=False)

    if args.create:
        create(args.output_dir)
    elif args.update:
        update(args.update, args.output_dir)
    elif args.list:
        list_engines()
    elif args.info:
        info(args.info)
    elif args.delete:
        delete(args.delete)
    elif args.agent_card:
        export_agent_card(args.agent_card, args.output_dir)


if __name__ == "__main__":
    main()
