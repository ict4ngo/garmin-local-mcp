"""Command-line entry point.

The CLI is useful standalone (sync/backfill/status) — the MCP server is one
subcommand among peers, so the warehouse has value even without an MCP client.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .config import load as load_config


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    return serve(args.config)


def cmd_login(args: argparse.Namespace) -> int:
    from .login import run_login

    return run_login(args.config, status_only=args.status, logout=args.logout)


def cmd_sync(args: argparse.Namespace) -> int:
    from datetime import date, timedelta

    from . import db, sync
    from .auth import AuthError, get_client
    from .endpoints import ENDPOINTS

    config = args.config
    config.ensure_dirs()
    try:
        end = args.end or sync.yesterday(config)
        start = args.start or (date.fromisoformat(end) - timedelta(days=6)).isoformat()
        for value in (start, end):  # validate before logging in
            date.fromisoformat(value)
        if start > end:
            raise ValueError(f"start {start} is after end {end}")
    except ValueError as e:
        print(f"Invalid date: {e}", file=sys.stderr)
        return 2

    endpoints = None
    if args.endpoints:
        endpoints = [name.strip() for name in args.endpoints.split(",") if name.strip()]
        unknown = [name for name in endpoints if name not in ENDPOINTS]
        if unknown:
            print(
                f"Unknown endpoints: {', '.join(unknown)}. Known: {', '.join(ENDPOINTS)}",
                file=sys.stderr,
            )
            return 2

    try:
        client = get_client(config.tokens_dir)
    except AuthError as e:
        print(f"{e} ({e.hint})", file=sys.stderr)
        return 1

    conn = db.connect(config.database_url)
    try:
        report = sync.sync_range(
            config, conn, client, start, end, endpoints=endpoints, force=args.force
        )
    finally:
        conn.close()
    print(f"sync {report['start']}..{report['end']}: "
          f"{report['requests']} requests, {report['rows']} rows")
    for name, s in report["endpoints"].items():
        print(f"  {name}: ok={s['ok']} empty={s['empty']} skipped={s['skipped']} "
              f"error={s['error']}")
    if report.get("auth_error"):
        err = report["auth_error"]
        print(f"auth error: {err['error']} ({err['hint']})", file=sys.stderr)
        return 1
    if report["aborted"]:
        print("aborted (rate limited); re-run the same command to resume", file=sys.stderr)
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from . import db

    config = args.config
    conn = db.connect(config.database_url)
    print(f"data dir: {config.data_dir}")
    print("coverage:")
    has_data = False
    for table in ("daily_wellness", "sleep", "hrv", "training_status", "activities"):
        coverage = conn.execute(
            f"SELECT MIN(date) AS first, MAX(date) AS last, COUNT(*) AS count FROM {table}"  # noqa: S608 - fixed names
        ).fetchone()
        has_data = has_data or coverage["count"] > 0
        span = (
            f"{coverage['first']} .. {coverage['last']}"
            if coverage["count"]
            else "(no data)"
        )
        print(f"  {table:16} {coverage['count']:6} rows  {span}")
    if not has_data:
        print("No database rows yet - run: garmin-local-mcp sync")
        conn.close()
        return 0
    errors = conn.execute(
        "SELECT endpoint, date, last_error FROM sync_state "
        "WHERE status='error' ORDER BY date, endpoint"
    ).fetchall()
    if errors:
        print(f"pending sync errors ({len(errors)}):")
        for row in errors:
            print(f"  {row['date']} {row['endpoint']}: {row['last_error']}")
    else:
        print("pending sync errors: none")
    conn.close()
    return 0


def cmd_import_fit(args: argparse.Namespace) -> int:
    from . import db
    from .importer import import_bundle

    config = args.config
    config.ensure_dirs()
    conn = db.connect(config.database_url)
    try:
        report = import_bundle(conn, args.folder, force=args.force, raw_dir=config.raw_dir)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"date: {report['date']}")
    print(f"imported: {', '.join(report['imported']) or '(nothing)'}")
    if report["skipped"]:
        print(f"skipped: {', '.join(report['skipped'])}")
    if report["quality_flags"]:
        print(f"quality flags: {', '.join(report['quality_flags'])}")
    return 0


def cmd_migrate_sqlite(args: argparse.Namespace) -> int:
    from .sqlite_import import migrate_sqlite

    try:
        report = migrate_sqlite(args.source, args.config.database_url)
    except Exception as e:
        print(f"SQLite migration failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from . import db, demo

    config = args.config
    config.ensure_dirs()
    conn = db.connect(config.database_url)
    generated = demo.is_demo(conn)
    has_data = any(
        conn.execute(f"SELECT EXISTS (SELECT 1 FROM {table}) AS present").fetchone()["present"]
        for table in (
            "daily_wellness", "sleep", "hrv", "activities", "training_status",
            "raw_snapshots", "sync_state",
        )
    )
    if has_data and not generated and not args.force:
        conn.close()
        print(
            "The PostgreSQL warehouse already contains real data and does not look like a demo.\n"
            "Refusing to overwrite real data. Use a separate GARMIN_MCP_DATABASE_URL\n"
            "for demo data,\n"
            "or pass --force if you are certain.",
            file=sys.stderr,
        )
        return 2
    if has_data:
        with conn.transaction():
            for table in (
                "daily_wellness", "sleep", "hrv", "activities", "training_status",
                "raw_snapshots", "sync_state", "demo_info",
            ):
                conn.execute(f"DELETE FROM {table}")
    try:
        report = demo.generate(conn, days=args.days, end=args.end, seed=args.seed)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    finally:
        # Windows will not let the next run unlink the file otherwise.
        conn.close()

    total = sum(report["rows"].values())
    print("Generated a demo store in the configured PostgreSQL warehouse")
    print(f"  {report['days']} days, {report['start']} .. {report['end']}, {total} rows")
    for table, count in report["rows"].items():
        print(f"    {table:16} {count:5}")
    ill_start, ill_end = report["illness_window"]
    print(f"  seeded illness window: {ill_start} .. {ill_end}")
    print(f"  sleep nights left missing: {', '.join(report['missing_sleep'])}")
    print("\nThis data is generated, not recorded. Try:")
    print(f"  garmin-local-mcp --data-dir {config.data_dir} status")
    print(f"  garmin-local-mcp --data-dir {config.data_dir} serve   # then ask an MCP client")
    return 0


def cmd_reparse(args: argparse.Namespace) -> int:
    from . import db, sync

    config = args.config
    config.ensure_dirs()
    conn = db.connect(config.database_url)
    try:
        report = sync.reparse(config, conn)
    finally:
        conn.close()
    if report["unknown_endpoints"]:
        print(f"skipped {report['unknown_endpoints']} snapshots from unknown endpoints")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="garmin-local-mcp",
        description=(
            "Local-first Garmin data warehouse with an analysis-grade MCP server. "
            "Sync once, analyze forever - even when the API is down."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--data-dir",
        help="Data directory (default: GARMIN_MCP_DATA_DIR or ~/.garmin-mcp)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="Run the stdio MCP server (never prompts)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("login", help="Interactive Garmin Connect login (supports MFA)")
    p.add_argument("--status", action="store_true", help="Show token status and exit")
    p.add_argument("--logout", action="store_true", help="Delete stored tokens and exit")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("sync", help="Sync daily wellness and activities into the local store")
    p.add_argument("--from", dest="start", metavar="DATE", help="Start date (YYYY-MM-DD)")
    p.add_argument("--to", dest="end", metavar="DATE", help="End date (default: yesterday)")
    p.add_argument("--endpoints", help="Comma-separated endpoint subset")
    p.add_argument("--force", action="store_true", help="Re-fetch even if already synced")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("status", help="Show sync coverage and pending errors")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("import-fit", help="Offline ingest of an exported wellness .fit bundle")
    p.add_argument("folder", help="Folder containing the exported .fit files")
    p.add_argument(
        "--force", action="store_true", help="Overwrite even rows sourced from the API"
    )
    p.set_defaults(func=cmd_import_fit)

    p = sub.add_parser(
        "migrate-sqlite", help="Copy a legacy SQLite database into the PostgreSQL warehouse"
    )
    p.add_argument("--from", dest="source", required=True, metavar="PATH")
    p.set_defaults(func=cmd_migrate_sqlite)

    from .demo import DEMO_DAYS, DEMO_SEED

    p = sub.add_parser(
        "demo", help="Seed a synthetic store so the tools can be tried without a Garmin account"
    )
    p.add_argument("--days", type=int, default=DEMO_DAYS, help="Days to generate")
    p.add_argument("--end", metavar="DATE", help="Last day (default: yesterday)")
    p.add_argument("--seed", type=int, default=DEMO_SEED, help="Deterministic seed")
    p.add_argument(
        "--force", action="store_true", help="Overwrite an existing non-demo database"
    )
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("reparse", help="Rebuild the database from raw snapshots (offline)")
    p.set_defaults(func=cmd_reparse)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.config = load_config(args.data_dir)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
