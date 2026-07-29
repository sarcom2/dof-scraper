"""Analytical lakehouse layer: Spark + Delta Lake, deployable to Databricks.

This package does NOT replace the SQLite store. `dof_ingest` is the
operational side (polite, sequential, robots-aware crawling); this is the
analytical side. The hand-off is a JSONL snapshot of the `nota` table,
produced by `dof-lake export`, which becomes the bronze layer.

The corpus does not need Spark for volume -- it fits in SQLite by design. The
reason this layer exists is semantics: the DOF amends published notices (fe de
erratas), and the scraper already tracks that with `revision` + content
hashes. That change feed maps directly onto the three things a lakehouse does
better than a row store:

  * MERGE-based idempotent loads (the same contract as Store.upsert_nota,
    ported to another engine),
  * SCD Type 2 history, so "what did this decree say before the correction?"
    is a query, not an excavation,
  * Delta time travel, so "what did the corpus look like on date X?" is
    answerable -- the same audit-trail ethos as the run ledger.

Everything runs locally with `pyspark` + `delta-spark`, and the identical code
deploys to Databricks Free Edition as an Asset Bundle (see databricks.yml).
"""
