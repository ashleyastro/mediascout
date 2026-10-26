"""FastAPI entry point: web UI, REST API, and the MCP endpoint for Claude / ChatGPT."""

from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings

from . import db, demo, exporters, scanner
from .config import settings
from .extract import normalize_name
from .mcp_server import mcp
from .models import EmailRequest, ScanParams, SuppressionRequest
from .outlets import load_seed_if_empty, parse_csv
from .results import journalists_for_scan
from .topics import INDUSTRIES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"

mcp_app = mcp.streamable_http_app(
    streamable_http_path=settings.mcp_path,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(settings.allowed_hosts),
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=["https://claude.ai", "https://chatgpt.com", settings.public_base_url],
    ),
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    db.mark_interrupted_scans()
    if settings.demo_mode:
        await demo.seed_demo_async()
        logging.info("Demo mode: fictional dataset loaded; crawling and email are off")
    else:
        loaded = load_seed_if_empty()
        if loaded:
            logging.info("Loaded %d seed outlets", loaded)
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="MediaScout", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optional password gate for the UI and API. The MCP endpoint is protected by MCP_SECRET."""
    path = request.url.path
    if not settings.app_password or path.startswith("/mcp") or path == "/healthz":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            _, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            password = ""
        if secrets.compare_digest(password, settings.app_password):
            return await call_next(request)
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="MediaScout"'})


def _not_in_demo(action: str) -> None:
    if settings.demo_mode:
        raise HTTPException(403, f"{action} is turned off in the demo.")


def _scan_or_404(scan_id: str) -> dict:
    scan = db.get_scan(scan_id)
    if not scan:
        raise HTTPException(404, "Scan not found")
    return scan


# ---------------------------------------------------------------- UI

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}


# ---------------------------------------------------------------- reference data

@app.get("/api/config")
def config() -> dict:
    return {
        "demo_mode": settings.demo_mode,
        "sample_scan_id": demo.SAMPLE_SCAN_ID if settings.demo_mode else None,
        "demo_executives": list(demo.DEMO_EXECUTIVES) if settings.demo_mode else [],
        "mcp_url": settings.public_base_url + settings.mcp_path,
        "email_enabled": bool(settings.smtp_host and settings.smtp_from) and not settings.demo_mode,
        "hunter_enabled": bool(settings.hunter_api_key),
        "newsapi_enabled": bool(settings.newsapi_key),
        "gdelt_enabled": settings.gdelt_enabled,
    }


@app.get("/api/industries")
def industries() -> list[dict]:
    return [{"id": k, "label": v["label"], "terms": v["terms"]} for k, v in INDUSTRIES.items()]


@app.get("/api/outlets")
def outlets(region: str | None = None, limit: int = Query(2000, le=10000)) -> dict:
    rows = db.list_outlets([region] if region else None, limit)
    return {"counts": db.outlet_counts(), "outlets": rows}


@app.post("/api/outlets/import")
async def import_outlets(file: UploadFile = File(...)) -> dict:
    _not_in_demo("Importing outlets")
    text = (await file.read()).decode("utf-8", errors="replace")
    rows, errors = parse_csv(text)
    if not rows:
        raise HTTPException(400, {"message": "No valid rows found", "errors": errors[:20]})
    db.upsert_outlets(rows)
    return {"imported": len(rows), "errors": errors[:50], "counts": db.outlet_counts()}


# ---------------------------------------------------------------- scans

@app.post("/api/scans", status_code=202)
async def create_scan(params: ScanParams) -> dict:
    return {"id": scanner.start_scan(params)}


@app.get("/api/scans")
def list_scans() -> list[dict]:
    return db.list_scans()


@app.get("/api/scans/{scan_id}")
def get_scan(scan_id: str) -> dict:
    return _scan_or_404(scan_id)


@app.post("/api/scans/{scan_id}/cancel")
def cancel_scan(scan_id: str) -> dict:
    _scan_or_404(scan_id)
    return {"cancelled": scanner.cancel_scan(scan_id)}


@app.get("/api/scans/{scan_id}/journalists")
def journalists(scan_id: str) -> dict:
    scan = _scan_or_404(scan_id)
    return {"scan": scan, "summary": exporters.scan_summary(scan), "journalists": journalists_for_scan(scan_id)}


@app.get("/api/scans/{scan_id}/export")
def export(scan_id: str, format: str = Query("csv", pattern="^(csv|json|md|prompt)$")) -> Response:
    scan = _scan_or_404(scan_id)
    rows = journalists_for_scan(scan_id)
    name = f"media-list-{scan_id}"
    if format == "csv":
        body, media, ext = exporters.to_csv(rows), "text/csv; charset=utf-8", "csv"
    elif format == "json":
        body, media, ext = exporters.to_json(scan, rows), "application/json", "json"
    elif format == "md":
        body, media, ext = exporters.to_markdown(scan, rows), "text/markdown; charset=utf-8", "md"
    else:
        body, media, ext = exporters.ai_prompt(scan, rows), "text/plain; charset=utf-8", "txt"
    return Response(body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{name}.{ext}"'})


@app.post("/api/scans/{scan_id}/email")
async def email(scan_id: str, req: EmailRequest) -> dict:
    _not_in_demo("Email")
    scan = _scan_or_404(scan_id)
    rows = journalists_for_scan(scan_id)
    try:
        await exporters.send_email_async(settings, req.to, scan, rows, req.note)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # SMTP errors
        logging.exception("email failed")
        raise HTTPException(502, f"Email delivery failed: {exc}") from exc
    return {"sent": True, "to": req.to, "journalists": len(rows)}


# ---------------------------------------------------------------- opt-outs

def _suppression_key(value: str) -> str:
    value = value.strip()
    return value.lower() if "@" in value else normalize_name(value)


@app.get("/api/suppressions")
def suppressions() -> list[str]:
    return sorted(db.list_suppressions())


@app.post("/api/suppressions")
def add_suppression(req: SuppressionRequest) -> dict:
    _not_in_demo("Editing the opt-out list")
    db.add_suppression(_suppression_key(req.value))
    return {"ok": True}


@app.delete("/api/suppressions")
def delete_suppression(value: str) -> dict:
    _not_in_demo("Editing the opt-out list")
    db.remove_suppression(_suppression_key(value))
    return {"ok": True}


@app.exception_handler(ValueError)
async def value_error(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=422)


# The MCP app is mounted last so the routes above take precedence.
app.mount("/", mcp_app)
