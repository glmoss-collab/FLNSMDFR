"""
HVAC insulation skill for agent orchestration.

Encapsulates project analysis, scope filtering, pricing, and bid package generation.
"""

import logging
from typing import Any, Dict, List, Optional

from hvac_insulation_estimator import ProjectQuote, MaterialItem, MeasurementItem
from guaranteed_insulation_scope import filter_specs_to_scope, filter_measurements_to_scope, get_scope_exclusion_summary
from guaranteed_insulation_bid_package import generate_bid_package_text
from claude_agent_tools import extract_project_info, extract_specifications, extract_measurements
from cloud_config import get_config
from data.bigquery_sink import BigQuerySink

logger = logging.getLogger(__name__)


class HVACInsulationSkill:
    def __init__(self, use_vertex_ai: bool = False):
        self.config = get_config()
        self.use_vertex_ai = use_vertex_ai
        self.bigquery = None
        if self.config.is_gcp():
            try:
                self.bigquery = BigQuerySink(project_id=self.config.gcp_project)
            except Exception as exc:
                logger.warning("BigQuery sink unavailable: %s", exc)

    def analyze_project(self, pdf_path: str) -> Dict[str, Any]:
        project_response = extract_project_info(pdf_path, use_vertex_ai=self.use_vertex_ai)
        spec_response = extract_specifications(pdf_path, use_vertex_ai=self.use_vertex_ai)
        measurement_response = extract_measurements(pdf_path, use_vertex_ai=self.use_vertex_ai)

        output = {
            "project_info": project_response.data,
            "specifications": spec_response.data,
            "measurements": measurement_response.data,
            "errors": [r for r in [project_response.error, spec_response.error, measurement_response.error] if r],
            "warnings": [w for w in [project_response.warnings, spec_response.warnings, measurement_response.warnings] if w],
        }

        if self.bigquery and spec_response.success:
            try:
                self.bigquery.insert_specifications(f"{self.config.gcp_project}.estimator.specifications", spec_response.data or [])
            except Exception as exc:
                logger.warning("Failed to write specifications to BigQuery: %s", exc)

        return output

    def generate_bid_package(self, quote: ProjectQuote, specs: List[Any], measurements: List[Any]) -> str:
        specs_before = len(specs)
        measurements_before = len(measurements)
        filtered_specs = filter_specs_to_scope([spec for spec in specs])
        filtered_measurements = filter_measurements_to_scope([m for m in measurements])
        exclusion_summary = get_scope_exclusion_summary(specs_before, len(filtered_specs), measurements_before, len(filtered_measurements))
        text = generate_bid_package_text(quote, exclusion_summary)
        return text

    def save_training_record(self, quote_id: str, quote_data: Dict[str, Any], outcome: Optional[Dict[str, Any]] = None) -> None:
        if not self.bigquery:
            return
        try:
            self.bigquery.insert_quotes(f"{self.config.gcp_project}.estimator.quotes", [quote_data])
            if outcome:
                self.bigquery.insert_outcome(f"{self.config.gcp_project}.estimator.outcomes", outcome)
        except Exception as exc:
            logger.warning("Failed to save training record to BigQuery: %s", exc)
