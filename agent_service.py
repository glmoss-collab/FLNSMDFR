"""
Application service layer for insulation estimation workflows.

This module is intentionally UI-agnostic so Streamlit, APIs, and agent runtimes
can share the same deterministic business operations.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from hvac_insulation_estimator import (
    DrawingMeasurementExtractor,
    InsulationSpec,
    MeasurementItem,
    PricingEngine,
    QuoteGenerator,
    SpecificationExtractor,
)


class EstimateRequest(BaseModel):
    project_name: str = Field(default="Commercial Mechanical Project")
    measurements: List[Dict[str, Any]]
    specifications: List[Dict[str, Any]]
    pricebook_path: Optional[str] = None
    markup: float = Field(default=1.0, gt=0)
    labor_rate: float = Field(default=65.0, ge=0)
    contingency_percent: float = Field(default=10.0, ge=0)


class EstimateResponse(BaseModel):
    project_name: str
    quote_number: str
    material_total: float
    labor_hours: float
    labor_rate: float
    labor_cost: float
    subtotal: float
    contingency_percent: float
    total: float
    materials: List[Dict[str, Any]]
    material_list: List[Dict[str, Any]]
    notes: List[str]


class InsulationEstimationService:
    """Business service with no framework/UI coupling."""

    @staticmethod
    def _to_spec(spec_data: Dict[str, Any]) -> InsulationSpec:
        return InsulationSpec(
            system_type=spec_data.get("system_type", "duct"),
            size_range=spec_data.get("size_range", "all"),
            thickness=float(spec_data.get("thickness", 1.0)),
            material=spec_data.get("material", "fiberglass"),
            facing=spec_data.get("facing"),
            special_requirements=list(spec_data.get("special_requirements", []) or []),
            location=spec_data.get("location", "indoor"),
        )

    @staticmethod
    def _to_measurement(measurement_data: Dict[str, Any]) -> MeasurementItem:
        return MeasurementItem(
            item_id=measurement_data.get("item_id", "MANUAL_0"),
            system_type=measurement_data.get("system_type", "duct"),
            size=measurement_data.get("size", '12"'),
            length=float(measurement_data.get("length", 0.0)),
            location=measurement_data.get("location", "Indoor"),
            elevation_changes=int(measurement_data.get("elevation_changes", 0)),
            fittings=dict(measurement_data.get("fittings", {}) or {}),
            notes=list(measurement_data.get("notes", []) or []),
        )

    def extract_specifications(self, pdf_path: str) -> List[Dict[str, Any]]:
        extractor = SpecificationExtractor()
        specs = extractor.extract_from_pdf(pdf_path)
        return [asdict(spec) for spec in specs]

    def extract_measurements(self, manual_measurements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        extractor = DrawingMeasurementExtractor()
        measurements = extractor.manual_entry_measurements(manual_measurements)
        return [asdict(item) for item in measurements]

    def estimate(self, request: EstimateRequest) -> EstimateResponse:
        spec_objs = [self._to_spec(spec) for spec in request.specifications]
        measurement_objs = [self._to_measurement(item) for item in request.measurements]

        engine = PricingEngine(price_book_path=request.pricebook_path, markup=request.markup)
        materials = engine.calculate_materials(measurement_objs, spec_objs)
        labor_hours, _ = engine.calculate_labor(materials)
        labor_cost = labor_hours * request.labor_rate

        quote = QuoteGenerator().generate_quote(
            project_name=request.project_name,
            measurements=measurement_objs,
            materials=materials,
            labor_hours=labor_hours,
            labor_cost=labor_cost,
            specs=spec_objs,
        )

        quote.labor_rate = request.labor_rate
        quote.contingency_percent = request.contingency_percent
        contingency_value = quote.subtotal * (request.contingency_percent / 100.0)
        quote.total = quote.subtotal + contingency_value
        material_total = sum(material.total_price for material in quote.materials)

        return EstimateResponse(
            project_name=quote.project_name,
            quote_number=quote.quote_number,
            material_total=round(material_total, 2),
            labor_hours=round(quote.labor_hours, 2),
            labor_rate=quote.labor_rate,
            labor_cost=round(labor_cost, 2),
            subtotal=round(quote.subtotal, 2),
            contingency_percent=quote.contingency_percent,
            total=round(quote.total, 2),
            materials=[asdict(material) for material in quote.materials],
            material_list=quote.material_list,
            notes=quote.notes,
        )
