"""Regression cases for source boundaries, model output, and framework parity."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from stage_01_model_tools_py.contracts_py import Usage
from stage_01_model_tools_py.transport_py import Http, ProviderError, ProviderFormatError

from .engines_py import ENGINES, ScriptedResearchModel, direct, graph, react, team
from .eval_py import CASES, evaluate, summary
from .index_py import (
    DEFAULT_CORPUS,
    ChromaIndex,
    Chunk,
    HashEmbedder,
    Hit,
    MemoryIndex,
    OpenAIEmbedder,
    load_chunks,
)
from .report_py import Claim, Draft, ExtractiveWriter, make_report, parse_draft


class ChunkTests(unittest.TestCase):
    def test_overlapping_windows_cover_tail_without_duplication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "doc.md").write_text("# Heading\none two three four five six seven")
            chunks = load_chunks(root, size=4, overlap=1)
            self.assertEqual(
                [chunk.text for chunk in chunks], ["one two three four", "four five six seven"]
            )
            self.assertEqual(chunks, load_chunks(root, size=4, overlap=1))
            self.assertEqual(len({chunk.id for chunk in chunks}), 2)

    def test_symlink_cannot_import_an_external_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "file.txt"
            target.write_text("private source")
            (root / "linked.md").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlinks"):
                load_chunks(root)

    def test_bad_chunk_options_fail_before_reading(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            load_chunks(DEFAULT_CORPUS, size=10, overlap=10)

    def test_overlong_document_fails_instead_of_silently_truncating(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "large.md").write_text("a" * 90_000)
            with self.assertRaisesRegex(ValueError, "too large"):
                load_chunks(root)


class EvidenceTests(unittest.TestCase):
    def test_quotes_are_checked_against_the_cited_passage(self):
        evidence = [Hit(Chunk("one", "runbook.md", "Use a stable delivery key."), 0.6)]
        draft = Draft(
            (
                Claim("Reuse the key.", "one", "Use a stable  delivery key."),
                Claim("The key expires daily.", "one", "expires daily"),
                Claim("Reuse the key.", "other", "Use a stable delivery key."),
            ),
            Usage(12, 7),
        )
        report = make_report("How?", evidence, draft, ("verify",))
        self.assertEqual(report.rejected, 2)
        self.assertEqual(report.claims, (draft.claims[0],))
        self.assertIn("[runbook.md#one]", report.render())
        self.assertEqual(report.usage.output_tokens, 7)

    def test_bad_model_fields_are_rejected_before_constructing_a_report(self):
        for data in (
            None,
            {"claims": "anything"},
            {"claims": [None]},
            {"claims": [{"chunk_id": 12, "quote": "a", "text": "b"}]},
        ):
            with self.subTest(data=data):
                with self.assertRaises(ProviderFormatError):
                    parse_draft(data)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.index = await MemoryIndex.build(load_chunks(DEFAULT_CORPUS))
        self.writer = ExtractiveWriter()

    async def test_all_installed_engines_use_the_same_evidence(self):
        question = CASES[1][0]
        baseline = await direct(question, self.index, self.writer, 3)
        self.assertIn("delivery.md", baseline.render())
        for name, engine in ENGINES.items():
            if name == "team" and not importlib.util.find_spec("autogen_agentchat"):
                continue
            with self.subTest(engine=name):
                component = ScriptedResearchModel(self.writer) if name == "react" else self.writer
                candidate = await engine(question, self.index, component, 3)
                self.assertEqual(candidate.claims, baseline.claims)
                self.assertEqual(candidate.evidence, baseline.evidence)

    @unittest.skipUnless(
        importlib.util.find_spec("autogen_agentchat"), "install requirements-labs.txt"
    )
    async def test_team_exposes_the_same_typed_provider_failure_as_other_engines(self):
        class UnavailableIndex:
            async def search(self, *_):
                raise ProviderError("test index", 429)

        with self.assertRaises(ProviderError) as error:
            await team(CASES[1][0], UnavailableIndex(), self.writer, 3)
        self.assertEqual(error.exception.status, 429)

    async def test_graph_skips_drafting_when_nothing_is_retrieved(self):
        class UncalledWriter:
            async def draft(self, *_):
                raise AssertionError("empty retrieval should skip drafting")

        report = await graph(CASES[-1][0], self.index, UncalledWriter(), 3)
        self.assertEqual(report.steps, ("retrieve", "verify"))
        self.assertIn("Insufficient evidence", report.render())

    async def test_react_uses_the_tool_observation(self):
        report = await react(CASES[1][0], self.index, ScriptedResearchModel(self.writer), 3)
        self.assertEqual(report.steps, ("search_corpus", "answer", "verify"))
        self.assertTrue(report.claims)
        self.assertEqual(report.rejected, 0)

    async def test_eval_counts_errors_as_well_as_abstentions(self):
        async def run(question):
            if question == CASES[0][0]:
                raise ProviderFormatError("details remain private")
            return await direct(question, self.index, self.writer, 3)

        rows = await evaluate(run, (CASES[0], CASES[-1]))
        data = json.loads(summary(rows, (1, 2)))
        self.assertEqual(data["errors"], 1)
        self.assertEqual(data["correct_abstentions"], [1, 1])
        self.assertEqual(data["expected_citations"], [0, 1])
        self.assertEqual(data["estimated_generation_usd"], 0)


class VectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedding_rows_are_sorted_back_to_input_order(self):
        async def respond(request):
            self.assertEqual(json.loads(request.content)["input"], ["one", "two"])
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0, 1]},
                        {"index": 0, "embedding": [1, 0]},
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await OpenAIEmbedder(Http(client), "fixture").embed(["one", "two"])
        self.assertEqual(result, [[1.0, 0.0], [0.0, 1.0]])

    async def test_duplicate_embedding_indexes_are_rejected(self):
        response = httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": [0, 1]},
                    {"index": 0, "embedding": [1, 0]},
                ]
            },
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
            with self.assertRaises(ProviderFormatError):
                await OpenAIEmbedder(Http(client), "fixture").embed(["one", "two"])

    @unittest.skipUnless(importlib.util.find_spec("chromadb"), "install requirements-labs.txt")
    async def test_chroma_reopens_and_changed_corpus_cannot_return_stale_evidence(self):
        first = [Chunk("first", "old.md", "retry timeout backoff")]
        second = [Chunk("second", "new.md", "retry timeout policy")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            original = await ChromaIndex.build(path, first, HashEmbedder())
            reopened = await ChromaIndex.build(path, first, HashEmbedder())
            changed = await ChromaIndex.build(path, second, HashEmbedder())
            self.assertEqual(original.collection.name, reopened.collection.name)
            self.assertEqual(
                [hit.chunk.id for hit in await reopened.search("retry timeout", 3)], ["first"]
            )
            self.assertEqual(
                [hit.chunk.id for hit in await changed.search("retry timeout", 3)], ["second"]
            )
