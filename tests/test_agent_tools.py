"""Tests for the tool layer: validation, confirm-gating, error mapping."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ping_admin_agent import agent as agent_mod
from ping_admin_agent.ping_client import PingError


@pytest.fixture
def fake_client(monkeypatch, stub_config):
    client = MagicMock()
    monkeypatch.setattr(agent_mod, "get_client", lambda: client)
    return client


# -- search / get -----------------------------------------------------


def test_create_oidc_validates_before_client(fake_client):
    result = agent_mod.create_oidc_application(
        "client-1", "Test", "web", [], ["openid"], ["authorization_code"],
        ["code"], "client_secret_basic", confirm=True,
    )
    assert result["status"] == "error"
    fake_client.create_oidc_application.assert_not_called()


SAML_SSO_ENTITIES = {
    "key": "sso-key",
    "idpLocation": "idp-location",
    "idpPrivateId": "idp-private-placeholder",
    "domain": "example.com",
    "spLocation": "sp-location",
    "spPrivateId": "sp-private-placeholder",
    "idpLoginUrl": "https://idp.example/login",
}
SAML_APP_CONFIG = {
    "appLocation": "app-location",
    "appLocationNoHttps": "app-location-no-https",
    "cotName": "cot-name",
    "roles": "roles-placeholder",
    "tenant": "tenant-placeholder",
}


def _saml_kwargs(**overrides):
    values = {
        "name": "SAML app",
        "description": "Description",
        "icon": "icon",
        "authoritative": True,
        "template_name": "saml",
        "template_version": "1.1.0",
        "sso_entities": dict(SAML_SSO_ENTITIES),
        "app_type_specific_config": dict(SAML_APP_CONFIG),
        "confirm": True,
    }
    values.update(overrides)
    return values


def test_create_saml_validates_before_client(fake_client):
    result = agent_mod.create_saml_application(**_saml_kwargs(name="   "))
    assert result == {"status": "error", "message": "name must be a nonblank string."}
    fake_client.create_saml_application.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("description", None),
        ("icon", []),
        ("authoritative", 1),
        ("template_name", "oidc"),
        ("template_version", "1.2"),
        ("sso_entities", {"key": "only"}),
        ("app_type_specific_config", {"appLocation": "only"}),
    ],
)
def test_create_saml_rejects_invalid_shape_before_client(fake_client, field, value):
    result = agent_mod.create_saml_application(**_saml_kwargs(**{field: value}))
    assert result["status"] == "error"
    fake_client.create_saml_application.assert_not_called()


def test_create_saml_rejects_unknown_nested_keys(fake_client):
    entities = dict(SAML_SSO_ENTITIES)
    entities["unexpected"] = "value"
    result = agent_mod.create_saml_application(
        **_saml_kwargs(sso_entities=entities)
    )
    assert result["status"] == "error"
    assert "unknown keys" in result["message"]
    fake_client.create_saml_application.assert_not_called()


def test_create_saml_happy(fake_client):
    fake_client.create_saml_application.return_value = {"_id": "saml-1", "name": "SAML app"}
    result = agent_mod.create_saml_application(
        **_saml_kwargs(name=" SAML app ", description=" Description ")
    )
    assert result == {
        "status": "success",
        "data": {"_id": "saml-1", "name": "SAML app"},
    }
    fake_client.create_saml_application.assert_called_once_with(
        name="SAML app",
        description="Description",
        icon="icon",
        authoritative=True,
        template_name="saml",
        template_version="1.1.0",
        sso_entities=SAML_SSO_ENTITIES,
        app_type_specific_config=SAML_APP_CONFIG,
    )


def test_create_saml_confirmation_includes_values_and_redacts_private_values(fake_client):
    result = agent_mod.create_saml_application(**_saml_kwargs(confirm=False))
    message = result["message"]
    assert "idp-location" in message
    assert "example.com" in message
    assert "app-location" in message
    assert "idp-private-placeholder" not in message
    assert "sp-private-placeholder" not in message
    assert "[REDACTED]" in message


def test_create_saml_confirmation_defaults_to_false(fake_client):
    values = _saml_kwargs()
    values.pop("confirm")
    result = agent_mod.create_saml_application(**values)
    assert result["status"] == "error"
    assert "confirmation" in result["message"].lower()
    fake_client.create_saml_application.assert_not_called()


def test_create_saml_maps_client_error(fake_client):
    fake_client.create_saml_application.side_effect = PingError("create failed")
    result = agent_mod.create_saml_application(**_saml_kwargs())
    assert result == {"status": "error", "message": "create failed"}


def test_create_saml_is_registered_and_documented():
    assert any(getattr(tool, "func", tool) is agent_mod.create_saml_application for tool in agent_mod.root_agent.tools)
    assert "create_saml_application" in agent_mod.INSTRUCTION
    assert "SAML" in agent_mod.root_agent.description


@pytest.mark.parametrize("confirm", ["false", "true", 0, 1, [], [1], None])
def test_create_saml_requires_boolean_true(fake_client, confirm):
    result = agent_mod.create_saml_application(**_saml_kwargs(confirm=confirm))
    assert result["status"] == "error"
    assert "confirmation" in result["message"].lower()
    fake_client.create_saml_application.assert_not_called()


@pytest.mark.parametrize("app_type", [[], {}, 1, None])
def test_create_oidc_rejects_non_string_app_type(fake_client, app_type):
    result = agent_mod.create_oidc_application(
        "client-1", "Test", app_type, ["https://app.example/callback"],
        ["openid"], ["authorization_code"], ["code"],
        "client_secret_basic", confirm=True,
    )
    assert result["status"] == "error"
    fake_client.create_oidc_application.assert_not_called()


def test_create_oidc_rejects_non_string_auth_method(fake_client):
    result = agent_mod.create_oidc_application(
        "client-1", "Test", "web", ["https://app.example/callback"],
        ["openid"], ["authorization_code"], ["code"], None, confirm=True,
    )
    assert result == {"status": "error", "message": "token_endpoint_auth_method must be a string."}
    fake_client.create_oidc_application.assert_not_called()


def test_create_oidc_native_accepts_custom_scheme(fake_client):
    fake_client.create_oidc_application.return_value = {"_id": "client-1"}
    result = agent_mod.create_oidc_application(
        "client-1", "Native", "native", ["com.example.app:/callback"],
        ["openid"], ["authorization_code"], ["code"], "none", confirm=True,
    )
    assert result["status"] == "success"
    fake_client.create_oidc_application.assert_called_once()


def test_create_oidc_happy(fake_client):
    fake_client.create_oidc_application.return_value = {"_id": "client-1"}
    result = agent_mod.create_oidc_application(
        " client-1 ", " Test ", "web", ["https://app.example/callback"],
        ["openid"], ["authorization_code"], ["code"], "client_secret_basic",
        confirm=True,
    )
    assert result == {"status": "success", "data": {"_id": "client-1"}}
    fake_client.create_oidc_application.assert_called_once()


def test_search_users_requires_query(fake_client):
    result = agent_mod.search_users("   ")
    assert result["status"] == "error"
    fake_client.search_users.assert_not_called()


def test_search_users_happy(fake_client):
    fake_client.search_users.return_value = {"result": [{"_id": "u1"}]}
    result = agent_mod.search_users("  Adam Smith  ")
    assert result == {"status": "success", "data": {"result": [{"_id": "u1"}]}}
    fake_client.search_users.assert_called_once_with(
        query="Adam Smith",
        page_size=10,
        fields="_id,userName,mail,givenName,sn,displayName,accountStatus",
    )


def test_search_users_preserves_multiple_matches(fake_client):
    matches = {"result": [{"_id": "u1"}, {"_id": "u2"}]}
    fake_client.search_users.return_value = matches
    result = agent_mod.search_users("Smith")
    assert result == {"status": "success", "data": matches}
    fake_client.search_users.assert_called_once()


def test_get_user_maps_client_error(fake_client):
    fake_client.get_user.side_effect = PingError("not found")
    result = agent_mod.get_user("u1")
    assert result == {"status": "error", "message": "not found"}


# -- confirm gating on destructive tools ------------------------------


DESTRUCTIVE_CALLS = [
    ("reset_user_password", {"user_id": "u1"}),
    ("set_user_status", {"user_id": "u1", "status": "inactive"}),
    ("logout_user_sessions", {"username": "alice"}),
    (
        "create_oidc_application",
        {
            "client_id": "client-1",
            "name": "Test client",
            "app_type": "web",
            "redirect_uris": ["https://app.example.com/callback"],
            "scopes": ["openid"],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_basic",
        },
    ),
    ("unenroll_mfa_device", {"user_id": "u1", "method": "push"}),
    (
        "change_user_membership",
        {
            "user_id": "u1",
            "kind": "groups",
            "membership_id": "g1",
            "action": "add",
        },
    ),
    (
        "change_user_assignment",
        {"user_id": "u1", "assignment_id": "a1", "action": "grant"},
    ),
]


@pytest.mark.parametrize("confirm", ["false", "true", 0, 1, [], [1], None])
def test_destructive_tools_require_boolean_true(fake_client, confirm):
    result = agent_mod.set_user_status(
        "u1", "inactive", confirm=confirm
    )
    assert result["status"] == "error"
    fake_client.patch_user.assert_not_called()



@pytest.mark.parametrize("tool_name,kwargs", DESTRUCTIVE_CALLS)
def test_destructive_tools_refuse_without_confirm(fake_client, tool_name, kwargs):
    tool = getattr(agent_mod, tool_name)
    result = tool(**kwargs)
    assert result["status"] == "error"
    assert "confirmation" in result["message"].lower()
    # No client method should have been invoked.
    assert not any(
        m.called
        for m in fake_client._mock_children.values()
        if hasattr(m, "called")
    )


def test_logout_without_confirm_does_not_lookup(fake_client):
    result = agent_mod.logout_user_sessions("alice")
    assert result["status"] == "error"
    fake_client.resolve_user_id.assert_not_called()
    fake_client.logout_user_sessions.assert_not_called()


def test_logout_resolves_unique_username_to_uuid(fake_client):
    fake_client.resolve_user_id.return_value = "550e8400-e29b-41d4-a716-446655440000"
    fake_client.logout_user_sessions.return_value = {"result": 1}
    result = agent_mod.logout_user_sessions(" alice ", confirm=True)
    assert result == {"status": "success", "data": {"result": 1}}
    fake_client.resolve_user_id.assert_called_once_with("alice")
    fake_client.logout_user_sessions.assert_called_once_with(
        "550e8400-e29b-41d4-a716-446655440000"
    )


@pytest.mark.parametrize(
    "message",
    [
        "No user found with exact username 'alice'.",
        "Multiple users found with exact username 'alice'.",
        "Exact username 'alice' is missing an IDM _id.",
    ],
)
def test_logout_maps_resolution_errors(fake_client, message):
    fake_client.resolve_user_id.side_effect = PingError(message)
    result = agent_mod.logout_user_sessions("alice", confirm=True)
    assert result == {"status": "error", "message": message}
    fake_client.logout_user_sessions.assert_not_called()


def test_logout_maps_am_client_error(fake_client):
    fake_client.resolve_user_id.return_value = "550e8400-e29b-41d4-a716-446655440000"
    fake_client.logout_user_sessions.side_effect = PingError("AM unavailable")
    result = agent_mod.logout_user_sessions("alice", confirm=True)
    assert result == {"status": "error", "message": "AM unavailable"}


def test_reset_password_generates_temp_password(fake_client):
    fake_client.patch_user.return_value = {"_id": "u1"}
    result = agent_mod.reset_user_password("u1", confirm=True)
    assert result["status"] == "success"
    assert isinstance(result["data"]["new_password"], str)
    assert len(result["data"]["new_password"]) >= 12
    # Client received the /password patch.
    fake_client.patch_user.assert_called_once()
    ops = fake_client.patch_user.call_args.args[1]
    assert ops[0]["field"] == "/password"


def test_reset_password_uses_supplied_password(fake_client):
    fake_client.patch_user.return_value = {"_id": "u1"}
    result = agent_mod.reset_user_password(
        "u1", confirm=True, new_password="Provided-1234"
    )
    assert result["data"]["new_password"] == "Provided-1234"


def test_set_user_status_validates_value(fake_client):
    result = agent_mod.set_user_status("u1", "banned", confirm=True)
    assert result["status"] == "error"
    fake_client.patch_user.assert_not_called()


def test_change_membership_add_field_ends_dash(fake_client):
    fake_client.patch_user.return_value = {"_id": "u1"}
    agent_mod.change_user_membership(
        "u1", "groups", "g1", "add", confirm=True
    )
    ops = fake_client.patch_user.call_args.args[1]
    assert ops[0]["operation"] == "add"
    assert ops[0]["field"] == "/groups/-"
    assert ops[0]["value"]["_ref"].endswith("alpha_group/g1")


def test_change_membership_removes_the_relationship_by_its_reference_id(fake_client):
    fake_client.list_user_relationship.return_value = {
        "result": [
            {"_id": "rel-1", "_ref": "managed/alpha_group/g1", "_refResourceId": "g1"},
            {"_id": "rel-2", "_ref": "managed/alpha_group/g2", "_refResourceId": "g2"},
        ]
    }
    fake_client.delete_user_relationship.return_value = {"_id": "rel-2"}
    result = agent_mod.change_user_membership("u1", "groups", "g2", "remove", confirm=True)
    assert result["status"] == "success"
    fake_client.list_user_relationship.assert_called_once_with("u1", "groups")
    # Ping's documented removal: the ID of the relationship, not of the group.
    fake_client.delete_user_relationship.assert_called_once_with("u1", "groups", "rel-2")
    fake_client.patch_user.assert_not_called()


def test_change_membership_does_not_remove_what_the_user_does_not_hold(fake_client):
    fake_client.list_user_relationship.return_value = {
        "result": [{"_id": "rel-1", "_ref": "managed/alpha_group/g1"}]
    }
    result = agent_mod.change_user_membership("u1", "groups", "g9", "remove", confirm=True)
    assert result["status"] == "error" and "nothing was removed" in result["message"]
    fake_client.delete_user_relationship.assert_not_called()


def test_authorization_roles_are_internal_roles_unless_a_reference_says_otherwise(fake_client):
    fake_client.patch_user.return_value = {"_id": "u1"}
    agent_mod.change_user_membership("u1", "authzRoles", "openidm-admin", "add", confirm=True)
    (op,) = fake_client.patch_user.call_args.args[1]
    # As Ping documents granting an authorization role.
    assert op == {
        "operation": "add",
        "field": "/authzRoles/-",
        "value": {"_ref": "internal/role/openidm-admin"},
    }
    agent_mod.change_user_membership("u1", "authzRoles", "managed/alpha_role/r1", "add", confirm=True)
    assert fake_client.patch_user.call_args.args[1][0]["value"] == {"_ref": "managed/alpha_role/r1"}
    # A role held under another collection is removed by that exact reference.
    fake_client.list_user_relationship.return_value = {
        "result": [{"_id": "rel-9", "_ref": "managed/alpha_role/r1"}]
    }
    agent_mod.change_user_membership("u1", "authzRoles", "managed/alpha_role/r1", "remove", confirm=True)
    fake_client.delete_user_relationship.assert_called_once_with("u1", "authzRoles", "rel-9")


@pytest.mark.parametrize(
    ("kind", "membership_id"),
    [
        ("groups", "internal/role/openidm-admin"),
        ("groups", "managed/alpha_user/u2"),
        ("authzRoles", "managed/alpha_group/g1"),
        ("groups", "../u2"),
        ("groups", "g1?_action=x"),
        ("groups", "managed/alpha_group/"),
    ],
)
def test_change_membership_refuses_a_reference_its_kind_does_not_allow(
    fake_client, kind, membership_id
):
    result = agent_mod.change_user_membership("u1", kind, membership_id, "add", confirm=True)
    assert result["status"] == "error"
    fake_client.patch_user.assert_not_called()
    fake_client.delete_user_relationship.assert_not_called()


def test_change_membership_touches_nothing_until_confirmed(fake_client):
    result = agent_mod.change_user_membership("u1", "groups", "g1", "remove")
    assert result["status"] == "error"
    fake_client.list_user_relationship.assert_not_called()
    fake_client.delete_user_relationship.assert_not_called()


def test_unenroll_mfa_deletes_each_device(fake_client):
    fake_client.list_user_devices.return_value = {
        "result": [{"uuid": "d1"}, {"uuid": "d2"}]
    }
    result = agent_mod.unenroll_mfa_device("u1", "push", confirm=True)
    assert result["status"] == "success"
    assert result["data"]["count"] == 2
    assert fake_client.delete_user_device.call_count == 2


def test_unenroll_mfa_rejects_bad_method(fake_client):
    result = agent_mod.unenroll_mfa_device("u1", "sms", confirm=True)
    assert result["status"] == "error"
    fake_client.list_user_devices.assert_not_called()


def test_get_user_activity_validates_source(fake_client):
    result = agent_mod.get_user_activity("alice", source="unknown-source")
    assert result["status"] == "error"
    fake_client.get_audit_events.assert_not_called()


def test_list_user_sessions_is_read_only_and_needs_a_user(fake_client):
    assert agent_mod.list_user_sessions("  ")["status"] == "error"
    fake_client.list_user_sessions.assert_not_called()
    fake_client.list_user_sessions.return_value = {"result": [], "resultCount": 0}
    result = agent_mod.list_user_sessions(" u1 ")
    assert result == {"status": "success", "data": {"result": [], "resultCount": 0}}
    fake_client.list_user_sessions.assert_called_once_with("u1")
    # It reads only: nothing that ends a session is touched.
    fake_client.logout_user_sessions.assert_not_called()


def test_list_user_sessions_reports_a_client_error(fake_client):
    fake_client.list_user_sessions.side_effect = agent_mod.PingError("denied")
    result = agent_mod.list_user_sessions("u1")
    assert result["status"] == "error" and "denied" in str(result)


def test_list_user_sessions_needs_no_confirmation():
    names = [getattr(tool, "name", getattr(tool, "__name__", "")) for tool in agent_mod.root_agent.tools]
    assert "list_user_sessions" in names
    confirmed = [tool.name for tool in agent_mod.root_agent.tools if hasattr(tool, "name") and type(tool).__name__ == "ConfirmedFunctionTool"]
    assert "list_user_sessions" not in confirmed and "logout_user_sessions" in confirmed


def test_get_user_activity_looks_back_by_default_and_tails_when_live(fake_client):
    fake_client.get_audit_events.return_value = {"result": []}
    fake_client.tail_audit_events.return_value = {"result": [], "pagedResultsCookie": "c2"}
    assert agent_mod.get_user_activity(" alice ")["status"] == "success"
    fake_client.get_audit_events.assert_called_once_with(
        source="am-authentication", username="alice", page_size=50
    )
    fake_client.tail_audit_events.assert_not_called()

    result = agent_mod.get_user_activity("alice", source="am-access", live=True, cookie="c1")
    assert result["data"]["pagedResultsCookie"] == "c2"
    fake_client.tail_audit_events.assert_called_once_with(
        source="am-access", username="alice", cookie="c1"
    )
    # No cookie yet means a fresh tail, not an empty cookie parameter.
    agent_mod.get_user_activity("alice", live=True)
    assert fake_client.tail_audit_events.call_args.kwargs["cookie"] is None
    fake_client.get_audit_events.assert_called_once()

