"""Parse Splunk bucket metadata from filesystem structure."""

import csv
import re
import struct
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path


@dataclass
class ParsedBucket:
    bucket_id: str
    path: Path
    earliest_time: int
    latest_time: int
    sequence_id: int
    guid: str
    index_time_earliest: int = 0
    index_time_latest: int = 0
    event_count: int = 0
    raw_size: int = 0
    hosts: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    sourcetypes: list[str] = field(default_factory=list)
    host_counts: dict[str, int] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    sourcetype_counts: dict[str, int] = field(default_factory=dict)


# Bucket directory naming: db_{latest_epoch}_{earliest_epoch}_{seq}_{GUID}
BUCKET_NAME_PATTERN = re.compile(
    r"^db_(\d+)_(\d+)_(\d+)_([A-F0-9-]+)$", re.IGNORECASE
)


def parse_bucket_name(name: str) -> tuple[int, int, int, str] | None:
    """Extract timestamps and IDs from bucket directory name."""
    m = BUCKET_NAME_PATTERN.match(name)
    if not m:
        return None
    latest = int(m.group(1))
    earliest = int(m.group(2))
    seq = int(m.group(3))
    guid = m.group(4)
    return latest, earliest, seq, guid


def parse_data_file(path: Path) -> list[tuple[str, int]]:
    """Parse a .data file (Hosts.data, Sources.data, SourceTypes.data).

    Returns list of (value, event_count) tuples, stripping the prefix
    (e.g., 'host::' from 'host::myhost.com').
    """
    entries = []
    try:
        content = path.read_bytes()
        for line in content.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split(b"\t")
            if len(parts) < 3:
                continue
            idx = parts[0]
            if idx == b"0":
                continue
            value = parts[1].decode("utf-8", errors="replace")
            event_count = int(parts[2].strip()) if parts[2].strip() else 0
            # Strip prefix like "host::", "source::", "sourcetype::"
            if "::" in value:
                value = value.split("::", 1)[1]
            entries.append((value, event_count))
    except (OSError, ValueError):
        pass
    return entries


def parse_bucket_info_csv(path: Path) -> tuple[int, int]:
    """Parse bucket_info.csv for index times. Returns (earliest, latest)."""
    try:
        content = path.read_text()
        reader = csv.DictReader(StringIO(content))
        for row in reader:
            et = int(row.get("indextime_et", 0))
            lt = int(row.get("indextime_lt", 0))
            return et, lt
    except (OSError, ValueError, KeyError):
        pass
    return 0, 0


def parse_raw_size(path: Path) -> int:
    """Read .rawSize file."""
    try:
        content = path.read_text().strip()
        return int(content)
    except (OSError, ValueError):
        return 0


def parse_bucket(bucket_path: Path) -> ParsedBucket | None:
    """Parse all metadata from a Splunk bucket directory."""
    name = bucket_path.name
    parsed = parse_bucket_name(name)
    if not parsed:
        return None

    latest, earliest, seq, guid = parsed

    bucket = ParsedBucket(
        bucket_id=guid,
        path=bucket_path,
        earliest_time=earliest,
        latest_time=latest,
        sequence_id=seq,
        guid=guid,
    )

    # Parse .data files
    hosts_file = bucket_path / "Hosts.data"
    if hosts_file.exists():
        entries = parse_data_file(hosts_file)
        bucket.hosts = [e[0] for e in entries]
        bucket.host_counts = {e[0]: e[1] for e in entries}

    sources_file = bucket_path / "Sources.data"
    if sources_file.exists():
        entries = parse_data_file(sources_file)
        bucket.sources = [e[0] for e in entries]
        bucket.source_counts = {e[0]: e[1] for e in entries}

    sourcetypes_file = bucket_path / "SourceTypes.data"
    if sourcetypes_file.exists():
        entries = parse_data_file(sourcetypes_file)
        bucket.sourcetypes = [e[0] for e in entries]
        bucket.sourcetype_counts = {e[0]: e[1] for e in entries}

    # Parse bucket_info.csv
    info_file = bucket_path / "bucket_info.csv"
    if info_file.exists():
        bucket.index_time_earliest, bucket.index_time_latest = parse_bucket_info_csv(
            info_file
        )

    # Parse .rawSize
    raw_size_file = bucket_path / ".rawSize"
    if raw_size_file.exists():
        bucket.raw_size = parse_raw_size(raw_size_file)

    # Get event count from first host entry (line 0 in .data has total)
    if hosts_file.exists():
        try:
            content = hosts_file.read_bytes()
            first_line = content.split(b"\n")[0]
            parts = first_line.split(b"\t")
            if len(parts) >= 3 and parts[0] == b"0":
                bucket.event_count = int(parts[2].strip())
        except (OSError, ValueError):
            pass

    return bucket
