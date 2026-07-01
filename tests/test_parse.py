"""Parsing tests, all against real captured pages.

These are the tests that would have caught the bugs this project exists to
document, so they assert the specific numbers rather than "some notas".
"""

from __future__ import annotations

import re

import pytest

from dof_ingest.parse import ParseError, decode, parse_index, parse_nota_body

# --------------------------------------------------------------------------
# the ugly case: a flat table with decoys that look exactly like the signal
# --------------------------------------------------------------------------


def test_vespertina_has_four_notas_not_six(index_ves: str) -> None:
    """The headline case, with the exact numbers.

    A naive scrape of this page finds six 7-digit codes. Two of them are
    `nota_to_doc.php?codnota=` links attached to *agency header* rows, not
    notas. If this assertion ever reads 6, the parser has regressed to matching
    on digits instead of on structure.
    """
    naive = sorted(set(re.findall(r"\b(5\d{6})\b", index_ves)))
    assert len(naive) == 6, "fixture drifted; the decoy case is the point of it"

    page = parse_index(index_ves, "2026-07-31", "VES")

    assert len(page.notas) == 4
    assert [n.codigo for n in page.notas] == [5795217, 5795218, 5795219, 5795221]
    assert page.doc_only_codes == [5795216, 5795220]
    # Together they account for every code on the page: nothing was lost, the
    # two extras were classified rather than ignored.
    assert sorted([n.codigo for n in page.notas] + page.doc_only_codes) == [int(c) for c in naive]


def test_agency_attribution_crosses_flat_rows(index_ves: str) -> None:
    """`organismo` lives in a *sibling* row, not an ancestor of the nota."""
    page = parse_index(index_ves, "2026-07-31", "VES")
    by_code = {n.codigo: n for n in page.notas}

    assert by_code[5795217].organismo == "SECRETARIA DE HACIENDA Y CREDITO PUBLICO"
    assert by_code[5795219].organismo == "SECRETARIA DE HACIENDA Y CREDITO PUBLICO"
    # The state must roll over at the next header row, not stick.
    assert by_code[5795221].organismo == "INSTITUTO MEXICANO DEL SEGURO SOCIAL"
    assert by_code[5795221].poder == "ORGANISMOS DESCONCENTRADOS O DESCENTRALIZADOS"
    assert by_code[5795217].poder == "PODER EJECUTIVO"
    assert all(n.seccion == "UNICA SECCION" for n in page.notas)


def test_matutina_full_page(index_mat: str) -> None:
    page = parse_index(index_mat, "2026-07-31", "MAT")
    assert len(page.notas) == 27
    assert len(page.doc_only_codes) == 12
    assert len({n.organismo for n in page.notas}) == 12
    assert all(n.titulo for n in page.notas)
    assert all(n.organismo for n in page.notas)
    assert all(n.url_detalle.endswith(str(n.codigo)) for n in page.notas)


def test_edition_param_is_not_optional(index_mat: str, index_ves: str) -> None:
    """Same date, same URL but for `edicion`: 27 notas vs 4, disjoint sets.

    Documents why `pipeline.index_url` always sends `edicion`. Omitting it
    returns whichever edition the site considers current and silently hides
    the rest.
    """
    mat = {n.codigo for n in parse_index(index_mat, "2026-07-31", "MAT").notas}
    ves = {n.codigo for n in parse_index(index_ves, "2026-07-31", "VES").notas}
    assert len(mat) == 27 and len(ves) == 4
    assert not (mat & ves)


# --------------------------------------------------------------------------
# 200 OK is not evidence that anything exists
# --------------------------------------------------------------------------


def test_missing_edition_is_a_state_not_an_error(index_empty: str) -> None:
    """Sunday: `index_111.php` answers 200 with the site's homepage."""
    page = parse_index(index_empty, "2026-07-26", "MAT")
    assert page.published is False
    assert page.notas == []


def test_layout_change_raises_instead_of_returning_empty(index_mat: str) -> None:
    """The canary. Silent emptiness is the failure mode that looks like data."""
    # A plausible redesign: the links become JS-driven <span>s. The codes are
    # still in the HTML, so "no notas" can only mean our walk stopped matching.
    broken = index_mat.replace("<a ", "<span ").replace("</a>", "</span>")
    # The links are still in the HTML, so "no notas" can only mean we broke.
    assert "nota_detalle.php?codigo=" in broken
    with pytest.raises(ParseError, match="markup changed"):
        parse_index(broken, "2026-07-31", "MAT")


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------


def test_index_declares_two_contradictory_charsets() -> None:
    """The page really does declare both, and the first one is wrong."""
    from tests.conftest import fixture_bytes

    raw = fixture_bytes("index_2026-07-31_MAT.html")
    declared = re.findall(rb"charset=[\"']?([\w-]+)", raw[:6000])
    assert b"ISO-8859-1" in declared and b"UTF-8" in declared
    assert declared[0].upper() == b"ISO-8859-1"  # the wrong one comes first

    # Trusting the document -> mojibake. Trusting the transport -> correct.
    assert "FederaciÃ³n" in raw.decode("cp1252")
    assert "Federación" in decode(raw, "UTF-8")


def test_decode_falls_back_without_a_header() -> None:
    assert decode("Federación".encode(), None) == "Federación"
    # Real cp1252 bytes are not valid UTF-8, so the sniff order resolves them.
    assert decode("Federación “x”".encode("cp1252"), None) == "Federación “x”"
    # A bogus declared charset must not take the whole page down.
    assert decode("Federación".encode(), "definitely-not-a-charset") == "Federación"


# --------------------------------------------------------------------------
# nota body
# --------------------------------------------------------------------------


def test_body_is_extracted_from_the_iframe_document(nota_body: str) -> None:
    text = parse_nota_body(nota_body)
    assert text.startswith("AVISO por el que se da a conocer el cambio de domicilio")
    assert "Al margen un sello con el Escudo Nacional" in text
    assert len(text) > 1000


def test_landing_page_carries_volatile_chrome(nota_landing: str, nota_body: str) -> None:
    """Why we fetch `docFuente` and not the page a human lands on.

    The landing page embeds a live FX ticker and a citation stamped with the
    current date. Hashing it would report a change every day, forever.
    """
    assert re.search(r"\[citado el \d{2}-\d{2}-\d{4}\]", nota_landing)
    assert "UDIS" in nota_landing

    # docFuente has none of it, which is what makes it hashable.
    assert "citado el" not in nota_body
    scrubbed = parse_nota_body(nota_landing)
    assert not re.search(r"\[citado el \d{2}-\d{2}-\d{4}\]", scrubbed)
