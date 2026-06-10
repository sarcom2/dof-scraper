"""Polite HTTP: robots.txt gate, per-host rate limiting, retries with backoff.

Every outbound request in this project goes through `PoliteClient.get`. There
is no second code path, which is the only way a politeness policy actually
holds: if it is possible to bypass it, someone eventually will.
"""

from __future__ import annotations

import logging
import random
import ssl
import time
import urllib.robotparser
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import certifi
import httpx

from .config import Settings

log = logging.getLogger(__name__)

CERT_DIR = Path(__file__).parent / "certs"


def build_ssl_context(extra_dir: Path = CERT_DIR) -> ssl.SSLContext:
    """Default verification, plus the intermediates the DOF forgets to send.

    `www.dof.gob.mx` presents its leaf certificate twice and never sends the
    Go Daddy G2 intermediate; `www.datos.gob.mx` omits the Let's Encrypt E8
    intermediate. Browsers and curl paper over this by following the
    certificate's AIA extension to fetch the missing link. OpenSSL does not, so
    Python fails with CERTIFICATE_VERIFY_FAILED where curl succeeds.

    The usual "fix" found in scraping tutorials is `verify=False`. That
    disables verification against every host, for a server-side cosmetic bug,
    and makes the crawler interceptable. We instead ship the two public
    intermediates -- both issued by roots already in certifi -- so the chain
    completes and verification stays fully on. See certs/README.md.
    """
    ctx = ssl.create_default_context(cafile=certifi.where())
    if extra_dir.is_dir():
        for pem in sorted(extra_dir.glob("*.pem")):
            ctx.load_verify_locations(cafile=str(pem))
    return ctx


class RobotsDenied(Exception):
    """robots.txt forbids this URL for our User-Agent. Not an error to retry."""


class FetchFailed(Exception):
    """All attempts exhausted, or a non-retryable status."""

    def __init__(self, url: str, status: int | None, detail: str) -> None:
        super().__init__(f"{url} -> {detail}")
        self.url = url
        self.status = status


class CircuitOpen(Exception):
    """Too many consecutive failures; the site is presumed down."""


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


def normalize_robots(text: str) -> list[str]:
    """Repair a robots.txt before handing it to `urllib.robotparser`.

    THE UGLY CASE THAT IS ALSO A SAFETY BUG.

    `www.dof.gob.mx/robots.txt` begins::

        User-agent: *
        #
        <blank line>
        Disallow: /nota_to_doc.php?
        Disallow: /copias_cert.php?
        Disallow: /nota_detalle.php?
        ...

    Python's `urllib.robotparser` implements the 1996 draft, in which a blank
    line *terminates* a group. Its parser sees `User-agent: *`, then a blank
    line, resets to `state = 0`, and then silently discards every `Disallow:`
    that follows because they no longer belong to any group. The measured
    result on the real file::

        >>> rp.parse(open("robots.txt").read().splitlines())
        >>> len(rp.entries)
        1                       # only the AdsBot-Google group survives
        >>> rp.can_fetch("*", "https://www.dof.gob.mx/nota_detalle.php?codigo=1")
        True                    # a page the DOF explicitly disallows

    So the stdlib, used the obvious way, hands you permission to crawl exactly
    the URLs the publisher asked you not to. Google's parser and RFC 9309
    (§2.2) both ignore blank lines inside a group, which is why no one at SEGOB
    has ever noticed the file is ambiguous -- every real crawler reads it the
    way they intended.

    This function makes the stdlib agree with the RFC:

      * drop comments and blank lines entirely, so they can never split a group;
      * re-insert a single separator before each `User-agent:` line that starts
        a *new* group, so consecutive `User-agent:` lines still share one group
        (RFC 9309 §2.2.1) and distinct groups stay distinct.

    Dropping the fragment along with the comment is also correct rather than
    incidental: several DOF rules end in `#gsc.tab=0`, and a fragment is never
    sent to the server, so it cannot be part of a path match.

    We fix the input instead of writing our own matcher because the stdlib's
    *matching* is fine -- it is only its grouping that is 30 years out of date.
    """
    out: list[str] = []
    prev_was_ua = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        is_ua = line.lower().startswith("user-agent")
        if is_ua and out and not prev_was_ua:
            out.append("")  # close the previous group, open a new one
        out.append(line)
        prev_was_ua = is_ua
    return out


