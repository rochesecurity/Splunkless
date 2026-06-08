"""Ingest pipeline — processes exported Splunk buckets into the Splunkless catalog."""

import logging
import sqlite3
import time
from pathlib import Path

from .bucket_parser import parse_bucket
from .models import BucketMetadata, init_db
from .tsidx_reader import extract_terms_from_tsidx, find_tsidx_files

logger = logging.getLogger(__name__)


def ingest_bucket(bucket_path: Path, conn: sqlite3.Connection, selective_terms: bool = True) -> bool:
    """Ingest a single Splunk bucket into the catalog.

    Steps:
      1. Parse bucket directory name for time range
      2. Read .data files for host/source/sourcetype metadata
      3. Extract terms from tsidx
      4. Store everything in the catalog database

    Args:
        bucket_path: Path to the bucket directory
        conn: SQLite connection to the catalog database
        selective_terms: Only index high-value terms (IPs, CVEs, hostnames)

    Returns:
        True if bucket was successfully ingested
    """
    start = time.time()

    bucket = parse_bucket(bucket_path)
    if not bucket:
        logger.warning(f"Could not parse bucket: {bucket_path}")
        return False

    # Check if already ingested
    existing = conn.execute(
        "SELECT 1 FROM buckets WHERE bucket_id = ?", (bucket.bucket_id,)
    ).fetchone()
    if existing:
        logger.info(f"Bucket {bucket.bucket_id} already ingested, skipping")
        return True

    # Find journal path
    journal_path = bucket_path / "rawdata" / "journal.zst"
    journal_str = str(journal_path) if journal_path.exists() else ""

    # Insert bucket metadata
    conn.execute(
        """INSERT INTO buckets (bucket_id, path, earliest_time, latest_time,
           index_time_earliest, index_time_latest, event_count, raw_size, journal_path)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            bucket.bucket_id,
            str(bucket_path),
            bucket.earliest_time,
            bucket.latest_time,
            bucket.index_time_earliest,
            bucket.index_time_latest,
            bucket.event_count,
            bucket.raw_size,
            journal_str,
        ),
    )

    # Insert host/source/sourcetype metadata
    for host in bucket.hosts:
        count = bucket.host_counts.get(host, 0)
        conn.execute(
            "INSERT OR IGNORE INTO bucket_hosts (bucket_id, host, event_count) VALUES (?, ?, ?)",
            (bucket.bucket_id, host, count),
        )

    for source in bucket.sources:
        count = bucket.source_counts.get(source, 0)
        conn.execute(
            "INSERT OR IGNORE INTO bucket_sources (bucket_id, source, event_count) VALUES (?, ?, ?)",
            (bucket.bucket_id, source, count),
        )

    for sourcetype in bucket.sourcetypes:
        count = bucket.sourcetype_counts.get(sourcetype, 0)
        conn.execute(
            "INSERT OR IGNORE INTO bucket_sourcetypes (bucket_id, sourcetype, event_count) VALUES (?, ?, ?)",
            (bucket.bucket_id, sourcetype, count),
        )

    # Extract and store terms from tsidx
    terms_count = 0
    for tsidx_path in find_tsidx_files(bucket_path):
        result = extract_terms_from_tsidx(tsidx_path, selective=selective_terms)
        for term in result.all_terms:
            conn.execute(
                "INSERT OR IGNORE INTO terms (term, bucket_id) VALUES (?, ?)",
                (term, bucket.bucket_id),
            )
            terms_count += 1

    conn.commit()

    elapsed = time.time() - start
    logger.info(
        f"Ingested bucket {bucket.bucket_id}: "
        f"{bucket.event_count} events, "
        f"{len(bucket.hosts)} hosts, "
        f"{terms_count} terms, "
        f"{elapsed:.2f}s"
    )
    return True


def ingest_directory(data_dir: Path, db_path: Path, selective_terms: bool = True) -> dict:
    """Ingest all buckets found under a data directory.

    Args:
        data_dir: Directory containing bucket subdirectories
        db_path: Path to create/open the SQLite catalog database
        selective_terms: Only index high-value terms

    Returns:
        Summary dict with counts
    """
    conn = init_db(db_path)
    summary = {"total": 0, "ingested": 0, "skipped": 0, "errors": 0}

    bucket_dirs = [
        d for d in data_dir.iterdir()
        if d.is_dir() and d.name.startswith("db_")
    ]

    for bucket_dir in sorted(bucket_dirs):
        summary["total"] += 1
        try:
            if ingest_bucket(bucket_dir, conn, selective_terms):
                summary["ingested"] += 1
            else:
                summary["skipped"] += 1
        except Exception as e:
            logger.error(f"Error ingesting {bucket_dir}: {e}")
            summary["errors"] += 1

    conn.close()
    return summary
