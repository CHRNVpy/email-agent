"""Command-line administration.

python -m app.cli auth                         # authorise the agent's Google account
python -m app.cli init                         # create tables, seed roles, create ADMIN_EMAIL
python -m app.cli users add alice@corp.com --role analyst
python -m app.cli users show alice@corp.com
python -m app.cli perms grant alice@corp.com "sql:crm:write" --expires 2026-12-31
python -m app.cli index drive                  # (re)index DRIVE_FOLDER_IDS into Qdrant
python -m app.cli ask "What is Apple's P/E ratio?" --sender alice@corp.com
"""

import argparse
import asyncio
import json
import logging
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app import telemetry
from app.config import settings
from app.permissions import service
from app.permissions.policy import DEFAULT_PERMISSIONS


async def cmd_init(_: argparse.Namespace) -> None:
    from app.main import bootstrap

    await bootstrap()
    print(f"App database ready at {settings.app_database_url}")


async def cmd_auth(_: argparse.Namespace) -> None:
    from app.google.auth import run_oauth_flow

    run_oauth_flow()
    print(f"Token saved to {settings.google_token_file}")


async def cmd_users(args: argparse.Namespace) -> None:
    if args.action == "add":
        await service.create_user(args.email, args.name, args.role or None)
        print(f"Created {args.email}")
    elif args.action == "list":
        for user in await service.list_users():
            state = "" if user.is_active else " (inactive)"
            print(f"{user.email:40} {', '.join(r.name for r in user.roles)}{state}")
    elif args.action == "show":
        principal = await service.resolve_principal(args.email)
        print(
            json.dumps(
                {
                    "email": principal.email,
                    "registered": principal.user_id is not None,
                    "granted": sorted(principal.granted),
                    "denied": sorted(principal.denied),
                },
                indent=2,
            )
        )


async def cmd_roles(args: argparse.Namespace) -> None:
    if args.action == "list":
        for role in await service.list_roles():
            print(f"{role.name:12} {', '.join(sorted(p.name for p in role.permissions))}")
    elif args.action in ("assign", "remove"):
        await service.set_role(args.email, args.role, assign=args.action == "assign")
        print("Done")
    elif args.action == "add-permission":
        await service.add_role_permission(args.role, args.permission)
        print("Done")


async def cmd_perms(args: argparse.Namespace) -> None:
    if args.action == "list":
        for name, description in DEFAULT_PERMISSIONS.items():
            print(f"{name:14} {description}")
        return
    if args.action == "clear":
        await service.clear_override(args.email, args.permission)
    else:
        expires = datetime.fromisoformat(args.expires) if args.expires else None
        await service.set_override(args.email, args.permission, granted=args.action == "grant", expires_at=expires)
    print("Done")


async def cmd_index(args: argparse.Namespace) -> None:
    from app.rag import indexers

    if args.source == "drive":
        chunks = await indexers.index_drive_folders(args.folder or None)
    elif args.source == "files":
        if not args.path:
            raise SystemExit("index files needs a directory: python -m app.cli index files ./docs")
        chunks = await indexers.index_local_files(Path(args.path))
    else:
        chunks = await indexers.index_sql_tables()
    print(f"Indexed {chunks} chunks into '{settings.qdrant_collection}'")


async def cmd_workflows(_: argparse.Namespace) -> None:
    from app.workflows.sheet import load_workflows

    for workflow in load_workflows(force=True):
        sources = ", ".join(f"{n} ({s.kind}{', iterate' if s.iterate else ''})" for n, s in workflow.sources.items())
        print(f"{workflow.name}\n  stages: {len(workflow.stages)}  actions: {workflow.actions or 'default'}")
        print(f"  sources: {sources or '-'}")


async def cmd_ask(args: argparse.Namespace) -> None:
    """Run the agent on a text as if it had arrived by email (no Gmail involved)."""
    from app.messages import IncomingEmail
    from app.pipeline import answer

    await cmd_init(args)
    principal = await service.resolve_principal(args.sender)
    service.set_principal(principal)
    email = IncomingEmail(id="cli", thread_id="cli", sender=args.sender, subject=args.subject, body=args.text)
    with telemetry.trace() as trace:
        reply = await answer(email, principal, args.model)
    print(reply)
    if args.stats:
        print("\n---\n" + json.dumps(trace.summary(), indent=2, default=str))


