"""Thin HTTP client for the PingAIC admin/help desk surface.

Talks to three tenant APIs:
  * IDM       (/openidm/managed/{realm}_user, group, role, assignment, application)
  * AM        (/am/json/realms/root/realms/{realm}/sessions/, users/{id}/devices)
  * monitoring (/monitoring/logs, audit)

One shared service-account JWT bearer token is cached and re-used until it
nears expiry. On a 401 the cached token is dropped so the next call refreshes
it.
"""

from __future__ import annotations

import json as jsonlib
import re
import threading
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from .auth import create_client_assertion
from .config import PingAdminConfig, get_config

# Exact response/request field names whose values must never be exposed. Keep
# this centralized so application response and HTTP-error redaction cannot drift.
_SECRET_KEYS = frozenset(
    {
        "client_secret",
        "clientSecret",
        "clientSecretValue",
        "userpassword",
        "password",
        "private",
        "privateKey",
        "private_key",
        "privateKeyMaterial",
        "privateCertificate",
        "certificate",
        "certificateChain",
        "cert",
        "idpPrivateId",
        "spPrivateId",
        "spPrivate",
        "secret",
        "secretValue",
        "secretReference",
        "signingCertificate",
        "encryptionCertificate",
        "signingKey",
        "encryptionKey",
    }
)


class PingError(RuntimeError):
    """Raised when a PingAIC API call fails or the token cannot be obtained."""

    # The HTTP status of the failed response, when there was one.
    status_code: int | None = None


# IDM returns only references from a relationship endpoint by default. These
# fields add the related object's own name and description beside each one:
# https://docs.pingidentity.com/pingoneaic/latest/idm-objects/roles-over-rest.html
RELATIONSHIP_FIELDS = "_ref/*,name,description"


def escape_crest_value(term: str) -> str:
    """Escape a value for use inside a double-quoted CREST _queryFilter.

    Keeps a caller-supplied search string from breaking the filter or turning
    into a wildcard. Applied everywhere `_queryFilter=... "<term>"` is built.
    """
    return term.replace("\\", "\\\\").replace('"', '\\"')


