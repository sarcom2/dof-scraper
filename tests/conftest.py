"""Shared fixtures.

Every fixture in `tests/fixtures/` is a byte-for-byte capture of a real
response, saved on 2026-08-01. The whole suite runs offline: tests that need a
network to pass are tests that fail for reasons unrelated to your code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text("utf-8")


@pytest.fixture
def index_mat() -> str:
    """2026-07-31 matutina: 27 notas across 12 agencies."""
    return fixture_text("index_2026-07-31_MAT.html")


@pytest.fixture
def index_ves() -> str:
    """2026-07-31 vespertina: 4 notas, 2 agency-block decoys."""
    return fixture_text("index_2026-07-31_VES.html")


@pytest.fixture
def index_empty() -> str:
    """A Sunday: index_111.php answers 200 with the site homepage."""
    return fixture_text("index_2026-07-26_EMPTY.html")


@pytest.fixture
def nota_body() -> str:
    return fixture_text("docFuente_5788395.html")


@pytest.fixture
def nota_landing() -> str:
    """The iframe shell -- carries the live FX ticker and today's date."""
    return fixture_text("nota_5788395.html")


@pytest.fixture
def robots_dof() -> str:
    return fixture_text("robots_dof.gob.mx.txt")


@pytest.fixture
def robots_sidof() -> str:
    return fixture_text("robots_sidof.segob.gob.mx.txt")
