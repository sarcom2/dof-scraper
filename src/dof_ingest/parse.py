"""HTML decoding and parsing.

This is where the DOF fights back. Read `docs/RESEARCH.md` for the field notes;
the short version lives in the docstrings below.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser, LexborNode

from .config import NOTA_BASE
from .models import IndexPage, Nota, canonical_text

log = logging.getLogger(__name__)


class ParseError(Exception):
    """The page did not look like what we expected. Fail loudly, never guess."""


# --------------------------------------------------------------------------
# 1. Decoding
# --------------------------------------------------------------------------

_META_CHARSET = re.compile(rb"""charset\s*=\s*["']?\s*([\w-]+)""", re.I)


def decode(body: bytes, http_charset: str | None) -> str:
    """Turn response bytes into text, in a defined order of trust.

    The DOF index page is a live example of why charset sniffing needs a
    policy rather than a library call. One single response contains::

        HTTP/1.1 200 OK
        Content-Type: text/html; charset=UTF-8
        ...
        <meta http-equiv="Content-Type" content="text/html; charset=ISO-8859-1">
        <meta charset="UTF-8">

    Two `<meta>` tags that contradict each other, and the *first* one is the
    wrong one. Anything that trusts the document's own first declaration --
    which includes `lxml`'s meta sniffing and several "just use
    `response.apparent_encoding`" recipes -- decodes valid UTF-8 as Latin-1 and
    silently produces `FederaciÃ³n` in every single record.

    Order of trust here:
      1. the HTTP `Content-Type` charset (the transport layer knows best, and
         WHATWG agrees: it outranks in-document declarations);
      2. a strict UTF-8 decode -- if the bytes *are* valid UTF-8, they were
         almost certainly meant to be UTF-8, since valid multi-byte UTF-8
         sequences essentially never occur by accident in Latin-1 text;
      3. cp1252 as the fallback, not latin-1: real-world "ISO-8859-1" Mexican
         government pages routinely contain 0x91-0x94 smart quotes, which are
         undefined in true latin-1 and mangle into control characters.

    `errors="replace"` at the end rather than an exception: one bad byte in a
    100 KB page should cost us one character, not the whole day's ingestion.
    """
    if http_charset:
        try:
            return body.decode(http_charset)
        except (LookupError, UnicodeDecodeError):
            log.debug("declared charset %r failed; sniffing", http_charset)

    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        pass

    meta = _META_CHARSET.search(body[:4096])
    declared = meta.group(1).decode("ascii", "ignore").lower() if meta else ""
    if declared in {"iso-8859-1", "latin-1", "latin1", "windows-1252", "cp1252"}:
        return body.decode("cp1252", errors="replace")
    if declared:
        try:
            return body.decode(declared, errors="replace")
        except LookupError:
            pass
    return body.decode("cp1252", errors="replace")


def decode_response(resp: httpx.Response) -> str:
    return decode(resp.content, resp.charset_encoding)


# --------------------------------------------------------------------------
# 2. The index page
# --------------------------------------------------------------------------

# Hierarchy in the index is encoded by CSS class on flat sibling <td> rows,
# not by nesting. Discovered by reading the markup; asserted by the fixtures.
CLS_SECCION = "txt_blanco"  # "UNICA SECCION", "PRIMERA SECCION", ...
CLS_PODER = "txt_blanco2"  # "PODER EJECUTIVO", "ORGANISMOS DESCONCENTRADOS..."
CLS_ORGANISMO = "subtitle_azul"  # "SECRETARIA DE HACIENDA Y CREDITO PUBLICO"

_FECHA_HDR = re.compile(r"Fecha:\s*(\d{2})/(\d{2})/(\d{4})")


def _classes(node: LexborNode) -> set[str]:
    return set((node.attributes.get("class") or "").split())


def _code_from_href(href: str, param: str) -> int | None:
    qs = parse_qs(urlsplit(href).query)
    raw = qs.get(param, [""])[0]
    return int(raw) if raw.isdigit() else None


