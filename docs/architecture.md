# Splunkless — GCP Architecture Options

## Context

**Problem**: Fortune 200 company ingesting 3.2 TB/day into Splunk. Some data needs 10-year retention. Keeping it all searchable in Splunk costs $2-5M/year in licensing.

**Goal**: Export Splunk buckets to cloud storage. Build a lightweight search layer that enables:
- Metadata search (host, source, sourcetype, time range) — instant
- String/term search (IPs, CVEs, hostnames) — seconds
- Event retrieval on demand — tens of seconds
- Ability to **restore buckets back into Splunk** when needed

**Scale at 10 years**:
| Metric | Value |
|--------|-------|
| Daily ingest | 3.2 TB (raw) / ~440 GB (compressed journals) |
| Total buckets | ~6 million |
| Compressed journals | ~1.6 PB |
| Metadata catalog | ~4 GB |
| Term-to-bucket index | ~100-200 GB |

---

## Splunk Bucket Anatomy (from analysis)

Each exported bucket is a directory containing:

```
db_{latest_epoch}_{earliest_epoch}_{seq}_{GUID}/
├── Hosts.data           # TSV: host names + event counts (672 bytes)
├── Sources.data         # TSV: data sources
├── SourceTypes.data     # TSV: sourcetypes
├── Strings.data         # TSV: field types
├── bucket_info.csv      # Index time range
├── *.tsidx              # Inverted index (~6.9 MB) — contains all indexed terms
├── bloomfilter          # Probabilistic term filter (328 KB)
├── rawdata/
│   ├── journal.zst      # Compressed events (8.2 MB → 60 MB) — the actual data
│   ├── slicemin.dat     # Slice timestamps
│   └── slicesv2.dat     # Slice index
├── splunk-autogen-params.dat
├── .rawSize             # Uncompressed size
└── .sizeManifest4.1     # On-disk size
```

**Key insight**: The `.data` files and `tsidx` provide rich metadata without touching the journal. We extract this metadata once, build a central index, then only need the journals for event retrieval.

### Restorability: What Splunk Needs

To restore a bucket into Splunk (e.g., for deep investigation via SPL):
- Splunk needs the **complete bucket directory** — all files, unchanged
- The `tsidx`, `bloomfilter`, `rawdata/`, and `.data` files must all be present
- Splunk re-registers the bucket via `splunk rebuild` or by placing it in the index's thaweddb directory

**This means the architecture must preserve the entire bucket, not just the journal.**

---

## Bucket Export and Restorability — Pros, Cons, and Trade-offs

### Option R1: Preserve Full Bucket (all files)

**What's stored**: The entire bucket directory as-is — journal.zst, tsidx, bloomfilter, .data files, slice files.

| Dimension | Assessment |
|-----------|-----------|
| **Storage cost** | ~15.4 MB per bucket (1.9× the journal alone). At 10 years: ~2.7 PB vs 1.6 PB journal-only. Extra $5K-15K/month on GCS depending on tier. |
| **Restorability** | Instant. Copy directory to Splunk indexer's thaweddb, run `splunk rebuild`, search immediately. No reprocessing needed. |
| **Exportability** | Trivial — download the GCS prefix, it's already a valid bucket. |
| **Complexity** | Lowest. No transformation needed. |

**Verdict**: **Recommended.** The 1.9× overhead (vs journal-only) is modest. At deep-archive pricing ($1/TB/month), the entire 2.7 PB costs ~$2,700/month. The ability to restore a bucket into Splunk in minutes is invaluable for incident response.

### Option R2: Journal-Only + Rebuild

**What's stored**: Only `journal.zst` + minimal metadata files (`.data`, `bucket_info.csv`).

| Dimension | Assessment |
|-----------|-----------|
| **Storage cost** | ~8.2 MB per bucket. At 10 years: ~1.6 PB. Saves ~48% vs full bucket. |
| **Restorability** | Possible but slow. Must run `splunk rebuild` which re-creates the tsidx and bloomfilter from the journal. Takes 2-10 minutes per bucket depending on size. |
| **Exportability** | Need to reconstruct the bucket directory structure and metadata files. |
| **Complexity** | Medium. Rebuild step requires a running Splunk instance. |

