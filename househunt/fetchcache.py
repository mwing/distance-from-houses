import dataclasses
import datetime as dt
import hashlib
import json

from .cache import Cache
from .models import Filters, Listing

NS_PREFIX = "fetch:"


class FetchCache:
    """Shared across users on purpose: listings are public and the key covers every fetch input."""

    def __init__(self, cache: Cache, ttl_hours: float):
        if not 0 < ttl_hours <= 24:
            raise ValueError("ttl_hours must be between 0 and 24: entries are bucketed by day")
        self.cache = cache
        self.ttl = dt.timedelta(hours=ttl_hours)

    @staticmethod
    def _key(source: str, filters: Filters, max_listings: int) -> str:
        f = dataclasses.asdict(filters)
        normalized = {k: sorted(v) if isinstance(v, list) else v for k, v in f.items()}
        blob = json.dumps({"source": source, "filters": normalized, "max": max_listings}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    @staticmethod
    def _ns(day: dt.date) -> str:
        return f"{NS_PREFIX}{day.isoformat()}"

    def get(self, source: str, filters: Filters, max_listings: int) -> list[Listing] | None:
        now = dt.datetime.now(dt.timezone.utc)
        key = self._key(source, filters, max_listings)
        for day in {now.date(), (now - self.ttl).date()}:
            hit = self.cache.get(self._ns(day), key)
            if hit and now - dt.datetime.fromisoformat(hit["fetched_at"]) < self.ttl:
                try:
                    return [Listing(**d) for d in hit["listings"]]
                except (TypeError, KeyError):
                    return None
        return None

    def put(self, source: str, filters: Filters, max_listings: int, listings: list[Listing]) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        value = {"fetched_at": now.isoformat(), "listings": [dataclasses.asdict(l) for l in listings]}
        self.cache.set(self._ns(now.date()), self._key(source, filters, max_listings), value)
        keep = {self._ns(now.date()), self._ns((now - self.ttl).date())}
        self.cache.delete_namespaces(NS_PREFIX, keep)
