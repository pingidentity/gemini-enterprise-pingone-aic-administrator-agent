"""Unit tests for PingClient. HTTP is mocked; no network calls happen."""

from __future__ import annotations

import time

import httpx
import pytest

from ping_admin_agent import ping_client as ping_client_module
from ping_admin_agent.ping_client import PingClient, PingError, escape_crest_value


class _FakeResponse:
    def __init__(self, status_code: int, json_body: object | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text
        self.content = b"" if json_body is None and not text else b"x"

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeHttp:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.responses: list[_FakeResponse] = []

    def request(self, method, url, params=None, json=None, headers=None):
        self.calls.append(
            (
                method,
                url,
                {"params": params, "json": json, "headers": dict(headers or {})},
            )
        )
        if not self.responses:
            raise AssertionError(f"No queued response for {method} {url}")
        return self.responses.pop(0)

    def post(self, url, data=None, auth=None, headers=None):
        self.calls.append(
            (
                "POST",
                url,
                {"data": data, "auth": auth, "headers": dict(headers or {})},
            )
        )
        if not self.responses:
            raise AssertionError(f"No queued response for POST {url}")
        return self.responses.pop(0)

    def close(self):
        pass


@pytest.fixture
def client(stub_config):
    c = PingClient(config=stub_config)
    c._http = _FakeHttp()
    return c


def _queue(client, *responses):
    client._http.responses.extend(responses)


def test_escape_crest_value():
    assert escape_crest_value('he said "hi"') == 'he said \\"hi\\"'
    assert escape_crest_value("a\\b") == "a\\\\b"


def test_token_request_uses_documented_jwt_bearer_grant(client, monkeypatch):
    assertion = "signed-test-assertion"
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: assertion)
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, {"result": []}),
    )
    client.search_users("alice")
    _, url, meta = client._http.calls[0]
    assert url == client._config.token_url
    assert meta["auth"] is None
    assert meta["data"] == {
        "client_id": "service-account",
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
        "scope": "fr:idm:* fr:am:*",
    }


def test_malformed_token_json_maps_to_ping_error(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(client, _FakeResponse(200, None, text="not-json"))
    with pytest.raises(PingError, match="invalid JSON"):
        client._get_token()


def test_token_json_array_maps_to_ping_error(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(client, _FakeResponse(200, []))
    with pytest.raises(PingError, match="invalid JSON object"):
        client._get_token()


def test_token_is_cached(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, {"result": []}),
        _FakeResponse(200, {"result": []}),
    )
    client.search_users("alice")
    client.search_users("bob")
    # One token call + two search calls = 3 HTTP calls.
    assert len(client._http.calls) == 3
    assert client._http.calls[0][1].endswith("/access_token")


def test_401_drops_cached_token(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(401, {"error": "unauthorized"}, text="unauthorized"),
    )
    with pytest.raises(PingError, match="Unauthorized"):
        client.get_user("some-id")
    assert client._token is None


def test_token_refresh_after_expiry(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "old", "expires_in": 1}),
        _FakeResponse(200, {"result": []}),
        _FakeResponse(200, {"access_token": "new", "expires_in": 3600}),
        _FakeResponse(200, {"result": []}),
    )
    fake_now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])
    client.search_users("alice")
    fake_now[0] += 3600  # past expiry
    client.search_users("bob")
    token_calls = [
        c for c in client._http.calls if c[1].endswith("/access_token")
    ]
    assert len(token_calls) == 2


def test_create_oidc_application_uses_native_admin_endpoint(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, {"_id": "client-1", "client_secret": "hidden"}),
    )
    result = client.create_oidc_application(
        client_id="client-1", name="Test", app_type="web",
        redirect_uris=["https://app.example/callback"], scopes=["openid"],
        grant_types=["authorization_code"], response_types=["code"],
        token_endpoint_auth_method="client_secret_basic",
    )
    assert result["client_secret"] == "[REDACTED]"
    method, url, meta = client._http.calls[1]
    assert method == "PUT"
    assert url.endswith("/realm-config/agents/OAuth2Client/client-1")
    assert meta["headers"]["Accept-API-Version"] == "resource=1.0"
    assert meta["json"]["coreOAuth2ClientConfig"]["clientType"]["value"] == "Confidential"