**Verdict**: Viable for deep archive where restoration is rare. But you need a Splunk instance available for rebuilds — partially defeats the purpose.

### Option R3: Journal-Only + New Index (No Splunk Restore)

**What's stored**: Only `journal.zst`. All search goes through Splunkless.

| Dimension | Assessment |
|-----------|-----------|
| **Storage cost** | Minimum: ~1.6 PB at 10 years. |
| **Restorability** | **Not possible** without a running Splunk instance to re-index the raw events. If you ever need SPL, you must re-ingest from raw JSON. |
| **Exportability** | Events are JSON inside zstd — universally portable. Can be ingested into any SIEM, data lake, or search engine. |
| **Complexity** | Medium. Splunkless must handle all search needs. |

**Verdict**: Most cost-efficient but burns the bridge back to Splunk. Only viable if you're confident Splunkless meets all search needs.

### Recommendation

**Use R1 (full bucket preservation) for the first 2 years**, then lifecycle-transition to R2 (journal + metadata only) for years 3-10. This gives:
- Instant Splunk restorability for recent investigations
- Rebuild-capable archives for older data
- ~40% storage savings on the long tail

```
Year 0-2:  Full bucket on GCS Nearline    ($5/TB/month)  → instant Splunk restore
Year 2-5:  Full bucket on GCS Coldline    ($2.5/TB/month) → instant restore, slower fetch
Year 5-10: Journal-only on GCS Archive    ($1.2/TB/month) → rebuild required
```

---

## Architecture A: Cloud-Native Serverless

**Philosophy**: Minimize ops. No servers to manage. Pay per query.

```
                         ┌──────────────────────────────────────────────┐
                         │              Cloud Scheduler                 │
                         │  (triggers ingest pipeline hourly)           │
                         └──────────────┬───────────────────────────────┘
                                        │
                         ┌──────────────▼───────────────────────────────┐
                         │         Cloud Functions (Python)             │
                         │  1. Watch GCS ingest bucket for new exports  │
                         │  2. Parse .data files → metadata             │
                         │  3. Parse tsidx → extract terms              │
                         │  4. Write to Firestore + catalog             │
                         │  5. Move bucket to tiered GCS                │
                         └──────────────┬───────────────────────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────────┐
              │                         │                             │
    ┌─────────▼──────────┐  ┌───────────▼──────────┐   ┌─────────────▼──────────┐
    │  Cloud SQL (PG)    │  │  Firestore           │   │  GCS Multi-Region      │
    │  (metadata catalog)│  │  (term→bucket index) │   │  (bucket archives)     │
    │                    │  │                       │   │                        │
    │  - bucket table    │  │  Collection: terms    │   │  gs://splunkless-hot/  │
    │  - host/source/st  │  │  Doc: {term}          │   │  gs://splunkless-warm/ │
    │  - time ranges     │  │  Field: bucket_ids[]  │   │  gs://splunkless-cold/ │
    │                    │  │                       │   │                        │
    │  HA: Regional      │  │  HA: Multi-region     │   │  HA: Dual/multi-region │
    │  ~4 GB             │  │  auto-replication     │   │  Object Versioning     │
    └────────────────────┘  └───────────────────────┘   └────────────────────────┘
              │                         │                             │
              └─────────────────────────┼─────────────────────────────┘
                                        │
                         ┌──────────────▼───────────────────────────────┐
                         │         Cloud Run (Query API)               │
                         │  1. Parse query (host/sourcetype/term/time) │
                         │  2. Query Cloud SQL for bucket candidates   │
                         │  3. Query Firestore for term intersection   │
                         │  4. Fetch journals from GCS on demand       │
                         │  5. Decompress + filter → return events     │
                         └─────────────────────────────────────────────┘
```

### GCP Services

| Component | Service | Config | Monthly Cost |
|-----------|---------|--------|-------------|
| Metadata catalog | Cloud SQL (PostgreSQL) | db-f1-micro HA | $50 |
| Term index | Firestore (Native mode) | Multi-region | $200-500 (at ~100M docs) |
| Journal storage | GCS multi-region | Nearline → Coldline → Archive lifecycle | $3,800 (10yr avg) |
| Ingest pipeline | Cloud Functions (2nd gen) | 1 GB RAM, triggered by GCS | $100 |
| Query API | Cloud Run | 2 vCPU, auto-scale 0-10 | $50-200 |
| Orchestration | Cloud Scheduler + Pub/Sub | Hourly triggers | $5 |

