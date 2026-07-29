"""Bronze: every snapshot ever exported, kept whole and deduplicated.

Bronze is append-mostly raw, but "raw" is not a license to duplicate: the
merge key is (codigo, _snapshot_date), so re-running the load over the same
export files is a no-op -- the same idempotency contract as the scraper, one
layer down. The snapshot date is recovered from the *filename*, not from
processing time, so a backfill of old export files lands under the date the
data describes, not the date someone remembered to run the job.
"""

from __future__ import annotations

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from .config import LakeSettings
from .tables import BRONZE_SCHEMA, BRONZE_TABLE_SCHEMA, ensure_table


def load_bronze(spark: SparkSession, settings: LakeSettings) -> int:
    """Merge all export files into bronze.notas_snapshot. Returns staged rows."""
    exports = sorted(settings.exports_dir.glob("notas-*.jsonl"))
    if not exports:
        raise SystemExit(
            f"no export files under {settings.exports_dir} -- run `dof-lake export` first"
        )

    staged = (
        spark.read.schema(BRONZE_SCHEMA)
        .json(str(settings.exports_dir / "notas-*.jsonl"))
        # _metadata.file_path, NOT input_file_name(): Unity Catalog rejects
        # the latter outright (UC_COMMAND_NOT_SUPPORTED), while the metadata
        # column works identically locally and on Databricks.
        .withColumn(
            "_snapshot_date",
            F.to_date(
                F.regexp_extract(F.col("_metadata.file_path"), r"notas-(\d{4}-\d{2}-\d{2})", 1)
            ),
        )
        .withColumn("_ingested_at", F.current_timestamp())
    )
    n = staged.count()
    if n == 0:
        raise SystemExit(f"export files under {settings.exports_dir} contain no rows")

    ensure_table(spark, settings, "bronze", "notas_snapshot", BRONZE_TABLE_SCHEMA)
    staged.createOrReplaceTempView("dof_lake_bronze_staged")
    spark.sql(
        f"""
        MERGE INTO {settings.table_ref('bronze', 'notas_snapshot')} t
        USING dof_lake_bronze_staged s
          ON t.codigo = s.codigo AND t._snapshot_date = s._snapshot_date
        WHEN NOT MATCHED THEN INSERT *
        """
    )
    return n
