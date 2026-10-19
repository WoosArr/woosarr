"""Recovery commands for the container console (no web UI needed).

    docker exec -it woosarr python -m woosarr.admin list
    docker exec -it woosarr python -m woosarr.admin reset-password <username>
    docker exec -it woosarr python -m woosarr.admin unlock <username>

On Unraid: Docker tab → WooSarr icon → Console, then run the command without
the `docker exec -it woosarr` prefix.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from sqlalchemy import func, select

from woosarr.core import auth
from woosarr.core.config import Settings
from woosarr.db.models import AdminUser
from woosarr.db.session import Database, make_engine, migrate
from woosarr.services import audit


def _db(settings: Settings) -> Database:
    engine = make_engine(settings.db_path)
    migrate(engine)
    return Database(engine)


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m woosarr.admin", description="WooSarr admin recovery")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list admin accounts")
    reset = sub.add_parser("reset-password", help="set a new password, unlock and enable the account")
    reset.add_argument("username")
    reset.add_argument("--generate", action="store_true", help="generate a random password and print it")
    unlock = sub.add_parser("unlock", help="clear a failed-login lockout")
    unlock.add_argument("username")
    args = parser.parse_args(argv)

    db = _db(settings or Settings())
    with db.session() as s:
        if args.cmd == "list":
            for u in s.scalars(select(AdminUser).order_by(func.lower(AdminUser.username), AdminUser.username)):
                state = "active" if u.is_active else "disabled"
                locked = " locked" if u.locked_until and u.locked_until > auth.utcnow() else ""
                print(f"{u.username}\t{u.auth_source}\t{state}{locked}")
            return 0
        user = s.scalar(select(AdminUser).where(AdminUser.username == args.username))
        if user is None:
            print(f"no admin named {args.username!r}", file=sys.stderr)
            return 1
        if args.cmd == "unlock":
            user.locked_until, user.failed_logins = None, 0
            audit.record(s, "console", "admin_unlocked", "admin", str(user.id))
            print(f"{user.username} unlocked")
            return 0
        if args.generate:
            password = secrets.token_urlsafe(18)
        else:
            password = getpass.getpass("New password: ")
            if password != getpass.getpass("Repeat: "):
                print("passwords don't match", file=sys.stderr)
                return 1
        try:
            user.password_hash = auth.hash_password(password)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        user.auth_source, user.is_active, user.locked_until, user.failed_logins = "local", True, None, 0
        auth.end_all_sessions(s, user.id)
        audit.record(s, "console", "admin_password_reset", "admin", str(user.id))
        print(f"password for {user.username} reset; all its sessions were signed out")
        if args.generate:
            print(f"new password: {password}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
