"""Drive PingClient against a real tenant, no Gemini/ADK involved.

Read-only by default. Writes are gated behind --confirm so a stray invocation
can't mutate tenant state.

Usage:
    python -m scripts.try_api search-users --q smith
    python -m scripts.try_api get-user <id>
    python -m scripts.try_api groups <id>
    python -m scripts.try_api assignments <id>
    python -m scripts.try_api activity <username> --source am-authentication
"""

from __future__ import annotations

import argparse
import json
import sys

from ping_admin_agent.ping_client import PingClient, PingError


def _dump(obj: object) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search-users", help="search users by name/email/username")
    p.add_argument("--q", required=True, help="search term")
    p.add_argument("--page-size", type=int, default=10)

    p = sub.add_parser("get-user", help="fetch a user by IDM _id")
    p.add_argument("user_id")

    p = sub.add_parser("groups", help="list a user's group memberships")
    p.add_argument("user_id")
    p.add_argument(
        "--kind", choices=("groups", "authzRoles"), default="groups"
    )

    p = sub.add_parser("assignments", help="list a user's direct assignments")
    p.add_argument("user_id")

    p = sub.add_parser("activity", help="recent audit events for a user")
    p.add_argument("username")
    p.add_argument(
        "--source",
        choices=(
            "am-authentication",
            "am-access",
            "am-activity",
            "idm-access",
        ),
        default="am-authentication",
    )
    p.add_argument("--page-size", type=int, default=20)

    args = parser.parse_args()
    client = PingClient()
    try:
        if args.cmd == "search-users":
            _dump(client.search_users(args.q, page_size=args.page_size))
        elif args.cmd == "get-user":
            _dump(client.get_user(args.user_id))
        elif args.cmd == "groups":
            _dump(client.list_user_relationship(args.user_id, args.kind))
        elif args.cmd == "assignments":
            _dump(client.list_user_relationship(args.user_id, "assignments"))
        elif args.cmd == "activity":
            _dump(
                client.get_audit_events(
                    source=args.source,
                    username=args.username,
                    page_size=args.page_size,
                )
            )
    except PingError as exc:
        print(f"PingError: {exc}", file=sys.stderr)
        sys.exit(2)
    finally:
        client.close()


if __name__ == "__main__":
    main()