async def cmd_poll(args: argparse.Namespace) -> None:
    """Answer emails by polling the inbox — for local runs without Pub/Sub or a public URL."""
    from app.pipeline import poll_inbox

    await cmd_init(args)
    logging.getLogger("app").setLevel(logging.INFO)
    since = datetime.now(UTC) - timedelta(minutes=args.backlog)
    print(f"Watching {settings.agent_email} every {args.interval}s (Ctrl+C to stop)")
    while True:
        try:
            handled = await poll_inbox(since)
            if handled:
                print(f"Handled {handled} new message(s)")
        except Exception as exc:  # network hiccups must not stop the loop
            logging.getLogger(__name__).error("Poll failed: %s", exc)
        if args.once:
            return
        await asyncio.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Email Agent administration")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create tables and seed default roles").set_defaults(func=cmd_init)
    sub.add_parser("auth", help="Authorise the agent's Google account").set_defaults(func=cmd_auth)

    users = sub.add_parser("users", help="Manage users")
    users.add_argument("action", choices=["add", "list", "show"])
    users.add_argument("email", nargs="?")
    users.add_argument("--name")
    users.add_argument("--role", action="append", help="Role to assign (repeatable)")
    users.set_defaults(func=cmd_users)

    roles = sub.add_parser("roles", help="Manage roles")
    roles.add_argument("action", choices=["list", "assign", "remove", "add-permission"])
    roles.add_argument("args", nargs="*", help="assign|remove: EMAIL ROLE; add-permission: ROLE PERMISSION")
    roles.set_defaults(func=cmd_roles)

    perms = sub.add_parser("perms", help="Per-user permission overrides")
    perms.add_argument("action", choices=["list", "grant", "deny", "clear"])
    perms.add_argument("email", nargs="?")
    perms.add_argument("permission", nargs="?", help="Pattern, e.g. sheets:write or sql:crm:*")
    perms.add_argument("--expires", help="ISO date/time when a grant expires")
    perms.set_defaults(func=cmd_perms)

    index = sub.add_parser("index", help="Load sources into the knowledge base")
    index.add_argument("source", choices=["drive", "sql", "files"])
    index.add_argument("path", nargs="?", help="Directory with .md/.txt files (for `files`)")
    index.add_argument("--folder", action="append", help="Drive folder id/URL (default: DRIVE_FOLDER_IDS)")
    index.set_defaults(func=cmd_index)

    sub.add_parser("workflows", help="List workflows defined in the sheet").set_defaults(func=cmd_workflows)

    poll = sub.add_parser("poll", help="Answer emails by polling the inbox (no Pub/Sub needed)")
    poll.add_argument("--interval", type=int, default=15, help="Seconds between checks")
    poll.add_argument("--backlog", type=int, default=0, help="Also handle messages from the last N minutes")
    poll.add_argument("--once", action="store_true", help="Check once and exit")
    poll.set_defaults(func=cmd_poll)

    ask = sub.add_parser("ask", help="Ask the agent directly, bypassing Gmail")
    ask.add_argument("text")
    ask.add_argument("--sender", default=settings.admin_email or "cli@localhost")
    ask.add_argument("--subject", default="CLI request")
    ask.add_argument("--model")
    ask.add_argument("--stats", action="store_true", help="Print latency per stage, tokens and cost")
    ask.set_defaults(func=cmd_ask)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "roles" and args.action != "list":
        if len(args.args) != 2:
            raise SystemExit("roles assign|remove EMAIL ROLE  /  roles add-permission ROLE PERMISSION")
        if args.action == "add-permission":
            args.role, args.permission = args.args
        else:
            args.email, args.role = args.args
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    # langchain-core installs its own warning filters on import; silence upstream pending deprecations.
    import langchain_core._api.deprecation  # noqa: F401

    warnings.filterwarnings("ignore", category=PendingDeprecationWarning)
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
