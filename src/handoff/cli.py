"""Command line entry points: createuser, logout-all, reap, serve."""

import argparse
import getpass
import os
import sqlite3
import sys

from handoff import auth, db

MIN_PASSWORD_LEN = 12


def user_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) c FROM users").fetchone()["c"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handoff")
    parser.add_argument("--db", default=os.environ.get("HANDOFF_DB", "handoff.db"))
    parser.add_argument(
        "--ttl-days", type=int, default=int(os.environ.get("HANDOFF_TTL_DAYS", "7"))
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("createuser", help="create a human login")
    create.add_argument("username")

    logout_all = sub.add_parser("logout-all", help="invalidate every session for a user")
    logout_all.add_argument("username")

    sub.add_parser("reap", help="delete expired folders now")

    serve = sub.add_parser("serve", help="run the service")
    serve.add_argument("--bind", default=os.environ.get("HANDOFF_BIND", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("HANDOFF_PORT", "8080")))
    return parser


def _createuser(conn: sqlite3.Connection, username: str) -> int:
    password = getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LEN:
        print(f"Password must be at least {MIN_PASSWORD_LEN} characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Confirm: ") != password:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    try:
        auth.create_user(conn, username, password)
    except sqlite3.IntegrityError:
        print(f"User already exists: {username}", file=sys.stderr)
        return 1
    print(f"Created user {username}.")
    return 0


def _logout_all(conn: sqlite3.Connection, username: str) -> int:
    try:
        count = auth.delete_all_sessions(conn, username)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Deleted {count} sessions for {username}.")
    return 0


def _serve(db_path: str, ttl_days: int, bind: str, port: int, conn: sqlite3.Connection) -> int:
    if bind == "0.0.0.0":  # noqa: S104
        print(
            "Refusing to bind 0.0.0.0. Bind a specific interface (your tailnet address)"
            " or 127.0.0.1 behind a reverse proxy.",
            file=sys.stderr,
        )
        return 1
    if user_count(conn) == 0:
        print(
            "No users exist. Run 'handoff createuser <name>' before serving.",
            file=sys.stderr,
        )
        return 1

    import uvicorn

    from handoff.app import create_app

    # proxy_headers honours X-Forwarded-Proto, so a TLS-terminating reverse proxy makes
    # request.url.scheme == "https" and the session cookie comes back marked Secure.
    uvicorn.run(
        create_app(db_path, ttl_days), host=bind, port=port,
        proxy_headers=True, forwarded_allow_ips="127.0.0.1",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    try:
        if args.command == "createuser":
            return _createuser(conn, args.username)
        if args.command == "logout-all":
            return _logout_all(conn, args.username)
        if args.command == "reap":
            print(f"Deleted {db.reap(conn)} expired folders.")
            return 0
        if args.command == "serve":
            return _serve(args.db, args.ttl_days, args.bind, args.port, conn)
    finally:
        conn.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
