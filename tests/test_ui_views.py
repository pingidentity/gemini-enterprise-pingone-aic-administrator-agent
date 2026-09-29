"""Panel layout: toolbar first, scannable results, a clear action hierarchy."""

import json

import pytest

from ping_admin_agent.ui_protocol import BASIC_CATALOG_ID, GEMINI_CATALOG_ID
from ping_admin_agent.ui_results import RuntimeResult, ToolResult
from ping_admin_agent.ui_views import render_result, working_update

USER = {
    "_id": "u1",
    "userName": "alice",
    "givenName": "Alice",
    "sn": "Smith",
    "displayName": "Alice Smith",
    "mail": "alice@example.com",
    "accountStatus": "active",
    "password": "DO_NOT_RENDER",
}
BOB = {
    **USER,
    "_id": "u2",
    "userName": "bob",
    "displayName": "Bob Ray",
    "accountStatus": "inactive",
}


def components_of(messages):
    update = next(m["updateComponents"] for m in messages if "updateComponents" in m)
    return {c["id"]: c for c in update["components"]}


def texts(messages):
    value = next(
        m["updateDataModel"]["value"] for m in messages if "updateDataModel" in m
    )
    return list(value.get("display", {}).values())


def icons(comps):
    return [c["icon"] for c in comps.values() if c["component"] == "MaterialIcon"]


def by_action(comps, name):
    return [
        c
        for c in comps.values()
        if c.get("action", {}).get("event", {}).get("name") == f"pingaic.{name}"
    ]


def content_of(comps):
    """The children of the panel's content column, in order."""
    return [comps[i] for i in comps[comps["root"]["children"][0]]["children"]]


def observed(name, data, args):
    return ToolResult(name, {"status": "success", "data": data}, args)


def test_workspace_puts_the_toolbar_first_and_secondary_tools_last(monkeypatch):
    monkeypatch.delenv("PING_ADMIN_CANVAS_LOGO", raising=False)
    comps = components_of(
        render_result(RuntimeResult(), "s", GEMINI_CATALOG_ID).messages()
    )
    header, toolbar, *_, last = content_of(comps)
    assert header["component"] == "MaterialRow"
    # The canvas drops external images, so an icon anchors the header by default.
    marks = [comps[i]["component"] for i in header["children"]]
    assert "MaterialIcon" in marks and not {"Image", "MaterialImage"} & set(marks)
    assert toolbar["component"] == "MaterialCard"
    (search,) = by_action(comps, "read.search_users")
    assert search["appearance"] == "filled" and search["leadingIcon"] == "search"
    assert last["component"] == "MaterialExpansionPanel"
    assert last["title"] == "Applications" and last["expanded"] is False


def search_parts(result):
    surface = render_result(result, "s", GEMINI_CATALOG_ID)
    messages = surface.messages()
    comps = components_of(messages)
    (button,) = by_action(comps, "read.search_users")
    (field,) = [c for c in comps.values() if c.get("label") == "Search users"]
    holder = content_of(comps)[1]
    return messages, comps, holder, field, button


def test_search_is_the_titled_task_on_the_start_screen():
    messages, comps, holder, field, button = search_parts(RuntimeResult())
    # A visible title, a short label, and the hint where a hint belongs.
    assert holder["component"] == "MaterialCard" and "Find a user" in texts(messages)
    assert field["placeholder"] == "Name, username or email"
    # Nothing else is on this screen, so Search is its one filled button.
    assert button["appearance"] == "filled"
    filled = [c for c in comps.values() if c.get("appearance") == "filled"]
    assert filled == [button]


def test_search_stays_open_among_results_but_lets_open_lead():
    results = RuntimeResult(
        observations=[observed("search_users", {"result": [USER, BOB]}, {"query": "s"})]
    )
    messages, comps, holder, _, button = search_parts(results)
    assert holder["component"] == "MaterialCard" and "Find a user" in texts(messages)
    assert button["appearance"] == "tonal"
    # The rows' Open buttons are the filled ones here.
    filled = {c["label"] for c in comps.values() if c.get("appearance") == "filled"}
    assert filled == {"Open"}


@pytest.mark.parametrize(
    "newest",
    [
        observed("get_user", USER, {"user_id": "u1"}),
        observed(
            "list_user_groups", {"result": []}, {"user_id": "u1", "kind": "groups"}
        ),
        observed("list_user_sessions", {"result": []}, {"user_id": "u1"}),
        ToolResult(
            "set_user_status", {"status": "error", "message": "x"}, {"user_id": "u1"}
        ),
    ],
)
def test_search_folds_into_one_line_once_an_account_is_the_task(newest):
    messages, comps, holder, field, button = search_parts(
        RuntimeResult(observations=[newest])
    )
    # One line under the header, closed until it is clicked.
    assert holder["component"] == "MaterialExpansionPanel"
    assert holder["title"] == "Find another user" and holder["expanded"] is False
    assert "description" not in holder and "Find a user" not in texts(messages)
    # The field and its button are inside it, ready when it opens.
    (row,) = [comps[i] for i in holder["children"]]
    assert row["children"] == [field["id"], button["id"]]
    assert button["appearance"] == "tonal"


def test_forms_and_reviews_have_no_search_to_distract():
    for result in (
        RuntimeResult(view="set_user_status", form_values={"user_id": "u1"}),
        RuntimeResult(
            confirmations=[
                {
                    "id": "t",
                    "tool": {"name": "set_user_status", "args": {"user_id": "u1"}},
                }
            ]
        ),
    ):
        comps = components_of(render_result(result, "s", GEMINI_CATALOG_ID).messages())
        assert not by_action(comps, "read.search_users")


