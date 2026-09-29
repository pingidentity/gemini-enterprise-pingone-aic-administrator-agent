"""AIC workspaces built from known tool results, with two catalog renderings.

The side panel is laid out like a dashboard: a compact header with the current
context, the search toolbar on top, the newest result in full, earlier results
collapsed, and secondary tools at the bottom.
"""

import json
from copy import deepcopy
from datetime import datetime, timezone

from .ui_actions import Field, UiCommand
from .ui_components import Components
from .ui_protocol import (
    WIRE_VERSION,
    canvas_logo_enabled,
    panel_enabled,
    validate_surface,
)
from .ui_results import LABELS, ToolResult, describe_result, review_args

READ_TOOLS = {
    "search_users",
    "get_user",
    "list_user_groups",
    "list_group_members",
    "list_user_assignments",
    "find_application",
    "get_user_activity",
    "list_user_sessions",
}
WRITE_TOOLS = {
    "reset_user_password",
    "set_user_status",
    "logout_user_sessions",
    "unenroll_mfa_device",
    "change_user_membership",
    "change_user_assignment",
    "create_oidc_application",
    "create_saml_application",
}
# Changes that take effect the moment they are approved and affect sign-in.
DESTRUCTIVE_TOOLS = {
    "reset_user_password",
    "logout_user_sessions",
    "unenroll_mfa_device",
    "set_user_status",
}
# Opener card shown in the chat stream when a workspace opens in the side panel.
PANELS = {
    "workspace": (
        "PingOne AIC administration",
        "Users, groups, activity and change requests",
        "manage_accounts",
    ),
    "confirmation": (
        "Review administrative changes",
        "Exact targets and arguments awaiting a decision",
        "verified_user",
    ),
}
PROFILE_COLUMNS = [
    ("_id", "User ID"),
    ("userName", "Username"),
    ("displayName", "Name"),
    ("mail", "Email"),
    ("accountStatus", "Status"),
]
APP_COLUMNS = [("_id", "ID"), ("name", "Name"), ("description", "Description")]
FORM_FIELDS = {
    "set_user_status": {"status": Field(choices=("active", "inactive"))},
    "unenroll_mfa_device": {"method": Field(choices=("push", "oath", "webauthn"))},
    "change_user_membership": {
        "kind": Field(choices=("groups", "authzRoles")),
        "membership_id": Field(kind="resource", required=True),
        "action": Field(choices=("add", "remove")),
    },
    "change_user_assignment": {
        "assignment_id": Field(kind="resource", required=True),
        "action": Field(choices=("grant", "revoke")),
    },
    "create_oidc_application": {
        "client_id": Field(kind="resource", required=True),
        "name": Field(required=True),
        "app_type": Field(choices=("web", "spa", "native")),
        "redirect_uris": Field(kind="multiline", limit=4096, required=True),
        "description": Field(limit=1000),
    },
    "create_saml_application": {
        "name": Field(required=True),
        "description": Field(limit=1000),
        "sso_entities": Field(kind="multiline", limit=8000, required=True),
        "app_type_specific_config": Field(kind="multiline", limit=8000, required=True),
        "authoritative": Field(kind="bool"),
    },
}
FIELD_LABELS = {
    "status": (
        "New account status",
        "Inactive accounts cannot sign in until reactivated.",
    ),
    "method": ("MFA method", "Every enrolled device of this type is removed."),
    "kind": ("Membership type", None),
    "membership_id": (
        "Group or role ID",
        "The IDM _id of the group or authorization role.",
    ),
    "action": ("Action", None),
    "assignment_id": ("Assignment ID", "The IDM _id of the managed assignment."),
    "add_member": ("To add", None),
    "name_filter": ("Filter by name", None),
}
CHOICE_LABELS = {
    "groups": "Groups",
    "authzRoles": "Authorization roles",
    "add": "Add",
    "remove": "Remove",
    "grant": "Grant",
    "revoke": "Revoke",
    "active": "Active",
    "inactive": "Inactive",
    "push": "Push",
    "oath": "OATH",
    "webauthn": "WebAuthn",
}
INVESTIGATE = (
    ("Groups", "group", "list_user_groups", {"kind": "groups"}),
    ("Roles", "badge", "list_user_groups", {"kind": "authzRoles"}),
    ("Assignments", "assignment", "list_user_assignments", {}),
    ("Sessions", "devices", "list_user_sessions", {}),
)
# The Changes row of the user card: label, icon, the view it opens, and what
# that view is told. Groups and roles share one tool but get a panel each.
# "Remove MFA devices" is withheld for now. Its form and tool are intact;
# restore the button by adding this entry back:
#     ("Remove MFA devices", "phonelink_erase", "unenroll_mfa_device", {}),
CHANGES = (
    ("Change account status", "toggle_on", "set_user_status", {}),
    ("Change groups", "group_add", "change_user_membership", {"kind": "groups"}),
    ("Change roles", "add_moderator", "change_user_membership", {"kind": "authzRoles"}),
    ("Change assignment", "assignment_ind", "change_user_assignment", {}),
)
# How each membership panel words itself.
MEMBERSHIPS = {
    "groups": {
        "title": "Change groups",
        "one": "group",
        "many": "groups",
        "icon": "group",
    },
    "authzRoles": {
        "title": "Change roles",
        "one": "role",
        "many": "roles",
        "icon": "badge",
    },
}


