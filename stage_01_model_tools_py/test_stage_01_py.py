"""Read these as scenarios: each names a behavior we need before adding frameworks."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

import httpx

from .agent_py import Assistant, BudgetExceeded, Session
from .chain_py import conversation_chain
from .contracts_py import ModelReply, ToolCall
from .model_py import DemoModel, OpenAIModel
from .tools_py import (
    Tool,
    Toolbox,
    ToolInputError,
    Weather,
    arithmetic,
    assistant_tools,
    demo_weather,
)
from .transport_py import Http, ProviderError, ProviderFormatError, RetryPolicy


class ArithmeticTests(unittest.TestCase):
    def test_precedence_and_unary_operators(self):
        self.assertEqual(arithmetic("-(2 + 5 * 3) % 5"), 3)

    def test_only_arithmetic_nodes_are_allowed(self):
        for value in ("__import__('os')", "2 ** 8", "True", "1/0", "1e100", "3 +"):
            with self.subTest(value=value):
                with self.assertRaises(ToolInputError):
                    arithmetic(value)

    def test_expression_size_is_bounded(self):
        with self.assertRaises(ToolInputError):
            arithmetic("1+" * 22 + "1")


class AssistantTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_tool_round_trip(self):
        session = Session()
        agent = Assistant(DemoModel(), assistant_tools(demo_weather))
        turn = await agent.ask("Weather in San Jose and calculate (12 + 8) / 4", session)
        self.assertIn("19 °C", turn.text)
        self.assertIn("5.0", turn.text)
        self.assertEqual(turn.steps[0].tools, ("weather", "calculate"))
        self.assertEqual(turn.steps[1].tools, ())
        items = session.context()
        self.assertEqual(items[1]["call_id"], items[3]["call_id"])
        self.assertEqual(items[2]["call_id"], items[4]["call_id"])

    async def test_argument_errors_are_observations_and_handler_is_not_run(self):
        handler = AsyncMock()
        tool = Tool("city", "Look up city", "name", "City name", handler)
        toolbox = Toolbox([tool])
        for arguments in (
            "{",
            "[]",
            '{"name": 3}',
            '{"name": "a", "name": "b"}',
            '{"name": "ok", "extra": true}',
        ):
            with self.subTest(arguments=arguments):
                output = await toolbox.execute(ToolCall("one", "city", arguments))
                self.assertEqual(json.loads(output)["error"]["code"], "invalid_arguments")
        output = await toolbox.execute(ToolCall("two", "shell", "{}"))
        self.assertEqual(json.loads(output)["error"]["code"], "unknown_tool")
        handler.assert_not_awaited()

    async def test_timeout_is_an_observation(self):
        async def slow(_):
            await asyncio.sleep(1)

        toolbox = Toolbox([Tool("slow", "Slow", "x", "Name", slow)], timeout=0.01)
        result = await toolbox.execute(ToolCall("id", "slow", '{"x":"go"}'))
        self.assertEqual(json.loads(result)["error"]["code"], "tool_timeout")

    async def test_read_only_tools_overlap_and_order_is_retained(self):
        first_started = asyncio.Event()
        second_finished = asyncio.Event()

        async def weather(city):
            first_started.set()
            await second_finished.wait()
            return {"city": city, "temperature_c": 15, "source": "test"}

        async def calculate(_):
            await first_started.wait()
            second_finished.set()
            return {"result": 11}

        toolbox = Toolbox(
            [
                Tool("weather", "Weather", "city", "City", weather),
                Tool("calculate", "Math", "expression", "Expression", calculate),
            ],
            timeout=0.5,
        )
        turn = await Assistant(DemoModel(), toolbox).ask(
            "Weather in Paris and calculate 2+9", Session()
        )
        self.assertTrue(turn.text.startswith("Paris: 15"))
        self.assertTrue(turn.text.endswith("Result: 11."))

    async def test_memory_trims_at_turn_boundaries(self):
        session = Session(max_turns=1)
        assistant = Assistant(DemoModel(), assistant_tools(demo_weather))
        await assistant.ask("calculate 4+1", session)
        self.assertEqual(len(session.context()), 4)
        await assistant.ask("weather in Paris", session)
        self.assertEqual(len(session.context()), 4)
        self.assertEqual(session.context()[0]["content"], "weather in Paris")
        session.reset()
        self.assertEqual(session.context(), [])

    async def test_follow_up_uses_session_history(self):
        session = Session()
        assistant = Assistant(DemoModel(), assistant_tools(demo_weather))
        await assistant.ask("calculate 18 / 2", session)
        follow_up = await assistant.ask("What did you calculate?", session)
        self.assertEqual(follow_up.text, "Result: 9.0.")
        self.assertEqual(follow_up.steps[0].tools, ())
        session.reset()
        missing = await assistant.ask("What did you calculate?", session)
        self.assertIn("don't have that result", missing.text)

    async def test_budget_failure_does_not_commit_partial_turn_or_call_tool(self):
        handler = AsyncMock()
        agent = Assistant(DemoModel(), assistant_tools(handler), max_rounds=1)
        session = Session()
        with self.assertRaises(BudgetExceeded):
            await agent.ask("Weather in Seattle", session)
        self.assertEqual(session.context(), [])
        handler.assert_not_awaited()

    async def test_duplicate_ids_are_rejected_before_dispatch(self):
        class DuplicateModel:
            async def reply(self, *_):
                call = ToolCall("duplicate", "weather", '{"city":"Seattle"}')
                return ModelReply("", (call, call), ())

        handler = AsyncMock()
        with self.assertRaises(ProviderFormatError):
            await Assistant(DuplicateModel(), assistant_tools(handler)).ask("weather", Session())
        handler.assert_not_awaited()

    async def test_lcel_uses_the_same_turn_and_memory(self):
        session = Session()
        agent = Assistant(DemoModel(), assistant_tools(demo_weather))
        turn = await conversation_chain(agent, session).ainvoke("  calculate 2 + 2  ")
        self.assertEqual(turn.text, "Result: 4.")
        self.assertEqual(session.context()[0]["content"], "calculate 2 + 2")


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_after_then_success(self):
        outcomes = iter(
            [
                httpx.Response(429, headers={"Retry-After": "0.5"}),
                httpx.Response(200, json={"status": "ok"}),
            ]
        )
        sleep = AsyncMock()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: next(outcomes))
        ) as client:
            result = await Http(client, sleep=sleep).json(
                "demo", "GET", "https://test.invalid", retry=True
            )
        sleep.assert_awaited_once_with(0.5)
        self.assertEqual(result, {"status": "ok"})

    async def test_long_retry_after_returns_control_to_the_scheduler(self):
        response = httpx.Response(429, headers={"Retry-After": "600"})
        sleep = AsyncMock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
            with self.assertRaises(ProviderError) as raised:
                await Http(client, sleep=sleep).json(
                    "demo", "GET", "https://test.invalid", retry=True
                )
        self.assertEqual(raised.exception.status, 429)
        sleep.assert_not_awaited()

    async def test_permanent_errors_and_non_retryable_operations_attempt_once(self):
        for code, retry in ((401, True), (503, False)):
            with self.subTest(code=code, retry=retry):
                handler = AsyncMock(return_value=httpx.Response(code, text="secret response"))
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(ProviderError) as raised:
                        await Http(client).json("demo", "POST", "https://test.invalid", retry=retry)
                self.assertNotIn("secret response", str(raised.exception))
                handler.assert_awaited_once()

    async def test_retries_are_bounded_even_during_network_errors(self):
        handler = AsyncMock(side_effect=httpx.ConnectError("network unavailable"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http = Http(client, RetryPolicy(attempts=2, initial_delay=0), sleep=AsyncMock())
            with self.assertRaises(ProviderError):
                await http.request("demo", "GET", "https://test.invalid", retry=True)
        self.assertEqual(handler.await_count, 2)

    async def test_live_weather_parses_a_pair_of_provider_responses(self):
        def handler(request):
            if request.url.path == "/v1/search":
                self.assertEqual(request.url.params["name"], "Paris")
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "latitude": 48.9,
                                "longitude": 2.3,
                                "name": "Paris",
                                "country": "France",
                            }
                        ]
                    },
                )
            self.assertEqual(request.url.params["current"], "temperature_2m")
            return httpx.Response(
                200, json={"current": {"temperature_2m": 17.3, "time": "2026-11-05T08:00"}}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await Weather(Http(client)).current("Paris")
        self.assertEqual(result["city"], "Paris, France")
        self.assertEqual(result["temperature_c"], 17.3)

    async def test_live_adapter_round_trips_reasoning_and_call_ids(self):
        requests = []

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                output = [
                    {"type": "reasoning", "summary": [], "encrypted_content": "opaque"},
                    {
                        "type": "function_call",
                        "call_id": "calc-123",
                        "name": "calculate",
                        "arguments": '{"expression":"2+2"}',
                    },
                ]
            else:
                output = [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "4"}],
                    }
                ]
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": output,
                    "usage": {"input_tokens": 7, "output_tokens": 2},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = OpenAIModel(Http(client), "fake-key", "fake-model")
            result = await Assistant(model, assistant_tools(demo_weather)).ask("2+2", Session())

        self.assertFalse(requests[0]["store"])
        self.assertEqual(requests[1]["input"][1]["encrypted_content"], "opaque")
        observation = requests[1]["input"][-1]
        self.assertEqual(observation["call_id"], "calc-123")
        self.assertEqual(json.loads(observation["output"])["value"]["result"], 4)
        self.assertEqual(result.usage.input_tokens, 14)

    def test_incomplete_and_malformed_model_replies(self):
        for data in ({"status": "incomplete"}, {"status": "completed", "output": [None]}):
            with self.subTest(data=data):
                with self.assertRaises(ProviderFormatError):
                    OpenAIModel.parse(data)


if __name__ == "__main__":
    unittest.main()