def test_basic_catalog_search_carries_its_hint_in_the_label():
    comps = components_of(
        render_result(RuntimeResult(), "s", BASIC_CATALOG_ID).messages()
    )
    labels = [c["label"] for c in comps.values() if c["component"] == "TextField"]
    assert "Search users: name, username or email" in labels


def test_header_shows_the_logo_only_when_opted_in(monkeypatch):
    monkeypatch.setenv("PING_ADMIN_CANVAS_LOGO", "true")
    comps = components_of(
        render_result(RuntimeResult(), "s", GEMINI_CATALOG_ID).messages()
    )
    header = content_of(comps)[0]
    marks = [comps[i]["component"] for i in header["children"]]
    # The basic Image, never MaterialImage: that one breaks on external URLs.
    assert "Image" in marks and "MaterialIcon" not in marks
    assert not any(c["component"] == "MaterialImage" for c in comps.values())


def test_search_results_are_compact_rows_with_one_open_action():
    result = RuntimeResult(
        observations=[
            observed("search_users", {"result": [USER, BOB]}, {"query": "smith"})
        ]
    )
    messages = render_result(result, "s", GEMINI_CATALOG_ID).messages()
    comps = components_of(messages)
    opens = by_action(comps, "read.get_user")
    assert len(opens) == 2 and all(o["leadingIcon"] == "open_in_new" for o in opens)
    assert not by_action(comps, "read.list_user_groups")
    assert icons(comps).count("check_circle") == 1 and icons(comps).count("block") == 1
    assert "2 matching users" in texts(messages)
    assert "DO_NOT_RENDER" not in json.dumps(messages)


def test_opened_account_groups_actions_and_marks_risky_ones():
    result = RuntimeResult(observations=[observed("get_user", USER, {"user_id": "u1"})])
    messages = render_result(result, "s", GEMINI_CATALOG_ID).messages()
    comps = components_of(messages)
    assert all(
        b["appearance"] == "tonal" for b in by_action(comps, "read.list_user_groups")
    )
    (status,) = by_action(comps, "view.set_user_status")
    assert status["appearance"] == "outlined" and status["leadingIcon"] == "toggle_on"
    (reset,) = by_action(comps, "prepare.reset_user_password")
    assert reset["color"] == "warn" and reset["leadingIcon"] == "lock_reset"
    # The address is a code span, which Markdown renderers never turn into a link.
    assert "`alice@example.com`" in texts(messages)
    assert "alice@example.com" not in texts(messages)
    assert {"User ID", "Username", "Email", "Investigate", "Changes"} <= set(
        texts(messages)
    )
    # First and last name, in that order, above the identifying fields.
    shown = texts(messages)
    assert {"First name", "Alice", "Last name", "Smith"} <= set(shown)
    assert (
        shown.index("First name") < shown.index("Last name") < shown.index("Username")
    )
    # A record without them shows "Not available" rather than dropping the rows.
    partial = {k: v for k, v in USER.items() if k not in {"givenName", "sn"}}
    result = RuntimeResult(
        observations=[observed("get_user", partial, {"user_id": "u1"})]
    )
    shown = texts(render_result(result, "s", GEMINI_CATALOG_ID).messages())
    assert (
        "First name" in shown
        and shown[shown.index("First name") + 1] == "Not available"
    )
    assert "alice · active" in texts(messages)
    assert "DO_NOT_RENDER" not in json.dumps(messages)


def test_review_emphasizes_approve_and_warns_on_destructive_changes():
    result = RuntimeResult(
        confirmations=[
            {
                "id": "tok",
                "tool": {"name": "reset_user_password", "args": {"user_id": "u1"}},
            }
        ]
    )
    comps = components_of(render_result(result, "s", GEMINI_CATALOG_ID).messages())
    (approve,) = by_action(comps, "confirm.approve")
    (reject,) = by_action(comps, "confirm.reject")
    assert approve["appearance"] == "filled" and approve["color"] == "primary"
    assert approve["leadingIcon"] == "check"
    assert reject["appearance"] == "outlined" and reject["color"] == "warn"
    assert "warning" in icons(comps)


def test_receipt_is_a_banner_with_a_next_step_and_no_password():
    result = RuntimeResult(
        observations=[
            observed(
                "reset_user_password",
                {"user": USER, "new_password": "Secret-1"},
                {"user_id": "u1"},
            )
        ]
    )
    messages = render_result(result, "s", GEMINI_CATALOG_ID).messages()
    comps = components_of(messages)
    assert "Secret-1" not in json.dumps(messages)
    assert any("new password is in the chat response" in t for t in texts(messages))
    assert "check_circle" in icons(comps)
    (open_account,) = by_action(comps, "read.get_user")
    assert open_account["label"] == "Open account"


def test_errors_and_rejections_become_banners():
    failed = ToolResult(
        "set_user_status", {"status": "error", "message": "boom"}, {"user_id": "u1"}
    )
    rejected = ToolResult("set_user_status", {"status": "rejected"}, {"user_id": "u2"})
    messages = render_result(
        RuntimeResult(observations=[failed, rejected]), "s", GEMINI_CATALOG_ID
    ).messages()
    comps = components_of(messages)
    assert any("Change account status: boom" in t for t in texts(messages))
    assert any("rejected; no change was made" in t for t in texts(messages))
    assert "error" in icons(comps) and "info" in icons(comps)


