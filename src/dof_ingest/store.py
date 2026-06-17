"""SQLite persistence: idempotent upserts, change history, run ledger.

SQLite because the whole corpus is a few hundred thousand rows of text and a
single writer. Postgres would add an operational dependency to buy concurrency
we do not have and durability guarantees `WAL` already gives us. If this ever
grows a second writer, the schema ports over unchanged.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from .models import Nota, content_hash

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS edition (
    fecha             TEXT    NOT NULL,
    edicion           TEXT    NOT NULL,
    -- ok | no_edition : the second is a real, recorded state, not an absence
    status            TEXT    NOT NULL,
    notas_found       INTEGER NOT NULL DEFAULT 0,
    doc_only_rejected INTEGER NOT NULL DEFAULT 0,
    content_hash      TEXT,
    first_seen_at     TEXT    NOT NULL,
    last_checked_at   TEXT    NOT NULL,
    last_changed_at   TEXT    NOT NULL,
    PRIMARY KEY (fecha, edicion)
);

CREATE TABLE IF NOT EXISTS nota (
    -- The DOF's own identifier. Using it as the PK is what makes re-running
    -- the pipeline a no-op rather than a duplication: there is no surrogate
    -- key that could differ between two runs over the same source row.
    codigo          INTEGER PRIMARY KEY,
    fecha           TEXT    NOT NULL,
    edicion         TEXT    NOT NULL,
    seccion         TEXT    NOT NULL DEFAULT '',
    poder           TEXT    NOT NULL DEFAULT '',
    organismo       TEXT    NOT NULL DEFAULT '',
    titulo          TEXT    NOT NULL,
    url_detalle     TEXT    NOT NULL DEFAULT '',
    url_origen      TEXT    NOT NULL DEFAULT '',

    -- Hash of the business fields only. Drives change detection.
    content_hash    TEXT    NOT NULL,

    body_text       TEXT,
    body_hash       TEXT,
    -- pending | ok | robots_denied | not_found | error
    body_status     TEXT    NOT NULL DEFAULT 'pending',
    body_fetched_at TEXT,

    revision        INTEGER NOT NULL DEFAULT 1,
    first_seen_at   TEXT    NOT NULL,
    -- last_seen_at moves on EVERY run; last_changed_at only when a hash moves.
    -- Keeping them apart is the whole point: "we looked" and "it changed" are
    -- different facts and conflating them destroys the audit trail.
    last_seen_at    TEXT    NOT NULL,
    last_changed_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS nota_fecha_idx       ON nota (fecha, edicion);
CREATE INDEX IF NOT EXISTS nota_organismo_idx   ON nota (organismo);
CREATE INDEX IF NOT EXISTS nota_body_status_idx ON nota (body_status);

-- Append-only. Every field-level change ever observed, so "when did this
-- decree's title change, and to what?" is answerable after the fact.
CREATE TABLE IF NOT EXISTS nota_revision (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo     INTEGER NOT NULL REFERENCES nota(codigo),
    revision   INTEGER NOT NULL,
    changed_at TEXT    NOT NULL,
    field      TEXT    NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    UNIQUE (codigo, revision, field)
);

-- The run ledger. Proving idempotency is an empirical claim, so the numbers
-- that back it have to be recorded, not asserted in a README.
CREATE TABLE IF NOT EXISTS run (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    command        TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'running',
    inserted       INTEGER NOT NULL DEFAULT 0,
    updated        INTEGER NOT NULL DEFAULT 0,
    unchanged      INTEGER NOT NULL DEFAULT 0,
    skipped_robots INTEGER NOT NULL DEFAULT 0,
    no_edition     INTEGER NOT NULL DEFAULT 0,
    errors         INTEGER NOT NULL DEFAULT 0,
    requests       INTEGER NOT NULL DEFAULT 0,
    retries        INTEGER NOT NULL DEFAULT 0,
    bytes_down     INTEGER NOT NULL DEFAULT 0,
    slept_s        REAL    NOT NULL DEFAULT 0,
    detail         TEXT
);
"""

