# Why I did it this way

Short notes on the main design choices, what I rejected, and when I’d change my
mind.

---

## ADR-1 — Scrape the DOF at all

**Status:** accepted, provisionally.

Every non-scraping route was checked and closed (see
[RESEARCH.md](RESEARCH.md)). The documented bulk endpoint exists and returns
`200 OK` with an empty list for every date.

**Rejected:** using the SIDOF `/diarios` WebService (broken); the
datos.gob.mx CKAN dataset (one agency, one month); parsing the per-edition
PDFs (unstructured, and the HTML index already carries the metadata).

**Revisit when:** `dof-ingest research` reports `SIDOF WebService /diarios` as
`OPEN`. The command exists so this is a scheduled fact, not a memory. At that
point this project should be deleted, not migrated.

---

## ADR-2 — Two hosts, because robots.txt says so

**Status:** accepted.

`www.dof.gob.mx/robots.txt` disallows `/nota_detalle.php?` and
`/nota_to_doc.php?`, removing the entire detail layer of that host. The same
notices are served by `sidof.segob.gob.mx`, which allows individual notes
except for 18 named IDs. So: **discover on www, enrich on sidof.**

**Rejected:** crawling `nota_detalle.php` anyway. It returns HTTP 200 and
nobody would notice, which is exactly why the rule matters: a politeness policy
that only applies when it’s convenient isn’t one.

**Consequence:** the 18 named notices are permanently absent from the corpus.
They are recorded as `body_status = 'robots_denied'` so the gap is visible in
`dof-ingest stats` rather than silently absent.

**Revisit when:** either robots.txt changes.

---

## ADR-3 — Normalise robots.txt before parsing it

**Status:** accepted.

`urllib.robotparser` implements the 1996 draft in which a blank line terminates
a group. The DOF's robots.txt has a blank line between `User-agent: *` and its
rules, so the stdlib discards all of them and returns `can_fetch(...) == True`
for explicitly disallowed URLs.

**Chosen:** normalise the *input* — drop comments and blank lines, re-insert
group separators per RFC 9309 §2.2.1 — and keep the stdlib matcher.

**Rejected:**
- *Use the stdlib as-is.* Would crawl disallowed pages while logging
  compliance. Not acceptable.
- *Add `protego` or `reppy`.* A third dependency to fix a 12-line input
  problem, and `protego` has had its own group-handling quirks.
- *Write a matcher.* The stdlib's path matching is correct and well-tested;
  only its grouping is out of date. Replacing the good part to fix the bad part
  is a bad trade.

**Revisit when:** `test_stdlib_alone_grants_access_to_disallowed_pages` starts
failing — that means CPython fixed it and the normaliser can be deleted.

---

## ADR-4 — Hash business fields, not bytes

**Status:** accepted.

Both DOF surfaces embed volatile chrome in every response: a live FX/UDIS
ticker, a citation stamped with today's date, a fresh session cookie. Raw-body
hashing would report a change on 100% of records every day.

**Chosen:** SHA-256 over a canonical JSON projection (sorted keys,
NFC-normalised, whitespace-collapsed) of the fields that carry meaning. Index
metadata and full text hash separately, because the DOF amends published text
without touching titles.

**Rejected:**
- *`sha256(response.content)`.* Always-on change detector; worse than none.
- *Conditional GET (`ETag` / `If-Modified-Since`).* Would be cheaper, but the
  endpoints send `Cache-Control: no-store` and no validators at all
  ([RESEARCH.md §4](RESEARCH.md#4-conditional-get--also-unavailable)).
- *Hashing only the title.* Would miss agency reassignments and section moves,
  both of which occur.

**Revisit when:** the DOF starts sending `ETag`. Conditional GET becomes the
cheap first layer, and content hashing stays as the second.

---

## ADR-5 — SQLite

**Status:** accepted.

One writer, a few hundred thousand rows of mostly text. WAL gives concurrent
readers, `PRAGMA user_version` gives schema versioning, and the whole corpus is
a single file you can email.

**Rejected:** Postgres — an operational dependency bought for concurrency we
don't have (1 req/s, no parallel fetch). The schema is plain enough to port
unchanged if that ever changes.

**Revisit when:** a second concurrent writer appears, or a consumer needs
network access to the data.

---

## ADR-6 — Split `discover` and `enrich`

**Status:** accepted.

Different cost and different failure profile. Discovery is ~1 request per
edition and cheap to redo; enrichment is 1 request per notice and is where the
crawl spends its time.

**Rejected:** one pass that fetches the body while parsing the index. A failure
in the expensive stage would force redoing the cheap one, and a
partially-enriched run would have no clean resume point.

**Consequence:** `enrich` is driven by a `body_status` queue, so it resumes
exactly where it stopped and `--limit` bounds any single run.

---

## ADR-7 — Bundle two missing TLS intermediates

**Status:** accepted, reluctantly.

`www.dof.gob.mx` sends its leaf certificate twice and omits the GoDaddy G2
intermediate; `www.datos.gob.mx` omits Let's Encrypt E8. curl and browsers hide
this by following the certificate's AIA extension; OpenSSL does not.

**Chosen:** ship both public intermediates, downloaded from the URL in each
server's own AIA extension, and add them to an otherwise-default SSL context.
Verification stays fully on.

**Rejected:**
- *`verify=False`.* Disables verification against every host, permanently, to
  work around someone else's misconfiguration. This is the most common bad habit
  in scraping code.
- *`truststore`.* Would work on macOS and Windows via the OS trust store, but
  not on Linux CI, which is where the pipeline would actually run.

**Revisit when:** either certificate expires — TLS will fail loudly and
`certs/README.md` has the re-download procedure — or SEGOB fixes its chain, at
which point delete the directory.

---

## ADR-8 — Date × edition enumeration instead of page numbers

**Status:** accepted.

The DOF has no `?page=N`. Its corpus is addressed by `(date, edition)`, three
editions per day, where a missing edition returns `200 OK` with the homepage.

**Consequence:** the cursor is a date range and the resume state lives in the
`edition` table, which is strictly more work than page numbers — it has to
distinguish "not published", "published and empty", and "we failed to read it",
and only the last one should be retried.

`--recheck-days` (default 7) bounds re-crawling of recent editions, because the
DOF amends editions after publication. Outside that window, recorded editions
are skipped entirely; otherwise "incremental" means "re-crawl everything,
forever".

---

## ADR-9 — Fail loudly on layout change

**Status:** accepted.

If the raw HTML contains `nota_detalle.php?codigo=` but the structural walk
matched nothing, `parse_index` raises `ParseError`. The pipeline counts it as
an error and — importantly — writes **no** `edition` row, so the date stays
eligible for the next run.

**Rejected:** returning an empty page. A pipeline that silently returns empty
is worse than one that crashes: empty looks like data, and a recorded empty
edition would suppress every future attempt at that date.
