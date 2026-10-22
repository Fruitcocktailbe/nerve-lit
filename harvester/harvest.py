"""Fase 1 — harvest PubMed-metadata.

    uv run python -m harvester.harvest search --query core
    uv run python -m harvester.harvest fetch --limit 20
    uv run python -m harvester.harvest run --query scope
    uv run python -m harvester.harvest stats

Twee stappen, allebei resumebaar:
  search  esearch -> PMID's in de werklijst (pmid_queue)
  fetch   efetch  -> volledige metadata voor alles wat nog geen record heeft
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sqlite3
import sys
from datetime import date, timedelta

from . import db, parse, queries
from .config import Config, load_config
from .ncbi import NCBIClient

log = logging.getLogger("harvest")

# PubMed weigert retstart + retmax > 10.000 binnen één zoekopdracht. Onder dat
# plafond blijven en grotere sets in datumvensters hakken.
PAGE_CAP = 9500
PAGE_SIZE = 5000
EARLIEST = date(1900, 1, 1)


def query_hash(term: str) -> str:
    return hashlib.sha256(term.encode("utf-8")).hexdigest()[:16]


def _fmt(day: date) -> str:
    return day.strftime("%Y/%m/%d")


def collect_pmids(
    conn: sqlite3.Connection,
    client: NCBIClient,
    term: str,
    query_name: str,
    limit: int | None = None,
) -> int:
    """Alle PMID's voor de query in de werklijst zetten.

    Deelt het zoekvenster recursief in tweeën tot elk venster onder PubMeds
    paginatieplafond zit. Afgewerkte vensters worden gelogd, zodat een
    onderbroken run niet opnieuw hoeft te beginnen.
    """
    qhash = query_hash(term)
    total_count = client.esearch_count(term)
    log.info("Query levert %d treffers op (hash %s)", total_count, qhash)
    if total_count == 0:
        return 0

    added = 0
    # Stack van datumvensters, nieuwste eerst — zo komt recent werk als eerste binnen.
    windows: list[tuple[date, date]] = [(EARLIEST, date.today())]

    while windows:
        if limit is not None and added >= limit:
            log.info("Limiet %d bereikt — search gestopt", limit)
            break

        start, end = windows.pop()
        mindate, maxdate = _fmt(start), _fmt(end)

        if db.window_done(conn, qhash, mindate, maxdate):
            log.debug("Venster %s..%s al gedaan", mindate, maxdate)
            continue

        count = client.esearch_count(term, mindate, maxdate)
        if count == 0:
            db.mark_window_done(conn, qhash, mindate, maxdate, 0)
            continue

        if count > PAGE_CAP and start < end:
            middle = start + (end - start) // 2
            # Nieuwste helft bovenop de stack -> die wordt eerst opgehaald.
            windows.append((start, middle))
            windows.append((middle + timedelta(days=1), end))
            log.debug("Venster %s..%s heeft %d treffers — opgesplitst", mindate, maxdate, count)
            continue

        if count > PAGE_CAP:
            log.warning(
                "Venster %s..%s heeft %d treffers op één dag — alleen de eerste %d worden gepakt",
                mindate, maxdate, count, PAGE_CAP,
            )

        window_added = 0
        truncated = False
        for retstart in range(0, min(count, PAGE_CAP), PAGE_SIZE):
            _, pmids = client.esearch_pmids(
                term, retstart=retstart, retmax=PAGE_SIZE, mindate=mindate, maxdate=maxdate
            )
            if not pmids:
                break
            window_added += db.enqueue_pmids(conn, pmids, query_name)
            conn.commit()
            if limit is not None and added + window_added >= limit:
                truncated = True
                break

        added += window_added
        if truncated:
            # Venster NIET afvinken: er staan nog pagina's open. Zou het hier
            # als 'done' geboekt worden, dan slaat een latere volledige run het
            # over en mist het corpus stilzwijgend duizenden artikels.
            log.info(
                "Venster %s..%s afgebroken op de limiet — blijft open voor een volgende run",
                mindate, maxdate,
            )
            break

        db.mark_window_done(conn, qhash, mindate, maxdate, count)
        log.info(
            "Venster %s..%s: %d treffers, %d nieuw in de werklijst (totaal nieuw: %d)",
            mindate, maxdate, count, window_added, added,
        )

    return added


def fetch_metadata(
    conn: sqlite3.Connection,
    client: NCBIClient,
    batch_size: int = 200,
    limit: int | None = None,
    from_query: str | None = None,
) -> int:
    """efetch voor elke PMID in de werklijst zonder metadata-record."""
    pending = db.pending_pmids(conn, limit=limit, query_name=from_query)
    if not pending:
        log.info("Niets te doen — geen PMID's zonder metadata")
        return 0

    log.info("%d PMID's zonder metadata, batches van %d", len(pending), batch_size)
    stored = 0
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset : offset + batch_size]
        xml_bytes = client.efetch_pubmed(batch)
        articles = parse.parse_efetch_xml(xml_bytes)
        stored += db.upsert_articles(conn, articles)

        missing = len(batch) - len(articles)
        if missing:
            # Ingetrokken of verwijderde records — die komen niet terug in de XML.
            log.warning("Batch %d: %d van %d PMID's leverden geen record op",
                        offset // batch_size + 1, missing, len(batch))
        log.info("%d/%d metadata-records opgeslagen", stored, len(pending))

    return stored


def print_stats(conn: sqlite3.Connection) -> None:
    values = db.stats(conn)
    width = max(len(key) for key in values)
    print("\n  nerve-lit — metadata-store")
    print("  " + "-" * (width + 12))
    for key, value in values.items():
        print(f"  {key.ljust(width)}  {value:>8,}")
    print()


def build_client(config: Config) -> NCBIClient:
    if not config.ncbi_api_key:
        log.warning("Geen NCBI_API_KEY in .env — rate-limit blijft 3 req/s in plaats van 10")
    if not config.ncbi_email:
        log.warning("Geen NCBI_EMAIL in .env — NCBI vraagt om identificatie bij bulkgebruik")
    return NCBIClient(
        api_key=config.ncbi_api_key, email=config.ncbi_email, tool=config.ncbi_tool
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harvester.harvest", description=__doc__)
    parser.add_argument(
        "command", choices=["search", "fetch", "run", "stats", "count"],
        help="search=PMID's verzamelen, fetch=metadata ophalen, run=allebei, "
             "count=alleen het aantal treffers tonen",
    )
    parser.add_argument(
        "--query", default="scope",
        help=f"named query ({', '.join(queries.NAMED_QUERIES)}) of een letterlijke PubMed-query",
    )
    parser.add_argument("--limit", type=int, default=None, help="stop na N records (dry-run)")
    parser.add_argument("--batch-size", type=int, default=200, help="PMID's per efetch-call")
    parser.add_argument(
        "--from-query", default=None,
        help="fetch alleen PMID's die via deze named query in de werklijst kwamen",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        # httpx logt elke request op INFO — bij 200 batches is dat onleesbaar.
        logging.getLogger("httpx").setLevel(logging.WARNING)

    config = load_config()
    config.ensure_dirs()
    term = queries.resolve(args.query)

    with db.session(config.db_path) as conn, build_client(config) as client:
        if args.command == "count":
            print(f"\n  {client.esearch_count(term):,} treffers voor query '{args.query}'\n")
            return 0

        if args.command == "stats":
            print_stats(conn)
            return 0

        if args.command in ("search", "run"):
            added = collect_pmids(conn, client, term, args.query, limit=args.limit)
            log.info("Search klaar — %d nieuwe PMID's in de werklijst", added)

        if args.command in ("fetch", "run"):
            stored = fetch_metadata(
                conn,
                client,
                batch_size=args.batch_size,
                limit=args.limit,
                # Bij `run` alleen ophalen wat deze query zelf aanbracht — anders
                # trekt een kleine dry-run de hele bestaande werklijst leeg.
                from_query=args.from_query or (args.query if args.command == "run" else None),
            )
            log.info("Fetch klaar — %d metadata-records opgeslagen", stored)

        print_stats(conn)

    return 0


if __name__ == "__main__":
    sys.exit(main())