def test_forms_use_human_labels_and_a_text_back_button():
    result = RuntimeResult(
        view="set_user_status",
        form_values={"user_id": "u1", "user_label": "Alice Smith"},
    )
    messages = render_result(result, "s", GEMINI_CATALOG_ID).messages()
    comps = components_of(messages)
    (status,) = [c for c in comps.values() if c["component"] == "MaterialSelect"]
    assert status["label"] == "New account status"
    assert [o["label"] for o in status["options"]] == ["Active", "Inactive"]
    assert "Applies to Alice Smith" in texts(messages)
    # An account form leads back to the account card, not to an empty workspace.
    (back,) = by_action(comps, "read.get_user")
    assert back["label"] == "Back to account" and not by_action(comps, "view.workspace")
    assert back["appearance"] == "text" and back["leadingIcon"] == "arrow_back"
    (review,) = by_action(comps, "prepare.set_user_status")
    assert review["appearance"] == "filled"
    # An application form has no account, so it still returns to the workspace.
    app_form = components_of(
        render_result(
            RuntimeResult(view="create_oidc_application"), "s", GEMINI_CATALOG_ID
        ).messages()
    )
    (to_workspace,) = by_action(app_form, "view.workspace")
    assert to_workspace["label"] == "Back to workspace"
    assert not by_action(app_form, "read.get_user")


def test_user_card_offers_groups_and_roles_as_separate_changes():
    result = RuntimeResult(observations=[observed("get_user", USER, {"user_id": "u1"})])
    surface = render_result(result, "s", GEMINI_CATALOG_ID)
    comps = components_of(surface.messages())
    buttons = {c["label"]: c for c in by_action(comps, "view.change_user_membership")}
    assert list(buttons) == ["Change groups", "Change roles"]
    assert "Change membership" not in [c.get("label") for c in comps.values()]
    told = {
        spec.command.args["kind"]: spec.command.args
        for spec in surface.actions.values()
        if spec.command.target == "change_user_membership"
    }
    # Each button opens its own panel for this exact account.
    assert told == {
        "groups": {"user_id": "u1", "user_label": "Alice Smith", "kind": "groups"},
        "authzRoles": {
            "user_id": "u1",
            "user_label": "Alice Smith",
            "kind": "authzRoles",
        },
    }


CHOICES = {
    "kind": "groups",
    "available": [
        {"id": "g2", "name": "Contractors"},
        {"id": "g3", "name": "Finance"},
        {"id": "g4", "name": "Finance"},
    ],
    "member": [{"id": "managed/alpha_group/g1", "name": "Help desk"}],
    "truncated": False,
    "filter": "",
}
ROLE_CHOICES = {
    "kind": "authzRoles",
    "available": [{"id": "openidm-admin", "name": "openidm-admin"}],
    "member": [
        {"id": "internal/role/openidm-authorized", "name": "openidm-authorized"}
    ],
    "truncated": False,
    "filter": "",
}


def membership_form(choices, **values):
    result = RuntimeResult(
        view="change_user_membership",
        form_values={
            "user_id": "u1",
            "user_label": "Alice Smith",
            "kind": choices.get("kind", "groups"),
            **values,
        },
        choices=choices,
    )
    return render_result(result, "s", GEMINI_CATALOG_ID)


def test_groups_panel_lists_current_groups_with_an_x_and_a_picker_to_add():
    surface = membership_form(CHOICES)
    messages = surface.messages()
    comps = components_of(messages)
    assert "Change groups" in texts(messages) and "Current groups" in texts(messages)
    # Each current group is a row with its name and an X that removes it.
    (remove,) = [c for c in comps.values() if c["component"] == "MaterialIconButton"]
    assert remove["icon"] == "close" and remove["color"] == "warn"
    assert remove["ariaLabel"] == remove["tooltip"] == "Remove Help desk"
    (held,) = [c for c in comps.values() if c.get("tooltip", "").startswith("ID: ")]
    assert held["label"] == "Help desk"
    (row,) = [c for c in comps.values() if remove["id"] in c.get("children", [])]
    assert row["children"][-1] == remove["id"]
    spec = surface.actions[remove["id"]]
    # The X is fixed on the server to this user, this kind and this exact reference.
    assert spec.once and not spec.fields
    assert spec.command.kind == "prepare" and spec.command.args == {
        "user_id": "u1",
        "kind": "groups",
        "membership_id": "managed/alpha_group/g1",
        "action": "remove",
    }
    # Adding picks from every group the user is not in; a repeated name shows its ID.
    (add,) = [c for c in comps.values() if c["component"] == "MaterialSelect"]
    assert add["label"] == "Group to add" and add["options"] == [
        {"value": "", "label": "Choose a group"},
        {"value": "g2", "label": "Contractors"},
        {"value": "g3", "label": "Finance (g3)"},
        {"value": "g4", "label": "Finance (g4)"},
    ]
    form = next(
        m["updateDataModel"]["value"]["form"]
        for m in messages
        if "updateDataModel" in m
    )
    assert form["add_member"] == ""
    (review,) = [c for c in comps.values() if c.get("label") == "Review add"]
    spec = surface.actions[review["id"]]
    assert spec.command.args == {"user_id": "u1", "kind": "groups", "action": "add"}
    (field,) = spec.fields.values()
    assert field.arg == "membership_id" and field.choices == ("g2", "g3", "g4")
    assert "3 groups can be added." in texts(messages)
    # No dropdown to remove, no type selector and no ID box: the panel is one kind.
    assert not [
        c
        for c in comps.values()
        if c.get("label") in {"Membership type", "Action", "Group ID"}
    ]
    (back,) = by_action(comps, "read.get_user")
    assert back["label"] == "Back to account"


