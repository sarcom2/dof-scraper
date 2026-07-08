"""Tests for the QA layer.

Offline and deterministic: the fixture corpus is built in-memory from the same
captured notes the ingestion tests use, and no test calls a model.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from dof_ingest.models import Nota
from dof_ingest.store import Store
from dof_qa.answer import ask, build_context
from dof_qa.evaluate import GoldenQuestion, evaluate, load_golden
from dof_qa.index import ChunkConfig, build, fold, split_text
from dof_qa.llm import Answer, ExtractiveProvider
from dof_qa.retrieve import Hit, coverage, fts_query, retrieve, rrf_fuse
from dof_qa.route import extract_agencies, extract_dates, plan

TODAY = date(2026, 8, 5)

CORPUS = [
    (5795170, "SECRETARIA DE ECONOMIA",
     "Aviso mediante el cual se da a conocer el monto del cupo máximo para exportar azúcar "
     "a los Estados Unidos de América, del ciclo azucarero 2026-2027.",
     "AVISO mediante el cual se da a conocer el monto del cupo máximo para exportar azúcar. "
     "El cupo máximo asciende a 1,200,000 toneladas métricas valor crudo. "
     + "Relleno legal. " * 60),
    (5795172, "SECRETARIA DE AGRICULTURA Y DESARROLLO RURAL",
     "Acuerdo por el que se modifica el similar que establece veda temporal para la pesca "
     "comercial de atún aleta amarilla.",
     "ACUERDO sobre veda temporal para la pesca comercial de atún aleta amarilla "
     "(Thunnus albacares) y barrilete en el Océano Pacífico. " + "Considerando. " * 60),
    (5795190, "BANCO DE MEXICO",
     "Tipo de cambio para solventar obligaciones denominadas en moneda extranjera.",
     "TIPO de cambio para solventar obligaciones en moneda extranjera. " + "Texto. " * 40),
    (5795184, "COMISION FEDERAL DE ELECTRICIDAD",
     "Tarifas Finales del Suministro Básico que deberá aplicar la Comisión Federal de "
     "Electricidad a sus usuarios.",
     "TARIFAS finales del suministro básico aplicables a usuarios domésticos. " + "Tarifa. " * 60),
]


@pytest.fixture
def db(tmp_path):
    with Store(tmp_path / "qa.sqlite3") as store:
        for codigo, organismo, titulo, body in CORPUS:
            store.upsert_nota(
                Nota(codigo=codigo, fecha="2026-07-31", edicion="MAT", titulo=titulo,
                     organismo=organismo, poder="PODER EJECUTIVO", seccion="UNICA SECCION",
                     url_detalle=f"https://sidof.segob.gob.mx/notas/{codigo}")
            )
            store.set_body(codigo, "ok", body)
        build(store.conn)
        yield store.conn


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------


def test_legal_markers_are_preferred_split_points() -> None:
    text = (
        "CONSIDERANDO que procede lo siguiente y demás razones expuestas.\n"
        "ARTÍCULO PRIMERO. Se establece la medida indicada en el presente acuerdo.\n"
        "ARTÍCULO SEGUNDO. Se deroga la disposición anterior en todos sus términos.\n"
        "TRANSITORIOS. El presente acuerdo entra en vigor al día siguiente."
    )
    pieces = split_text(text, ChunkConfig(chunk_chars=200, overlap=20))
    assert len(pieces) >= 3
    # Each article stays whole — splitting mid-article would file text under the
    # wrong article number, which in a legal corpus is a correctness bug.
    assert any(p[2].startswith("ARTÍCULO PRIMERO") for p in pieces)
    assert any(p[2].startswith("ARTÍCULO SEGUNDO") for p in pieces)


def test_unbroken_prose_still_gets_chunked() -> None:
    """DOF notes routinely arrive as one 50 KB paragraph."""
    text = "palabra " * 4000
    pieces = split_text(text, ChunkConfig(chunk_chars=1200, overlap=150))
    assert len(pieces) > 10
    assert all(len(p[2]) <= 1200 for p in pieces)
    # Overlap only applies to the hard-slice tier, and must actually overlap.
    assert pieces[1][0] < pieces[0][1]


def test_short_documents_survive() -> None:
    assert split_text("Texto muy breve pero real.", ChunkConfig())[0][2].startswith("Texto")
    assert split_text("", ChunkConfig()) == []


def test_index_is_idempotent(db: sqlite3.Connection) -> None:
    first = build(db)
    assert first["unchanged"] == len(CORPUS)
    assert first["indexed"] == 0
    assert first["chunks"] == 0


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------


def test_counting_questions_route_to_sql(db: sqlite3.Connection) -> None:
    cov = coverage(db)
    p = plan("¿Cuántas notas publicó el Banco de México en julio de 2026?", cov.organismos, TODAY)
    assert p.strategy == "sql"
    assert p.wants_count
    # Temporal and aggregate words are consumed by their own extractors and
    # must not leak into `terms` — that is what made counting questions look
    # topical and route to hybrid.
    assert p.terms == []


def test_topical_questions_route_to_retrieval(db: sqlite3.Connection) -> None:
    p = plan("¿Qué se publicó sobre la veda de atún?", coverage(db).organismos, TODAY)
    assert p.strategy == "retrieval"
    assert "atun" in p.terms


def test_agency_plus_topic_routes_to_hybrid(db: sqlite3.Connection) -> None:
    p = plan("¿Qué publicó la CFE sobre tarifas este trimestre?", coverage(db).organismos, TODAY)
    assert p.strategy == "hybrid"
    assert p.filters.organismos and "electricidad" in p.filters.organismos[0]
    assert p.filters.fecha_desde == "2026-07-01"  # Q3 of the pinned `today`
    assert p.filters.fecha_hasta == "2026-09-30"


def test_relative_dates_are_pinned_not_read_from_the_clock() -> None:
    assert extract_dates("este trimestre", date(2026, 2, 10)) == ("2026-01-01", "2026-03-31")
    assert extract_dates("este trimestre", date(2026, 8, 5)) == ("2026-07-01", "2026-09-30")
    assert extract_dates("en julio de 2026", TODAY) == ("2026-07-01", "2026-07-31")
    assert extract_dates("el 31 de julio de 2026", TODAY) == ("2026-07-31", "2026-07-31")
    assert extract_dates("segundo trimestre de 2025", TODAY) == ("2025-04-01", "2025-06-30")
    assert extract_dates("sin fecha alguna", TODAY) == (None, None)


def test_acronyms_resolve_to_corpus_agency_names(db: sqlite3.Connection) -> None:
    orgs = coverage(db).organismos
    assert any("electricidad" in a for a in extract_agencies("¿qué publicó la CFE?", orgs))
    # An agency that exists in Mexico but not in this corpus still produces a
    # filter, so the coverage gate can refuse for the right reason.
    found = extract_agencies("¿qué publicó la Secretaría de Salud?", orgs)
    assert found and "salud" in found[0]
    # A non-agency acronym in the text must not be mistaken for one.
    assert extract_agencies("¿qué convenio hay sobre la CURP?", orgs) == []


# --------------------------------------------------------------------------
# retrieval
# --------------------------------------------------------------------------


def test_fts_query_neutralises_match_syntax() -> None:
    """User text flows into FTS5 MATCH, which has its own syntax."""
    for hostile in ['" OR "', "NEAR(a b)", "col:value", "*", "a-b", "'; DROP"]:
        q = fts_query([hostile])
        # Every emitted token is quoted, so nothing can escape into syntax.
        assert all(p.startswith('"') for p in q.split(" OR ") if p)


def test_prefix_stemming_bridges_spanish_morphology(db: sqlite3.Connection) -> None:
    """"exportaciones" must reach a note that only says "exportar"."""
    p = plan("¿Qué publicó la Secretaría de Economía sobre exportaciones?",
             coverage(db).organismos, TODAY)
    with_prefix = retrieve(db, p, k=5, use_prefix=True)
    without = retrieve(db, p, k=5, use_prefix=False)
    assert any(h.codigo == 5795170 for h in with_prefix)
    assert not without  # documents the gap the prefix trick closes


def test_rrf_scores_are_not_bm25_scores() -> None:
    """Regression pin for a bug that refused correctly-ranked results.

    RRF returns ~1/(60+rank) ≈ 0.016 while BM25 runs 0-45. Thresholding the
    fused score meant that fusing a second query formulation silently rejected
    everything, with the right document sitting at rank 1.
    """
    def hit(cid: int, score: float) -> Hit:
        return Hit(cid, cid, 0, score, score, "t", "T", "O", "2026-07-31", "MAT", "u")

    fused = rrf_fuse([[hit(1, 40.0), hit(2, 30.0)], [hit(2, 20.0), hit(1, 10.0)]], k=2)
    assert fused[0].score < 0.1          # fusion score: small by construction
    assert fused[0].bm25 > 1.0           # BM25 survives the fusion intact
    assert {h.codigo for h in fused} == {1, 2}


def test_structured_prefilter_narrows_candidates(db: sqlite3.Connection) -> None:
    p = plan("¿Qué publicó la CFE sobre tarifas?", coverage(db).organismos, TODAY)
    filtered = retrieve(db, p, k=10, use_prefilter=True)
    assert filtered and all(h.codigo == 5795184 for h in filtered)


def test_per_note_cap_preserves_citation_breadth(db: sqlite3.Connection) -> None:
    p = plan("acuerdo aviso texto", coverage(db).organismos, TODAY)
    hits = retrieve(db, p, k=10, per_nota=2)
    from collections import Counter

    assert max(Counter(h.codigo for h in hits).values(), default=0) <= 2


# --------------------------------------------------------------------------
# answering and refusal
# --------------------------------------------------------------------------


def test_refuses_specifically_when_agency_is_outside_the_corpus(db: sqlite3.Connection) -> None:
    res = ask(db, "¿Qué publicó COFEPRIS sobre dispositivos médicos este trimestre?",
              ExtractiveProvider(), today=TODAY)
    assert res.refused
    assert res.refused_because == "out_of_coverage"
    # The refusal names the limitation instead of saying "no sé".
    assert "organismo" in res.answer.answer.lower()


def test_refuses_specifically_when_dates_are_outside_the_corpus(db: sqlite3.Connection) -> None:
    res = ask(db, "¿Qué decretos se publicaron en enero de 2020?",
              ExtractiveProvider(), today=TODAY)
    assert res.refused_because == "out_of_coverage"
    assert "2026-07-31" in res.answer.answer  # states what it does cover


def test_answers_with_citations(db: sqlite3.Connection) -> None:
    res = ask(db, "¿Cuál es el cupo máximo para exportar azúcar?",
              ExtractiveProvider(), today=TODAY)
    assert not res.refused
    assert 5795170 in res.citations


def test_fabricated_citations_are_stripped(db: sqlite3.Connection) -> None:
    """A citation to a note the model was never shown is a fabrication."""

    class Liar:
        name = "liar"

        def answer(self, question: str, context: str) -> Answer:
            return Answer(sufficient=True, answer="Inventado.",
                          citations=[9999999], provider="liar")

    res = ask(db, "¿Qué se publicó sobre la veda de atún?", Liar(), today=TODAY)
    assert res.refused
    assert res.refused_because == "fabricated_citations"


def test_partially_fabricated_citations_keep_the_real_ones(db: sqlite3.Connection) -> None:
    class HalfLiar:
        name = "half"

        def answer(self, question: str, context: str) -> Answer:
            return Answer(sufficient=True, answer="Mitad.",
                          citations=[5795172, 9999999], provider="half")

    res = ask(db, "¿Qué se publicó sobre la veda de atún?", HalfLiar(), today=TODAY)
    assert res.citations == [5795172]
    assert not res.refused


def test_malformed_provider_output_degrades_to_refusal() -> None:
    assert Answer.from_payload({"nonsense": 1}, "x").sufficient is False


def test_sql_route_counts_without_a_model(db: sqlite3.Connection) -> None:
    res = ask(db, "¿Cuántas notas publicó el Banco de México?", ExtractiveProvider(), today=TODAY)
    assert not res.refused
    assert res.answer.provider == "sql"
    assert "1 notas" in res.answer.answer


def test_context_labels_every_passage_with_its_code(db: sqlite3.Connection) -> None:
    res = ask(db, "¿Qué se publicó sobre la veda de atún?", ExtractiveProvider(), today=TODAY)
    ctx = build_context(res.hits)
    for hit in res.hits:
        assert f"[nota codigo={hit.codigo}]" in ctx


# --------------------------------------------------------------------------
# the harness itself
# --------------------------------------------------------------------------


def test_questions_without_evidence_are_skipped_not_failed(db: sqlite3.Connection) -> None:
    golden = [
        GoldenQuestion(id="x", question="¿veda de atún?", kind="answerable",
                       gold_codigos=[1234567], today="2026-08-05"),
    ]
    report = evaluate(db, golden, ExtractiveProvider(), generate=False)
    assert report["counts"]["skipped_not_in_corpus"] == 1
    assert report["questions"][0]["status"] == "skipped_not_in_corpus"


def test_aggregate_questions_do_not_deflate_recall(db: sqlite3.Connection) -> None:
    """A pure count has nothing to recall; averaging it in as 0.0 is a bug."""
    golden = [
        GoldenQuestion(id="a", question="¿Qué se publicó sobre la veda de atún?",
                       kind="answerable", gold_codigos=[5795172], today="2026-08-05"),
        GoldenQuestion(id="b", question="¿Cuántas notas publicó el Banco de México?",
                       kind="answerable", gold_codigos=[], today="2026-08-05"),
    ]
    report = evaluate(db, golden, ExtractiveProvider(), generate=False)
    assert report["counts"]["answerable"] == 2
    assert report["counts"]["answerable_with_gold"] == 1
    assert report["metrics"]["recall@8"] == 1.0


def test_over_refusal_is_reported_alongside_refusal(db: sqlite3.Connection) -> None:
    """Refusing everything must not look like a perfect score."""
    golden = [
        GoldenQuestion(id="a", question="¿Qué publicó COFEPRIS?", kind="unanswerable",
                       today="2026-08-05"),
        GoldenQuestion(id="b", question="¿Qué se publicó sobre la veda de atún?",
                       kind="answerable", gold_codigos=[5795172], today="2026-08-05"),
    ]
    report = evaluate(db, golden, ExtractiveProvider(), generate=False)
    assert report["metrics"]["refusal_rate_unanswerable"] == 1.0
    assert report["metrics"]["over_refusal_rate_answerable"] == 0.0


# --------------------------------------------------------------------------
# the golden set is itself an artifact under test
# --------------------------------------------------------------------------


def test_golden_set_is_wellformed() -> None:
    from pathlib import Path

    golden = load_golden(Path("eval/golden.jsonl"))
    assert len(golden) >= 40, "the set should be 40-60 questions"
    assert all(q.kind in {"answerable", "unanswerable"} for q in golden)
    assert all(q.today for q in golden), "every question must pin its date"
    # A meaningful share must be deliberately unanswerable, or the refusal
    # metrics are measured on too few points to mean anything.
    unanswerable = [q for q in golden if q.kind == "unanswerable"]
    assert len(unanswerable) >= 10
    assert all(not q.gold_codigos for q in unanswerable)
    assert all(q.note for q in golden), "every question needs a stated reason to exist"


def test_fold_matches_the_tokeniser() -> None:
    assert fold("Comisión Federal") == "comision federal"
    assert fold("ATÚN") == "atun"


def test_index_writes_survive_the_connection(tmp_path) -> None:
    """Regression pin: the index must actually reach the file.

    Python's sqlite3 defaults to `isolation_level=""`, which wraps DML in an
    implicit transaction. `executescript` commits the DDL, so the tables
    appeared while every chunk INSERT was discarded on close -- and `build()`
    still reported a healthy `indexed=27 chunks=504`, because within the
    process the rows were there. Only a second connection could see the truth.
    """
    import sqlite3 as _sqlite3

    path = tmp_path / "durable.sqlite3"
    with Store(path) as store:
        codigo, organismo, titulo, body = CORPUS[0]
        store.upsert_nota(
            Nota(codigo=codigo, fecha="2026-07-31", edicion="MAT", titulo=titulo,
                 organismo=organismo, url_detalle="u")
        )
        store.set_body(codigo, "ok", body)

    # A separate connection with the default isolation level, exactly as the
    # CLI used to open it.
    conn = _sqlite3.connect(path, isolation_level=None)
    conn.row_factory = _sqlite3.Row
    counters = build(conn)
    assert counters["chunks"] > 0
    conn.close()

    verify = _sqlite3.connect(path)
    assert verify.execute("SELECT COUNT(*) FROM chunk").fetchone()[0] == counters["chunks"]
    verify.close()
