"""Tool observations and deterministic chat text, independent of the model.

The chat carries one readable line per outcome; structured records live in the
A2UI surface. Raw tool JSON is never echoed into the conversation.
"""

import json
import re
from dataclasses import dataclass, field

LABELS = {
    "search_users": "User search",
    "get_user": "User profile",
    "list_user_groups": "Groups & roles",
    "list_group_members": "Group members",
    "list_user_assignments": "Assignments",
    "find_application": "Applications",
    "get_user_activity": "Recent activity",
    "list_user_sessions": "Active sessions",
    "reset_user_password": "Reset password",
    "set_user_status": "Change account status",
    "logout_user_sessions": "End user sessions",
    "unenroll_mfa_device": "Remove MFA devices",
    "change_user_membership": "Change membership",
    "change_user_assignment": "Change assignment",
    "create_oidc_application": "Create OIDC application",
    "create_saml_application": "Create SAML application",
}


@dataclass
class ToolResult:
    name: str
    response: dict
    args: dict = field(default_factory=dict)
    # The UiCommand.origin of the control that produced this result, if any.
    origin: dict = field(default_factory=dict)

    @property
    def data(self):
        value = self.response.get("data", {})
        return value if isinstance(value, dict) else {}


@dataclass
class RuntimeResult:
    text: str = ""
    observations: list[ToolResult] = field(default_factory=list)
    confirmations: list[dict] = field(default_factory=list)
    view: str = "workspace"
    form_values: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    # What a form offers to pick from, loaded from the tenant when it is shown.
    choices: dict = field(default_factory=dict)
    # Display names of records offered in this conversation, by ID, so a review
    # and its receipt can name what a picker selected.
    names: dict = field(default_factory=dict)


def review_args(args):
    # Original values remain in the ADK session. Never echo credential values
    # into a surface or the separate action registry.
    if isinstance(args, dict):
        return {
            key: "[redacted]"
            if any(
                word in key.lower()
                for word in ("password", "secret", "private", "token")
            )
            and key != "token_endpoint_auth_method"
            else review_args(value)
            for key, value in args.items()
            if key != "confirm"
        }
    if isinstance(args, list):
        return [review_args(value) for value in args]
    return args


def describe_args(args: dict) -> str:
    """The exact arguments as readable field/value pairs, secrets redacted."""
    parts = []
    for key, value in review_args(args).items():
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, default=str)
        )
        parts.append(f"{key.replace('_', ' ')} {text}")
    return ", ".join(parts) or "no arguments"


def describe_change(name: str, args: dict) -> str:
    return f"{LABELS.get(name, name)}: {describe_args(args)}"


# An address that is not already inside a code span.
_EMAIL = re.compile(r"(?<![`\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![`\w])")


def unlinked(text: str) -> str:
    """Send email addresses as inline code so clients do not link them.

    Gemini Enterprise turns a bare address into a ``mailto:`` link and offers
    no property to switch that off. Markdown renderers never link inside a code
    span, and copying one yields the clean address, unlike an invisible
    character hidden inside it.
    """
    return _EMAIL.sub(lambda match: f"`{match.group(0)}`", text)


def _rows(data: dict) -> list:
    value = data.get("result")
    return value if isinstance(value, list) else []


def _names(rows: list, brief: bool, limit: int = 10) -> str:
    """The display names of listed records, for clients without a panel."""
    names = [
        str(row["name"])[:80]
        for row in rows
        if isinstance(row, dict) and row.get("name")
    ]
    if brief or not names:
        return ""
    more = f", and {len(names) - limit} more" if len(names) > limit else ""
    return f" {', '.join(names[:limit])}{more}."


def account_names(observations) -> dict[str, str]:
    """Usernames by account ID, from any user records these results carry.

    A receipt then reads "user alice.smith" instead of a UUID. A relationship
    record also has an ``_id``, but no ``userName``, so it is never mistaken
    for an account.
    """
    found: dict[str, str] = {}
    for item in observations:
        if item.response.get("status") != "success":
            continue
        data = item.response.get("data")
        if not isinstance(data, dict):
            continue
        listed = data.get("result")
        records = [
            data,
            data.get("user"),
            *(listed if isinstance(listed, list) else []),
        ]
        for record in records:
            if (
                isinstance(record, dict)
                and record.get("_id")
                and record.get("userName")
            ):
                found[str(record["_id"])] = str(record["userName"])
    return found