def test_roles_panel_is_the_same_panel_worded_for_roles():
    surface = membership_form(ROLE_CHOICES)
    messages = surface.messages()
    comps = components_of(messages)
    shown = texts(messages)
    assert {
        "Change roles",
        "Current roles",
        "Add a role",
        "1 role can be added.",
    } <= set(shown)
    assert "Preparing: Change roles" in shown
    (remove,) = [c for c in comps.values() if c["component"] == "MaterialIconButton"]
    assert remove["ariaLabel"] == "Remove openidm-authorized"
    assert surface.actions[remove["id"]].command.args == {
        "user_id": "u1",
        "kind": "authzRoles",
        "membership_id": "internal/role/openidm-authorized",
        "action": "remove",
    }
    (add,) = [c for c in comps.values() if c["component"] == "MaterialSelect"]
    assert (
        add["label"] == "Role to add" and add["options"][0]["label"] == "Choose a role"
    )
    assert "badge" in icons(comps) and "group" not in icons(comps)
    # The opener card in the chat names the panel too.
    assert comps["root"]["cardTitle"] == "Change roles"
    assert (
        components_of(membership_form(CHOICES).messages())["root"]["cardTitle"]
        == "Change groups"
    )


def test_membership_panel_offers_a_name_filter_when_the_list_is_long():
    comps = components_of(membership_form({**CHOICES, "truncated": True}).messages())
    (narrow,) = by_action(comps, "view.change_user_membership")
    assert narrow["label"] == "Filter"
    surface = membership_form({**ROLE_CHOICES, "filter": "adm"}, name_filter="adm")
    messages = surface.messages()
    assert any("matching “adm”" in t for t in texts(messages))
    # The filter keeps the account and the kind it applies to, on the server.
    (spec,) = [s for s in surface.actions.values() if s.command.kind == "view"]
    assert spec.command.args == {
        "user_id": "u1",
        "user_label": "Alice Smith",
        "kind": "authzRoles",
    }
    assert list(spec.fields) == ["name_filter"]


def test_membership_panel_handles_empty_lists_and_a_failed_listing():
    empty = {
        "kind": "authzRoles",
        "available": [],
        "member": [],
        "truncated": False,
        "filter": "",
    }
    messages = membership_form(empty).messages()
    comps = components_of(messages)
    assert not [
        c
        for c in comps.values()
        if c["component"] in {"MaterialSelect", "MaterialIconButton"}
    ]
    assert "Alice Smith does not belong to any role." in texts(messages)
    assert "No role can be added." in texts(messages)

    failed = membership_form(
        {"kind": "groups", "error": "GET groups returned 403: denied"}
    )
    messages = failed.messages()
    comps = components_of(messages)
    assert any("The groups could not be listed" in t for t in texts(messages))
    # Typing an ID is the way through, for this kind only.
    (review,) = by_action(comps, "prepare.change_user_membership")
    assert review["label"] == "Review change" and review["appearance"] == "filled"
    spec = failed.actions[review["id"]]
    assert spec.command.args == {"user_id": "u1", "kind": "groups"}
    assert sorted(spec.fields) == ["action", "membership_id"]
    assert [
        c["label"] for c in comps.values() if c["component"] == "MaterialInput"
    ] == ["Group ID"]


def test_review_and_receipt_name_the_picked_group():
    names = {"g2": "Contractors"}
    args = {"user_id": "u1", "kind": "groups", "membership_id": "g2", "action": "add"}
    review = RuntimeResult(
        confirmations=[
            {"id": "t", "tool": {"name": "change_user_membership", "args": args}}
        ],
        names=names,
    )
    shown = texts(render_result(review, "s", GEMINI_CATALOG_ID).messages())
    assert {"Membership name", "Contractors", "Membership id", "g2"} <= set(shown)
    removing = RuntimeResult(
        confirmations=[
            {
                "id": "t",
                "tool": {
                    "name": "change_user_membership",
                    "args": {
                        **args,
                        "membership_id": "managed/alpha_group/g1",
                        "action": "remove",
                    },
                },
            }
        ],
        names={"managed/alpha_group/g1": "Help desk"},
    )
    assert "Help desk" in texts(
        render_result(removing, "s", GEMINI_CATALOG_ID).messages()
    )
    done = RuntimeResult(
        observations=[observed("change_user_membership", {}, args)], names=names
    )
    shown = texts(render_result(done, "s", GEMINI_CATALOG_ID).messages())
    assert "Added group Contractors to user u1." in shown
    # Without a known name the ID is shown, and an ID never rewrites part of a word.
    unnamed = RuntimeResult(observations=[observed("change_user_membership", {}, args)])
    assert "Added group g2 to user u1." in texts(
        render_result(unnamed, "s", GEMINI_CATALOG_ID).messages()
    )


def test_account_card_does_not_offer_mfa_removal_for_now():
    result = RuntimeResult(observations=[observed("get_user", USER, {"user_id": "u1"})])
    comps = components_of(render_result(result, "s", GEMINI_CATALOG_ID).messages())
    assert not by_action(comps, "view.unenroll_mfa_device")
    assert "Remove MFA devices" not in [c.get("label") for c in comps.values()]
    # The other changes are still one click away.
    for action in (
        "view.set_user_status",
        "view.change_user_membership",
        "view.change_user_assignment",
        "prepare.reset_user_password",
        "prepare.logout_user_sessions",
    ):
        assert by_action(comps, action), action


