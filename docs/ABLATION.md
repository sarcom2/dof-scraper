# What actually moved recall on Mexican legal Spanish

Every number here is reproducible:

```bash
uv run dof-qa ablate          # the table below, ~30s, no model, no network
uv run dof-qa eval --check    # the CI gate
```

Measured on 2026-08-06 against a **27-note corpus** (2026-07-31 matutina, 12
agencies, 504 chunks) and a **51-question golden set**. The corpus is small —
`www.dof.gob.mx` had an expired TLS certificate on the day this was built, so
it could not be grown. Read the caveats at the bottom before generalising any
row.

---

## The table

Each row disables exactly one thing and re-runs the whole golden set.
Retrieval-only, so every number is deterministic.

| Variant | recall@8 | Δ | hit@8 | over-refusal |
|---|---:|---:|---:|---:|
| **baseline (everything on)** | **94.8%** | — | 96.9% | 0.0% |
| without diacritic folding | 88.5% | **−6.2pp** | 90.6% | 5.6% |
| chunks 600 / overlap 80 | 90.9% | −3.9pp | 93.8% | 0.0% |
| without prefix stemming | 91.0% | −3.7pp | 93.8% | 2.8% |
| chunks 2400 / overlap 300 | 94.0% | −0.8pp | 96.9% | 0.0% |
| without acronym expansion | 94.8% | +0.0pp | 96.9% | 0.0% |
| without structured pre-filter | 94.8% | +0.0pp | 96.9% | 0.0% |
| without either of those two | 94.8% | +0.0pp | 96.9% | 0.0% |

---

## What helped

### 1. Diacritic folding — −6.2pp, the largest single lever

`tokenize = "unicode61 remove_diacritics 2"` on the FTS5 table. Users type
`atun`, `informacion`, `indigenas`; the DOF never omits the accent. Without
folding, an accent-less query for an accented term matches nothing at all —
not "worse ranking", *zero results*:

```
remove_diacritics=2:   "atun"      -> [5795172]
remove_diacritics=0:   "atun"      -> []
remove_diacritics=2:   "indigenas" -> [5795177, 5795178]
remove_diacritics=0:   "indigenas" -> []
```

It also drove over-refusal from 0.0% to 5.6%, because a query that matches
nothing hits the `no_hits` gate and the system correctly-but-uselessly
declines a question it should have answered.

### 2. Chunk size — 1200 chars beats 600 by 3.9pp

600-char chunks fragment the evidence: a single provision gets split across two
passages and neither scores well enough to reach the top-k. Going the other way
(2400) costs only 0.8pp, so the curve is asymmetric — **when in doubt, chunk
bigger**. Under-chunking on a legal corpus is the more expensive mistake.

### 3. Prefix stemming — −3.7pp

Spanish inflects by suffix, so truncating query terms longer than 6 characters
to a prefix wildcard (`"export"*`) behaves as a crude stemmer:

```
"¿Qué publicó la Secretaría de Economía sobre exportaciones?"
  with prefix:    [5795170]   <- the note says "exportar"
  without:        []
```

A real Snowball stemmer would be more precise. It is also a new dependency, and
the prefix trick recovers most of the gap, so it stays out until the numbers
say otherwise.

---

## What I expected to help and didn't

This is the part most write-ups omit, so it is the part worth reading.

### Acronym expansion: +0.0pp

I built the `ACRONYMS` table first, convinced it would be the biggest win —
users type `COFEPRIS`, the corpus says `COMISION FEDERAL PARA LA PROTECCION
CONTRA RIESGOS SANITARIOS`, and no lexical index bridges that on its own.

It contributes nothing, and the reason is architectural rather than
linguistic: **the acronym is already resolved upstream.** `extract_agencies`
maps `CFE → comision federal de electricidad` to build the *filter*, so by the
time the query reaches FTS5 the agency constraint is already applied via SQL.
Expanding the acronym a second time on the query side is redundant work on a
constraint that has already been enforced.

The table still earns its place — routing depends on it, and removing it would
break the pre-filter — but it earns it as **routing vocabulary, not as query
expansion**. Two features I had thought of as separate turned out to be one.

### Structured pre-filter: +0.0pp on recall

This one I am *not* prepared to call settled, and the honest reason is corpus
size. With 27 notes from a single day, there is essentially nothing for a
filter to exclude: every note is already a candidate, so restricting to one
agency cannot improve what the top-8 contains.

