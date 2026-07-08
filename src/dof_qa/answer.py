"""Grounded answering: plan -> retrieve -> answer, with citations or a refusal.

The refusal path gets as much attention as the answer path. A QA system over a
legal corpus that guesses is worse than one that says "no sé", because a
confident wrong answer about a decree is acted on.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .llm import Answer, Provider
from .retrieve import Coverage, Hit, coverage, retrieve, run_sql
from .route import Plan
from .route import plan as make_plan

log = logging.getLogger(__name__)

# Below this BM25 score the top hit is lexical noise -- a stopword-ish overlap
# rather than a topical match. Tuned on the golden set; it is an ablation axis
# and the single biggest lever on the refusal-rate metrics.
MIN_SCORE = 0.5


@dataclass(slots=True)
class Result:
    question: str
    plan: Plan
    hits: list[Hit] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    answer: Answer | None = None
    refused_because: str = ""

    @property
    def citations(self) -> list[int]:
        return self.answer.citations if self.answer else []

    @property
    def refused(self) -> bool:
        return self.answer is None or not self.answer.sufficient


def build_context(hits: list[Hit], max_chars: int = 1400) -> str:
    """Render retrieved passages for the model.

    The `codigo` is printed in a fixed, machine-checkable position on every
    block. That is what lets the eval harness verify a citation actually
    corresponds to a passage the model was shown, rather than to a number it
    invented that happens to look like a DOF code.
    """
    blocks = []
    for hit in hits:
        body = hit.texto[:max_chars]
        blocks.append(
            f"[nota codigo={hit.codigo}] {hit.organismo} — {hit.fecha} ({hit.edicion})\n"
            f"Título: {hit.titulo}\n{body}"
        )
    return "\n---\n".join(blocks)


def _coverage_refusal(plan: Plan, cov: Coverage) -> str:
    """Refuse *specifically* when the question is outside what we hold.

    "No sé" and "no tengo datos de ese periodo" are different answers, and the
    second one is far more useful: it tells the user the question is fine and
    the corpus is the limitation. It also keeps the eval honest, because a
    corpus-coverage refusal is a correct refusal rather than a retrieval miss.
    """
    f = plan.filters
    if (f.fecha_desde or f.fecha_hasta) and not cov.covers_range(f.fecha_desde, f.fecha_hasta):
        return (
            f"El corpus sólo cubre {cov.fecha_min} a {cov.fecha_max}; "
            f"la pregunta pide {f.fecha_desde or '*'} a {f.fecha_hasta or '*'}."
        )
    for org in f.organismos:
        if not cov.covers_agency(org):
            return f"No hay notas de ese organismo en el corpus ({cov.n_notas} notas indexadas)."
    return ""


def ask(
    conn: sqlite3.Connection,
    question: str,
    provider: Provider,
    k: int = 8,
    today: date | None = None,
    use_expansion: bool = True,
    use_prefilter: bool = True,
    use_prefix: bool = True,
    min_score: float = MIN_SCORE,
) -> Result:
    """Answer one question. Never raises on a bad question -- refuses instead."""
    cov = coverage(conn)
    p = make_plan(question, cov.organismos, today=today)
    result = Result(question=question, plan=p)
    log.debug("plan: %s", p.describe())

    # 1. Coverage gate, before any retrieval. Cheap, and it produces a better
    #    refusal than an empty result set would.
    reason = _coverage_refusal(p, cov)
    if reason:
        result.refused_because = "out_of_coverage"
        result.answer = Answer.refusal(reason, provider="gate")
        return result

    # 2. Exact path. Counting and enumeration are SQL problems; asking a
    #    language model to count retrieved passages is asking it to be wrong.
    if p.strategy == "sql":
        result.rows = run_sql(conn, p)
        if not result.rows:
            result.refused_because = "no_rows"
            result.answer = Answer.refusal(
                f"No hay notas que cumplan ese criterio ({p.filters.describe()}).", "gate"
            )
            return result
        result.answer = _answer_from_sql(p, result.rows)
        return result

    # 3. Retrieval / hybrid path.
    result.hits = retrieve(
        conn, p, k=k, use_expansion=use_expansion,
        use_prefilter=use_prefilter, use_prefix=use_prefix,
    )
    if not result.hits:
        result.refused_because = "no_hits"
        result.answer = Answer.refusal("No encontré pasajes relevantes en el corpus.", "gate")
        return result
    if result.hits[0].bm25 < min_score:
        # Something matched, but only weakly. Handing weak context to a model
        # is how ungrounded answers get generated: it will use what it is
        # given. Refusing here is a retrieval decision, not a model decision.
        result.refused_because = "low_score"
        result.answer = Answer.refusal(
            f"Los pasajes encontrados no son suficientemente relevantes "
            f"(mejor coincidencia BM25 {result.hits[0].bm25:.2f}).",
            "gate",
        )
        return result

    context = build_context(result.hits)
    result.answer = provider.answer(question, context)

    # 4. Citation validation. A citation to a note that was not in the context
    #    is a fabrication, no matter how plausible the prose around it is.
    shown = {h.codigo for h in result.hits}
    invented = [c for c in result.answer.citations if c not in shown]
    if invented:
        log.warning("provider %s cited notes it was not shown: %s", provider.name, invented)
        result.answer.citations = [c for c in result.answer.citations if c in shown]
        if not result.answer.citations:
            result.refused_because = "fabricated_citations"
            result.answer = Answer.refusal(
                "La respuesta citó notas que no estaban en el contexto; se descartó.",
                provider.name,
            )
    return result


def _answer_from_sql(p: Plan, rows: list[dict[str, Any]]) -> Answer:
    """Compose an exact answer with real citations, no model involved."""
    if p.wants_count:
        total = sum(int(r["n"]) for r in rows)
        detail = "; ".join(f"{r['organismo']}: {r['n']}" for r in rows[:10])
        return Answer(
            sufficient=True,
            answer=f"{total} notas ({p.filters.describe()}). Por organismo — {detail}.",
            citations=[],
            provider="sql",
        )
    listing = "\n".join(
        f"- [{r['codigo']}] {r['fecha']} {r['organismo']}: {r['titulo'][:110]}" for r in rows[:20]
    )
    return Answer(
        sufficient=True,
        answer=f"{len(rows)} notas ({p.filters.describe()}):\n{listing}",
        citations=[int(r["codigo"]) for r in rows[:20]],
        provider="sql",
    )