def test_account_card_offers_sessions_under_investigate():
    result = RuntimeResult(observations=[observed("get_user", USER, {"user_id": "u1"})])
    surface = render_result(result, "s", GEMINI_CATALOG_ID)
    comps = components_of(surface.messages())
    (sessions,) = by_action(comps, "read.list_user_sessions")
    assert sessions["label"] == "Sessions" and sessions["leadingIcon"] == "devices"
    # Same quiet style as the other Investigate buttons, in the same row.
    assert sessions["appearance"] == "tonal"
    (row,) = [c for c in comps.values() if sessions["id"] in c.get("children", [])]
    labels = [comps[i]["label"] for i in row["children"]]
    assert labels == ["Groups", "Roles", "Assignments", "Sessions", "Recent activity"]
    (command,) = [
        spec.command
        for spec in surface.actions.values()
        if spec.command.target == "list_user_sessions"
    ]
    assert command.kind == "read" and command.args == {"user_id": "u1"}


def test_sessions_view_shows_times_and_a_way_back_but_no_handle():
    listed = {
        "result": [
            {
                "latestAccessTime": "2024-01-15T07:42:42.544Z",
                "maxIdleExpirationTime": "2024-01-15T08:12:42Z",
                "maxSessionExpirationTime": "not-a-date",
                "realm": "/alpha",
            }
        ]
    }
    result = RuntimeResult(
        observations=[observed("list_user_sessions", listed, {"user_id": "u1"})]
    )
    messages = render_result(result, "s", GEMINI_CATALOG_ID).messages()
    comps = components_of(messages)
    model = next(
        m["updateDataModel"]["value"] for m in messages if "updateDataModel" in m
    )
    (table,) = model["tables"].values()
    # Readable times; a value that is not a timestamp is shown as it came.
    assert table == [
        {
            "used": "2024-01-15 07:42 UTC",
            "idle": "2024-01-15 08:12 UTC",
            "ends": "not-a-date",
        }
    ]
    assert "Active sessions · u1" in texts(messages)
    assert any(t.startswith("1 active session.") for t in texts(messages))
    (back,) = by_action(comps, "read.get_user")
    assert back["label"] == "Back to account"
    (refresh,) = by_action(comps, "read.list_user_sessions")
    assert refresh["label"] == "Refresh"
    assert "shandle" not in json.dumps(messages)
    # It reads only: ending sessions stays on the account card, behind a review.
    assert not by_action(comps, "prepare.logout_user_sessions")

    none = RuntimeResult(
        observations=[observed("list_user_sessions", {"result": []}, {"user_id": "u1"})]
    )
    shown = texts(render_result(none, "s", GEMINI_CATALOG_ID).messages())
    assert "This user has no active sessions." in shown
    assert any(t.startswith("0 active sessions.") for t in shown)


def test_result_screens_name_the_account_instead_of_its_id():
    names = {"u1": "alice"}
    for tool, args, title in (
        ("list_user_groups", {"user_id": "u1", "kind": "groups"}, "Groups · alice"),
        (
            "list_user_groups",
            {"user_id": "u1", "kind": "authzRoles"},
            "Authorization roles · alice",
        ),
        ("list_user_assignments", {"user_id": "u1"}, "Assignments · alice"),
        ("list_user_sessions", {"user_id": "u1"}, "Active sessions · alice"),
    ):
        result = RuntimeResult(
            observations=[observed(tool, {"result": []}, args)], names=names
        )
        shown = texts(render_result(result, "s", GEMINI_CATALOG_ID).messages())
        assert title in shown and not any("· u1" in t for t in shown)
        # Before the account has been seen, its ID is all there is to show.
        unnamed = RuntimeResult(observations=[observed(tool, {"result": []}, args)])
        assert title.replace("alice", "u1") in texts(
            render_result(unnamed, "s", GEMINI_CATALOG_ID).messages()
        )

    reset = RuntimeResult(
        observations=[
            observed(
                "reset_user_password", {"new_password": "S3cret-x"}, {"user_id": "u1"}
            )
        ],
        names=names,
    )
    shown = texts(render_result(reset, "s", GEMINI_CATALOG_ID).messages())
    assert (
        "Password reset for alice. The new password is in the chat response." in shown
    )

    earlier = RuntimeResult(
        observations=[
            observed(
                "list_user_groups", {"result": []}, {"user_id": "u1", "kind": "groups"}
            ),
            observed("list_user_sessions", {"result": []}, {"user_id": "u1"}),
        ],
        names=names,
    )
    comps = components_of(render_result(earlier, "s", GEMINI_CATALOG_ID).messages())
    assert "Groups & roles · alice" in [c.get("title") for c in comps.values()]


def test_review_leads_with_the_username_and_keeps_the_exact_id():
    review = RuntimeResult(
        confirmations=[
            {
                "id": "t",
                "tool": {
                    "name": "set_user_status",
                    "args": {"user_id": "u1", "status": "active"},
                },
            }
        ],
        names={"u1": "alice"},
    )
    shown = texts(render_result(review, "s", GEMINI_CATALOG_ID).messages())
    assert shown.index("User name") < shown.index("User id")
    assert {"alice", "u1", "active"} <= set(shown)