class PingClient:
    """Synchronous client. One instance is shared across tool calls."""

    def __init__(self, config: PingAdminConfig | None = None) -> None:
        self._config = config or get_config()
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=self._config.request_timeout_seconds)

    # -- token handling --------------------------------------------------

    def _fetch_token(self) -> tuple[str, float]:
        cfg = self._config
        try:
            assertion = create_client_assertion(
                cfg.service_account_id,
                cfg.private_jwk,
                cfg.token_url,
            )
            resp = self._http.post(
                cfg.token_url,
                data={
                    "client_id": "service-account",
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                    "scope": cfg.scopes,
                },
                auth=None,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise PingError(f"Token request failed: {exc}") from exc
        except ValueError as exc:
            raise PingError("Could not create service-account token assertion.") from exc

        if resp.status_code != 200:
            raise PingError(
                f"Token endpoint returned {resp.status_code}: {resp.text[:500]}"
            )
        try:
            payload = resp.json()
        except (TypeError, ValueError) as exc:
            raise PingError("Token endpoint returned an invalid JSON response.") from exc
        if not isinstance(payload, dict):
            raise PingError("Token endpoint returned an invalid JSON object.")
        token = payload.get("access_token")
        if not token:
            raise PingError(f"Token response missing access_token: {payload}")
        expires_in = float(payload.get("expires_in", 3600))
        return token, time.monotonic() + expires_in - cfg.token_leeway_seconds

    def _get_token(self) -> str:
        with self._lock:
            if self._token is None or time.monotonic() >= self._token_expiry:
                self._token, self._token_expiry = self._fetch_token()
            return self._token

    def _drop_token(self) -> None:
        with self._lock:
            self._token = None

    # -- generic request -------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        extra_headers: dict[str, str] | None = None,
        redact_values: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        try:
            resp = self._http.request(
                method, url, params=params, json=json, headers=headers
            )
        except httpx.HTTPError as exc:
            raise PingError(f"{method} {url} failed: {exc}") from exc

        if resp.status_code == 401:
            self._drop_token()
            if url.startswith(f"{self._config.monitoring_base}/logs"):
                raise PingError(
                    "Unauthorized (401) from the PingAIC monitoring API. "
                    "Verify PING_ADMIN_AUDIT_API_KEY and "
                    "PING_ADMIN_AUDIT_API_SECRET credentials."
                )
            raise PingError(
                f"Unauthorized (401) from {url}. Verify the PingAIC service "
                "account has the scopes and admin groups this operation needs."
            )
        if resp.status_code >= 400:
            error_text = self._redact_error_text(resp.text, redact_values)
            error = PingError(
                f"{method} {url} returned {resp.status_code}: {error_text}"
            )
            error.status_code = resp.status_code
            raise error
        if not resp.content:
            return {}
        try:
            payload = resp.json()
        except (TypeError, ValueError) as exc:
            raise PingError(
                f"{method} {url} returned invalid JSON in a successful response."
            ) from exc
        if not isinstance(payload, dict):
            raise PingError(
                f"{method} {url} returned an invalid JSON object in a successful response."
            )
        return payload

    @staticmethod
    def _redact_error_text(text: str, redact_values: tuple[str, ...] = ()) -> str:
        """Bound and redact submitted and server-generated secret-shaped errors."""
        error_text = text[:800]
        for secret in redact_values:
            if secret:
                error_text = error_text.replace(secret, "[REDACTED]")
        # Error bodies are commonly JSON, so redact values by their field names
        # before falling back to conservative PEM/token-shaped masking.
        try:
            parsed = jsonlib.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            redacted = PingClient._redact_application_response(parsed)
            try:
                error_text = jsonlib.dumps(redacted)[:800]
            except (TypeError, ValueError):
                pass
        error_text = re.sub(
            r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
            "[REDACTED PRIVATE KEY]",
            error_text,
            flags=re.DOTALL,
        )
        error_text = re.sub(
            r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
            "[REDACTED CERTIFICATE]",
            error_text,
            flags=re.DOTALL,
        )
        return error_text

    # -- IDM helpers -----------------------------------------------------

    def _idm_url(self, relative_path: str) -> str:
        return f"{self._config.idm_base}/{relative_path.lstrip('/')}"

    def _managed_user_url(self, user_id: str = "") -> str:
        base = self._idm_url(self._config.managed_user_path)
        return f"{base}/{user_id}" if user_id else base

    # -- users -----------------------------------------------------------

    @staticmethod
    def _name_search_tokens(query: str) -> tuple[str, str] | None:
        """Return ``(givenName, sn)`` for an explicitly recognizable name.

        A plain two-token query is interpreted as ``First Last``. The comma
        form is intentionally explicit so ``Last, First`` is supported without
        guessing the order of arbitrary multiword input.
        """
        if "," in query:
            parts = [part.strip() for part in query.split(",")]
            if len(parts) == 2:
                surname, given_name = parts
                if (
                    len(surname.split()) == 1
                    and len(given_name.split()) == 1
                    and surname
                    and given_name
                ):
                    return given_name, surname
            return None

        parts = query.split()
        if len(parts) == 2:
            return parts[0], parts[1]
        return None

    @classmethod
    def _user_search_filter(cls, query: str) -> str:
        """Build a broad user filter plus an optional compound name clause."""
        normalized = query.strip()
        term = escape_crest_value(normalized)
        parts = [
            f'userName co "{term}"',
            f'mail co "{term}"',
            f'givenName co "{term}"',
            f'sn co "{term}"',
            f'displayName co "{term}"',
        ]
        name_tokens = cls._name_search_tokens(normalized)
        if name_tokens:
            given_name, surname = name_tokens
            parts.append(
                f"(givenName co \"{escape_crest_value(given_name)}\" and sn co \"{escape_crest_value(surname)}\")"
            )
        return " or ".join(parts) if term else "true"

    def search_users(
        self, query: str, page_size: int = 10, fields: str | None = None
    ) -> dict[str, Any]:
        """Search users by name, username, or email.

        Uses case-insensitive substring matching across common fields. A
        two-token query is also treated as ``First Last``; ``Last, First`` is
        supported when written with an explicit comma.
        """
        params: dict[str, Any] = {
            "_queryFilter": self._user_search_filter(query),
            "_pageSize": page_size,
        }
        if fields:
            params["_fields"] = fields
        return self._request("GET", self._managed_user_url(), params=params)

    def resolve_user_id(self, username: str) -> str:
        """Resolve one exact ``userName`` to its IDM ``_id``.

        The logout-by-user AM action requires the managed-user UUID, while the
        tool accepts a login username. This deliberately rejects zero, multiple,
        or malformed matches rather than selecting a result from a broad search.
        """
        term = escape_crest_value(username)
        payload = self._request(
            "GET",
            self._managed_user_url(),
            params={
                "_queryFilter": f'userName eq "{term}"',
                "_pageSize": 10,
                "_fields": "_id,userName",
            },
        )
        matches = payload.get("result", [])
        if not isinstance(matches, list):
            raise PingError("User lookup returned an invalid result list.")
        exact_matches = [
            user
            for user in matches
            if isinstance(user, dict) and user.get("userName") == username
        ]
        if not exact_matches:
            raise PingError(f"No user found with exact username {username!r}.")
        if len(exact_matches) > 1:
            raise PingError(f"Multiple users found with exact username {username!r}.")
        user_id = exact_matches[0].get("_id")
        if not isinstance(user_id, str) or not user_id.strip():
            raise PingError(f"Exact username {username!r} is missing an IDM _id.")
        try:
            return str(uuid.UUID(user_id.strip()))
        except ValueError as exc:
            raise PingError(
                f"Exact username {username!r} has an invalid IDM _id."
            ) from exc

    def get_user(self, user_id: str, fields: str | None = None) -> dict[str, Any]:
        params = {"_fields": fields} if fields else None
        return self._request("GET", self._managed_user_url(user_id), params=params)

    def patch_user(
        self, user_id: str, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Send an IDM JSON-Patch to /managed/{realm}_user/{id}.

        `operations` is a list of ForgeRock-flavored patch ops, e.g.
            [{"operation": "replace", "field": "/accountStatus", "value": "inactive"}]
        """
        return self._request(
            "PATCH",
            self._managed_user_url(user_id),
            json=operations,
            extra_headers={"Content-Type": "application/json"},
        )

    def list_user_relationship(
        self, user_id: str, relationship: str, page_size: int = 50
    ) -> dict[str, Any]:
        """List a relationship collection on a user (groups, authzRoles, assignments).

        Each record carries the related object's ``name`` and ``description``
        beside its reference, so callers can show "Help desk" instead of an ID.
        """
        url = f"{self._managed_user_url(user_id)}/{relationship.lstrip('/')}"
        params = {"_queryFilter": "true", "_pageSize": page_size}
        try:
            return self._request(
                "GET", url, params={**params, "_fields": RELATIONSHIP_FIELDS}
            )
        except PingError as exc:
            # A tenant that rejects the field list still gets its references.
            if exc.status_code != 400:
                raise
        return self._request("GET", url, params=params)

    def delete_user_relationship(
        self, user_id: str, relationship: str, reference_id: str
    ) -> dict[str, Any]:
        """Remove one relationship from a user by its reference ID.

        Ping documents two ways to remove a relationship: a PATCH whose value
        repeats the entire object, reference properties and revision included,
        or this DELETE by the ID of the relationship itself. That ID is the
        ``_id`` of a record from ``list_user_relationship``, not the ID of the
        group or role it points to.
        https://docs.pingidentity.com/pingoneaic/latest/idm-objects/roles-over-rest.html
        """
        reference_id = reference_id.strip()
        if not reference_id or any(c in reference_id for c in "/\\?#%"):
            raise PingError("A relationship reference ID is required.")
        url = (
            f"{self._managed_user_url(user_id)}/{relationship.lstrip('/')}"
            f"/{reference_id}"
        )
        return self._request("DELETE", url)

    # -- groups ----------------------------------------------------------

    def list_groups(self, name_filter: str = "", page_size: int = 200) -> dict[str, Any]:
        """List groups in name order, optionally those whose name contains a term."""
        term = name_filter.strip()
        return self._request(
            "GET",
            self._idm_url(self._config.managed_group_path),
            params={
                "_queryFilter": f'name co "{escape_crest_value(term)}"' if term else "true",
                "_fields": "_id,name,description",
                "_pageSize": page_size,
                "_sortKeys": "name",
            },
        )

    def list_internal_roles(
        self, name_filter: str = "", page_size: int = 200
    ) -> dict[str, Any]:
        """List authorization (internal) roles in name order.

        A user's authzRoles reference these as ``internal/role/<id>``.
        https://docs.pingidentity.com/pingoneaic/idm-auth/authorization-and-roles.html
        """
        term = name_filter.strip()
        return self._request(
            "GET",
            self._idm_url("internal/role"),
            params={
                "_queryFilter": f'name co "{escape_crest_value(term)}"' if term else "true",
                "_fields": "_id,name,description",
                "_pageSize": page_size,
                "_sortKeys": "name",
            },
        )

    def list_group_members(
        self, group_id: str, page_size: int = 50
    ) -> dict[str, Any]:
        url = f"{self._idm_url(self._config.managed_group_path)}/{group_id}/members"
        return self._request(
            "GET", url, params={"_queryFilter": "true", "_pageSize": page_size}
        )

    # -- applications / entitlements -------------------------------------

    @staticmethod
    def _redact_application_response(value: Any) -> Any:
        """Remove credential- and key-shaped fields before returning an app response.

        Managed application responses can nest SAML configuration several levels
        deep.  Keep the redaction recursive and cover the observed private-ID
        aliases as well as the existing OAuth credential names.  Exact key
        matching is intentional: fields such as the observed ``ssoEntities.key``
        are identifiers, not secrets, and must remain available to callers.
        """
        if isinstance(value, dict):
            return {
                key: "[REDACTED]"
                if key in _SECRET_KEYS
                else PingClient._redact_application_response(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [PingClient._redact_application_response(item) for item in value]
        return value

    def create_saml_application(
        self,
        name: str,
        description: str,
        icon: str,
        authoritative: bool,
        template_name: str,
        template_version: str,
        sso_entities: dict[str, Any],
        app_type_specific_config: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a managed SAML application record.

        This is deliberately separate from :meth:`create_oidc_application`.
        The payload follows the observed Alpha tenant shape and is sent to the
        documented managed-application create operation.  Validation and the
        confirmation gate belong at the agent-tool boundary; this client method
        only maps the explicit Python arguments to their API field names.
        """
        payload = {
            "name": name,
            "description": description,
            "icon": icon,
            "authoritative": authoritative,
            "templateName": template_name,
            "templateVersion": template_version,
            "ssoEntities": dict(sso_entities),
            "appTypeSpecificConfig": dict(app_type_specific_config),
        }
        result = self._request(
            "POST",
            self._idm_url(self._config.managed_application_path),
            json=payload,
            extra_headers={"Accept-API-Version": "resource=1.0"},
            redact_values=tuple(
                value
                for value in self._redaction_values(payload)
                if value
            ),
        )
        return self._redact_application_response(result)

    @staticmethod
    def _redaction_values(value: Any) -> list[str]:
        """Collect string values from secret-shaped request fields for errors."""
        def collect(item: Any) -> list[str]:
            if isinstance(item, dict):
                values: list[str] = []
                for key, nested in item.items():
                    if key in _SECRET_KEYS and isinstance(nested, str):
                        values.append(nested)
                    else:
                        values.extend(collect(nested))
                return values
            if isinstance(item, list):
                values: list[str] = []
                for nested in item:
                    values.extend(collect(nested))
                return values
            return []

        return collect(value)

    def create_oidc_application(
        self,
        client_id: str,
        name: str,
        app_type: str,
        redirect_uris: list[str],
        scopes: list[str],
        grant_types: list[str],
        response_types: list[str],
        token_endpoint_auth_method: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Create an OAuth/OIDC client through the native AM admin endpoint.

        This is intentionally separate from the undocumented Application
        Management ``alpha_application`` object. The endpoint's nested payload
        shape is documented by PingOne Advanced Identity Cloud.
        """
        url = (
            f"{self._config.am_realm_base}/realm-config/agents/OAuth2Client/"
            f"{quote(client_id, safe='')}"
        )
        client_type = "Public" if app_type in {"native", "spa"} else "Confidential"
        core: dict[str, Any] = {
            "clientName": {"inherited": False, "value": [name]},
            "clientType": {"inherited": False, "value": client_type},
            "redirectionUris": {"inherited": False, "value": redirect_uris},
            "scopes": {"inherited": False, "value": scopes},
        }
        if description:
            core["description"] = {"inherited": False, "value": description}
        payload = {
            "coreOAuth2ClientConfig": core,
            "advancedOAuth2ClientConfig": {
                "grantTypes": {"inherited": False, "value": grant_types},
                "responseTypes": {"inherited": False, "value": response_types},
                "tokenEndpointAuthMethod": {
                    "inherited": False,
                    "value": token_endpoint_auth_method,
                },
            },
        }
        result = self._request(
            "PUT",
            url,
            json=payload,
            extra_headers={"Accept-API-Version": "resource=1.0"},
        )
        return self._redact_application_response(result)

    def find_application(
        self, query: str, page_size: int = 10
    ) -> dict[str, Any]:
        term = escape_crest_value(query)
        filter_expr = f'name co "{term}"' if term else "true"
        return self._request(
            "GET",
            self._idm_url(self._config.managed_application_path),
            params={"_queryFilter": filter_expr, "_pageSize": page_size},
        )

    # -- AM: sessions ----------------------------------------------------

    def _am_session_headers(self) -> dict[str, str]:
        # Existing AM device endpoints use this resource version.
        return {"Accept-API-Version": "resource=4.0, protocol=1.0"}

    def _am_logout_headers(self) -> dict[str, str]:
        return {"Accept-API-Version": "resource=5.1, protocol=1.0"}

    def logout_user_sessions(self, user_id: str) -> dict[str, Any]:
        """Invalidate every active AM session owned by an IDM user UUID."""
        try:
            user_id = str(uuid.UUID(user_id.strip()))
        except (AttributeError, ValueError) as exc:
            raise PingError("logout_user_sessions requires an IDM user UUID.") from exc
        url = f"{self._config.am_realm_base}/sessions/"
        return self._request(
            "POST",
            url,
            params={"_action": "logoutByUser"},
            json={"username": user_id},
            extra_headers=self._am_logout_headers(),
        )

    # What a caller may see of a session. A session handle can end that
    # session, so it never leaves this client; neither does the internal DN.
    _SESSION_FIELDS = (
        "latestAccessTime",
        "maxIdleExpirationTime",
        "maxSessionExpirationTime",
        "realm",
    )

    def list_user_sessions(self, user_id: str) -> dict[str, Any]:
        """List the active server-side AM sessions owned by an IDM user UUID.

        AM keys a session by the user's UUID, not the login name, and its query
        needs the realm too. Client-side sessions cannot be listed.
        https://docs.pingidentity.com/pingoneaic/am-sessions/managing-sessions-REST.html
        """
        try:
            user_id = str(uuid.UUID(user_id.strip()))
        except (AttributeError, ValueError) as exc:
            raise PingError("list_user_sessions requires an IDM user UUID.") from exc
        payload = self._request(
            "GET",
            f"{self._config.am_realm_base}/sessions",
            params={
                "_queryFilter": (
                    f'username eq "{user_id}" and realm eq "/{self._config.realm}"'
                )
            },
            extra_headers=self._am_session_headers(),
        )
        found = payload.get("result")
        sessions = [
            {key: row.get(key) for key in self._SESSION_FIELDS}
            for row in (found if isinstance(found, list) else [])
            if isinstance(row, dict)
        ]
        return {"result": sessions, "resultCount": len(sessions)}

    # -- AM: MFA devices -------------------------------------------------

    _MFA_DEVICE_TYPES = ("push", "oath", "webauthn")

    def list_user_devices(
        self, user_id: str, method: str
    ) -> dict[str, Any]:
        """List enrolled 2FA devices of the given type for a user.

        method: one of "push", "oath", "webauthn".
        """
        if method not in self._MFA_DEVICE_TYPES:
            raise PingError(
                f"method must be one of {self._MFA_DEVICE_TYPES}, got {method!r}"
            )
        url = f"{self._config.am_realm_base}/users/{user_id}/devices/2fa/{method}"
        return self._request(
            "GET",
            url,
            params={"_queryFilter": "true"},
            extra_headers=self._am_session_headers(),
        )

    def delete_user_device(
        self, user_id: str, method: str, uuid: str
    ) -> dict[str, Any]:
        if method not in self._MFA_DEVICE_TYPES:
            raise PingError(
                f"method must be one of {self._MFA_DEVICE_TYPES}, got {method!r}"
            )
        url = (
            f"{self._config.am_realm_base}/users/{user_id}/devices/2fa/"
            f"{method}/{uuid}"
        )
        return self._request(
            "DELETE", url, extra_headers=self._am_session_headers()
        )

    # -- monitoring / audit ---------------------------------------------

    def _audit_headers(self) -> dict[str, str]:
        audit_key = self._config.audit_api_key
        audit_secret = self._config.audit_api_secret
        if not audit_key or not audit_secret:
            raise PingError(
                "Audit API credentials are not configured for the monitoring API."
            )
        return {"x-api-key": audit_key, "x-api-secret": audit_secret}

    def _audit_user_filter(self, username: str) -> str:
        field = self._config.audit_username_field
        if not field:
            raise PingError(
                "Audit username filtering is not configured. Set "
                "PING_ADMIN_AUDIT_USERNAME_FIELD to the documented audit "
                "payload path for this tenant."
            )
        return f'{field} co "{escape_crest_value(username)}"'

    def get_audit_events(
        self,
        source: str,
        username: str | None = None,
        page_size: int = 50,
        begin_time: str | None = None,
    ) -> dict[str, Any]:
        """Read audit events from /monitoring/logs.

        source: "am-authentication", "am-access", "am-activity", "idm-access",
                or another documented source.
        """
        params: dict[str, Any] = {"source": source, "_pageSize": page_size}
        if username:
            params["_queryFilter"] = self._audit_user_filter(username)
        if begin_time:
            params["beginTime"] = begin_time
        return self._request(
            "GET",
            f"{self._config.monitoring_base}/logs",
            params=params,
            extra_headers=self._audit_headers(),
        )

    def tail_audit_events(
        self, source: str, username: str | None = None, cookie: str | None = None
    ) -> dict[str, Any]:
        """Follow a log source through /monitoring/logs/tail.

        The first call returns the last 15 seconds. Passing the previous
        response's ``pagedResultsCookie`` continues from its last entry, which
        is returned again, so callers de-duplicate.
        https://docs.pingidentity.com/pingoneaic/tenants/audit-debug-logs-pull.html
        """
        params: dict[str, Any] = {"source": source}
        if username:
            params["_queryFilter"] = self._audit_user_filter(username)
        if cookie:
            params["_pagedResultsCookie"] = cookie
        return self._request(
            "GET",
            f"{self._config.monitoring_base}/logs/tail",
            params=params,
            extra_headers=self._audit_headers(),
        )

    def close(self) -> None:
        self._http.close()


_client: PingClient | None = None
_client_lock = threading.Lock()


def get_client() -> PingClient:
    """Return a lazily-initialized shared client."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = PingClient()
    return _client
