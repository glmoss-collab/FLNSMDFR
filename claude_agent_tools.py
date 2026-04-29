"""
Claude agent tools for insulation estimation.

Contains helper functions for PDF extraction, Claude/Vertex interaction,
and project metadata/specification/measurement extraction.
"""

import os
import json
import base64
import logging
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

import pdfplumber

try:
    from pdf2image import convert_from_path
except ImportError:
    convert_from_path = None

from pydantic import ValidationError

from hvac_insulation_estimator import SpecificationExtractor, DrawingMeasurementExtractor
from pydantic_models import (
    InsulationSpecExtracted,
    MeasurementItemExtracted,
    ProjectInfoExtracted,
    ToolResponse,
)
from vertex_ai_client import get_claude_client

logger = logging.getLogger(__name__)


def pdf_to_base64_images(pdf_path: str, pages: Optional[List[int]] = None) -> List[Tuple[int, str]]:
    if convert_from_path is None:
        raise ImportError("pdf2image is required to convert PDFs into images")
    images = convert_from_path(pdf_path, first_page=min(pages) if pages else 1, last_page=max(pages) if pages else None)
    result: List[Tuple[int, str]] = []
    for idx, image in enumerate(images):
        page_number = pages[idx] if pages else idx + 1
        buffer = image.tobytes() if hasattr(image, "tobytes") else None
        if buffer is None:
            from io import BytesIO
            buff = BytesIO()
            image.save(buff, format="PNG")
            buffer = buff.getvalue()
        result.append((page_number, base64.b64encode(buffer).decode("utf-8")))
    return result


def extract_text_from_pdf(pdf_path: str, pages: Optional[List[int]] = None) -> Dict[int, str]:
    text_by_page: Dict[int, str] = {}
    with pdfplumber.open(pdf_path) as pdf:
        page_indices = pages if pages else list(range(1, len(pdf.pages) + 1))
        for page_number in page_indices:
            page = pdf.pages[page_number - 1]
            text_by_page[page_number] = page.extract_text() or ""
    return text_by_page


def _clean_json_text(text: str) -> str:
    if "```json" in text:
        start = text.find("```json") + len("```json")
        end = text.find("```", start)
        return text[start:end].strip() if end != -1 else text[start:].strip()
    if "{" in text and "}" in text:
        start = text.find("{")
        end = text.rfind("}") + 1
        return text[start:end]
    return text.strip()


def _parse_response_json(response_text: str) -> Any:
    try:
        return json.loads(_clean_json_text(response_text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse JSON from Claude response: {exc}\n{text[:200]}") from exc


def get_claude_messages_client(use_vertex_ai: bool = False):
    return get_claude_client(use_vertex_ai=use_vertex_ai)


def extract_project_info(pdf_path: str, use_vertex_ai: bool = False) -> ToolResponse:
    try:
        text_by_page = extract_text_from_pdf(pdf_path, pages=[1, 2, 3])
        client = get_claude_messages_client(use_vertex_ai=use_vertex_ai)
        prompt = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Extract high-level project metadata from the following pages."},
                {"type": "text", "text": "\n\n".join([f'Page {n}: {text[:1500]}' for n, text in text_by_page.items()])}
            ]
        }
        response = client.messages.create(model="claude-opus-4-5-20251101", max_tokens=1500, messages=[prompt])
        response_text = response.content[0].text if response.content else ""
        raw = _parse_response_json(response_text)
        project = ProjectInfoExtracted(**raw)
        return ToolResponse(success=True, data=project.dict(), metadata={"source": pdf_path})
    except ValidationError as exc:
        logger.warning("Project info validation failed: %s", exc)
        return ToolResponse(success=False, error=str(exc))
    except Exception as exc:
        return ToolResponse(success=False, error=str(exc))


def extract_specifications(pdf_path: str, use_vertex_ai: bool = False) -> ToolResponse:
    extractor = SpecificationExtractor()
    try:
        specs = extractor.extract_from_pdf(pdf_path)
        validated = []
        for spec in specs:
            normalized = {
                "system_type": spec.system_type,
                "size_range": spec.size_range,
                "thickness": spec.thickness,
                "material": spec.material,
                "facing": spec.facing or "unfaced",
                "special_requirements": spec.special_requirements,
                "location": spec.location,
                "confidence": 0.8,
                "spec_text": "",
                "page_number": 1,
            }
            validated.append(InsulationSpecExtracted(**normalized).dict())
        return ToolResponse(success=True, data=validated, metadata={"source": pdf_path})
    except ValidationError as exc:
        logger.warning("Specification validation failed: %s", exc)
        return ToolResponse(success=False, error=str(exc))
    except Exception as exc:
        return ToolResponse(success=False, error=str(exc))


def extract_measurements(pdf_path: str, use_vertex_ai: bool = False) -> ToolResponse:
    extractor = DrawingMeasurementExtractor()
    try:
        measurements = extractor.extract_from_pdf(pdf_path)
        validated = []
        for item in measurements:
            validated.append(MeasurementItemExtracted(
                item_id=item.item_id,
                system_type=item.system_type,
                size=item.size,
                length=item.length,
                location=item.location,
                elevation_changes=item.elevation_changes,
                fittings=item.fittings,
                notes=item.notes,
                page_number=1,
                sheet_number=None,
                confidence=0.8
            ).dict())
        return ToolResponse(success=True, data=validated, metadata={"source": pdf_path})
    except ValidationError as exc:
        logger.warning("Measurement validation failed: %s", exc)
        return ToolResponse(success=False, error=str(exc))
    except Exception as exc:
        return ToolResponse(success=False, error=str(exc))