def test_drill_down_views_lead_back_to_the_account():
    groups = RuntimeResult(
        observations=[
            observed(
                "list_user_groups", {"result": []}, {"user_id": "u1", "kind": "groups"}
            )
        ]
    )
    comps = components_of(render_result(groups, "s", GEMINI_CATALOG_ID).messages())
    (back,) = by_action(comps, "read.get_user")
    assert back["label"] == "Back to account" and back["leadingIcon"] == "arrow_back"
    assert back["appearance"] == "text"
    # It sits above the table, where a long result cannot push it out of reach.
    view = content_of(comps)[2]
    assert comps[view["children"][0]]["children"] == [back["id"]]

    # The activity tool only knows a username; the origin carries the account.
    activity = ToolResult(
        "get_user_activity",
        {"status": "success", "data": {"result": []}},
        {"username": "alice"},
        {"user_id": "u1"},
    )
    surface = render_result(
        RuntimeResult(observations=[activity]), "s", GEMINI_CATALOG_ID
    )
    comps = components_of(surface.messages())
    (back,) = by_action(comps, "read.get_user")
    assert back["label"] == "Back to account"
    view = content_of(comps)[2]
    assert comps[view["children"][0]]["children"] == [back["id"]]
    # Refreshing the activity keeps the way back, and the tool never sees the ID.
    (refresh,) = [
        spec.command
        for spec in surface.actions.values()
        if spec.command.target == "get_user_activity"
    ]
    assert refresh.origin == {"user_id": "u1"} and "user_id" not in refresh.args

    # Activity requested in chat has no account to return to.
    typed = ToolResult(
        "get_user_activity",
        {"status": "success", "data": {"result": []}},
        {"username": "alice"},
    )
    comps = components_of(
        render_result(
            RuntimeResult(observations=[typed]), "s", GEMINI_CATALOG_ID
        ).messages()
    )
    assert not by_action(comps, "read.get_user")


RECORDS = [
    {
        "_refResourceId": "3f2a",
        "_ref": "managed/alpha_group/3f2a",
        "name": "Help desk administrators",
        "description": "Tier 1 support",
    },
    {"_refResourceId": "9c1e", "_ref": "managed/alpha_group/9c1e"},
]


@pytest.mark.parametrize(
    ("tool", "args", "title", "icon"),
    [
        (
            "list_user_groups",
            {"user_id": "u1", "kind": "groups"},
            "Groups · u1",
            "group",
        ),
        (
            "list_user_groups",
            {"user_id": "u1", "kind": "authzRoles"},
            "Authorization roles · u1",
            "badge",
        ),
        ("list_user_assignments", {"user_id": "u1"}, "Assignments · u1", "assignment"),
    ],
)
def test_relationship_lists_show_display_names_with_the_id_on_hover(
    tool, args, title, icon
):
    result = RuntimeResult(observations=[observed(tool, {"result": RECORDS}, args)])
    messages = render_result(result, "s", GEMINI_CATALOG_ID).messages()
    comps = components_of(messages)
    labels = {c["label"]: c for c in comps.values() if c.get("tooltip")}
    # The display name is what is listed; its ID shows on hover.
    named = labels["Help desk administrators"]
    assert named["component"] == "MaterialButton" and named["tooltip"] == "ID: 3f2a"
    # It is a label, not a control: it has no action to dispatch.
    assert "action" not in named and named["appearance"] == "text"
    # A record IDM returned without a name is still listed, by its ID.
    assert labels["9c1e"]["tooltip"] == "ID: 9c1e"
    shown = texts(messages)
    assert title in shown and "Tier 1 support" in shown
    # The reference stays selectable, because the change forms ask for the ID.
    assert "managed/alpha_group/3f2a" in shown
    assert icons(comps).count(icon) == 2
    assert not any(c["component"] == "MaterialTable" for c in comps.values())


def test_relationship_lists_handle_none_and_count_correctly():
    empty = RuntimeResult(
        observations=[
            observed("list_user_assignments", {"result": []}, {"user_id": "u1"})
        ]
    )
    shown = texts(render_result(empty, "s", GEMINI_CATALOG_ID).messages())
    assert "This user has no assignments." in shown
    assert any(t.startswith("0 assignments.") for t in shown)
    one = RuntimeResult(
        observations=[
            observed(
                "list_user_groups",
                {"result": RECORDS[:1]},
                {"user_id": "u1", "kind": "groups"},
            )
        ]
    )
    assert any(
        t.startswith("1 group.")
        for t in texts(render_result(one, "s", GEMINI_CATALOG_ID).messages())
    )


def test_relationship_lists_fall_back_to_plain_text_in_the_basic_catalog():
    result = RuntimeResult(
        observations=[
            observed(
                "list_user_groups",
                {"result": RECORDS},
                {"user_id": "u1", "kind": "groups"},
            )
        ]
    )
    messages = render_result(result, "s", BASIC_CATALOG_ID).messages()
    comps = components_of(messages)
    assert not any(c["component"].startswith("Material") for c in comps.values())
    assert not any("tooltip" in c for c in comps.values())
    assert {"Help desk administrators", "managed/alpha_group/3f2a"} <= set(
        texts(messages)
    )


def login_event(second, name, result, **extra):
    return {
        "timestamp": f"2026-09-08T15:48:{second:02}.120Z",
        "type": "application/json",
        "source": "am-authentication",
        "payload": {
            "_id": f"event-{second}",
            "timestamp": f"2026-09-08T15:48:{second:02}.120Z",
            "eventName": name,
            "result": result,
            "principal": ["alice@example.test"],
            "entries": [
                {
                    "info": {
                        "treeName": "Login",
                        "displayName": "Data Store Decision",
                        "nodeOutcome": "false",
                    }
                }
            ],
            **extra,
        },
    }


