"""Chat text for tool outcomes stays readable and free of raw JSON."""

import pytest

from ping_admin_agent.ui_results import (
    ToolResult,
    account_names,
    describe_change,
    describe_result,
    unlinked,
)


@pytest.mark.parametrize(
    ("name", "args", "data", "expected"),
    [
        ("set_user_status", {"user_id": "u1", "status": "inactive"}, {"userName": "alice"}, "Account alice is now inactive."),
        ("logout_user_sessions", {"username": "alice"}, {}, "Ended all sessions for user alice."),
        ("unenroll_mfa_device", {"user_id": "u1", "method": "push"}, {"count": 2}, "Removed 2 push MFA device(s) from user u1."),
        ("change_user_membership", {"user_id": "u1", "kind": "groups", "membership_id": "g1", "action": "add"}, {}, "Added group g1 to user u1."),
        ("change_user_membership", {"user_id": "u1", "kind": "authzRoles", "membership_id": "r1", "action": "remove"}, {}, "Removed role r1 from user u1."),
        ("change_user_assignment", {"user_id": "u1", "assignment_id": "a1", "action": "revoke"}, {}, "Revoked assignment a1 from user u1."),
        ("create_oidc_application", {"client_id": "portal", "name": "Portal"}, {"_id": "portal"}, "Created OIDC application Portal (client ID portal)."),
        ("create_saml_application", {"name": "Payroll"}, {"_id": "app1"}, "Created SAML application Payroll (ID app1)."),
        ("get_user", {"user_id": "u1"}, {"userName": "alice", "_id": "u1", "accountStatus": "active", "mail": "a@example.test"}, "Account alice (ID u1): status active, email a@example.test."),
        ("list_user_groups", {"user_id": "u1", "kind": "authzRoles"}, {"result": [{}, {}]}, "Roles for user u1: 2 record(s)."),
        ("get_user_activity", {"username": "alice", "source": "am-access"}, {"result": [{}, {}, {}]}, "Recent am-access activity for alice: 3 event(s)."),
    ],
)
def test_receipts_are_one_readable_line(name, args, data, expected):
    text = describe_result(ToolResult(name, {"status": "success", "data": data}, args))
    assert text == expected
    assert "{" not in text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("email alice.smith@example.test.", "email `alice.smith@example.test`."),
        ("alice@example.com · alice", "`alice@example.com` · alice"),
        ("a@b.co and c.d+tag@sub.example.org!", "`a@b.co` and `c.d+tag@sub.example.org`!"),
        # Already a code span, not an address, or a generated password: unchanged.
        ("see `bob@example.com`", "see `bob@example.com`"),
        ("user@host and managed/alpha_group/3f2a", "user@host and managed/alpha_group/3f2a"),
        ("New password: aB3-kd_Lm9", "New password: aB3-kd_Lm9"),
        ("", ""),
    ],
)
def test_email_addresses_are_sent_as_code_so_clients_do_not_link_them(text, expected):
    assert unlinked(text) == expected
    # Applying it twice changes nothing more.
    assert unlinked(unlinked(text)) == expected


def test_opened_account_line_leaves_the_email_to_the_panel():
    account = ToolResult(
        "get_user",
        {
            "status": "success",
            "data": {
                "userName": "alice",
                "_id": "u1",
                "accountStatus": "active",
                "mail": "a@example.test",
            },
        },
        {"user_id": "u1"},
    )
    assert describe_result(account, brief=True) == "Account alice (ID u1): status active."
    assert "a@example.test" in describe_result(account)


UUID = "b0f30dfb-4e01-457e-a567-c258a74e4fe2"


def test_usernames_are_learned_from_any_user_record():
    seen = [
        ToolResult("get_user", {"status": "success", "data": {"_id": UUID, "userName": "alice"}}),
        ToolResult(
            "search_users",
            {"status": "success", "data": {"result": [{"_id": "u2", "userName": "bob"}, "junk"]}},
        ),
        ToolResult(
            "reset_user_password",
            {"status": "success", "data": {"user": {"_id": "u3", "userName": "carol"}}},
        ),
        # A relationship has an _id too, but it is not an account.
        ToolResult(
            "list_user_groups",
            {"status": "success", "data": {"result": [{"_id": "rel-1", "name": "Help desk"}]}},
        ),
        ToolResult("get_user", {"status": "error", "data": {"_id": "u4", "userName": "dave"}}),
    ]
    assert account_names(seen) == {UUID: "alice", "u2": "bob", "u3": "carol"}


