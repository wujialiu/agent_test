"""Local FastAPI surface for drafting, reviewing, and exporting study sections."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from stage_01_model_tools_py.model_py import OpenAIModel
from stage_01_model_tools_py.transport_py import Http, ProviderError, ProviderFormatError

from .domain_py import Record, Sections, ShortText
from .ingest_py import demo_snapshot
from .service_py import StudyService, snapshot_info
from .storage_py import Conflict, Missing, Revision, Store
from .workflow_py import FixtureAgent, ModelAgent

WEB_BUILD = Path(__file__).parent / "web" / "dist"
SnapshotID = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class GenerateRequest(Record):
    expected_revision: Annotated[int, Field(ge=0, le=1_000_000, strict=True)]
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]


class ReviewRequest(Record):
    revision: Annotated[int, Field(ge=1, le=1_000_000, strict=True)]
    decision: Literal["approve", "request_changes"]
    reviewer: ShortText
    note: Annotated[str, Field(max_length=800)] = ""

    @model_validator(mode="after")
    def require_explanation(self):
        if self.decision == "request_changes" and not self.note:
            raise ValueError("Describe the changes the author should make.")
        return self


def create_app(
    database: Path,
    *,
    live_credentials: tuple[str, str] | None = None,
    web_build: Path | None = WEB_BUILD,
) -> FastAPI:
    store = Store(database)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(timeout=12) as http:
            agent = (
                ModelAgent(OpenAIModel(Http(http), *live_credentials))
                if live_credentials
                else FixtureAgent()
            )
            app.state.study_service = StudyService(store, agent)
            yield

    app = FastAPI(title="Study document review lab", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        # The browser workspace is local and uses same-origin JSON requests.
        # Block a third-party page from sending writes to the loopback service.
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin writes are disabled."}, status_code=403)
        return await call_next(request)

    def respond(status: int, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=status)

    @app.exception_handler(Missing)
    async def missing(_, error):
        return respond(404, error)

    @app.exception_handler(Conflict)
    async def conflict(_, error):
        return respond(409, error)

    @app.exception_handler(ProviderError)
    @app.exception_handler(ProviderFormatError)
    async def provider_failed(_, error):
        return respond(502, error)

    @app.exception_handler(TimeoutError)
    async def deadline(_, error):
        return JSONResponse({"detail": "Draft generation timed out."}, status_code=504)

    @app.get("/api/snapshots")
    def snapshots():
        return store.snapshots()

    @app.post("/api/scenarios/{scenario}")
    def seed(scenario: Literal["baseline", "amended", "conflict"]):
        return snapshot_info(store.ingest(demo_snapshot(scenario)))

    @app.get("/api/snapshots/{snapshot_id}/workspace")
    def workspace(snapshot_id: SnapshotID):
        return app.state.study_service.workspace(snapshot_id)

    @app.post("/api/snapshots/{snapshot_id}/sections/{section}/draft")
    async def generate(
        snapshot_id: SnapshotID, section: Sections, data: GenerateRequest
    ) -> Revision:
        return await app.state.study_service.generate(
            snapshot_id, section, data.expected_revision, data.request_id
        )

    @app.post("/api/snapshots/{snapshot_id}/sections/{section}/review")
    def review(snapshot_id: SnapshotID, section: Sections, data: ReviewRequest) -> Revision:
        return app.state.study_service.review(
            snapshot_id, section, data.revision, data.decision, data.reviewer, data.note
        )

    @app.get("/api/snapshots/{snapshot_id}/sections/{section}/history")
    def history(snapshot_id: SnapshotID, section: Sections) -> list[Revision]:
        return store.history(store.snapshot(snapshot_id).study, section)

    @app.get("/api/snapshots/{snapshot_id}/audit")
    def audit(snapshot_id: SnapshotID):
        return store.audit(store.snapshot(snapshot_id).study)

    @app.get("/api/snapshots/{snapshot_id}/export")
    def export(snapshot_id: SnapshotID):
        return app.state.study_service.export(snapshot_id)

    if web_build and (web_build / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_build, html=True), name="review-workspace")
    else:

        @app.get("/")
        def root():
            return {
                "docs": "/docs",
                "ui": "Build the React workspace with npm ci and npm run build.",
            }

    return app
