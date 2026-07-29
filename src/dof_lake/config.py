"""Lakehouse configuration: local Delta paths OR a Unity Catalog location.

One frozen dataclass, same convention as dof_ingest.config. The dual local /
Databricks mode is deliberate: CI and laptops run against plain Delta paths
under `data/lake/`; the Databricks job passes a catalog and writes managed
Unity Catalog tables instead. The SQL is identical either way -- only the
table reference changes.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LakeSettings:
    """Where the snapshots live and where the Delta tables go.

    If `catalog` is set we are on Databricks: tables are referenced as
    `{catalog}.dof_{layer}.{table}` and the medallion layers are schemas.
    Otherwise tables are Delta paths under `lake_path/{layer}/{table}`.
    """

    db_path: Path = Path("data/dof.sqlite3")
    exports_dir: Path = Path("data/exports")
    lake_path: Path = Path("data/lake")
    catalog: str | None = None

    def table_ref(self, layer: str, table: str) -> str:
        """A reference usable directly in Spark SQL, in both worlds.

        Spark 4 removed the `delta.`/path`` direct-query syntax, so local
        mode registers each path in the session catalog under `dof_local.*`
        (see tables.ensure_table) and references that. The path hash in the
        name keeps two lake roots in one session (read: the test suite) from
        aliasing each other; Databricks mode is a plain UC three-part name.
        """
        if self.catalog:
            return f"{self.catalog}.dof_{layer}.{table}"
        path = (self.lake_path / layer / table).resolve()
        digest = hashlib.md5(str(path).encode()).hexdigest()[:8]
        return f"dof_local.{layer}_{table}_{digest}"

    @classmethod
    def from_env(cls) -> LakeSettings:
        return cls(
            db_path=Path(os.environ.get("DOF_DB", "data/dof.sqlite3")),
            exports_dir=Path(os.environ.get("DOF_EXPORTS", "data/exports")),
            lake_path=Path(os.environ.get("DOF_LAKE", "data/lake")),
            catalog=os.environ.get("DOF_CATALOG") or None,
        )