**Estimated total: $4,200-4,800/month**

### Fault Tolerance

| Failure | Mitigation |
|---------|-----------|
| Cloud SQL outage | HA regional instance, automatic failover |
| Firestore outage | Multi-region replication (99.999% SLA) |
| GCS outage | Multi-region bucket (99.95% SLA) |
| Cloud Function crash | Automatic retry with dead-letter queue |
| Query API overload | Cloud Run auto-scaling, max concurrency limits |
| Region failure | Multi-region GCS + Firestore survive. Cloud SQL needs cross-region read replica ($100/mo more) |

### Pros
- Zero server management
- Auto-scales to zero when idle (cheap for infrequent searches)
- Firestore handles term index at any scale without capacity planning
- GCS lifecycle policies automate tiering
- Cloud Functions handle bursty ingest (many buckets exported at once)

### Cons
- Firestore cost scales with document reads — a term search touching 1000 docs = $0.06 per query (adds up)
- Cloud SQL micro instance may bottleneck under concurrent queries
- Cold-start latency on Cloud Run/Functions (1-3s) adds to query time
- 9-minute Cloud Function timeout limits single-bucket ingest size (fine for normal buckets, problematic for >10 GB)
- Firestore term documents have a 1 MB limit — terms appearing in >~100K buckets need sharding

### Best For
- Small-to-medium teams with limited ops capacity
- Infrequent search patterns (compliance audits, incident response)
- Budget-sensitive deployments

---

## Architecture B: Managed Containers + Bigtable

**Philosophy**: Production-grade. Handles sustained query load. Strong consistency.

```
                         ┌──────────────────────────────────────────────┐
                         │            Pub/Sub (ingest trigger)          │
                         │  (Splunk export → notification → pipeline)  │
                         └──────────────┬───────────────────────────────┘
                                        │
                         ┌──────────────▼───────────────────────────────┐
                         │         Dataflow (Streaming/Batch)          │
                         │  - Reads new buckets from GCS staging       │
                         │  - Parses .data + tsidx in parallel         │
                         │  - Writes metadata to Spanner               │
                         │  - Writes terms to Bigtable                 │
                         │  - Moves buckets to archive GCS             │
                         │  Autoscaling: 1-20 workers                  │
                         └──────────────┬───────────────────────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────────┐
              │                         │                             │
    ┌─────────▼──────────┐  ┌───────────▼──────────┐   ┌─────────────▼──────────┐
    │  Cloud Spanner     │  │  Bigtable             │   │  GCS Dual-Region       │
    │  (metadata catalog)│  │  (term→bucket index)  │   │  (bucket archives)     │
    │                    │  │                        │   │                        │
    │  Regional (1 node) │  │  Row key: term#bucket  │   │  Dual-region:          │
    │  Strong consistency│  │  Column: metadata      │   │  US-EAST1 + US-WEST1   │
    │  Auto-scaling      │  │                        │   │  or EU multi-region    │
    │                    │  │  1-3 nodes             │   │                        │
    │  Multi-region      │  │  SSD storage           │   │  Object Lock (WORM)    │
    │  option available  │  │  Replication: 2 zones  │   │  for compliance        │
    └────────────────────┘  └────────────────────────┘   └────────────────────────┘
              │                         │                             │
              └─────────────────────────┼─────────────────────────────┘
                                        │
                         ┌──────────────▼───────────────────────────────┐
                         │         GKE Autopilot (Query Service)       │
                         │                                              │
                         │  ┌─────────────┐  ┌──────────────────────┐  │
                         │  │ Query API   │  │ Journal Fetch Worker │  │
                         │  │ (Go/Python) │  │ (parallel decompress)│  │
                         │  │ 2-8 pods    │  │ 0-20 pods            │  │
                         │  └─────────────┘  └──────────────────────┘  │
                         │                                              │
                         │  Memorystore (Redis) — query result cache    │
                         └─────────────────────────────────────────────┘
```

### GCP Services

