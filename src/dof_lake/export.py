"""The hand-off from the operational store to the lakehouse: JSONL snapshots.

No Spark in this module -- exporting a few hundred thousand rows from SQLite
is a streaming copy, and giving Spark a JDBC path into the operational
database would couple the analytical layer to a file the crawler holds a WAL
lock on. A snapshot file per run is also what lands naturally in a Unity
Catalog volume on Databricks: the job never sees SQLite at all.

Each snapshot carries the full `nota` projection INCLUDING the revision and
both content hashes, because that is the change feed the silver layer's
SCD-2 merge consumes. Snapshots are named by date so a re-export on the same
day overwrites rather than accumulates.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dof_ingest.store import Store

from .config import LakeSettings


def snapshot_date() -> str:
    return datetime.now(UTC).date().isoformat()


def export_snapshot(settings: LakeSettings, day: str | None = None) -> tuple[Path, int]:
    """Write `notas-<day>.jsonl` into the exports dir. Returns (path, rows)."""
    day = day or snapshot_date()
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    out = settings.exports_dir / f"notas-{day}.jsonl"
    with Store(settings.db_path) as store, out.open("w", encoding="utf-8") as fh:
        n = store.export(fh, "jsonl", include_body=True)
    return out, n
