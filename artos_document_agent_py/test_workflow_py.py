"""Use fictional evidence to exercise the boundaries a document reviewer depends on."""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from .domain_py import TEMPLATE, FactValue, Proposal, Proposals
from .ingest_py import FIXTURES, demo_snapshot, load_manifest
from .service_py import StudyService
from .storage_py import Conflict, Store
from .workflow_py import FixtureAgent, ModelAgent, retrieve, validate


class IngestionTests(unittest.TestCase):
    def test_sources_have_exact_locators_and_distinct_immutable_versions(self):
        first = demo_snapshot("baseline")
        second = demo_snapshot("amended")
        same = demo_snapshot("baseline")
        self.assertEqual(first, same)
        self.assertNotEqual(first.id, second.id)
        safety = next(row for row in first.evidence if row.source.filename == "safety-v1.csv")
        self.assertEqual(safety.locator, "CSV line 2")
        self.assertEqual(json.loads(safety.excerpt)["study"], first.study)
        design = next(row for row in first.evidence if row.source.filename == "protocol.json")
        self.assertEqual(design.locator, "/facts/0")
        self.assertEqual(len(design.source.sha256), 64)

    def test_manifest_cannot_ingest_rows_from_a_different_study(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = json.loads((FIXTURES / "baseline.json").read_text())
            manifest["study"] = "study-b202"
            (root / "baseline.json").write_text(json.dumps(manifest))
            for item in manifest["sources"]:
                shutil.copy(FIXTURES / item["filename"], root)
            with self.assertRaisesRegex(ValueError, "different study"):
                load_manifest(root / "baseline.json")

    def test_source_cannot_escape_manifest_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {
                "study": "study-a101",
                "label": "Invalid",
                "sources": [
                    {"document_id": "protocol", "version": "v1", "filename": "../private.json"}
                ],
            }
            (root / "invalid.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "regular file"):
                load_manifest(root / "invalid.json")

    def test_reimporting_a_changed_file_under_the_same_version_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in FIXTURES.iterdir():
                shutil.copy(path, root)
            store = Store(root / "study.sqlite3")
            original = store.ingest(load_manifest(root / "baseline.json"))
            path = root / "safety-v1.csv"
            path.write_text(path.read_text().replace("5,participants,49", "6,participants,49"))
            with self.assertRaisesRegex(Conflict, "already has different content"):
                store.ingest(load_manifest(root / "baseline.json"))
            self.assertEqual(store.snapshot(original.id), original)
            self.assertEqual(len(store.audit(original.study)), 1)


class QualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_retrieval_distinguishes_population_arm_and_timepoint(self):
        section = TEMPLATE["efficacy"]
        candidates = retrieve(demo_snapshot("baseline"), section)
        self.assertEqual([row.fact.value for row in candidates["active"]], ["31"])
        self.assertEqual([row.fact.value for row in candidates["placebo"]], ["20"])
        output = await FixtureAgent().propose(section, candidates)
        draft = validate(section, candidates, output, "test")
        self.assertEqual(draft.findings, ())
        self.assertIn("31/50 participants (62.0%)", draft.claims[0].sentence)

    async def test_missing_and_conflicting_sources_are_both_blocking(self):
        section = TEMPLATE["safety"]
        candidates = retrieve(demo_snapshot("conflict"), section)
        candidates["placebo"] = ()
        proposals = await FixtureAgent().propose(section, candidates)
        draft = validate(section, candidates, proposals, "test")
        self.assertEqual(draft.claims, ())
        self.assertEqual(
            [finding.code for finding in draft.findings],
            ["conflicting_evidence", "missing_evidence"],
        )
        self.assertEqual(len(draft.findings[0].evidence_ids), 2)

    async def test_real_citation_with_an_altered_number_is_rejected(self):
        section = TEMPLATE["safety"]
        candidates = retrieve(demo_snapshot("baseline"), section)
        proposals = await FixtureAgent().propose(section, candidates)
        active, placebo = proposals.claims
        changed = active.model_copy(
            update={"fact": FactValue(value="6", unit="participants", denominator=49)}
        )
        draft = validate(section, candidates, Proposals(claims=(changed, placebo)), "test")
        self.assertEqual([finding.code for finding in draft.findings], ["claim_mismatch"])
        self.assertEqual([claim.slot_id for claim in draft.claims], ["placebo"])

    async def test_citation_for_another_arm_and_duplicate_slot_are_rejected(self):
        section = TEMPLATE["efficacy"]
        candidates = retrieve(demo_snapshot("baseline"), section)
        proposals = await FixtureAgent().propose(section, candidates)
        active, placebo = proposals.claims
        wrong = Proposal(slot_id="active", evidence_id=placebo.evidence_id, fact=placebo.fact)
        draft = validate(section, candidates, Proposals(claims=(wrong, placebo, placebo)), "test")
        self.assertEqual(
            [finding.code for finding in draft.findings], ["invalid_citation", "duplicate_proposal"]
        )
        self.assertNotEqual(active.evidence_id, placebo.evidence_id)

    async def test_model_proposes_structured_claims_but_uses_same_qc_gate(self):
        section = TEMPLATE["safety"]
        candidates = retrieve(demo_snapshot("baseline"), section)
        proposed = await FixtureAgent().propose(section, candidates)
        model = AsyncMock()
        model.model = "fixture-model"
        model.structured.return_value = (proposed.model_dump(mode="json"), None)
        result = await ModelAgent(model).propose(section, candidates)
        self.assertEqual(result, proposed)
        self.assertEqual(model.structured.await_args.args[2], "section_proposals")


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "study.sqlite3"
        self.store = Store(self.database)
        self.baseline = self.store.ingest(demo_snapshot("baseline"))
        self.service = StudyService(self.store, FixtureAgent())

    async def test_identical_request_is_safe_to_retry_concurrently(self):
        requests = [
            self.service.generate(self.baseline.id, "safety", 0, "same-request") for _ in range(2)
        ]
        first, second = await asyncio.gather(*requests)
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.history(self.baseline.study, "safety")), 1)
        with self.assertRaisesRegex(Conflict, "already been used"):
            await self.service.generate(self.baseline.id, "design", 0, "same-request")

    async def test_competing_edits_cannot_publish_two_current_revisions(self):
        requests = [
            self.service.generate(self.baseline.id, "safety", 0, request_id)
            for request_id in ("writer-a", "writer-b")
        ]
        results = await asyncio.gather(*requests, return_exceptions=True)
        self.assertEqual(sum(isinstance(result, Conflict) for result in results), 1)
        self.assertEqual(len(self.store.history(self.baseline.study, "safety")), 1)

    async def test_b202_practice_data_blocks_then_repairs_the_missing_result(self):
        practice = Path(__file__).with_name("practice_data")
        incomplete = self.store.ingest(load_manifest(practice / "b202-missing.json"))
        revised = self.store.ingest(load_manifest(practice / "b202-corrected.json"))
        self.assertEqual(incomplete.study, "study-b202")
        for section in ("design", "disposition", "efficacy"):
            draft = await self.service.generate(incomplete.id, section, 0, section)
            self.service.review(incomplete.id, section, draft.number, "approve", "Reviewer", "")
        draft = await self.service.generate(incomplete.id, "safety", 0, "b202-safety-1")
        self.assertEqual(
            [(finding.slot_id, finding.code) for finding in draft.draft.findings],
            [("placebo", "missing_evidence")],
        )
        self.assertEqual([claim.slot_id for claim in draft.draft.claims], ["active"])
        with self.assertRaises(Conflict):
            self.service.review(incomplete.id, "safety", 1, "approve", "Reviewer", "")
        self.assertFalse(self.service.workspace(revised.id)["export_ready"])

        updated = await self.service.generate(revised.id, "safety", 1, "b202-safety-2")
        placebo = updated.draft.claims[1]
        self.assertIn("5/69 participants (7.2%)", placebo.sentence)
        source = next(row for row in updated.draft.evidence if row.id == placebo.evidence_id)
        self.assertEqual((source.source.filename, source.locator), ("safety-v2.json", "/facts/3"))
        self.service.review(revised.id, "safety", 2, "approve", "Reviewer", "")
        self.assertTrue(self.service.export(revised.id)["export_ready"])

    async def test_invalidated_section_reapproval_and_export_after_restart(self):
        for section in TEMPLATE:
            draft = await self.service.generate(self.baseline.id, section, 0, section)
            self.service.review(self.baseline.id, section, draft.number, "approve", "Reviewer", "")
        self.assertTrue(self.service.export(self.baseline.id)["export_ready"])
        amendment = self.store.ingest(demo_snapshot("amended"))
        reopened = StudyService(Store(self.database), FixtureAgent())
        with self.assertRaisesRegex(Conflict, "safety"):
            reopened.export(amendment.id)
        statuses = {
            section["id"]: section["status"]
            for section in reopened.workspace(amendment.id)["sections"]
        }
        self.assertEqual(
            statuses,
            {
                "design": "approved",
                "disposition": "approved",
                "efficacy": "approved",
                "safety": "stale",
            },
        )
        with self.assertRaisesRegex(Conflict, "Evidence changed"):
            reopened.review(amendment.id, "safety", 1, "approve", "Reviewer", "")
        revised = await reopened.generate(amendment.id, "safety", 1, "new-safety")
        self.assertIn("6/49", revised.draft.claims[0].sentence)
        self.assertIsNone(revised.review)
        reopened.review(amendment.id, "safety", 2, "approve", "Reviewer", "Checked amendment")
        self.assertTrue(reopened.export(amendment.id)["export_ready"])
        old = self.store.history(amendment.study, "safety")[1]
        self.assertIn("5/49", old.draft.claims[0].sentence)
        self.assertEqual(old.draft.evidence[0].source.version, "v1")
        self.assertEqual(old.review.decision, "approve")
        self.assertEqual(self.store.audit(amendment.study)[0]["event"], "review_recorded")
