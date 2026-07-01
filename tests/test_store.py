"""Idempotency and content hashing, asserted rather than claimed."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

import pytest

from dof_ingest.models import Nota, content_hash
from dof_ingest.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    with Store(tmp_path / "t.sqlite3") as s:
        yield s


def make_nota(**kw: object) -> Nota:
    base = Nota(
        codigo=5795217,
        fecha="2026-07-31",
        edicion="VES",
        titulo="Acuerdo por el que se dan a conocer los porcentajes",
        organismo="SECRETARIA DE HACIENDA Y CREDITO PUBLICO",
        poder="PODER EJECUTIVO",
        seccion="UNICA SECCION",
        url_detalle="https://sidof.segob.gob.mx/notas/5795217",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# the contract
# --------------------------------------------------------------------------


def test_running_twice_inserts_once(store: Store) -> None:
    nota = make_nota()
    assert store.upsert_nota(nota) == "inserted"
    assert store.upsert_nota(nota) == "unchanged"
    assert store.upsert_nota(nota) == "unchanged"
    assert store.conn.execute("SELECT COUNT(*) FROM nota").fetchone()[0] == 1


def test_unchanged_moves_last_seen_but_not_last_changed(store: Store) -> None:
    """The distinction that turns 'no duplicates' into a measurement."""
    nota = make_nota()
    store.upsert_nota(nota, now="2026-08-01T00:00:00+00:00")
    store.upsert_nota(nota, now="2026-08-02T00:00:00+00:00")

    row = store.conn.execute("SELECT * FROM nota").fetchone()
    assert row["last_seen_at"] == "2026-08-02T00:00:00+00:00"
    assert row["last_changed_at"] == "2026-08-01T00:00:00+00:00"
    assert row["revision"] == 1


def test_a_real_change_bumps_revision_and_records_the_diff(store: Store) -> None:
    store.upsert_nota(make_nota(), now="2026-08-01T00:00:00+00:00")
    amended = make_nota(titulo="Acuerdo ... (fe de erratas)")
    assert store.upsert_nota(amended, now="2026-08-05T00:00:00+00:00") == "updated"

    row = store.conn.execute("SELECT * FROM nota").fetchone()
    assert row["revision"] == 2
    assert row["last_changed_at"] == "2026-08-05T00:00:00+00:00"

    diffs = store.conn.execute("SELECT * FROM nota_revision").fetchall()
    assert [d["field"] for d in diffs] == ["titulo"]
    assert diffs[0]["old_value"].endswith("porcentajes")
    assert diffs[0]["new_value"].endswith("(fe de erratas)")


def test_cosmetic_edits_are_not_changes(store: Store) -> None:
    """Whitespace and unicode normalisation happen before hashing.

    The DOF re-indents its HTML at random and mixes precomposed with
    decomposed accents. Neither is a change to the nota.
    """
    store.upsert_nota(make_nota())
    reflowed = make_nota(titulo="Acuerdo  por el que\n se dan a conocer los  porcentajes")
    assert store.upsert_nota(reflowed) == "unchanged"

    decomposed = make_nota(organismo="SECRETARÍA DE HACIENDA".replace("Í", "Í"))
    precomposed = make_nota(organismo="SECRETARÍA DE HACIENDA")
    assert decomposed.hash() == precomposed.hash()


def test_url_origen_is_outside_the_hash() -> None:
    """Rediscovering a nota through another index URL is not a content change."""
    a = make_nota(url_origen="https://www.dof.gob.mx/index_111.php?x=1")
    b = make_nota(url_origen="https://www.dof.gob.mx/index_111.php?x=2")
    assert a.hash() == b.hash()


def test_hash_is_order_independent_and_stable() -> None:
    assert content_hash({"a": "1", "b": "2"}) == content_hash({"b": "2", "a": "1"})
    # Pinned so a refactor of the canonicalisation cannot silently invalidate
    # every stored hash and trigger a full re-ingest that looks like real churn.
    assert content_hash({"titulo": "  Acuerdo   x "}) == content_hash({"titulo": "Acuerdo x"})


# --------------------------------------------------------------------------
# bodies
# --------------------------------------------------------------------------


def test_body_has_its_own_change_signal(store: Store) -> None:
    store.upsert_nota(make_nota())
    assert store.set_body(5795217, "ok", "texto original") == "inserted"
    assert store.set_body(5795217, "ok", "texto original") == "unchanged"
    assert store.set_body(5795217, "ok", "texto corregido") == "updated"

    row = store.conn.execute("SELECT * FROM nota").fetchone()
    assert row["body_status"] == "ok"
    assert row["body_hash"] == content_hash({"body": "texto corregido"})


def test_robots_denied_is_recorded_not_dropped(store: Store) -> None:
    store.upsert_nota(make_nota())
    store.set_body(5795217, "robots_denied")
    assert store.stats()["notas_bloqueadas_por_robots"] == 1
    # ...and it is not handed out again as pending work on the next run.
    assert store.pending_bodies(10) == []


def test_pending_queue_skips_finished_work(store: Store) -> None:
    store.upsert_nota(make_nota())
    store.upsert_nota(make_nota(codigo=5795218))
    assert len(store.pending_bodies(10)) == 2
    store.set_body(5795217, "ok", "x")
    store.set_body(5795218, "error")
    assert len(store.pending_bodies(10)) == 0
    assert len(store.pending_bodies(10, retry_errors=True)) == 1


# --------------------------------------------------------------------------
# editions & export
# --------------------------------------------------------------------------


def test_edition_change_detection(store: Store) -> None:
    assert store.record_edition("2026-07-31", "VES", "ok", 4, 2, "h1") is True
    assert store.record_edition("2026-07-31", "VES", "ok", 4, 2, "h1") is False
    assert store.record_edition("2026-07-31", "VES", "ok", 5, 2, "h2") is True


def test_no_edition_is_a_recorded_state(store: Store) -> None:
    store.record_edition("2026-07-26", "MAT", "no_edition", 0, 0, None)
    assert store.known_editions(["2026-07-26"]) == {("2026-07-26", "MAT"): "no_edition"}
    assert store.stats()["ediciones_sin_publicacion"] == 1


def test_export_round_trips(store: Store) -> None:
    store.upsert_nota(make_nota())
    store.set_body(5795217, "ok", "cuerpo")

    buf = io.StringIO()
    assert store.export(buf, "jsonl", include_body=True) == 1
    import json

    row = json.loads(buf.getvalue())
    assert row["codigo"] == 5795217
    assert row["body_text"] == "cuerpo"
    assert row["organismo"] == "SECRETARIA DE HACIENDA Y CREDITO PUBLICO"

    buf = io.StringIO()
    store.export(buf, "csv")
    assert buf.getvalue().splitlines()[0].startswith("codigo,fecha,edicion")


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    with Store(path) as s:
        s.conn.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError, match="newer schema"):
        Store(path)
