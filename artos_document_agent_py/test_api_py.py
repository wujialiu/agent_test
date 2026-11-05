"""Exercise the same HTTP contracts the reviewer interface calls."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from .api_py import create_app


class ReviewAPITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = create_app(Path(directory.name) / "test.sqlite3", web_build=None)
        self.client = self.enterContext(TestClient(app))

    def scenario(self, name: str) -> str:
        response = self.client.post(f"/api/scenarios/{name}", json={})
        self.assertEqual(response.status_code, 200, response.text)
        return "/api/snapshots/" + response.json()["id"]

    def test_conflict_surfaces_both_sources_and_blocks_approval_and_export(self):
        base = self.scenario("conflict")
        section = base + "/sections/safety"
        draft = self.client.post(
            section + "/draft", json={"expected_revision": 0, "request_id": "first-safety"}
        )
        self.assertEqual(draft.status_code, 200, draft.text)
        first = draft.json()["draft"]["findings"][0]
        self.assertEqual(first["code"], "conflicting_evidence")
        self.assertEqual(len(first["evidence_ids"]), 2)
        review = self.client.post(
            section + "/review", json={"revision": 1, "decision": "approve", "reviewer": "Reviewer"}
        )
        self.assertEqual(review.status_code, 409)
        self.assertEqual(self.client.get(base + "/export").status_code, 409)

    def test_request_changes_requires_note_and_preserves_historical_decision(self):
        base = self.scenario("baseline")
        section = base + "/sections/design"
        request = {"expected_revision": 0, "request_id": "design-1"}
        self.assertEqual(self.client.post(section + "/draft", json=request).status_code, 200)
        decision = {"revision": 1, "decision": "request_changes", "reviewer": "Reviewer"}
        self.assertEqual(self.client.post(section + "/review", json=decision).status_code, 422)
        decision["note"] = "Review design wording"
        self.assertEqual(self.client.post(section + "/review", json=decision).status_code, 200)
        self.assertEqual(self.client.post(section + "/review", json=decision).status_code, 200)
        self.assertEqual(
            self.client.post(
                section + "/draft", json={"expected_revision": 1, "request_id": "design-2"}
            ).status_code,
            200,
        )
        self.assertEqual(self.client.post(section + "/review", json=decision).status_code, 409)
        history = self.client.get(section + "/history").json()
        self.assertIsNone(history[0]["review"])
        self.assertEqual(history[1]["review"]["note"], "Review design wording")

    def test_loopback_rejects_cross_origin_write(self):
        response = self.client.post(
            "/api/scenarios/baseline", json={}, headers={"Origin": "https://example.invalid"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/snapshots").json(), [])