def parse_index(html: str, fecha: str, edicion: str, source_url: str = "") -> IndexPage:
    """Extract the notas of one (date, edition) index page.

    THE UGLY CASE -- a flat table plus a decoy that looks exactly like the
    signal.

    The index is one long `<table>` where the document hierarchy
    (section -> branch of government -> agency -> nota) is *not* expressed by
    nesting. Every level is a sibling `<tr>`, distinguished only by the CSS
    class of its `<td>`. So the agency a nota belongs to is not reachable from
    the nota's own node: you have to walk the rows in document order and carry
    the current section/branch/agency as state.

    Worse, the agency header row carries a "Ver WORD" link that exports that
    whole agency block::

        <td class="subtitle_azul">SECRETARIA DE HACIENDA Y CREDITO PUBLICO
          <a href="/nota_to_doc.php?codnota=5795216"><img alt="Ver WORD"></a></td>
        <td><a class="enlaces" href="/nota_detalle.php?codigo=5795217&...">Acuerdo...</a></td>

    `codnota=5795216` is allocated from the same sequence as real nota codes
    and is indistinguishable from one by value. On the 2026-07-31 vespertina
    fixture, a `\\b5\\d{6}\\b` regex over the page yields **6** codes; only
    **4** are notas. The other two are agency-block Word exports. Grabbing
    `codnota=` instead is no better -- every real nota has one of those too, so
    you still get 6.

    There is no lexical way to tell them apart. The only discriminator is
    structural position: a nota is an `<a>` pointing at `nota_detalle.php` in a
    content row; an agency block is an `<a>` pointing at `nota_to_doc.php`
    inside a `subtitle_azul` header row. Hence: parse the tree, keep state, and
    match on the link target -- not on the digits.

    We keep the rejected codes in `IndexPage.doc_only_codes` so a run report
    can show that we saw them and dropped them deliberately, rather than the
    reader having to take it on faith.
    """
    tree = LexborHTMLParser(html)
    body_text = tree.body.text() if tree.body else ""
    looks_like_index = bool(_FECHA_HDR.search(body_text))

    page = IndexPage(fecha=fecha, edicion=edicion)
    seccion = poder = organismo = ""
    seen: set[int] = set()

    root = tree.root
    if root is None:  # pragma: no cover - lexbor always gives a root
        raise ParseError("empty document")

    for node in root.traverse(include_text=False):
        tag = node.tag
        if tag == "td":
            cls = _classes(node)
            text = canonical_text(node.text())
            if not text:
                continue
            if CLS_SECCION in cls:
                seccion = text
            elif CLS_PODER in cls:
                poder = text
            elif CLS_ORGANISMO in cls:
                # The "Ver WORD" <img alt> would otherwise be concatenated into
                # the agency name by .text(); alt text is not rendered content
                # for our purposes, so strip anything after the first newline
                # and any trailing icon labels.
                organismo = _clean_organismo(text)
            continue

        if tag != "a":
            continue
        href = node.attributes.get("href") or ""

        if "nota_to_doc.php" in href:
            code = _code_from_href(href, "codnota")
            # Only the ones hanging off an agency header are decoys; a
            # nota_to_doc link inside a content row is that nota's own Word
            # export and is already covered by its nota_detalle sibling.
            if code is not None and _in_header_row(node) and code not in page.doc_only_codes:
                page.doc_only_codes.append(code)
            continue

        if "nota_detalle.php" not in href:
            continue
        codigo = _code_from_href(href, "codigo")
        titulo = canonical_text(node.text())
        if codigo is None or not titulo:
            continue
        if codigo in seen:  # the DOF occasionally repeats a link in the index
            continue
        seen.add(codigo)
        page.notas.append(
            Nota(
                codigo=codigo,
                fecha=fecha,
                edicion=edicion,
                titulo=titulo,
                organismo=organismo,
                poder=poder,
                seccion=seccion,
                # NOT the www.dof.gob.mx detail URL: robots.txt disallows
                # /nota_detalle.php there. sidof serves the same nota and
                # allows it. See http.RobotsGate.
                url_detalle=f"{NOTA_BASE}/{codigo}",
                url_origen=source_url,
            )
        )

    # --- canary ----------------------------------------------------------
    # If the raw HTML advertises detail links but our structural walk produced
    # nothing, the CSS classes we key on have changed. That is a code bug, not
    # a quiet day, and it must not be reported as "0 notas published" -- an
    # ingestion pipeline that silently returns empty is worse than one that
    # crashes, because the emptiness looks like data.
    if not page.notas and "nota_detalle.php?codigo=" in html:
        raise ParseError(
            f"{fecha}/{edicion}: page contains nota_detalle links but the parser "
            "matched none -- the index markup changed; refusing to report an empty day"
        )

    page.published = bool(page.notas)
    if not page.published:
        if looks_like_index:
            # A real index page with no entries. Rare, but legitimate.
            log.info("%s/%s: index present, no entries", fecha, edicion)
        else:
            # THE SECOND UGLY CASE. Ask index_111.php for an edition that does
            # not exist -- any Sunday, or an EXT on a day without one -- and it
            # answers `200 OK` with the DOF *homepage*, not a 404 and not an
            # empty index. So "HTTP 200" carries no information about whether
            # the thing you asked for exists, and a scraper that trusts the
            # status code records a successful crawl of nothing.
            #
            # We detect it positively (no `Fecha: dd/mm/yyyy` header anywhere)
            # and record it as `no_edition` so the run report distinguishes
            # "did not publish" from "we failed to read it".
            log.info(
                "%s/%s: no such edition (site served its homepage)", fecha, edicion
            )
    return page


