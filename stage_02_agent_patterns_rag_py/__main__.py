"""Run grounded research locally or compare orchestration engines on a fixed suite."""

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

import httpx

from stage_01_model_tools_py.agent_py import BudgetExceeded
from stage_01_model_tools_py.model_py import OpenAIModel
from stage_01_model_tools_py.transport_py import Http, ProviderError, ProviderFormatError

from .engines_py import ENGINES, ScriptedResearchModel
from .eval_py import evaluate, summary
from .index_py import (
    DEFAULT_CORPUS,
    ChromaIndex,
    HashEmbedder,
    MemoryIndex,
    OpenAIEmbedder,
    load_chunks,
)
from .report_py import ExtractiveWriter, ModelWriter


async def run(args: argparse.Namespace) -> None:
    chunks = load_chunks(args.corpus, args.chunk_size, args.overlap)
    async with httpx.AsyncClient(timeout=15) as client:
        http = Http(client)
        api_key = os.getenv("OPENAI_API_KEY", "")
        if args.chroma:
            embedder = OpenAIEmbedder(http, api_key) if args.live_embeddings else HashEmbedder()
            index = await ChromaIndex.build(args.chroma, chunks, embedder)
        else:
            index = await MemoryIndex.build(chunks)

        model = OpenAIModel(http, api_key, os.getenv("OPENAI_MODEL", "")) if args.live else None
        writer = ModelWriter(model) if model else ExtractiveWriter()
        react_model = model or ScriptedResearchModel(writer)

        def execute(engine: str, question: str):
            component = react_model if engine == "react" else writer
            return ENGINES[engine](question, index, component, args.top_k)

        if args.compare:
            for engine in ENGINES:
                rows = await evaluate(lambda question: execute(engine, question))
                print(f"{engine}: {summary(rows, args.rates)}")
                if args.details:
                    for row in rows:
                        print("  " + json.dumps(asdict(row)))
            return
        async with asyncio.timeout(60):
            report = await execute(args.engine, args.question)
        print(report.render())
        print(f"Steps: {' → '.join(report.steps)} | rejected citations: {report.rejected}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "question", nargs="?", default="How does an outbox use a stable delivery key?"
    )
    parser.add_argument("--engine", choices=ENGINES, default="react")
    parser.add_argument(
        "--compare", action="store_true", help="run every engine on the included corpus"
    )
    parser.add_argument(
        "--details", action="store_true", help="include per-question evaluation results"
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--chunk-size", type=int, default=100)
    parser.add_argument("--overlap", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--chroma", type=Path, help="use a persistent Chroma directory")
    parser.add_argument("--live", action="store_true", help="enable live report generation")
    parser.add_argument(
        "--live-embeddings", action="store_true", help="embed the corpus via OpenAI"
    )
    parser.add_argument(
        "--rates",
        type=float,
        nargs=2,
        metavar=("INPUT_USD", "OUTPUT_USD"),
        help="generation cost per million tokens",
    )
    args = parser.parse_args()
    if args.details and not args.compare:
        parser.error("--details requires --compare")
    if args.live_embeddings and not args.chroma:
        parser.error("--live-embeddings requires --chroma")
    if args.compare and args.corpus != DEFAULT_CORPUS:
        parser.error("--compare uses expected sources from the built-in corpus")
    if args.rates and any(rate < 0 for rate in args.rates):
        parser.error("--rates must be nonnegative")
    try:
        asyncio.run(run(args))
    except ModuleNotFoundError as exc:
        parser.error(f"{exc}. Install requirements-labs.txt for Chroma and AutoGen.")
    except (ValueError, ProviderError, ProviderFormatError, BudgetExceeded, TimeoutError) as exc:
        parser.exit(1, f"Research failed: {exc}\n")


if __name__ == "__main__":
    main()
