#!/usr/bin/env bash
# Backfill the DOF in weekly windows, exporting a snapshot after each one.
#
# Bi-temporal semantics: each snapshot is stamped with the EFFECTIVE date of
# the window it loads (--day), while bronze._ingested_at records the real
# processing timestamp. That is how production backfills work -- effective
# time and ingestion time are different clocks, and conflating them is what
# makes reprocessed history lie.
set -euo pipefail
cd "$(dirname "$0")/.."

since="$1"   # e.g. 2026-06-01
until="$2"   # e.g. 2026-08-06

week_start="$since"
while [[ "$week_start" < "$until" || "$week_start" == "$until" ]]; do
    week_end="$(date -j -v+6d -f %F "$week_start" +%F)"
    [[ "$week_end" > "$until" ]] && week_end="$until"

    echo "=== window $week_start .. $week_end ==="
    uv run dof-ingest run --since "$week_start" --until "$week_end" --limit 1000
    uv run dof-lake export --day "$week_end"

    [[ "$week_end" == "$until" ]] && break
    week_start="$(date -j -v+1d -f %F "$week_end" +%F)"
done
echo "=== backfill complete ==="
uv run dof-ingest stats