def describe_result(
    item: ToolResult, *, brief: bool = False, names: dict | None = None
) -> str:
    """One readable line per outcome.

    ``brief`` is for A2UI clients, whose panel already shows the records.
    ``names`` maps known IDs to what people call them, so a receipt names the
    account and the group rather than their UUIDs.
    Generated passwords are deliberately part of the privileged chat text.
    """
    names = names or {}
    label = LABELS.get(item.name, item.name)
    if item.response.get("status") == "rejected":
        return f"{label}: rejected; no change was made."
    if item.response.get("status") != "success":
        message = item.response.get(
            "message", item.response.get("error", "No successful result was returned.")
        )
        return f"{label}: {message}"
    name, data, args = item.name, item.data, item.args
    rows = _rows(data)
    user = args.get("user_id", "")
    # The account's username when it is known, otherwise the ID it was given.
    user = names.get(user) or data.get("userName") or user
    if name == "search_users":
        if brief:
            return f"Found {len(rows)} matching users. Open an account in the panel to continue."
        return "\n".join(
            [
                f"Found {len(rows)} matching users.",
                *[
                    f"{row.get('userName', '')} | {row.get('displayName', '')}; {row.get('mail', '')}; ID: {row.get('_id', '')}"
                    for row in rows
                    if isinstance(row, dict)
                ],
            ]
        )
    if name == "get_user":
        account = (
            f"Account {data.get('userName') or user or 'user'} "
            f"(ID {data.get('_id') or user or 'unknown'}): "
            f"status {data.get('accountStatus') or 'unknown'}"
        )
        # The panel already shows the address; the chat line stays short there.
        if brief:
            return f"{account}."
        return f"{account}, email {data.get('mail') or 'not available'}."
    if name == "list_user_groups":
        kind = "Roles" if args.get("kind") == "authzRoles" else "Groups"
        return f"{kind} for user {user}: {len(rows)} record(s).{_names(rows, brief)}"
    if name == "list_user_assignments":
        return (
            f"Assignments for user {user}: {len(rows)} record(s).{_names(rows, brief)}"
        )
    if name == "list_group_members":
        return f"Members of group {args.get('group_id', '')}: {len(rows)} record(s)."
    if name == "find_application":
        return (
            f"Applications matching {args.get('query', '')!r}: {len(rows)} record(s)."
        )
    if name == "list_user_sessions":
        count = len(rows)
        line = f"Active sessions for user {user}: {count}."
        used = [
            str(row.get("latestAccessTime"))
            for row in rows
            if row.get("latestAccessTime")
        ]
        if brief or not used:
            return line
        return f"{line} Last used: {', '.join(used[:10])}."
    if name == "get_user_activity":
        return (
            f"Recent {args.get('source', 'am-authentication')} activity for "
            f"{args.get('username', '')}: {len(rows)} event(s)."
        )
    if name == "reset_user_password":
        if data.get("new_password"):
            return f"Password reset for {user}. New password: {data['new_password']}"
        return f"Password reset for {user}."
    if name == "set_user_status":
        status = args.get("status") or data.get("accountStatus") or "updated"
        return f"Account {data.get('userName') or user or 'user'} is now {status}."
    if name == "logout_user_sessions":
        return f"Ended all sessions for user {args.get('username', '')}."
    if name == "unenroll_mfa_device":
        return (
            f"Removed {data.get('count', 0)} {args.get('method', '')} MFA "
            f"device(s) from user {user}."
        )
    if name == "change_user_membership":
        adding = args.get("action") == "add"
        kind = "role" if args.get("kind") == "authzRoles" else "group"
        held = args.get("membership_id", "")
        return (
            f"{'Added' if adding else 'Removed'} {kind} {names.get(held) or held} "
            f"{'to' if adding else 'from'} user {user}."
        )
    if name == "change_user_assignment":
        granting = args.get("action") == "grant"
        held = args.get("assignment_id", "")
        return (
            f"{'Granted' if granting else 'Revoked'} assignment "
            f"{names.get(held) or held} {'to' if granting else 'from'} user {user}."
        )
    if name == "create_oidc_application":
        return (
            f"Created OIDC application {args.get('name', '')} "
            f"(client ID {args.get('client_id', '')})."
        )
    if name == "create_saml_application":
        ident = data.get("_id")
        suffix = f" (ID {ident})." if ident else "."
        return f"Created SAML application {args.get('name', '')}{suffix}"
    return f"{label}: completed."