The mechanism it is meant to protect against — a post-filter decimating a
global top-k down to one or two eligible rows — **cannot occur below k**.
On a corpus of tens of thousands of notes across years, where one agency is
~2% of rows, I expect this to be among the largest effects. Right now the
experiment has no power to detect it, and reporting +0.0pp as "pre-filtering
doesn't matter" would be reading a null result as a negative one.

What the pre-filter *does* already buy, measurably, is **precision and correct
refusals**: `h05` ("¿Qué publicó la Secretaría de Gobernación sobre
asociaciones religiosas?") returns only Gobernación notes, and the COFEPRIS
question refuses with `out_of_coverage` instead of returning topically-adjacent
health-sounding text. Neither shows up in recall@k.

---

## The finding that changed the eval, not the system

The first ablation run reported diacritic folding at **+0.0pp**. That was
wrong, and it was wrong in the most dangerous way — it looked like a clean
negative result.

The cause was the golden set: every question was written with correct accents,
exactly like the corpus, so nothing ever exercised the folding. Adding five
accent-less paraphrases (`a01`–`a05`) *still* showed +0.0pp, because BM25 runs
an OR query and those questions carried enough unaccented content words
(`veda`, `cupo`, `exportar`) to find the right note anyway.

Only single-discriminative-term questions isolated it:

```
{"id": "a06", "question": "que hay sobre atun?",      "gold_codigos": [5795172]}
{"id": "a07", "question": "que hay sobre indigenas?", "gold_codigos": [5795177, 5795178]}
```

With those, folding went from +0.0pp to **−6.2pp** — from "useless" to "the
biggest lever in the system". Nothing about the retrieval code changed.

Two things follow, and they are the reason this section exists:

1. **A null result is a claim about the experiment before it is a claim about
   the system.** The first two rounds measured the golden set's blind spot, not
   the tokenizer.
2. **An OR-based lexical retriever is resilient to individual term misses**,
   which makes single-feature ablation harder than it looks: you have to
   construct queries where the feature under test is load-bearing, or you
   measure nothing.

---

## Why FTS5/BM25 and not embeddings

Not measured — there is no vector baseline in this repo, and I am not going to
claim a win I did not run. The reasoning that led to the choice:

- **Mexican legal Spanish is high-precision lexical.** `acción de
  inconstitucionalidad 79/2025`, `Registro Federal Inmobiliario 25-6254-0`,
  `Thunnus albacares`. Exact identifiers are what users search by, and
  embeddings blur exactly those. `r05` and `u12` are the same question shape
  with different case numbers; a system that cannot tell 79/2025 from 12/2021
  fails one of them.
- **Cost of being wrong is high, cost of a miss is low.** A regulatory-monitoring
  user would rather re-query than be handed a confidently wrong decree.
- **It runs in CI.** Zero dependencies, no GPU, no API key, byte-identical
  results on every machine — which is what makes the whole eval a build gate
  instead of a notebook.

The honest next experiment is a hybrid: BM25 ∪ embeddings fused with the same
RRF already in `retrieve.py`, measured on the same golden set. `retrieve.rrf_fuse`
takes a list of runs precisely so a second retriever can be added without
touching anything else.

---

## Bugs the harness found

Both were silent, and neither would have surfaced from eyeballing answers.

**RRF and BM25 are not the same scale.** The refusal gate thresholded whatever
score ranked the hit. Fusing a second query formulation replaced the calibrated
BM25 value (0–45) with an RRF score (~1/(60+rank) ≈ 0.016), so every fused
query fell under the threshold and was refused — with the correct document
sitting at rank 1. Measured as 24% over-refusal on answerable questions;
invisible in any single spot-check. `Hit` now carries `score` and `bm25`
separately, pinned by `test_rrf_scores_are_not_bm25_scores`.

**The index never reached disk.** `sqlite3.connect()` defaults to
`isolation_level=""`, which opens an implicit transaction before DML.
`executescript` (used for the DDL) issues its own COMMIT, so the *tables*
persisted while every chunk INSERT after them was discarded on close.
`build()` reported `indexed=27 chunks=504` — true inside the process, gone
from the file. The next process found an empty index and the eval scored 6.7%.
Pinned by `test_index_writes_survive_the_connection`, which verifies through a
second connection.

Both are the failure mode this repo keeps running into: **the operation
reports success and the data is not there.**

---

## Generation: what a model adds, and what it costs

Retrieval is identical across providers (deterministic), so this table isolates
the generation layer. Same corpus, same 51 questions.

| | extractive | `gemma4:12b` (local) |
|---|---:|---:|
| refusal on unanswerable | 53.3% | **100%** |
| over-refusal on answerable | **0.0%** | 11.1% |
| citation precision | 73.6% | **82.0%** |
| citation recall | 76.9% | **80.7%** |
| citation F1 | 75.2% | **81.4%** |

**Semantic refusal is the thing a model buys.** The gates can only catch
structural impossibility. Seven questions require reading:

| id | Why no gate could catch it |
|---|---|
| `u12` | Right court, right document type, **wrong case number** (12/2021 vs 79/2025). BM25 = 19.67, higher than several answerable questions |
| `u11` | Agency is in the corpus, topic is not (CFE / nuclear energy) |
| `u10` | Topically adjacent to the agriculture notes without being answerable by them |
| `u02`, `u07`, `u08` | Health-regulatory vocabulary that shares stems with real notes |
| `u05` | Asks for a forecast; the DOF does not publish forecasts |

`u12` is the load-bearing case for the whole "why lexical retrieval needs a
reader on top" argument. Its BM25 score sits *above* several correct answers,
so no threshold anywhere separates it. This is also why `MIN_SCORE` stays low
(0.5) and functions as a did-anything-match gate rather than a relevance
judgement — the score distributions of answerable (2.4–45.5) and unanswerable
(4.5–19.7) questions overlap almost completely.

**The cost is 11.1% over-refusal** — 4 of 36. Naming them matters, because two
of the four are not model errors:

- `r02` / `a02` (sugar quota): retrieval put the correct note at rank 1 and the
  model replied *"the excerpts state a quota was published but do not contain
  the figure."* Reading the chunk, that is **correct** — the number lives
  elsewhere in the note. A chunking limitation, correctly surfaced as a
  refusal instead of an invented number.
- `a07` (*"que hay sobre indigenas?"*): retrieved both correct notes, declined
  anyway. A genuine over-refusal on a vague question.
- `h02`: retrieved 3 of 4 correct notes, declined.

So the honest reading of 11.1% is closer to ~5.5% model over-refusal plus
~5.5% retrieval depth surfacing as refusal. Splitting those apart is the next
measurement, not a claim I can make from this table.

**Citations improve too** (75.2% → 81.4% F1). The extractive baseline cites its
top 3 passages whether or not all three carry the answer, which caps its
precision; the model cites what it used. 24 of 32 answers have perfectly
precise citations.

**One failure the schema caught.** `gemma4:12b` returns citations as *positions*
(`[1, 2]`) rather than note codes when the prompt does not spell out the
difference. The validator rejected the answer, the eval scored it as a refusal,
and the fix was a prompt line naming the failure explicitly. A regex-based
citation parser would have accepted `[1, 2]` as valid output.

---

## Caveats

- **27 notes, one day, 12 agencies.** Recall@8 of 94.8% on a corpus this size
  is a smoke test, not a benchmark. Several questions have only one correct
  note and eight slots to find it in.
- **No COFEPRIS coverage.** The health-regulatory questions this system is
  aimed at (`u01`, `u02`, `u08`, `u09`) are currently scored as *correct
  refusals*, which is honest but is not the same as answering them. They
  become answerable questions the moment the crawl reaches that content, and
  the harness will start scoring them automatically.
- **Refusal on unanswerable questions caps at 53.3% in CI**, because
  retrieval-only mode can refuse only at the gates. Semantic refusal — "right
  agency, right document type, wrong case number" (`u12`) — requires
  generation; see the generation numbers in the README.
- **Single run per variant.** Retrieval is deterministic so there is no
  variance to average. Generation is not: `gemma4:12b` runs at `temperature 0`
  with schema-constrained decoding, which makes it close to reproducible but
  not guaranteed. One run, not an average.
- **One local model, no API comparison.** The `anthropic` provider exists and
  is wired to the same harness, but the numbers above are `gemma4:12b` only.
  Publishing a Claude-vs-local table would need an API key and a budget; the
  code path is there for whoever wants to run it.
