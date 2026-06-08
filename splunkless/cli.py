"""CLI interface for Splunkless — ingest buckets and search the catalog."""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .ingest import ingest_bucket, ingest_directory
from .models import init_db
from .search import SearchEngine, SearchQuery

DEFAULT_DB = Path("splunkless.db")


def cmd_ingest(args):
    """Ingest buckets from a directory."""
    data_dir = Path(args.data_dir)
    db_path = Path(args.db)

    if not data_dir.exists():
        print(f"Error: {data_dir} does not exist")
        sys.exit(1)

    # Check if it's a single bucket or a directory of buckets
    if data_dir.name.startswith("db_"):
        conn = init_db(db_path)
        ok = ingest_bucket(data_dir, conn, selective_terms=not args.all_terms)
        conn.close()
        if ok:
            print(f"Ingested bucket: {data_dir.name}")
        else:
            print(f"Failed to ingest: {data_dir.name}")
    else:
        summary = ingest_directory(data_dir, db_path, selective_terms=not args.all_terms)
        print(f"Ingest complete: {json.dumps(summary, indent=2)}")


def cmd_search(args):
    """Search the catalog."""
    engine = SearchEngine(Path(args.db))

    query = SearchQuery(
        host=args.host,
        source=args.source,
        sourcetype=args.sourcetype,
        terms=args.terms if args.terms else [],
        max_events=args.max_events,
    )

    if args.earliest:
        query.earliest = int(datetime.fromisoformat(args.earliest).timestamp())
    if args.latest:
        query.latest = int(datetime.fromisoformat(args.latest).timestamp())

    # Parse field filters (key=value pairs)
    if args.field:
        for f in args.field:
            if "=" in f:
                k, v = f.split("=", 1)
                query.field_filters[k] = v

    result = engine.search(query, fetch_events=args.fetch)

    print(f"\n{'='*70}")
    print(f"Search completed in {result.search_time_ms:.1f}ms")
    print(f"Candidate buckets: {len(result.candidate_buckets)}")

    if result.candidate_buckets:
        print(f"\n--- Matching Buckets ---")
        for b in result.candidate_buckets[:20]:
            et = datetime.fromtimestamp(b.earliest_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            lt = datetime.fromtimestamp(b.latest_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"  {b.bucket_id[:12]}...  {et} - {lt}  events={b.event_count:,}  [{b.match_reason}]")

        if len(result.candidate_buckets) > 20:
            print(f"  ... and {len(result.candidate_buckets) - 20} more")

    if args.fetch and result.events:
        print(f"\n--- Events ({result.total_events_found} found, showing first {min(5, len(result.events))}) ---")
        for event in result.events[:5]:
            print(json.dumps(event, indent=2)[:500])
            print("  ...")

    engine.close()


def cmd_stats(args):
    """Show catalog statistics."""
    engine = SearchEngine(Path(args.db))
    stats = engine.stats()

    print(f"\n{'='*50}")
    print(f"  Splunkless Catalog Statistics")
    print(f"{'='*50}")
    print(f"  Buckets indexed:     {stats['buckets']:>12,}")
    print(f"  Total events:        {stats['total_events']:>12,}")
    print(f"  Unique hosts:        {stats['unique_hosts']:>12,}")
    print(f"  Unique sourcetypes:  {stats['unique_sourcetypes']:>12,}")
    print(f"  Indexed terms:       {stats['indexed_terms']:>12,}")

    if stats["earliest_time"]:
        et = datetime.fromtimestamp(stats["earliest_time"], tz=timezone.utc)
        lt = datetime.fromtimestamp(stats["latest_time"], tz=timezone.utc)
        print(f"  Time range:          {et.strftime('%Y-%m-%d %H:%M')} - {lt.strftime('%Y-%m-%d %H:%M')} UTC")

    print()

    print("  --- Hosts ---")
    for host, count in engine.list_hosts()[:10]:
        print(f"    {host:<45s} {count:>10,} events")

    print("\n  --- Sourcetypes ---")
    for st, count in engine.list_sourcetypes()[:10]:
        print(f"    {st:<45s} {count:>10,} events")

    print("\n  --- Sources ---")
    for src, count in engine.list_sources()[:10]:
        print(f"    {src:<45s} {count:>10,} events")

    engine.close()


def cmd_terms(args):
    """Search indexed terms by prefix."""
    engine = SearchEngine(Path(args.db))
    results = engine.search_terms(args.prefix, limit=args.limit)

    print(f"\nTerms matching '{args.prefix}*':")
    for term, count in results:
        print(f"  {term:<50s}  in {count} bucket(s)")

    engine.close()


def main():
    parser = argparse.ArgumentParser(
        prog="splunkless",
        description="Search engine for exported Splunk buckets — no Splunk required",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to catalog database")
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ingest
    p_ingest = subparsers.add_parser("ingest", help="Ingest Splunk buckets into the catalog")
    p_ingest.add_argument("data_dir", help="Directory containing bucket(s)")
    p_ingest.add_argument("--all-terms", action="store_true",
                          help="Index all terms (not just IPs, CVEs, hostnames)")
    p_ingest.set_defaults(func=cmd_ingest)

    # search
    p_search = subparsers.add_parser("search", help="Search the catalog")
    p_search.add_argument("--host", help="Filter by host")
    p_search.add_argument("--source", help="Filter by source")
    p_search.add_argument("--sourcetype", help="Filter by sourcetype")
    p_search.add_argument("--terms", nargs="+", help="Search terms (strings to find)")
    p_search.add_argument("--earliest", help="Earliest time (ISO format)")
    p_search.add_argument("--latest", help="Latest time (ISO format)")
    p_search.add_argument("--field", nargs="+", help="Field filters (key=value)")
    p_search.add_argument("--fetch", action="store_true", help="Fetch and return actual events")
    p_search.add_argument("--max-events", type=int, default=100, help="Max events to return")
    p_search.set_defaults(func=cmd_search)

    # stats
    p_stats = subparsers.add_parser("stats", help="Show catalog statistics")
    p_stats.set_defaults(func=cmd_stats)

    # terms
    p_terms = subparsers.add_parser("terms", help="Search indexed terms by prefix")
    p_terms.add_argument("prefix", help="Term prefix to search")
    p_terms.add_argument("--limit", type=int, default=20, help="Max results")
    p_terms.set_defaults(func=cmd_terms)

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    args.func(args)


if __name__ == "__main__":
    main()
