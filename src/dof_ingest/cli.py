"""Command line interface.

argparse rather than Typer/Click: seven subcommands with scalar options is
exactly what argparse is for, and a CLI framework would be the project's third
dependency in exchange for prettier `--help`.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from . import research as research_mod
from .config import EDITIONS, INDEX_BASE, Settings
from .http import CircuitOpen, PoliteClient
from .pipeline import discover, enrich
from .store import RunCounters, Store


def _setup_logging(verbose: bool, as_json: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    if as_json:
        # Structured logs so a run in CI or a scheduler is greppable by field
        # instead of by regex over prose.
        class JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                return json.dumps(
                    {
                        "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                        "level": record.levelname,
                        "logger": record.name,
                        "msg": record.getMessage(),
                    },
                    ensure_ascii=False,
                )

        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logging.basicConfig(level=level, handlers=[handler])
    else:
        logging.basicConfig(
            level=level, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"
        )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _resolve_range(args: argparse.Namespace) -> tuple[date, date]:
    if args.last_days:
        until = date.today()
        return until - timedelta(days=args.last_days - 1), until
    since = date.fromisoformat(args.since)
    until = date.fromisoformat(args.until) if args.until else since
    if until < since:
        raise SystemExit("--until is before --since")
    return since, until


def _report(counters: RunCounters, client: PoliteClient) -> None:
    s = client.stats
    print(f"\n  {counters.as_line()}")
    print(
        f"  http: {s.requests} requests, {s.retries} retries, "
        f"{s.bytes_down / 1024:.0f} KiB, {s.slept_s:.1f}s spent being polite"
    )
    for line in counters.detail[:10]:
        print(f"    ! {line}")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_research(args: argparse.Namespace, settings: Settings) -> int:
    """Re-run the API and bulk-source probes live."""
    with PoliteClient(settings) as client:
        results = research_mod.probe_all(client, args.fecha)
    research_mod.render(results, as_json=args.json)
    return 0


def cmd_discover(args: argparse.Namespace, settings: Settings) -> int:
    since, until = _resolve_range(args)
    editions = tuple(args.editions.split(",")) if args.editions else EDITIONS
    with Store(settings.db_path) as store, PoliteClient(settings) as client:
        run_id = store.start_run(f"discover {since}..{until} {','.join(editions)}")
        try:
            counters = discover(
                store, client, settings, since, until, editions, INDEX_BASE, args.recheck_days
            )
            store.finish_run(run_id, counters, vars(client.stats))
        except CircuitOpen as exc:
            store.finish_run(run_id, RunCounters(errors=1, detail=[str(exc)]),
                             vars(client.stats), status="aborted")
            print(f"aborted: {exc}", file=sys.stderr)
            return 2
        _report(counters, client)
    return 0


def cmd_enrich(args: argparse.Namespace, settings: Settings) -> int:
    with Store(settings.db_path) as store, PoliteClient(settings) as client:
        run_id = store.start_run(f"enrich limit={args.limit}")
        try:
            counters = enrich(store, client, args.limit, retry_errors=args.retry_errors)
            store.finish_run(run_id, counters, vars(client.stats))
        except CircuitOpen as exc:
            store.finish_run(run_id, RunCounters(errors=1, detail=[str(exc)]),
                             vars(client.stats), status="aborted")
            print(f"aborted: {exc}", file=sys.stderr)
            return 2
        _report(counters, client)
    return 0


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    rc = cmd_discover(args, settings)
    return rc or cmd_enrich(args, settings)


def cmd_stats(args: argparse.Namespace, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        stats = store.stats()
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            return 0
        width = max(len(k) for k in stats)
        print(f"\n  {settings.db_path}\n")
        for key, value in stats.items():
            print(f"  {key.replace('_', ' '):<{width}}  {value}")
        print("\n  últimas corridas:")
        print(f"    {'id':>3}  {'comando':<34} {'ins':>5} {'upd':>5} {'unch':>6} "
              f"{'robots':>7} {'err':>4}")
        for r in store.recent_runs(args.limit):
            print(
                f"    {r['run_id']:>3}  {r['command'][:34]:<34} {r['inserted']:>5} "
                f"{r['updated']:>5} {r['unchanged']:>6} {r['skipped_robots']:>7} {r['errors']:>4}"
            )
    return 0


def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        if args.out == "-":
            n = store.export(sys.stdout, args.format, args.include_body)
        else:
            path = Path(args.out)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="") as fh:
                n = store.export(fh, args.format, args.include_body)
            print(f"  {n} notas -> {path}")
    return 0


def cmd_robots(args: argparse.Namespace, settings: Settings) -> int:
    """Explain, for a given URL, what robots.txt says and why."""
    with PoliteClient(settings) as client:
        allowed = client.robots.allows(args.url)
        delay = client.robots.crawl_delay(args.url)
    print(f"\n  {args.url}")
    print(f"  user-agent : {settings.user_agent}")
    print(f"  verdict    : {'ALLOWED' if allowed else 'DISALLOWED'}")
    print(f"  crawl-delay: {delay if delay is not None else 'not declared'}\n")
    return 0 if allowed else 1


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dof-ingest",
        description="Idempotent, robots-aware ingestion of the Diario Oficial de la Federación.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--log-json", action="store_true", help="structured logs on stderr")
    p.add_argument("--db", type=Path, help="override the SQLite path")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("research", help="probe for an official API / bulk download first")
    r.add_argument("--fecha", default=None, help="probe date (YYYY-MM-DD); default: last weekday")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_research)

    def add_range(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--since", help="YYYY-MM-DD")
        sp.add_argument("--until", help="YYYY-MM-DD (default: --since)")
        sp.add_argument("--last-days", type=int, help="shorthand for a window ending today")
        sp.add_argument("--editions", help=f"comma-separated (default: {','.join(EDITIONS)})")
        sp.add_argument(
            "--recheck-days", type=int, default=7,
            help="re-visit editions newer than N days even if already recorded (default: 7)",
        )

    d = sub.add_parser("discover", help="crawl index pages and record notas")
    add_range(d)
    d.set_defaults(func=cmd_discover)

    def add_enrich(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--limit", type=int, default=200, help="max notas to fetch (default: 200)")
        sp.add_argument("--retry-errors", action="store_true")

    e = sub.add_parser("enrich", help="fetch the full text of pending notas")
    add_enrich(e)
    e.set_defaults(func=cmd_enrich)

    a = sub.add_parser("run", help="discover + enrich")
    add_range(a)
    add_enrich(a)
    a.set_defaults(func=cmd_run)

    s = sub.add_parser("stats", help="what is in the store")
    s.add_argument("--json", action="store_true")
    s.add_argument("--limit", type=int, default=8)
    s.set_defaults(func=cmd_stats)

    x = sub.add_parser("export", help="dump the corpus")
    x.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    x.add_argument("--out", default="-", help="output path, or - for stdout")
    x.add_argument("--include-body", action="store_true")
    x.set_defaults(func=cmd_export)

    b = sub.add_parser("robots", help="explain the robots.txt verdict for a URL")
    b.add_argument("url")
    b.set_defaults(func=cmd_robots)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, args.log_json)

    settings = Settings.from_env()
    if args.db:
        settings = dataclasses.replace(settings, db_path=args.db)

    # Sensible default for the crawl commands: catch up the last week.
    if (
        args.command in {"discover", "run"}
        and getattr(args, "since", None) is None
        and getattr(args, "last_days", None) is None
    ):
        args.last_days = 7

    try:
        return int(args.func(args, settings))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
