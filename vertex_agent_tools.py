"""
Deterministic tool wrappers for Vertex agent orchestration.
"""

from __future__ import annotations

from typing import Any, Dict, List

from agent_service import EstimateRequest, InsulationEstimationService
from gcs_storage import get_storage


SERVICE = InsulationEstimationService()


def tool_estimate_project(input_data: Dict[str, Any]) -> Dict[str, Any]:
    request = EstimateRequest(**input_data)
    result = SERVICE.estimate(request)
    return {"success": True, "result": result.model_dump()}


def tool_extract_specifications(input_data: Dict[str, Any]) -> Dict[str, Any]:
    pdf_path = input_data["pdf_path"]
    specs = SERVICE.extract_specifications(pdf_path)
    return {"success": True, "specifications": specs, "count": len(specs)}


def tool_extract_measurements(input_data: Dict[str, Any]) -> Dict[str, Any]:
    measurement_data = input_data.get("measurement_data", [])
    measurements = SERVICE.extract_measurements(measurement_data)
    return {"success": True, "measurements": measurements, "count": len(measurements)}


def tool_upload_file(input_data: Dict[str, Any]) -> Dict[str, Any]:
    destination_path = input_data["destination_path"]
    file_text = input_data["file_text"]
    storage = get_storage()
    uri = storage.upload_file(
        file_data=file_text.encode("utf-8"),
        destination_path=destination_path,
        content_type="text/plain",
    )
    return {"success": True, "uri": uri}


def tool_get_download_url(input_data: Dict[str, Any]) -> Dict[str, Any]:
    source_path = input_data["source_path"]
    expiration_minutes = int(input_data.get("expiration_minutes", 60))
    storage = get_storage()
    url = storage.get_download_url(source_path, expiration_minutes=expiration_minutes)
    return {"success": True, "download_url": url}


VERTEX_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "estimate_project",
        "description": "Generate insulation estimate and quote summary from structured specs and measurements.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string"},
                "measurements": {"type": "array", "items": {"type": "object"}},
                "specifications": {"type": "array", "items": {"type": "object"}},
                "pricebook_path": {"type": "string"},
                "markup": {"type": "number", "default": 1.0},
                "labor_rate": {"type": "number", "default": 65.0},
                "contingency_percent": {"type": "number", "default": 10.0},
            },
            "required": ["project_name", "measurements", "specifications"],
        },
    },
    {
        "name": "extract_specifications",
        "description": "Extract insulation specifications from a PDF path.",
        "input_schema": {
            "type": "object",
            "properties": {"pdf_path": {"type": "string"}},
            "required": ["pdf_path"],
        },
    },
    {
        "name": "extract_measurements",
        "description": "Normalize and validate measurement entries.",
        "input_schema": {
            "type": "object",
            "properties": {"measurement_data": {"type": "array", "items": {"type": "object"}}},
            "required": ["measurement_data"],
        },
    },
    {
        "name": "upload_file",
        "description": "Upload text output into configured storage backend.",
        "input_schema": {
            "type": "object",
            "properties": {
                "destination_path": {"type": "string"},
                "file_text": {"type": "string"},
            },
            "required": ["destination_path", "file_text"],
        },
    },
    {
        "name": "get_download_url",
        "description": "Generate a signed download URL for an existing stored file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string"},
                "expiration_minutes": {"type": "integer", "default": 60},
            },
            "required": ["source_path"],
        },
    },
]


TOOL_REGISTRY = {
    "estimate_project": tool_estimate_project,
    "extract_specifications": tool_extract_specifications,
    "extract_measurements": tool_extract_measurements,
    "upload_file": tool_upload_file,
    "get_download_url": tool_get_download_url,
}
