"""Rate limiting, backoff and the circuit breaker.

All deterministic: a seeded RNG, an injected clock, and an injected sleep. A
retry test that actually sleeps is a retry test nobody runs.
"""

from __future__ import annotations

import random

import httpx
import pytest

from dof_ingest.config import Settings
from dof_ingest.http import (
    CircuitOpen,
    FetchFailed,
    PoliteClient,
    RateLimiter,
    backoff_delay,
    parse_retry_after,
)

ROBOTS_ALLOW_ALL = "User-agent: *\nDisallow:\n"
URL = "https://example.gob.mx/thing"


def _client(handler: object, **overrides: object) -> tuple[PoliteClient, list[float]]:
    slept: list[float] = []
    settings = Settings(requests_per_second=1000.0, **overrides)  # type: ignore[arg-type]
    client = PoliteClient(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
        sleep=slept.append,
        rng=random.Random(1234),
    )
    # Give the rate limiter its own no-op sleep so `slept` records *only*
    # backoff. Full jitter can legitimately return a sub-millisecond delay, so
    # separating the channels is more honest than filtering by magnitude.
    client.limiter.sleep = lambda _: None
    return client, slept


def _serve(*statuses: int, headers: dict[str, str] | None = None) -> object:
    seq = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        code = seq.pop(0) if seq else 200
        return httpx.Response(code, text="ok", headers=headers or {})

    return handler


# --------------------------------------------------------------------------
# backoff
# --------------------------------------------------------------------------


def test_backoff_uses_full_jitter_within_an_exponential_window() -> None:
    rng = random.Random(0)
    for attempt, ceiling in [(0, 1.0), (1, 2.0), (2, 4.0), (3, 8.0)]:
        samples = [backoff_delay(attempt, 1.0, 60.0, rng) for _ in range(200)]
        assert all(0.0 <= s <= ceiling for s in samples)
        # Full jitter, not equal jitter: the window's lower half is reachable.
        assert min(samples) < ceiling / 2


def test_backoff_respects_the_cap() -> None:
    rng = random.Random(0)
    assert all(backoff_delay(20, 1.0, 30.0, rng) <= 30.0 for _ in range(100))


def test_retry_after_overrides_our_own_schedule() -> None:
    rng = random.Random(0)
    assert backoff_delay(0, 1.0, 60.0, rng, retry_after=17.0) == 17.0
    # ...but never past the cap; a hostile or broken header cannot park us.
    assert backoff_delay(0, 1.0, 30.0, rng, retry_after=9999.0) == 30.0


def test_parse_retry_after_handles_both_forms() -> None:
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("nonsense") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # in the past -> now


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------


def test_rate_limiter_enforces_a_minimum_gap() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    limiter = RateLimiter(
        requests_per_second=2.0, jitter_ratio=0.0, sleep=sleep,
        clock=lambda: now[0], rng=random.Random(0),
    )
    limiter.wait("h")  # first call is free
    assert slept == []
    limiter.wait("h")
    assert slept == [pytest.approx(0.5)]

    # Different hosts do not throttle each other.
    limiter.wait("other")
    assert len(slept) == 1


def test_rate_limiter_jitter_breaks_the_periodic_pattern() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    limiter = RateLimiter(
        requests_per_second=1.0, jitter_ratio=0.25, sleep=sleep,
        clock=lambda: now[0], rng=random.Random(7),
    )
    for _ in range(20):
        limiter.wait("h")
    assert len(set(slept)) > 1
    assert all(0.75 <= s <= 1.25 for s in slept)


# --------------------------------------------------------------------------
# retries
# --------------------------------------------------------------------------


def test_transient_failures_are_retried_then_succeed() -> None:
    client, slept = _client(_serve(503, 503, 200))
    with client:
        assert client.get(URL).status_code == 200
    assert client.stats.retries == 2
    assert len(slept) == 2


def test_client_errors_are_not_retried() -> None:
    client, slept = _client(_serve(404))
    with client, pytest.raises(FetchFailed) as exc:
        client.get(URL)
    assert exc.value.status == 404
    assert client.stats.retries == 0
    assert slept == []  # retrying a 404 is how you turn a mistake into a ban


def test_retry_after_header_is_honoured() -> None:
    client, slept = _client(_serve(429, 200, headers={"Retry-After": "5"}))
    with client:
        client.get(URL)
    assert slept == [5.0]


def test_exhausted_attempts_raise() -> None:
    client, _ = _client(_serve(503, 503, 503, 503), max_attempts=4)
    with client, pytest.raises(FetchFailed, match="4 attempts"):
        client.get(URL)


def test_circuit_opens_after_repeated_failures() -> None:
    client, _ = _client(_serve(*([500] * 100)), max_attempts=1, max_consecutive_failures=3)
    with client:
        for _ in range(3):
            with pytest.raises(FetchFailed):
                client.get(URL)
        with pytest.raises(CircuitOpen):
            client.get(URL)


def test_a_success_resets_the_circuit() -> None:
    client, _ = _client(_serve(500, 500, 200, 500), max_attempts=1, max_consecutive_failures=3)
    with client:
        for _ in range(2):
            with pytest.raises(FetchFailed):
                client.get(URL)
        assert client.get(URL).status_code == 200
        with pytest.raises(FetchFailed):
            client.get(URL)  # counter restarted, so this is failure #1, not #3
