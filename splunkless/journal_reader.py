"""Read and decompress Splunk journal.zst files to extract events.

The journal format is:
  - Standard zstd compression
  - Binary header with bucket metadata (host, source, sourcetype, field names)
  - Repeated event records: small binary framing + JSON payload

Events in this sample are Tenable.io vulnerability findings, but the reader
is generic and works with any sourcetype that stores JSON events.
"""

import json
import re
from pathlib import Path
from typing import Generator

import zstandard


def decompress_journal(journal_path: Path) -> bytes:
    """Decompress a journal.zst file and return raw bytes."""
    dctx = zstandard.ZstdDecompressor()
    with open(journal_path, "rb") as f:
        return dctx.stream_reader(f).read()


def extract_events(journal_path: Path) -> Generator[dict, None, None]:
    """Extract JSON events from a journal.zst file.

    Yields parsed JSON dicts for each event found in the journal.
    Uses regex to find JSON object boundaries rather than parsing
    the proprietary binary framing.
    """
    data = decompress_journal(journal_path)

    # Find JSON objects by tracking brace depth
    i = 0
    while i < len(data):
        if data[i] == ord("{"):
            depth = 0
            start = i
            for j in range(i, len(data)):
                if data[j] == ord("{"):
                    depth += 1
                elif data[j] == ord("}"):
                    depth -= 1
                    if depth == 0:
                        try:
                            event_bytes = data[start : j + 1]
                            event = json.loads(event_bytes)
                            yield event
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass
                        i = j + 1
                        break
            else:
                break
        else:
            i += 1


def extract_events_filtered(
    journal_path: Path,
    field_filters: dict[str, str] | None = None,
    text_filter: str | None = None,
    max_events: int = 0,
) -> list[dict]:
    """Extract events with optional filtering.

    Args:
        journal_path: Path to journal.zst
        field_filters: Dict of field_name: value to match (supports nested via dot notation)
        text_filter: Substring to search in the raw JSON text
        max_events: Max events to return (0 = unlimited)
    """
    results = []
    count = 0

    if text_filter:
        data = decompress_journal(journal_path)
        text_filter_bytes = text_filter.encode("utf-8")
        if text_filter_bytes not in data:
            return []

    for event in extract_events(journal_path):
        if max_events and count >= max_events:
            break

        if text_filter:
            event_str = json.dumps(event)
            if text_filter.lower() not in event_str.lower():
                continue

        if field_filters:
            match = True
            for field_path, expected in field_filters.items():
                value = _get_nested(event, field_path)
                if value is None or str(value).lower() != expected.lower():
                    match = False
                    break
            if not match:
                continue

        results.append(event)
        count += 1

    return results


def _get_nested(obj: dict, path: str):
    """Get a nested value from a dict using dot notation."""
    parts = path.split(".")
    current = obj
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current
