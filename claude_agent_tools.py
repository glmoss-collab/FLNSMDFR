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

from dataclasses import asdict

from hvac_insulation_estimator import (
    DrawingMeasurementExtractor,
    InsulationSpec,
    MeasurementItem,
    PricingEngine,
    QuoteGenerator,
    SpecificationExtractor,
)
from pydantic_models import (
    InsulationSpecExtracted,
    MeasurementItemExtracted,
    ProjectInfoExtracted,
    ToolResponse,
)
from vertex_ai_client import get_claude_client

logger = logging.getLogger(__name__)


CLAUDE_VISION_MAX_DIMENSION = 1568


def pdf_to_base64_images(
    pdf_path: str,
    pages: Optional[List[int]] = None,
    max_dimension: int = CLAUDE_VISION_MAX_DIMENSION,
) -> List[Tuple[int, str]]:
    """Render PDF pages as base64-encoded PNGs sized for Claude vision.

    Prefers the PyMuPDF-based optimized path from utils_pdf when available;
    falls back to pdf2image + PIL with the same max_dimension cap.
    """
    try:
        from utils_pdf import PYMUPDF_AVAILABLE, pdf_to_base64_images_optimized
        if PYMUPDF_AVAILABLE:
            return pdf_to_base64_images_optimized(
                pdf_path, pages=pages, max_dimension=max_dimension
            )
    except ImportError:
        pass

    if convert_from_path is None:
        raise ImportError("pdf2image is required to convert PDFs into images")

    from io import BytesIO

    images = convert_from_path(
        pdf_path,
        first_page=min(pages) if pages else 1,
        last_page=max(pages) if pages else None,
    )
    result: List[Tuple[int, str]] = []
    for idx, image in enumerate(images):
        page_number = pages[idx] if pages else idx + 1
        if max(image.size) > max_dimension:
            image.thumbnail((max_dimension, max_dimension))
        buff = BytesIO()
        image.save(buff, format="PNG")
        result.append((page_number, base64.b64encode(buff.getvalue()).decode("utf-8")))
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
        raise ValueError(f"Failed to parse JSON from Claude response: {exc}\n{response_text[:200]}") from exc


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
        return ToolResponse(success=True, data=project.model_dump(), metadata={"source": pdf_path})
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
            validated.append(InsulationSpecExtracted(**normalized).model_dump())
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
            ).model_dump())
        return ToolResponse(success=True, data=validated, metadata={"source": pdf_path})
    except ValidationError as exc:
        logger.warning("Measurement validation failed: %s", exc)
        return ToolResponse(success=False, error=str(exc))
    except Exception as exc:
        return ToolResponse(success=False, error=str(exc))


# ============================================================================
# DOWNSTREAM TOOLS (validate / cross-ref / pricing / quote)
# ============================================================================
# These tools consume the dicts produced by the extract_* tools and wrap the
# engines in hvac_insulation_estimator. They return flat dicts (not
# ToolResponse) because that is what the agent dispatchers in
# claude_estimation_agent.InsulationEstimationAgent._update_session_data and
# hvac_insulation_skill._update_session_data ultimately read with
# result.get("total"), result.get("specifications"), etc.

_DUCT_THICKNESS_BAND = (1.0, 3.0)   # ASHRAE 90.1 commercial-typical
_PIPE_THICKNESS_BAND = (0.5, 3.0)
_OUTDOOR_PROTECTIONS = {"aluminum_jacket", "weatherproofing", "stainless_bands"}


def _to_insulation_spec(d: Dict[str, Any]) -> InsulationSpec:
    """Build the engine's InsulationSpec dataclass from an extracted dict.

    Collapses fine-grained extracted system types (e.g. "supply_duct",
    "chilled_water_pipe") down to the engine's coarse family ("duct", "pipe",
    "equipment") so PricingEngine._find_applicable_spec can match against
    measurements (which already use coarse names).
    """
    return InsulationSpec(
        system_type=_spec_system_family(d.get("system_type", "")) or d.get("system_type", ""),
        size_range=d.get("size_range", "all"),
        thickness=float(d.get("thickness", 0.0) or 0.0),
        material=d.get("material", ""),
        facing=d.get("facing"),
        special_requirements=list(d.get("special_requirements", []) or []),
        location=d.get("location", "indoor"),
    )


