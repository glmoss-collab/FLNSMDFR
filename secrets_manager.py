"""
Secret Manager helpers for FLNSMDFR.
Supports local env fallback and Google Secret Manager retrieval.
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_secret(secret_name: str, project_id: Optional[str] = None) -> Optional[str]:
    """Retrieve a secret value from GCP Secret Manager or environment."""
    env_key = secret_name.upper().replace("-", "_")
    value = os.getenv(env_key)
    if value:
        return value

    project_id = project_id or os.getenv("GCP_PROJECT")
    if not project_id:
        logger.warning("No GCP_PROJECT configured and secret is not in environment")
        return None

    try:
        from google.cloud import secretmanager
    except ImportError as exc:
        raise ImportError("google-cloud-secret-manager is required for Secret Manager access") from exc

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    payload = response.payload.data.decode("UTF-8")
    return payload
