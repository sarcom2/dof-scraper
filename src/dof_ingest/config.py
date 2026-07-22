"""Runtime configuration.

One frozen dataclass, overridable from the environment. No YAML, no config
framework: every knob here is a scalar with a defensible default, and the only
consumer is the CLI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# A real, contactable User-Agent is not decoration. If this scraper ever
# misbehaves, the DOF sysadmin needs a way to tell us instead of blocking the
# whole ASN. Replace the URL/email with yours before running this in anger.
DEFAULT_USER_AGENT = (
    "dof-ingest/0.1 (+https://github.com/sarcom2/dof-scraper; contact: marcel_lara1@hotmail.com)"
)

# Discovery lives on www.dof.gob.mx, enrichment on sidof.segob.gob.mx.
# Both hosts are checked against their own robots.txt (see http.RobotsGate).
INDEX_BASE = "https://www.dof.gob.mx/index_111.php"

# The human-facing note page. We store it as the citable URL but we never
# parse it: it is a shell around an iframe, and its masthead carries a live FX
# ticker plus a citation stamped with today's date (see parse.parse_nota_body).
NOTA_BASE = "https://sidof.segob.gob.mx/notas"

# The iframe target: the actual text of the nota, clean UTF-8, no chrome.
# sidof's robots.txt disallows this exact route for 18 specific note IDs,
# which is what confirms it is the canonical content endpoint.
NOTA_BODY_BASE = "https://sidof.segob.gob.mx/notas/docFuente"

# The DOF publishes up to three editions per day. Passing no `edicion` param
# does NOT mean "all of them" -- it silently returns whichever one the site
# considers current, which on 2026-07-31 was the vespertina (4 notes) instead
# of the matutina (27). Always enumerate explicitly. See docs/RESEARCH.md.
EDITIONS = ("MAT", "VES", "EXT")


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    return default if raw is None else float(raw)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    return default if raw is None else int(raw)


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything tunable, in one place."""

    db_path: Path = Path("data/dof.sqlite3")
    user_agent: str = DEFAULT_USER_AGENT

    # --- politeness -------------------------------------------------------
    # 1 request/second per host. The DOF is a single Apache box serving the
    # whole federal government; there is no upside to going faster and the
    # downside is a block. robots.txt declares no Crawl-delay, so this is our
    # own conservative choice -- RobotsGate will raise it if one ever appears.
    requests_per_second: float = 1.0
    jitter_ratio: float = 0.25  # +/-25% so we don't hit a perfect 1 Hz pattern
    max_concurrency: int = 1  # per host; kept at 1 deliberately

    # --- resilience -------------------------------------------------------
    timeout_s: float = 30.0
    max_attempts: int = 4  # 1 initial + 3 retries
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 60.0
    # Circuit breaker: if the site is down, stop hammering it and fail the run
    # loudly instead of writing a half-empty dataset that looks complete.
    max_consecutive_failures: int = 8

    obey_robots: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            db_path=Path(os.environ.get("DOF_DB", "data/dof.sqlite3")),
            user_agent=os.environ.get("DOF_USER_AGENT", DEFAULT_USER_AGENT),
            requests_per_second=_env_float("DOF_RPS", 1.0),
            timeout_s=_env_float("DOF_TIMEOUT", 30.0),
            max_attempts=_env_int("DOF_MAX_ATTEMPTS", 4),
            obey_robots=os.environ.get("DOF_OBEY_ROBOTS", "1") != "0",
        )
