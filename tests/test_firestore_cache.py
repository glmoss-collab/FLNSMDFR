"""
Tests for firestore_cache._expired logic.

Avoids the Firestore client entirely — _expired is a static method that
operates on a plain dict, so we can pass synthetic docs that mimic what
Firestore would return (DatetimeWithNanoseconds is a tz-aware datetime
subclass, so a vanilla tz-aware datetime is a faithful stand-in).
"""

from datetime import datetime, timedelta, timezone

import pytest

from firestore_cache import FirestoreCache, MemoryCache


class TestFirestoreCacheExpired:
    def test_aware_future_is_not_expired(self):
        doc = {"expires_at": datetime.now(tz=timezone.utc) + timedelta(seconds=60)}
        assert FirestoreCache._expired(doc) is False

    def test_aware_past_is_expired(self):
        doc = {"expires_at": datetime.now(tz=timezone.utc) - timedelta(seconds=60)}
        assert FirestoreCache._expired(doc) is True

    def test_naive_legacy_datetime_is_treated_as_utc(self):
        # Backward compat: pre-fix entries were written naive. Treat as UTC
        # rather than crashing on naive-vs-aware comparison.
        naive_future = (datetime.now(tz=timezone.utc) + timedelta(seconds=60)).replace(tzinfo=None)
        assert FirestoreCache._expired({"expires_at": naive_future}) is False

    def test_missing_expires_at_treated_as_expired(self):
        # Malformed doc evicts itself instead of raising.
        assert FirestoreCache._expired({}) is True

    def test_no_attribute_error_on_datetime_input(self):
        # Regression: pre-fix code called expires_at.datetime() which raises
        # AttributeError on any real datetime. This test would have caught it.
        doc = {"expires_at": datetime.now(tz=timezone.utc) + timedelta(seconds=60)}
        try:
            FirestoreCache._expired(doc)
        except AttributeError as e:
            pytest.fail(f"_expired raised AttributeError on a datetime: {e}")


class TestMemoryCacheTTL:
    def test_get_returns_value_before_expiry(self):
        cache = MemoryCache(default_ttl=60)
        cache.set("k", "v")
        assert cache.get("k") == "v"

    def test_get_returns_none_after_expiry(self):
        cache = MemoryCache(default_ttl=60)
        cache.set("k", "v", ttl=-1)  # already expired
        assert cache.get("k") is None

    def test_invalidate_removes_entry(self):
        cache = MemoryCache()
        cache.set("k", "v")
        cache.invalidate("k")
        assert cache.get("k") is None

    def test_clear_by_category(self):
        cache = MemoryCache()
        cache.set("k1", "v", category="a")
        cache.set("k2", "v", category="b")
        deleted = cache.clear(category="a")
        assert deleted == 1
        assert cache.get("k1", category="a") is None
        assert cache.get("k2", category="b") == "v"
