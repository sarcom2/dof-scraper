"""End-to-end, against a stub server that replays the real captured pages.

The point of these tests is the *second* run: the same input must produce zero
writes. That is the claim the whole project is built around, so it is asserted
here rather than demonstrated once by hand in a terminal.
"""

from __future__ import annotations

import random
from datetime import date
from pathlib import Path

import httpx
import pytest

from dof_ingest.config import Settings
from dof_ingest.http import PoliteClient
from dof_ingest.pipeline import discover, enrich, index_url
from dof_ingest.store import Store
from tests.conftest import fixture_text

INDEX_BASE = "https://dof.gob.mx/index_111.php"
BODY_BASE = "https://sidof.segob.gob.mx/notas/docFuente"

PAGES = {
    ("2026-07-31", "MAT"): "index_2026-07-31_MAT.html",
    ("2026-07-31", "VES"): "index_2026-07-31_VES.html",
    ("2026-07-26", "MAT"): "index_2026-07-26_EMPTY.html",
}


def make_handler(calls: list[str]) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if request.url.path == "/robots.txt":
            # Map www.dof.gob.mx to the renamed fixture
            host = request.url.host
            if host == "www.dof.gob.mx":
                host = "dof.gob.mx"
            name = f"robots_{host}.txt"
            return httpx.Response(200, text=fixture_text(name))
        if request.url.path.startswith("/notas/docFuente/"):
            return httpx.Response(200, text=fixture_text("docFuente_5788395.html"))
        q = request.url.params
        day = f"{q['year']}-{int(q['month']):02d}-{int(q['day']):02d}"
        key = (day, q.get("edicion", "MAT"))
        if key in PAGES:
            return httpx.Response(200, text=fixture_text(PAGES[key]))
        # Every other edition: the site's 200-OK homepage, as in production.
        return httpx.Response(200, text=fixture_text("index_2026-07-26_EMPTY.html"))

    return handler


@pytest.fixture
def rig(tmp_path: Path):
    calls: list[str] = []
    settings = Settings(db_path=tmp_path / "t.sqlite3", requests_per_second=1000.0)
    client = PoliteClient(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(make_handler(calls))),
        sleep=lambda _: None,
        rng=random.Random(0),
    )
    with Store(settings.db_path) as store, client:
        yield store, client, settings, calls


def test_run_twice_writes_nothing_the_second_time(rig) -> None:
    store, client, settings, _ = rig
    day = date(2026, 7, 31)

    # recheck_days high enough that the second pass really re-fetches, which
    # is the harder claim: not "we skipped it", but "we re-read it and still
    # wrote nothing".
    first = discover(store, client, settings, day, day, ("MAT", "VES", "EXT"),
                     INDEX_BASE, recheck_days=3650)
    assert first.inserted == 31  # 27 matutina + 4 vespertina
    assert first.updated == 0
    assert first.no_edition == 1  # there was no extraordinaria that day

    second = discover(store, client, settings, day, day, ("MAT", "VES", "EXT"),
                      INDEX_BASE, recheck_days=3650)
    assert (second.inserted, second.updated) == (0, 0)
    assert second.unchanged == 31

    assert store.conn.execute("SELECT COUNT(*) FROM nota").fetchone()[0] == 31
    assert store.conn.execute("SELECT MAX(revision) FROM nota").fetchone()[0] == 1


def test_recheck_window_bounds_the_recrawl(rig) -> None:
    """Outside the window an already-recorded edition is not fetched again."""
    store, client, settings, calls = rig
    day = date(2026, 7, 31)
    discover(store, client, settings, day, day, ("MAT",), INDEX_BASE, recheck_days=0)
    before = len(calls)
    discover(store, client, settings, day, day, ("MAT",), INDEX_BASE, recheck_days=0)
    assert len(calls) == before  # zero additional requests


def test_missing_edition_is_recorded_and_not_confused_with_failure(rig) -> None:
    store, client, settings, _ = rig
    day = date(2026, 7, 26)
    counters = discover(store, client, settings, day, day, ("MAT",), INDEX_BASE)
    assert counters.no_edition == 1
    assert counters.errors == 0
    assert store.known_editions(["2026-07-26"]) == {("2026-07-26", "MAT"): "no_edition"}


def test_enrich_skips_notes_robots_disallows(rig) -> None:
    """The blocked notes return HTTP 200. We skip them anyway, and say so."""
    store, client, _settings, calls = rig
    from dof_ingest.models import Nota

    # 5381640 is one of the 18 IDs sidof names in robots.txt.
    for codigo in (5381640, 5795217):
        store.upsert_nota(
            Nota(codigo=codigo, fecha="2026-07-31", edicion="MAT", titulo="t",
                 organismo="o", url_detalle=f"https://sidof.segob.gob.mx/notas/{codigo}")
        )

    counters = enrich(store, client, limit=10, body_base=BODY_BASE)
    assert counters.skipped_robots == 1
    assert counters.inserted == 1

    rows = dict(store.conn.execute("SELECT codigo, body_status FROM nota"))
    assert rows[5381640] == "robots_denied"
    assert rows[5795217] == "ok"
    # And we never even asked the server for the blocked one.
    assert not any("docFuente/5381640" in c for c in calls)


def test_enrich_is_idempotent(rig) -> None:
    store, client, settings, _ = rig
    day = date(2026, 7, 31)
    discover(store, client, settings, day, day, ("VES",), INDEX_BASE)

    first = enrich(store, client, limit=10, body_base=BODY_BASE)
    assert first.inserted == 4
    # Nothing is pending any more, so a second pass is a no-op.
    second = enrich(store, client, limit=10, body_base=BODY_BASE)
    assert (second.inserted, second.updated, second.errors) == (0, 0, 0)


def test_index_url_always_names_the_edition() -> None:
    url = index_url(INDEX_BASE, date(2026, 7, 31), "MAT")
    assert "year=2026&month=07&day=31&edicion=MAT" in url


def test_layout_change_is_an_error_not_an_empty_day(rig, monkeypatch) -> None:
    """A ParseError must not write an edition row that suppresses future runs."""
    store, client, settings, _ = rig
    import dof_ingest.pipeline as pipe

    def broken(html: str, *a: object, **kw: object):
        from dof_ingest.parse import ParseError

        raise ParseError("markup changed")

    monkeypatch.setattr(pipe, "parse_index", broken)
    day = date(2026, 7, 31)
    counters = discover(store, client, settings, day, day, ("MAT",), INDEX_BASE)
    assert counters.errors == 1
    assert store.known_editions(["2026-07-31"]) == {}
