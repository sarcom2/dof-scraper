"""Executing a plan: BM25 over pre-filtered candidates, plus exact SQL.

Two retrieval paths and one fusion step. Nothing here calls a model, so every
number the eval harness reports for retrieval is reproducible on any machine.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .route import Filters, Plan

# BM25 field weights. `bm25()` in FTS5 returns a *negative* score (more
# negative = better), so we negate it everywhere below to get the usual
# "bigger is better" convention.
#
# Title is weighted heavily because DOF titles are unusually informative --
# they are effectively a one-sentence abstract written by the issuing agency
# ("ACUERDO por el que se dan a conocer los montos de los estímulos fiscales
# aplicables a..."). Agency sits between title and body. These three numbers
# are an ablation axis.
W_TITULO, W_ORGANISMO, W_TEXTO = 8.0, 4.0, 1.0


@dataclass(slots=True)
class Hit:
    chunk_id: int
    codigo: int
    chunk_ix: int
    # `score` is whatever ranked this hit — BM25 alone, or an RRF fusion score.
    # `bm25` is always the raw lexical score. They must stay separate: RRF
    # scores live around 1/(60+rank) ~ 0.016 while BM25 runs 0-20, so a
    # relevance threshold applied to `score` silently rejects everything the
    # moment a second query formulation is fused in. That bug refused
    # perfectly-ranked results and cost ~24% over-refusal before it was found.
    score: float
    bm25: float
    texto: str
    titulo: str
    organismo: str
    fecha: str
    edicion: str
    url: str

    def citation(self) -> str:
        return f"DOF {self.fecha} ({self.edicion}) — {self.organismo} — nota {self.codigo}"


@dataclass(slots=True)
class Coverage:
    """What the corpus actually contains. The basis for honest refusals."""

    n_notas: int
    n_con_texto: int
    fecha_min: str | None
    fecha_max: str | None
    organismos: list[str] = field(default_factory=list)

    def covers_range(self, desde: str | None, hasta: str | None) -> bool:
        if not self.fecha_min or not self.fecha_max:
            return False
        if desde and desde > self.fecha_max:
            return False
        return not (hasta and hasta < self.fecha_min)

    def covers_agency(self, folded_needle: str) -> bool:
        from .index import fold

        return any(folded_needle in fold(o) or fold(o) in folded_needle for o in self.organismos)


def coverage(conn: sqlite3.Connection) -> Coverage:
    row = conn.execute(
        """SELECT COUNT(*) n, SUM(body_status='ok') ok, MIN(fecha) lo, MAX(fecha) hi
           FROM nota"""
    ).fetchone()
    orgs = [r[0] for r in conn.execute("SELECT DISTINCT organismo FROM nota ORDER BY 1")]
    return Coverage(
        n_notas=int(row["n"] or 0),
        n_con_texto=int(row["ok"] or 0),
        fecha_min=row["lo"],
        fecha_max=row["hi"],
        organismos=orgs,
    )


# --------------------------------------------------------------------------
# FTS query construction
# --------------------------------------------------------------------------

_SAFE = re.compile(r"[^\w\s]", re.UNICODE)


# Spanish inflects almost entirely by suffix, so a fixed-length prefix behaves
# as a cheap stemmer: "exportaciones" and "exportar" share "export". Without
# this, "¿qué publicó la Secretaría de Economía sobre exportaciones?" matched
# nothing, because the note says "exportar". A real Snowball stemmer would be
# more precise, but it is a new dependency and the ablation shows the prefix
# trick recovers most of the gap.
PREFIX_LEN = 6


def fts_query(
    terms: list[str],
    expansions: dict[str, list[str]] | None = None,
    use_prefix: bool = True,
) -> str:
    """Build a safe FTS5 MATCH expression.

    Trust boundary. FTS5 MATCH has its own syntax — `NEAR`, `*`, `^`, `-`,
    quotes, column filters — and user text flows straight into it. An
    unescaped apostrophe or a stray `"` is at best a crash and at worst a
    query that silently means something else. So every term is stripped of
    punctuation and wrapped in double quotes, which makes it a literal
    string token to FTS5 regardless of content.

    Terms are OR-ed, not AND-ed. In a corpus this specialised, requiring every
    term collapses recall to zero on the first vocabulary mismatch; BM25 already
    rewards documents that match more of them.
    """
    parts: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        cleaned = _SAFE.sub(" ", term).strip()
        for token in cleaned.split():
            if len(token) <= 2 or token in seen:
                continue
            seen.add(token)
            if use_prefix and len(token) > PREFIX_LEN:
                # `"stem"*` is a prefix query. The quotes still make the stem a
                # literal token, so user text cannot break out into FTS syntax.
                parts.append(f'"{token[:PREFIX_LEN]}"*')
            else:
                parts.append(f'"{token}"')

    for t in terms:
        add(t)
    for alts in (expansions or {}).values():
        for alt in alts:
            add(alt)

    return " OR ".join(parts)


def _filter_sql(filters: Filters) -> tuple[str, list[Any]]:
    """Structured predicates as a *pre*-filter, joined before LIMIT."""
    clauses, params = [], []
    if filters.organismos:
        ors = []
        for org in filters.organismos:
            # The corpus stores agency names unaccented and uppercase already,
            # but we fold both sides so the filter cannot fail on a stray
            # accent that the source happened to include.
            ors.append("LOWER(n.organismo) LIKE ?")
            params.append(f"%{org}%")
        clauses.append("(" + " OR ".join(ors) + ")")
    if filters.fecha_desde:
        clauses.append("n.fecha >= ?")
        params.append(filters.fecha_desde)
    if filters.fecha_hasta:
        clauses.append("n.fecha <= ?")
        params.append(filters.fecha_hasta)
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


# --------------------------------------------------------------------------
# retrieval
# --------------------------------------------------------------------------


def search(
    conn: sqlite3.Connection,
    match: str,
    filters: Filters,
    k: int = 10,
    weights: tuple[float, float, float] = (W_TITULO, W_ORGANISMO, W_TEXTO),
) -> list[Hit]:
    """BM25 over the candidate set that survives the structured filter."""
    if not match:
        return []
    where, params = _filter_sql(filters)
    sql = f"""
        SELECT f.chunk_id, f.codigo, c.chunk_ix, c.texto,
               n.titulo, n.organismo, n.fecha, n.edicion, n.url_detalle,
               bm25(chunk_fts, ?, ?, ?) AS raw
        FROM chunk_fts f
        JOIN chunk c ON c.chunk_id = f.chunk_id
        JOIN nota  n ON n.codigo   = f.codigo
        WHERE chunk_fts MATCH ?{where}
        ORDER BY raw
        LIMIT ?
    """
    rows = conn.execute(sql, [*weights, match, *params, k]).fetchall()
    return [
        Hit(
            chunk_id=int(r["chunk_id"]),
            codigo=int(r["codigo"]),
            chunk_ix=int(r["chunk_ix"]),
            score=-float(r["raw"]),  # FTS5 bm25 is negative; flip it
            bm25=-float(r["raw"]),
            texto=r["texto"],
            titulo=r["titulo"],
            organismo=r["organismo"],
            fecha=r["fecha"],
            edicion=r["edicion"],
            url=r["url_detalle"],
        )
        for r in rows
    ]


def rrf_fuse(runs: list[list[Hit]], k: int = 10, damping: int = 60) -> list[Hit]:
    """Reciprocal Rank Fusion across several query formulations.

    Used to combine the literal question with its acronym/synonym expansion.
    RRF rather than score addition because BM25 scores from different MATCH
    expressions are not on a comparable scale — a rare-term query produces
    much larger magnitudes than a common-term one, so summing lets the
    narrower query dominate purely by arithmetic. RRF only uses rank, which
    is exactly the property we want when the runs are not commensurable.

    `damping=60` is the value from the original RRF paper; it is flat enough
    over 10-100 that tuning it is not worth the overfitting risk on a golden
    set this size.
    """
    scores: dict[int, float] = {}
    best: dict[int, Hit] = {}
    for run in runs:
        for rank, hit in enumerate(run, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (damping + rank)
            if hit.chunk_id not in best:
                best[hit.chunk_id] = hit
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:k]
    out = []
    for chunk_id, score in ordered:
        hit = best[chunk_id]
        out.append(
            Hit(
                hit.chunk_id, hit.codigo, hit.chunk_ix, score, hit.bm25, hit.texto,
                hit.titulo, hit.organismo, hit.fecha, hit.edicion, hit.url,
            )
        )
    return out


def dedupe_by_nota(hits: list[Hit], per_nota: int = 2) -> list[Hit]:
    """Cap chunks per notice.

    A 68 KB decree shatters into ~60 chunks; without a cap it can occupy the
    entire top-k and starve every other notice. Citation *coverage* — how many
    distinct notices the answer rests on — matters more than depth into one.
    """
    seen: dict[int, int] = {}
    out = []
    for hit in hits:
        n = seen.get(hit.codigo, 0)
        if n < per_nota:
            seen[hit.codigo] = n + 1
            out.append(hit)
    return out


def run_sql(conn: sqlite3.Connection, plan: Plan, limit: int = 200) -> list[dict[str, Any]]:
    """The exact-answer path: enumerate or count, never rank."""
    where, params = _filter_sql(plan.filters)
    where = where.removeprefix(" AND ") or "1=1"
    if plan.wants_count:
        rows = conn.execute(
            f"""SELECT n.organismo, COUNT(*) AS n
                FROM nota n WHERE {where}
                GROUP BY n.organismo ORDER BY n DESC""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    rows = conn.execute(
        f"""SELECT n.codigo, n.fecha, n.edicion, n.organismo, n.titulo, n.url_detalle
            FROM nota n WHERE {where}
            ORDER BY n.fecha DESC, n.codigo LIMIT ?""",
        [*params, limit],
    ).fetchall()
    return [dict(r) for r in rows]


def retrieve(
    conn: sqlite3.Connection,
    plan: Plan,
    k: int = 10,
    use_expansion: bool = True,
    use_prefilter: bool = True,
    use_prefix: bool = True,
    per_nota: int = 2,
    weights: tuple[float, float, float] = (W_TITULO, W_ORGANISMO, W_TEXTO),
) -> list[Hit]:
    """Full retrieval for a plan. The flags are the ablation surface."""
    filters = plan.filters if use_prefilter else Filters()

    runs = []
    literal = fts_query(plan.terms, use_prefix=use_prefix)
    if literal:
        runs.append(search(conn, literal, filters, k=k * 3, weights=weights))
    if use_expansion and plan.expansions:
        expanded = fts_query(plan.terms, plan.expansions, use_prefix=use_prefix)
        if expanded and expanded != literal:
            runs.append(search(conn, expanded, filters, k=k * 3, weights=weights))

    if not runs:
        return []
    fused = rrf_fuse(runs, k=k * 3) if len(runs) > 1 else runs[0]
    return dedupe_by_nota(fused, per_nota=per_nota)[:k]