def activity_result(records, *, live=True, new=None):
    data = {"result": records, **({"new": new} if new is not None else {})}
    return RuntimeResult(
        observations=[
            ToolResult(
                "get_user_activity",
                {"status": "success", "data": data},
                {
                    "username": "alice",
                    "source": "am-authentication",
                    **({"live": True} if live else {}),
                },
                {"user_id": "u1"},
            )
        ]
    )


def test_live_activity_lists_events_newest_first_and_opens_to_their_json():
    records = [
        login_event(1, "AM-NODE-LOGIN-COMPLETED", "FAILED", password="hunter2"),
        login_event(7, "AM-LOGIN-COMPLETED", "SUCCESSFUL"),
    ]
    surface = render_result(activity_result(records, new=1), "s", GEMINI_CATALOG_ID)
    messages = surface.messages()
    comps = components_of(messages)
    panels = [
        c
        for c in comps.values()
        if c["component"] == "MaterialExpansionPanel" and "UTC" in c["title"]
    ]
    # One scannable line per event, newest first, closed until it is clicked.
    assert [p["title"] for p in panels] == [
        "15:48:07 UTC · AM-LOGIN-COMPLETED",
        "15:48:01 UTC · AM-NODE-LOGIN-COMPLETED",
    ]
    assert (
        panels[1]["description"]
        == "FAILED · Login · Data Store Decision · outcome false"
    )
    assert all(p["expanded"] is False for p in panels)
    # Opening one shows its JSON, kept as preformatted text.
    (detail_id,) = panels[1]["children"]
    detail = comps[detail_id]
    assert detail["style"]["whiteSpace"] == "pre-wrap"
    display_model = next(
        m["updateDataModel"]["value"]["display"]
        for m in messages
        if "updateDataModel" in m
    )
    text = display_model[detail["text"]["path"].rsplit("/", 1)[-1]]
    assert text.startswith("```json\n{") and text.endswith("\n```")
    body = json.loads(text[len("```json\n") : -len("\n```")])
    assert body["eventName"] == "AM-NODE-LOGIN-COMPLETED"
    # Inside a code block an address stays as it is, and a secret never shows.
    assert (
        body["principal"] == ["alice@example.test"] and body["password"] == "[redacted]"
    )
    assert "hunter2" not in json.dumps(messages)
    assert "2 events, 1 new." in texts(messages)
    assert not any(
        c["component"] in {"MaterialTable", "MaterialTabs"} for c in comps.values()
    )


def test_live_activity_refresh_keeps_following_the_same_account():
    surface = render_result(
        activity_result([login_event(1, "AM-LOGIN-COMPLETED", "SUCCESSFUL")], new=0),
        "s",
        GEMINI_CATALOG_ID,
    )
    messages = surface.messages()
    comps = components_of(messages)
    (refresh,) = [c for c in by_action(comps, "read.get_user_activity")]
    assert refresh["label"] == "Refresh"
    (spec,) = [
        s for s in surface.actions.values() if s.command.target == "get_user_activity"
    ]
    assert spec.command.args == {"username": "alice", "live": True}
    assert spec.command.origin == {"user_id": "u1"} and list(spec.fields) == ["source"]
    assert "1 event. Nothing new since the last refresh." in texts(messages)
    (back,) = by_action(comps, "read.get_user")
    assert back["label"] == "Back to account"


def test_live_activity_says_what_to_do_when_nothing_has_happened_yet():
    messages = render_result(
        activity_result([], new=0), "s", GEMINI_CATALOG_ID
    ).messages()
    assert "No new events yet. Refresh after the user tries again." in texts(messages)
    comps = components_of(messages)
    assert by_action(comps, "read.get_user_activity") and by_action(
        comps, "read.get_user"
    )


def test_activity_shows_only_the_latest_events_and_dates_a_look_back():
    many = [
        login_event(i % 60, f"EVENT-{i}", "SUCCESSFUL", _id=f"id-{i}")
        for i in range(40)
    ]
    messages = render_result(
        activity_result(many, new=40), "s", GEMINI_CATALOG_ID
    ).messages()
    comps = components_of(messages)
    panels = [
        c
        for c in comps.values()
        if c["component"] == "MaterialExpansionPanel" and "UTC" in c["title"]
    ]
    assert len(panels) == 30 and panels[0]["title"].endswith("EVENT-39")
    assert "40 events, 40 new. Showing the latest 30." in texts(messages)
    # A look back over a day, as the model requests it, shows the date as well.
    looked_back = render_result(
        activity_result(many[:1], live=False), "s", GEMINI_CATALOG_ID
    ).messages()
    (panel,) = [
        c
        for c in components_of(looked_back).values()
        if c["component"] == "MaterialExpansionPanel" and "UTC" in c["title"]
    ]
    assert panel["title"] == "2026-09-08 15:48:00 UTC · EVENT-0"


def test_activity_copes_with_plain_text_and_malformed_entries():
    records = [
        {"timestamp": "not-a-time", "payload": "WARNING something odd"},
        {"payload": '{"eventName": "AM-LOGOUT", "timestamp": "2026-09-08T15:50:00Z"}'},
        {"payload": 42},
    ]
    comps = components_of(
        render_result(
            activity_result(records, new=3), "s", GEMINI_CATALOG_ID
        ).messages()
    )
    titles = [
        c["title"]
        for c in comps.values()
        if c["component"] == "MaterialExpansionPanel"
        and c["title"] not in {"Applications", "Find another user"}
    ]
    assert titles == [
        "Unknown time · Log entry",
        "15:50:00 UTC · AM-LOGOUT",
        "not-a-time · WARNING something odd",
    ]


