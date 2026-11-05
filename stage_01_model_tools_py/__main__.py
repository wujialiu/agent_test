"""Run the first stage as a single prompt, a REPL, or an LCEL demo."""

import argparse
import asyncio
import logging
import os

import httpx

from .agent_py import Assistant, BudgetExceeded, Session
from .chain_py import conversation_chain
from .model_py import DemoModel, OpenAIModel
from .tools_py import Weather, assistant_tools, demo_weather
from .transport_py import Http, ProviderError, ProviderFormatError


async def run(args: argparse.Namespace) -> None:
    async with httpx.AsyncClient(timeout=8) as client:
        http = Http(client)
        model = (
            OpenAIModel(http, os.getenv("OPENAI_API_KEY", ""), os.getenv("OPENAI_MODEL", ""))
            if args.live
            else DemoModel()
        )
        weather = Weather(http).current if args.live_weather else demo_weather
        agent = Assistant(model, assistant_tools(weather))
        session = Session()
        chain = conversation_chain(agent, session) if args.chain else None

        async def answer(prompt: str) -> None:
            try:
                turn = await (chain.ainvoke(prompt) if chain else agent.ask(prompt, session))
            except (
                ProviderError,
                ProviderFormatError,
                BudgetExceeded,
                TimeoutError,
                ValueError,
            ) as exc:
                print(f"Turn failed: {exc}")
                if args.prompt:
                    raise SystemExit(1) from None
                return
            print(turn.text)
            if args.trace:
                for step in turn.steps:
                    print(
                        f"  round {step.round_number}: {', '.join(step.tools) or 'answer'} "
                        f"({step.elapsed_ms:.1f} ms)"
                    )
                print(f"  tokens: {turn.usage.input_tokens} in, {turn.usage.output_tokens} out")

        if args.prompt:
            await answer(args.prompt)
            return
        print("Weather and math assistant. /reset clears conversation, /quit exits.")
        while True:
            try:
                prompt = (await asyncio.to_thread(input, "you> ")).strip()
            except EOFError:
                return
            if prompt == "/quit":
                return
            if prompt == "/reset":
                session.reset()
            elif prompt:
                await answer(prompt)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="answer a single prompt and exit")
    parser.add_argument("--chain", action="store_true", help="compose the call with LCEL")
    parser.add_argument("--live", action="store_true", help="call the OpenAI Responses API")
    parser.add_argument(
        "--live-weather", action="store_true", help="fetch current Open-Meteo weather"
    )
    parser.add_argument(
        "--trace", action="store_true", help="display tool names, latency, and usage"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.trace else logging.WARNING)
    # httpx's INFO request logs can contain query strings such as locations.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
