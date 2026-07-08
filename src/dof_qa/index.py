"""Chunking and the FTS5 index.

The retrieval substrate. Deliberately built on SQLite FTS5 rather than a vector
store, for reasons that are measured rather than asserted -- see
`docs/ABLATION.md`. The short version: Mexican legal Spanish is a
high-precision lexical domain. Statutes are cited by exact article number, and
agencies are named by exact legal title. BM25 over a diacritic-folding
tokenizer is very hard to beat there, and it costs zero dependencies, runs in
CI, and returns identical results on every machine.

The index is rebuilt idempotently against `nota.body_hash`, reusing the change
detection the ingestion pipeline already computes: a note whose text has not
moved is not re-chunked.
"""

from __future__ import annotations

import itertools
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass

log = logging.getLogger(__name__)

SCHEMA = """
-- Chunks are stored in their own table so we can rebuild the FTS index
-- (different tokenizer, different chunk size) without re-deriving them, and so
-- a chunk has a stable identity to cite.
CREATE TABLE IF NOT EXISTS chunk (
    chunk_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo     INTEGER NOT NULL REFERENCES nota(codigo),
    chunk_ix   INTEGER NOT NULL,      -- position within the note, 0-based
    texto      TEXT    NOT NULL,
    n_chars    INTEGER NOT NULL,
    -- Provenance for citations: which part of the note this came from.
    char_start INTEGER NOT NULL,
    char_end   INTEGER NOT NULL,
    UNIQUE (codigo, chunk_ix)
);
CREATE INDEX IF NOT EXISTS chunk_codigo_idx ON chunk (codigo);

-- Tracks what the index was built from, so `build` is incremental and
-- idempotent: same corpus in, zero writes out.
CREATE TABLE IF NOT EXISTS chunk_source (
    codigo      INTEGER PRIMARY KEY REFERENCES nota(codigo),
    body_hash   TEXT NOT NULL,
    chunk_chars INTEGER NOT NULL,
    overlap     INTEGER NOT NULL,
    n_chunks    INTEGER NOT NULL,
    built_at    TEXT NOT NULL
);
"""

