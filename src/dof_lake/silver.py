"""Silver: the SCD-2 dimension. This is the heart of the lakehouse layer.

One row per (codigo, revision): the full text as it read during that
revision's lifetime, with [valid_from, valid_to) bracketing it. The version
key is the scraper's own `revision`, which Store.upsert_nota only bumps when
a content hash moved -- so the change feed has exactly the semantics SCD-2
needs, and the merge stays deterministic and idempotent without a watermark
table: re-observed revisions anti-join away against what silver already has.

The merge is two explicit steps rather than the "UNION ALL with NULL key"
single-statement trick. That trick saves a scan at the cost of being the one
Spark idiom nobody can explain in an interview; two boring steps -- close the
current row, then append the new versions -- state the intent directly.
"""

from __future__ import annotations

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

from .config import LakeSettings
from .tables import SILVER_COLUMNS, SILVER_SCHEMA, ensure_table, read_delta


def _events(spark: SparkSession, settings: LakeSettings) -> DataFrame:
    """One row per distinct (codigo, revision) ever observed in bronze.

    effective_date = the earliest snapshot that carried that revision. A
    revision observed for the first time on snapshot D became true *by* D --
    that is the honest timestamp, and it is what makes a backfill of old
    export files produce correct history instead of stamping everything
    "today".
    """
    bronze = read_delta(spark, settings, "bronze", "notas_snapshot")
    first_seen = Window.partitionBy("codigo", "revision").orderBy("_snapshot_date")
    return (
        bronze.withColumn("_rn", F.row_number().over(first_seen))
        .filter("_rn = 1")
        .select(
            "codigo", "revision", F.col("_snapshot_date").alias("effective_date"),
            F.to_date("fecha").alias("fecha"), "edicion", "seccion", "poder",
            "organismo", "titulo", "url_detalle", "content_hash", "body_hash",
            "body_status", "body_text",
        )
    )


def merge_silver(spark: SparkSession, settings: LakeSettings) -> int:
    """Advance silver.notas with any revisions not yet recorded. Returns new rows."""
    ensure_table(spark, settings, "silver", "notas", SILVER_SCHEMA)

    silver = read_delta(spark, settings, "silver", "notas")
    seen = silver.select("codigo", "revision")
    new = _events(spark, settings).join(seen, ["codigo", "revision"], "left_anti")
    n = new.count()
    if n == 0:
        # The idempotency claim, measured rather than asserted: a re-run over
        # the same bronze produces zero candidate revisions, so neither the
        # closing merge nor the append below executes a single write.
        return 0

    # Chain the batch internally first: within one batch, revision N's
    # valid_to is revision N+1's effective_date. The batch's latest revision
    # gets valid_to NULL provisionally; step 2 closes the previously-current
    # row, and step 3 re-derives is_current from the data itself so arrival
    # order cannot leave two live versions.
    by_revision = Window.partitionBy("codigo").orderBy("revision")
    chained = (
        new.withColumn("_next_date", F.lead("effective_date").over(by_revision))
        .withColumn("_max_rev", F.max("revision").over(Window.partitionBy("codigo")))
    )
    chained.createOrReplaceTempView("dof_lake_silver_events")

    ref = settings.table_ref("silver", "notas")

    # Step 1: close whatever row is currently live for each touched codigo --
    # but only if the batch actually carries a NEWER revision for it. Without
    # the max_rev guard, a late-arriving old snapshot would close a newer
    # live row with a valid_to in its own past.
    spark.sql(
        f"""
        MERGE INTO {ref} t
        USING (SELECT codigo, MIN(effective_date) AS close_date, MAX(revision) AS max_rev
               FROM dof_lake_silver_events GROUP BY codigo) s
          ON t.codigo = s.codigo AND t.is_current AND s.max_rev > t.revision
        WHEN MATCHED THEN UPDATE SET t.is_current = false, t.valid_to = s.close_date
        """
    )

    # Step 2: append the new versions.
    spark.sql(
        f"""
        INSERT INTO {ref} ({', '.join(SILVER_COLUMNS)})
        SELECT codigo, revision, fecha, edicion, seccion, poder, organismo, titulo,
               url_detalle, content_hash, body_hash, body_status, body_text,
               effective_date AS valid_from,
               _next_date AS valid_to,
               (_next_date IS NULL AND revision = _max_rev) AS is_current
        FROM dof_lake_silver_events
        """
    )

    # Step 3: repair pass. The two invariants of an SCD-2 table are re-derived
    # from the data itself rather than trusted to the merge above:
    #   a) exactly one live row per codigo -- the highest revision;
    #   b) no closed row left with valid_to NULL (which out-of-order snapshot
    #      arrival could produce in step 2).
    # Both are O(table) at this scale and turn "the merge is correct" into
    # "the table is provably consistent after every run".
    spark.sql(
        f"""
        MERGE INTO {ref} t
        USING (SELECT codigo, MAX(revision) AS max_rev FROM {ref} GROUP BY codigo) s
          ON t.codigo = s.codigo
        WHEN MATCHED THEN UPDATE SET t.is_current = (t.revision = s.max_rev)
        """
    )
    spark.sql(
        f"""
        MERGE INTO {ref} t
        USING (
          SELECT a.codigo, a.revision, MIN(b.valid_from) AS close_date
          FROM {ref} a JOIN {ref} b ON a.codigo = b.codigo AND b.revision > a.revision
          WHERE a.is_current = false AND a.valid_to IS NULL
          GROUP BY a.codigo, a.revision
        ) s
          ON t.codigo = s.codigo AND t.revision = s.revision
        WHEN MATCHED THEN UPDATE SET t.valid_to = s.close_date
        """
    )
    return n
