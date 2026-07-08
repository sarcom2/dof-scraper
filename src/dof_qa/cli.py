"""`dof-qa` — index, ask, evaluate, ablate."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any

from .answer import ask
from .evaluate import check_thresholds, evaluate, load_golden, render
from .index import ChunkConfig, build, drop_index
from .llm import get_provider
from .retrieve import coverage

DEFAULT_DB = Path("data/dof.sqlite3")
DEFAULT_GOLDEN = Path("eval/golden.jsonl")

# CI gate. Deliberately set at the level the system currently clears, not at an
# aspirational one: a threshold nobody meets gets disabled within a week, and a
# disabled gate is worse than none.
THRESHOLDS = {
    "recall@8": 0.90,
    "hit_rate@8": 0.90,
    "routing_accuracy": 0.95,
    # Retrieval-only (the CI mode) can only refuse at the *gates* — coverage,
    # no hits, low score. Catching "right agency, wrong case number" needs a
    # model, so the CI floor is set to what gates can actually achieve. Raising
    # it here would just mean disabling the check the first time it fired.
    "refusal_rate_unanswerable": 0.50,
    "over_refusal_rate_answerable": 0.10,  # ceiling, not a floor
}


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"{path} not found — run `dof-ingest run` first to build a corpus.")
    # isolation_level=None (autocommit), matching dof_ingest.store.Store.
    #
    # This is not a style choice. Python's sqlite3 defaults to
    # isolation_level="", which opens an implicit transaction before DML and
    # requires an explicit commit(). `executescript` (used for the DDL) issues
    # its own COMMIT first, so the schema landed while every INSERT after it
    # was silently rolled back on close. `build()` cheerfully reported
    # "indexed=27 chunks=504" -- true within the process, gone from the file.
    # The next process found an empty index and the eval scored 6.7%.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_index(args: argparse.Namespace) -> int:
    conn = _connect(args.db)
    cfg = ChunkConfig(chunk_chars=args.chunk_chars, overlap=args.overlap)
    if args.rebuild:
        drop_index(conn)
    counters = build(conn, cfg, force=args.rebuild)
    cov = coverage(conn)
    print(
        f"\n  indexed={counters['indexed']} unchanged={counters['unchanged']} "
        f"chunks={counters['chunks']} sin_texto={counters['skipped_no_text']}"
    )
    print(f"  corpus: {cov.n_notas} notas ({cov.n_con_texto} con texto), "
          f"{len(cov.organismos)} organismos, {cov.fecha_min}..{cov.fecha_max}\n")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    conn = _connect(args.db)
    provider = get_provider(args.provider)
    today = date.fromisoformat(args.today) if args.today else None
    result = ask(conn, args.question, provider, k=args.k, today=today)

    if args.json:
        print(json.dumps(
            {
                "question": result.question,
                "strategy": result.plan.strategy,
                "sufficient": result.answer.sufficient if result.answer else False,
                "answer": result.answer.answer if result.answer else "",
                "citations": result.citations,
                "retrieved": [h.codigo for h in result.hits],
                "refused_because": result.refused_because,
            },
            ensure_ascii=False, indent=2,
        ))
        return 0 if not result.refused else 1

    print(f"\n  {result.plan.describe()}\n")
    ans = result.answer
    if ans is None or not ans.sufficient:
        print(f"  NO SÉ — {ans.answer if ans else 'sin respuesta'}\n")
        return 1
    print(f"  {ans.answer}\n")
    if ans.citations:
        print("  Fuentes:")
        by_code = {h.codigo: h for h in result.hits}
        for code in ans.citations:
            hit = by_code.get(code)
            if hit:
                print(f"    [{code}] {hit.fecha} {hit.organismo} — {hit.titulo[:80]}")
                print(f"           {hit.url}")
            else:
                print(f"    [{code}]")
        print()
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    conn = _connect(args.db)
    golden = load_golden(args.golden)
    provider = get_provider(args.provider)
    report = evaluate(
        conn, golden, provider, k=args.k,
        generate=not args.no_generate,
        use_expansion=not args.no_expansion,
        use_prefilter=not args.no_prefilter,
        use_prefix=not args.no_prefix,
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        print(f"  report -> {args.out}")
    print(render(report, verbose=args.verbose))

    if args.check:
        failures = check_thresholds(report, THRESHOLDS)
        if failures:
            print("  UMBRALES NO ALCANZADOS:")
            for f in failures:
                print(f"    ✗ {f}")
            print()
            return 1
        print("  todos los umbrales alcanzados\n")
    return 0


def cmd_ablate(args: argparse.Namespace) -> int:
    """Measure what actually moves recall, including what didn't.

    Each row re-indexes or re-configures one thing and re-runs the golden set.
    Retrieval-only by default, so the whole sweep is deterministic and free.
    """
    conn = _connect(args.db)
    golden = load_golden(args.golden)
    provider = get_provider("extractive")

    variants: list[tuple[str, dict[str, Any], ChunkConfig | None]] = [
        ("baseline (todo activado)", {}, None),
        ("sin expansión de acrónimos", {"use_expansion": False}, None),
        ("sin pre-filtro estructurado", {"use_prefilter": False}, None),
        ("sin ninguno de los dos", {"use_expansion": False, "use_prefilter": False}, None),
        ("chunks 600 (overlap 80)", {}, ChunkConfig(chunk_chars=600, overlap=80)),
        ("chunks 2400 (overlap 300)", {}, ChunkConfig(chunk_chars=2400, overlap=300)),
        ("sin prefijo-stemming", {"use_prefix": False}, None),
        ("sin plegado de acentos", {}, ChunkConfig(remove_diacritics=False)),
    ]

    rows: list[tuple[str, float, float, float, float]] = []
    for label, kwargs, cfg in variants:
        if cfg is not None:
            drop_index(conn)
            build(conn, cfg, force=True)
        elif rows:  # restore the default index after a chunking variant
            drop_index(conn)
            build(conn, ChunkConfig(), force=True)
        report = evaluate(conn, golden, provider, k=args.k, generate=False, **kwargs)
        m = report["metrics"]
        rows.append((label, m[f"recall@{args.k}"], m[f"hit_rate@{args.k}"],
                     m["refusal_rate_unanswerable"], m["over_refusal_rate_answerable"]))
        print(f"  · {label}: recall={m[f'recall@{args.k}']:.1%}")

    # Leave the store on the default configuration.
    drop_index(conn)
    build(conn, ChunkConfig(), force=True)

    base = rows[0][1]
    print(f"\n  {'variante':<32} {'recall':>8} {'Δ':>8} {'hit':>7} {'refusal':>8} {'over-ref':>9}")
    print(f"  {'-' * 32} {'-' * 8} {'-' * 8} {'-' * 7} {'-' * 8} {'-' * 9}")
    for label, recall, hit, refusal, over in rows:
        delta = "" if label.startswith("baseline") else f"{(recall - base) * 100:+.1f}pp"
        print(f"  {label:<32} {recall:>7.1%} {delta:>8} {hit:>6.1%} {refusal:>7.1%} {over:>8.1%}")
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dof-qa",
        description="Grounded question answering over the DOF corpus, with a real eval harness.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("index", help="build the chunk + FTS5 index (idempotent)")
    i.add_argument("--chunk-chars", type=int, default=1200)
    i.add_argument("--overlap", type=int, default=150)
    i.add_argument("--rebuild", action="store_true", help="drop and rebuild from scratch")
    i.set_defaults(func=cmd_index)

    a = sub.add_parser("ask", help="answer one question with citations, or refuse")
    a.add_argument("question")
    a.add_argument("--provider", help="extractive | ollama[:model] | anthropic[:model]")
    a.add_argument("--k", type=int, default=8)
    a.add_argument("--today", help="pin relative dates, YYYY-MM-DD")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_ask)

    e = sub.add_parser("eval", help="run the golden set")
    e.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    e.add_argument("--provider", help="default: extractive")
    e.add_argument("--k", type=int, default=8)
    e.add_argument("--no-generate", action="store_true", help="retrieval metrics only (CI mode)")
    e.add_argument("--no-expansion", action="store_true")
    e.add_argument("--no-prefilter", action="store_true")
    e.add_argument("--no-prefix", action="store_true", help="disable prefix stemming")
    e.add_argument("--out", type=Path, help="write the JSON report here")
    e.add_argument("--check", action="store_true", help="exit 1 if a threshold is missed")
    e.set_defaults(func=cmd_eval)

    b = sub.add_parser("ablate", help="what actually moved recall, and what didn't")
    b.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    b.add_argument("--k", type=int, default=8)
    b.set_defaults(func=cmd_ablate)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(message)s",
    )
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
