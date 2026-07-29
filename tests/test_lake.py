"""Lakehouse tests: the idempotency and SCD-2 claims, measured.

These run against a local Spark session with Delta on tmp_path -- no
Databricks account, no network. They are skipped when the `lake` extra is not
installed, so the core suite still runs in a bare `uv sync` environment; CI
has a dedicated job that installs the extra and executes them.

The scenario mirrors the domain's real failure mode: a nota is published,
then amended (revision 2), then re-observed unchanged. Every step asserts
row-level facts, not just counts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pyspark = pytest.importorskip("pyspark", reason="lake extra not installed")

from pyspark.sql import SparkSession  # noqa: E402

from dof_lake import bronze, gold, silver  # noqa: E402
from dof_lake.config import LakeSettings  # noqa: E402
from dof_lake.export import export_snapshot  # noqa: E402


def _nota(codigo: int, titulo: str, revision: int, organismo: str = "SHCP") -> dict[str, object]:
    return {
        "codigo": codigo,
        "fecha": "2026-07-31",
        "edicion": "MAT",
        "seccion": "PRIMERA SECCION",
        "poder": "PODER EJECUTIVO",
        "organismo": organismo,
        "titulo": titulo,
        "url_detalle": f"https://sidof.segob.gob.mx/notas/{codigo}",
        "content_hash": f"hash-{codigo}-r{revision}",
        "body_hash": f"body-{codigo}-r{revision}",
        "body_status": "ok",
        "revision": revision,
        "first_seen_at": "2026-07-31T08:00:00+00:00",
        "last_seen_at": "2026-07-31T08:00:00+00:00",
        "last_changed_at": "2026-07-31T08:00:00+00:00",
        "body_text": f"Texto de la nota {codigo} revision {revision}",
    }


def _write_export(exports_dir: Path, day: str, rows: list[dict[str, object]]) -> None:
    exports_dir.mkdir(parents=True, exist_ok=True)
    with (exports_dir / f"notas-{day}.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


@pytest.fixture(scope="module")
def spark() -> SparkSession:
    """A local Spark session -- skipped, not failed, when no JDK is around.

    PySpark needs Java; on this machine it is `brew install openjdk@17` plus
    JAVA_HOME (the Makefile's lake targets set it). A missing JDK is an
    environment fact, not a broken build.
    """
    import os
    import subprocess

    # macOS ships a /usr/bin/java STUB that prints "install Java" and exits
    # non-zero, so `which java` is not evidence of a JDK. Run it.
    have_java = bool(os.environ.get("JAVA_HOME"))
    if not have_java:
        try:
            have_java = subprocess.run(
                ["java", "-version"], capture_output=True, timeout=10
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            have_java = False
    if not have_java:
        pytest.skip("no JDK available (brew install openjdk@17, or set JAVA_HOME)")

    from dof_lake.session import get_spark

    session = get_spark("dof-lake-tests")
    yield session
    session.stop()


@pytest.fixture
def lake(tmp_path: Path) -> LakeSettings:
    return LakeSettings(
        db_path=tmp_path / "dof.sqlite3",
        exports_dir=tmp_path / "exports",
        lake_path=tmp_path / "lake",
    )


def _versions(spark: SparkSession, settings: LakeSettings, codigo: int) -> list[tuple[int, bool]]:
    rows = (
        spark.read.format("delta")
        .load(str(settings.lake_path / "silver" / "notas"))
        .filter(f"codigo = {codigo}")
        .select("revision", "is_current")
        .orderBy("revision")
        .collect()
    )
    return [(int(r["revision"]), bool(r["is_current"])) for r in rows]


def test_export_round_trip(lake: LakeSettings) -> None:
    """export needs no Spark: SQLite -> JSONL with the full change-feed columns."""
    from dof_ingest.models import Nota
    from dof_ingest.store import Store

    with Store(lake.db_path) as store:
        store.upsert_nota(Nota(codigo=101, fecha="2026-07-31", edicion="MAT",
                               titulo="ACUERDO de prueba", organismo="SHCP"))
    out, n = export_snapshot(lake, day="2026-08-01")
    assert n == 1 and out.name == "notas-2026-08-01.jsonl"
    row = json.loads(out.read_text("utf-8").strip())
    for col in ("codigo", "content_hash", "body_hash", "revision"):
        assert col in row, f"bronze needs {col} for the SCD-2 feed"
    assert row["revision"] == 1


def test_bronze_is_idempotent(spark: SparkSession, lake: LakeSettings) -> None:
    _write_export(lake.exports_dir, "2026-08-01", [_nota(101, "ACUERDO A", 1)])
    assert bronze.load_bronze(spark, lake) == 1
    assert bronze.load_bronze(spark, lake) == 1  # re-staged, but...
    table = spark.read.format("delta").load(str(lake.lake_path / "bronze" / "notas_snapshot"))
    assert table.count() == 1  # ...not duplicated
    assert table.select("_snapshot_date").first()["_snapshot_date"].isoformat() == "2026-08-01"


def test_silver_scd2_full_lifecycle(spark: SparkSession, lake: LakeSettings) -> None:
    """Publish, amend, re-observe: the three states of a DOF nota."""
    day1 = [_nota(101, "ACUERDO A", 1), _nota(102, "ACUERDO B", 1, "COFEPRIS")]
    _write_export(lake.exports_dir, "2026-08-01", day1)
    bronze.load_bronze(spark, lake)
    assert silver.merge_silver(spark, lake) == 2
    assert _versions(spark, lake, 101) == [(1, True)]

    # Re-run over the same bronze: zero candidate revisions, zero writes.
    assert silver.merge_silver(spark, lake) == 0
    assert _versions(spark, lake, 101) == [(1, True)]

    # The amendment arrives in the next snapshot.
    _write_export(
        lake.exports_dir, "2026-08-05",
        [_nota(101, "ACUERDO A (corregido)", 2), _nota(102, "ACUERDO B", 1, "COFEPRIS")],
    )
    bronze.load_bronze(spark, lake)
    assert silver.merge_silver(spark, lake) == 1

    rows = (
        spark.read.format("delta").load(str(lake.lake_path / "silver" / "notas"))
        .filter("codigo = 101").orderBy("revision").collect()
    )
    assert [(int(r["revision"]), bool(r["is_current"])) for r in rows] == [(1, False), (2, True)]
    assert rows[0]["valid_to"].isoformat() == "2026-08-05"  # closed when the fix appeared
    assert rows[1]["valid_from"].isoformat() == "2026-08-05"
    assert rows[1]["valid_to"] is None
    # The untouched nota stayed exactly one live version 1.
    assert _versions(spark, lake, 102) == [(1, True)]

    counts = gold.build_gold(spark, lake)
    assert counts["amendments_by_organismo"] == 2
    amended = (
        spark.read.format("delta").load(str(lake.lake_path / "gold" / "amendments_by_organismo"))
        .filter("organismo = 'SHCP'").first()
    )
    assert amended["notas"] == 1 and amended["amended"] == 1
    assert amended["amendment_rate"] == 1.0
    assert amended["avg_days_to_correction"] == 5.0


def test_silver_handles_multi_revision_backfill(spark: SparkSession, lake: LakeSettings) -> None:
    """Three snapshots loaded at once: the version chain is built in one pass."""
    _write_export(lake.exports_dir, "2026-08-01", [_nota(101, "ACUERDO A", 1)])
    _write_export(lake.exports_dir, "2026-08-03", [_nota(101, "ACUERDO A v2", 2)])
    _write_export(lake.exports_dir, "2026-08-05", [_nota(101, "ACUERDO A v3", 3)])
    bronze.load_bronze(spark, lake)
    assert silver.merge_silver(spark, lake) == 3

    rows = (
        spark.read.format("delta").load(str(lake.lake_path / "silver" / "notas"))
        .orderBy("revision").collect()
    )
    assert [(int(r["revision"]), bool(r["is_current"])) for r in rows] == [
        (1, False), (2, False), (3, True),
    ]
    assert rows[0]["valid_to"].isoformat() == "2026-08-03"
    assert rows[1]["valid_from"].isoformat() == "2026-08-03"
    assert rows[1]["valid_to"].isoformat() == "2026-08-05"
    assert rows[2]["valid_to"] is None
    # And the invariant holds: exactly one live row.
    live = [r for r in rows if r["is_current"]]
    assert len(live) == 1 and live[0]["revision"] == 3