def test_create_saml_application_posts_observed_managed_shape(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    sso_entities = {
        "key": "sso-key",
        "idpLocation": "idp-location",
        "idpPrivateId": "idp-private-placeholder",
        "domain": "example.com",
        "spLocation": "sp-location",
        "spPrivateId": "sp-private-placeholder",
        "idpLoginUrl": "https://idp.example/login",
    }
    app_config = {
        "appLocation": "app-location",
        "appLocationNoHttps": "app-location-no-https",
        "cotName": "cot-name",
        "roles": "roles-placeholder",
        "tenant": "tenant-placeholder",
    }
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(201, {"_id": "saml-1", "name": "SAML app"}),
    )

    result = client.create_saml_application(
        name="SAML app",
        description="Description",
        icon="icon",
        authoritative=True,
        template_name="saml",
        template_version="1.1.0",
        sso_entities=sso_entities,
        app_type_specific_config=app_config,
    )

    assert result == {"_id": "saml-1", "name": "SAML app"}
    method, url, meta = client._http.calls[1]
    assert method == "POST"
    assert url == "https://openam-test-tenant.forgeblocks.com/openidm/managed/alpha_application"
    assert meta["headers"] == {
        "Authorization": "Bearer abc",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-API-Version": "resource=1.0",
    }
    assert meta["json"] == {
        "name": "SAML app",
        "description": "Description",
        "icon": "icon",
        "authoritative": True,
        "templateName": "saml",
        "templateVersion": "1.1.0",
        "ssoEntities": sso_entities,
        "appTypeSpecificConfig": app_config,
    }


def test_create_saml_application_recursively_redacts_observed_private_fields(
    client, monkeypatch
):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    response = {
        "_id": "saml-1",
        "ssoEntities": {
            "key": "sso-key",
            "idpPrivateId": "idp-private-placeholder",
            "nested": [{"spPrivateId": "sp-private-placeholder"}],
        },
        "nested": {"credentials": {"privateKey": "key-placeholder"}},
    }
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(201, response),
    )

    result = client.create_saml_application(
        name="SAML app",
        description="Description",
        icon="icon",
        authoritative=True,
        template_name="saml",
        template_version="1.1.0",
        sso_entities={
            "key": "sso-key",
            "idpLocation": "idp-location",
            "idpPrivateId": "idp-private-placeholder",
            "domain": "example.com",
            "spLocation": "sp-location",
            "spPrivateId": "sp-private-placeholder",
            "idpLoginUrl": "https://idp.example/login",
        },
        app_type_specific_config={
            "appLocation": "app-location",
            "appLocationNoHttps": "app-location-no-https",
            "cotName": "cot-name",
            "roles": "roles-placeholder",
            "tenant": "tenant-placeholder",
        },
    )

    assert result["ssoEntities"]["key"] == "sso-key"
    assert result["ssoEntities"]["idpPrivateId"] == "[REDACTED]"
    assert result["ssoEntities"]["nested"][0]["spPrivateId"] == "[REDACTED]"
    assert result["nested"]["credentials"]["privateKey"] == "[REDACTED]"


