"""Recovery CLI for the user account store (forgot-password / account audit).

Minimal offline tool — the service itself has no "reset by email" path. Run it
inside the container so it sees the data volume (stop the service first, see
below; ``run --rm`` creates a throwaway container instead of exec'ing into the
stopped one):

    docker compose run --rm anki-mcp python -m src.useradmin list
    docker compose run --rm anki-mcp python -m src.useradmin reset-password alice
    docker compose run --rm anki-mcp python -m src.useradmin set-role alice admin

The store path comes from --users-file or the USERS_FILE environment variable,
defaulting to /data/users.json (the container path).

IMPORTANT: stop the service first (``docker compose stop anki-mcp``). The
running service loads the store once at startup and writes its in-memory copy
back atomically on every account change, so external edits made while it runs
are invisible to it and will be overwritten by its next save. Start it again
with ``docker compose start anki-mcp``.
"""

import argparse
import getpass
import os
import sys

from src.users import UserStore

DEFAULT_USERS_FILE = "/data/users.json"


def _open_store(args: argparse.Namespace) -> UserStore:
    path = args.users_file or os.getenv("USERS_FILE") or DEFAULT_USERS_FILE
    try:
        return UserStore(path)
    except (OSError, ValueError) as e:
        # A missing file yields an empty store instead of raising; a corrupt or
        # unreadable one raises — for an auth store that must be loud, and here
        # it almost certainly means a wrong path or a damaged volume.
        print(f"ERROR: cannot open user store at {path!r}: {e}", file=sys.stderr)
        raise SystemExit(1)


def cmd_list(args: argparse.Namespace) -> int:
    store = _open_store(args)
    users = sorted(store.list_users(), key=lambda u: u.username)
    if not users:
        print(f"No accounts found in {store.file_path} (a missing file reads as an empty store).")
        return 0
    print(f"{len(users)} account(s) in {store.file_path}:")
    for user in users:
        line = f"  {user.username}  role={user.role}"
        if not user.token_plaintext:
            line += "  (legacy record: portal cannot show token until regenerated)"
        print(line)
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    store = _open_store(args)
    if args.username not in {u.username for u in store.list_users()}:
        print(f"ERROR: unknown username: {args.username!r}", file=sys.stderr)
        return 1
    new_password = getpass.getpass(f"New password for {args.username!r}: ")
    if not new_password:
        print("ERROR: password must not be empty.", file=sys.stderr)
        return 1
    confirm = getpass.getpass("Confirm new password: ")
    if new_password != confirm:
        print("ERROR: passwords do not match.", file=sys.stderr)
        return 1
    try:
        store.set_password(args.username, new_password)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    # The password change bumps the user's sessions_invalid_before epoch, so
    # once the service restarts and reloads the store (this CLI must run with
    # the service stopped) web sessions issued before the reset stop
    # authenticating. Start the service again with: docker compose start anki-mcp
    print(f"Password updated for {args.username!r}.")
    return 0


def cmd_set_role(args: argparse.Namespace) -> int:
    store = _open_store(args)
    if args.role not in ("admin", "user"):
        print(f"ERROR: role must be 'admin' or 'user', got {args.role!r}.", file=sys.stderr)
        return 1
    users = store.list_users()
    if args.username not in {u.username for u in users}:
        print(f"ERROR: unknown username: {args.username!r}", file=sys.stderr)
        return 1
    if args.role != "admin" and not any(
        u.role == "admin" and u.username != args.username for u in users
    ):
        print(
            f"ERROR: cannot demote {args.username!r}: it is the last admin account. "
            "Promote another account to admin first.",
            file=sys.stderr,
        )
        return 1
    store.set_role(args.username, args.role)
    print(f"Role of {args.username!r} set to {args.role}.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.useradmin",
        description="Offline recovery CLI for the Anki MCP user store.",
    )
    parser.add_argument(
        "--users-file",
        default="",
        help="Path to users.json (default: the USERS_FILE env var, else /data/users.json).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all accounts and their roles.")

    p_reset = sub.add_parser("reset-password", help="Set a new password for an account.")
    p_reset.add_argument("username", help="Account to update.")

    p_role = sub.add_parser(
        "set-role", help="Set an account's role (admin|user) — the lockout recovery path."
    )
    p_role.add_argument("username", help="Account to update.")
    p_role.add_argument("role", choices=["admin", "user"], help="New role.")

    args = parser.parse_args(argv)
    if args.command == "list":
        return cmd_list(args)
    if args.command == "reset-password":
        return cmd_reset_password(args)
    return cmd_set_role(args)


if __name__ == "__main__":
    raise SystemExit(main())
