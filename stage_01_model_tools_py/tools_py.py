"""Read-only tools with schema checks and a deliberately small execution surface."""

import ast
import asyncio
import json
import math
import operator
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from .contracts_py import JsonObject, ToolCall
from .transport_py import Http, ProviderError, ProviderFormatError


class ToolInputError(ValueError):
    """Arguments cannot safely be passed to the tool."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    argument: str
    argument_description: str
    handler: Callable[[str], Awaitable[JsonObject]]
    input_limit: int = 150

    def schema(self) -> JsonObject:
        # The API schema helps the model choose and populate the tool.
        # execute() still validates the arguments before trusting them.
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    self.argument: {"type": "string", "description": self.argument_description}
                },
                "required": [self.argument],
                "additionalProperties": False,
            },
            "strict": True,
        }


class Toolbox:
    def __init__(self, tools: Sequence[Tool], timeout: float = 10.0):
        self.tools = {tool.name: tool for tool in tools}
        if len(self.tools) != len(tools) or timeout <= 0:
            raise ValueError("tool names must be unique and timeout positive")
        self.timeout = timeout

    @property
    def schemas(self) -> list[JsonObject]:
        return [tool.schema() for tool in self.tools.values()]

    async def execute(self, call: ToolCall) -> str:
        tool = self.tools.get(call.name)
        if tool is None:
            return error_output("unknown_tool")
        try:
            if len(call.arguments) > 4096:
                raise ToolInputError("arguments exceed the input limit")
            arguments = json.loads(call.arguments, object_pairs_hook=unique_keys)
            if not isinstance(arguments, dict) or arguments.keys() != {tool.argument}:
                raise ToolInputError("argument names do not match schema")
            value = arguments[tool.argument]
            if not isinstance(value, str) or not value.strip() or len(value) > tool.input_limit:
                raise ToolInputError("expected a bounded non-empty string")
            async with asyncio.timeout(self.timeout):
                result = await tool.handler(value.strip())
            return json.dumps({"ok": True, "value": result}, allow_nan=False)
        except (ToolInputError, json.JSONDecodeError, RecursionError):
            return error_output("invalid_arguments")
        except TimeoutError:
            return error_output("tool_timeout")
        except ProviderFormatError:
            return error_output("invalid_provider_response")
        except ProviderError:
            return error_output("provider_unavailable")


def unique_keys(pairs: list[tuple[str, object]]) -> dict:
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ToolInputError("duplicate JSON keys")
    return values


def error_output(code: str) -> str:
    return json.dumps({"ok": False, "error": {"code": code}})


BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}
UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
NUMBER_LIMIT = 10**15


def arithmetic(expression: str) -> float | int:
    """Walk an allowlist of AST nodes instead of evaluating arbitrary Python."""
    if not expression or len(expression) > 120:
        raise ToolInputError("expression length is out of range")
    try:
        tree = ast.parse(expression, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 40:
            raise ToolInputError("expression is too complex")
        return evaluate(tree.body)
    except (SyntaxError, ArithmeticError, RecursionError) as exc:
        raise ToolInputError("invalid arithmetic") from exc


def evaluate(node: ast.AST) -> float | int:
    if isinstance(node, ast.Constant) and type(node.value) in (float, int):
        value = node.value
    elif isinstance(node, ast.BinOp) and type(node.op) in BINARY:
        value = BINARY[type(node.op)](evaluate(node.left), evaluate(node.right))
    elif isinstance(node, ast.UnaryOp) and type(node.op) in UNARY:
        value = UNARY[type(node.op)](evaluate(node.operand))
    else:
        raise ToolInputError("unsupported arithmetic operation")
    if not math.isfinite(value) or abs(value) > NUMBER_LIMIT:
        raise ToolInputError("arithmetic magnitude is out of range")
    return value


async def calculate(expression: str) -> JsonObject:
    return {"result": arithmetic(expression)}


class Weather:
    """Geocode a city, then request a Celsius reading from Open-Meteo."""

    def __init__(self, http: Http):
        self.http = http

    async def current(self, city: str) -> JsonObject:
        geocode = await self.http.json(
            "Open-Meteo geocoding",
            "GET",
            "https://geocoding-api.open-meteo.com/v1/search",
            retry=True,
            params={"name": city, "count": 1, "language": "en"},
        )
        results = geocode.get("results", [])
        if results == []:
            raise ToolInputError("city could not be geocoded")
        try:
            place = results[0]
            latitude, longitude = float(place["latitude"]), float(place["longitude"])
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError("invalid coordinates")
            label = ", ".join(part for part in (place["name"], place.get("country")) if part)
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderFormatError("invalid location data") from None
        forecast = await self.http.json(
            "Open-Meteo forecast",
            "GET",
            "https://api.open-meteo.com/v1/forecast",
            retry=True,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m",
                "temperature_unit": "celsius",
                "timezone": "auto",
            },
        )
        try:
            current = forecast["current"]
            temperature = float(current["temperature_2m"])
            datetime.fromisoformat(current["time"])
            if not math.isfinite(temperature):
                raise ValueError("non-finite temperature")
        except (KeyError, TypeError, ValueError):
            raise ProviderFormatError("invalid current weather data") from None
        return {
            "city": label,
            "temperature_c": temperature,
            "local_time": current["time"],
            "source": "Open-Meteo",
        }


async def demo_weather(city: str) -> JsonObject:
    sample = {"san jose": 19, "seattle": 12, "paris": 16}
    temperature = sample.get(city.casefold())
    if temperature is None:
        raise ToolInputError("the fixture has no reading for this city")
    return {"city": city, "temperature_c": temperature, "source": "fixture"}


def assistant_tools(weather: Callable[[str], Awaitable[JsonObject]]) -> Toolbox:
    return Toolbox(
        [
            Tool(
                "weather",
                "Get the current outdoor temperature in Celsius for a city.",
                "city",
                "City name only, for example San Jose.",
                weather,
                input_limit=80,
            ),
            Tool(
                "calculate",
                "Evaluate arithmetic with numbers, parentheses, +, -, *, /, and %.",
                "expression",
                "A numeric expression, for example (12 + 8) / 4.",
                calculate,
                input_limit=120,
            ),
        ]
    )