def rows(data):
    value = data.get("result", [])
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _profile(user):
    return {
        **{key: user.get(key) for key, _ in PROFILE_COLUMNS},
        "givenName": user.get("givenName"),
        "sn": user.get("sn"),
        "displayName": user.get("displayName")
        or " ".join(str(user.get(k) or "") for k in ("givenName", "sn")).strip()
        or user.get("userName"),
    }


def _status(user):
    return user.get("accountStatus") or "unknown"


def _who(item):
    args = item.args
    return (
        args.get("user_id")
        or args.get("username")
        or args.get("query")
        or args.get("group_id")
        or ""
    )


def _account_name(b, user_id):
    """The username for an account ID when it is known, otherwise the ID."""
    return b.names.get(user_id) or user_id


def _account_id(item):
    """The account a result belongs to, from its arguments or its origin."""
    return item.args.get("user_id") or item.origin.get("user_id")


def _back_to_account(b, user_id):
    # A click returns only its own result, so the account card is gone from
    # the panel. Going back re-opens it, which also refreshes its data.
    return b.button(
        "Back to account",
        UiCommand("read", "get_user", {"user_id": user_id}),
        icon="arrow_back",
        appearance="text",
    )


def _open_account(b, user_id):
    return b.button(
        "Open account",
        UiCommand("read", "get_user", {"user_id": user_id}),
        icon="person",
        appearance="tonal",
    )


def _summary(b, user):
    """Name, status and identifying fields of one account; never the raw record."""
    name = user.get("displayName") or user.get("userName") or "User"
    header = b.row(
        [
            b.text(name, "h3", weight=1),
            b.status(_status(user), ok=_status(user) == "active"),
        ],
        align="center",
    )
    return b.column(
        [
            header,
            b.key_values(
                [
                    ("First name", user.get("givenName")),
                    ("Last name", user.get("sn")),
                    ("Username", user.get("userName")),
                    ("Email", user.get("mail")),
                    ("User ID", user.get("_id")),
                ]
            ),
        ]
    )


def _result_row(b, user):
    """One scannable line per search result with a single action."""
    user = _profile(user)
    name = user.get("displayName") or user.get("userName") or "User"
    detail = " · ".join(v for v in (user.get("mail"), user.get("userName")) if v)
    children = [
        b.column(
            [b.text(name, "body1"), b.text(detail or "No email on record", "caption")],
            weight=3,
        ),
        b.status(_status(user), ok=_status(user) == "active"),
    ]
    if user.get("_id"):
        children.append(
            b.button(
                "Open",
                UiCommand("read", "get_user", {"user_id": user["_id"]}),
                primary=True,
                icon="open_in_new",
            )
        )
    return b.row(children, align="center")


def _account(b, user):
    """The opened account: summary, then investigation, then changes."""
    user = _profile(user)
    user_id, username = user.get("_id"), user.get("userName")
    label = user.get("displayName") or username or user_id or "user"
    children = [_summary(b, user)]
    if user_id:
        investigate = [
            b.button(
                text,
                UiCommand("read", tool, {"user_id": user_id, **args}),
                icon=icon,
                appearance="tonal",
            )
            for text, icon, tool, args in INVESTIGATE
        ]
        if username:
            investigate.append(
                b.button(
                    "Recent activity",
                    # The activity tool takes a username; the origin carries the
                    # account ID so the activity view can lead back here.
                    UiCommand(
                        "read",
                        "get_user_activity",
                        {"username": username, "live": True},
                        {"user_id": user_id},
                    ),
                    icon="history",
                    appearance="tonal",
                )
            )
        children += [b.divider(), b.text("Investigate", "caption"), b.row(investigate)]
        changes = [
            b.button(
                text,
                UiCommand(
                    "view", view, {"user_id": user_id, "user_label": label, **told}
                ),
                icon=icon,
            )
            for text, icon, view, told in CHANGES
        ]
        risky = [
            b.button(
                "Review password reset",
                UiCommand("prepare", "reset_user_password", {"user_id": user_id}),
                once=True,
                danger=True,
                icon="lock_reset",
            )
        ]
        if username:
            risky.append(
                b.button(
                    "Terminate Sessions",
                    UiCommand(
                        "prepare",
                        "logout_user_sessions",
                        {"username": username, "expected_user_id": user_id},
                    ),
                    once=True,
                    danger=True,
                    icon="logout",
                )
            )
        children += [
            b.divider(),
            b.text("Changes", "caption"),
            b.row(changes),
            b.row(risky),
        ]
    return b.card(children, "User account")


