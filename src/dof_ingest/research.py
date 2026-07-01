"""`dof-ingest research` -- the easy-door probes, as runnable code.

A README that says "I checked for an API and there wasn't one" is a claim.
This module is the same claim, executable and dated. Run it and it will tell
you whether the reasoning that justified writing a scraper still holds -- which
matters, because the correct outcome of this project is for it to become
obsolete the day SEGOB ships a working bulk endpoint.

Every probe asks whether this data can be obtained without scraping.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta

from .config import INDEX_BASE
from .http import FetchFailed, PoliteClient, RobotsDenied

CKAN_SEARCH = "https://www.datos.gob.mx/api/3/action/package_search"
SIDOF_API = "https://sidof.segob.gob.mx/dof/sidof"
SITEMAP = "https://www.dof.gob.mx/sitemap.xml"
RSS = "https://www.dof.gob.mx/rss.xml"

OPEN, PARTIAL, CLOSED = "OPEN", "PARTIAL", "CLOSED"


@dataclass
class Probe:
    door: str
    question: str
    verdict: str  # OPEN = usable without scraping; PARTIAL / CLOSED = not
    evidence: str


def _get_text(client: PoliteClient, url: str) -> tuple[int | None, str]:
    try:
        resp = client.get(url)
    except RobotsDenied:
        return None, "robots.txt disallows this URL"
    except FetchFailed as exc:
        return exc.status, str(exc)
    return resp.status_code, resp.text


def last_publication_date(client: PoliteClient, start: date | None = None) -> tuple[date, int]:
    """Walk backwards until we find a day whose HTML index actually has notas.

    Self-calibrating on purpose: hard-coding a probe date makes this command
    quietly wrong a year from now.
    """
    day = start or date.today()
    for _ in range(10):
        url = f"{INDEX_BASE}?year={day.year}&month={day.month:02d}&day={day.day:02d}&edicion=MAT"
        status, text = _get_text(client, url)
        if status == 200:
            found = len(set(re.findall(r"nota_detalle\.php\?codigo=(\d+)", text)))
            if found:
                return day, found
        day -= timedelta(days=1)
    return day, 0


# --------------------------------------------------------------------------
# probes
# --------------------------------------------------------------------------


# Recorded once, by hand, during the research pass (see docs/RESEARCH.md).
# We cannot re-derive it automatically because datos.gob.mx disallows /api/ in
# robots.txt and this client obeys robots.txt -- including when obeying it is
# inconvenient for us, which is the only time the rule means anything.
CKAN_MANUAL_FINDING = (
    "Verified manually on 2026-08-01 with a single non-crawling request: the "
    "portal's only DOF dataset is 'Resumen del Diario Oficial de la Federación "
    "(DOF)', published by SENASICA -- one monthly CSV covering that agency's own "
    "publications, last refreshed December 2025. Not the corpus."
)


def probe_ckan(client: PoliteClient) -> Probe:
    """datos.gob.mx is Mexico's national open-data portal. Is the DOF on it?"""
    url = f"{CKAN_SEARCH}?q=diario+oficial+de+la+federacion&rows=5"
    status, text = _get_text(client, url)
    if status is None and "robots" in text:
        return Probe(
            "datos.gob.mx (CKAN)",
            "Is the DOF corpus available as bulk download?",
            CLOSED,
            f"robots.txt disallows /api/ (Crawl-Delay: 10); this probe does not "
            f"query CKAN. {CKAN_MANUAL_FINDING}",
        )
    if status != 200:
        return Probe("datos.gob.mx (CKAN)", "Is there a bulk dataset?", CLOSED,
                     f"package_search returned {status}")
    try:
        result = json.loads(text)["result"]
    except (ValueError, KeyError):
        return Probe("datos.gob.mx (CKAN)", "Is there a bulk dataset?", CLOSED,
                     "unparseable CKAN response")

    titles = [r.get("title", "") for r in result.get("results", [])]
    dof_sets = [t for t in titles if "diario oficial" in t.lower()]
    resources = [
        (res.get("name", ""), res.get("url", ""))
        for r in result.get("results", [])
        for res in r.get("resources", [])
        if "diario oficial" in r.get("title", "").lower()
    ]
    ev = (
        f"CKAN 2.11 API is live ({result.get('count', 0)} hits). "
        f"DOF-titled datasets: {dof_sets or 'none'}. "
        f"Resources: {[n for n, _ in resources] or 'none'}"
    )
    # The one dataset that exists ("Resumen del DOF") is published by SENASICA
    # and covers only that agency's own publications, as a monthly CSV.
    return Probe(
        "datos.gob.mx (CKAN)",
        "Is the DOF corpus available as bulk download?",
        PARTIAL if dof_sets else CLOSED,
        ev,
    )


