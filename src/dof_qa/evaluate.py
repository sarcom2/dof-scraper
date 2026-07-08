"""The eval harness.

This is the centre of the project, not the agent. A retrieval system without a
published evaluation is a demo; the interesting engineering is in measuring a
non-deterministic component honestly, and in being willing to publish the
numbers that did not improve.

Four metrics, chosen because each one catches a failure the others miss:

  recall@k          Did retrieval put the right notes in front of the model?
                    Deterministic, no model needed -- so it runs in CI.
  citation F1       Did the answer cite what it actually used, and only that?
                    Precision catches over-citing ("cite everything, something
                    will be right"); recall catches under-citing.
  refusal rate      On questions the corpus genuinely cannot answer, does it
                    say so? A system that never refuses has no groundedness,
                    it just has confidence.
  over-refusal      The counterweight. Refusing everything scores 100% on the
                    metric above, so it is measured against answerable
                    questions too. Reporting one without the other is the
                    single easiest way to publish a flattering lie.

Plus routing accuracy, because the SQL/retrieval/hybrid decision is the part
of this system most likely to regress silently.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .answer import ask
from .llm import Provider


@dataclass(slots=True)
class GoldenQuestion:
    """One hand-verified question.

    `gold_codigos` are the notes a correct answer must rest on -- verified by
    reading the notes, not by running the system and recording what it found.
    A golden set built from the system's own output measures self-consistency,
    not correctness.
    """

    id: str
    question: str
    kind: str  # "answerable" | "unanswerable"
    strategy: str = ""  # expected route: sql | retrieval | hybrid
    gold_codigos: list[int] = field(default_factory=list)
    today: str = ""  # pins relative dates ("este trimestre") for reproducibility
    note: str = ""  # why this question is in the set

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> GoldenQuestion:
        return cls(
            id=str(raw["id"]),
            question=str(raw["question"]),
            kind=str(raw["kind"]),
            strategy=str(raw.get("strategy", "")),
            gold_codigos=[int(c) for c in raw.get("gold_codigos", [])],
            today=str(raw.get("today", "")),
            note=str(raw.get("note", "")),
        )


def load_golden(path: Path) -> list[GoldenQuestion]:
    """JSONL, not YAML: one question per line, zero dependencies, clean diffs.

    A golden set is reviewed in pull requests. Line-oriented beats nested here.
    """
    out = []
    for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            out.append(GoldenQuestion.parse(json.loads(line)))
        except (ValueError, KeyError) as exc:
            raise SystemExit(f"{path}:{lineno}: bad golden question: {exc}") from exc
    ids = [q.id for q in out]
    if len(set(ids)) != len(ids):
        raise SystemExit(f"{path}: duplicate question ids")
    return out


# --------------------------------------------------------------------------


@dataclass(slots=True)
class QuestionResult:
    id: str
    kind: str
    status: str  # ok | skipped_not_in_corpus
    expected_strategy: str = ""
    actual_strategy: str = ""
    recall_at_k: float = 0.0
    hit: bool = False
    refused: bool = False
    refused_because: str = ""
    citations: list[int] = field(default_factory=list)
    gold: list[int] = field(default_factory=list)
    citation_precision: float = 0.0
    citation_recall: float = 0.0
    retrieved: list[int] = field(default_factory=list)


def _corpus_codigos(conn: sqlite3.Connection) -> set[int]:
    return {int(r[0]) for r in conn.execute("SELECT codigo FROM nota")}


def evaluate(
    conn: sqlite3.Connection,
    golden: list[GoldenQuestion],
    provider: Provider,
    k: int = 8,
    generate: bool = True,
    **ask_kwargs: Any,
) -> dict[str, Any]:
    """Run the golden set. Returns a report dict (also the CI artifact).

    `generate=False` runs retrieval only -- no model call. That mode is what
    executes in CI: it is deterministic, free, needs no GPU or API key, and it
    covers the metric that actually gates quality (recall@k). If retrieval did
    not surface the right note, no amount of generation quality can recover.
    """
    present = _corpus_codigos(conn)
    results: list[QuestionResult] = []

    for q in golden:
        # Honesty gate. A question whose evidence is not in the corpus cannot
        # be scored as a retrieval failure -- there is nothing to retrieve. It
        # is reported as skipped, with a count, so a shrinking corpus shows up
        # as "fewer questions scored" rather than as a quiet metric drop.
        if q.kind == "answerable" and q.gold_codigos and not set(q.gold_codigos) & present:
            results.append(
                QuestionResult(
                    id=q.id, kind=q.kind,
                    status="skipped_not_in_corpus", gold=q.gold_codigos,
                )
            )
            continue

        today = date.fromisoformat(q.today) if q.today else None
        res = ask(
            conn,
            q.question,
            provider=provider if generate else _NullProvider(),
            k=k,
            today=today,
            **ask_kwargs,
        )

        # The SQL route returns rows, not ranked passages. Recall asks "did
        # the system surface the right notes", which is path-independent -- so
        # an enumerated row counts exactly like a retrieved chunk. Scoring only
        # `hits` made every correctly-routed SQL question look like a total
        # retrieval miss.
        retrieved = [h.codigo for h in res.hits] or [
            int(r["codigo"]) for r in res.rows if "codigo" in r
        ]
        # Dedupe while preserving rank: several chunks of one note count once.
        seen_order = list(dict.fromkeys(retrieved))
        gold = set(q.gold_codigos)
        found = gold & set(seen_order)

        # In retrieval-only mode the provider is a stub that always declines,
        # so `res.refused` would report ~100% over-refusal — an artefact of the
        # measurement, not a property of the system. What is meaningful without
        # a model is whether a *gate* refused (coverage, no hits, low score),
        # which is exactly what `refused_because` records.
        refused = bool(res.refused_because) if not generate else res.refused

        qr = QuestionResult(
            id=q.id,
            kind=q.kind,
            status="ok",
            expected_strategy=q.strategy,
            actual_strategy=res.plan.strategy,
            recall_at_k=len(found) / len(gold) if gold else -1.0,  # -1 = not scoreable
            hit=bool(found),
            refused=refused,
            refused_because=res.refused_because,
            citations=res.citations,
            gold=q.gold_codigos,
            retrieved=seen_order[:k],
        )
        if generate and res.answer and res.answer.sufficient:
            cited = set(res.citations)
            qr.citation_precision = len(cited & gold) / len(cited) if cited else 0.0
            qr.citation_recall = len(cited & gold) / len(gold) if gold else 0.0
        results.append(qr)

    return _summarise(results, k=k, provider=provider.name, generate=generate)


class _NullProvider:
    """Retrieval-only mode. Always refuses, so generation metrics stay at zero
    instead of being silently fabricated from a provider that never ran."""

    name = "none"

    def answer(self, question: str, context: str) -> Any:
        from .llm import Answer

        return Answer.refusal("generation disabled", "none")


def _summarise(
    results: list[QuestionResult], k: int, provider: str, generate: bool
) -> dict[str, Any]:
    scored = [r for r in results if r.status == "ok"]
    answerable = [r for r in scored if r.kind == "answerable"]
    unanswerable = [r for r in scored if r.kind == "unanswerable"]
    routed = [r for r in scored if r.expected_strategy]

    def mean(xs: list[float]) -> float:
        return round(statistics.fmean(xs), 4) if xs else 0.0

    # A question with no gold notes (a pure count, e.g. "how many did X
    # publish?") has nothing to recall. Averaging it in as 0.0 was silently
    # deflating recall by the share of aggregate questions in the set -- a
    # metric bug that punishes adding SQL coverage to the golden set.
    scoreable = [r for r in answerable if r.recall_at_k >= 0.0]
    metrics = {
        f"recall@{k}": mean([r.recall_at_k for r in scoreable]),
        f"hit_rate@{k}": mean([1.0 if r.hit else 0.0 for r in scoreable]),
        "routing_accuracy": mean(
            [1.0 if r.actual_strategy == r.expected_strategy else 0.0 for r in routed]
        ),
        # The pair that must always be read together.
        "refusal_rate_unanswerable": mean([1.0 if r.refused else 0.0 for r in unanswerable]),
        "over_refusal_rate_answerable": mean([1.0 if r.refused else 0.0 for r in answerable]),
    }
    if generate:
        answered = [r for r in answerable if not r.refused]
        metrics["citation_precision"] = mean([r.citation_precision for r in answered])
        metrics["citation_recall"] = mean([r.citation_recall for r in answered])
        p, rc = metrics["citation_precision"], metrics["citation_recall"]
        metrics["citation_f1"] = round(2 * p * rc / (p + rc), 4) if (p + rc) else 0.0

    return {
        "provider": provider,
        "generation": generate,
        "k": k,
        "counts": {
            "total": len(results),
            "scored": len(scored),
            "skipped_not_in_corpus": len(results) - len(scored),
            "answerable": len(answerable),
            "answerable_with_gold": len(scoreable),
            "unanswerable": len(unanswerable),
        },
        "metrics": metrics,
        "questions": [asdict(r) for r in results],
    }


# --------------------------------------------------------------------------


def render(report: dict[str, Any], verbose: bool = False) -> str:
    c, m = report["counts"], report["metrics"]
    lines = [
        "",
        f"  provider={report['provider']}  generation={report['generation']}  k={report['k']}",
        f"  {c['scored']}/{c['total']} preguntas evaluadas "
        f"({c['answerable']} respondibles, {c['unanswerable']} no respondibles)",
    ]
    if c["skipped_not_in_corpus"]:
        lines.append(
            f"  {c['skipped_not_in_corpus']} omitidas: su evidencia aún no está en el corpus"
        )
    lines.append("")
    width = max(len(x) for x in m)
    for key, value in m.items():
        bar = "█" * round(value * 30)
        lines.append(f"  {key:<{width}}  {value:>6.1%}  {bar}")
    lines.append("")

    if verbose:
        lines.append("  fallos:")
        for q in report["questions"]:
            bad = (
                q["status"] != "ok"
                or (q["kind"] == "answerable" and (not q["hit"] or q["refused"]))
                or (q["kind"] == "unanswerable" and not q["refused"])
                or (q["expected_strategy"] and q["expected_strategy"] != q["actual_strategy"])
            )
            if not bad:
                continue
            lines.append(
                f"    {q['id']:<6} {q['status']:<22} kind={q['kind']:<12} "
                f"route={q['actual_strategy'] or '-'}/{q['expected_strategy'] or '-'} "
                f"hit={q['hit']} refused={q['refused']}({q['refused_because'] or '-'})"
            )
            if q["gold"]:
                lines.append(f"           gold={q['gold']}  retrieved={q['retrieved'][:5]}")
        lines.append("")
    return "\n".join(lines)


def check_thresholds(report: dict[str, Any], thresholds: dict[str, float]) -> list[str]:
    """CI gate. Returns the list of violated thresholds (empty means pass)."""
    failures = []
    for key, floor in thresholds.items():
        actual = report["metrics"].get(key)
        if actual is None:
            continue
        # over_refusal is a ceiling, everything else is a floor.
        if key.startswith("over_refusal"):
            if actual > floor:
                failures.append(f"{key}={actual:.1%} exceeds ceiling {floor:.1%}")
        elif actual < floor:
            failures.append(f"{key}={actual:.1%} below floor {floor:.1%}")
    return failures
