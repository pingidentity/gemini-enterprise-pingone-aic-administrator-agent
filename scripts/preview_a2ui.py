"""Export synthetic, schema-validated AIC views without calling a tenant or model."""

import argparse
import base64
import json
from pathlib import Path

from ping_admin_agent.ui_protocol import BASIC_CATALOG_ID, GEMINI_CATALOG_ID
from ping_admin_agent.ui_results import RuntimeResult, ToolResult
from ping_admin_agent.ui_views import FORM_FIELDS, render_result

ROOT = Path(__file__).resolve().parents[1]


def fixtures():
    user = {
        "_id": "demo-alice",
        "userName": "alice.smith",
        "givenName": "Alice",
        "sn": "Smith",
        "displayName": "Alice Smith",
        "mail": "alice.smith@example.test",
        "accountStatus": "active",
    }
    relationship = {
        "result": [
            {
                "_refResourceId": "help-desk",
                "name": "Help desk administrators",
                "_ref": "managed/alpha_group/help-desk",
            }
        ]
    }

    def result(name, data, args=None, origin=None):
        return RuntimeResult(
            observations=[
                ToolResult(
                    name, {"status": "success", "data": data}, args or {}, origin or {}
                )
            ]
        )

    views = {
        "Start": RuntimeResult(),
        "User profile": result("get_user", user),
        "User search": result(
            "search_users",
            {
                "result": [
                    user,
                    {
                        **user,
                        "_id": "demo-avery",
                        "userName": "avery.smith",
                        "displayName": "Avery Smith",
                        "mail": "avery.smith@example.test",
                        "accountStatus": "inactive",
                    },
                ]
            },
        ),
        "Groups & roles": result(
            "list_user_groups",
            relationship,
            {"user_id": "demo-alice", "kind": "groups"},
        ),
        "Workspace history": RuntimeResult(
            observations=[
                ToolResult(
                    "search_users",
                    {"status": "success", "data": {"result": [user]}},
                    {"query": "smith"},
                ),
                ToolResult(
                    "get_user",
                    {"status": "success", "data": user},
                    {"user_id": "demo-alice"},
                ),
                ToolResult(
                    "list_user_groups",
                    {"status": "success", "data": relationship},
                    {"user_id": "demo-alice", "kind": "groups"},
                ),
            ]
        ),
        "Sessions": result(
            "list_user_sessions",
            {
                "result": [
                    {
                        "latestAccessTime": "2026-09-08T15:42:42.544Z",
                        "maxIdleExpirationTime": "2026-09-08T16:12:42Z",
                        "maxSessionExpirationTime": "2026-09-08T17:31:22Z",
                        "realm": "/alpha",
                    },
                    {
                        "latestAccessTime": "2026-09-08T09:05:10Z",
                        "maxIdleExpirationTime": "2026-09-08T09:35:10Z",
                        "maxSessionExpirationTime": "2026-09-08T11:00:00Z",
                        "realm": "/alpha",
                    },
                ]
            },
            {"user_id": "demo-alice"},
        ),
        "Assignments": result(
            "list_user_assignments",
            {
                "result": [
                    {
                        "_refResourceId": "portal-access",
                        "name": "Employee portal",
                        "_ref": "managed/alpha_assignment/portal-access",
                    }
                ]
            },
            {"user_id": "demo-alice"},
        ),
        "Recent activity": result(
            "get_user_activity",
            {
                "result": [
                    {
                        "payload": {
                            "_id": f"demo-event-{i + 1}",
                            "timestamp": f"2026-09-08T15:48:{10 + i * 7:02}.120Z",
                            "eventName": "AM-NODE-LOGIN-COMPLETED"
                            if i % 2
                            else "AM-LOGIN-COMPLETED",
                            "result": "SUCCESSFUL" if i != 2 else "FAILED",
                            "transactionId": f"demo-transaction-{i + 1}",
                            "principal": ["alice.smith"],
                            "entries": [
                                {
                                    "info": {
                                        "treeName": "Login",
                                        "displayName": "Data Store Decision",
                                        "nodeOutcome": "true" if i != 2 else "false",
                                    }
                                }
                            ],
                        }
                    }
                    for i in range(5)
                ],
                "new": 2,
            },
            {"username": "alice.smith", "source": "am-authentication", "live": True},
            {"user_id": "demo-alice"},
        ),
        "Applications": result(
            "find_application",
            {
                "result": [
                    {
                        "_id": "employee-portal",
                        "name": "Employee portal",
                        "description": "Workforce application",
                    }
                ]
            },
            {"query": "portal"},
        ),
        "Review change": RuntimeResult(
            confirmations=[
                {
                    "id": "demo-approval-not-valid-at-runtime",
                    "tool": {
                        "name": "reset_user_password",
                        "args": {"user_id": "demo-alice"},
                    },
                }
            ]
        ),
        "Approved outcome": result(
            "reset_user_password", {"user": user}, {"user_id": "demo-alice"}
        ),
        "Rejected outcome": RuntimeResult(
            observations=[ToolResult("reset_user_password", {"status": "rejected"})]
        ),
        "Empty results": result("search_users", {"result": []}),
        "Service error": RuntimeResult(
            observations=[
                ToolResult(
                    "get_user_activity",
                    {
                        "status": "error",
                        "message": "The monitoring service is unavailable. Retry the read later.",
                    },
                )
            ]
        ),
    }
    names = {
        "set_user_status": "Account status",
        "unenroll_mfa_device": "MFA removal",
        "change_user_membership": "Membership form",
        "change_user_assignment": "Assignment form",
        "create_oidc_application": "OIDC application",
        "create_saml_application": "SAML application",
    }
    for name in FORM_FIELDS:
        views[names[name]] = RuntimeResult(
            view=name,
            form_values={"user_id": "demo-alice"}
            if not name.startswith("create_")
            else {},
        )
    membership = views.pop("Membership form")
    for label, kind, held, available in (
        (
            "Change groups",
            "groups",
            [("managed/alpha_group/help-desk", "Help desk administrators")],
            [("contractors", "Contractors"), ("finance", "Finance")],
        ),
        (
            "Change roles",
            "authzRoles",
            [("internal/role/openidm-authorized", "openidm-authorized")],
            [("openidm-admin", "openidm-admin"), ("help-desk", "Help desk admin")],
        ),
    ):
        views[label] = RuntimeResult(
            view=membership.view,
            form_values={
                "user_id": "demo-alice",
                "user_label": "Alice Smith",
                "kind": kind,
            },
            choices={
                "kind": kind,
                "member": [{"id": i, "name": n} for i, n in held],
                "available": [{"id": i, "name": n} for i, n in available],
                "truncated": False,
                "filter": "",
            },
        )
    views["Validation feedback"] = RuntimeResult(
        view="create_oidc_application",
        form_values={
            "name": "Employee portal",
            "client_id": "employee-portal",
            "app_type": "spa",
            "redirect_uris": "http://portal.example.test/callback",
        },
        errors={"redirect_uris": "Use an HTTPS redirect URI."},
    )
    return {
        label: {
            name: render_result(value, f"preview-{index}", catalog).messages()
            for index, (name, value) in enumerate(views.items())
        }
        for label, catalog in (
            ("Gemini Material", GEMINI_CATALOG_ID),
            ("Basic fallback", BASIC_CATALOG_ID),
        )
    }


def export(output_dir, fragment_output=None):
    values = fixtures()
    encoded = json.dumps(values, ensure_ascii=False).replace("</", "<\\/")
    logo = base64.b64encode(
        (ROOT / "ping_admin_agent/assets/ping-identity-logo.png").read_bytes()
    ).decode()
    fragment = (
        (ROOT / "examples/a2ui/gallery.html")
        .read_text()
        .replace("/* FIXTURE_DATA */ {}", encoded)
    )
    fragment = fragment.replace("LOGO_DATA_URI", "data:image/png;base64," + logo)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "fixtures.json").write_text(json.dumps(values, indent=2))
    (output_dir / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>PingOne AIC A2UI preview</title>" + fragment + "</html>"
    )
    if fragment_output:
        fragment_output.write_text(fragment)
    print(
        f"Validated {sum(map(len, values.values()))} surfaces; preview: {output_dir / 'index.html'}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("/tmp/pingaic-a2ui-preview")
    )
    parser.add_argument("--fragment-output", type=Path)
    args = parser.parse_args()
    export(args.output_dir, args.fragment_output)


if __name__ == "__main__":
    main()
