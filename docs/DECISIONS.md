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

---

## ADR-10 — FTS5/BM25 instead of embeddings

**Status:** accepted, and untested against the alternative.

Mexican legal Spanish is a high-precision lexical domain. Users search by exact
identifier — `acción de inconstitucionalidad 79/2025`, `Registro Federal
Inmobiliario 25-6254-0`, `Thunnus albacares` — and those are exactly what
embeddings blur. Golden questions `r05` and `u12` are the same shape with
different case numbers; a retriever that can't tell 79/2025 from 12/2021 gets
one of them wrong.

FTS5 also costs zero dependencies, needs no GPU or API key, and returns
byte-identical results on every machine. That's what lets the eval be a build
gate instead of a notebook.

**Rejected:** a vector store. Not because I measured it and it lost — I didn't
measure it, and I'm not claiming a win I didn't run. The reasoning above is why
I started lexical.

**Revisit when:** questions start arriving as paraphrases with no shared
vocabulary. The honest next experiment is hybrid — BM25 ∪ embeddings fused with
the RRF already in `retrieve.py`, on the same golden set. `rrf_fuse` takes a
list of runs precisely so a second retriever drops in without touching anything
else.

---

## ADR-11 — Deterministic routing, no model in the loop

**Status:** accepted.

The route (SQL / retrieval / hybrid) is decided by regexes and a vocabulary
table, not by an LLM.

Counting questions go to SQL because counting is arithmetic. A RAG system that
answers "¿cuántas notas publicó la SHCP?" by summarising retrieved passages is
guessing at a number it could have computed exactly, and it will be confidently
wrong at some corpus size.

**Rejected:** an LLM router. It would be non-deterministic, untestable in CI,
and able to fail in new ways between runs on identical input. Routing accuracy
is currently 100% on the golden set; that number means something because the
component can't drift.

**Consequence:** the router only knows vocabulary it's been given. `ACRONYMS`
and `SYNONYMS` are maintained by hand, and an unlisted agency won't produce a
filter.

**Revisit when:** the hand-maintained vocabulary becomes the bottleneck — the
symptom is routing accuracy dropping while retrieval stays fine.

---

## ADR-12 — The structured predicate is a pre-filter, not a post-filter

**Status:** accepted; the recall benefit is not yet measurable.

Agency and date constrain the candidate set *before* BM25 ranks it, via a JOIN
against `nota`. Post-filtering would pick the global top-k and then throw most
of it away: ask for k=8 and keep one.

**The ablation reports +0.0pp**, and I'm calling that a null result rather than
a negative one. With 27 notes from a single day there's nothing for a filter to
exclude — the decimation it prevents can't happen below k. On a corpus of tens
of thousands of notes where one agency is ~2% of rows, I expect this to be one
of the largest effects. Right now the experiment has no power to detect it.

What it already buys, measurably: precision, and refusals for the right reason.
The COFEPRIS question refuses with `out_of_coverage` instead of returning
topically-adjacent health-sounding text.

**Revisit when:** the corpus spans years. Then the number will mean something.

---

## ADR-13 — Refuse at the gate, not only at the model

**Status:** accepted.

Three refusals happen before any model sees anything: the question's date range
falls outside the corpus, its agency has no notes, or nothing matched above the
BM25 floor. Each names its own reason.

"No sé" and "el corpus sólo cubre 2026-07-31" are different answers. The second
tells the user the question was fine and the data is the gap — and it costs
nothing to produce.

**Rejected:** letting the model decide everything. Handing weak context to an
LLM is how ungrounded answers get generated: it will use what it's given.

**Consequence:** gate-level refusal caps at 53.3% on the golden set, because
catching "right agency, right document type, wrong case number" (`u12`) needs a
model. CI measures the gate floor; the generation numbers are reported
separately.

**Note the score-scale trap:** the BM25 floor must be applied to `Hit.bm25`,
never `Hit.score`. Fusing a second query formulation replaces the calibrated
BM25 value with an RRF score around 0.016, so the gate refuses everything with
the right document at rank 1. Pinned by
`test_rrf_scores_are_not_bm25_scores`.

