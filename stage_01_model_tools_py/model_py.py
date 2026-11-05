"""Two interchangeable models. The scripted model makes failure drills reproducible."""

import json
import re
from collections.abc import Sequence

from .contracts_py import JsonObject, ModelReply, ToolCall, Usage, message
from .transport_py import Http, ProviderFormatError

API = "https://api.openai.com/v1"


class OpenAIModel:
    def __init__(self, http: Http, api_key: str, model: str):
        if not api_key.strip() or not model.strip():
            raise ValueError("set OPENAI_API_KEY and OPENAI_MODEL")
        self.http = http
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.model = model

    async def reply(
        self,
        instructions: str,
        messages: Sequence[JsonObject],
        tools: Sequence[JsonObject],
    ) -> ModelReply:
        return self.parse(await self._request(instructions, messages, tools))

    async def structured(
        self,
        instructions: str,
        prompt: str,
        name: str,
        schema: JsonObject,
    ) -> tuple[JsonObject, Usage]:
        """Use the provider's schema, then parse again at the application boundary."""
        data = await self._request(
            instructions,
            [message("user", prompt)],
            [],
            text={
                "format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}
            },
        )
        reply = self.parse(data)
        try:
            value = json.loads(reply.text)
        except ValueError:
            raise ProviderFormatError(
                "model refused or returned invalid structured output"
            ) from None
        if not isinstance(value, dict):
            raise ProviderFormatError("structured output must be an object")
        return value, reply.usage

    async def _request(self, instructions, messages, tools, **options) -> JsonObject:
        # Generation has no application side effect. A lost response and retry
        # can still incur provider cost, so the retry and output limits matter.
        return await self.http.json(
            "OpenAI Responses",
            "POST",
            f"{API}/responses",
            retry=True,
            headers=self.headers,
            json={
                "model": self.model,
                "instructions": instructions,
                "input": list(messages),
                "tools": list(tools),
                "store": False,
                "include": ["reasoning.encrypted_content"],
                "max_output_tokens": 1500,
                **options,
            },
        )

    @staticmethod
    def parse(data: JsonObject) -> ModelReply:
        if data.get("status") != "completed":
            raise ProviderFormatError("model response was incomplete or failed")
        try:
            items = tuple(data["output"])
            calls = tuple(
                ToolCall(item["call_id"], item["name"], item["arguments"])
                for item in items
                if item["type"] == "function_call"
            )
            if any(
                not isinstance(value, str) or not value
                for call in calls
                for value in (call.id, call.name, call.arguments)
            ):
                raise TypeError("invalid function call")
            text = "\n".join(
                content["text"]
                for item in items
                if item["type"] == "message"
                for content in item["content"]
                if content["type"] == "output_text"
            )
            usage = data.get("usage") or {}
            return ModelReply(
                text,
                calls,
                items,
                Usage(int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderFormatError("model response fields did not match the contract") from None


class DemoModel:
    """A tiny scenario script, useful for understanding orchestration in isolation."""

    async def reply(self, instructions, messages, tools) -> ModelReply:
        del instructions
        last_user = max(index for index, item in enumerate(messages) if item.get("role") == "user")
        observations = [
            json.loads(item["output"])
            for item in messages[last_user + 1 :]
            if item.get("type") == "function_call_output"
        ]
        if observations:
            parts = []
            for observation in observations:
                if not observation["ok"]:
                    parts.append(f"Tool error: {observation['error']['code']}.")
                    continue
                value = observation["value"]
                if "temperature_c" in value:
                    parts.append(
                        f"{value['city']}: {value['temperature_c']} °C ({value['source']})."
                    )
                else:
                    parts.append(f"Result: {value['result']}.")
            return self.answer(" ".join(parts))

        text = messages[last_user]["content"].strip()
        available = {tool["name"] for tool in tools}
        calls = []
        city = re.search(r"weather in ([\w .'-]+?)(?:[?;]| and |$)", text, re.I)
        expression = re.search(r"(?:calculate|compute)\s+([^?;]+)", text, re.I)
        if city and "weather" in available:
            calls.append(("weather", {"city": city.group(1).strip()}))
        if expression and "calculate" in available:
            calls.append(("calculate", {"expression": expression.group(1).strip()}))
        if not calls:
            # Scripted follow-ups prove the next turn can see actual tool results.
            # A real model can interpret a much wider range of references.
            field = {
                "what did you calculate": "result",
                "what was the weather": "temperature_c",
            }.get(text.casefold().rstrip("?"))
            if field:
                for item in reversed(messages[:last_user]):
                    if item.get("type") == "function_call_output":
                        prior = json.loads(item["output"])
                        if prior["ok"] and field in prior["value"]:
                            value = prior["value"]
                            answer = (
                                f"Result: {value['result']}."
                                if field == "result"
                                else f"{value['city']}: {value['temperature_c']} °C ({value['source']})."
                            )
                            return self.answer(answer)
                return self.answer("I don't have that result in this session.")
            return self.answer("Try 'weather in San Jose' or 'calculate (12 + 8) / 4'.")

        items = tuple(
            {
                "type": "function_call",
                "call_id": f"demo-{last_user}-{index}",
                "name": name,
                "arguments": json.dumps(arguments),
            }
            for index, (name, arguments) in enumerate(calls)
        )
        return ModelReply(
            "",
            tuple(ToolCall(item["call_id"], item["name"], item["arguments"]) for item in items),
            items,
        )

    @staticmethod
    def answer(text: str) -> ModelReply:
        return ModelReply(text, (), (message("assistant", text),))
