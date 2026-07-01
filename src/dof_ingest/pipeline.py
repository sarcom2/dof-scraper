"""Orchestration: discover -> enrich.

Two stages, deliberately separate and independently re-runnable.

`discover` walks (date, edition) index pages and records the notas it finds.
`enrich` fetches the full text of notas whose body is still pending.

They are split because they have different failure profiles and different
costs. Discovery is ~1 request per edition and cheap to redo; enrichment is 1
request per nota and is where a crawl actually spends its time. Coupling them
would mean a failure in the expensive stage forces you to redo the cheap one,
and -- worse -- that a partially-enriched run has no clean resume point.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date, timedelta

from .config import NOTA_BODY_BASE, Settings
from .http import CircuitOpen, FetchFailed, PoliteClient, RobotsDenied
from .models import content_hash
from .parse import ParseError, decode_response, parse_index, parse_nota_body
from .store import RunCounters, Store

log = logging.getLogger(__name__)

EDITION_PARAM = {"MAT": "MAT", "VES": "VES", "EXT": "EXT"}


def date_range(since: date, until: date) -> Iterator[date]:
    day = since
    while day <= until:
        yield day
        day += timedelta(days=1)


def index_url(base: str, day: date, edicion: str) -> str:
    # The `edicion` parameter is not optional in practice. Omitting it does not
    # mean "all editions": on 2026-07-31 the bare URL returned the vespertina
    # (4 notas) and silently hid the matutina (27). See docs/RESEARCH.md.
    return (
        f"{base}?year={day.year}&month={day.month:02d}&day={day.day:02d}"
        f"&edicion={EDITION_PARAM[edicion]}"
    )


def discover(
    store: Store,
    client: PoliteClient,
    settings: Settings,
    since: date,
    until: date,
    editions: tuple[str, ...],
    index_base: str,
    recheck_days: int = 0,
) -> RunCounters:
    """Crawl index pages for [since, until] and upsert every nota found.

    `recheck_days` re-visits editions already recorded as complete, because the
    DOF does amend an edition after publishing it (a nota gets a fe de erratas
    or is withdrawn). Outside that window we trust what we have -- otherwise
    "incremental" means "re-crawl everything, forever".
    """
    counters = RunCounters()
    fechas = [d.isoformat() for d in date_range(since, until)]
    known = store.known_editions(fechas)
    horizon = date.today() - timedelta(days=recheck_days)

    for day in date_range(since, until):
        fecha = day.isoformat()
        for edicion in editions:
            status = known.get((fecha, edicion))
            if status is not None and day < horizon:
                log.debug("%s/%s already recorded (%s); skipping", fecha, edicion, status)
                continue

            url = index_url(index_base, day, edicion)
            try:
                resp = client.get(url)
            except RobotsDenied:
                counters.skipped_robots += 1
                counters.detail.append(f"robots denied index {fecha}/{edicion}")
                continue
            except CircuitOpen:
                raise
            except FetchFailed as exc:
                counters.errors += 1
                counters.detail.append(f"fetch {fecha}/{edicion}: {exc}")
                log.error("%s", exc)
                continue

            html = decode_response(resp)
            try:
                page = parse_index(html, fecha, edicion, source_url=url)
            except ParseError as exc:
                # A ParseError is a code bug, not a data condition. Count it,
                # surface it, and do NOT write an empty edition row that would
                # make the next run skip this date forever.
                counters.errors += 1
                counters.detail.append(str(exc))
                log.error("%s", exc)
                continue

            if not page.published:
                counters.no_edition += 1
                store.record_edition(fecha, edicion, "no_edition", 0, 0, None)
                continue

            # Edition-level hash over the *set* of nota hashes: cheap way to
            # tell "this edition is byte-for-byte what we already have" without
            # re-reading every row.
            page_hash = content_hash({"notas": "|".join(sorted(n.hash() for n in page.notas))})

            with store.transaction():
                for nota in page.notas:
                    outcome = store.upsert_nota(nota)
                    setattr(counters, outcome, getattr(counters, outcome) + 1)
                store.record_edition(
                    fecha, edicion, "ok", len(page.notas), len(page.doc_only_codes), page_hash
                )
            log.info("%s (%d rejected as agency blocks)", page.summary, len(page.doc_only_codes))

    return counters


def enrich(
    store: Store,
    client: PoliteClient,
    limit: int,
    retry_errors: bool = False,
    body_base: str = NOTA_BODY_BASE,
) -> RunCounters:
    """Fetch the full text of notas whose body is still pending."""
    counters = RunCounters()
    for row in store.pending_bodies(limit, retry_errors=retry_errors):
        codigo = int(row["codigo"])
        landing = row["url_detalle"]
        body_url = f"{body_base}/{codigo}"

        try:
            # Check BOTH URLs against robots. sidof's robots.txt names 18
            # specific notes under both `/notas/{id}` and `/notas/docFuente/{id}`,
            # but a scraper that only checked the iframe target would sail past
            # any note blocked solely on its landing page. Publishers express
            # intent at the resource level; we honour it at the resource level.
            client.check_robots(landing)
            client.check_robots(body_url)
            resp = client.get(body_url)
        except RobotsDenied:
            # These URLs return HTTP 200, but robots.txt says no. Record them
            # as `robots_denied` so the gap isn't hidden.
            counters.skipped_robots += 1
            store.set_body(codigo, "robots_denied")
            log.info("nota %s: skipped, robots.txt disallows it", codigo)
            continue
        except CircuitOpen:
            raise
        except FetchFailed as exc:
            status = "not_found" if exc.status in (404, 410) else "error"
            counters.errors += 1
            counters.detail.append(f"nota {codigo}: {exc}")
            store.set_body(codigo, status)
            continue

        text = parse_nota_body(decode_response(resp))
        if not text:
            counters.errors += 1
            counters.detail.append(f"nota {codigo}: empty body")
            store.set_body(codigo, "error")
            continue

        outcome = store.set_body(codigo, "ok", text)
        setattr(counters, outcome, getattr(counters, outcome) + 1)

    return counters