| Component | Service | Config | Monthly Cost |
|-----------|---------|--------|-------------|
| Metadata catalog | Cloud Spanner | 1 node regional (100 PU) | $65 |
| Term index | Bigtable | 1 node SSD, 2-zone replication | $750 |
| Journal storage | GCS dual-region | US or EU pair, lifecycle tiered | $4,200 |
| Ingest pipeline | Dataflow | 1-20 n1-standard-2 workers (batch) | $300 |
| Query API | GKE Autopilot | 2-8 pods, auto-scale | $200 |
| Journal workers | GKE Autopilot | 0-20 pods (burst) | $100 |
| Query cache | Memorystore (Redis) | 1 GB basic | $35 |
| Orchestration | Pub/Sub + Cloud Scheduler | | $10 |

**Estimated total: $5,660/month**

### Bigtable Schema for Term Index

```
Row key:    <term>#<bucket_id>
Column family: meta
  - earliest_time: int64
  - latest_time: int64
  - event_count: int64

Example rows:
  "CVE-2025-29966#BA290D6F" → {earliest: 1752287409, latest: 1752291236, events: 3}
  "10.42.120.10#BA290D6F"   → {earliest: 1752287409, latest: 1752291236, events: 15}

Prefix scan: "CVE-2025-*" → instant range scan
Time filter: checked after key lookup (secondary filter)
```

This schema supports:
- Exact term lookup: single row read
- Prefix scan: `CVE-2025-*` → efficient Bigtable prefix scan
- Term + time intersection: read term rows, filter by time columns
- Hot term handling: Bigtable distributes across nodes automatically

### Fault Tolerance

| Failure | Mitigation |
|---------|-----------|
| Spanner outage | Multi-region config survives single region loss (99.999% SLA) |
| Bigtable node failure | Automatic rebalancing, 2-zone replication |
| GCS outage | Dual-region: synchronous replication between paired regions |
| Dataflow worker crash | Automatic retry + checkpointing |
| GKE pod failure | Auto-restart, HPA maintains minimum replicas |
| Region failure | GCS dual-region survives. Spanner multi-region survives. Bigtable needs manual replication cluster setup to second region (~$750/mo more) |

### Pros
- Bigtable handles massive term index efficiently (billions of rows, prefix scans, no sharding needed)
- Dataflow auto-scales ingest workers — handles burst exports during Splunk maintenance
- Spanner provides global strong consistency for the metadata catalog
- GKE separates query API from journal fetch workers — can scale independently
- Redis cache eliminates repeated queries for the same terms
- Bigtable's row key design naturally supports prefix-based wildcard search

### Cons
- Bigtable minimum cost is ~$750/month even at low utilization (1 node minimum)
- Spanner multi-region adds significant cost (~$2,600/month for 3-region)
- More moving parts to monitor (Dataflow, GKE, Bigtable, Spanner, Redis)
- Dataflow jobs have startup latency (~2-5 min) — not ideal for real-time ingest
- Requires Kubernetes familiarity for the query layer
- GKE Autopilot cost is less predictable than Cloud Run

### Best For
- Teams with existing GKE/Kubernetes operations
- Sustained query load (multiple analysts searching daily)
- Need for prefix/wildcard search performance
- Organizations that value strong consistency guarantees

---

## Architecture C: AlloyDB + BigQuery Hybrid

**Philosophy**: Leverage BigQuery for analytics and large-scale searches. AlloyDB for operational queries. Best for orgs already invested in the Google data ecosystem.

