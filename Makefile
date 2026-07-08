.PHONY: install test lint fmt check research demo clean index ask eval ablate

install:            ## install deps into .venv
	uv sync

test:               ## offline test suite
	uv run pytest

lint:               ## ruff + mypy --strict
	uv run ruff check .
	uv run mypy

fmt:                ## autofix
	uv run ruff check --fix .
	uv run ruff format src tests

check: lint test    ## everything CI runs

research:           ## re-probe for an official API / bulk download
	uv run dof-ingest research

demo:               ## prove idempotency end to end (hits the live site, ~40s)
	@rm -f data/demo.sqlite3*
	@echo "=== run 1 ==="
	DOF_DB=data/demo.sqlite3 uv run dof-ingest discover --since 2026-07-29 --until 2026-07-31
	@echo "=== run 2 (same window: expect inserted=0) ==="
	DOF_DB=data/demo.sqlite3 uv run dof-ingest discover --since 2026-07-29 --until 2026-07-31
	DOF_DB=data/demo.sqlite3 uv run dof-ingest stats

index:              ## build the chunk + FTS5 index over the corpus
	uv run dof-qa index

eval:               ## run the golden set (retrieval only, no model needed)
	uv run dof-qa eval --no-generate --check

eval-local:         ## run the golden set with the local model via ollama
	uv run dof-qa eval --provider ollama --out eval/report-ollama.json

ablate:             ## what actually moved recall, and what didn't
	uv run dof-qa ablate

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache data/demo.sqlite3*