def _clean_organismo(text: str) -> str:
    """Drop icon alt-text that selectolax folds into the header cell."""
    for noise in ("Ver WORD", "Ver Imagen", "Ver PDF"):
        text = text.replace(noise, " ")
    return canonical_text(text)


def _in_header_row(node: LexborNode) -> bool:
    """True if this <a> lives inside an agency header cell."""
    parent = node.parent
    depth = 0
    while parent is not None and depth < 4:
        if parent.tag == "td" and CLS_ORGANISMO in _classes(parent):
            return True
        parent = parent.parent
        depth += 1
    return False


# --------------------------------------------------------------------------
# 3. The nota detail page (sidof)
# --------------------------------------------------------------------------

# Chrome that changes on every request and must never reach the content hash.
_VOLATILE = (
    re.compile(r"Tipo de cambio y tasas de inter.s.*?ver hist.rico", re.S | re.I),
    re.compile(r"\[citado el \d{2}-\d{2}-\d{4}\]"),
    re.compile(r"D.LAR\s+[\d.,]+"),
    re.compile(r"UDIS\s+[\d.,]+"),
    re.compile(r"TIIE[^%]{0,20}[\d.,]+%"),
    re.compile(r"CCP(?:-UDIS|-D.LARES)?\s+[\d.,]+%"),
    re.compile(r"CPP\s+[\d.,]+%"),
)

def parse_nota_body(html: str) -> str:
    """Extract the legal text of a nota from its `docFuente` document.

    WHY WE FETCH `docFuente` AND NOT THE PAGE A HUMAN LANDS ON.

    `https://sidof.segob.gob.mx/notas/{codigo}` is the human-facing page, and
    it does not contain the nota. It contains an `<iframe>` pointing at
    `/notas/docFuente/{codigo}`, which is where the actual text lives.

    That turned out to be lucky, because the landing page is unhashable. Its
    masthead renders a live FX/UDIS ticker (`DÓLAR 17.3213  UDIS 8.844190`)
    and its citation box is stamped with *today's* date
    (`[citado el 01-08-2026]`). Hash that page and every record in the corpus
    reports a content change every single day -- a change detector that always
    fires is worse than none, because it looks like it's working when it
    isn't.

    `docFuente` is clean: valid UTF-8, no chrome, no dynamic values. The
    `_VOLATILE` scrub below is therefore not load-bearing today. It stays as a
    cheap regression guard, because the failure it prevents (100% false-change
    rate) is both expensive and completely silent.
    """
    tree = LexborHTMLParser(html)
    for tag in ("script", "style", "noscript", "nav", "header", "footer", "form"):
        for node in tree.css(tag):
            node.decompose()

    root = tree.body or tree.root
    text = root.text() if root is not None else ""

    for pattern in _VOLATILE:
        text = pattern.sub(" ", text)
    return canonical_text(text)