def test_successful_invalid_json_maps_to_ping_error(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    client._token = "cached"
    client._token_expiry = time.monotonic() + 1000
    _queue(client, _FakeResponse(201, None, text="not-json"))
    with pytest.raises(PingError, match="invalid JSON"):
        client.get_user("u1")


def test_successful_json_array_maps_to_ping_error(client):
    client._token = "cached"
    client._token_expiry = time.monotonic() + 1000
    _queue(client, _FakeResponse(200, []))
    with pytest.raises(PingError, match="invalid JSON object"):
        client.get_user("u1")


def test_server_generated_secrets_are_redacted_in_http_errors(client):
    client._token = "cached"
    client._token_expiry = time.monotonic() + 1000
    _queue(
        client,
        _FakeResponse(
            400,
            {"error": {"privateKey": "server-private", "certificate": "server-cert"}},
            text='{"error":{"privateKey":"server-private","certificate":"server-cert"}}',
        ),
    )
    with pytest.raises(PingError) as exc_info:
        client.get_user("u1")
    message = str(exc_info.value)
    assert "server-private" not in message
    assert "server-cert" not in message
    assert message.count("[REDACTED]") >= 2


def test_pem_secrets_are_redacted_in_plain_http_errors(client):
    client._token = "cached"
    client._token_expiry = time.monotonic() + 1000
    pem = "-----BEGIN PRIVATE KEY-----secret-----END PRIVATE KEY-----"
    _queue(client, _FakeResponse(400, None, text=pem))
    with pytest.raises(PingError) as exc_info:
        client.get_user("u1")
    assert pem not in str(exc_info.value)
    assert "[REDACTED PRIVATE KEY]" in str(exc_info.value)


def test_create_saml_application_redacts_request_secrets_in_http_errors(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(
            400,
            {"error": "bad request"},
            text="invalid idp-private-placeholder and sp-private-placeholder",
        ),
    )

    with pytest.raises(PingError) as exc_info:
        client.create_saml_application(
            name="SAML app",
            description="Description",
            icon="icon",
            authoritative=True,
            template_name="saml",
            template_version="1.1.0",
            sso_entities={
                "key": "sso-key",
                "idpLocation": "idp-location",
                "idpPrivateId": "idp-private-placeholder",
                "domain": "example.com",
                "spLocation": "sp-location",
                "spPrivateId": "sp-private-placeholder",
                "idpLoginUrl": "https://idp.example/login",
            },
            app_type_specific_config={
                "appLocation": "app-location",
                "appLocationNoHttps": "app-location-no-https",
                "cotName": "cot-name",
                "roles": "roles-placeholder",
                "tenant": "tenant-placeholder",
            },
        )

    assert "idp-private-placeholder" not in str(exc_info.value)
    assert "sp-private-placeholder" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


def test_create_oidc_native_uses_public_client_type(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, {"_id": "native-client"}),
    )
    client.create_oidc_application(
        client_id="native-client", name="Native", app_type="native",
        redirect_uris=["com.example.app:/callback"], scopes=["openid"],
        grant_types=["authorization_code"], response_types=["code"],
        token_endpoint_auth_method="none",
    )
    meta = client._http.calls[1][2]
    assert meta["json"]["coreOAuth2ClientConfig"]["clientType"]["value"] == "Public"
    assert meta["json"]["coreOAuth2ClientConfig"]["redirectionUris"]["value"] == ["com.example.app:/callback"]


def test_search_users_builds_broad_filter_and_escapes_query(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, {"result": []}),
    )
    client.search_users('need "quotes"', page_size=5, fields="_id,userName")
    _, url, meta = client._http.calls[1]
    assert url.endswith("/openidm/managed/alpha_user")
    filter_expr = meta["params"]["_queryFilter"]
    # All 5 fields ORed and quotes escaped.
    for field in ("userName", "mail", "givenName", "sn", "displayName"):
        assert f'{field} co "need \\"quotes\\""' in filter_expr
    assert " or " in filter_expr
    assert meta["params"]["_pageSize"] == 5
    assert meta["params"]["_fields"] == "_id,userName"


def test_search_users_adds_first_last_name_clause(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, {"result": []}),
    )
    client.search_users("  Adam   Smith  ")
    filter_expr = client._http.calls[1][2]["params"]["_queryFilter"]
    assert '(givenName co "Adam" and sn co "Smith")' in filter_expr
    assert 'displayName co "Adam   Smith"' in filter_expr
    assert filter_expr.count("givenName co") == 2
    assert filter_expr.count("sn co") == 2


def test_search_users_supports_explicit_last_first_name(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, {"result": []}),
    )
    client.search_users("Smith, Adam")
    filter_expr = client._http.calls[1][2]["params"]["_queryFilter"]
    assert '(givenName co "Adam" and sn co "Smith")' in filter_expr


def test_search_users_does_not_tokenize_three_part_query(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, {"result": []}),
    )
    client.search_users("Mary Jane Watson")
    filter_expr = client._http.calls[1][2]["params"]["_queryFilter"]
    assert "givenName co \"Mary\"" not in filter_expr
    assert 'displayName co "Mary Jane Watson"' in filter_expr


def test_search_users_escapes_name_tokens(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, {"result": []}),
    )
    client.search_users('A\\"dam Smi\\\\th')
    filter_expr = client._http.calls[1][2]["params"]["_queryFilter"]
    assert '(givenName co "A\\\\\\"dam" and sn co "Smi\\\\\\\\th")' in filter_expr
    assert " or (givenName" in filter_expr