# `remove_diacritics 2` is the Unicode-aware form (the older `1` mishandles
# characters outside Latin-1). It is what makes "informacion" match
# "información", which matters because users type without accents and the DOF
# never omits them. Whether it actually moves recall is measured, not assumed.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    titulo,
    organismo,
    texto,
    chunk_id UNINDEXED,
    codigo   UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2"
);
"""


@dataclass(frozen=True, slots=True)
class ChunkConfig:
    """Every knob here is an ablation axis. Defaults are the ones that won."""

    chunk_chars: int = 1200
    overlap: int = 150
    remove_diacritics: bool = True

    @property
    def tokenizer(self) -> str:
        return (
            'unicode61 remove_diacritics 2' if self.remove_diacritics
            else 'unicode61 remove_diacritics 0'
        )


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------

# DOF notices are structured legal prose. They break at these markers far more
# meaningfully than at a fixed character count, so we prefer them as split
# points and only fall back to hard slicing inside an oversized section.
_SECTION = re.compile(
    r"(?m)^(?=\s*(?:"
    r"ART[IÍ]CULO\s+(?:PRIMERO|SEGUNDO|TERCERO|CUARTO|QUINTO|SEXTO|S[EÉ]PTIMO|OCTAVO|"
    r"NOVENO|D[EÉ]CIMO|[0-9]+)"
    r"|CONSIDERANDO|TRANSITORIOS?|ANEXO|CAP[IÍ]TULO|SECCI[OÓ]N|T[IÍ]TULO"
    r")\b)"
)

_PARAGRAPH = re.compile(r"\n\s*\n")


def split_text(text: str, cfg: ChunkConfig) -> list[tuple[int, int, str]]:
    """Split into (char_start, char_end, text) chunks.

    Three tiers, in order of preference:

      1. legal section markers (ARTÍCULO, CONSIDERANDO, TRANSITORIOS, ...);
      2. paragraph breaks;
      3. hard character slicing with overlap.

    Tier 3 exists because DOF notices routinely arrive as one unbroken
    paragraph -- the HTML-to-text conversion loses the original layout -- so a
    purely structural splitter would emit a single 53 KB chunk and destroy
    retrieval precision. Overlap only applies to tier 3: splitting *at* a legal
    boundary and then bleeding the next article backwards would put text under
    the wrong article number, which in a legal corpus is a correctness bug, not
    a tuning choice.
    """
    text = text.strip()
    if not text:
        return []

    # Tier 1 / 2: find candidate boundaries.
    bounds = [m.start() for m in _SECTION.finditer(text)]
    if len(bounds) < 2:
        bounds = [m.end() for m in _PARAGRAPH.finditer(text)]
    bounds = [0] + [b for b in bounds if b > 0] + [len(text)]
    bounds = sorted(set(bounds))

    pieces: list[tuple[int, int]] = []
    for start, end in itertools.pairwise(bounds):
        if end - start <= cfg.chunk_chars:
            if end > start:
                pieces.append((start, end))
            continue
        # Tier 3: this section is too big; slice it with overlap.
        step = max(1, cfg.chunk_chars - cfg.overlap)
        pos = start
        while pos < end:
            stop = min(pos + cfg.chunk_chars, end)
            pieces.append((pos, stop))
            if stop >= end:
                break
            pos += step

    out = []
    for start, end in pieces:
        body = text[start:end].strip()
        if len(body) >= 40:  # a 3-word fragment is noise, not a passage
            out.append((start, end, body))
    return out or [(0, len(text), text)]


# --------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------


def ensure_schema(conn: sqlite3.Connection, cfg: ChunkConfig) -> None:
    conn.executescript(SCHEMA)
    conn.executescript(FTS_SCHEMA.replace('unicode61 remove_diacritics 2', cfg.tokenizer))


def drop_index(conn: sqlite3.Connection) -> None:
    """Tear down the derived index only. `nota` is never touched.

    Used by the ablation runner, which rebuilds the index once per
    configuration. Everything here is re-derivable from `nota`, so this is
    safe in a way that dropping source data never would be.
    """
    conn.executescript(
        "DROP TABLE IF EXISTS chunk_fts;"
        "DROP TABLE IF EXISTS chunk;"
        "DROP TABLE IF EXISTS chunk_source;"
    )


def build(
    conn: sqlite3.Connection, cfg: ChunkConfig | None = None, force: bool = False
) -> dict[str, int]:
    """(Re)build the chunk index. Returns counters.

    Idempotent by the same mechanism the scraper uses: a note is re-chunked
    only if its `body_hash` changed, or the chunking config changed. Run it
    twice and the second run reports `indexed=0 unchanged=N`.
    """
    cfg = cfg or ChunkConfig()
    ensure_schema(conn, cfg)

    known = {
        int(r["codigo"]): (r["body_hash"], r["chunk_chars"], r["overlap"])
        for r in conn.execute("SELECT * FROM chunk_source")
    }
    counters = {"indexed": 0, "unchanged": 0, "skipped_no_text": 0, "chunks": 0}

    rows = conn.execute(
        """SELECT codigo, titulo, organismo, body_text, body_hash, body_status
           FROM nota ORDER BY codigo"""
    ).fetchall()

    for row in rows:
        codigo = int(row["codigo"])
        if row["body_status"] != "ok" or not row["body_text"]:
            # robots_denied / pending / error. Not an error here: the note is
            # still in the corpus with its metadata, it just has no full text
            # to retrieve over. Structured questions can still reach it.
            counters["skipped_no_text"] += 1
            continue

        signature = (row["body_hash"], cfg.chunk_chars, cfg.overlap)
        if not force and known.get(codigo) == signature:
            counters["unchanged"] += 1
            continue

        conn.execute("DELETE FROM chunk_fts WHERE codigo = ?", (codigo,))
        conn.execute("DELETE FROM chunk WHERE codigo = ?", (codigo,))

        pieces = split_text(row["body_text"], cfg)
        for ix, (start, end, body) in enumerate(pieces):
            cur = conn.execute(
                """INSERT INTO chunk (codigo, chunk_ix, texto, n_chars, char_start, char_end)
                   VALUES (?,?,?,?,?,?)""",
                (codigo, ix, body, len(body), start, end),
            )
            chunk_id = int(cur.lastrowid or 0)
            # The title and agency are repeated into every chunk of the note.
            # They are short, heavily weighted at query time, and carry the
            # signal most questions actually key on ("¿qué publicó COFEPRIS?").
            # Without this, a chunk from the middle of a decree is unreachable
            # by the agency that issued it.
            conn.execute(
                """INSERT INTO chunk_fts (titulo, organismo, texto, chunk_id, codigo)
                   VALUES (?,?,?,?,?)""",
                (row["titulo"], row["organismo"], body, chunk_id, codigo),
            )
            counters["chunks"] += 1

        conn.execute(
            """INSERT INTO chunk_source (codigo, body_hash, chunk_chars, overlap,
                                         n_chunks, built_at)
               VALUES (?,?,?,?,?,datetime('now'))
               ON CONFLICT(codigo) DO UPDATE SET
                   body_hash=excluded.body_hash, chunk_chars=excluded.chunk_chars,
                   overlap=excluded.overlap, n_chunks=excluded.n_chunks,
                   built_at=excluded.built_at""",
            (codigo, row["body_hash"], cfg.chunk_chars, cfg.overlap, len(pieces)),
        )
        counters["indexed"] += 1

    # Notes deleted from the corpus (never happens today, but the index must
    # not outlive its source) lose their chunks too.
    conn.execute(
        "DELETE FROM chunk_source WHERE codigo NOT IN (SELECT codigo FROM nota)"
    )
    return counters


def fold(text: str) -> str:
    """Diacritic-fold and lowercase, matching what the FTS tokenizer does.

    Used on the *query* side so acronym expansion and highlighting agree with
    the index rather than quietly disagreeing with it.
    """
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
