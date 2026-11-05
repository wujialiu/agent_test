"""Typed boundary shared by the agent loop, offline model, and live provider."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

JsonObject: TypeAlias = dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )

    def cost(self, input_per_million: float, output_per_million: float) -> float:
        return (
            self.input_tokens * input_per_million + self.output_tokens * output_per_million
        ) / 1_000_000


@dataclass(frozen=True)
class ModelReply:
    text: str
    calls: tuple[ToolCall, ...]
    # Keep provider-native items so call IDs and opaque reasoning can round-trip.
    items: tuple[JsonObject, ...]
    usage: Usage = Usage()


class Model(Protocol):
    async def reply(
        self,
        instructions: str,
        messages: Sequence[JsonObject],
        tools: Sequence[JsonObject],
    ) -> ModelReply: ...


def message(role: str, content: str) -> JsonObject:
    return {"role": role, "content": content}


def tool_message(call: ToolCall, output: str) -> JsonObject:
    return {"type": "function_call_output", "call_id": call.id, "output": output}