def test_patch_user_sends_json_body(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, {"_id": "u1", "accountStatus": "inactive"}),
    )
    ops = [{"operation": "replace", "field": "/accountStatus", "value": "inactive"}]
    result = client.patch_user("u1", ops)
    assert result["accountStatus"] == "inactive"
    method, url, meta = client._http.calls[1]
    assert method == "PATCH"
    assert url.endswith("/openidm/managed/alpha_user/u1")
    assert meta["json"] == ops


def test_resolve_user_id_requires_exact_unique_uuid(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    user_id = "550e8400-e29b-41d4-a716-446655440000"
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, {"result": [{"_id": user_id, "userName": "alice.smith"}]}),
    )
    assert client.resolve_user_id("alice.smith") == user_id
    _, url, meta = client._http.calls[1]
    assert url.endswith("/openidm/managed/alpha_user")
    assert meta["params"] == {
        "_queryFilter": 'userName eq "alice.smith"',
        "_pageSize": 10,
        "_fields": "_id,userName",
    }


@pytest.mark.parametrize(
    "result,match",
    [
        ({"result": []}, "No user found"),
        ({"result": [{"_id": "550e8400-e29b-41d4-a716-446655440000", "userName": "alice"}, {"_id": "550e8400-e29b-41d4-a716-446655440001", "userName": "alice"}]}, "Multiple users"),
        ({"result": [{"userName": "alice"}]}, "missing an IDM _id"),
    ],
)
def test_resolve_user_id_rejects_unsafe_matches(client, monkeypatch, result, match):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, result),
    )
    with pytest.raises(PingError, match=match):
        client.resolve_user_id("alice")


def test_logout_by_user_uses_documented_am_endpoint(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    user_id = "550e8400-e29b-41d4-a716-446655440000"
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, {"result": 2}),
    )
    client.logout_user_sessions(user_id)
    _, url, meta = client._http.calls[1]
    assert url.endswith("/realms/alpha/sessions/")
    assert meta["params"] == {"_action": "logoutByUser"}
    assert meta["json"] == {"username": user_id}
    assert meta["headers"]["Accept-API-Version"] == "resource=5.1, protocol=1.0"


def test_logout_by_user_rejects_non_uuid_before_request(client):
    with pytest.raises(PingError, match="requires an IDM user UUID"):
        client.logout_user_sessions("alice.smith")
    assert client._http.calls == []


def test_delete_user_device_rejects_bad_method(client):
    with pytest.raises(PingError, match="method must be"):
        client.delete_user_device("u1", "sms", "some-uuid")


def test_audit_headers_are_monitoring_only(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, {"result": []}),
    )
    client.get_audit_events("am-access")
    _, url, meta = client._http.calls[1]
    assert url.endswith("/monitoring/logs")
    assert meta["headers"]["x-api-key"] == "test-audit-key"
    assert meta["headers"]["x-api-secret"] == "test-audit-secret"


