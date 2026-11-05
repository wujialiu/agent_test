"""Failure drills for the worker, receiver, and third-party API boundaries."""

import asyncio
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from stage_01_model_tools_py.tools_py import demo_weather
from stage_01_model_tools_py.transport_py import Http, ProviderError, ProviderFormatError

from .assistant_py import (
    CALENDAR_SCOPE,
    DemoCalendar,
    GoogleWorkspace,
    OAuth,
    OAuthError,
    briefing,
    save_token,
    send_due,
)
from .feeds_py import MAX_FEED_BYTES, demo_feed, fetch_feeds, parse_feed
from .monitor_py import Monitor, analyze, schedule_slot
from .storage_py import Inbox, Reminder, Store, utc_text

NOW = datetime(2030, 1, 2, 3, 5, tzinfo=UTC)
SLOT = schedule_slot(NOW, 60)
MODULE = "stage_03_production_projects_py"


class FeedTests(unittest.IsolatedAsyncioTestCase):
    def test_feed_content_is_plain_text_with_a_stable_article_id(self):
        raw = b"""<rss><channel><item><title> &lt;b&gt; Hello &lt;/b&gt; </title>
            <description><![CDATA[<script>ignore()</script>Public text.]]></description>
            <link>https://example.invalid/post#heading</link>
            </item></channel></rss>"""
        (first,) = parse_feed(raw)
        (second,) = parse_feed(raw.replace(b"#heading", b"#different"))
        self.assertEqual(first.title, "Hello")
        self.assertEqual(first.text, "Public text.")
        self.assertEqual(first.id, second.id)

    def test_invalid_xml_and_entity_declarations_are_rejected(self):
        for raw in (b"<rss>", b"<!DOCTYPE rss><rss/>", b"\xff<rss/>"):
            with self.subTest(raw=raw):
                with self.assertRaises(ProviderFormatError):
                    parse_feed(raw)

    def test_atom_link_and_content_are_mapped_into_the_same_article_type(self):
        source = b"""<feed xmlns="http://www.w3.org/2005/Atom">
            <entry><title>Test</title><summary>Test summary</summary>
            <link rel="self" href="https://example.invalid/entry.xml"/>
            <link href="https://example.invalid/article"/></entry></feed>"""
        (item,) = parse_feed(source)
        self.assertEqual(item.url, "https://example.invalid/article")
        self.assertEqual(item.text, "Test summary")

    async def test_failed_and_oversized_feeds_are_counted_separately(self):
        xml = b"""<rss><channel><item><title>Success</title>
            <link>https://example.invalid/success</link></item></channel></rss>"""

        def serve(request):
            data = b"x" * (MAX_FEED_BYTES + 1) if request.url.path == "/large" else xml
            return httpx.Response(200, content=data)

        urls = [f"https://example.invalid/{path}" for path in ("good", "large", "duplicate")]
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            intake = await fetch_feeds(Http(client), urls)
        self.assertEqual(len(intake.items), 1)
        self.assertEqual(intake.unavailable, 1)

    async def test_model_fallback_handles_invalid_labels_but_not_bad_credentials(self):
        items = (await demo_feed()).items
        model = AsyncMock()
        model.structured.return_value = ({"items": [{"id": "made-up", "label": "positive"}]}, None)
        self.assertEqual((await analyze(items, model)).method, "keyword_fallback")
        model.structured.side_effect = ProviderError("model", 429)
        self.assertEqual((await analyze(items, model)).method, "keyword_fallback")
        model.structured.side_effect = ProviderError("model", 401)
        with self.assertRaises(ProviderError):
            await analyze(items, model)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.store = Store(self.path / "worker.sqlite3")
        self.inbox = Inbox(self.path / "inbox.sqlite3")

    async def test_concurrent_workers_deliver_one_report_for_a_slot(self):
        first = Monitor("concurrent", self.store, self.inbox, demo_feed)
        second = Monitor("concurrent", self.store, self.inbox, demo_feed)
        records = await asyncio.gather(first.run(SLOT), second.run(SLOT))
        self.assertEqual(records[0], records[1])
        self.assertTrue(records[0].delivered)
        self.assertEqual(len(self.inbox.recent()), 1)

    async def test_tick_drains_pending_reports_before_the_next_slot(self):
        def crash():
            raise RuntimeError("stop after receiving")

        first = Monitor("resume", self.store, self.inbox, demo_feed, after_delivery=crash)
        with self.assertRaisesRegex(RuntimeError, "stop after receiving"):
            await first.run(SLOT)

        newer = schedule_slot(datetime(2030, 1, 2, 4, tzinfo=UTC), 60)
        fetch = AsyncMock(side_effect=demo_feed)
        next_process = Monitor("resume", self.store, self.inbox, fetch)
        await next_process.tick(newer)
        fetch.assert_awaited_once()
        self.assertEqual(self.store.pending("resume"), [])
        self.assertEqual(len(self.inbox.recent()), 2)

    def test_process_exit_after_delivery_replays_without_duplicate(self):
        args = [sys.executable, "-m", MODULE, "--state", str(self.path), "monitor", "--slot", SLOT]
        first = subprocess.run(
            [*args, "--crash-after-delivery"], capture_output=True, text=True, timeout=15
        )
        self.assertEqual(first.returncode, 75, first.stderr)
        record = self.store.get("sentiment-demo", SLOT)
        self.assertFalse(record.delivered)
        self.assertEqual(len(self.inbox.recent()), 1)

        second = subprocess.run(args, capture_output=True, text=True, timeout=15)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(json.loads(second.stdout)["delivered"])
        self.assertEqual(len(self.inbox.recent()), 1)
        self.assertEqual(self.store.get("sentiment-demo", SLOT).key, record.key)

    def test_reminder_replay_uses_same_receiver_contract(self):
        due = Reminder("test", utc_text(NOW), "Practice the failure drill")
        self.store.add_reminder(due)
        self.store.add_reminder(due)
        with self.assertRaisesRegex(ValueError, "different content"):
            self.store.add_reminder(Reminder("test", utc_text(NOW), "Different reminder"))
        self.assertEqual(send_due(self.store, self.inbox, NOW), 1)
        self.assertEqual(send_due(self.store, self.inbox, NOW), 0)
        self.assertEqual(len(self.inbox.recent()), 1)

    def test_receiver_rejects_the_same_key_with_different_payload(self):
        self.assertTrue(self.inbox.deliver("key", "one"))
        self.assertFalse(self.inbox.deliver("key", "one"))
        with self.assertRaisesRegex(ValueError, "different content"):
            self.inbox.deliver("key", "two")

    def test_schedule_uses_absolute_time(self):
        same_time = datetime.fromisoformat("2030-01-01T19:05:00-08:00")
        self.assertEqual(schedule_slot(same_time, 60), SLOT)
        with self.assertRaises(ValueError):
            schedule_slot(NOW.replace(tzinfo=None), 60)


class PersonalAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_briefing_combines_fixture_calendar_and_weather(self):
        value = await briefing(DemoCalendar(), demo_weather, "Seattle", NOW)
        self.assertIn("fixture", value["events"][0]["title"])
        self.assertEqual(value["weather"]["temperature_c"], 12)
        self.assertEqual(value["outfit"], "Bring a light jacket.")

    async def test_google_reads_events_and_gmail_headers_without_message_bodies(self):
        oauth = AsyncMock()
        oauth.access_token.return_value = "fake"

        def serve(request):
            self.assertEqual(request.headers["Authorization"], "Bearer fake")
            self.assertEqual(request.method, "GET")
            if request.url.path.startswith("/calendar/"):
                self.assertEqual(request.url.params["timeMin"], utc_text(NOW))
                return httpx.Response(
                    200, json={"items": [{"start": {"date": "2030-01-02"}, "summary": "Meeting"}]}
                )
            if request.url.path.endswith("/messages"):
                return httpx.Response(200, json={"messages": [{"id": "abc"}]})
            self.assertEqual(request.url.params["format"], "metadata")
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Hello"},
                            {"name": "To", "value": "private example"},
                        ]
                    }
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            google = GoogleWorkspace(Http(client), oauth)
            self.assertEqual((await google.events(NOW))[0]["start"], "2030-01-02")
            self.assertEqual(await google.inbox_headers(), [{"subject": "Hello"}])

    @unittest.skipUnless(
        importlib.util.find_spec("google_auth_oauthlib"), "install requirements-labs.txt"
    )
    async def test_expired_token_is_refreshed_and_replaced_privately(self):
        data = {
            "token": "old",
            "refresh_token": "fake-refresh",
            "client_id": "fake-id",
            "client_secret": "fake-secret",
            "scopes": [CALENDAR_SCOPE],
            "expiry": "2020-01-01T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oauth" / "token.json"
            save_token(path, json.dumps(data))

            def refresh(credential, request):
                credential.token = "new"

            with patch(
                "google.oauth2.credentials.Credentials.refresh", autospec=True, side_effect=refresh
            ) as mock:
                self.assertEqual(await OAuth(path).access_token(), "new")
                mock.assert_called_once()

            self.assertEqual(json.loads(path.read_text())["token"], "new")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(OAuthError, "private"):
                await OAuth(path).access_token()
