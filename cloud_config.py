"""
Cloud Configuration Module
=========================

Centralized configuration for local and GCP deployments.
Supports environment detection, storage/cache selection, and secret access.
"""

import os
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Any, Dict

logger = logging.getLogger(__name__)


class Environment(Enum):
    LOCAL = "local"
    GCP = "gcp"


class CacheBackend(Enum):
    MEMORY = "memory"
    FILE = "file"
    FIRESTORE = "firestore"


class StorageBackend(Enum):
    LOCAL = "local"
    GCS = "gcs"


@dataclass
class CloudConfig:
    gcp_project: Optional[str] = field(default=None)
    environment: Environment = field(default=Environment.LOCAL)
    cache_backend: CacheBackend = field(default=CacheBackend.MEMORY)
    storage_backend: StorageBackend = field(default=StorageBackend.LOCAL)
    gcs_bucket: Optional[str] = field(default=None)
    gcs_prefix: str = field(default="uploads")
    log_level: str = field(default="INFO")

    def __post_init__(self):
        self._detect_environment()
        self._configure_backends()
        self._setup_logging()

    def _detect_environment(self):
        self.gcp_project = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
        if self.gcp_project:
            self.environment = Environment.GCP
            logger.info(f"Detected GCP environment for project={self.gcp_project}")
        else:
            self.environment = Environment.LOCAL
            logger.info("Detected local development environment")

    def _configure_backends(self):
        cache_backend = os.getenv("CACHE_BACKEND", "memory").lower()
        self.cache_backend = CacheBackend(cache_backend) if cache_backend in CacheBackend._value2member_map_ else CacheBackend.MEMORY

        storage_backend = os.getenv("STORAGE_BACKEND", "local").lower()
        self.storage_backend = StorageBackend(storage_backend) if storage_backend in StorageBackend._value2member_map_ else StorageBackend.LOCAL

        self.gcs_bucket = os.getenv("GCS_BUCKET", self.gcs_bucket)
        self.log_level = os.getenv("LOG_LEVEL", self.log_level).upper()
        logger.info(f"Cache backend={self.cache_backend.value}, storage backend={self.storage_backend.value}")

    def _setup_logging(self):
        logging.basicConfig(level=getattr(logging, self.log_level, logging.INFO),
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")

    def is_gcp(self) -> bool:
        return self.environment == Environment.GCP

    def get_anthropic_api_key(self) -> Optional[str]:
        key = os.getenv("ANTHROPIC_API_KEY")
        if key:
            return key
        if self.is_gcp():
            try:
                from secrets_manager import get_secret
                return get_secret("anthropic-api-key", project_id=self.gcp_project)
            except Exception as exc:
                logger.warning(f"Unable to load Anthropic API key from Secret Manager: {exc}")
        return None

    def get_gemini_api_key(self) -> Optional[str]:
        key = os.getenv("GEMINI_API_KEY")
        if key:
            return key
        if self.is_gcp():
            try:
                from secrets_manager import get_secret
                return get_secret("gemini-api-key", project_id=self.gcp_project)
            except Exception as exc:
                logger.warning(f"Unable to load Gemini API key from Secret Manager: {exc}")
        return None

    def get_cache(self):
        if self.cache_backend == CacheBackend.FILE:
            from firestore_cache import FileCache
            return FileCache()
        if self.cache_backend == CacheBackend.FIRESTORE:
            from firestore_cache import FirestoreCache
            return FirestoreCache(project_id=self.gcp_project)
        from firestore_cache import MemoryCache
        return MemoryCache()

    def get_storage(self):
        if self.storage_backend == StorageBackend.GCS:
            from gcs_storage import GCSStorage
            if not self.gcs_bucket:
                raise ValueError("GCS_BUCKET is required for GCS storage")
            return GCSStorage(bucket_name=self.gcs_bucket, prefix=self.gcs_prefix)
        from gcs_storage import LocalStorage
        return LocalStorage()


_config: Optional[CloudConfig] = None


def get_config() -> CloudConfig:
    global _config
    if _config is None:
        _config = CloudConfig()
    return _config


config = get_config()
