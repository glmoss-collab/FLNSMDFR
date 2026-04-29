"""
BigQuery sink for insulation estimation training and analytics.

Streams extraction, quote, and outcome records into BigQuery tables.
"""

import os
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class BigQuerySink:
    def __init__(self, project_id: Optional[str] = None):
        try:
            from google.cloud import bigquery
        except ImportError as exc:
            raise ImportError("google-cloud-bigquery is required for BigQuery integration") from exc
        self.project_id = project_id or os.getenv("GCP_PROJECT")
        self.client = bigquery.Client(project=self.project_id)

    def _insert(self, table_id: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        errors = self.client.insert_rows_json(table_id, rows)
        if errors:
            logger.error("BigQuery insert errors for %s: %s", table_id, errors)
            return {"success": False, "errors": errors}
        return {"success": True, "inserted_rows": len(rows)}

    def insert_specifications(self, table_id: str, specs: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._insert(table_id, specs)

    def insert_measurements(self, table_id: str, measurements: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._insert(table_id, measurements)

    def insert_quotes(self, table_id: str, quotes: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._insert(table_id, quotes)

    def insert_outcome(self, table_id: str, outcome: Dict[str, Any]) -> Dict[str, Any]:
        return self._insert(table_id, [outcome])


def get_bigquery_sink() -> BigQuerySink:
    return BigQuerySink()