# Fields whose change we care about enough to record individually.
TRACKED_FIELDS = ("titulo", "organismo", "poder", "seccion", "fecha", "edicion")


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class RunCounters:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_robots: int = 0
    no_edition: int = 0
    errors: int = 0
    detail: list[str] = field(default_factory=list)

    def merge(self, other: RunCounters) -> None:
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.skipped_robots += other.skipped_robots
        self.no_edition += other.no_edition
        self.errors += other.errors
        self.detail.extend(other.detail)

    def as_line(self) -> str:
        return (
            f"inserted={self.inserted} updated={self.updated} unchanged={self.unchanged} "
            f"skipped_robots={self.skipped_robots} no_edition={self.no_edition} "
            f"errors={self.errors}"
        )


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # WAL so a long-running crawl does not block `dof-ingest stats` in
        # another terminal. NORMAL sync is the right trade for a corpus we can
        # always re-derive from the source.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        current = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path} was written by a newer schema (v{current} > v{SCHEMA_VERSION}). "
                "Refusing to touch it."
            )
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # IMMEDIATE takes the write lock up front instead of upgrading
        # mid-transaction, which is where SQLITE_BUSY deadlocks come from.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- notas -------------------------------------------------------------

    def upsert_nota(self, nota: Nota, now: str | None = None) -> str:
        """Insert, update, or touch. Returns 'inserted' | 'updated' | 'unchanged'.

        This is the idempotency contract, and it has exactly three outcomes:

          * never seen        -> INSERT, revision 1
          * seen, hash equal  -> touch `last_seen_at` and nothing else
          * seen, hash differs-> UPDATE, revision += 1, append the field diffs
                                 to `nota_revision`, move `last_changed_at`

        The middle case is the one that matters. Running the pipeline twice
        over the same range produces `updated=0`, and the rows are not merely
        left alone -- they are *proven* untouched, because `last_changed_at`
        and `revision` did not move while `last_seen_at` did. That distinction
        is what turns "no duplicates" from a claim into a measurement.
        """
        now = now or utcnow()
        new_hash = nota.hash()
        row = self.conn.execute(
            "SELECT * FROM nota WHERE codigo = ?", (nota.codigo,)
        ).fetchone()

        if row is None:
            self.conn.execute(
                """INSERT INTO nota (codigo, fecha, edicion, seccion, poder, organismo,
                                     titulo, url_detalle, url_origen, content_hash,
                                     revision, first_seen_at, last_seen_at, last_changed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                (
                    nota.codigo, nota.fecha, nota.edicion, nota.seccion, nota.poder,
                    nota.organismo, nota.titulo, nota.url_detalle, nota.url_origen,
                    new_hash, now, now, now,
                ),
            )
            return "inserted"

        if row["content_hash"] == new_hash:
            # Cheap by design: one UPDATE of one column. Re-crawling a decade
            # of the DOF should cost writes proportional to what changed, not
            # to what exists.
            self.conn.execute(
                "UPDATE nota SET last_seen_at = ? WHERE codigo = ?", (now, nota.codigo)
            )
            return "unchanged"

        revision = int(row["revision"]) + 1
        for fname in TRACKED_FIELDS:
            old, new = row[fname], getattr(nota, fname)
            if old != new:
                self.conn.execute(
                    """INSERT OR IGNORE INTO nota_revision
                       (codigo, revision, changed_at, field, old_value, new_value)
                       VALUES (?,?,?,?,?,?)""",
                    (nota.codigo, revision, now, fname, old, new),
                )
        self.conn.execute(
            """UPDATE nota SET fecha=?, edicion=?, seccion=?, poder=?, organismo=?,
                               titulo=?, url_detalle=?, url_origen=?, content_hash=?,
                               revision=?, last_seen_at=?, last_changed_at=?
               WHERE codigo=?""",
            (
                nota.fecha, nota.edicion, nota.seccion, nota.poder, nota.organismo,
                nota.titulo, nota.url_detalle, nota.url_origen, new_hash,
                revision, now, now, nota.codigo,
            ),
        )
        return "updated"

    def set_body(
        self, codigo: int, status: str, text: str | None = None, now: str | None = None
    ) -> str:
        """Store an enriched body. Returns 'inserted' | 'unchanged' | 'updated'.

        Body text gets its own hash and its own change signal, independent of
        the index metadata: the DOF does amend the text of a published nota
        without touching its title, and we want to notice.
        """
        now = now or utcnow()
        new_hash = content_hash({"body": text}) if text is not None else None
        row = self.conn.execute(
            "SELECT body_hash, body_status FROM nota WHERE codigo = ?", (codigo,)
        ).fetchone()
        if row is None:
            raise KeyError(f"nota {codigo} not in store")

        if row["body_hash"] == new_hash and row["body_status"] == status:
            outcome = "unchanged"
        elif row["body_hash"] is None and new_hash is not None:
            outcome = "inserted"
        else:
            outcome = "updated"

        # Only advance last_changed_at when the text actually moved.
        self.conn.execute(
            """UPDATE nota SET body_text=?, body_hash=?, body_status=?, body_fetched_at=?,
                               last_changed_at = COALESCE(?, last_changed_at)
               WHERE codigo=?""",
            (text, new_hash, status, now, None if outcome == "unchanged" else now, codigo),
        )
        return outcome

    def pending_bodies(self, limit: int, retry_errors: bool = False) -> list[sqlite3.Row]:
        statuses = ["pending"] + (["error"] if retry_errors else [])
        placeholders = ",".join("?" * len(statuses))
        return list(
            self.conn.execute(
                f"""SELECT codigo, url_detalle FROM nota
                    WHERE body_status IN ({placeholders})
                    ORDER BY fecha DESC, codigo LIMIT ?""",
                (*statuses, limit),
            )
        )

    # -- editions ----------------------------------------------------------

    def record_edition(
        self, fecha: str, edicion: str, status: str, notas: int, rejected: int,
        page_hash: str | None, now: str | None = None,
    ) -> bool:
        """Returns True if this edition's content changed since last check."""
        now = now or utcnow()
        row = self.conn.execute(
            "SELECT content_hash FROM edition WHERE fecha=? AND edicion=?", (fecha, edicion)
        ).fetchone()
        changed = row is None or row["content_hash"] != page_hash
        if row is None:
            self.conn.execute(
                """INSERT INTO edition (fecha, edicion, status, notas_found,
                                        doc_only_rejected, content_hash,
                                        first_seen_at, last_checked_at, last_changed_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (fecha, edicion, status, notas, rejected, page_hash, now, now, now),
            )
        else:
            self.conn.execute(
                """UPDATE edition SET status=?, notas_found=?, doc_only_rejected=?,
                                      content_hash=?, last_checked_at=?,
                                      last_changed_at = COALESCE(?, last_changed_at)
                   WHERE fecha=? AND edicion=?""",
                (status, notas, rejected, page_hash, now, now if changed else None, fecha, edicion),
            )
        return changed

    def known_editions(self, fechas: Sequence[str]) -> dict[tuple[str, str], str]:
        if not fechas:
            return {}
        qs = ",".join("?" * len(fechas))
        rows = self.conn.execute(
            f"SELECT fecha, edicion, status FROM edition WHERE fecha IN ({qs})", tuple(fechas)
        )
        return {(r["fecha"], r["edicion"]): r["status"] for r in rows}

    # -- run ledger --------------------------------------------------------

    def start_run(self, command: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO run (command, started_at) VALUES (?,?)", (command, utcnow())
        )
        return int(cur.lastrowid or 0)

    def finish_run(
        self, run_id: int, counters: RunCounters, http: dict[str, Any], status: str = "ok"
    ) -> None:
        self.conn.execute(
            """UPDATE run SET finished_at=?, status=?, inserted=?, updated=?, unchanged=?,
                              skipped_robots=?, no_edition=?, errors=?,
                              requests=?, retries=?, bytes_down=?, slept_s=?, detail=?
               WHERE run_id=?""",
            (
                utcnow(), status, counters.inserted, counters.updated, counters.unchanged,
                counters.skipped_robots, counters.no_edition, counters.errors,
                http.get("requests", 0), http.get("retries", 0), http.get("bytes_down", 0),
                http.get("slept_s", 0.0),
                json.dumps(counters.detail[:50], ensure_ascii=False) if counters.detail else None,
                run_id,
            ),
        )

    # -- reporting & export ------------------------------------------------

    def stats(self) -> dict[str, Any]:
        def one(query: str) -> int | str:
            return self.conn.execute(query).fetchone()[0]  # type: ignore[no-any-return]

        return {
            "notas": one("SELECT COUNT(*) FROM nota"),
            "notas_con_texto": one("SELECT COUNT(*) FROM nota WHERE body_status='ok'"),
            "notas_pendientes": one("SELECT COUNT(*) FROM nota WHERE body_status='pending'"),
            "notas_bloqueadas_por_robots": one(
                "SELECT COUNT(*) FROM nota WHERE body_status='robots_denied'"
            ),
            "notas_revisadas_mas_de_una_vez": one("SELECT COUNT(*) FROM nota WHERE revision > 1"),
            "cambios_registrados": one("SELECT COUNT(*) FROM nota_revision"),
            "ediciones_ok": one("SELECT COUNT(*) FROM edition WHERE status='ok'"),
            "ediciones_sin_publicacion": one(
                "SELECT COUNT(*) FROM edition WHERE status='no_edition'"
            ),
            "rango": one("SELECT COALESCE(MIN(fecha)||' .. '||MAX(fecha),'-') FROM nota"),
            "organismos": one("SELECT COUNT(DISTINCT organismo) FROM nota"),
            "corridas": one("SELECT COUNT(*) FROM run"),
        }

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM run ORDER BY run_id DESC LIMIT ?", (limit,)
            )
        )

    EXPORT_COLUMNS = (
        "codigo", "fecha", "edicion", "seccion", "poder", "organismo", "titulo",
        "url_detalle", "content_hash", "body_hash", "body_status", "revision",
        "first_seen_at", "last_seen_at", "last_changed_at",
    )

    def export(self, out: TextIO, fmt: str = "jsonl", include_body: bool = False) -> int:
        cols = list(self.EXPORT_COLUMNS) + (["body_text"] if include_body else [])
        rows = self.conn.execute(
            f"SELECT {','.join(cols)} FROM nota ORDER BY fecha, edicion, codigo"
        )
        n = 0
        if fmt == "csv":
            w = csv.writer(out)
            w.writerow(cols)
            for r in rows:
                w.writerow([r[c] for c in cols])
                n += 1
        else:
            for r in rows:
                out.write(json.dumps(dict(zip(cols, r, strict=True)), ensure_ascii=False) + "\n")
                n += 1
        return n
