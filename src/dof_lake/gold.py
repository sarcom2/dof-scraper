"""Gold: analytics over *changes*, not word counts.

Every portfolio pipeline ends in a GROUP BY. This one has something rarer to
aggregate: the DOF amends published notices (fe de erratas), and the silver
SCD-2 history makes those amendments a first-class dataset. The three marts:

  * amendments_by_organismo -- who corrects themselves, how often, how fast;
  * monthly_activity      -- publication volume by branch of government;
  * correction_feed       -- the most recent corrections, as a product table.
"""

from __future__ import annotations

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from .config import LakeSettings
from .tables import read_delta, write_gold


def build_gold(spark: SparkSession, settings: LakeSettings) -> dict[str, int]:
    silver = read_delta(spark, settings, "silver", "notas")
    current = silver.filter("is_current")

    # Per codigo: the highest revision seen, and when revision 2 took effect
    # (i.e. when the first correction happened).
    versions = silver.groupBy("codigo").agg(F.max("revision").alias("max_rev"))
    first_correction = (
        silver.filter("revision = 2")
        .select(F.col("codigo"), F.col("valid_from").alias("corrected_on"))
    )

    amendments = (
        current.join(versions, "codigo")
        .join(first_correction, "codigo", "left")
        .groupBy("organismo")
        .agg(
            F.count("*").alias("notas"),
            F.sum(F.when(F.col("max_rev") > 1, 1).otherwise(0)).alias("amended"),
            F.round(
                F.avg(F.when(F.col("max_rev") > 1, 1).otherwise(0)), 4
            ).alias("amendment_rate"),
            F.round(
                F.avg(F.datediff("corrected_on", "fecha")), 1
            ).alias("avg_days_to_correction"),
        )
        .orderBy(F.desc("amended"))
    )
    write_gold(amendments, settings, "amendments_by_organismo")

    activity = (
        current.join(versions, "codigo")
        .groupBy(F.date_trunc("month", "fecha").alias("month"), "poder")
        .agg(
            F.count("*").alias("notas"),
            F.countDistinct("organismo").alias("organismos"),
            F.sum(F.when(F.col("max_rev") > 1, 1).otherwise(0)).alias("amended"),
        )
        .orderBy("month", "poder")
    )
    write_gold(activity, settings, "monthly_activity")

    feed = (
        silver.filter("revision > 1").alias("h")
        .join(current.select("codigo", "organismo", "titulo", "fecha").alias("c"), "codigo")
        .select(
            "codigo",
            F.col("c.organismo").alias("organismo"),
            F.col("c.titulo").alias("titulo"),
            F.col("h.revision").alias("revision"),
            F.col("c.fecha").alias("fecha"),
            F.col("h.valid_from").alias("corrected_on"),
            F.datediff(F.col("h.valid_from"), F.col("c.fecha")).alias("days_to_correction"),
        )
        .orderBy(F.desc("corrected_on"))
        .limit(50)
    )
    write_gold(feed, settings, "correction_feed")

    return {
        "amendments_by_organismo": amendments.count(),
        "monthly_activity": activity.count(),
        "correction_feed": feed.count(),
    }