@pytest.mark.parametrize(
    ("name", "args", "data", "expected"),
    [
        ("change_user_membership", {"user_id": UUID, "kind": "groups", "membership_id": "g2", "action": "add"}, {}, "Added group Contractors to user alice."),
        ("change_user_membership", {"user_id": UUID, "kind": "authzRoles", "membership_id": "internal/role/r1", "action": "remove"}, {}, "Removed role Help desk admin from user alice."),
        ("change_user_assignment", {"user_id": UUID, "assignment_id": "a1", "action": "grant"}, {}, "Granted assignment Portal access to user alice."),
        ("unenroll_mfa_device", {"user_id": UUID, "method": "push"}, {"count": 1}, "Removed 1 push MFA device(s) from user alice."),
        ("reset_user_password", {"user_id": UUID}, {}, "Password reset for alice."),
        ("list_user_groups", {"user_id": UUID, "kind": "groups"}, {"result": []}, "Groups for user alice: 0 record(s)."),
        ("list_user_assignments", {"user_id": UUID}, {"result": []}, "Assignments for user alice: 0 record(s)."),
        ("list_user_sessions", {"user_id": UUID}, {"result": []}, "Active sessions for user alice: 0."),
    ],
)
def test_receipts_name_the_account_and_the_record_instead_of_their_ids(name, args, data, expected):
    names = {
        UUID: "alice",
        "g2": "Contractors",
        "internal/role/r1": "Help desk admin",
        "a1": "Portal access",
    }
    item = ToolResult(name, {"status": "success", "data": data}, args)
    assert describe_result(item, names=names) == expected
    assert UUID not in describe_result(item, names=names)
    # Without known names the IDs still identify what happened.
    assert UUID in describe_result(item)


def test_a_status_change_names_the_account_from_its_own_response():
    activated = ToolResult(
        "set_user_status",
        {"status": "success", "data": {"_id": UUID, "userName": "alice", "accountStatus": "active"}},
        {"user_id": UUID, "status": "active"},
    )
    assert describe_result(activated) == "Account alice is now active."


def test_session_text_counts_and_never_shows_a_handle():
    sessions = ToolResult(
        "list_user_sessions",
        {
            "status": "success",
            "data": {
                "result": [
                    {"latestAccessTime": "2024-01-15T07:42:42.544Z"},
                    {"latestAccessTime": "2024-01-15T06:00:00Z"},
                ]
            },
        },
        {"user_id": "u1"},
    )
    assert describe_result(sessions, brief=True) == "Active sessions for user u1: 2."
    assert describe_result(sessions) == (
        "Active sessions for user u1: 2. "
        "Last used: 2024-01-15T07:42:42.544Z, 2024-01-15T06:00:00Z."
    )
    none = ToolResult(
        "list_user_sessions", {"status": "success", "data": {"result": []}}, {"user_id": "u1"}
    )
    assert describe_result(none) == "Active sessions for user u1: 0."


def test_relationship_text_names_the_records_for_text_clients_only():
    groups = ToolResult(
        "list_user_groups",
        {
            "status": "success",
            "data": {"result": [{"name": "Help desk"}, {"name": "Contractors"}, {}]},
        },
        {"user_id": "u1", "kind": "groups"},
    )
    assert describe_result(groups) == (
        "Groups for user u1: 3 record(s). Help desk, Contractors."
    )
    # The panel already lists them, so the chat line stays short there.
    assert describe_result(groups, brief=True) == "Groups for user u1: 3 record(s)."
    many = ToolResult(
        "list_user_assignments",
        {
            "status": "success",
            "data": {"result": [{"name": f"A{i}"} for i in range(12)]},
        },
        {"user_id": "u1"},
    )
    assert describe_result(many).endswith("A8, A9, and 2 more.")


def test_search_text_is_brief_for_panel_clients_and_listed_for_text_clients():
    item = ToolResult(
        "search_users",
        {"status": "success", "data": {"result": [{"userName": "alice", "_id": "u1"}]}},
        {"query": "ali"},
    )
    assert describe_result(item, brief=True) == (
        "Found 1 matching users. Open an account in the panel to continue."
    )
    assert "ID: u1" in describe_result(item)


def test_password_receipt_keeps_the_password_and_change_summaries_redact_it():
    reset = ToolResult(
        "reset_user_password",
        {"status": "success", "data": {"new_password": "Xy-9"}},
        {"user_id": "u1"},
    )
    assert describe_result(reset) == "Password reset for u1. New password: Xy-9"
    summary = describe_change("reset_user_password", {"user_id": "u1", "new_password": "Xy-9"})
    assert summary == "Reset password: user id u1, new password [redacted]"


def test_errors_and_rejections_are_labeled():
    failed = ToolResult("set_user_status", {"status": "error", "message": "Unauthorized (401)"})
    assert describe_result(failed) == "Change account status: Unauthorized (401)"
    rejected = ToolResult("set_user_status", {"status": "rejected"})
    assert describe_result(rejected) == "Change account status: rejected; no change was made."
