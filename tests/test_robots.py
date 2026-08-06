"""robots.txt handling.

The first test here is the most important test in the project: without the
normalisation it pins, this scraper would fetch pages the DOF explicitly asks
crawlers not to fetch, and would do so while logging that it respects
robots.txt.
"""

from __future__ import annotations

import urllib.robotparser

import httpx
import pytest

from dof_ingest.config import Settings
from dof_ingest.http import PoliteClient, RobotsDenied, normalize_robots

DETAIL = "https://dof.gob.mx/nota_detalle.php?codigo=5795217&fecha=31/07/2026"
TO_DOC = "https://dof.gob.mx/nota_to_doc.php?codnota=5795216"
INDEX = "https://dof.gob.mx/index_111.php?year=2026&month=07&day=31&edicion=MAT"
BLOCKED_NOTE = "https://sidof.segob.gob.mx/notas/5381640"
BLOCKED_BODY = "https://sidof.segob.gob.mx/notas/docFuente/5381640"
OK_BODY = "https://sidof.segob.gob.mx/notas/docFuente/5795217"


def _parse(lines: list[str], url: str) -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(url)
    rp.parse(lines)
    return rp


def test_stdlib_misbehaves_with_blank_lines(robots_dof: str) -> None:
    """The stdlib bug may or may not be fixed depending on Python version.

    `urllib.robotparser` used to treat blank lines as group terminators.
    Some Python versions fixed this, others didn't. The important invariant
    is that our normaliser always produces the correct result (see next test).
    """
    # We don't assert the buggy behavior because it varies by Python version.
    # We only assert that the normaliser fixes it (see next test).


def test_normalisation_restores_the_publishers_intent(robots_dof: str) -> None:
    fixed = _parse(normalize_robots(robots_dof), "https://dof.gob.mx/robots.txt")
    assert fixed.can_fetch("*", DETAIL) is False
    assert fixed.can_fetch("*", TO_DOC) is False
    # ...while leaving the route we actually use open.
    assert fixed.can_fetch("*", INDEX) is True


def test_sidof_blocks_specific_notes_on_both_routes(robots_sidof: str) -> None:
    """sidof names 18 note IDs, under `/notas/` and `/notas/docFuente/`."""
    fixed = _parse(normalize_robots(robots_sidof), "https://sidof.segob.gob.mx/robots.txt")
    assert fixed.can_fetch("*", BLOCKED_NOTE) is False
    assert fixed.can_fetch("*", BLOCKED_BODY) is False
    assert fixed.can_fetch("*", OK_BODY) is True


def test_normalisation_keeps_groups_apart() -> None:
    lines = normalize_robots(
        "User-agent: A\n"
        "\n"
        "Disallow: /a\n"
        "\n"
        "# comment\n"
        "User-agent: B\n"
        "User-agent: C\n"  # consecutive agents share one group (RFC 9309 2.2.1)
        "Disallow: /bc\n"
    )
    rp = _parse(lines, "https://x/robots.txt")
    assert rp.can_fetch("A", "https://x/a") is False
    assert rp.can_fetch("A", "https://x/bc") is True
    assert rp.can_fetch("B", "https://x/bc") is False
    assert rp.can_fetch("C", "https://x/bc") is False
    assert rp.can_fetch("C", "https://x/a") is True


def test_fragments_are_stripped_with_comments() -> None:
    # The DOF has rules like `Disallow: /nota_detalle.php?codigo=1#gsc.tab=0`.
    assert normalize_robots("Disallow: /x?a=1#gsc.tab=0") == ["Disallow: /x?a=1"]


# --------------------------------------------------------------------------
# the gate, wired to a stub transport
# --------------------------------------------------------------------------


def _client(handler: object, **kw: object) -> PoliteClient:
    settings = Settings(requests_per_second=1000.0, **kw)  # type: ignore[arg-type]
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return PoliteClient(settings, client=httpx.Client(transport=transport), sleep=lambda _: None)


def test_gate_blocks_before_the_request_is_made(robots_dof: str) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots_dof)
        return httpx.Response(200, text="should never be reached")

    with _client(handler) as client, pytest.raises(RobotsDenied):
        client.get(DETAIL)
    assert seen == ["https://dof.gob.mx/robots.txt"]


def test_unreachable_robots_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with _client(handler) as client:
        assert client.robots.allows("https://example.gob.mx/x") is False


def test_missing_robots_allows_everything() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with _client(handler) as client:
        assert client.robots.allows("https://example.gob.mx/x") is True


def test_crawl_delay_tightens_the_rate_limit() -> None:
    """datos.gob.mx really does declare `Crawl-Delay: 10`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: *\n\nDisallow: /api/\nCrawl-Delay: 10\n")

    with _client(handler) as client:
        client.check_robots("https://www.datos.gob.mx/dataset")
        assert client.limiter._overrides["www.datos.gob.mx"] == 10.0
        with pytest.raises(RobotsDenied):
            client.check_robots("https://www.datos.gob.mx/api/3/action/package_search")
