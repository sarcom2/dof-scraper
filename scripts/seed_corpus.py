"""Rebuild the evaluation corpus from a committed fixture, with no network.

The eval gate needs real notes to retrieve over. Re-crawling in CI would make
the build depend on a government website being reachable — exactly the kind of
test nobody trusts and everybody eventually disables. So the corpus the
ablation numbers were measured on is committed as `tests/fixtures/corpus.jsonl`
and replayed through the real store and the real indexer.

That makes the published numbers reproducible by anyone who clones the repo,
which is the only version of "here are my eval results" worth publishing.

The content is public information published by the Mexican federal government;
see the licensing note in the README.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dof_ingest.models import Nota  # noqa: E402
from dof_ingest.store import Store  # noqa: E402
from dof_qa.index import build  # noqa: E402

CORPUS = ROOT / "tests" / "fixtures" / "corpus.jsonl"


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/ci.sqlite3")
    rows = [json.loads(line) for line in CORPUS.read_text("utf-8").splitlines() if line.strip()]

    with Store(out) as store:
        for row in rows:
            store.upsert_nota(
                Nota(
                    codigo=int(row["codigo"]), fecha=row["fecha"], edicion=row["edicion"],
                    titulo=row["titulo"], organismo=row["organismo"],
                    poder=row.get("poder", ""), seccion=row.get("seccion", ""),
                    url_detalle=row.get("url_detalle", ""),
                )
            )
            if row.get("body_text"):
                store.set_body(int(row["codigo"]), "ok", row["body_text"])
        counters = build(store.conn)

    print(f"seeded {len(rows)} notas, {counters['chunks']} chunks -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