def _search(b, item):
    users = rows(item.data)
    children = [
        b.text(f"{len(users)} matching users", "h3"),
        b.text(
            "Open the exact account to investigate it. Results may be limited to the requested page.",
            "caption",
        ),
    ]
    if len(users) > 10:
        children.append(
            b.text(f"Showing 10 of {len(users)} returned users.", "caption")
        )
    for index, user in enumerate(users[:10]):
        if index:
            children.append(b.divider())
        children.append(_result_row(b, user))
    if not users:
        children.append(b.banner("No matching users were returned.", "info"))
    return b.card(children, "Search results")


# What each relationship list is called, one record of it, and its icon.
RELATIONSHIPS = {
    "groups": ("Groups", "group", "group"),
    "authzRoles": ("Authorization roles", "role", "badge"),
    "assignments": ("Assignments", "assignment", "assignment"),
}
RELATIONSHIP_LIMIT = 30


def _relationship_kind(item):
    return (
        "assignments"
        if item.name == "list_user_assignments"
        else item.args.get("kind", "groups")
    )


def _relationship_row(b, row, icon):
    """One record by its display name; the ID shows on hover and beneath it."""
    ref_id = row.get("_refResourceId") or row.get("_id") or ""
    # A record IDM returned without a name is still listed, by its ID.
    name = row.get("name") or ref_id or "Unnamed record"
    # Captions line up with the label inside the text-style button above them.
    indent = {"paddingLeft": "12px"}
    lines = [b.hover_label(name, f"ID: {ref_id}" if ref_id else "No ID returned")]
    if row.get("description"):
        lines.append(b.text(row["description"], "caption", style=indent))
    # The reference stays selectable: the change forms ask for this ID.
    lines.append(b.text(row.get("_ref") or ref_id, "caption", style=indent))
    return b.row(
        [b.icon(icon, color="primary"), b.column(lines, weight=1)], align="center"
    )


def _relationships(b, item):
    title, noun, icon = RELATIONSHIPS.get(
        _relationship_kind(item), RELATIONSHIPS["groups"]
    )
    records = rows(item.data)
    count = len(records)
    children = [
        b.text(f"{title} · {_account_name(b, item.args.get('user_id', ''))}", "h3"),
        b.text(
            f"{count} {noun}{'' if count == 1 else 's'}. "
            "Hover over a name to see its ID.",
            "caption",
        ),
    ]
    for index, row in enumerate(records[:RELATIONSHIP_LIMIT]):
        if index:
            children.append(b.divider())
        children.append(_relationship_row(b, row, icon))
    if count > RELATIONSHIP_LIMIT:
        children.append(
            b.text(
                f"Showing {RELATIONSHIP_LIMIT} of {count} returned records.", "caption"
            )
        )
    if not records:
        children.append(b.banner(f"This user has no {noun}s.", "info"))
    account = _account_id(item)
    return b.column(
        [
            *([b.row([_back_to_account(b, account)])] if account else []),
            b.card(children, title),
            b.row(
                [
                    b.button(
                        "Refresh",
                        UiCommand("read", item.name, item.args),
                        icon="refresh",
                        appearance="text",
                    )
                ]
            ),
        ]
    )