```
                         ┌──────────────────────────────────────────────┐
                         │     Cloud Composer (Airflow)                 │
                         │  DAG: hourly_bucket_ingest                  │
                         │  DAG: daily_term_rollup                     │
                         │  DAG: weekly_storage_lifecycle              │
                         └──────────────┬───────────────────────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────────┐
              │                         │                             │
    ┌─────────▼──────────┐  ┌───────────▼──────────┐   ┌─────────────▼──────────┐
    │  AlloyDB            │  │  BigQuery            │   │  GCS Multi-Region      │
    │  (operational)      │  │  (analytical)        │   │  (bucket archives)     │
    │                     │  │                      │   │                        │
    │  - Bucket catalog   │  │  Table: terms        │   │  Multi-region          │
    │  - Host/source/st   │  │    (term, bucket_id, │   │  (US, EU, or ASIA)     │
    │  - Recent searches  │  │     earliest, latest) │   │                       │
    │  - Query audit log  │  │                       │   │  Lifecycle:            │
    │                     │  │  Table: events_cache  │   │  Standard → Nearline  │
    │  HA: Regional       │  │    (pre-extracted     │   │  → Coldline → Archive │
    │  2 vCPUs            │  │     hot events)       │   │                       │
    │  Read pool: 2 nodes │  │                       │   │  Object Lock (WORM)   │
    └─────────────────────┘  │  Materialized Views:  │   │  Retention policies   │
                             │  - term_stats         │   └───────────────────────┘
                             │  - host_summary       │             │
                             │  - daily_volume       │             │
                             └───────────────────────┘             │
                                        │                          │
                         ┌──────────────▼──────────────────────────▼┐
                         │        Cloud Run (Query Service)         │
                         │                                          │
                         │  Fast path: AlloyDB (metadata lookups)   │
                         │  Scan path: BigQuery (term searches)     │
                         │  Fetch path: GCS (journal retrieval)     │
                         │                                          │
                         │  + BigQuery external tables over GCS     │
                         │    for direct journal querying (future)   │
                         └──────────────────────────────────────────┘
```

### BigQuery Term Index Design

```sql
-- Main term-to-bucket mapping
CREATE TABLE splunkless.terms (
  term STRING NOT NULL,
  bucket_id STRING NOT NULL,
  earliest_time TIMESTAMP NOT NULL,
  latest_time TIMESTAMP NOT NULL,
  event_count INT64,
  journal_gcs_path STRING
)
PARTITION BY DATE(earliest_time)
CLUSTER BY term, bucket_id;

-- Pre-aggregated stats (materialized view)
CREATE MATERIALIZED VIEW splunkless.term_stats AS
SELECT
  term,
  COUNT(DISTINCT bucket_id) as bucket_count,
  SUM(event_count) as total_events,
  MIN(earliest_time) as first_seen,
  MAX(latest_time) as last_seen
FROM splunkless.terms
GROUP BY term;

-- Example query: find buckets for a CVE in a time range
SELECT bucket_id, journal_gcs_path, event_count
FROM splunkless.terms
WHERE term = 'CVE-2025-29966'
  AND earliest_time >= '2025-01-01'
  AND latest_time <= '2025-12-31';
-- Cost: scans only the relevant partitions, clustered on term → very fast
```

### BigQuery Event Cache (Optional — Phase 4)

```sql
-- For hot data (last 90 days), pre-extract events into BigQuery
CREATE TABLE splunkless.events_cache (
  bucket_id STRING,
  event_time TIMESTAMP,
  host STRING,
  sourcetype STRING,
  severity STRING,
  plugin_id INT64,
  plugin_name STRING,
  asset_fqdn STRING,
  ipv4 STRING,
  cve STRING,  -- repeated field for multi-CVE
  raw_json STRING
)
PARTITION BY DATE(event_time)
CLUSTER BY host, sourcetype, severity;

-- Full-power SQL over recent events — no journal decompression needed
SELECT asset_fqdn, severity, COUNT(*) as vuln_count
FROM splunkless.events_cache
WHERE cve = 'CVE-2025-29966'
  AND event_time >= '2025-07-01'
GROUP BY 1, 2
ORDER BY vuln_count DESC;
```

### GCP Services

| Component | Service | Config | Monthly Cost |
|-----------|---------|--------|-------------|
| Operational catalog | AlloyDB | 2 vCPU + 2-node read pool, regional HA | $400 |
| Term index + analytics | BigQuery | On-demand pricing, ~200 GB active storage | $250 |
| Event cache (90 days) | BigQuery | Partitioned, ~2 TB active | $100 |
| Journal storage | GCS multi-region | Lifecycle tiered | $4,200 |
| Ingest orchestration | Cloud Composer (small) | 1 environment | $350 |
| Query API | Cloud Run | 2 vCPU, auto-scale | $100 |

**Estimated total: $5,400/month**

### Fault Tolerance

