"""Domain records and content hashing.

The hash is the heart of the change-detection story, so it gets its own
carefully-scoped function rather than `sha256(response.content)`.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field

_WS = re.compile(r"\s+")


def canonical_text(value: str | None) -> str:
    """Normalise a string so that cosmetic edits do not read as content changes.

    NFC because the DOF mixes precomposed and decomposed accents depending on
    which editor produced the source document; whitespace collapse because the
    HTML is hand-formatted and re-indented at random.
    """
    if value is None:
        return ""
    return _WS.sub(" ", unicodedata.normalize("NFC", value)).strip()


def content_hash(fields: dict[str, object]) -> str:
    """Stable SHA-256 over the *business* fields of a record.

    Deliberately NOT a hash of the raw HTTP body. Hashing the raw page would
    report a change on every single run, because both DOF surfaces embed
    volatile chrome in every response:

      * the sidof note page renders a live FX/UDIS ticker
        ("DÓLAR 17.3213  UDIS 8.844190") in the masthead;
      * the same page renders a suggested citation containing *today's* date
        ("[citado el 01-08-2026]");
      * www.dof.gob.mx issues a fresh `DOF_WEB` session cookie per request.

    So we hash a canonical JSON projection of the fields we actually care
    about. `sort_keys` makes it order-independent; `ensure_ascii=False` keeps
    the accented text as-is so the NFC normalisation above is what decides
    equality.
    """
    canon = {k: canonical_text(v) if isinstance(v, str) else v for k, v in sorted(fields.items())}
    blob = json.dumps(canon, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Nota:
    """One entry of one edition of the Diario Oficial."""

    codigo: int  # DOF's own primary key, stable across time
    fecha: str  # ISO date, YYYY-MM-DD
    edicion: str  # MAT | VES | EXT
    titulo: str
    organismo: str  # e.g. "SECRETARIA DE HACIENDA Y CREDITO PUBLICO"
    poder: str = ""  # e.g. "PODER EJECUTIVO"
    seccion: str = ""  # e.g. "UNICA SECCION"
    url_detalle: str = ""  # sidof.segob.gob.mx (robots-allowed)
    url_origen: str = ""  # the index page this was discovered on

    def hash(self) -> str:
        """Hash over the fields that define the record's meaning.

        `url_origen` is excluded on purpose: rediscovering the same nota
        through a different index URL is not a content change.
        """
        return content_hash(
            {
                "codigo": self.codigo,
                "fecha": self.fecha,
                "edicion": self.edicion,
                "titulo": self.titulo,
                "organismo": self.organismo,
                "poder": self.poder,
                "seccion": self.seccion,
            }
        )

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class IndexPage:
    """Parse result for one (date, edition) index page.

    `doc_only_codes` is not dead weight: those are the `nota_to_doc.php?codnota=`
    identifiers that sit on the agency *header* rows and look exactly like nota
    codes. We record them so the run report can prove we saw them and rejected
    them on purpose. See docs/RESEARCH.md ("the ugly case").
    """

    fecha: str
    edicion: str
    notas: list[Nota] = field(default_factory=list)
    doc_only_codes: list[int] = field(default_factory=list)
    published: bool = True  # False => the DOF did not publish this edition

    @property
    def summary(self) -> str:
        return (
            f"{self.fecha}/{self.edicion}: {len(self.notas)} notas, "
            f"{len(self.doc_only_codes)} agency-block links rejected"
        )
