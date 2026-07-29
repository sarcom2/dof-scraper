"""SparkSession construction.

Two environments, one code path:

  * Local / CI: build a session with the Delta extensions and a tiny number
    of shuffle partitions -- the corpus is megabytes, and the default of 200
    partitions would spend more time scheduling empty tasks than reading.
  * Databricks: the runtime already owns the session (serverless on the Free
    Edition), so we just adopt it. Detecting the runtime via environment
    variable is the documented approach and avoids importing databricks-sdk
    for something the runtime already tells us.
"""

from __future__ import annotations

import os
import subprocess

from pyspark.sql import SparkSession


def on_databricks() -> bool:
    return "DATABRICKS_RUNTIME_VERSION" in os.environ


def _have_java() -> bool:
    # macOS ships a /usr/bin/java STUB that prints "install Java" and exits
    # non-zero, so `which java` is not evidence of a JDK. Run it.
    if os.environ.get("JAVA_HOME"):
        return True
    try:
        return subprocess.run(
            ["java", "-version"], capture_output=True, timeout=10
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def get_spark(app_name: str = "dof-lake") -> SparkSession:
    if on_databricks():
        return SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()

    if not _have_java():
        raise SystemExit(
            "PySpark needs a JDK and none was found.\n"
            "  brew install openjdk@17   # then use `make lake`, which sets JAVA_HOME"
        )

    import delta

    builder = (
        SparkSession.builder.appName(app_name)
        .master("local[2]")
        # Megabyte-scale data: 200 shuffle partitions is pure overhead.
        .config("spark.sql.shuffle.partitions", "4")
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config(
            "spark.sql.catalog.spark_catalog",
            "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        )
    )
    # configure_spark_with_delta_pip only pins the matching io.delta Maven
    # artifact onto the classpath (since Delta 4.0 it no longer registers the
    # SQL extension + catalog -- hence the two configs above). Skipping the
    # JARs is the classic "JavaPackage object is not callable" failure;
    # skipping the configs is "DELTA_CONFIGURE_SPARK_SESSION_WITH_EXTENSION".
    return delta.configure_spark_with_delta_pip(builder).getOrCreate()
