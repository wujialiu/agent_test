"""Run the sentiment worker, inspect its inbox, or try the personal assistant."""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from stage_01_model_tools_py.model_py import OpenAIModel
from stage_01_model_tools_py.tools_py import ToolInputError, Weather, demo_weather
from stage_01_model_tools_py.transport_py import Http, ProviderError, ProviderFormatError

from .assistant_py import DemoCalendar, GoogleWorkspace, OAuth, OAuthError, briefing, send_due
from .feeds_py import demo_feed, fetch_feeds
from .monitor_py import Monitor, schedule_slot
from .storage_py import Inbox, Reminder, Store, utc_text

DEFAULT_STATE = Path(".agent_lab/production")


def crash() -> None:
    # Exit without running cleanup handlers to model a process killed mid-job.
    os._exit(75)


def output(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


async def run(args: argparse.Namespace) -> None:
    now = datetime.now(UTC)
    if args.command == "authorize":
        await asyncio.to_thread(OAuth(args.token, args.gmail).authorize, args.credentials)
        print(f"Authorization saved to {args.token}")
        return
    if args.command == "inbox":
        output(
            [
                {"key": row["key"], **json.loads(row["payload"])}
                for row in Inbox(args.state / "inbox.sqlite3").recent()
            ]
        )
        return
    if args.command in {"remind", "reminders-tick"}:
        store = Store(args.state / "worker.sqlite3")
        if args.command == "remind":
            when = utc_text(datetime.fromisoformat(args.at))
            if not 1 <= len(args.id) <= 100 or not 1 <= len(args.text.strip()) <= 500:
                raise ValueError("reminders require an ID and up to 500 characters of text")
            store.add_reminder(Reminder(args.id, when, args.text.strip()))
            print(f"Scheduled {args.id} for {when}")
        else:
            print(f"New reminders: {send_due(store, Inbox(args.state / 'inbox.sqlite3'), now)}")
        return

    async with httpx.AsyncClient(timeout=10) as client:
        http = Http(client)
        if args.command == "briefing":
            workspace = (
                GoogleWorkspace(http, OAuth(args.token, args.gmail)) if args.google else None
            )
            weather = Weather(http).current if args.live_weather else demo_weather
            result = await briefing(workspace or DemoCalendar(), weather, args.city, now)
            if args.gmail:
                result["inbox_headers"] = await workspace.inbox_headers()
            output(result)
            return

        model = (
            OpenAIModel(http, os.getenv("OPENAI_API_KEY", ""), os.getenv("OPENAI_MODEL", ""))
            if args.live_model
            else None
        )

        async def intake():
            return await fetch_feeds(http, args.feed) if args.feed else await demo_feed()

        mode = "live" if args.feed or model else "demo"
        worker = Monitor(
            args.job or f"sentiment-{mode}",
            Store(args.state / "worker.sqlite3"),
            Inbox(args.state / "inbox.sqlite3"),
            intake,
            model,
            after_delivery=crash if args.crash_after_delivery else None,
        )
        slot = (
            utc_text(datetime.fromisoformat(args.slot))
            if args.slot
            else schedule_slot(now, args.minutes)
        )
        result = await worker.tick(slot)
        output({"key": result.key, "delivered": result.delivered, **json.loads(result.payload)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state", type=Path, default=DEFAULT_STATE, help="local runtime data directory"
    )
    parser.add_argument("--verbose", action="store_true", help="emit worker events to stderr")
    commands = parser.add_subparsers(dest="command", required=True)

    monitor = commands.add_parser("monitor", help="resume pending reports and run the current slot")
    monitor.add_argument(
        "--job", help="stable job name (use a different name per feed configuration)"
    )
    monitor.add_argument(
        "--feed", action="append", default=[], help="operator-configured HTTPS RSS/Atom URL"
    )
    monitor.add_argument("--live-model", action="store_true")
    monitor.add_argument("--minutes", type=int, default=60, help="UTC slot duration")
    monitor.add_argument("--slot", help="explicit ISO 8601 slot with offset")
    monitor.add_argument(
        "--crash-after-delivery", action="store_true", help="simulate a crash (exit 75)"
    )

    commands.add_parser("inbox", help="show the local receiver's recent reports and reminders")
    authorize = commands.add_parser("authorize", help="sign into the Google test account")
    authorize.add_argument(
        "--credentials", required=True, type=Path, help="Desktop OAuth client JSON"
    )
    brief = commands.add_parser("briefing", help="read upcoming events and suggest an outfit")
    brief.add_argument("--city", default="San Jose")
    brief.add_argument("--google", action="store_true", help="use real Google Calendar events")
    brief.add_argument("--live-weather", action="store_true")
    for command in (authorize, brief):
        command.add_argument(
            "--gmail", action="store_true", help="also request or read Gmail metadata"
        )
        command.add_argument(
            "--token", type=Path, help="override the token path under the selected state directory"
        )

    remind = commands.add_parser("remind", help="schedule a durable local reminder")
    remind.add_argument("--id", required=True, help="stable caller-supplied reminder ID")
    remind.add_argument("--at", required=True, help="ISO 8601 date and time including UTC offset")
    remind.add_argument("text")
    commands.add_parser("reminders-tick", help="deliver due reminders to the local inbox")

    args = parser.parse_args()
    if hasattr(args, "token") and args.token is None:
        args.token = args.state / "oauth" / "workspace-token.json"
    if args.command == "briefing" and args.gmail and not args.google:
        parser.error("briefing --gmail requires --google")
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)

    expected = (
        ValueError,
        ToolInputError,
        ProviderError,
        ProviderFormatError,
        OAuthError,
        sqlite3.Error,
        TimeoutError,
    )
    try:
        try:
            asyncio.run(run(args))
        except* expected as errors:
            # TaskGroup wraps dependency failures. CLI errors should remain concise.
            def display(group):
                for error in group.exceptions:
                    if isinstance(error, BaseExceptionGroup):
                        display(error)
                    else:
                        print(f"{type(error).__name__}: {error}", file=sys.stderr)

            display(errors)
            raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except ModuleNotFoundError as exc:
        parser.error(f"{exc}. Install requirements-labs.txt for Google OAuth.")


if __name__ == "__main__":
    main()
