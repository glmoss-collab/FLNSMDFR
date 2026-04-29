"""
Cache backends for FLNSMDFR.
Supports in-memory, file-based, and Firestore cache implementations.
"""

import os
import json
import hashlib
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


class CacheBackend(ABC):
    @abstractmethod
    def get(self, key: str, category: str = "default") -> Optional[Any]:
        pass

    @abstractmethod
    def set(self, key: str, value: Any, category: str = "default", ttl: Optional[int] = None) -> None:
        pass

    @abstractmethod
    def invalidate(self, key: str, category: str = "default") -> None:
        pass

    @abstractmethod
    def clear(self, category: Optional[str] = None) -> int:
        pass

    @abstractmethod
    def stats(self) -> Dict[str, Any]:
        pass


class MemoryCache(CacheBackend):
    def __init__(self, default_ttl: int = 3600):
        self._store: Dict[str, Dict[str, Any]] = {}
        self.default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def _doc_key(self, key: str, category: str) -> str:
        return f"{category}:{key}"

    def _expired(self, entry: Dict[str, Any]) -> bool:
        return datetime.now() > entry["expires_at"]

    def get(self, key: str, category: str = "default") -> Optional[Any]:
        doc_key = self._doc_key(key, category)
        entry = self._store.get(doc_key)
        if not entry or self._expired(entry):
            self._misses += 1
            self._store.pop(doc_key, None)
            return None
        self._hits += 1
        return entry["value"]

    def set(self, key: str, value: Any, category: str = "default", ttl: Optional[int] = None) -> None:
        ttl = ttl or self.default_ttl
        expires_at = datetime.now() + timedelta(seconds=ttl)
        self._store[self._doc_key(key, category)] = {
            "value": value,
            "expires_at": expires_at
        }

    def invalidate(self, key: str, category: str = "default") -> None:
        self._store.pop(self._doc_key(key, category), None)

    def clear(self, category: Optional[str] = None) -> int:
        if category:
            keys = [k for k in self._store if k.startswith(f"{category}:")]
            for k in keys:
                self._store.pop(k, None)
            return len(keys)
        count = len(self._store)
        self._store.clear()
        return count

    def stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            "backend": "memory",
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 2) if total else 0.0
        }


class FileCache(MemoryCache):
    def __init__(self, base_dir: str = "./cache", default_ttl: int = 3600):
        super().__init__(default_ttl=default_ttl)
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _key_path(self, key: str, category: str) -> Path:
        safe_key = hashlib.sha256(f"{category}:{key}".encode()).hexdigest()
        return self.base_dir / f"{safe_key}.json"

    def get(self, key: str, category: str = "default") -> Optional[Any]:
        path = self._key_path(key, category)
        if not path.exists():
            self._misses += 1
            return None
        data = json.loads(path.read_text())
        expires_at = datetime.fromisoformat(data["expires_at"])
        if datetime.now() > expires_at:
            path.unlink(missing_ok=True)
            self._misses += 1
            return None
        self._hits += 1
        return data["value"]

    def set(self, key: str, value: Any, category: str = "default", ttl: Optional[int] = None) -> None:
        path = self._key_path(key, category)
        ttl = ttl or self.default_ttl
        path.write_text(json.dumps({
            "value": value,
            "expires_at": (datetime.now() + timedelta(seconds=ttl)).isoformat()
        }))

    def invalidate(self, key: str, category: str = "default") -> None:
        self._key_path(key, category).unlink(missing_ok=True)

    def clear(self, category: Optional[str] = None) -> int:
        deleted = 0
        for path in self.base_dir.glob("*.json"):
            if category is None or path.stem.startswith(hashlib.sha256(f"{category}:".encode()).hexdigest()[:8]):
                path.unlink(missing_ok=True)
                deleted += 1
        return deleted


class FirestoreCache(CacheBackend):
    def __init__(self, project_id: Optional[str] = None, collection_name: str = "cache", default_ttl: int = 86400):
        try:
            from google.cloud import firestore
        except ImportError as exc:
            raise ImportError("google-cloud-firestore is required for Firestore cache") from exc
        self.project_id = project_id or os.getenv("GCP_PROJECT")
        self.client = firestore.Client(project=self.project_id)
        self.collection = self.client.collection(collection_name)
        self.default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def _doc_id(self, key: str, category: str) -> str:
        raw = f"{category}:{key}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _expired(self, doc: Dict[str, Any]) -> bool:
        expires_at = doc.get("expires_at")
        if hasattr(expires_at, "timestamp"):
            expires_at = expires_at.datetime()
        return datetime.now() > expires_at

    def get(self, key: str, category: str = "default") -> Optional[Any]:
        doc = self.collection.document(self._doc_id(key, category)).get()
        if not doc.exists:
            self._misses += 1
            return None
        data = doc.to_dict()
        if self._expired(data):
            self.collection.document(self._doc_id(key, category)).delete()
            self._misses += 1
            return None
        self._hits += 1
        return data.get("value")

    def set(self, key: str, value: Any, category: str = "default", ttl: Optional[int] = None) -> None:
        ttl = ttl or self.default_ttl
        self.collection.document(self._doc_id(key, category)).set({
            "key": key,
            "category": category,
            "value": value,
            "expires_at": datetime.now() + timedelta(seconds=ttl)
        })

    def invalidate(self, key: str, category: str = "default") -> None:
        self.collection.document(self._doc_id(key, category)).delete()

    def clear(self, category: Optional[str] = None) -> int:
        query = self.collection
        if category:
            query = query.where("category", "==", category)
        deleted = 0
        for doc in query.stream():
            doc.reference.delete()
            deleted += 1
        return deleted

    def stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            "backend": "firestore",
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 2) if total else 0.0
        }
