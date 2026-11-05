"""Async HTTP with explicit timeouts and a bounded retry policy.

A single client is shared across requests so HTTP connections can be reused.
The caller chooses whether an operation is safe to repeat.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

logger = logging.getLogger(__name__)
TRANSIENT = {408, 429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    """A sanitized error. URLs, response bodies, and tokens stay out of logs."""

    def __init__(self, service: str, status: int | None = None):
        self.service = service
        self.status = status
        super().__init__(f"{service} request failed" + (f" (HTTP {status})" if status else ""))

    @property
    def transient(self) -> bool:
        return self.status is None or self.status in TRANSIENT


class ProviderFormatError(ValueError):
    """The provider answered but its response did not match our contract."""


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    initial_delay: float = 0.25
    max_delay: float = 5.0

    def __post_init__(self) -> None:
        if self.attempts < 1 or not 0 <= self.initial_delay <= self.max_delay:
            raise ValueError("invalid retry policy")

    def delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                seconds = float(retry_after)
            except ValueError:
                try:
                    seconds = (
                        parsedate_to_datetime(retry_after) - datetime.now(UTC)
                    ).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    seconds = -1
            if seconds >= 0:
                return seconds
        # Jitter keeps several workers from retrying in lockstep.
        return random.uniform(0, min(self.initial_delay * 2 ** (attempt - 1), self.max_delay))


class Http:
    def __init__(
        self,
        client: httpx.AsyncClient,
        policy: RetryPolicy = RetryPolicy(),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.client = client
        self.policy = policy
        self.sleep = sleep

    async def request(
        self,
        service: str,
        method: str,
        url: str,
        *,
        retry: bool = False,
        max_bytes: int | None = None,
        **kwargs,
    ) -> httpx.Response:
        # A timeout does not tell us whether a remote side effect happened.
        # Write callers must opt in only if the receiver can deduplicate.
        attempts = self.policy.attempts if retry else 1
        for attempt in range(1, attempts + 1):
            retry_after = None
            try:
                response = await self._send(method, url, max_bytes, **kwargs)
                if response.is_success:
                    return response
                error = ProviderError(service, response.status_code)
                retry_after = response.headers.get("Retry-After")
            except httpx.TransportError:
                error = ProviderError(service)
            if not error.transient or attempt == attempts:
                raise error from None
            delay = self.policy.delay(attempt, retry_after)
            if delay > self.policy.max_delay:
                # Leave a long cooldown to a scheduler or the caller's deadline.
                raise error
            logger.info(
                "provider_retry service=%s attempt=%d status=%s", service, attempt, error.status
            )
            await self.sleep(delay)
        raise AssertionError("validated retry policy always runs at least once")

    async def _send(self, method, url, max_bytes, **kwargs) -> httpx.Response:
        if max_bytes is None:
            return await self.client.request(method, url, **kwargs)
        # Bound decoded bytes before passing untrusted content to a parser.
        # Closing the response also releases the connection on an early exit.
        async with self.client.stream(method, url, **kwargs) as response:
            body = bytearray()
            if response.is_success:
                async for part in response.aiter_bytes(chunk_size=16_384):
                    if len(body) + len(part) > max_bytes:
                        raise ProviderFormatError("provider response exceeded the byte limit")
                    body.extend(part)
            headers = response.headers.copy()
            for key in ("content-encoding", "content-length", "transfer-encoding"):
                headers.pop(key, None)
            return httpx.Response(response.status_code, headers=headers, content=bytes(body))

    async def json(self, service: str, method: str, url: str, **kwargs) -> dict:
        response = await self.request(service, method, url, **kwargs)
        try:
            data = response.json()
        except ValueError:
            raise ProviderFormatError(f"{service} returned invalid JSON") from None
        if not isinstance(data, dict):
            raise ProviderFormatError(f"{service} returned a non-object response")
        return data
