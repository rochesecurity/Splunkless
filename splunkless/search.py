"""Search engine — query the Splunkless catalog and retrieve events."""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .journal_reader import extract_events_filtered
from .models import init_db


@dataclass
class SearchQuery:
    host: str | None = None
    source: str | None = None
    sourcetype: str | None = None
    terms: list[str] = field(default_factory=list)
    earliest: int = 0
    latest: int = 0
    field_filters: dict[str, str] = field(default_factory=dict)
    max_events: int = 100


@dataclass
class BucketMatch:
    bucket_id: str
    path: str
    earliest_time: int
    latest_time: int
    event_count: int
    journal_path: str
    match_reason: str


@dataclass
class SearchResult:
    query: SearchQuery
    candidate_buckets: list[BucketMatch]
    events: list[dict]
    search_time_ms: float
    buckets_scanned: int
    total_events_found: int


class SearchEngine:
    def __init__(self, db_path: Path):
        self.conn = init_db(db_path)
        self.conn.row_factory = sqlite3.Row

    def find_buckets(self, query: SearchQuery) -> list[BucketMatch]:
        """Find buckets matching the query using metadata + term index."""
        joins = []
        conditions = []
        params = []

        if query.earliest:
            conditions.append("b.latest_time >= ?")
            params.append(query.earliest)
        if query.latest:
            conditions.append("b.earliest_time <= ?")
            params.append(query.latest)
        if query.host:
            joins.append("JOIN bucket_hosts bh ON b.bucket_id = bh.bucket_id")
            conditions.append("bh.host = ?")
            params.append(query.host)
        if query.sourcetype:
            joins.append("JOIN bucket_sourcetypes bs ON b.bucket_id = bs.bucket_id")
            conditions.append("bs.sourcetype = ?")
            params.append(query.sourcetype)
        if query.source:
            joins.append("JOIN bucket_sources bsrc ON b.bucket_id = bsrc.bucket_id")
            conditions.append("bsrc.source = ?")
            params.append(query.source)

        join_clause = " ".join(joins)
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT DISTINCT b.* FROM buckets b {join_clause} WHERE {where_clause}"

        rows = self.conn.execute(sql, params).fetchall()
        bucket_ids = {row["bucket_id"] for row in rows}

        # Intersect with term filter
        if query.terms:
            for term in query.terms:
                term_buckets = {
                    row[0]
                    for row in self.conn.execute(
                        "SELECT bucket_id FROM terms WHERE term = ? COLLATE NOCASE",
                        (term,),
                    ).fetchall()
                }
                bucket_ids &= term_buckets

            # Re-fetch full bucket data for remaining IDs
            if bucket_ids:
                placeholders = ",".join("?" * len(bucket_ids))
                rows = self.conn.execute(
                    f"SELECT * FROM buckets WHERE bucket_id IN ({placeholders})",
                    list(bucket_ids),
                ).fetchall()
            else:
                rows = []

        matches = []
        for row in rows:
            reasons = []
            if query.host:
                reasons.append(f"host={query.host}")
            if query.sourcetype:
                reasons.append(f"sourcetype={query.sourcetype}")
            if query.terms:
                reasons.append(f"terms={','.join(query.terms)}")
            if query.earliest or query.latest:
                reasons.append("time_range")

            matches.append(
                BucketMatch(
                    bucket_id=row["bucket_id"],
                    path=row["path"],
                    earliest_time=row["earliest_time"],
                    latest_time=row["latest_time"],
                    event_count=row["event_count"],
                    journal_path=row["journal_path"],
                    match_reason=" AND ".join(reasons) if reasons else "unfiltered",
                )
            )

        return matches

    def search(self, query: SearchQuery, fetch_events: bool = False) -> SearchResult:
        """Execute a search query.

        Phase 1: Find candidate buckets via metadata + term index
        Phase 2 (optional): Decompress journals and extract matching events
        """
        import time

        start = time.time()

        buckets = self.find_buckets(query)

        events = []
        buckets_scanned = 0

        if fetch_events:
            text_filter = query.terms[0] if query.terms else None
            remaining = query.max_events

            for bucket in buckets:
                if remaining <= 0:
                    break
                journal = Path(bucket.journal_path)
                if not journal.exists():
                    continue

                buckets_scanned += 1
                bucket_events = extract_events_filtered(
                    journal,
                    field_filters=query.field_filters or None,
                    text_filter=text_filter,
                    max_events=remaining,
                )
                events.extend(bucket_events)
                remaining -= len(bucket_events)

        elapsed = (time.time() - start) * 1000

        return SearchResult(
            query=query,
            candidate_buckets=buckets,
            events=events,
            search_time_ms=elapsed,
            buckets_scanned=buckets_scanned,
            total_events_found=len(events),
        )

    def list_hosts(self) -> list[tuple[str, int]]:
        """List all indexed hosts with total event counts."""
        rows = self.conn.execute(
            "SELECT host, SUM(event_count) as total FROM bucket_hosts GROUP BY host ORDER BY total DESC"
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def list_sourcetypes(self) -> list[tuple[str, int]]:
        """List all indexed sourcetypes with total event counts."""
        rows = self.conn.execute(
            "SELECT sourcetype, SUM(event_count) as total FROM bucket_sourcetypes GROUP BY sourcetype ORDER BY total DESC"
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def list_sources(self) -> list[tuple[str, int]]:
        """List all indexed sources."""
        rows = self.conn.execute(
            "SELECT source, SUM(event_count) as total FROM bucket_sources GROUP BY source ORDER BY total DESC"
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def search_terms(self, prefix: str, limit: int = 20) -> list[tuple[str, int]]:
        """Search terms by prefix, returns (term, bucket_count)."""
        rows = self.conn.execute(
            "SELECT term, COUNT(bucket_id) as cnt FROM terms WHERE term LIKE ? COLLATE NOCASE GROUP BY term ORDER BY cnt DESC LIMIT ?",
            (prefix + "%", limit),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def stats(self) -> dict:
        """Return catalog statistics."""
        buckets = self.conn.execute("SELECT COUNT(*) FROM buckets").fetchone()[0]
        hosts = self.conn.execute("SELECT COUNT(DISTINCT host) FROM bucket_hosts").fetchone()[0]
        sourcetypes = self.conn.execute("SELECT COUNT(DISTINCT sourcetype) FROM bucket_sourcetypes").fetchone()[0]
        terms = self.conn.execute("SELECT COUNT(DISTINCT term) FROM terms").fetchone()[0]
        events = self.conn.execute("SELECT SUM(event_count) FROM buckets").fetchone()[0] or 0

        time_range = self.conn.execute(
            "SELECT MIN(earliest_time), MAX(latest_time) FROM buckets"
        ).fetchone()

        return {
            "buckets": buckets,
            "unique_hosts": hosts,
            "unique_sourcetypes": sourcetypes,
            "indexed_terms": terms,
            "total_events": events,
            "earliest_time": time_range[0] if time_range[0] else 0,
            "latest_time": time_range[1] if time_range[1] else 0,
        }

    def close(self):
        self.conn.close()
