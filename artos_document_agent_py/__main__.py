"""Serve the review workspace or import a local source manifest."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .domain_py import TEMPLATE
from .ingest_py import demo_snapshot, load_manifest
from .service_py import StudyService, snapshot_info
from .storage_py import Conflict, Store
from .workflow_py import FixtureAgent

DEFAULT_DB = Path(".agent_lab/artos/workspace.sqlite3")


async def demo(database: Path) -> dict:
    store = Store(database)
    service = StudyService(store, FixtureAgent())
    baseline = store.ingest(demo_snapshot("baseline"))
    for section in TEMPLATE:
        current = store.current(baseline.study, section)
        if (
            current
            and current.review
            and current.review.decision == "approve"
            and not service.stale(baseline, current)
        ):
            continue
        expected = current.number if current else 0
        draft = await service.generate(baseline.id, section, expected, f"demo-{section}-{expected}")
        service.review(
            baseline.id, section, draft.number, "approve", "demo-reviewer", "Synthetic data"
        )
    approved = service.export(baseline.id)
    amended = store.ingest(demo_snapshot("amended"))
    conflict = demo_snapshot("conflict")
    graph = service.graph
    conflict_state = await graph.ainvoke({"snapshot": conflict, "section": TEMPLATE["safety"]})
    return {
        "baseline_export_ready": approved["export_ready"],
        "amended_sections": {
            row["id"]: row["status"] for row in service.workspace(amended.id)["sections"]
        },
        "conflict_findings": [finding.model_dump() for finding in conflict_state["draft"].findings],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help=f"workspace database (default: {DEFAULT_DB})")
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve", help="start a local browser workspace and API")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument(
        "--live", action="store_true", help="allow the OpenAI model to propose claims"
    )
    ingest = commands.add_parser("ingest", help="import an operator-supplied JSON manifest")
    ingest.add_argument("manifest", type=Path)
    commands.add_parser("demo", help="exercise approval, amended sources, and a conflict offline")
    args = parser.parse_args()
    database = args.db or DEFAULT_DB
    try:
        if args.command == "serve":
            import uvicorn

            from .api_py import create_app

            credentials = None
            if args.live:
                credentials = (os.getenv("OPENAI_API_KEY", ""), os.getenv("OPENAI_MODEL", ""))
                if not all(value.strip() for value in credentials):
                    raise ValueError("Set OPENAI_API_KEY and OPENAI_MODEL to use live drafting.")
            uvicorn.run(
                create_app(database, live_credentials=credentials), host="127.0.0.1", port=args.port
            )
            return

        if args.command == "ingest":
            result = snapshot_info(Store(database).ingest(load_manifest(args.manifest)))
        elif args.db:
            if args.db.exists():
                raise ValueError("The demo requires a new database path.")
            result = asyncio.run(demo(args.db))
        else:
            with TemporaryDirectory() as directory:
                result = asyncio.run(demo(Path(directory) / "demo.sqlite3"))
        print(json.dumps(result, indent=2))
    except (ValueError, OSError, Conflict) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
