"""Command line interface for the lakehouse layer.

argparse, same convention as dof-ingest: subcommands with scalar options.
`export` needs no JVM; the rest build a local Spark session unless running
inside Databricks, where the runtime's session is adopted instead.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import LakeSettings
from .export import export_snapshot


def _settings(args: argparse.Namespace) -> LakeSettings:
    s = LakeSettings.from_env()
    return LakeSettings(
        db_path=Path(args.db) if args.db else s.db_path,
        exports_dir=Path(args.exports_dir) if args.exports_dir else s.exports_dir,
        lake_path=Path(args.lake_path) if args.lake_path else s.lake_path,
        catalog=args.catalog if args.catalog is not None else s.catalog,
    )


def cmd_export(args: argparse.Namespace, settings: LakeSettings) -> int:
    out, n = export_snapshot(settings)
    print(f"  {n} notas -> {out}")
    return 0


def cmd_bronze(args: argparse.Namespace, settings: LakeSettings) -> int:
    from . import bronze
    from .session import get_spark

    spark = get_spark()
    n = bronze.load_bronze(spark, settings)
    print(f"  bronze: staged {n} rows from {settings.exports_dir}")
    return 0


def cmd_silver(args: argparse.Namespace, settings: LakeSettings) -> int:
    from . import silver
    from .session import get_spark

    spark = get_spark()
    n = silver.merge_silver(spark, settings)
    print(f"  silver: {n} new revision(s) merged")
    return 0


def cmd_gold(args: argparse.Namespace, settings: LakeSettings) -> int:
    from . import gold
    from .session import get_spark

    spark = get_spark()
    counts = gold.build_gold(spark, settings)
    for table, n in counts.items():
        print(f"  gold.{table}: {n} rows")
    return 0


def cmd_all(args: argparse.Namespace, settings: LakeSettings) -> int:
    rc = cmd_export(args, settings)
    for cmd in (cmd_bronze, cmd_silver, cmd_gold):
        rc = rc or cmd(args, settings)
    return rc


def cmd_stats(args: argparse.Namespace, settings: LakeSettings) -> int:
    from .session import get_spark
    from .tables import read_delta

    spark = get_spark()
    bronze = read_delta(spark, settings, "bronze", "notas_snapshot")
    silver = read_delta(spark, settings, "silver", "notas")
    print(f"\n  {'lake':<8} {settings.lake_path if not settings.catalog else settings.catalog}\n")
    print(f"  bronze.notas_snapshot   {bronze.count():>6} rows "
          f"({bronze.select('_snapshot_date').distinct().count()} snapshots)")
    print(f"  silver.notas            {silver.count():>6} versions "
          f"({silver.filter('is_current').count()} current)")
    for table in ("amendments_by_organismo", "monthly_activity", "correction_feed"):
        try:
            n: object = read_delta(spark, settings, "gold", table).count()
        except Exception:
            n = "-"
        print(f"  gold.{table:<21} {n!s:>6} rows")
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dof-lake",
        description="Spark/Delta lakehouse over the DOF corpus (bronze -> silver SCD-2 -> gold).",
    )
    p.add_argument("--db", help="SQLite path for `export` (default: data/dof.sqlite3)")
    p.add_argument("--exports-dir", help="where snapshots live (default: data/exports)")
    p.add_argument("--lake-path", help="local Delta root (default: data/lake)")
    p.add_argument("--catalog", help="Unity Catalog name; set on Databricks, unset locally")
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("export", help="SQLite -> JSONL snapshot (no Spark needed)")
    e.set_defaults(func=cmd_export)

    for name, help_ in (
        ("bronze", "merge export files into bronze.notas_snapshot"),
        ("silver", "SCD-2 merge into silver.notas"),
        ("gold", "rebuild the gold marts"),
        ("all", "export + bronze + silver + gold"),
        ("stats", "row counts per layer"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=globals()[f"cmd_{name}"])
    return p


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args, _settings(args)))


if __name__ == "__main__":
    raise SystemExit(main())
