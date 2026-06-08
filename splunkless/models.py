"""Data models for Splunkless catalog and term index."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BucketMetadata:
    bucket_id: str
    path: str
    earliest_time: int
    latest_time: int
    index_time_earliest: int
    index_time_latest: int
    event_count: int
    hosts: list[str]
    sources: list[str]
    sourcetypes: list[str]
    raw_size: int
    journal_path: str


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS buckets (
    bucket_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    earliest_time INTEGER NOT NULL,
    latest_time INTEGER NOT NULL,
    index_time_earliest INTEGER,
    index_time_latest INTEGER,
    event_count INTEGER NOT NULL,
    raw_size INTEGER,
    journal_path TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_buckets_time
    ON buckets(earliest_time, latest_time);

CREATE TABLE IF NOT EXISTS bucket_hosts (
    bucket_id TEXT NOT NULL,
    host TEXT NOT NULL,
    event_count INTEGER,
    PRIMARY KEY (bucket_id, host),
    FOREIGN KEY (bucket_id) REFERENCES buckets(bucket_id)
);

CREATE INDEX IF NOT EXISTS idx_bucket_hosts_host ON bucket_hosts(host);

CREATE TABLE IF NOT EXISTS bucket_sources (
    bucket_id TEXT NOT NULL,
    source TEXT NOT NULL,
    event_count INTEGER,
    PRIMARY KEY (bucket_id, source),
    FOREIGN KEY (bucket_id) REFERENCES buckets(bucket_id)
);

CREATE INDEX IF NOT EXISTS idx_bucket_sources_source ON bucket_sources(source);

CREATE TABLE IF NOT EXISTS bucket_sourcetypes (
    bucket_id TEXT NOT NULL,
    sourcetype TEXT NOT NULL,
    event_count INTEGER,
    PRIMARY KEY (bucket_id, sourcetype),
    FOREIGN KEY (bucket_id) REFERENCES buckets(bucket_id)
);

CREATE INDEX IF NOT EXISTS idx_bucket_sourcetypes_st ON bucket_sourcetypes(sourcetype);

CREATE TABLE IF NOT EXISTS terms (
    term TEXT NOT NULL,
    bucket_id TEXT NOT NULL,
    PRIMARY KEY (term, bucket_id),
    FOREIGN KEY (bucket_id) REFERENCES buckets(bucket_id)
);

CREATE INDEX IF NOT EXISTS idx_terms_term ON terms(term);
CREATE INDEX IF NOT EXISTS idx_terms_bucket ON terms(bucket_id);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