def test_account_card_remembers_itself_on_the_activity_button():
    surface = render_result(
        RuntimeResult(observations=[observed("get_user", USER, {"user_id": "u1"})]),
        "s",
        GEMINI_CATALOG_ID,
    )
    (activity,) = [
        spec.command
        for spec in surface.actions.values()
        if spec.command.target == "get_user_activity"
    ]
    # The card starts a live tail; the tool still never sees the account ID.
    assert activity.args == {"username": "alice", "live": True}
    assert activity.origin == {"user_id": "u1"}


def test_a_failed_drill_down_still_leads_back_to_the_account():
    failed = ToolResult(
        "get_user_activity",
        {"status": "error", "message": "The monitoring service is unavailable."},
        {"username": "alice"},
        {"user_id": "u1"},
    )
    comps = components_of(
        render_result(
            RuntimeResult(observations=[failed]), "s", GEMINI_CATALOG_ID
        ).messages()
    )
    (back,) = by_action(comps, "read.get_user")
    assert back["label"] == "Back to account"


def test_earlier_results_collapse_and_the_newest_stays_open():
    result = RuntimeResult(
        observations=[
            observed("get_user", USER, {"user_id": "u1"}),
            observed(
                "list_user_groups", {"result": []}, {"user_id": "u1", "kind": "groups"}
            ),
        ]
    )
    comps = components_of(render_result(result, "s", GEMINI_CATALOG_ID).messages())
    _, _, newest, older, _ = content_of(comps)
    assert newest["component"] != "MaterialExpansionPanel"
    assert older["component"] == "MaterialExpansionPanel" and older["expanded"] is False
    assert older["title"] == "User profile · u1"


def test_working_update_shows_progress_in_the_header_and_moves_nothing():
    surface = render_result(RuntimeResult(), "s", GEMINI_CATALOG_ID)
    rendered = components_of(surface.messages())
    canvas = {"id": "s", **surface.canvas}
    header = content_of(rendered)[0]
    (update,) = working_update(canvas, GEMINI_CATALOG_ID)
    assert update["updateComponents"]["surfaceId"] == "s"
    comps = {c["id"]: c for c in update["updateComponents"]["components"]}
    # Only the header's own components travel; the panel root is never touched.
    assert "root" not in comps and len(comps) == 3
    row = comps[header["id"]]
    (spinner_id,) = set(row["children"]) - set(header["children"])
    assert row["children"] == [*header["children"], spinner_id]
    spinner = comps[spinner_id]
    assert spinner["component"] == "MaterialProgressSpinner"
    assert spinner["mode"] == "indeterminate" and spinner["diameter"] <= 24
    (caption,) = [c for c in comps.values() if c["component"] == "MaterialText"]
    assert caption["text"] == "Working on your request"
    assert rendered[caption["id"]]["text"] != caption["text"]
    # Nothing is inserted above or below the content.
    assert not any(c["component"] == "MaterialProgressBar" for c in comps.values())
    # Restoring sends the original header back, unchanged.
    (restore,) = working_update(canvas, GEMINI_CATALOG_ID, working=False)
    restored = {c["id"]: c for c in restore["updateComponents"]["components"]}
    assert restored == {
        header["id"]: header,
        caption["id"]: rendered[caption["id"]],
    }
    # A panel recorded before headers were remembered gets no partial update.
    assert working_update({"id": "s"}, GEMINI_CATALOG_ID) == []


def test_forms_and_reviews_also_show_progress_in_their_header():
    results = [
        RuntimeResult(view="set_user_status", form_values={"user_id": "u1"}),
        RuntimeResult(
            confirmations=[
                {
                    "id": "t",
                    "tool": {"name": "set_user_status", "args": {"user_id": "u1"}},
                }
            ]
        ),
    ]
    for result in results:
        surface = render_result(result, "s", GEMINI_CATALOG_ID)
        (update,) = working_update({"id": "s", **surface.canvas}, GEMINI_CATALOG_ID)
        kinds = [c["component"] for c in update["updateComponents"]["components"]]
        assert kinds == ["MaterialProgressSpinner", "MaterialText", "MaterialRow"]


@pytest.mark.parametrize("catalog", [GEMINI_CATALOG_ID, BASIC_CATALOG_ID])
def test_every_view_renders_in_both_catalogs(catalog):
    results = [
        RuntimeResult(),
        RuntimeResult(
            observations=[
                observed("search_users", {"result": [USER, BOB]}, {"query": "s"})
            ]
        ),
        RuntimeResult(
            observations=[
                observed("get_user", USER, {"user_id": "u1"}),
                observed(
                    "list_user_groups",
                    {"result": []},
                    {"user_id": "u1", "kind": "groups"},
                ),
                ToolResult(
                    "set_user_status",
                    {"status": "error", "message": "x"},
                    {"user_id": "u1"},
                ),
            ]
        ),
        RuntimeResult(
            confirmations=[
                {
                    "id": "t",
                    "tool": {"name": "set_user_status", "args": {"user_id": "u1"}},
                }
            ]
        ),
        *[
            RuntimeResult(view=view, form_values={"user_id": "u1"})
            for view in (
                "set_user_status",
                "change_user_membership",
                "create_oidc_application",
                "create_saml_application",
            )
        ],
    ]
    for result in results:
        messages = render_result(result, "s", catalog).messages()
        assert any("updateComponents" in m for m in messages)
    if catalog == BASIC_CATALOG_ID:
        comps = components_of(render_result(results[2], "s", catalog).messages())
        assert not any(c["component"].startswith("Material") for c in comps.values())
