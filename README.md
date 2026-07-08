# dof-ingest

*Built: July – August 2026*

An idempotent, robots-aware ingestion pipeline for Mexico's **Diario Oficial de
la Federación** — the federal register that publishes laws, decrees and official
notices.

It scrapes DOF notices (date, edition, section, branch of government, issuing
agency, title and full legal text) into SQLite. Running it twice over the same
dates writes nothing the second time.

```bash
uv sync
uv run dof-ingest research                                  # start here
uv run dof-ingest run --last-days 3
uv run dof-ingest stats
```

---

## 1. I checked for an official API first

I didn’t want to scrape if I didn’t have to. Before writing a parser I checked
the API, CKAN, the sitemap, RSS and the per-edition PDFs. `dof-ingest research`
reruns those checks live, so the README is not the only evidence:

```bash
uv run dof-ingest research
```

| Door | Question | Verdict |
|---|---|---|
| **SIDOF WebService** `/dof/sidof/diarios/{fecha}` | The officially documented bulk endpoint | **Broken.** Answers `200 OK` with `ListaDiarios: []` for every date tested, while the HTML index for the same date lists 27 notices |
| **SIDOF WebService** `/dof/sidof/indicadores/{fecha}` | Is the API host even up? | Works. Which is what proves the endpoint above is broken and not my network |
| **datos.gob.mx** (CKAN 2.11) | National open-data portal | The only DOF dataset is *"Resumen del DOF"*, published by **SENASICA** — one monthly CSV covering that one agency's own notices, last refreshed December 2025. Not the corpus |
| **`sitemap.xml`** | Enumerate note URLs | 586 URLs, all `lastmod 2007`, all pointing at `diariooficial.segob.gob.mx` — a host decommissioned years ago. Static pages only |
| **`rss.xml`** | A feed to poll | `404` |
| **Per-edition PDFs** | Bulk download | Exist, but they are typeset documents with no structured metadata — you would have to parse them to get back what the HTML index already gives you |