def _to_measurement_item(d: Dict[str, Any]) -> MeasurementItem:
    """Build the engine's MeasurementItem dataclass from an extracted dict."""
    return MeasurementItem(
        item_id=d.get("item_id", ""),
        system_type=d.get("system_type", ""),
        size=d.get("size", ""),
        length=float(d.get("length", 0.0) or 0.0),
        location=d.get("location", ""),
        elevation_changes=int(d.get("elevation_changes", 0) or 0),
        fittings=dict(d.get("fittings", {}) or {}),
        notes=list(d.get("notes", []) or []),
    )


def _spec_system_family(system_type: str) -> str:
    """Collapse the extracted system_type to the engine's coarse family."""
    if not system_type:
        return ""
    if "duct" in system_type:
        return "duct"
    if "pipe" in system_type or system_type == "refrigerant_pipe":
        return "pipe"
    if system_type == "equipment":
        return "equipment"
    return system_type


def validate_specifications(specifications: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run sanity checks against ASHRAE-style heuristics on extracted specs.

    Each issue carries severity ("error" | "warning" | "info"), a message,
    and the offending spec's ``system_type`` and ``page_number`` when known.
    """
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    recommendations: List[Dict[str, Any]] = []

    for spec in specifications or []:
        family = _spec_system_family(spec.get("system_type", ""))
        thickness = float(spec.get("thickness", 0.0) or 0.0)
        location = (spec.get("location") or "indoor").lower()
        special_reqs = set(spec.get("special_requirements") or [])
        ident = {
            "spec_or_item_id": spec.get("system_type"),
            "page_number": spec.get("page_number"),
        }

        if thickness <= 0:
            errors.append({"severity": "error", "message": "Thickness must be > 0", **ident})
        else:
            band = _DUCT_THICKNESS_BAND if family == "duct" else _PIPE_THICKNESS_BAND
            lo, hi = band
            if thickness < lo:
                warnings.append({
                    "severity": "warning",
                    "message": f"Thickness {thickness}\" is below typical {family} range {lo}-{hi}\"",
                    "suggestion": f"Verify against ASHRAE 90.1 minimums for {family}",
                    **ident,
                })
            elif thickness > hi:
                warnings.append({
                    "severity": "warning",
                    "message": f"Thickness {thickness}\" is above typical {family} range {lo}-{hi}\"",
                    "suggestion": "Confirm with engineer; may indicate cold-storage / cryogenic",
                    **ident,
                })

        if location in {"outdoor", "exposed_to_weather", "exposed"}:
            if not (_OUTDOOR_PROTECTIONS & special_reqs):
                errors.append({
                    "severity": "error",
                    "message": "Outdoor/exposed spec missing weather protection",
                    "suggestion": "Add aluminum_jacket, stainless_bands, or weatherproofing",
                    **ident,
                })

        if family == "pipe" and "chilled" in (spec.get("system_type") or ""):
            if "vapor_barrier" not in special_reqs and "mastic_seal" not in special_reqs:
                recommendations.append({
                    "severity": "info",
                    "message": "Chilled-water pipe typically requires a continuous vapor barrier",
                    "suggestion": "Consider vapor_barrier or mastic_seal",
                    **ident,
                })

    if errors:
        status = "error"
    elif warnings:
        status = "warning"
    else:
        status = "pass"

    summary = (
        f"Validated {len(specifications or [])} specs: "
        f"{len(errors)} errors, {len(warnings)} warnings, {len(recommendations)} recommendations."
    )

    return {
        "success": True,
        "status": status,
        "total_items_validated": len(specifications or []),
        "errors": errors,
        "warnings": warnings,
        "recommendations": recommendations,
        "summary": summary,
    }


def cross_reference_data(
    specifications: List[Dict[str, Any]],
    measurements: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Match measurements to specs and flag gaps in either direction."""
    spec_families = {_spec_system_family(s.get("system_type", "")) for s in (specifications or [])}
    spec_families.discard("")

    unmatched_measurements: List[Dict[str, Any]] = []
    for m in measurements or []:
        if _spec_system_family(m.get("system_type", "")) not in spec_families:
            unmatched_measurements.append({
                "item_id": m.get("item_id"),
                "system_type": m.get("system_type"),
                "reason": "no specification covers this system family",
            })

    measurement_families = {_spec_system_family(m.get("system_type", "")) for m in (measurements or [])}
    measurement_families.discard("")
    unused_specs: List[Dict[str, Any]] = []
    for s in specifications or []:
        if _spec_system_family(s.get("system_type", "")) not in measurement_families:
            unused_specs.append({
                "system_type": s.get("system_type"),
                "page_number": s.get("page_number"),
                "reason": "no measurement references this system family",
            })

    return {
        "success": True,
        "unmatched_measurements": unmatched_measurements,
        "unused_specs": unused_specs,
        "matched_count": len(measurements or []) - len(unmatched_measurements),
        "summary": (
            f"{len(measurements or []) - len(unmatched_measurements)} of "
            f"{len(measurements or [])} measurements matched a spec; "
            f"{len(unused_specs)} specs unused."
        ),
    }


def calculate_pricing(
    measurements: List[Dict[str, Any]],
    specifications: List[Dict[str, Any]],
    pricebook_path: Optional[str] = None,
    markup: float = 1.0,
    contingency_percent: float = 10.0,
) -> Dict[str, Any]:
    """Compute materials + labor + total for the project.

    Returns a flat dict whose top-level ``total`` matches what the agent
    dispatcher reads. ``markup`` multiplies unit prices; ``contingency_percent``
    is added on top of subtotal.
    """
    try:
        engine = PricingEngine(price_book_path=pricebook_path, markup=markup)
        meas_objs = [_to_measurement_item(m) for m in (measurements or [])]
        spec_objs = [_to_insulation_spec(s) for s in (specifications or [])]

        materials = engine.calculate_materials(meas_objs, spec_objs)
        labor_hours, labor_cost = engine.calculate_labor(materials)

        material_total = sum(m.total_price for m in materials)
        subtotal = material_total + labor_cost
        contingency = subtotal * (contingency_percent / 100.0)
        total = subtotal + contingency

        return {
            "success": True,
            "materials": [asdict(m) for m in materials],
            "material_total": round(material_total, 2),
            "labor_hours": round(labor_hours, 2),
            "labor_cost": round(labor_cost, 2),
            "subtotal": round(subtotal, 2),
            "contingency_percent": contingency_percent,
            "contingency": round(contingency, 2),
            "total": round(total, 2),
            "metadata": {
                "pricebook_path": pricebook_path,
                "markup": markup,
                "spec_count": len(spec_objs),
                "measurement_count": len(meas_objs),
            },
        }
    except Exception as exc:
        logger.exception("calculate_pricing failed")
        return {"success": False, "error": str(exc)}


def generate_quote(
    project_name: str,
    measurements: List[Dict[str, Any]],
    specifications: List[Dict[str, Any]],
    pricebook_path: Optional[str] = None,
    markup: float = 1.0,
) -> Dict[str, Any]:
    """Build a full ProjectQuote (number, totals, notes, material list)."""
    try:
        engine = PricingEngine(price_book_path=pricebook_path, markup=markup)
        meas_objs = [_to_measurement_item(m) for m in (measurements or [])]
        spec_objs = [_to_insulation_spec(s) for s in (specifications or [])]

        materials = engine.calculate_materials(meas_objs, spec_objs)
        labor_hours, labor_cost = engine.calculate_labor(materials)

        quote = QuoteGenerator().generate_quote(
            project_name=project_name,
            measurements=meas_objs,
            materials=materials,
            labor_hours=labor_hours,
            labor_cost=labor_cost,
            specs=spec_objs,
        )

        return {
            "success": True,
            "project_name": quote.project_name,
            "quote_number": quote.quote_number,
            "date": quote.date,
            "labor_hours": round(quote.labor_hours, 2),
            "labor_rate": quote.labor_rate,
            "subtotal": round(quote.subtotal, 2),
            "contingency_percent": quote.contingency_percent,
            "total": round(quote.total, 2),
            "materials": [asdict(m) for m in quote.materials],
            "material_list": quote.material_list,
            "notes": quote.notes,
        }
    except Exception as exc:
        logger.exception("generate_quote failed")
        return {"success": False, "error": str(exc)}


# ============================================================================
# AGENT TOOL REGISTRY
# ============================================================================
# Maps tool names to handlers and exposes Anthropic-format tool schemas so the
# orchestrator (claude_estimation_agent.InsulationEstimationAgent) can advertise
# capabilities to Claude and dispatch tool_use blocks.

AGENT_TOOLS: Dict[str, Any] = {
    "extract_project_info": extract_project_info,
    "extract_specifications": extract_specifications,
    "extract_measurements": extract_measurements,
    "validate_specifications": validate_specifications,
    "cross_reference_data": cross_reference_data,
    "calculate_pricing": calculate_pricing,
    "generate_quote": generate_quote,
}


_PDF_PATH_PROPERTY = {
    "pdf_path": {
        "type": "string",
        "description": "Absolute or workspace-relative path to the PDF document.",
    },
    "use_vertex_ai": {
        "type": "boolean",
        "description": "Route the request through Vertex AI Model Garden instead of the direct Anthropic API.",
        "default": False,
    },
}

_SPEC_ARRAY_SCHEMA = {
    "type": "array",
    "description": "List of insulation specification dicts as produced by extract_specifications.",
    "items": {"type": "object"},
}

_MEASUREMENT_ARRAY_SCHEMA = {
    "type": "array",
    "description": "List of measurement dicts as produced by extract_measurements.",
    "items": {"type": "object"},
}


def get_tool_schemas() -> List[Dict[str, Any]]:
    """Return Anthropic tool-use schemas for every tool in AGENT_TOOLS."""
    return [
        {
            "name": "extract_project_info",
            "description": (
                "Extract project metadata (name, number, client, location, "
                "architect, engineer) from the cover sheet of a construction PDF."
            ),
            "input_schema": {
                "type": "object",
                "properties": _PDF_PATH_PROPERTY,
                "required": ["pdf_path"],
            },
        },
        {
            "name": "extract_specifications",
            "description": (
                "Extract insulation specifications (system type, thickness, material, "
                "facing, location) from a specification PDF."
            ),
            "input_schema": {
                "type": "object",
                "properties": _PDF_PATH_PROPERTY,
                "required": ["pdf_path"],
            },
        },
        {
            "name": "extract_measurements",
            "description": (
                "Extract measurement takeoff items (lengths, sizes, fittings, "
                "elevation changes) from mechanical drawings."
            ),
            "input_schema": {
                "type": "object",
                "properties": _PDF_PATH_PROPERTY,
                "required": ["pdf_path"],
            },
        },
        {
            "name": "validate_specifications",
            "description": (
                "Validate a list of extracted insulation specs against ASHRAE-style "
                "heuristics (thickness bands by system family, weather protection on "
                "outdoor specs, vapor-barrier on chilled-water pipe). Returns a "
                "report with errors, warnings, and recommendations."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"specifications": _SPEC_ARRAY_SCHEMA},
                "required": ["specifications"],
            },
        },
        {
            "name": "cross_reference_data",
            "description": (
                "Cross-check extracted specs against measurements. Flags measurements "
                "with no matching spec system family and specs with no matching "
                "measurement."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "specifications": _SPEC_ARRAY_SCHEMA,
                    "measurements": _MEASUREMENT_ARRAY_SCHEMA,
                },
                "required": ["specifications", "measurements"],
            },
        },
        {
            "name": "calculate_pricing",
            "description": (
                "Compute materials, labor, contingency, and total cost for the "
                "project. Wraps PricingEngine. Returns a flat dict with top-level "
                "'total' and a breakdown."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "measurements": _MEASUREMENT_ARRAY_SCHEMA,
                    "specifications": _SPEC_ARRAY_SCHEMA,
                    "pricebook_path": {
                        "type": "string",
                        "description": "Optional path to a JSON pricebook overriding defaults.",
                    },
                    "markup": {
                        "type": "number",
                        "description": "Multiplier applied to unit prices (default 1.0).",
                        "default": 1.0,
                    },
                    "contingency_percent": {
                        "type": "number",
                        "description": "Percent of subtotal added as contingency (default 10).",
                        "default": 10.0,
                    },
                },
                "required": ["measurements", "specifications"],
            },
        },
        {
            "name": "generate_quote",
            "description": (
                "Generate a full ProjectQuote (quote number, totals, notes, "
                "consolidated material list). Wraps QuoteGenerator."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_name": {"type": "string"},
                    "measurements": _MEASUREMENT_ARRAY_SCHEMA,
                    "specifications": _SPEC_ARRAY_SCHEMA,
                    "pricebook_path": {"type": "string"},
                    "markup": {"type": "number", "default": 1.0},
                },
                "required": ["project_name", "measurements", "specifications"],
            },
        },
    ]
