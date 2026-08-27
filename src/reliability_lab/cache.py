from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from redis.exceptions import RedisError

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory response cache with similarity and privacy guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up the closest unexpired response allowed by the guardrails."""
        if _is_uncacheable(query):
            return None, 0.0

        now = time.time()
        self._entries = [
            entry for entry in self._entries if now - entry.created_at <= self.ttl_seconds
        ]

        best_entry: CacheEntry | None = None
        best_score = 0.0
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_entry = entry
                best_score = score

        if best_entry is None or best_score < self.similarity_threshold:
            return None, best_score

        if _looks_like_false_hit(query, best_entry.key):
            self.false_hit_log.append(
                {
                    "query": query,
                    "cached_key": best_entry.key,
                    "score": best_score,
                    "reason": "date_or_number_mismatch",
                }
            )
            return None, best_score

        return best_entry.value, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response unless the query is privacy-sensitive."""
        if _is_uncacheable(query):
            return
        self._entries.append(
            CacheEntry(
                key=query,
                value=value,
                created_at=time.time(),
                metadata=dict(metadata or {}),
            )
        )

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Compute cosine similarity over normalized words and character trigrams."""
        if a == b:
            return 1.0

        def tokens(text: str) -> list[str]:
            words = re.findall(r"\w+", text.lower())
            trigrams = [word[index : index + 3] for word in words for index in range(len(word) - 2)]
            return words + trigrams

        vector_a = Counter(tokens(a))
        vector_b = Counter(tokens(b))
        if not vector_a or not vector_b:
            return 0.0

        dot_product = sum(count * vector_b[token] for token, count in vector_a.items())
        magnitude_a = math.sqrt(sum(count * count for count in vector_a.values()))
        magnitude_b = math.sqrt(sum(count * count for count in vector_b.values()))
        return dot_product / (magnitude_a * magnitude_b)


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    Cache state is shared across gateway instances through Redis hashes and TTLs.

    Data model (suggested):
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    For similarity lookup: SCAN all keys with self.prefix, HGET each entry's
    "query" field, compute similarity locally via ResponseCache.similarity().

    Provided helpers:
        _is_uncacheable(query)          — True if privacy-sensitive
        _looks_like_false_hit(q, key)   — True if 4-digit numbers differ
        self._query_hash(query)         — deterministic short hash for Redis key
        ResponseCache.similarity(a, b)  — reuse your improved similarity function
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except RedisError:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up an exact or similarity match from Redis."""
        if _is_uncacheable(query):
            return None, 0.0

        exact_key = f"{self.prefix}{self._query_hash(query)}"
        exact_response = self._redis.hget(exact_key, "response")
        if exact_response is not None:
            return str(exact_response), 1.0

        best_query: str | None = None
        best_response: str | None = None
        best_score = 0.0
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            cached = self._redis.hmget(key, "query", "response")
            cached_query, cached_response = cached
            if cached_query is None or cached_response is None:
                continue
            score = ResponseCache.similarity(query, str(cached_query))
            if score > best_score:
                best_query = str(cached_query)
                best_response = str(cached_response)
                best_score = score

        if best_query is None or best_response is None or best_score < self.similarity_threshold:
            return None, best_score

        if _looks_like_false_hit(query, best_query):
            self.false_hit_log.append(
                {
                    "query": query,
                    "cached_key": best_query,
                    "score": best_score,
                    "reason": "date_or_number_mismatch",
                }
            )
            return None, best_score

        return best_response, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with a server-enforced TTL."""
        if _is_uncacheable(query):
            return
        key = f"{self.prefix}{self._query_hash(query)}"
        mapping = {"query": query, "response": value}
        if metadata:
            mapping.update({f"meta:{name}": item for name, item in metadata.items()})
        pipeline = self._redis.pipeline(transaction=True)
        pipeline.hset(key, mapping=mapping)
        pipeline.expire(key, self.ttl_seconds)
        pipeline.execute()

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