| Failure | Mitigation |
|---------|-----------|
| AlloyDB outage | Regional HA (automatic failover), read pool survives primary failure |
| BigQuery outage | Multi-region dataset option, 99.99% SLA |
| GCS outage | Multi-region bucket (99.95% SLA), Object Lock prevents deletion |
| Composer failure | Airflow DAGs are idempotent, automatic retry |
| Region failure | BigQuery multi-region survives. AlloyDB needs cross-region replica ($400/mo more). GCS multi-region survives. |

### Pros
- **BigQuery is ideal for the term index**: clustered + partitioned tables handle billions of term-bucket pairs, prefix scans via `LIKE 'CVE-2025%'`, and time-range filtering natively
- Analytics-ready: materialized views give instant dashboards (term trends, host summaries, volume over time)
- Event cache in BigQuery enables full SQL over recent events without journal decompression
- Cloud Composer (Airflow) provides observable, debuggable ingest pipelines with dependency management
- BigQuery slots are shared — queries are cheap when the term index is small
- Path to **BigQuery external tables over GCS**: in the future, query journal.zst files directly from BigQuery using UDFs

### Cons
- AlloyDB is more expensive than Cloud SQL for a small metadata catalog
- Cloud Composer minimum cost is ~$350/month (heavyweight for a simple hourly ingest)
- BigQuery on-demand pricing is unpredictable if analysts run many broad queries
- BigQuery query latency floor is ~1-2 seconds (slot scheduling overhead) — slower than Bigtable for single-key lookups
- More complex IAM setup (BigQuery datasets, AlloyDB private IP, Composer service accounts)
- Event cache in BigQuery requires ongoing ETL to decompress journals — 3.2 TB/day × 90 days = continuous pipeline

### Best For
- Organizations already using BigQuery for analytics
- Teams that want SQL access to the term index and event data
- Use cases that combine search with analytics (trend reports, compliance dashboards)
- Future-proofing toward direct BigQuery federation over archived journals

---

## Architecture Comparison Matrix

| Dimension | A: Serverless | B: Containers+Bigtable | C: AlloyDB+BigQuery |
|-----------|--------------|----------------------|-------------------|
| **Monthly cost** | $4,200-4,800 | $5,660 | $5,400 |
| **10-year total** | ~$550K | ~$680K | ~$650K |
| **Ops complexity** | Low | Medium-High | Medium |
| **Metadata query** | <1s | <100ms | <1s |
| **Term search** | 1-5s | <1s | 1-3s |
| **Wildcard search** | Limited | Fast (prefix scan) | Good (LIKE) |
| **Event retrieval** | 5-30s | 5-20s | 5-30s (or <5s from cache) |
| **Analytics** | None | Basic | Full SQL + dashboards |
| **Splunk restore** | Manual GCS download | Automated via pipeline | Automated via DAG |
| **Scale ceiling** | Firestore limits | Bigtable scales infinitely | BigQuery scales infinitely |
| **Min team size** | 1 engineer | 2-3 engineers | 2 engineers |
| **GKE required** | No | Yes | No |
| **Cold start** | 1-3s | None (always-on) | 1-2s (Cloud Run) |
| **Multi-region** | Built-in (Firestore, GCS) | Extra cost (Bigtable) | Built-in (BigQuery, GCS) |

### Decision Framework

```
Do you already run GKE?
  YES → Architecture B (Bigtable gives you the fastest term lookups)
  NO  →
    Do you use BigQuery for analytics?
      YES → Architecture C (natural fit, adds analytics for free)
      NO  →
        Is search infrequent (< 50 queries/day)?
          YES → Architecture A (cheapest, auto-scales to zero)
          NO  → Architecture B (sustained load needs always-on infra)
```

---

## Multi-Region Storage Strategy (All Architectures)

### GCS Bucket Layout

```
gs://splunkless-{region}-archive/
  └── {index_name}/
      └── {year}/{month}/{day}/
          └── db_{latest}_{earliest}_{seq}_{guid}/
              ├── Hosts.data
              ├── Sources.data
              ├── SourceTypes.data
              ├── Strings.data
              ├── bucket_info.csv
              ├── *.tsidx                    # kept for Splunk restorability
              ├── bloomfilter
              ├── rawdata/journal.zst
              ├── rawdata/slicemin.dat
              ├── rawdata/slicesv2.dat
              └── splunk-autogen-params.dat
```