I also **rejected CompraNet as the target for this project** on the same
principle: its contracting data is published as bulk OCDS at
[contratacionesabiertas.mx](https://www.contratacionesabiertas.mx/). Scraping a
portal that offers a bulk download is the wrong answer regardless of how good
the scraper is.

`research` probes live and picks the most recent published date it can find,
so the results don’t go stale. If SEGOB ever fixes `/diarios`, it prints this
and you can bin the scraper:

```
=> No endpoint returns the corpus. Scraping the HTML index is the
   remaining option -- as a last resort, within robots.txt.
```

Full evidence, with raw responses: **[docs/RESEARCH.md](docs/RESEARCH.md)**.

---

## 2. robots.txt shaped the pipeline

`www.dof.gob.mx/robots.txt` disallows `/nota_detalle.php?` and
`/nota_to_doc.php?` — **the entire detail layer of the site**. The obvious
design (crawl the index, follow each link) is off limits.

The same notice is also served by `sidof.segob.gob.mx`, whose robots.txt
*allows* individual notes, excluding 18 specific IDs. The pipeline uses two
hosts because that is the only legal path the publisher left:

```
  www.dof.gob.mx                             sidof.segob.gob.mx
  ──────────────                             ──────────────────
  index_111.php?year&month&day&edicion       /notas/{codigo}           (landing, unused)
        │  ALLOWED                                  │
        │                                    /notas/docFuente/{codigo} (the text)
        ▼                                           ▲  ALLOWED, minus 18 IDs
   discover ──── codigo, titulo, organismo ───► enrich
        │                                           │
        └──────────────► SQLite ◄───────────────────┘

  /nota_detalle.php?  ✗ DISALLOWED — never requested
  /nota_to_doc.php?   ✗ DISALLOWED — never requested
```

Those 18 blocked notes still return **HTTP 200**, but robots.txt says no, so
we skip them. We store them as `robots_denied` so the gap is visible, not
hidden.

```bash
$ uv run dof-ingest robots "https://sidof.segob.gob.mx/notas/docFuente/5381640"
  verdict    : DISALLOWED
$ echo $?
1
```

`enrich` checks both URLs against robots before fetching either. A publisher
blocking one route means the resource is off limits, not just that particular
URL.

---

## 3. Idempotent ingestion

The unit of identity is the DOF's own `codigo`, used directly as the primary
key. There is no surrogate key that could differ between two runs, so a re-run
is structurally incapable of duplicating a row.

Every upsert has exactly three outcomes:

| Condition | Action |
|---|---|
| never seen | `INSERT`, `revision = 1` |
| seen, hash equal | touch `last_seen_at`, **nothing else** |
| seen, hash differs | `UPDATE`, `revision += 1`, append field diffs to `nota_revision`, move `last_changed_at` |

Splitting `last_seen_at` from `last_changed_at` matters: "we looked" and "it
changed" are different events. That makes "no duplicates" provable, not just
claimed — the rows aren't simply left alone, you can show they were untouched.

Real output, two runs over the same three days:

```
$ dof-ingest discover --since 2026-07-29 --until 2026-07-31
  inserted=73 updated=0 unchanged=0  skipped_robots=0 no_edition=5 errors=0
  http: 10 requests, 0 retries, 563 KiB, 1.5s spent being polite

$ dof-ingest discover --since 2026-07-29 --until 2026-07-31     # same window
  inserted=0  updated=0 unchanged=73 skipped_robots=0 no_edition=5 errors=0
  http: 10 requests, 0 retries, 563 KiB, 1.3s spent being polite
```

Note the second run **re-fetched and re-parsed everything** and still wrote
nothing. That is the harder claim; skipping the fetch would be the easy one.
It is asserted in
[`tests/test_pipeline.py`](tests/test_pipeline.py), not just demonstrated here.

Every run is recorded in a `run` ledger, so the numbers above are queryable
history rather than a screenshot:

```
$ dof-ingest stats
  notas                           94
  notas con texto                 94
  rango                           2015-02-11 .. 2026-07-31
  organismos                      26

  últimas corridas:
     id  comando                              ins   upd   unch  robots  err
      2  discover 2026-07-29..2026-07-31 MA     0     0     73       0    0
      1  discover 2026-07-29..2026-07-31 MA    73     0      0       0    0
```

**Bounded re-crawling.** `--recheck-days N` (default 7) re-visits recent
editions because the DOF does amend them after publication (*fe de erratas*,
withdrawals). Outside that window, already-recorded editions are skipped
entirely — otherwise "incremental" just means "re-crawl everything, forever".

---

## 4. Content hashing

`content_hash` is **not** `sha256(response.content)`. Hashing the raw page
would report a change on every single run, because both DOF surfaces embed
volatile chrome in every response:

- the note page renders a live FX/UDIS ticker (`DÓLAR 17.3213  UDIS 8.844190`);
- its citation box is stamped with **today's** date (`[citado el 01-08-2026]`);
- `www.dof.gob.mx` issues a fresh `DOF_WEB` session cookie per request.

A change detector that fires on every record every day is worse than none — it
looks like it's working when it isn't.

So the hash covers a canonical JSON projection of the *business* fields:

- **NFC-normalised** — the DOF mixes precomposed and decomposed accents
  depending on which editor produced the source document;
- **whitespace-collapsed** — the HTML is hand-formatted and re-indented at
  random;
- **`url_origen` excluded** — rediscovering a notice through a different index
  URL is not a content change;
- **sorted keys** — field order can never affect the digest.

Index metadata and full text get **separate** hashes, because the DOF amends
the text of a published notice without touching its title, and we want to
notice that.

---

## 5. Rate limiting and backoff

Every outbound request goes through `PoliteClient.get`. There is no second code
path — the only way a politeness policy holds is if it cannot be bypassed.

- **1 req/s per host, concurrency 1.** The DOF is one Apache box serving the
  federal government. There is no upside to going faster.
- **±25% jitter.** A perfectly periodic 1 Hz request pattern fingerprints a bot
  in an access log at a glance.
- **`Crawl-delay` honoured** when stricter than our default —
  `datos.gob.mx` declares `Crawl-Delay: 10` and gets it.
- **Full jitter backoff**, `uniform(0, min(cap, base·2ⁿ))`. Not equal jitter:
  when several workers back off together, equal jitter keeps them clustered and
  they re-collide.
- **`Retry-After` wins.** The server is telling you what it wants; ignoring it
  is how you get banned.
- **4xx is never retried** (except 429). Retrying a 404 turns one mistake into
  a ban.
- **Circuit breaker.** After 8 consecutive failures the run aborts loudly
  rather than writing a half-empty dataset that looks complete.
- **A real, contactable User-Agent**, so a sysadmin can email you instead of
  blocking your ASN.

Every second spent waiting is counted and reported (`1.5s spent being polite`),
because politeness you can't measure is politeness you'll quietly drop under
deadline.

---

## 6. The ugly cases

Public data is messy. I ran into four silent failures — the kind that make the
corpus look fine when it isn't.

### 6.1 A flat table, and decoys identical to the signal

The index is one long `<table>` where the hierarchy
(section → branch → agency → notice) is **not** expressed by nesting. Every
level is a sibling `<tr>` distinguished only by a CSS class. The agency a
notice belongs to is not reachable from the notice's own node — you have to
walk rows in document order and carry state.

Worse, the agency header row carries a "Ver WORD" link that exports the whole
agency block:

```html
<td class="subtitle_azul">SECRETARIA DE HACIENDA Y CREDITO PUBLICO
  <a href="/nota_to_doc.php?codnota=5795216"><img alt="Ver WORD"></a></td>
<td><a class="enlaces" href="/nota_detalle.php?codigo=5795217&...">Acuerdo...</a></td>
```

`codnota=5795216` is allocated from the same sequence as real notice codes and
is **indistinguishable from one by value**. On the 2026-07-31 vespertina:

| Approach | Codes found | Correct? |
|---|---|---|
| `re.findall(r"\b5\d{6}\b")` | 6 | ✗ 50% false positives |
| `re.findall(r"codnota=(\d+)")` | 6 | ✗ every real notice has one too |
| **structural walk on link target** | **4** | ✓ |

Regex won't tell them apart; only the DOM position will. The rejected codes go
into `doc_only_codes` so the run report shows what was dropped and why — no need
to trust me blindly.

### 6.2 `200 OK` that means "no"

Ask for an edition that doesn't exist — any Sunday, or an `EXT` on a day
without one — and `index_111.php` answers **`200 OK` with the site's
homepage**. Not a 404, not an empty index. Trust the 200 and you record a
successful crawl of nothing.

Detected positively (no `Fecha: dd/mm/yyyy` header anywhere) and stored as
`no_edition`, which is a real state, distinct from "we failed to read it".

And the inverse canary: **if the raw HTML contains `nota_detalle.php?codigo=`
but the structural walk matched none, that's a `ParseError`, not an empty day.**
A pipeline that silently returns empty is worse than one that crashes: empty
looks like data.

### 6.3 A robots.txt parser that grants access to disallowed pages

This one is a safety bug, and it's in the standard library.

`www.dof.gob.mx/robots.txt` starts:

```
User-agent: *
#
                      ← blank line
Disallow: /nota_detalle.php?
Disallow: /nota_to_doc.php?
```

`urllib.robotparser` implements the 1996 draft, where a blank line *terminates*
a group. It sees the blank line, resets state, and **silently discards every
rule that follows**:

```python
>>> rp.parse(open("robots.txt").read().splitlines())
>>> len(rp.entries)
1                    # only the AdsBot-Google group survived
>>> rp.can_fetch("*", "https://www.dof.gob.mx/nota_detalle.php?codigo=1")
True                 # a page the DOF explicitly disallows
```

Google and RFC 9309 ignore blank lines inside a group, so real crawlers read the
file the way SEGOB intended and nobody there noticed. Use the stdlib blindly and
it gives you permission to crawl URLs the publisher explicitly blocked, while
your logs still claim you follow robots.txt.

`normalize_robots()` fixes the *input* (drop comments and blanks, re-insert
group separators per RFC 9309 §2.2.1) rather than reimplementing the matcher,
which is fine — only the grouping is 30 years out of date. Both behaviours are
pinned in [`tests/test_robots.py`](tests/test_robots.py), so if a future Python
fixes this we find out from a failing test.

### 6.4 Two other traps worth naming

**Contradictory charsets.** One response declares
`<meta ... charset=ISO-8859-1>` *and* `<meta charset="UTF-8">`, with the wrong
one first, while the HTTP header correctly says UTF-8 and the bytes are valid
UTF-8. Anything trusting the document's first declaration produces `FederaciÃ³n`
in every record. Order of trust: HTTP header → strict UTF-8 → cp1252 (not
latin-1: these pages carry `0x91`–`0x94` smart quotes).

**A broken TLS chain.** `www.dof.gob.mx` sends its leaf certificate **twice**
and omits the GoDaddy G2 intermediate; `www.datos.gob.mx` omits Let's Encrypt
E8. curl follows the AIA extension and finds the missing intermediate; OpenSSL
doesn’t, so Python fails. The usual scraping workaround is `verify=False`, which
turns off TLS verification for every host just because one server forgot its
chain. We ship the two public intermediates from their AIA URLs instead, so TLS
stays on.
See [`src/dof_ingest/certs/README.md`](src/dof_ingest/certs/README.md).

### 6.5 One more, discovered while testing

The `edicion` parameter is not optional. Omitting it does **not** mean "all
editions" — on 2026-07-31 the bare URL returned the *vespertina* (4 notices)
and silently hid the *matutina* (27). The pipeline always enumerates editions
explicitly; pinned by `test_edition_param_is_not_optional`.

---

## 7. Asking the corpus questions — `dof-qa`

The scraper produces a queryable corpus of Mexican federal legal text. This is
the part that reads it: ask a question in Spanish, get an answer with a
citation to every note it used — or an explicit refusal.

```bash
uv run dof-qa index
uv run dof-qa ask "¿Qué publicó la SHCP sobre inmuebles federales este trimestre?"
uv run dof-qa eval --check      # the golden set, as a build gate
uv run dof-qa ablate            # what actually moved recall
```

**The eval harness is the point, not the agent.** A retrieval system without a
published evaluation is a demo. The measurements and the honest negative
results are in **[docs/ABLATION.md](docs/ABLATION.md)**; the corpus they were
measured on is committed, so the numbers reproduce from a clean clone.

### Routing is the actual engineering

The corpus is hybrid — strict structured metadata *and* unstructured legal
text — so questions need different machinery:

| Question | Route | Why |
|---|---|---|
| *¿Cuántas notas publicó la SHCP en julio?* | **SQL** | Counting is arithmetic. A RAG system that answers this by summarising retrieved passages is guessing at a number it could have computed |
| *¿Qué dice el DOF sobre dispositivos médicos?* | **retrieval** | No structured predicate to apply |
| *¿Qué publicó COFEPRIS sobre registros sanitarios este trimestre?* | **hybrid** | Agency and date become a **pre-filter** on the candidate set, not a post-filter on the results |

Pre- versus post-filtering is the load-bearing detail. Post-filtering picks the
global top-k and then decimates it; ask for k=8 and you may keep one.
Pre-filtering picks k=8 *within* the eligible set.

No model decides the route. Routing is deterministic, unit-tested, and scored
separately — currently **100%** on the golden set — because it is the component
most likely to regress silently.

### Groundedness is enforced, not requested

- Answers are produced under a **JSON schema** (`output_config.format` on
  Claude, `format` on ollama), so citations are structurally guaranteed rather
  than regex-extracted from prose.
- **Every cited code is checked against the passages the model was actually
  shown.** A citation to a note that was not in the context is a fabrication
  and the answer is discarded. This is not hypothetical — `gemma4:12b` cited
  `[1, 2]`, the *positions* of the excerpts rather than their note codes, and
  the validator caught it.
- **Refusals name the limitation.** "No sé" and "the corpus only covers
  2026-07-31" are different answers, and the second one tells the user the
  question was fine and the data is the gap.

```
$ dof-qa ask "¿Qué publicó COFEPRIS sobre registros sanitarios este trimestre?"
  strategy=hybrid | organismo~comision federal para la proteccion contra riesgos
  sanitarios, fecha 2026-07-01..2026-09-30 | terms=['registros','sanitarios',...]

  NO SÉ — No hay notas de ese organismo en el corpus (27 notas indexadas).
$ echo $?
1
```

### The metrics, and why each one is there

Measured over **51 hand-verified golden questions** (36 answerable, 15
deliberately unanswerable), each pinning its own `today` so relative dates like
*este trimestre* are reproducible:

| Metric | Result | What it catches |
|---|---:|---|
| `recall@8` | **94.8%** | Did retrieval put the right notes in front of the model? Deterministic — runs in CI with no model |
| `hit_rate@8` | 96.9% | At least one correct note surfaced |
| `routing_accuracy` | 100% | SQL / retrieval / hybrid decision |
| `refusal_rate_unanswerable` | 53.3% (gates only) | Does it say "no" when it should? |
| `over_refusal_rate_answerable` | **0.0%** | The counterweight — refusing everything scores 100% on the line above |

Reporting a refusal rate without its over-refusal counterweight is the easiest
way to publish a flattering lie, which is why they sit next to each other.

**Two bugs the harness found that no spot-check would have.** Fusing a second
query formulation swapped a calibrated BM25 score (0–45) for an RRF score
(≈0.016), so the relevance gate refused *every* fused query with the correct
document at rank 1 — 24% over-refusal, invisible one question at a time. And
the chunk index never reached disk: Python's `sqlite3` default isolation level
committed the DDL and discarded every INSERT, while `build()` reported
`indexed=27 chunks=504`. Both are pinned by regression tests. Both are the same
failure this repo keeps meeting: *the operation reports success and the data is
not there.*

### Providers

Generation is pluggable and **optional** — the retrieval layer and the entire
eval harness run with no model at all:

| Provider | Use |
|---|---|
| `extractive` (default) | No model. Quotes passages with their citations. Cannot hallucinate, runs in CI, and is the baseline any generated answer must beat (citation F1 **70.7%**) |
| `ollama[:model]` | Local, free, offline. Defaults to `gemma4:12b` |
| `anthropic[:model]` | Claude via the official SDK. `uv sync --extra anthropic` |

---

## Data model

```
edition(fecha, edicion) ──1:N──> nota(codigo) ──1:N──> nota_revision
                                    │
run  (append-only ledger)           └── body_text, body_hash, body_status
```

| Table | Purpose |
|---|---|
| `nota` | one row per notice, keyed by the DOF's `codigo`; separate hashes for index metadata and full text |
| `nota_revision` | append-only field-level diffs — *when did this decree's title change, and to what?* |
| `edition` | per (date, edition) crawl state: `ok` / `no_edition`, counts, page hash |
| `run` | per-run counters and HTTP stats — the evidence behind the idempotency claim |

Details and the full DDL: [`src/dof_ingest/store.py`](src/dof_ingest/store.py).

---

## CLI

```
dof-ingest research                      probe for an API / bulk download first
dof-ingest discover --last-days 7        crawl index pages, record notices
dof-ingest enrich   --limit 200          fetch full text of pending notices
dof-ingest run      --last-days 7        both
dof-ingest stats                         what's in the store + run history
dof-ingest export   --format jsonl --out data/notas.jsonl
dof-ingest robots   <url>                explain the robots.txt verdict (exit 1 if denied)

dof-qa index                             build the chunk + FTS5 index (idempotent)
dof-qa ask "<pregunta>"                  answer with citations, or refuse (exit 1)
dof-qa eval --check                      run the golden set as a build gate
dof-qa ablate                            what moved recall, and what didn't
```

Global: `--db PATH`, `-v`, `--log-json` (structured logs for CI/schedulers).
Environment: `DOF_DB`, `DOF_RPS`, `DOF_TIMEOUT`, `DOF_MAX_ATTEMPTS`,
`DOF_USER_AGENT`.

---

## Development

```bash
make install     # uv sync
make test        # 51 tests, fully offline
make lint        # ruff + mypy --strict
make check       # all of the above
```

Every fixture is a raw response saved on 2026-08-01, including both robots.txt
files. The suite runs offline — I don’t want a test failing because a government
site is down. CI runs it on Python 3.11, 3.12 and 3.13.

---

## What I left out

I left some things out on purpose. Here's why:

| Not built | Why | When I'd add it |
|---|---|---|
| Postgres | one writer, a few hundred thousand text rows; SQLite + WAL covers it | a second concurrent writer, or a consumer that needs network access |
| Async / concurrency | we're voluntarily capped at 1 req/s — parallelism would buy nothing we're allowed to use | never, unless the DOF publishes a rate limit that permits it |
| Celery / Airflow | `discover` and `enrich` are independently resumable and idempotent; `cron` plus the run ledger is the whole orchestrator | when this feeds something with real SLAs |
| Typer / Click | seven subcommands of scalar options is what `argparse` is for | when the CLI grows shared option groups worth abstracting |
| `tenacity` | the retry policy *is* the interesting part; hiding it in a decorator hides the reasoning | never — this one should stay visible |
| Full-text search | SQLite FTS5 is one `CREATE VIRTUAL TABLE` away once there's a consumer asking for it | when someone actually queries the corpus |
| PDF extraction | the HTML `docFuente` route has the full text; the PDFs are the same content, typeset | if the DOF ever publishes PDF-only notices |

---

## Legal and ethical notes

DOF content is public information published by the Mexican federal government.
This pipeline reads only what is publicly served, obeys both hosts' robots.txt
(including the 18 notices it blocks by name), identifies itself with a
contactable User-Agent, and requests at 1 req/s with no concurrency. It does
not authenticate, does not bypass any access control, and stores nothing that
isn't already public.

Before running it under your own name, set `DOF_USER_AGENT` to something that
points at you.

## License

MIT — see [LICENSE](LICENSE).
