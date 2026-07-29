"""Table schemas and the local/Databricks write abstraction.

Schemas are declared once, here, as StructTypes rather than inferred per run:
inference is how a column quietly changes type between two runs and nobody
notices until the MERGE fails. The same StructType creates the table locally
(DataFrame API -- Spark 4 no longer accepts DDL with a schema for external
Delta tables) and renders the CREATE TABLE statement on Unity Catalog, so the
file layout and the catalog can never drift apart.
"""

from __future__ import annotations

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import types as T

from .config import LakeSettings

# The payload of one export line: Store.EXPORT_COLUMNS plus body_text.
# fecha stays STRING in bronze -- bronze keeps what the source said; typing
# it to DATE is silver's job.
BRONZE_SCHEMA = T.StructType(
    [
        T.StructField("codigo", T.LongType(), False),
        T.StructField("fecha", T.StringType(), True),
        T.StructField("edicion", T.StringType(), True),
        T.StructField("seccion", T.StringType(), True),
        T.StructField("poder", T.StringType(), True),
        T.StructField("organismo", T.StringType(), True),
        T.StructField("titulo", T.StringType(), True),
        T.StructField("url_detalle", T.StringType(), True),
        T.StructField("content_hash", T.StringType(), True),
        T.StructField("body_hash", T.StringType(), True),
        T.StructField("body_status", T.StringType(), True),
        T.StructField("revision", T.LongType(), True),
        T.StructField("first_seen_at", T.StringType(), True),
        T.StructField("last_seen_at", T.StringType(), True),
        T.StructField("last_changed_at", T.StringType(), True),
        T.StructField("body_text", T.StringType(), True),
    ]
)

# Bronze as stored: the payload plus lineage. The snapshot date comes from the
# export FILENAME, not the clock -- see bronze.py.
BRONZE_TABLE_SCHEMA = T.StructType(
    [
        *BRONZE_SCHEMA.fields,
        T.StructField("_snapshot_date", T.DateType(), False),
        T.StructField("_ingested_at", T.TimestampType(), False),
    ]
)

# SCD-2: one row per (codigo, revision). valid_to NULL + is_current mark the
# live version; the rest are history. `revision` comes from the scraper, which
# only bumps it when a content hash moved -- so it is a source-assigned,
# monotonic version key and the merge needs no watermark table.
SILVER_SCHEMA = T.StructType(
    [
        T.StructField("codigo", T.LongType(), False),
        T.StructField("revision", T.LongType(), False),
        T.StructField("fecha", T.DateType(), True),
        T.StructField("edicion", T.StringType(), True),
        T.StructField("seccion", T.StringType(), True),
        T.StructField("poder", T.StringType(), True),
        T.StructField("organismo", T.StringType(), True),
        T.StructField("titulo", T.StringType(), True),
        T.StructField("url_detalle", T.StringType(), True),
        T.StructField("content_hash", T.StringType(), True),
        T.StructField("body_hash", T.StringType(), True),
        T.StructField("body_status", T.StringType(), True),
        T.StructField("body_text", T.StringType(), True),
        T.StructField("valid_from", T.DateType(), True),
        T.StructField("valid_to", T.DateType(), True),
        T.StructField("is_current", T.BooleanType(), False),
    ]
)

SILVER_COLUMNS = tuple(f.name for f in SILVER_SCHEMA.fields)


def _sql_type(dt: T.DataType) -> str:
    if isinstance(dt, T.LongType):
        return "BIGINT"
    if isinstance(dt, T.BooleanType):
        return "BOOLEAN"
    if isinstance(dt, T.DateType):
        return "DATE"
    if isinstance(dt, T.TimestampType):
        return "TIMESTAMP"
    return "STRING"


def _ddl(schema: T.StructType) -> str:
    return ", ".join(f"{f.name} {_sql_type(f.dataType)}" for f in schema.fields)


def ensure_table(
    spark: SparkSession, settings: LakeSettings, layer: str, table: str, schema: T.StructType
) -> None:
    """Create the Delta table if absent, and make it SQL-addressable.

    Local mode is two steps: an empty DataFrame write materialises the Delta
    log at the path, then a catalog entry (no schema clause -- Delta keeps the
    schema in its own log) makes the path usable in MERGE statements. The
    catalog entry is metadata only; dropping it never touches the files.
    """
    if settings.catalog:
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS {settings.catalog}.dof_{layer}")
        spark.sql(
            f"CREATE TABLE IF NOT EXISTS {settings.table_ref(layer, table)} "
            f"({_ddl(schema)}) USING DELTA"
        )
    else:
        from delta import DeltaTable

        # Absolute everywhere: a relative LOCATION is resolved against the
        # metastore warehouse, not the cwd, and the table silently registers
        # pointing at nothing.
        path = str((settings.lake_path / layer / table).resolve())
        if not DeltaTable.isDeltaTable(spark, path):
            spark.createDataFrame([], schema).write.format("delta").save(path)
        spark.sql("CREATE SCHEMA IF NOT EXISTS dof_local")
        spark.sql(
            f"CREATE TABLE IF NOT EXISTS {settings.table_ref(layer, table)} "
            f"USING DELTA LOCATION '{path}'"
        )


def read_delta(spark: SparkSession, settings: LakeSettings, layer: str, table: str) -> DataFrame:
    if settings.catalog:
        return spark.read.table(settings.table_ref(layer, table))
    return spark.read.format("delta").load(str(settings.lake_path / layer / table))


def write_gold(df: DataFrame, settings: LakeSettings, table: str) -> None:
    """Gold marts are full-refreshed: derived data, seconds to rebuild.

    Incremental gold is the right answer when a rebuild is expensive. At this
    scale a full refresh is the *correctness-preserving* choice -- no merge
    keys to get wrong, no stale rows when the silver logic changes.
    """
    if settings.catalog:
        # writeTo(...).createOrReplace() creates the TABLE but not the schema
        # above it -- on a fresh catalog that's a SCHEMA_NOT_FOUND.
        df.sparkSession.sql(f"CREATE SCHEMA IF NOT EXISTS {settings.catalog}.dof_gold")
        df.writeTo(settings.table_ref("gold", table)).using("delta").createOrReplace()
    else:
        df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(
            str(settings.lake_path / "gold" / table)
        )
