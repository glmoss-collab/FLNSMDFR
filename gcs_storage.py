"""
Google Cloud Storage Integration Module
========================================

Provides local and GCS storage backends for file uploads, downloads, and signed URLs.
"""

import os
import io
import uuid
import hashlib
from abc import ABC, abstractmethod
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, BinaryIO, Union, List, Dict, Any


class StorageBackend(ABC):
    @abstractmethod
    def upload_file(self, file_data: Union[bytes, BinaryIO], destination_path: str, content_type: Optional[str] = None, metadata: Optional[Dict[str, str]] = None) -> str:
        pass

    @abstractmethod
    def download_file(self, source_path: str) -> bytes:
        pass

    @abstractmethod
    def get_download_url(self, source_path: str, expiration_minutes: int = 60) -> str:
        pass

    @abstractmethod
    def delete_file(self, source_path: str) -> bool:
        pass

    @abstractmethod
    def list_files(self, prefix: str = "") -> List[str]:
        pass

    @abstractmethod
    def file_exists(self, source_path: str) -> bool:
        pass


class LocalStorage(StorageBackend):
    def __init__(self, base_dir: str = "./storage"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _full_path(self, path: str) -> Path:
        return self.base_dir / path

    def upload_file(self, file_data: Union[bytes, BinaryIO], destination_path: str, content_type: Optional[str] = None, metadata: Optional[Dict[str, str]] = None) -> str:
        full_path = self._full_path(destination_path)
        full_path.parent.mkdir(parents=True, exist_ok=True)
        data = file_data if isinstance(file_data, bytes) else file_data.read()
        with open(full_path, "wb") as f:
            f.write(data)
        return str(full_path)

    def download_file(self, source_path: str) -> bytes:
        full_path = self._full_path(source_path)
        if not full_path.exists():
            raise FileNotFoundError(source_path)
        return full_path.read_bytes()

    def get_download_url(self, source_path: str, expiration_minutes: int = 60) -> str:
        return str(self._full_path(source_path))

    def delete_file(self, source_path: str) -> bool:
        full_path = self._full_path(source_path)
        if full_path.exists():
            full_path.unlink()
            return True
        return False

    def list_files(self, prefix: str = "") -> List[str]:
        root = self._full_path(prefix) if prefix else self.base_dir
        if not root.exists():
            return []
        return [str(p.relative_to(self.base_dir)) for p in root.rglob("*") if p.is_file()]

    def file_exists(self, source_path: str) -> bool:
        return self._full_path(source_path).exists()


class GCSStorage(LocalStorage):
    def __init__(self, bucket_name: str, prefix: str = "", project_id: Optional[str] = None):
        try:
            from google.cloud import storage
        except ImportError as exc:
            raise ImportError("google-cloud-storage is required for GCS storage") from exc
        self.bucket_name = bucket_name
        self.prefix = prefix.strip("/")
        self.project_id = project_id or os.getenv("GCP_PROJECT")
        self.client = storage.Client(project=self.project_id)
        self.bucket = self.client.bucket(bucket_name)

    def _full_path(self, path: str) -> str:
        if self.prefix:
            return f"{self.prefix}/{path.lstrip('/')}"
        return path.lstrip("/")

    def _detect_content_type(self, path: str) -> str:
        import mimetypes
        content_type, _ = mimetypes.guess_type(path)
        return content_type or "application/octet-stream"

    def upload_file(self, file_data: Union[bytes, BinaryIO], destination_path: str, content_type: Optional[str] = None, metadata: Optional[Dict[str, str]] = None) -> str:
        full_path = self._full_path(destination_path)
        blob = self.bucket.blob(full_path)
        blob.content_type = content_type or self._detect_content_type(destination_path)
        if metadata:
            blob.metadata = metadata
        if isinstance(file_data, bytes):
            blob.upload_from_string(file_data, content_type=blob.content_type)
        else:
            blob.upload_from_file(file_data, content_type=blob.content_type)
        return f"gs://{self.bucket_name}/{full_path}"

    def download_file(self, source_path: str) -> bytes:
        blob = self.bucket.blob(self._full_path(source_path))
        if not blob.exists():
            raise FileNotFoundError(source_path)
        return blob.download_as_bytes()

    def get_download_url(self, source_path: str, expiration_minutes: int = 60) -> str:
        blob = self.bucket.blob(self._full_path(source_path))
        if not blob.exists():
            raise FileNotFoundError(source_path)
        return blob.generate_signed_url(expiration=timedelta(minutes=expiration_minutes), method="GET")

    def delete_file(self, source_path: str) -> bool:
        blob = self.bucket.blob(self._full_path(source_path))
        if blob.exists():
            blob.delete()
            return True
        return False

    def list_files(self, prefix: str = "") -> List[str]:
        search_prefix = self._full_path(prefix) if prefix else self.prefix
        return [blob.name[len(self.prefix)+1:] if self.prefix and blob.name.startswith(self.prefix + "/") else blob.name for blob in self.client.list_blobs(self.bucket, prefix=search_prefix)]

    def file_exists(self, source_path: str) -> bool:
        return self.bucket.blob(self._full_path(source_path)).exists()


_storage: Optional[StorageBackend] = None


def get_storage() -> StorageBackend:
    global _storage
    if _storage is None:
        backend = os.getenv("STORAGE_BACKEND", "local").lower()
        if backend == "gcs":
            bucket_name = os.getenv("GCS_BUCKET")
            if not bucket_name:
                raise ValueError("GCS_BUCKET is required for GCS storage")
            _storage = GCSStorage(bucket_name=bucket_name)
        else:
            _storage = LocalStorage()
    return _storage