class RobotsGate:
    """Per-origin robots.txt cache.

    Why this is not boilerplate for the DOF specifically: robots.txt on
    www.dof.gob.mx disallows `/nota_detalle.php?` and `/nota_to_doc.php?`,
    i.e. the *entire detail layer* of the site. That single line is what forced
    this pipeline's two-host architecture -- discover on www.dof.gob.mx (the
    index is allowed), enrich on sidof.segob.gob.mx (individual notes are
    allowed there, with 18 specific IDs excluded). See docs/DECISIONS.md.
    """

    def __init__(self, fetch: Callable[[str], httpx.Response], user_agent: str) -> None:
        self._fetch = fetch
        self._ua = user_agent
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    def _parser(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        origin = self._origin(url)
        if origin in self._cache:
            return self._cache[origin]

        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{origin}/robots.txt")
        try:
            resp = self._fetch(f"{origin}/robots.txt")
        except Exception as exc:  # network error
            # Fail closed. A robots.txt we could not read is not a robots.txt
            # that said yes. RFC 9309 §2.3.1.4 says treat unreachable as
            # "disallow all" -- and an unreachable server is one we should not
            # be hammering anyway.
            log.warning("robots.txt unreachable for %s (%s); failing closed", origin, exc)
            self._cache[origin] = None
            return None

        if resp.status_code >= 500:
            log.warning("robots.txt %s for %s; failing closed", resp.status_code, origin)
            self._cache[origin] = None
            return None
        if resp.status_code >= 400:
            # 404 means "no rules published" -> everything allowed. Parsing an
            # empty ruleset (rather than setting a flag) keeps `last_checked`
            # populated, which is what makes can_fetch default to True.
            log.info("no robots.txt at %s (%s); allowing all", origin, resp.status_code)
            rp.parse([])
        else:
            rp.parse(normalize_robots(resp.text))

        self._cache[origin] = rp
        return rp

    def allows(self, url: str) -> bool:
        rp = self._parser(url)
        if rp is None:
            return False
        return rp.can_fetch(self._ua, url)

    def crawl_delay(self, url: str) -> float | None:
        rp = self._parser(url)
        if rp is None:
            return None
        raw = rp.crawl_delay(self._ua)
        return float(raw) if raw is not None else None

    def load_from_text(self, origin: str, text: str) -> None:
        """Seed the cache directly. Used by the offline tests."""
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{origin}/robots.txt")
        rp.parse(normalize_robots(text))
        self._cache[origin] = rp


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------


@dataclass
class RateLimiter:
    """Minimum interval between requests, tracked per host.

    Not a token bucket: a bucket lets you burst, and bursting is exactly the
    behaviour that gets a scraper blocked. A hard floor between consecutive
    requests to the same host is both simpler and stricter.
    """

    requests_per_second: float
    jitter_ratio: float = 0.25
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    rng: random.Random = field(default_factory=random.Random)
    _last: dict[str, float] = field(default_factory=dict, init=False)
    _overrides: dict[str, float] = field(default_factory=dict, init=False)

    def set_min_interval(self, host: str, seconds: float) -> None:
        """Honour a robots.txt Crawl-delay that is stricter than our default."""
        base = 1.0 / self.requests_per_second
        if seconds > base:
            log.info("host %s: robots Crawl-delay %.1fs overrides our %.1fs", host, seconds, base)
            self._overrides[host] = seconds

    def wait(self, host: str) -> float:
        interval = self._overrides.get(host, 1.0 / self.requests_per_second)
        # Jitter breaks the perfectly-periodic pattern that trivially
        # fingerprints a bot in an access log.
        interval *= 1.0 + self.rng.uniform(-self.jitter_ratio, self.jitter_ratio)
        now = self.clock()
        last = self._last.get(host)
        delay = 0.0
        if last is not None:
            delay = max(0.0, last + interval - now)
            if delay:
                self.sleep(delay)
        self._last[host] = self.clock()
        return delay


# --------------------------------------------------------------------------
# retries
# --------------------------------------------------------------------------

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def backoff_delay(
    attempt: int, base: float, cap: float, rng: random.Random, retry_after: float | None = None
) -> float:
    """Exponential backoff with *full* jitter, plus Retry-After override.

    Full jitter (`uniform(0, window)`) rather than the more common
    `window/2 + uniform(0, window/2)`: when several workers back off together,
    equal-jitter keeps them clustered and they re-collide. Full jitter spreads
    them across the whole window. (AWS Architecture Blog, "Exponential Backoff
    and Jitter".)

    A server-sent `Retry-After` always wins -- it is the server telling us
    exactly what it wants, and second-guessing it is how you get banned.
    """
    if retry_after is not None:
        return min(retry_after, cap)
    window = min(cap, base * (2**attempt))
    return rng.uniform(0.0, window)


def parse_retry_after(value: str | None) -> float | None:
    """`Retry-After` is either delta-seconds or an HTTP-date. Handle both."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC) if when.tzinfo else _dt.datetime.now()
    return max(0.0, (when - now).total_seconds())


# --------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------


@dataclass
class FetchStats:
    requests: int = 0
    retries: int = 0
    robots_denied: int = 0
    bytes_down: int = 0
    slept_s: float = 0.0


class PoliteClient:
    """The only way out to the network."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._client = client or httpx.Client(
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-MX,es;q=0.9",
            },
            timeout=httpx.Timeout(settings.timeout_s),
            follow_redirects=True,
            http2=False,
            verify=build_ssl_context(),
        )
        self.limiter = RateLimiter(
            requests_per_second=settings.requests_per_second,
            jitter_ratio=settings.jitter_ratio,
            sleep=self._instrumented_sleep,
            rng=self._rng,
        )
        # robots.txt is itself fetched through the raw client -- rate-limited,
        # but *not* robots-gated, otherwise we would need robots.txt to decide
        # whether we may read robots.txt.
        self.robots = RobotsGate(self._raw_get, settings.user_agent)
        self.stats = FetchStats()
        self._consecutive_failures = 0

    # -- plumbing ----------------------------------------------------------

    def _instrumented_sleep(self, seconds: float) -> None:
        self.stats.slept_s += seconds
        self._sleep(seconds)

    def _raw_get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        host = urlsplit(url).netloc
        self.limiter.wait(host)
        self.stats.requests += 1
        resp = self._client.get(url, headers=headers)
        self.stats.bytes_down += len(resp.content)
        return resp

    # -- public API --------------------------------------------------------

    def check_robots(self, url: str) -> None:
        """Raise RobotsDenied if we may not fetch `url`."""
        if not self.settings.obey_robots:
            return
        if not self.robots.allows(url):
            self.stats.robots_denied += 1
            raise RobotsDenied(url)
        delay = self.robots.crawl_delay(url)
        if delay:
            self.limiter.set_min_interval(urlsplit(url).netloc, delay)

    def get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        """Robots-checked, rate-limited, retrying GET.

        Raises RobotsDenied, FetchFailed or CircuitOpen. Never returns a
        non-2xx response: the caller should not have to re-check.
        """
        self.check_robots(url)

        if self._consecutive_failures >= self.settings.max_consecutive_failures:
            raise CircuitOpen(
                f"{self._consecutive_failures} consecutive failures; "
                "refusing to keep hitting the site"
            )

        last_detail = "no attempt made"
        last_status: int | None = None
        retry_after: float | None = None

        for attempt in range(self.settings.max_attempts):
            retry_after = None
            try:
                resp = self._raw_get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_detail, last_status = f"{type(exc).__name__}: {exc}", None
            else:
                last_status = resp.status_code
                if resp.status_code < 400:
                    self._consecutive_failures = 0
                    return resp
                if resp.status_code not in RETRYABLE_STATUS:
                    # 403/404/410 will not improve by asking again. Retrying
                    # them is how a scraper turns one mistake into a ban.
                    self._consecutive_failures += 1
                    raise FetchFailed(url, resp.status_code, f"HTTP {resp.status_code}")
                last_detail = f"HTTP {resp.status_code}"
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))

            if attempt + 1 >= self.settings.max_attempts:
                break

            delay = backoff_delay(
                attempt,
                self.settings.backoff_base_s,
                self.settings.backoff_cap_s,
                self._rng,
                retry_after,
            )
            self.stats.retries += 1
            log.warning(
                "attempt %d/%d failed for %s (%s); sleeping %.1fs",
                attempt + 1,
                self.settings.max_attempts,
                url,
                last_detail,
                delay,
            )
            self._instrumented_sleep(delay)

        self._consecutive_failures += 1
        raise FetchFailed(url, last_status, f"{self.settings.max_attempts} attempts: {last_detail}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
