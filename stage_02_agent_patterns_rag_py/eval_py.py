"""Compare engines on fixed questions and expected source documents."""

import asyncio
import json
import math
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from stage_01_model_tools_py.agent_py import BudgetExceeded
from stage_01_model_tools_py.contracts_py import Usage
from stage_01_model_tools_py.transport_py import ProviderError, ProviderFormatError

from .report_py import Report

CASES = (
    ("How do we retry a rate limit with Retry-After?", "retries.md"),
    ("How does an outbox use a stable delivery key?", "delivery.md"),
    ("How should we measure retrieval recall and citation correctness?", "retrieval.md"),
    ("How should we compare p95 latency across tasks?", "observability.md"),
    ("What is the chemical formula for caffeine?", None),
)


@dataclass(frozen=True)
class Evaluation:
    question: str
    expected_source: str | None
    retrieved: bool
    cited: bool
    abstained: bool
    rejected: int
    latency_ms: float
    input_tokens: int
    output_tokens: int
    error: str | None = None


def score(report: Report, expected: str | None, elapsed_ms: float) -> Evaluation:
    sources = {hit.chunk.id: hit.chunk.source for hit in report.evidence}
    return Evaluation(
        report.question,
        expected,
        expected in sources.values(),
        any(sources[claim.chunk_id] == expected for claim in report.claims),
        not report.claims,
        report.rejected,
        elapsed_ms,
        report.usage.input_tokens,
        report.usage.output_tokens,
    )


async def evaluate(
    run: Callable[[str], Awaitable[Report]],
    cases: tuple[tuple[str, str | None], ...] = CASES,
) -> list[Evaluation]:
    rows = []
    for question, expected in cases:
        start = time.monotonic()
        try:
            async with asyncio.timeout(60):
                report = await run(question)
            rows.append(score(report, expected, (time.monotonic() - start) * 1000))
        except (ProviderError, ProviderFormatError, BudgetExceeded, TimeoutError) as exc:
            # Count failed requests in the denominator and keep exception bodies private.
            rows.append(
                Evaluation(
                    question,
                    expected,
                    False,
                    False,
                    False,
                    0,
                    (time.monotonic() - start) * 1000,
                    0,
                    0,
                    type(exc).__name__,
                )
            )
    return rows


def summary(rows: list[Evaluation], rates: tuple[float, float] | None = None) -> str:
    supported = [row for row in rows if row.expected_source]
    unsupported = [row for row in rows if row.expected_source is None]
    latencies = sorted(row.latency_ms for row in rows)
    input_tokens = sum(row.input_tokens for row in rows)
    output_tokens = sum(row.output_tokens for row in rows)
    return json.dumps(
        {
            "samples": len(rows),
            "retrieval_hits": [sum(row.retrieved for row in supported), len(supported)],
            "expected_citations": [sum(row.cited for row in supported), len(supported)],
            "correct_abstentions": [sum(row.abstained for row in unsupported), len(unsupported)],
            "rejected_citations": sum(row.rejected for row in rows),
            "errors": sum(bool(row.error) for row in rows),
            "median_ms": round(statistics.median(latencies), 2),
            "p95_ms": round(latencies[math.ceil(0.95 * len(latencies)) - 1], 2),
            "tokens": {"input": input_tokens, "output": output_tokens},
            "estimated_generation_usd": (
                Usage(input_tokens, output_tokens).cost(*rates) if rates else None
            ),
        }
    )
