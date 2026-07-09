"""
API-first backend for Vertex agent and insulation workflows.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent_service import EstimateRequest, InsulationEstimationService
from data.bigquery_sink import get_bigquery_sink
from firestore_cache import FirestoreCache
from gcs_storage import get_storage
from vertex_agent_orchestrator import VertexAgentOrchestrator

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="FLNSMDFR Vertex Agent API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

web_dir = Path(__file__).parent / "web"
if web_dir.exists():
    app.mount("/web", StaticFiles(directory=str(web_dir), html=True), name="web")

service = InsulationEstimationService()
orchestrator = VertexAgentOrchestrator()
storage = get_storage()


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    session_id: str
    message: str
    tools_executed: list[Dict[str, Any]] = Field(default_factory=list)


def _load_session(session_id: str) -> list[Dict[str, Any]]:
    try:
        cache = FirestoreCache(collection_name="agent_sessions")
        return cache.get(session_id, category="conversation") or []
    except Exception:  # noqa: BLE001
        return []


def _save_session(session_id: str, conversation: list[Dict[str, Any]]) -> None:
    try:
        cache = FirestoreCache(collection_name="agent_sessions")
        cache.set(session_id, conversation, category="conversation", ttl=86400)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Session cache write failed: %s", exc)


def _audit_event(event_type: str, payload: Dict[str, Any]) -> None:
    row = {
        "event_type": event_type,
        "payload_json": json.dumps(payload),
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    table_id = "agent_telemetry.events"
    try:
        sink = get_bigquery_sink()
        sink.insert_outcome(table_id, row)
    except Exception as exc:  # noqa: BLE001
        logger.info("Audit sink unavailable: %s", exc)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> FileResponse:
    index_file = web_dir / "chat.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Chat UI not found")
    return FileResponse(str(index_file))


@app.post("/agent/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    session_id = request.session_id or str(uuid.uuid4())
    history = _load_session(session_id)
    result = orchestrator.run(request.message, conversation=history)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "Agent execution failed"))
    conversation = result.get("conversation", [])
    _save_session(session_id, conversation)
    _audit_event("agent_chat", {"session_id": session_id, "message": request.message})
    return ChatResponse(
        session_id=session_id,
        message=result.get("message", ""),
        tools_executed=result.get("tools_executed", []),
    )


@app.post("/agent/jobs/estimate")
def estimate_job(request: EstimateRequest) -> Dict[str, Any]:
    estimate = service.estimate(request)
    _audit_event("estimate_job", {"project_name": request.project_name, "total": estimate.total})
    return {"success": True, "result": estimate.model_dump()}


@app.post("/files/upload")
async def upload_file(file: UploadFile = File(...)) -> Dict[str, Any]:
    data = await file.read()
    destination = f"uploads/{uuid.uuid4()}-{file.filename}"
    uri = storage.upload_file(data, destination, content_type=file.content_type)
    _audit_event("file_upload", {"filename": file.filename, "destination": destination})
    return {"success": True, "uri": uri, "path": destination}


@app.get("/files/{file_id}/download-url")
def download_url(file_id: str) -> Dict[str, Any]:
    path = f"uploads/{file_id}"
    url = storage.get_download_url(path, expiration_minutes=60)
    return {"success": True, "download_url": url}
