"""Recovery commands for the container console (no web UI needed).

    docker exec -it woosarr python -m woosarr.admin list
    docker exec -it woosarr python -m woosarr.admin reset-password <username>
    docker exec -it woosarr python -m woosarr.admin unlock <username>
    docker exec -i woosarr python -m woosarr.admin export [--parts settings,libraries] [--secrets] > woosarr-config.json
    docker exec -i woosarr python -m woosarr.admin import - --yes < woosarr-config.json

On Unraid: Docker tab → WooSarr icon → Console, then run the command without
the `docker exec -it woosarr` prefix.
"""

from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
from pathlib import Path

from sqlalchemy import func, select

from woosarr.core import auth
from woosarr.core.config import Settings
from woosarr.core.secret_store import SecretStore
from woosarr.core.secrets import SecretBox, load_or_create_key
from woosarr.db.models import AdminUser
from woosarr.db.session import Database, make_engine, migrate
from woosarr.services import audit, config_import, config_transfer


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
    export = sub.add_parser("export", help="print this server's configuration (redirect it to a file on your computer)")
    export.add_argument("--parts", default="", help=f"comma-separated, default all: {','.join(config_transfer.PARTS)}")
    export.add_argument("--secrets", action="store_true",
                        help="include tokens and secrets, encrypted with a passphrase (asked for)")
    imp = sub.add_parser("import", help="import an export file (preview, then apply after confirming)")
    imp.add_argument("file", help="path inside the container, or - to read it from standard input")
    imp.add_argument("--parts", default="", help=f"comma-separated, default everything in the file: "
                                                 f"{','.join(config_import.IMPORT_PARTS)}")
    imp.add_argument("--secrets", action="store_true", help="import tokens and secrets (asks for the passphrase)")
    imp.add_argument("--yes", action="store_true", help="apply without asking (required when the file is -)")
    args = parser.parse_args(argv)

    settings = settings or Settings()
    if args.cmd == "export":
        return _export(settings, args)
    if args.cmd == "import":
        return _import(settings, args)
    db = _db(settings)
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
            was_locked = bool(user.locked_until and user.locked_until > auth.utcnow())
            user.locked_until, user.failed_logins = None, 0
            audit.record(s, "console", "admin_unlocked", "admin", str(user.id), username=user.username,
                         was_locked=was_locked)
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
        # What the reset changed besides the password, for the audit trail (the password never goes there).
        details = {"username": user.username, "generated": bool(args.generate),
                   "was_locked": bool(user.locked_until and user.locked_until > auth.utcnow()),
                   "was_disabled": not user.is_active}
        if user.auth_source != "local":
            details["sign_in_was"] = user.auth_source
        user.auth_source, user.is_active, user.locked_until, user.failed_logins = "local", True, None, 0
        details["sessions_ended"] = auth.end_all_sessions(s, user.id)
        audit.record(s, "console", "admin_password_reset", "admin", str(user.id), **details)
        print(f"password for {user.username} reset; all its sessions were signed out")
        if args.generate:
            print(f"new password: {password}")
        return 0


if __name__ == "__main__":
    sys.exit(main())


def _export(settings: Settings, args: argparse.Namespace) -> int:
    """Writes the export to stdout, never to a file on the server: run it from the host with
    `docker exec -i woosarr python -m woosarr.admin export > woosarr-config.json`."""
    if sys.stdout.isatty():
        print("the export goes to standard output; redirect it to a file on your computer:\n"
              "  docker exec -i woosarr python -m woosarr.admin export > woosarr-config.json\n"
              "or use Settings → Export in the web UI", file=sys.stderr)
        return 1
    parts = [p.strip() for p in args.parts.split(",") if p.strip()]
    passphrase = None
    if args.secrets:
        passphrase = getpass.getpass(f"Passphrase for the secrets (at least {config_transfer.PASSPHRASE_MIN} characters): ")
        if passphrase != getpass.getpass("Repeat: "):
            print("passphrases don't match", file=sys.stderr)
            return 1
    store = SecretStore(SecretBox(load_or_create_key(settings.config_dir)))
    db = _db(settings)
    try:
        with db.session() as s:
            doc = config_transfer.export(s, store, parts, passphrase, args.secrets)
            summary = doc["summary"]
            audit.record(s, "console", "config_exported", None, None, parts=summary["parts"],
                         counts=summary["counts"], encrypted=summary["encrypted"])
    except config_transfer.TransferError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    json.dump(doc, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    counts = ", ".join(f"{k} {v}" for k, v in summary["counts"].items())
    print(f"exported {', '.join(summary['parts'])}" + (f" ({counts})" if counts else "")
          + (f"; {summary['encrypted']} value(s) encrypted with the passphrase" if args.secrets else "; no tokens"),
          file=sys.stderr)
    for name in summary["unreadable"]:
        print(f"warning: {name} couldn't be decrypted with this server's master key and was left out", file=sys.stderr)
    return 0


def _import(settings: Settings, args: argparse.Namespace) -> int:
    if args.file == "-" and not args.yes:
        print("reading the file from standard input needs --yes (there's no way to ask for confirmation)",
              file=sys.stderr)
        return 1
    try:
        raw = json.load(sys.stdin) if args.file == "-" else json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"can't read {args.file}: {exc}", file=sys.stderr)
        return 1
    parts = [p.strip() for p in args.parts.split(",") if p.strip()]
    if not args.secrets:
        parts = [p for p in (parts or config_import.IMPORT_PARTS) if p != config_import.SECRETS]
    passphrase = getpass.getpass("Passphrase for the secrets: ") if args.secrets else None
    store = SecretStore(SecretBox(load_or_create_key(settings.config_dir)))
    db = _db(settings)
    try:
        doc = config_import.parse(raw)
        with db.session() as s:
            preview = config_import.run(s, store, doc, parts, passphrase, apply=False)
            s.rollback()
        _print_result(doc, preview, "would import")
        if not args.yes and input("Import this? Type yes: ").strip().lower() != "yes":
            print("nothing imported", file=sys.stderr)
            return 1
        with db.session() as s:
            result = config_import.run(s, store, doc, parts, passphrase, apply=True, actor="console")
            audit.record(s, "console", "config_imported", None, None,
                         parts={p.name: p.counts for p in result.parts}, source_plex=doc.source.plex_server,
                         source_discord=doc.source.discord_guild, exported_at=doc.exported_at,
                         encrypted=result.encrypted_count, warnings=len(result.warnings))
    except config_transfer.TransferError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print_result(doc, result, "imported")
    print("dry-run is on. Restart the container so the running app picks up the import, review the planned "
          "changes, then turn dry-run off under Settings → General.", file=sys.stderr)
    return 0


def _print_result(doc: config_import.ExportFile, result: config_import.Result, verb: str) -> None:
    src = doc.source
    print(f"{verb} from {src.plex_server or '?'} / {src.discord_guild or '?'} (exported {doc.exported_at or '?'}):",
          file=sys.stderr)
    for part in result.parts:
        print(f"  {part.name}: {part.summary}", file=sys.stderr)
    for warning in result.warnings:
        print(f"  warning: {warning}", file=sys.stderr)
