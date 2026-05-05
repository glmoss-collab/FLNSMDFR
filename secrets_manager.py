"""
Secret Manager helpers for FLNSMDFR.
Supports local env fallback and Google Secret Manager retrieval.
"""

import os
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_client: Any = None
_client_unavailable: bool = False


def _get_client() -> Any:
    """Lazily import and cache the Secret Manager client.

    Returns None when the google-cloud-secret-manager SDK is not installed
    (e.g. local dev without GCP extras). Callers must handle None.
    """
    global _client, _client_unavailable
    if _client is not None or _client_unavailable:
        return _client
    try:
        from google.cloud import secretmanager
    except ImportError:
        logger.info(
            "google-cloud-secret-manager not installed; Secret Manager lookups disabled"
        )
        _client_unavailable = True
        return None
    _client = secretmanager.SecretManagerServiceClient()
    return _client


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

    client = _get_client()
    if client is None:
        return None

    try:
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as exc:
        logger.warning("Secret Manager lookup failed for %s: %s", secret_name, exc)
        return None