---

## ADR-14 — Citations validated against what the model was shown

**Status:** accepted.

Answers are produced under a JSON schema, and every cited `codigo` is checked
against the passages actually in the context. Codes that weren't there are
stripped; if nothing survives, the answer is discarded and becomes a refusal.

This isn't hypothetical. `gemma4:12b` cited `[1, 2]` — the *positions* of the
excerpts rather than their note codes. Prose parsing would have accepted it.

**Rejected:** extracting citations from free text with a regex. A citation you
parsed out of prose is a citation you can't trust, and trustworthy citations
are the entire product.

**Consequence:** a model that cites badly gets a refusal rather than a wrong
answer. That's the correct trade for a legal corpus, and it shows up honestly
in the over-refusal metric instead of hiding in the citation score.

---

## ADR-15 — The evaluation corpus is committed

**Status:** accepted.

`tests/fixtures/corpus.jsonl` holds the 27 notes the published numbers were
measured on, and `scripts/seed_corpus.py` replays it. A clean clone reproduces
94.8% recall@8 exactly.

**Rejected:** re-crawling in CI. It would make the build depend on a government
website being reachable — and on the day this was written, `www.dof.gob.mx` had
an expired TLS certificate. A test that fails because SEGOB let a cert lapse is
a test that gets disabled.

**Consequence:** ~540 KB of public government text in git. Worth it: eval
results nobody else can reproduce aren't results.

---

## ADR-16 — Spark for a corpus that fits in SQLite

**Status:** accepted, with the reason stated plainly.

The corpus does not need Spark for volume — it is megabytes, and ADR-5 chose
SQLite for exactly that reason. The lakehouse layer (`dof_lake`) exists
because the DOF's *amendment semantics* map onto three things a Delta
lakehouse does natively and a row store doesn't:

1. **MERGE-based idempotent loads.** The scraper's three-outcome upsert
   (insert / touch / version-bump) is re-implemented as Delta MERGE, proving
   the idempotency contract is a property of the design, not of SQLite.
2. **SCD Type 2.** The DOF corrects published notices (fe de erratas) and the
   scraper already tracks that with `revision` + content hashes. Silver turns
   that change feed into one row per (codigo, revision) with
   `[valid_from, valid_to)` brackets, so "what did this decree say before the
   correction?" is a query, not an excavation.
3. **Time travel.** "What did the corpus look like on date X?" — the same
   audit-trail ethos as the run ledger, one layer up.

The version key is the scraper's own `revision`, which only moves when a
content hash moves. That makes the merge deterministic with no watermark
table: re-observed revisions anti-join away. Two invariants (one live row per
codigo; no closed row with `valid_to NULL`) are re-derived from the data
after every merge, so consistency is proven, not assumed.

**Rejected:**
- *Spark in the crawler.* The crawl is sequential and rate-limited at 1 req/s
  as a politeness contract (ADR-5's spirit). Parallelising it would break the
  promise the project is built on.
- *The single-statement "UNION ALL with NULL merge key" SCD-2 trick.* Saves
  one scan, costs comprehensibility. Two explicit steps — close the live row,
  append the new versions — say what they do.
- *Watermark-based incrementality.* A watermark breaks under out-of-order
  backfills; source-assigned monotonic revisions don't.
- *Pretending this is big data.* It isn't, and the README says so. The
  portable part is the change-capture pattern; at 1000× the volume the same
  code runs unchanged, which is the actual claim.

**Consequence:** everything runs locally (`pyspark` + `delta-spark`, JDK 17)
and the identical code deploys to Databricks Free Edition via the Asset
Bundle in `databricks.yml`. The scraper never runs on Databricks — only
snapshots travel — because the DOF rate limit should be honoured from a
residential ASN, not a datacenter one.

**Revisit when:** the corpus reaches the tens of gigabytes, at which point
silver's full-table invariant pass should become an incremental merge.