def _when(value):
    """An AM timestamp as "2026-09-21 14:05 UTC"; anything else as it came."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return value
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _sessions(b, item):
    sessions = rows(item.data)
    count = len(sessions)
    account = _account_id(item)
    children = [
        *([b.row([_back_to_account(b, account)])] if account else []),
        b.text(
            f"Active sessions · {_account_name(b, item.args.get('user_id', ''))}", "h3"
        ),
        b.text(f"{count} active session{'' if count == 1 else 's'}.", "caption"),
    ]
    if sessions:
        children.append(
            b.table(
                "Active sessions",
                [
                    ("used", "Last used"),
                    ("idle", "Times out if idle"),
                    ("ends", "Ends regardless"),
                ],
                [
                    {
                        "used": _when(row.get("latestAccessTime")),
                        "idle": _when(row.get("maxIdleExpirationTime")),
                        "ends": _when(row.get("maxSessionExpirationTime")),
                    }
                    for row in sessions
                ],
            )
        )
    else:
        children.append(b.banner("This user has no active sessions.", "info"))
    children.append(
        b.row(
            [
                b.button(
                    "Refresh",
                    UiCommand("read", item.name, item.args),
                    icon="refresh",
                    appearance="text",
                )
            ]
        )
    )
    return b.column(children)


ACTIVITY_SHOWN = 30
ACTIVITY_SOURCES = ("am-authentication", "am-access", "am-activity", "idm-access")


def _event_payload(record):
    payload = record.get("payload", record)
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except ValueError:
            return {"message": payload}
        payload = parsed if isinstance(parsed, dict) else {"message": payload}
    return payload if isinstance(payload, dict) else {}


def _event_summary(record, *, dated):
    """The one line an operator scans: when, what happened, and how it went."""
    payload = _event_payload(record)
    stamp = payload.get("timestamp") or record.get("timestamp")
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        when = moment.astimezone(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC" if dated else "%H:%M:%S UTC"
        )
    except ValueError:
        when = str(stamp or "Unknown time")
    event = (
        payload.get("eventName")
        or payload.get("operation")
        or str(payload.get("message") or "")[:80]
        or record.get("type")
        or "Log entry"
    )
    response = payload.get("response")
    response = response if isinstance(response, dict) else {}
    result = payload.get("result")
    bits = [result if isinstance(result, str) else response.get("status")]
    entries = payload.get("entries")
    first = entries[0] if isinstance(entries, list) and entries else {}
    info = first.get("info") if isinstance(first, dict) else None
    info = info if isinstance(info, dict) else {}
    bits += [info.get("treeName"), info.get("displayName")]
    if info.get("nodeOutcome"):
        bits.append(f"outcome {info['nodeOutcome']}")
    http = payload.get("http")
    request = http.get("request") if isinstance(http, dict) else None
    if isinstance(request, dict):
        bits.append(
            " ".join(str(x) for x in (request.get("method"), request.get("path")) if x)
        )
    bits.append(payload.get("objectId"))
    return (
        f"{when} · {event}",
        " · ".join(str(bit) for bit in bits if bit) or "No further summary",
    )


def _activity(b, item):
    live = bool(item.args.get("live"))
    records = [row for row in rows(item.data) if isinstance(row, dict)]
    # Newest first: the event just produced is the one being looked for.
    shown = list(reversed(records))[:ACTIVITY_SHOWN]
    source = item.args.get("source", "am-authentication")
    account = _account_id(item)
    new = item.data.get("new")
    children = [
        *([b.row([_back_to_account(b, account)])] if account else []),
        b.text(f"Recent activity · {item.args.get('username', '')}", "h3"),
        b.row(
            [
                b.choice(
                    "source",
                    "Audit source",
                    [(name, name) for name in ACTIVITY_SOURCES],
                    source,
                    weight=1,
                ),
                b.button(
                    "Refresh",
                    UiCommand(
                        "read",
                        "get_user_activity",
                        {"username": item.args.get("username"), "live": True},
                        dict(item.origin),
                    ),
                    fields={"source": Field(choices=ACTIVITY_SOURCES)},
                    icon="refresh",
                    appearance="tonal",
                ),
            ],
            align="center",
        ),
    ]
    if not records:
        children.append(
            b.banner(
                "No new events yet. Refresh after the user tries again."
                if live
                else "No events were returned.",
                "info",
            )
        )
        return b.column(children)
    count = len(records)
    line = f"{count} event{'' if count == 1 else 's'}"
    if live and isinstance(new, int):
        line += f", {new} new" if new else ". Nothing new since the last refresh"
    if count > len(shown):
        line += f". Showing the latest {len(shown)}"
    children.append(b.text(f"{line}.", "caption"))
    for record in shown:
        title, description = _event_summary(record, dated=not live)
        details = json.dumps(
            review_args(_event_payload(record)),
            indent=2,
            ensure_ascii=False,
            default=str,
        )
        children.append(b.expansion(title, description, [b.code(details[:6000])]))
    return b.column(children)


def _receipt_text(b, item: ToolResult) -> str:
    """The chat receipt, minus anything the panel must never show."""
    if item.name == "reset_user_password":
        user = _account_name(b, item.args.get("user_id", ""))
        return f"Password reset for {user}. The new password is in the chat response."
    return describe_result(item, names=b.names)


def _next_step(b, item: ToolResult) -> list[str]:
    user_id = _account_id(item)
    if user_id:
        # A failed read was a drill-down from the account; a change is an outcome.
        button = _back_to_account if item.name in READ_TOOLS else _open_account
        return [b.row([button(b, user_id)])]
    username = item.args.get("username")
    if username:
        return [
            b.row(
                [
                    b.button(
                        "Find account",
                        UiCommand("read", "search_users", {"query": username}),
                        icon="search",
                        appearance="tonal",
                    )
                ]
            )
        ]
    return []


def _observation(b, item: ToolResult):
    name, data = item.name, item.data
    label = LABELS.get(name, name)
    status = item.response.get("status")
    if status == "rejected":
        return b.column(
            [
                b.banner(f"{label}: rejected; no change was made.", "info"),
                *_next_step(b, item),
            ]
        )
    if status != "success":
        message = item.response.get(
            "message", item.response.get("error", "No successful result was returned.")
        )
        return b.column(
            [b.banner(f"{label}: {message}", "error"), *_next_step(b, item)]
        )
    if name == "search_users":
        return _search(b, item)
    if name == "get_user":
        return _account(b, data)
    if name == "get_user_activity":
        return _activity(b, item)
    if name == "list_user_sessions":
        return _sessions(b, item)
    if name in {"list_user_groups", "list_user_assignments"}:
        return _relationships(b, item)
    if name == "list_group_members":
        return b.table(
            "Group members", PROFILE_COLUMNS, [_profile(row) for row in rows(data)]
        )
    if name == "find_application":
        return b.table("Applications", APP_COLUMNS, rows(data))
    # Write receipts show the outcome and the way back; identifiers only.
    return b.column([b.banner(_receipt_text(b, item), "success"), *_next_step(b, item)])


def _form(b, result):
    if result.view == "change_user_membership":
        return _membership_form(b, result)
    view, fixed = result.view, result.form_values
    children = [b.text(LABELS[view], "h3")]
    target = fixed.get("user_label") or fixed.get("user_id")
    if target:
        children.append(b.text(f"Applies to {target}", "caption"))
    for key, error in result.errors.items():
        label = FIELD_LABELS.get(key, (key.replace("_", " ").capitalize(), None))[0]
        children.append(b.banner(f"{label}: {error}", "error"))
    fields = []
    if view == "create_oidc_application":
        fields += [
            b.input("client_id", "Client ID"),
            b.input("name", "Application name"),
            b.choice(
                "app_type",
                "Application type",
                [("web", "Web"), ("spa", "Single-page app"), ("native", "Native")],
                "spa",
            ),
            b.input("redirect_uris", "Redirect URIs (one per line)", long=True),
            b.input("description", "Description"),
            b.text(
                "Authorization code, PKCE S256 and the openid scope. Web clients use client_secret_basic; SPA/native clients use none.",
                "caption",
            ),
        ]
    elif view == "create_saml_application":
        fields += [
            b.input("name", "Application name"),
            b.input("description", "Description"),
            b.input(
                "sso_entities",
                "Observed SSO entity configuration (JSON)",
                "{}",
                long=True,
            ),
            b.input(
                "app_type_specific_config",
                "Application configuration (JSON)",
                "{}",
                long=True,
            ),
            b.checkbox("authoritative", "Authoritative application"),
            b.text(
                "Uses the supported saml 1.1.0 template. Enter configuration observed in this tenant.",
                "caption",
            ),
        ]
    else:
        for key, field in FORM_FIELDS[view].items():
            label, hint = FIELD_LABELS.get(
                key, (key.replace("_", " ").capitalize(), None)
            )
            if field.choices:
                fields.append(
                    b.choice(
                        key,
                        label,
                        [(x, CHOICE_LABELS.get(x, x)) for x in field.choices],
                        field.choices[0],
                    )
                )
            else:
                fields.append(b.input(key, label))
            if hint:
                fields.append(b.text(hint, "caption"))
    target_args = {"user_id": fixed["user_id"]} if fixed.get("user_id") else {}
    children += [
        *fields,
        b.row(
            [
                b.button(
                    "Review change",
                    UiCommand("prepare", view, target_args),
                    fields=FORM_FIELDS[view],
                    primary=True,
                    once=True,
                    icon="fact_check",
                ),
                _back_to_account(b, fixed["user_id"])
                if fixed.get("user_id")
                else b.button(
                    "Back to workspace",
                    UiCommand("view", "workspace"),
                    icon="arrow_back",
                    appearance="text",
                ),
            ]
        ),
    ]
    return b.card(children, "Prepare administrative change")


def _membership_kind(result):
    kind = result.choices.get("kind") or result.form_values.get("kind")
    return kind if kind in MEMBERSHIPS else "groups"


def _named_options(items):
    """Select options by name; a repeated name also shows part of its ID."""
    seen = [item["name"] for item in items]
    return [
        (
            item["id"],
            item["name"]
            if seen.count(item["name"]) == 1
            else f"{item['name']} ({str(item['id'])[:8]})",
        )
        for item in items
    ]


def _membership_form(b, result):
    """One panel per kind: what the user holds, each with an X, and what to add."""
    fixed, choices = result.form_values, result.choices
    kind = _membership_kind(result)
    words = MEMBERSHIPS[kind]
    user_id = fixed.get("user_id", "")
    who = fixed.get("user_label") or "This user"
    children = [b.text(words["title"], "h3")]
    if fixed.get("user_label") or user_id:
        children.append(
            b.text(f"Applies to {fixed.get('user_label') or user_id}", "caption")
        )
    for key, error in result.errors.items():
        label = FIELD_LABELS.get(key, (key.replace("_", " ").capitalize(), None))[0]
        children.append(b.banner(f"{label}: {error}", "error"))

    def change(action, **extra):
        return UiCommand(
            "prepare",
            "change_user_membership",
            {"user_id": user_id, "kind": kind, **extra, "action": action},
        )

    back = (
        _back_to_account(b, user_id)
        if user_id
        else b.button(
            "Back to workspace",
            UiCommand("view", "workspace"),
            icon="arrow_back",
            appearance="text",
        )
    )
    if not choices or choices.get("error"):
        # Nothing to pick from, so the record's ID is typed instead.
        if choices.get("error"):
            children.append(
                b.banner(
                    f"The {words['many']} could not be listed: {choices['error']} "
                    "Enter an ID instead.",
                    "warning",
                )
            )
        children += [
            b.input("membership_id", f"{words['one'].capitalize()} ID"),
            b.choice("action", "Action", [("add", "Add"), ("remove", "Remove")], "add"),
            b.row(
                [
                    b.button(
                        "Review change",
                        UiCommand(
                            "prepare",
                            "change_user_membership",
                            {"user_id": user_id, "kind": kind},
                        ),
                        fields={
                            "membership_id": Field(kind="resource", required=True),
                            "action": Field(choices=("add", "remove")),
                        },
                        primary=True,
                        once=True,
                        icon="fact_check",
                    ),
                    back,
                ]
            ),
        ]
        return b.card(children, words["title"])

    member, available = choices.get("member", []), choices.get("available", [])
    children.append(b.text(f"Current {words['many']}", "subtitle2"))
    for index, item in enumerate(member):
        if index:
            children.append(b.divider())
        children.append(
            b.row(
                [
                    b.icon(words["icon"], color="primary"),
                    b.column(
                        [b.hover_label(item["name"], f"ID: {item['id']}")], weight=1
                    ),
                    b.icon_button(
                        "close",
                        change("remove", membership_id=item["id"]),
                        label=f"Remove {item['name']}",
                        once=True,
                        danger=True,
                    ),
                ],
                align="center",
            )
        )
    if not member:
        children.append(
            b.text(f"{who} does not belong to any {words['one']}.", "caption")
        )

    term = choices.get("filter", "")
    matching = f" matching “{term}”" if term else ""
    children += [b.divider(), b.text(f"Add a {words['one']}", "subtitle2")]
    if available:
        ids = tuple(item["id"] for item in available)
        children.append(
            b.row(
                [
                    b.choice(
                        "add_member",
                        f"{words['one'].capitalize()} to add",
                        [("", f"Choose a {words['one']}"), *_named_options(available)],
                        "",
                        weight=1,
                    ),
                    b.button(
                        "Review add",
                        change("add"),
                        # Only an offered record is accepted, whatever is submitted.
                        fields={"add_member": Field(choices=ids, arg="membership_id")},
                        primary=True,
                        once=True,
                        icon="add",
                    ),
                ],
                align="center",
            )
        )
    count = len(available)
    if choices.get("truncated"):
        note = (
            f"Showing the first {count} {words['many']}{matching} by name. "
            "Filter by name to find others."
        )
    elif count:
        note = f"{count} {words['one'] if count == 1 else words['many']}{matching} can be added."
    else:
        note = f"No {words['one']}{matching} can be added."
    children.append(b.text(note, "caption"))
    if choices.get("truncated") or term:
        children.append(
            b.row(
                [
                    b.input("name_filter", "Filter by name", weight=1),
                    b.button(
                        "Filter",
                        UiCommand(
                            "view",
                            "change_user_membership",
                            {
                                "user_id": user_id,
                                "user_label": fixed.get("user_label", ""),
                                "kind": kind,
                            },
                        ),
                        fields={"name_filter": Field(limit=80)},
                        icon="filter_list",
                        appearance="text",
                    ),
                ],
                align="center",
            )
        )
    children += [b.divider(), b.row([back])]
    return b.card(children, words["title"])


def _confirmation(b, result):
    children = [
        b.text("Review administrative changes", "h3"),
        b.text(
            "Nothing has run yet. Each decision applies to the exact target and arguments shown.",
            "body2",
        ),
    ]
    for item in result.confirmations:
        tool = item["tool"]
        name = tool["name"]
        pairs = [
            (
                key.replace("_", " ").capitalize(),
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False),
            )
            for key, value in review_args(tool.get("args", {})).items()
        ]
        # A change chosen from a picker carries only an ID; show its name too.
        named = [
            (
                f"{key.replace('_id', '').replace('_', ' ').capitalize()} name",
                b.names[value],
            )
            for key, value in tool.get("args", {}).items()
            if key.endswith("_id") and isinstance(value, str) and b.names.get(value)
        ]
        body = [
            b.text(LABELS.get(name, name), "h4"),
            b.key_values([*named, *pairs], limit=16000),
        ]
        if name in DESTRUCTIVE_TOOLS:
            body.append(
                b.banner(
                    "Takes effect in the tenant as soon as it is approved.", "warning"
                )
            )
        body.append(
            b.row(
                [
                    b.button(
                        "Approve",
                        UiCommand(
                            "confirm", "approve", {"id": item["id"], "confirmed": True}
                        ),
                        once=True,
                        primary=True,
                        icon="check",
                    ),
                    b.button(
                        "Reject",
                        UiCommand(
                            "confirm", "reject", {"id": item["id"], "confirmed": False}
                        ),
                        once=True,
                        danger=True,
                        icon="close",
                    ),
                ]
            )
        )
        children.append(b.card(body, "Review exact change"))
    if len(result.confirmations) > 1:
        children.append(
            b.text("Changes run after every pending review has a decision.", "caption")
        )
    children.append(
        b.text("An approval expires after 10 minutes and can be used once.", "caption")
    )
    return b.column(children)


def _header(b, context):
    # The logo is opt-in until the basic Image is confirmed to render in
    # Gemini Enterprise; an icon anchors the header otherwise.
    mark = (
        b.logo(compact=True)
        if canvas_logo_enabled()
        else b.icon("admin_panel_settings", color="primary")
    )
    caption = b.text(context, "caption")
    row = b.row(
        [
            mark,
            b.column(
                [b.text("PingOne AIC administration", "subtitle1"), caption],
                weight=1,
            ),
        ],
        align="center",
    )
    # Remembered so a later turn can show progress here without moving anything.
    b.header = {"row": b.component(row), "caption": b.component(caption)}
    return row


def _user_search(b, mode):
    """The user search, as prominent as the screen needs it to be.

    On the start screen it is the task, so it is a titled card with the only
    filled button. Among search results it stays open, but the rows' Open
    buttons lead. Once an account or anything under it is open, the account is
    the task, and search folds into one line that opens with a click.
    """
    controls = b.row(
        [
            b.input(
                "query",
                "Search users",
                weight=3,
                placeholder="Name, username or email",
            ),
            b.button(
                "Search",
                UiCommand("read", "search_users"),
                fields={"query": Field(required=True)},
                primary=mode == "start",
                icon="search",
                appearance=None if mode == "start" else "tonal",
            ),
        ],
        align="center",
    )
    if mode == "collapsed":
        return b.expansion("Find another user", "", [controls])
    return b.card([b.text("Find a user", "subtitle1"), controls], "Find a user")


def _applications(b):
    return b.expansion(
        "Applications",
        "Find an application or create a new one",
        [
            b.input("application_query", "Application name"),
            b.row(
                [
                    b.button(
                        "Find applications",
                        UiCommand("read", "find_application"),
                        fields={"application_query": Field(required=True)},
                        icon="search",
                    ),
                    b.button(
                        "Create OIDC app",
                        UiCommand("view", "create_oidc_application"),
                        icon="add",
                        appearance="text",
                    ),
                    b.button(
                        "Create SAML app",
                        UiCommand("view", "create_saml_application"),
                        icon="add",
                        appearance="text",
                    ),
                ]
            ),
        ],
    )


def _context(newest):
    if newest is None:
        return "Search for a user to begin"
    if newest.name == "get_user" and newest.response.get("status") == "success":
        user = _profile(newest.data)
        return (
            f"{user.get('userName') or user.get('_id') or 'account'} · {_status(user)}"
        )
    return LABELS.get(newest.name, newest.name)


def _section_title(b, item):
    who = _who(item)
    return f"{LABELS.get(item.name, item.name)} · {_account_name(b, who) or 'result'}"


def _panel(view):
    if not panel_enabled():
        return None
    title, description, icon = PANELS.get(
        view,
        (
            LABELS.get(view, view),
            "Complete the form, then review the exact change",
            "edit_document",
        ),
    )
    return {"title": title, "description": description, "icon": icon}


def render_result(result, surface_id, catalog_id):
    b = Components(surface_id, catalog_id, values=result.form_values)
    b.names = dict(result.names)
    if result.confirmations:
        children = [
            _header(b, "Awaiting your decision"),
            *[_observation(b, item) for item in result.observations],
            _confirmation(b, result),
        ]
        return b.finish(children, "confirmation", panel=_panel("confirmation"))
    if result.view in FORM_FIELDS:
        title = (
            MEMBERSHIPS[_membership_kind(result)]["title"]
            if result.view == "change_user_membership"
            else LABELS[result.view]
        )
        children = [_header(b, f"Preparing: {title}"), _form(b, result)]
        panel = _panel(result.view)
        if panel:
            panel = {**panel, "title": title}
        return b.finish(children, result.view, panel=panel)
    latest = {
        (item.name, json.dumps(item.args, sort_keys=True)): item
        for item in result.observations
    }
    items = list(latest.values())[-6:]
    newest = items[-1] if items else None
    if newest is None:
        search = "start"
    elif newest.name == "search_users":
        search = "results"
    else:
        search = "collapsed"
    children = [_header(b, _context(newest)), _user_search(b, search)]
    if result.text and not items:
        children.append(b.text(result.text))
    # Newest result first and in full; earlier results stay one click away.
    for index, item in enumerate(reversed(items)):
        b.scope = f"result{index}"
        body = _observation(b, item)
        children.append(
            body
            if index == 0
            else b.expansion(_section_title(b, item), "Earlier result", [body])
        )
    b.scope = ""
    if len(latest) > 6:
        children.append(b.text("Showing the latest six distinct results.", "caption"))
    children.append(_applications(b))
    return b.finish(children, "workspace", panel=_panel("workspace"))


def loading_surface(surface_id, catalog_id):
    b = Components(surface_id, catalog_id)
    children = [b.text("Working on your request", "h3")]
    if b.material:
        children.append(b.progress("Request in progress"))
    return b.finish(children, "loading")


WORKING_TEXT = "Working on your request"


def working_update(canvas, catalog_id, *, working=True):
    """Partial update of the existing panel: progress inside its header row.

    The header is already two text lines tall and its title takes all the free
    width, so a small spinner at the end of the row and a swapped context line
    move nothing else in the panel. Only the header's own components travel;
    the client holds everything they reference, and the panel root is never
    touched. With ``working=False`` the original header is sent again.
    """
    header = canvas.get("header")
    if not header:
        return []
    row, caption = deepcopy(header["row"]), deepcopy(header["caption"])
    components = [caption, row]
    if working:
        b = Components(canvas["id"], catalog_id)
        spinner = b.add(
            "MaterialProgressSpinner",
            mode="indeterminate",
            diameter=20,
            strokeWidth=3,
            ariaLabel=WORKING_TEXT,
        )
        # A literal replaces the data binding; the final render restores it.
        caption["text"] = WORKING_TEXT
        row["children"] = [*row["children"], spinner]
        components = [*b.items, caption, row]
    messages = [
        {
            "version": WIRE_VERSION,
            "updateComponents": {"surfaceId": canvas["id"], "components": components},
        }
    ]
    validate_surface(messages, catalog_id, partial=True)
    return messages