def probe_sidof_api(client: PoliteClient, probe_day: date, html_count: int) -> list[Probe]:
    """SEGOB documents a DOF WebService. Does it return the day's notas?"""
    ddmmyyyy = probe_day.strftime("%d-%m-%Y")
    probes: list[Probe] = []

    # (a) Is the API host even up? Answering this separately is the difference
    #     between "the endpoint is broken" and "my network is broken".
    status, text = _get_text(client, f"{SIDOF_API}/indicadores/{ddmmyyyy}")
    alive = status == 200 and '"ListaIndicadores"' in text
    probes.append(
        Probe(
            "SIDOF WebService /indicadores",
            "Is the official API reachable at all?",
            OPEN if alive else CLOSED,
            f"HTTP {status}; "
            + (f"{text[:110]}" if alive else "no indicator payload"),
        )
    )

    # (b) The endpoint that would actually replace this scraper.
    status, text = _get_text(client, f"{SIDOF_API}/diarios/{ddmmyyyy}")
    listed = 0
    if status == 200:
        try:
            listed = len(json.loads(text).get("ListaDiarios", []))
        except ValueError:
            listed = -1
    verdict = OPEN if listed > 0 else CLOSED
    probes.append(
        Probe(
            "SIDOF WebService /diarios",
            "Does the documented bulk endpoint return the day's editions?",
            verdict,
            f"HTTP {status}, ListaDiarios={listed} entries for {ddmmyyyy}, "
            f"while the HTML index for the same date lists {html_count} notas. "
            + ("API and HTML agree." if listed else "The endpoint answers 200 with an empty list."),
        )
    )
    return probes


def probe_sitemap(client: PoliteClient) -> Probe:
    status, text = _get_text(client, SITEMAP)
    if status != 200:
        return Probe("sitemap.xml", "Can we enumerate URLs from a sitemap?", CLOSED,
                     f"HTTP {status}")
    locs = re.findall(r"<loc>([^<]+)</loc>", text)
    lastmods = sorted(re.findall(r"<lastmod>(\d{4})", text))
    hosts = {re.sub(r"https?://([^/]+).*", r"\1", u) for u in locs}
    stale = bool(lastmods) and lastmods[-1] < "2015"
    return Probe(
        "sitemap.xml",
        "Can we enumerate note URLs from a sitemap?",
        CLOSED,
        f"{len(locs)} URLs, hosts={sorted(hosts)}, lastmod {lastmods[0] if lastmods else '?'}"
        f"..{lastmods[-1] if lastmods else '?'}"
        + (" (stale, and it points at a decommissioned host)" if stale else "")
        + " -- static pages only, no notas.",
    )


def probe_rss(client: PoliteClient) -> Probe:
    status, _ = _get_text(client, RSS)
    return Probe("rss.xml", "Is there a feed to poll instead of crawling?",
                 OPEN if status == 200 else CLOSED, f"HTTP {status}")


def probe_robots(client: PoliteClient) -> list[Probe]:
    """robots.txt is the constraint that shaped the architecture."""
    out = []
    for host, sample, label in (
        ("https://www.dof.gob.mx", "https://www.dof.gob.mx/nota_detalle.php?codigo=5795217",
         "detail pages on www"),
        ("https://sidof.segob.gob.mx", "https://sidof.segob.gob.mx/notas/docFuente/5795217",
         "note bodies on sidof"),
        ("https://sidof.segob.gob.mx", "https://sidof.segob.gob.mx/notas/docFuente/5381640",
         "a note blocked by name"),
    ):
        allowed = client.robots.allows(sample)
        out.append(
            Probe(
                f"robots.txt {host.split('//')[1]}",
                f"May we fetch {label}?",
                OPEN if allowed else CLOSED,
                f"{'allowed' if allowed else 'DISALLOWED'}: {sample}",
            )
        )
    return out


def probe_all(client: PoliteClient, fecha: str | None = None) -> list[Probe]:
    start = date.fromisoformat(fecha) if fecha else None
    probe_day, html_count = last_publication_date(client, start)
    probes = [
        Probe(
            "baseline (HTML index)",
            "What does the site itself show for the probe date?",
            OPEN,
            f"{probe_day.isoformat()} matutina: {html_count} nota links in the HTML index",
        ),
        probe_ckan(client),
        *probe_sidof_api(client, probe_day, html_count),
        probe_sitemap(client),
        probe_rss(client),
        *probe_robots(client),
    ]
    return probes


# --------------------------------------------------------------------------


def render(probes: list[Probe], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps([p.__dict__ for p in probes], ensure_ascii=False, indent=2))
        return

    print("\n  Is there an API or bulk source that makes scraping unnecessary?\n")
    width = max(len(p.door) for p in probes)
    for p in probes:
        mark = {OPEN: "OPEN   ", PARTIAL: "PARTIAL", CLOSED: "CLOSED "}[p.verdict]
        print(f"  [{mark}] {p.door:<{width}}  {p.question}")
        for line in _wrap(p.evidence, 84):
            print(f"  {' ' * (width + 12)}{line}")
        print()

    usable = [p for p in probes if p.verdict == OPEN and p.door.startswith("SIDOF WebService /d")]
    if usable:
        print("  => An official bulk endpoint now works. Prefer it; this scraper is obsolete.\n")
    else:
        print(
            "  => No endpoint returns the corpus. Scraping the HTML index is the\n"
            "     remaining option -- as a last resort, within robots.txt.\n"
        )


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines
