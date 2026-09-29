"""PingAIC admin/help desk ADK agent.

Exposes 15 help-desk tools against a PingOne Advanced Identity Cloud tenant.
The agent runs in service mode: every API call is attributed to a single
PingAIC service admin identity. Human attribution lives in Gemini Enterprise
access logs, not PingAIC audit.

Destructive tools (password reset, session kill, status change, MFA unenroll,
group membership changes, assignment grants/revokes, OIDC and SAML application creation)
use ADK confirmation in the agent. The underlying Python functions retain a
confirm=True guard for direct callers; the model cannot set that flag itself.
"""

from __future__ import annotations

import os
import secrets
import string
from typing import Any
from urllib.parse import urlparse

from google.adk.agents import Agent
from google.adk.models.google_llm import Gemini

from .confirmed_tool import ConfirmedFunctionTool
from .ping_client import PingError, get_client


def _build_model() -> Gemini:
    # Model endpoints and Agent Runtime/Sessions locations are independent.
    # Gemini 3.5 Flash is not served from the default runtime region us-central1.
    return Gemini(
        model=os.environ.get("PING_ADMIN_MODEL", "").strip() or "gemini-3.5-flash",
        client_kwargs={
            "location": os.environ.get("PING_ADMIN_MODEL_LOCATION", "").strip()
            or "global",
        },
    )


USER_STATUSES = ("active", "inactive")
MFA_METHODS = ("push", "oath", "webauthn")
MEMBERSHIP_KINDS = ("groups", "authzRoles")
AUDIT_SOURCES = ("am-authentication", "am-access", "am-activity", "idm-access")
SAML_TEMPLATE_NAME = "saml"
SAML_TEMPLATE_VERSION = "1.1.0"
SAML_SSO_ENTITY_KEYS = frozenset(
    {
        "key",
        "idpLocation",
        "idpPrivateId",
        "domain",
        "spLocation",
        "spPrivateId",
        "idpLoginUrl",
    }
)
SAML_APP_CONFIG_KEYS = frozenset(
    {"appLocation", "appLocationNoHttps", "cotName", "roles", "tenant"}
)


def _ok(data: Any) -> dict[str, Any]:
    return {"status": "success", "data": data}


def _err(message: str) -> dict[str, Any]:
    return {"status": "error", "message": message}


def _require_confirm(action: str, confirm: bool) -> dict[str, Any] | None:
    if confirm is True:
        return None
    return _err(
        f"Refusing to {action} without an explicit confirmation. Restate the "
        "specific target and action to the user, get their confirmation, then "
        "call this tool again with confirm=true."
    )


def _clean_string_list(values: Any, field: str) -> tuple[list[str] | None, str | None]:
    if not isinstance(values, list) or not values:
        return None, f"{field} must contain at least one value."
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return None, f"{field} must contain only nonblank strings."
    return [value.strip() for value in values], None


def _validate_redirect_uris(values: list[str], app_type: str) -> str | None:
    if app_type in {"web", "spa"} and not values:
        return "redirect_uris must contain at least one URL."
    for value in values:
        parsed = urlparse(value)
        if parsed.fragment or not parsed.scheme:
            return "redirect_uris must contain absolute URLs without fragments."
        if app_type in {"web", "spa"}:
            if parsed.scheme != "https" or not parsed.netloc:
                return "web and spa redirect_uris must be absolute HTTPS URLs without fragments."
        elif app_type == "native" and parsed.scheme in {"http", "https", "javascript", "data"}:
            # Native clients commonly use private-use schemes such as
            # com.example.app:/callback; they must not be browser URLs or
            # executable/data schemes.
            return "native redirect_uris must use a non-web custom scheme."
    return None


def _validate_oidc_inputs(
    name: str,
    app_type: str,
    redirect_uris: list[str],
    scopes: list[str],
    grant_types: list[str],
    response_types: list[str],
    token_endpoint_auth_method: str,
) -> str | None:
    if not name.strip():
        return "name is required."
    if not isinstance(app_type, str) or app_type not in {"native", "spa", "web", "service"}:
        return "app_type must be one of native, spa, web, service."
    if "openid" not in scopes:
        return "scopes must include 'openid'."
    if not grant_types or not response_types:
        return "grant_types and response_types are required."
    if app_type in {"native", "spa", "web"}:
        error = _validate_redirect_uris(redirect_uris, app_type)
        if error:
            return error
    elif redirect_uris:
        return "service applications must not specify redirect_uris."
    if app_type in {"native", "spa"} and token_endpoint_auth_method != "none":
        return f"{app_type} applications must use token_endpoint_auth_method='none'."
    if app_type == "service" and "client_credentials" not in grant_types:
        return "service applications must include the client_credentials grant."
    return None


def _validate_saml_object(
    value: Any, field: str, expected_keys: frozenset[str]
) -> tuple[dict[str, str] | None, str | None]:
    """Validate an explicitly supplied object against the observed key shape."""
    if not isinstance(value, dict):
        return None, f"{field} must be an object."
    actual_keys = set(value)
    missing = expected_keys - actual_keys
    unknown = actual_keys - expected_keys
    if missing:
        return None, f"{field} is missing required keys: {', '.join(sorted(missing))}."
    if unknown:
        return None, f"{field} contains unknown keys: {', '.join(sorted(unknown))}."
    if any(not isinstance(item, str) or not item.strip() for item in value.values()):
        return None, f"{field} must contain only nonblank strings."
    return {key: value[key].strip() for key in sorted(expected_keys)}, None


_SAML_PRIVATE_KEYS = frozenset(
    {"idpPrivateId", "spPrivateId", "spPrivate", "privateKey", "private_key", "secret"}
)


def _redact_saml_confirmation_values(values: dict[str, str]) -> dict[str, str]:
    return {
        key: "[REDACTED]" if key in _SAML_PRIVATE_KEYS else value
        for key, value in values.items()
    }


def create_saml_application(
    name: str,
    description: str,
    icon: str,
    authoritative: bool,
    template_name: str,
    template_version: str,
    sso_entities: dict[str, Any],
    app_type_specific_config: dict[str, Any],
    confirm: bool = False,
) -> dict[str, Any]:
    """Create an observed managed SAML application record (requires confirmation)."""
    scalar_values = {
        "name": name,
        "description": description,
        "icon": icon,
        "template_name": template_name,
        "template_version": template_version,
    }
    for field, value in scalar_values.items():
        if not isinstance(value, str) or not value.strip():
            return _err(f"{field} must be a nonblank string.")
    if template_name != SAML_TEMPLATE_NAME:
        return _err(f"template_name must be exactly {SAML_TEMPLATE_NAME!r}.")
    if template_version != SAML_TEMPLATE_VERSION:
        return _err(f"template_version must be exactly {SAML_TEMPLATE_VERSION!r}.")
    if type(authoritative) is not bool:
        return _err("authoritative must be a boolean.")
    cleaned_sso, error = _validate_saml_object(
        sso_entities, "sso_entities", SAML_SSO_ENTITY_KEYS
    )
    if error:
        return _err(error)
    cleaned_config, error = _validate_saml_object(
        app_type_specific_config,
        "app_type_specific_config",
        SAML_APP_CONFIG_KEYS,
    )
    if error:
        return _err(error)
    refusal = _require_confirm(
        f"create SAML application {name.strip()!r} with template "
        f"{template_name!r}/{template_version!r}; SSO entities "
        f"{_redact_saml_confirmation_values(cleaned_sso)!r}; "
        f"application-specific config "
        f"{_redact_saml_confirmation_values(cleaned_config)!r}",
        confirm,
    )
    if refusal:
        return refusal
    try:
        return _ok(
            get_client().create_saml_application(
                name=name.strip(),
                description=description.strip(),
                icon=icon.strip(),
                authoritative=authoritative,
                template_name=template_name,
                template_version=template_version,
                sso_entities=cleaned_sso,
                app_type_specific_config=cleaned_config,
            )
        )
    except PingError as exc:
        return _err(str(exc))


def create_oidc_application(
    client_id: str,
    name: str,
    app_type: str,
    redirect_uris: list[str],
    scopes: list[str],
    grant_types: list[str],
    response_types: list[str],
    token_endpoint_auth_method: str,
    description: str = "",
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a native OIDC client (tenant mutation; requires confirmation)."""
    if not isinstance(client_id, str) or not client_id.strip():
        return _err("client_id is required.")
    if not isinstance(name, str) or not name.strip():
        return _err("name is required.")
    cleaned_lists = {}
    for field, raw in (("scopes", scopes), ("grant_types", grant_types), ("response_types", response_types)):
        cleaned, error = _clean_string_list(raw, field)
        if error:
            return _err(error)
        cleaned_lists[field] = cleaned
    if not isinstance(redirect_uris, list):
        return _err("redirect_uris must be a list.")
    if any(not isinstance(value, str) or not value.strip() for value in redirect_uris):
        return _err("redirect_uris must contain only nonblank strings.")
    values = {
        "client_id": client_id.strip(),
        "name": name.strip(),
        "description": description.strip() if isinstance(description, str) else "",
        "redirect_uris": [v.strip() for v in redirect_uris],
        "scopes": cleaned_lists["scopes"],
        "grant_types": cleaned_lists["grant_types"],
        "response_types": cleaned_lists["response_types"],
        "token_endpoint_auth_method": (
            token_endpoint_auth_method.strip()
            if isinstance(token_endpoint_auth_method, str)
            else token_endpoint_auth_method
        ),
    }
    if not isinstance(values["token_endpoint_auth_method"], str):
        return _err("token_endpoint_auth_method must be a string.")
    error = _validate_oidc_inputs(
        values["name"], app_type, values["redirect_uris"], values["scopes"],
        values["grant_types"], values["response_types"],
        values["token_endpoint_auth_method"],
    )
    if error:
        return _err(error)
    refusal = _require_confirm(
        f"create OIDC application {values['name']!r} (client id {values['client_id']!r}, "
        f"type {app_type!r}, redirects {values['redirect_uris']!r})", confirm
    )
    if refusal:
        return refusal
    try:
        return _ok(get_client().create_oidc_application(app_type=app_type, **values))
    except PingError as exc:
        return _err(str(exc))


def _generate_temp_password(length: int = 20) -> str:
    """A URL-safe temporary password meeting typical PingAIC complexity rules."""
    alphabet = string.ascii_letters + string.digits + "-_"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
        ):
            return candidate


# -----------------------------------------------------------------------------
# User account ops
# -----------------------------------------------------------------------------


def search_users(query: str, page_size: int = 10) -> dict[str, Any]:
    """Search users by name, userName, mail, or display name.

    Args:
        query: Text to match. Case-insensitive substring across the common
            fields. Two-part names support ``First Last`` and explicit
            ``Last, First`` input in addition to the broad field search.
            Empty is not allowed (would return every user).
        page_size: Maximum number of users to return (default 10).

    Returns:
        The matching users. Each result includes the user's `_id`, `userName`,
        `mail`, `givenName`, `sn`, and `accountStatus` where present.
    """
    if not query.strip():
        return _err("query is required; refusing to page every user in the tenant.")
    try:
        return _ok(
            get_client().search_users(
                query=query.strip(),
                page_size=page_size,
                fields="_id,userName,mail,givenName,sn,displayName,accountStatus",
            )
        )
    except PingError as exc:
        return _err(str(exc))


def get_user(user_id: str) -> dict[str, Any]:
    """Get a user's full profile by IDM `_id`.

    Args:
        user_id: The user's IDM `_id` (from search_users).

    Returns:
        The full managed-user object.
    """
    if not user_id.strip():
        return _err("user_id is required.")
    try:
        return _ok(get_client().get_user(user_id.strip()))
    except PingError as exc:
        return _err(str(exc))


def reset_user_password(
    user_id: str, confirm: bool = False, new_password: str = ""
) -> dict[str, Any]:
    """Set a user's password to a new value (destructive; requires confirm).

    If `new_password` is empty, a strong temporary password is generated and
    returned in the tool response so the help desk agent can share it with the
    user through an out-of-band channel.

    Args:
        user_id: The user's IDM `_id`.
        confirm: Must be True to proceed. Restate the target user and get
            explicit user confirmation before setting this.
        new_password: Optional. If empty, a temp password is generated.

    Returns:
        On success, the (masked) user object plus the new password so it can be
        conveyed to the end user by the help desk agent.
    """
    if not user_id.strip():
        return _err("user_id is required.")
    refusal = _require_confirm(f"reset password for user {user_id!r}", confirm)
    if refusal:
        return refusal
    password = new_password or _generate_temp_password()
    try:
        result = get_client().patch_user(
            user_id.strip(),
            [{"operation": "replace", "field": "/password", "value": password}],
        )
        return _ok({"user": result, "new_password": password})
    except PingError as exc:
        return _err(str(exc))


def set_user_status(
    user_id: str, status: str, confirm: bool = False
) -> dict[str, Any]:
    """Enable or disable a user (destructive; requires confirm).

    Args:
        user_id: The user's IDM `_id`.
        status: "active" or "inactive".
        confirm: Must be True to proceed.

    Returns:
        The updated user object.
    """
    if not user_id.strip():
        return _err("user_id is required.")
    if status not in USER_STATUSES:
        return _err(f"status must be one of {', '.join(USER_STATUSES)}.")
    refusal = _require_confirm(f"set user {user_id!r} to {status!r}", confirm)
    if refusal:
        return refusal
    try:
        return _ok(
            get_client().patch_user(
                user_id.strip(),
                [
                    {
                        "operation": "replace",
                        "field": "/accountStatus",
                        "value": status,
                    }
                ],
            )
        )
    except PingError as exc:
        return _err(str(exc))


def logout_user_sessions(
    username: str, confirm: bool = False, expected_user_id: str = ""
) -> dict[str, Any]:
    """Invalidate every active AM session for a login username.

    After confirmation, the username is resolved to exactly one managed-user
    UUID. Ambiguous, missing, or malformed IDM records are refused before AM
    is called.

    Args:
        username: The user's login `userName` (not IDM `_id`).
        confirm: Must be True to proceed.
    """
    if not username.strip():
        return _err("username is required.")
    refusal = _require_confirm(
        f"kill all sessions for user {username!r}", confirm
    )
    if refusal:
        return refusal
    username = username.strip()
    try:
        client = get_client()
        user_id = client.resolve_user_id(username)
        if expected_user_id and user_id != expected_user_id:
            return _err("The username now resolves to a different account. Search again before ending sessions.")
        return _ok(client.logout_user_sessions(user_id))
    except PingError as exc:
        return _err(str(exc))


def list_user_sessions(user_id: str) -> dict[str, Any]:
    """List a user's active sign-in sessions (read-only).

    Returns, per session, when it was last used (`latestAccessTime`), when it
    times out if idle (`maxIdleExpirationTime`) and when it ends regardless
    (`maxSessionExpirationTime`). Only server-side sessions can be listed, and
    sessions cannot be ended one at a time: use `logout_user_sessions`.

    Args:
        user_id: The user's IDM `_id` (a UUID), not the login name.
    """
    if not user_id.strip():
        return _err("user_id is required.")
    try:
        return _ok(get_client().list_user_sessions(user_id.strip()))
    except PingError as exc:
        return _err(str(exc))


def unenroll_mfa_device(
    user_id: str, method: str, confirm: bool = False
) -> dict[str, Any]:
    """Remove all MFA devices of the given type for a user (requires confirm).

    Lists the user's enrolled devices for `method` then deletes each. On success,
    returns the list of devices that were removed.

    Args:
        user_id: The user's IDM `_id`.
        method: "push", "oath", or "webauthn".
        confirm: Must be True to proceed.
    """
    if not user_id.strip():
        return _err("user_id is required.")
    if method not in MFA_METHODS:
        return _err(f"method must be one of {', '.join(MFA_METHODS)}.")
    refusal = _require_confirm(
        f"unenroll {method!r} MFA devices for user {user_id!r}", confirm
    )
    if refusal:
        return refusal
    try:
        client = get_client()
        listing = client.list_user_devices(user_id.strip(), method)
        removed: list[dict[str, Any]] = []
        for device in listing.get("result", []):
            uuid = device.get("uuid") or device.get("_id")
            if not uuid:
                continue
            client.delete_user_device(user_id.strip(), method, uuid)
            removed.append(device)
        return _ok({"removed": removed, "count": len(removed)})
    except PingError as exc:
        return _err(str(exc))


# -----------------------------------------------------------------------------
# Group & role membership
# -----------------------------------------------------------------------------


def list_user_groups(user_id: str, kind: str = "groups") -> dict[str, Any]:
    """List a user's group or authorization-role memberships.

    Each record has the group's or role's display `name` (and `description`
    when set) and its IDM `_id` in `_refResourceId`. Refer to records by name
    and use `_refResourceId` wherever an ID is required.

    Args:
        user_id: The user's IDM `_id`.
        kind: "groups" (default) or "authzRoles".
    """
    if not user_id.strip():
        return _err("user_id is required.")
    if kind not in MEMBERSHIP_KINDS:
        return _err(f"kind must be one of {', '.join(MEMBERSHIP_KINDS)}.")
    try:
        return _ok(get_client().list_user_relationship(user_id.strip(), kind))
    except PingError as exc:
        return _err(str(exc))


def list_group_members(group_id: str, page_size: int = 50) -> dict[str, Any]:
    """List the members of a group.

    Args:
        group_id: The group's IDM `_id`.
        page_size: Maximum number of members to return.
    """
    if not group_id.strip():
        return _err("group_id is required.")
    try:
        return _ok(
            get_client().list_group_members(group_id.strip(), page_size=page_size)
        )
    except PingError as exc:
        return _err(str(exc))


def _membership_ref(kind: str, membership_id: str) -> str | None:
    """The IDM reference for a group or authorization role, or None if unsafe.

    A bare ID means the usual collection: the realm's managed groups, or the
    internal roles that Ping documents for authzRoles. A full reference is
    accepted only under a collection that kind may point to.
    """
    from .config import get_config

    cfg = get_config()
    allowed = (
        (cfg.managed_group_path,)
        if kind == "groups"
        else ("internal/role", cfg.managed_role_path)
    )
    value = membership_id.strip().strip("/")
    collection, _, record = value.rpartition("/")
    if not record or any(c in record for c in "\\?#%") or record in {".", ".."}:
        return None
    if not collection:
        return f"{allowed[0]}/{record}"
    return value if collection in allowed else None


def change_user_membership(
    user_id: str,
    kind: str,
    membership_id: str,
    action: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Add or remove a group / authzRole membership (destructive; requires confirm).

    Args:
        user_id: The user's IDM `_id`.
        kind: "groups" or "authzRoles".
        membership_id: The IDM `_id` of the group or authorization role, as
            `_refResourceId` gives it in `list_user_groups`. An authorization
            role is an internal role unless a full reference says otherwise.
        action: "add" or "remove".
        confirm: Must be True to proceed.
    """
    if not user_id.strip():
        return _err("user_id is required.")
    if kind not in MEMBERSHIP_KINDS:
        return _err(f"kind must be one of {', '.join(MEMBERSHIP_KINDS)}.")
    if action not in ("add", "remove"):
        return _err("action must be 'add' or 'remove'.")
    if not membership_id.strip():
        return _err("membership_id is required.")
    reference = _membership_ref(kind, membership_id)
    if reference is None:
        return _err("membership_id must be an ID, or a reference this kind allows.")
    refusal = _require_confirm(
        f"{action} {kind} {membership_id!r} for user {user_id!r}", confirm
    )
    if refusal:
        return refusal
    try:
        client = get_client()
        if action == "add":
            # Ping's documented grant: append a reference to the relationship.
            op = {"operation": "add", "field": f"/{kind}/-", "value": {"_ref": reference}}
            return _ok(client.patch_user(user_id.strip(), [op]))
        # A PATCH remove must repeat the entire stored object, so remove the
        # relationship itself instead, by the reference ID IDM gave it.
        held = client.list_user_relationship(user_id.strip(), kind).get("result")
        matches = [
            row
            for row in (held if isinstance(held, list) else [])
            if isinstance(row, dict) and row.get("_ref") == reference and row.get("_id")
        ]
        if not matches:
            return _err("This user does not have that membership; nothing was removed.")
        return _ok(
            client.delete_user_relationship(user_id.strip(), kind, matches[0]["_id"])
        )
    except PingError as exc:
        return _err(str(exc))


# -----------------------------------------------------------------------------
# App assignments & entitlements
# -----------------------------------------------------------------------------


def list_user_assignments(user_id: str) -> dict[str, Any]:
    """List a user's direct entitlement assignments.

    Each record has the assignment's display `name` (and `description` when
    set) and its IDM `_id` in `_refResourceId`.

    Args:
        user_id: The user's IDM `_id`.
    """
    if not user_id.strip():
        return _err("user_id is required.")
    try:
        return _ok(
            get_client().list_user_relationship(user_id.strip(), "assignments")
        )
    except PingError as exc:
        return _err(str(exc))


def change_user_assignment(
    user_id: str,
    assignment_id: str,
    action: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Grant or revoke a direct assignment (destructive; requires confirm).

    Args:
        user_id: The user's IDM `_id`.
        assignment_id: The IDM `_id` of the managed assignment.
        action: "grant" or "revoke".
        confirm: Must be True to proceed.
    """
    if not user_id.strip():
        return _err("user_id is required.")
    if not assignment_id.strip():
        return _err("assignment_id is required.")
    if action not in ("grant", "revoke"):
        return _err("action must be 'grant' or 'revoke'.")
    refusal = _require_confirm(
        f"{action} assignment {assignment_id!r} for user {user_id!r}", confirm
    )
    if refusal:
        return refusal
    from .config import get_config

    cfg = get_config()
    patch_op = "add" if action == "grant" else "remove"
    op = {
        "operation": patch_op,
        "field": "/assignments/-" if action == "grant" else "/assignments",
        "value": {"_ref": f"{cfg.managed_assignment_path}/{assignment_id.strip()}"},
    }
    try:
        return _ok(get_client().patch_user(user_id.strip(), [op]))
    except PingError as exc:
        return _err(str(exc))


def find_application(query: str, page_size: int = 10) -> dict[str, Any]:
    """Find an application by name.

    Args:
        query: Case-insensitive substring on the application's `name`.
        page_size: Maximum number of results.
    """
    if not query.strip():
        return _err("query is required.")
    try:
        return _ok(
            get_client().find_application(
                query=query.strip(), page_size=page_size
            )
        )
    except PingError as exc:
        return _err(str(exc))


# -----------------------------------------------------------------------------
# Audit / troubleshoot
# -----------------------------------------------------------------------------


def get_user_activity(
    username: str,
    source: str = "am-authentication",
    page_size: int = 50,
    live: bool = False,
    cookie: str = "",
) -> dict[str, Any]:
    """Read audit events for a user.

    By default this looks back over the last 24 hours, which is what you want
    to explain something that already happened. With `live=True` it follows the
    log instead: the first call returns only the last 15 seconds, and each
    later call continues from the previous response's `pagedResultsCookie`,
    passed as `cookie`. Use live mode only while the user is retrying.

    Args:
        username: The user's `userName` to filter events by.
        source: One of "am-authentication", "am-access", "am-activity",
            "idm-access".
        page_size: Maximum number of events to return when looking back.
        live: Follow new events instead of looking back.
        cookie: In live mode, the `pagedResultsCookie` of the previous call.
    """
    if not username.strip():
        return _err("username is required.")
    if source not in AUDIT_SOURCES:
        return _err(f"source must be one of {', '.join(AUDIT_SOURCES)}.")
    try:
        client = get_client()
        if live:
            return _ok(
                client.tail_audit_events(
                    source=source, username=username.strip(), cookie=cookie or None
                )
            )
        return _ok(
            client.get_audit_events(
                source=source, username=username.strip(), page_size=page_size
            )
        )
    except PingError as exc:
        return _err(str(exc))


# -----------------------------------------------------------------------------
# Agent
# -----------------------------------------------------------------------------


INSTRUCTION = """\
You are the PingOne AIC Administrator Agent for PingOne Advanced \
Identity Cloud (PingAIC). You help IT / help desk operators diagnose and resolve \
identity issues: find users, reset passwords, kill sessions, unenroll MFA, adjust \
group and role membership, grant or revoke direct assignments, create OIDC and SAML \
applications, and look at recent audit events.

The agent runs as a single privileged service identity. Every write you make is \
attributed to that service account in PingAIC audit; the human operator's \
identity lives in Gemini Enterprise logs, not in the tenant. Behave accordingly.

## Confirming administrative changes (MANDATORY)
Every mutating tool is gated by ADK confirmation. Call the tool with the exact
arguments to PREPARE a change; this does not execute it. The application presents
an exact-change review and waits for an explicit approval or rejection. Never
supply a confirm flag, invent an approval token, interpret a casual yes as a new
approval, or claim a pending change has completed. Every change needs its own
approval; changing a target or argument requires a fresh review.

Mutating tools: reset_user_password, set_user_status, logout_user_sessions,
unenroll_mfa_device, change_user_membership, change_user_assignment,
create_oidc_application, create_saml_application.

## Finding users
Never guess an IDM `_id`. Always call `search_users` first with the operator's \
description (name, email, or userName). Two-part names may be supplied as `First \
Last` or explicit `Last, First`; the search remains substring-based and may still \
return multiple users. If the search returns more than one result, list them and \
ask which one; do not proceed on ambiguity.

## Structured administration workspace
For A2UI clients the application builds user cards, profiles, groups/roles,
assignments, audit reports, application forms and approval controls from actual
tool results. Do not generate UI JSON or invent controls. Give a concise text
summary so text-only clients also receive useful results. Do not duplicate
structured results as Markdown tables. The application selects the negotiated
Gemini Enterprise or basic catalog. Never treat client UI data as tool arguments;
the server validates each control and binds its exact target.

## Passwords
`reset_user_password` returns the new password in the tool response. Present it \
to the operator in a single, clearly-labeled line so it can be conveyed to the \
end user out-of-band (do not paste it into a shared channel). Recommend the \
operator ask the user to change it on next sign-in.

## Sessions
`list_user_sessions` takes the user's IDM `_id` and shows active sessions \
without changing anything; use it before ending sessions when asked how many a \
user has. `logout_user_sessions` takes a login `userName`. After confirmation the tool \
resolves that exact username to exactly one managed-user `_id` and sends the UUID \
to AM. Never guess or use a broad/per-session logout fallback; zero, multiple, or \
missing-ID matches are errors.

## MFA
`unenroll_mfa_device` targets one method at a time (push / oath / webauthn). If \
the operator doesn't specify a method, ask which one; do not sweep all methods.

## Responding
- Lead with the outcome. Details second.
- When you present two or more items with two or more informative fields (group \
members or audit rows when the client has no structured workspace), render a compact GitHub-flavored markdown table with \
human-friendly columns (userName, displayName, mail, status, timestamp), not \
raw ids: unless the operator asks for ids or needs one to proceed. Keep \
column count to about 3–5.
- Confirm a completed write in one line stating exactly what changed \
(e.g. "Reset password for user `alice.smith` (id `abc-123`); new password shown \
below.").
- Never invent ids, statuses, timestamps, or counts. Report only what tools \
returned. If a list is empty, say so plainly.

## Errors
- When a tool returns status "error", state the problem in plain language and \
suggest the next step. Do not retry the same call blindly. If the error \
mentions a missing group or unauthorized token, the tenant's service-client \
token-mod script likely needs an admin group added; flag that to the operator.
"""


root_agent = Agent(
    name="pingaic_admin_agent",
    model=_build_model(),
    description=(
        "User search, password reset, session/MFA management, group/role "
        "membership, direct assignments, OIDC and SAML application creation, "
        "and audit lookup."
    ),
    instruction=INSTRUCTION,
    tools=[
        search_users,
        get_user,
        ConfirmedFunctionTool(reset_user_password),
        ConfirmedFunctionTool(set_user_status),
        ConfirmedFunctionTool(logout_user_sessions),
        list_user_sessions,
        ConfirmedFunctionTool(unenroll_mfa_device),
        list_user_groups,
        list_group_members,
        ConfirmedFunctionTool(change_user_membership),
        list_user_assignments,
        ConfirmedFunctionTool(change_user_assignment),
        find_application,
        ConfirmedFunctionTool(create_oidc_application),
        ConfirmedFunctionTool(create_saml_application),
        get_user_activity,
    ],
)
