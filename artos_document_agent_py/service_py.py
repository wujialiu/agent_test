"""Coordinate the reviewer workflow independently of the browser and CLI."""

import asyncio

from .domain_py import TEMPLATE, TEMPLATE_VERSION, Snapshot
from .storage_py import Conflict, Missing, Revision, Store
from .workflow_py import DraftAgent, build_workflow, evidence_signature, retrieve


def snapshot_info(snapshot: Snapshot) -> dict:
    return {"id": snapshot.id, "study": snapshot.study, "label": snapshot.label}


class StudyService:
    def __init__(self, store: Store, agent: DraftAgent):
        self.store = store
        self.graph = build_workflow(agent)

    async def generate(
        self, snapshot_id: str, section: str, expected: int, request_id: str
    ) -> Revision:
        if section not in TEMPLATE:
            raise Missing("Section not found.")
        snapshot = await asyncio.to_thread(self.store.snapshot, snapshot_id)
        replay = await asyncio.to_thread(
            self.store.check_draft_request, snapshot, section, expected, request_id
        )
        if replay:
            return replay
        # Leave the database available during provider calls. save_draft()
        # repeats the revision check inside its transaction to catch races.
        async with asyncio.timeout(45):
            state = await self.graph.ainvoke({"snapshot": snapshot, "section": TEMPLATE[section]})
        return await asyncio.to_thread(
            self.store.save_draft, snapshot, section, expected, request_id, state["draft"]
        )

    @staticmethod
    def stale(snapshot: Snapshot, revision: Revision) -> bool:
        section = TEMPLATE[revision.section]
        signature = evidence_signature(section, retrieve(snapshot, section))
        return (
            revision.draft.template_version != TEMPLATE_VERSION
            or revision.draft.evidence_signature != signature
        )

    def workspace(self, snapshot_id: str) -> dict:
        snapshot = self.store.snapshot(snapshot_id)
        current = self.store.current_sections(snapshot.study, tuple(TEMPLATE))
        sections = []
        for section_id, section in TEMPLATE.items():
            revision = current[section_id]
            stale = bool(revision and self.stale(snapshot, revision))
            if revision is None:
                status = "not_drafted"
            elif stale:
                status = "stale"
            elif revision.review and revision.review.decision == "request_changes":
                status = "changes_requested"
            elif revision.draft.findings:
                status = "blocked"
            elif revision.review:
                status = "approved"
            else:
                status = "pending_review"
            sections.append(
                {"id": section_id, "title": section.title, "status": status, "revision": revision}
            )
        return {
            "snapshot": snapshot_info(snapshot),
            "sections": sections,
            "export_ready": all(section["status"] == "approved" for section in sections),
        }

    def review(self, snapshot_id, section, revision, decision, reviewer, note) -> Revision:
        snapshot = self.store.snapshot(snapshot_id)
        current = self.store.current(snapshot.study, section)
        if current and current.number == revision and self.stale(snapshot, current):
            raise Conflict(
                "Evidence changed for the selected snapshot. Regenerate before reviewing."
            )
        return self.store.review(snapshot.study, section, revision, decision, reviewer, note)

    def export(self, snapshot_id: str) -> dict:
        workspace = self.workspace(snapshot_id)
        if not workspace["export_ready"]:
            blocked = ", ".join(
                f"{section['id']} ({section['status']})"
                for section in workspace["sections"]
                if section["status"] != "approved"
            )
            raise Conflict(f"Export requires current approvals for all sections: {blocked}.")
        # Preserve each approved revision and its evidence. The chosen snapshot
        # can reuse an earlier approved section with identical evidence.
        return workspace