def test_normal_calls_do_not_send_audit_headers(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")
    _queue(
        client,
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
        _FakeResponse(200, {"result": []}),
    )
    client.search_users("alice")
    headers = client._http.calls[0][2]["headers"]
    assert "x-api-key" not in headers
    assert "x-api-secret" not in headers


def test_audit_username_filter_requires_configured_payload_path(client):
    client._config = client._config.__class__(
        base_url=client._config.base_url,
        realm=client._config.realm,
        service_account_id=client._config.service_account_id,
        private_jwk=client._config.private_jwk,
        token_url=client._config.token_url,
        audit_api_key=client._config.audit_api_key,
        audit_api_secret=client._config.audit_api_secret,
    )
    with pytest.raises(PingError, match="username filtering"):
        client.get_audit_events("am-access", username="alice")


def test_monitoring_401_mentions_audit_credentials(client):
    client._token = "cached"
    client._token_expiry = time.monotonic() + 1000
    _queue(client, _FakeResponse(401, {"error": "unauthorized"}, text="unauthorized"))
    with pytest.raises(PingError, match="monitoring API.*AUDIT"):
        client.get_audit_events("am-access")


def test_transport_error_wrapped(client, monkeypatch):
    monkeypatch.setattr(ping_client_module, "create_client_assertion", lambda *args: "assertion")

    def raise_transport(*_a, **_k):
        raise httpx.ConnectError("boom")

    client._http.request = raise_transport
    client._token = "cached"
    client._token_expiry = time.monotonic() + 1000
    with pytest.raises(PingError, match="failed: boom"):
        client.get_user("u1")


def _ready(client):
    client._token = "tok"
    client._token_expiry = time.monotonic() + 1000


@pytest.mark.parametrize("relationship", ["groups", "authzRoles", "assignments"])
def test_list_user_relationship_asks_for_display_names(client, relationship):
    _ready(client)
    record = {"_refResourceId": "g1", "_ref": "managed/alpha_group/g1", "name": "Help desk"}
    _queue(client, _FakeResponse(200, {"result": [record]}))
    assert client.list_user_relationship("u1", relationship) == {"result": [record]}
    ((method, url, meta),) = client._http.calls
    assert method == "GET" and url.endswith(f"/managed/alpha_user/u1/{relationship}")
    # Documented IDM syntax: the related object's fields beside its reference.
    assert meta["params"] == {
        "_queryFilter": "true",
        "_pageSize": 50,
        "_fields": "_ref/*,name,description",
    }


def test_list_user_relationship_falls_back_when_the_field_list_is_rejected(client):
    _ready(client)
    _queue(
        client,
        _FakeResponse(400, text="Invalid _fields"),
        _FakeResponse(200, {"result": [{"_refResourceId": "g1"}]}),
    )
    assert client.list_user_relationship("u1", "groups") == {
        "result": [{"_refResourceId": "g1"}]
    }
    first, second = client._http.calls
    assert "_fields" in first[2]["params"] and "_fields" not in second[2]["params"]
    assert second[2]["params"] == {"_queryFilter": "true", "_pageSize": 50}


def test_list_user_relationship_does_not_hide_other_errors(client):
    _ready(client)
    _queue(client, _FakeResponse(403, text="Access denied"))
    with pytest.raises(PingError) as raised:
        client.list_user_relationship("u1", "groups")
    assert raised.value.status_code == 403
    assert len(client._http.calls) == 1


def test_list_groups_is_sorted_by_name_and_filters_safely(client):
    _ready(client)
    _queue(
        client,
        _FakeResponse(200, {"result": [{"_id": "g1", "name": "Help desk"}]}),
        _FakeResponse(200, {"result": []}),
    )
    assert client.list_groups() == {"result": [{"_id": "g1", "name": "Help desk"}]}
    client.list_groups('  help "desk"  ', page_size=25)
    everything, filtered = (call[2]["params"] for call in client._http.calls)
    assert client._http.calls[0][1].endswith("/openidm/managed/alpha_group")
    assert everything == {
        "_queryFilter": "true",
        "_fields": "_id,name,description",
        "_pageSize": 200,
        "_sortKeys": "name",
    }
    # The term is trimmed and its quotes escaped, so it cannot alter the filter.
    assert filtered["_queryFilter"] == 'name co "help \\"desk\\""'
    assert filtered["_pageSize"] == 25


USER_UUID = "b0f30dfb-4e01-457e-a567-c258a74e4fe2"


def test_list_user_sessions_queries_by_uuid_and_realm_and_hides_handles(client):
    _ready(client)
    session = {
        "_rev": "647523949",
        "username": USER_UUID,
        "universalId": f"id={USER_UUID},ou=user,o=alpha,ou=services,ou=am-config",
        "realm": "/alpha",
        "sessionHandle": "shandle:SECRET-HANDLE",
        "latestAccessTime": "2024-01-15T07:42:42.544Z",
        "maxIdleExpirationTime": "2024-01-15T08:12:42Z",
        "maxSessionExpirationTime": "2024-01-15T09:31:22Z",
    }
    _queue(client, _FakeResponse(200, {"result": [session, "not-a-record"]}))
    listed = client.list_user_sessions(f"  {USER_UUID.upper()}  ")
    ((method, url, meta),) = client._http.calls
    assert method == "GET" and url.endswith("/am/json/realms/root/realms/alpha/sessions")
    # Ping's documented query: the user's UUID and the realm, on resource 4.0.
    assert meta["params"] == {
        "_queryFilter": f'username eq "{USER_UUID}" and realm eq "/alpha"'
    }
    assert meta["headers"]["Accept-API-Version"] == "resource=4.0, protocol=1.0"
    assert listed == {
        "result": [
            {
                "latestAccessTime": "2024-01-15T07:42:42.544Z",
                "maxIdleExpirationTime": "2024-01-15T08:12:42Z",
                "maxSessionExpirationTime": "2024-01-15T09:31:22Z",
                "realm": "/alpha",
            }
        ],
        "resultCount": 1,
    }
    # A handle can end a session, so it never leaves the client.
    assert "SECRET-HANDLE" not in str(listed) and "universalId" not in str(listed)


@pytest.mark.parametrize("user_id", ["alice", "", "../other", 'x" or realm eq "/'])
def test_list_user_sessions_refuses_anything_but_a_uuid(client, user_id):
    _ready(client)
    with pytest.raises(PingError, match="requires an IDM user UUID"):
        client.list_user_sessions(user_id)
    assert client._http.calls == []


def test_list_user_sessions_tolerates_an_empty_answer(client):
    _ready(client)
    _queue(client, _FakeResponse(200, {"resultCount": 0}))
    assert client.list_user_sessions(USER_UUID) == {"result": [], "resultCount": 0}


def test_tail_follows_a_source_for_one_user_and_continues_from_a_cookie(client):
    _ready(client)
    _queue(
        client,
        _FakeResponse(200, {"result": [], "pagedResultsCookie": "c1"}),
        _FakeResponse(200, {"result": [], "pagedResultsCookie": "c2"}),
    )
    assert client.tail_audit_events("am-authentication", username='al"ice') == {
        "result": [],
        "pagedResultsCookie": "c1",
    }
    client.tail_audit_events("am-authentication", username="alice", cookie="c1")
    first, second = client._http.calls
    assert first[0] == "GET" and first[1].endswith("/monitoring/logs/tail")
    # The first call has no cookie: the API then returns the last 15 seconds.
    assert first[2]["params"] == {
        "source": "am-authentication",
        "_queryFilter": 'payload.userName co "al\\"ice"',
    }
    assert second[2]["params"]["_pagedResultsCookie"] == "c1"
    for call in (first, second):
        assert call[2]["headers"]["x-api-key"] == "test-audit-key"
        assert call[2]["headers"]["x-api-secret"] == "test-audit-secret"
        assert "_pageSize" not in call[2]["params"]


def test_tail_401_mentions_audit_credentials(client):
    _ready(client)
    _queue(client, _FakeResponse(401, {"error": "unauthorized"}, text="unauthorized"))
    with pytest.raises(PingError, match="monitoring API.*AUDIT"):
        client.tail_audit_events("am-access")


def test_delete_user_relationship_targets_the_reference_id(client):
    _ready(client)
    _queue(client, _FakeResponse(200, {"_id": "rel-1"}))
    assert client.delete_user_relationship("u1", "authzRoles", " rel-1 ") == {"_id": "rel-1"}
    ((method, url, meta),) = client._http.calls
    assert method == "DELETE"
    assert url.endswith("/openidm/managed/alpha_user/u1/authzRoles/rel-1")
    assert meta["params"] is None and meta["json"] is None


@pytest.mark.parametrize("reference_id", ["", "  ", "a/b", "../u2", "x?_action=y", "a#b"])
def test_delete_user_relationship_refuses_anything_but_a_plain_id(client, reference_id):
    _ready(client)
    with pytest.raises(PingError, match="reference ID is required"):
        client.delete_user_relationship("u1", "groups", reference_id)
    assert client._http.calls == []


def test_list_internal_roles_is_sorted_by_name_and_filters_safely(client):
    _ready(client)
    _queue(client, _FakeResponse(200, {"result": []}), _FakeResponse(200, {"result": []}))
    client.list_internal_roles()
    client.list_internal_roles(' admin" or true or name co "', page_size=10)
    everything, filtered = client._http.calls
    assert everything[1].endswith("/openidm/internal/role")
    assert everything[2]["params"] == {
        "_queryFilter": "true",
        "_fields": "_id,name,description",
        "_pageSize": 200,
        "_sortKeys": "name",
    }
    assert filtered[2]["params"]["_queryFilter"] == (
        'name co "admin\\" or true or name co \\""'
    )
    assert filtered[2]["params"]["_pageSize"] == 10