### Lifecycle Policy

```json
{
  "lifecycle": {
    "rule": [
      {"action": {"type": "SetStorageClass", "storageClass": "NEARLINE"},
       "condition": {"age": 90}},
      {"action": {"type": "SetStorageClass", "storageClass": "COLDLINE"},
       "condition": {"age": 730}},
      {"action": {"type": "SetStorageClass", "storageClass": "ARCHIVE"},
       "condition": {"age": 1825}},
      {"action": {"type": "Delete"},
       "condition": {"age": 3650}}
    ]
  }
}
```

| Tier | Age | Cost ($/TB/mo) | Retrieval | Use Case |
|------|-----|---------------|-----------|----------|
| Standard | 0-90 days | $26 | Instant | Active investigations |
| Nearline | 90 days-2 yr | $10 | <1s, $0.01/GB | Recent archives |
| Coldline | 2-5 yr | $4 | <1s, $0.02/GB | Older archives |
| Archive | 5-10 yr | $1.2 | Hours, $0.05/GB | Deep compliance retention |

### Multi-Region Options

| GCS Option | Regions | Cost Premium | Use Case |
|------------|---------|-------------|----------|
| Dual-region | US-EAST1 + US-WEST1 | +20% | US-centric, fast failover |
| Dual-region | EUROPE-WEST1 + EUROPE-WEST4 | +20% | EU data residency (GDPR) |
| Multi-region | US (auto) | +2× vs regional | Max availability |
| Multi-region | EU (auto) | +2× vs regional | EU compliance + HA |

**Recommendation for Roche**: EU multi-region for GDPR compliance, or dual-region (EUROPE-WEST1 + EUROPE-WEST4) for cost efficiency with EU data residency.

### Compliance Controls

- **Object Lock (Retention)**: Prevent deletion/modification for retention period
- **Object Versioning**: Protect against accidental overwrites
- **Bucket Lock**: Make retention policy irrevocable (compliance requirement)
- **Access Logging**: Cloud Audit Logs for all bucket access
- **Encryption**: Customer-Managed Encryption Keys (CMEK) via Cloud KMS
- **VPC Service Controls**: Restrict data exfiltration to authorized networks

---

## Splunk Restore Workflow

When an analyst needs to deep-dive with SPL on archived data:

```
1. Analyst searches Splunkless:
   splunkless search --host reawslsendsa.roche.com --terms CVE-2025-29966
   → Returns: 12 matching buckets

2. Analyst requests restore:
   splunkless restore --bucket-ids BA290D6F,C3E8F1A2,... --target-index restored_vuln

3. Splunkless pipeline:
   a. Downloads full bucket directories from GCS
   b. Places them in /opt/splunk/var/lib/splunk/restored_vuln/thaweddb/
   c. Runs: splunk rebuild /opt/splunk/var/lib/splunk/restored_vuln/thaweddb/db_*
   d. Notifies analyst: "12 buckets restored, 112,536 events searchable"

4. Analyst searches in Splunk:
   index=restored_vuln host=reawslsendsa.roche.com CVE-2025-29966
   → Full SPL power over the targeted data

5. After investigation, cleanup:
   splunkless restore --cleanup --target-index restored_vuln
   → Removes thawed buckets from Splunk indexer
```

**Key**: You only restore the specific buckets identified by Splunkless — not the entire archive. This is typically 10-100 buckets, not millions.

---

## Implementation Timeline

| Phase | Duration | Deliverable |
|-------|----------|-------------|
| 1. Prototype | 2 weeks | Python CLI: ingest + search (this repo) |
| 2. GCS integration | 2 weeks | Bucket upload, lifecycle policies, multi-region |
| 3. Catalog service | 3 weeks | Cloud SQL/Spanner + API (chosen architecture) |
| 4. Term index | 3 weeks | Bigtable/Firestore/BigQuery term-to-bucket mapping |
| 5. Splunk restore | 2 weeks | Automated thaw/rebuild workflow |
| 6. Backfill | 4 weeks | Process existing frozen/cold buckets |
| 7. Production | 2 weeks | Monitoring, alerting, access controls |
| **Total** | **~18 weeks** | |
