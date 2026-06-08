"""Extract indexed terms from Splunk tsidx files.

The tsidx (Time Series Index) is Splunk's inverted index stored in each bucket.
Format: _TSIDX9 header + posting lists + prefix-compressed term dictionary.

We extract terms by scanning the term dictionary region for printable ASCII
sequences, then classify them into searchable categories. This avoids needing
to reverse-engineer the full posting list format — we only need the terms
themselves, not their per-event positions (our index maps terms to buckets,
not to individual events).
"""

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

TSIDX_MAGIC = b"_TSIDX"

IP_PATTERN = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
CVE_PATTERN = re.compile(r"^CVE-\d{4}-\d+$", re.IGNORECASE)
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
FQDN_PATTERN = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.-]+\.)+[a-z]{2,}$")

# Terms that appear in virtually every bucket and have no search value
STOP_TERMS = frozenset(
    {
        "the",
        "and",
        "for",
        "not",
        "are",
        "but",
        "was",
        "this",
        "that",
        "with",
        "from",
        "have",
        "has",
        "will",
        "can",
        "all",
        "been",
        "each",
        "which",
        "their",
        "said",
        "true",
        "false",
        "none",
        "null",
        "timestamp",
        "event",
        "host",
        "source",
        "sourcetype",
        "punct",
    }
)


@dataclass
class TermExtractionResult:
    total_terms: int = 0
    ips: set[str] = field(default_factory=set)
    cves: set[str] = field(default_factory=set)
    hostnames: set[str] = field(default_factory=set)
    all_terms: set[str] = field(default_factory=set)
    version: str = ""


def find_term_dictionary_offset(data: bytes) -> int:
    """Find the start of the term dictionary in a tsidx file.

    The term dictionary starts after the posting lists section,
    identifiable by the host::/source::/sourcetype:: metadata entries.
    Falls back to scanning for the first dense printable text region.
    """
    # Look for 'host::' which marks the start of the term dictionary metadata
    marker = data.find(b"host::")
    if marker > 0:
        # Back up to find the actual start of this section
        # The metadata block starts with some length/type bytes before "host::"
        scan_start = max(0, marker - 64)
        for i in range(marker, scan_start, -1):
            if data[i] < 0x20 and data[i] not in (0x01, 0x03, 0x04, 0x05, 0x06, 0x07):
                return i + 1
        return scan_start

    # Fallback: look for 'source::' or 'sourcetype::'
    for prefix in (b"source::", b"sourcetype::"):
        pos = data.find(prefix)
        if pos > 0:
            return max(0, pos - 64)

    # Last resort: start from 60% into the file (term dict is in the latter portion)
    return int(len(data) * 0.6)


def extract_terms_from_tsidx(tsidx_path: Path, selective: bool = True) -> TermExtractionResult:
    """Extract searchable terms from a tsidx file.

    Args:
        tsidx_path: Path to the .tsidx file
        selective: If True, only keep high-value terms (IPs, CVEs, hostnames, etc.)
                   If False, keep all terms >= 3 chars (larger index, more coverage)
    """
    result = TermExtractionResult()

    data = tsidx_path.read_bytes()

    if not data.startswith(TSIDX_MAGIC):
        return result

    # Extract version
    result.version = data[:8].decode("ascii", errors="replace").rstrip("\x00")

    # Locate term dictionary
    term_dict_offset = find_term_dictionary_offset(data)

    # Extract all printable ASCII sequences from the term dictionary region
    raw_terms: list[str] = []
    current: list[str] = []
    for b in data[term_dict_offset:]:
        if 32 <= b < 127:
            current.append(chr(b))
        else:
            if len(current) >= 2:
                raw_terms.append("".join(current))
            current = []
    if len(current) >= 2:
        raw_terms.append("".join(current))

    result.total_terms = len(set(t.lower() for t in raw_terms))

    for term in raw_terms:
        t = term.strip()
        if len(t) < 2:
            continue

        tl = t.lower()

        if tl in STOP_TERMS:
            continue

        # Classify the term
        if IP_PATTERN.match(t):
            result.ips.add(t)
            result.all_terms.add(t)
        elif CVE_PATTERN.match(t):
            result.cves.add(t.upper())
            result.all_terms.add(t.upper())
        elif FQDN_PATTERN.match(t):
            result.hostnames.add(tl)
            result.all_terms.add(tl)
        elif not selective:
            if len(t) >= 3 and not t.isdigit():
                result.all_terms.add(tl)

    return result


def find_tsidx_files(bucket_path: Path) -> list[Path]:
    """Find all .tsidx files in a bucket directory."""
    return list(bucket_path.glob("*.tsidx"))
