import secrets
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import Body, Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.errors import ServiceError
from app.providers import Registry
from app.runtime import Runtime
from app.schemas import (
    CleanupMailboxes,
    CreateMailbox,
    MailboxCleanupPreview,
    MailboxCleanupResult,
    MailboxPage,
    MailboxView,
    MessagePage,
    MessageView,
    OperationPage,
    OperationView,
    SendMessage,
)
from app.service import Service

bearer = HTTPBearer(auto_error=False)
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def create_app(settings: Settings | None = None, registry: Registry | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = Runtime(settings or Settings.from_yaml(), registry)
        app.state.service = app.state.runtime.refresh()
        app.state.config_store = app.state.runtime.config_store
        yield

    app = FastAPI(title="Temp Mail Gateway", version="0.1.0", lifespan=lifespan)

    if (FRONTEND_DIST / "index.html").is_file():
        index_file = FRONTEND_DIST / "index.html"

        def frontend():
            return FileResponse(index_file, headers={"Cache-Control": "no-cache"})

        for path in ("/", "/inbox", "/providers", "/operations", "/settings"):
            app.add_api_route(path, frontend, methods=["GET"], include_in_schema=False)

        if (FRONTEND_DIST / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="frontend-assets")

    def service(request: Request) -> Service:
        # FastAPI caches this dependency for authentication and business logic within a request.
        snapshot = request.app.state.runtime.refresh()
        request.app.state.service = snapshot
        return snapshot

    def require_auth(
        svc: Annotated[Service, Depends(service)],
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        expected = svc.settings.api_token
        if credentials is None or not secrets.compare_digest(credentials.credentials.encode(), expected.encode()):
            raise ServiceError("UNAUTHORIZED", "A valid bearer token is required", 401)

    @app.exception_handler(ServiceError)
    async def on_service_error(request: Request, exc: ServiceError):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else {}
        if request.url.path == "/v1/config":
            headers["Cache-Control"] = "no-store"
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        # Pydantic's default error includes input values, which can contain mail bodies or tokens.
        details = [{"location": list(error["loc"]), "type": error["type"]} for error in exc.errors()]
        if request.url.path == "/v1/config":
            return JSONResponse(
                {"error": {"code": "CONFIG_INVALID", "message": "Invalid configuration changes"}},
                status_code=422,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse({"error": {"code": "VALIDATION_ERROR", "message": "Invalid request", "details": details}}, status_code=422)

    @app.exception_handler(sqlite3.OperationalError)
    async def on_database_error(request: Request, exc: sqlite3.OperationalError):
        return JSONResponse({"error": {"code": "STORAGE_UNAVAILABLE", "message": "Storage is temporarily unavailable"}}, status_code=503)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(svc: Annotated[Service, Depends(service)]):
        with svc.db.connect() as conn:
            conn.execute("SELECT 1 FROM operations LIMIT 1")
        return {"status": "ready"}

    auth = [Depends(require_auth)]

    @app.get("/v1/config", dependencies=auth)
    def get_config(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        result = request.app.state.runtime.configuration()
        request.app.state.service = request.app.state.runtime.service
        return result

    @app.put("/v1/config", dependencies=auth)
    def save_config(request: Request, response: Response, body: Annotated[dict, Body()]):
        response.headers["Cache-Control"] = "no-store"
        result = request.app.state.config_store.save(body)
        request.app.state.service = request.app.state.runtime.service
        return result

    @app.get("/v1/capabilities", dependencies=auth)
    def capabilities(svc: Annotated[Service, Depends(service)]):
        return {"providers": [{"id": provider.id, "capabilities": asdict(provider.capabilities)} for provider in svc.registry.all()]}

    @app.post("/v1/mailboxes", status_code=202, response_model=OperationView, dependencies=auth)
    def create_mailbox(body: CreateMailbox, idempotency_key: IdempotencyKey, svc: Annotated[Service, Depends(service)]):
        payload = body.model_dump()
        payload["required_capabilities"] = sorted(set(payload["required_capabilities"]))
        return svc.submit("create", idempotency_key, payload)

    @app.get("/v1/mailboxes", response_model=MailboxPage, dependencies=auth)
    def list_mailboxes(
        svc: Annotated[Service, Depends(service)],
        limit: Limit = 50,
        offset: Offset = 0,
        email: Annotated[str | None, Query(max_length=320)] = None,
    ):
        return svc.list_mailboxes(limit, offset, email)

    @app.get("/v1/mailboxes/cleanup-preview", response_model=MailboxCleanupPreview, dependencies=auth)
    def mailbox_cleanup_preview(response: Response, svc: Annotated[Service, Depends(service)]):
        response.headers["Cache-Control"] = "no-store"
        return svc.mailbox_cleanup_preview()

    @app.post("/v1/mailboxes/cleanup", response_model=MailboxCleanupResult, dependencies=auth)
    def cleanup_mailboxes(body: CleanupMailboxes, response: Response, svc: Annotated[Service, Depends(service)]):
        response.headers["Cache-Control"] = "no-store"
        return svc.cleanup_mailboxes(body.cutoff, body.expected_count, body.revision)

    @app.get("/v1/mailboxes/{mailbox_id}", response_model=MailboxView, dependencies=auth)
    def get_mailbox(mailbox_id: str, svc: Annotated[Service, Depends(service)]):
        return svc.get_mailbox(mailbox_id)

    @app.delete("/v1/mailboxes/{mailbox_id}", status_code=202, response_model=OperationView, dependencies=auth)
    def delete_mailbox(mailbox_id: str, idempotency_key: IdempotencyKey, svc: Annotated[Service, Depends(service)]):
        return svc.submit("delete", idempotency_key, {}, mailbox_id)

    @app.get("/v1/mailboxes/{mailbox_id}/messages", response_model=MessagePage, dependencies=auth)
    def list_messages(mailbox_id: str, svc: Annotated[Service, Depends(service)], limit: Limit = 50, offset: Offset = 0):
        return svc.list_messages(mailbox_id, limit, offset)

    @app.get("/v1/mailboxes/{mailbox_id}/messages/{message_id}", response_model=MessageView, dependencies=auth)
    def get_message(mailbox_id: str, message_id: str, svc: Annotated[Service, Depends(service)]):
        return svc.get_message(mailbox_id, message_id)

    @app.post("/v1/mailboxes/{mailbox_id}/messages", status_code=202, response_model=OperationView, dependencies=auth)
    def send_message(mailbox_id: str, body: SendMessage, idempotency_key: IdempotencyKey, svc: Annotated[Service, Depends(service)]):
        return svc.submit("send", idempotency_key, body.model_dump(), mailbox_id)

    @app.get("/v1/operations", response_model=OperationPage, dependencies=auth)
    def list_operations(svc: Annotated[Service, Depends(service)], limit: Limit = 50, offset: Offset = 0):
        return svc.list_operations(limit, offset)

    @app.get("/v1/operations/{operation_id}", response_model=OperationView, dependencies=auth)
    def get_operation(operation_id: str, svc: Annotated[Service, Depends(service)]):
        return svc.get_operation(operation_id)

    return app
