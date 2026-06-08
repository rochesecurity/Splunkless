# Splunkless

Search engine for exported Splunk buckets — no Splunk license required.

Splunkless builds a lightweight index over Splunk's native bucket format, enabling fast metadata and string searches across terabytes of archived log data stored on cheap object storage.

## The Problem

Splunk Enterprise licensing at scale ($2-5M/year for 3.2 TB/day) makes long-term retention expensive. Regulatory and compliance requirements often mandate 5-10 year retention. Splunkless lets you export cold/frozen buckets from Splunk, archive them on GCS/S3, and still search them — at ~97% lower cost.

## How It Works

Splunk buckets contain rich pre-built metadata that Splunkless extracts and catalogs:

| File | What Splunkless Extracts | Search Use |
|------|------------------------|------------|
| **Directory name** (`db_{latest}_{earliest}_...`) | Event time range | Time-range filtering |
| **Hosts.data** | Hostnames + event counts | `--host` filter |
| **Sources.data** | Data sources | `--source` filter |
| **SourceTypes.data** | Source types | `--sourcetype` filter |
| **\*.tsidx** | ~224K indexed terms per bucket (IPs, CVEs, hostnames, etc.) | `--terms` string search |
| **rawdata/journal.zst** | Compressed JSON events | On-demand event retrieval |

The catalog and term index live in SQLite (~4 GB for millions of buckets). Journals stay on object storage and are fetched only when events need to be read.

## Quick Start

```bash
pip install .

# Ingest a bucket (or directory of buckets)
splunkless ingest /path/to/splunk/data/

# Show what's indexed
splunkless stats

# Search by metadata
splunkless search --host myserver.example.com --sourcetype syslog

# Search by term (IPs, CVEs, hostnames)
splunkless search --terms CVE-2025-29966

# Combined search with event retrieval
splunkless search --host myserver.example.com --terms CVE-2025-29966 --fetch

# Browse indexed terms
splunkless terms CVE-2025
splunkless terms 10.42
```

## Search Performance

| Query Type | What's Searched | Latency |
|------------|----------------|---------|
| host / sourcetype / source | Metadata catalog | < 1s |
| IP, CVE, hostname string | Term-to-bucket index | < 5s |
| Combined (metadata + term) | Intersection | < 5s |
| Event retrieval (`--fetch`) | Journal decompression | 5-30s |

## Splunk Restorability

Archived buckets can be restored back into Splunk for full SPL access:

1. Splunkless identifies the relevant buckets
2. Download full bucket directories from object storage
3. Place in Splunk's `thaweddb` directory
4. Run `splunk rebuild` — search immediately

This works because Splunkless preserves the complete bucket structure (tsidx, bloomfilter, journal, metadata).

## Architecture at Scale

See [docs/architecture.md](docs/architecture.md) for three competing GCP deployment architectures at Fortune 200 scale (3.2 TB/day, 10-year retention):

| Architecture | Monthly Cost | Best For |
|-------------|-------------|----------|
| **A: Serverless** (Cloud Functions + Firestore + GCS) | ~$4,500 | Small teams, infrequent search |
| **B: Containers + Bigtable** (GKE + Bigtable + Spanner) | ~$5,700 | Sustained query load, prefix search |
| **C: AlloyDB + BigQuery** (BigQuery + AlloyDB + Composer) | ~$5,400 | Analytics + search, SQL access |

All architectures include multi-region GCS storage, fault tolerance, lifecycle tiering (Standard -> Nearline -> Coldline -> Archive), and compliance controls (Object Lock, CMEK, audit logging).

## Project Structure

```
splunkless/
  bucket_parser.py    # Parses bucket directory names and .data metadata files
  tsidx_reader.py     # Extracts indexed terms from Splunk's tsidx inverted index
  journal_reader.py   # Decompresses journal.zst and extracts JSON events
  models.py           # SQLite schema for the metadata catalog and term index
  search.py           # Search engine: metadata + term lookup + event retrieval
  ingest.py           # Ingest pipeline: bucket directory -> catalog
  cli.py              # Command-line interface
docs/
  architecture.md     # GCP architecture options with cost analysis
```

## Requirements

- Python 3.11+
- `zstandard` (for journal.zst decompression)

## License

MIT
