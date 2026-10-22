"""Fase 3 — bouw de vectorindex.

    uv run python -m index.build run --limit 50
    uv run python -m index.build run --source pdf
    uv run python -m index.build stats

Per artikel: PDF (of anders het abstract) -> chunks -> bge-m3 -> LanceDB.
Idempotent: artikels met index_status 'indexed' worden overgeslagen, en
herindexeren gooit eerst de oude chunks van die PMID weg.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from harvester import db
from harvester.config import load_config

from . import store
from .embed import Embedder
from .extract import extract_chunks

log = logging.getLogger("index")


def pending_articles(
    conn: sqlite3.Connection,
    limit: int | None,
    source: str,
    reindex: bool,
    retry_failed: bool = False,
) -> list[sqlite3.Row]:
    if reindex:
        conditions = []
    elif retry_failed:
        conditions = ["index_status IN ('pending', 'failed')"]
    else:
        conditions = ["index_status = 'pending'"]
    if source == "fulltext":
        conditions.append("fulltext_path IS NOT NULL")
    elif source == "abstract":
        conditions.append("fulltext_path IS NULL AND abstract IS NOT NULL AND abstract != ''")
    else:
        conditions.append("(fulltext_path IS NOT NULL OR (abstract IS NOT NULL AND abstract != ''))")

    sql = "SELECT * FROM articles"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    # Full-text eerst, dan recent — de waardevolste chunks komen als eerste binnen.
    sql += " ORDER BY (fulltext_path IS NULL), year DESC"
    params: list[object] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, tuple(params)))


def _chunk_rows(row: sqlite3.Row, chunks, source: str, vectors) -> list[dict]:
    return [
        {
            "id": f"{row['pmid']}:{chunk.index}",
            "pmid": row["pmid"],
            "chunk_index": chunk.index,
            "text": chunk.text,
            "source": source,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "title": row["title"] or "",
            "journal": row["journal_iso"] or row["journal"] or "",
            "year": int(row["year"]) if row["year"] else 0,
            "doi": row["doi"] or "",
            "vector": vector,
        }
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]


def index_group(table, embedder: Embedder, group: list[sqlite3.Row]) -> tuple[dict[str, int], int]:
    """Indexeer een groep artikels met één embed-call voor de hele groep.

    Per artikel embedden zou voor de ~34k abstract-only artikels neerkomen op
    één HTTP-round-trip per tekst; gegroepeerd scheelt dat een orde van grootte
    in doorlooptijd.
    """
    statuses: dict[str, int] = {}
    extracted: list[tuple[sqlite3.Row, list, str]] = []
    texts: list[str] = []

    for row in group:
        path = Path(row["fulltext_path"]) if row["fulltext_path"] else None
        try:
            chunks, source = extract_chunks(path, row["abstract"])
        except Exception as exc:  # noqa: BLE001 - kapot bestand mag de groep niet slopen
            log.warning("PMID %s extractie faalde: %s", row["pmid"], exc)
            statuses[row["pmid"]] = "failed"
            continue
        if not chunks:
            statuses[row["pmid"]] = "empty"
            continue
        extracted.append((row, chunks, source))
        texts.extend(chunk.text for chunk in chunks)

    if not texts:
        return statuses, 0

    try:
        vectors = embedder.embed(texts)
        per_article = [None] * len(extracted)
        cursor = 0
        for position, (_, chunks, _) in enumerate(extracted):
            per_article[position] = vectors[cursor : cursor + len(chunks)]
            cursor += len(chunks)
    except Exception as exc:  # noqa: BLE001
        # Terugvallen op artikel-per-artikel: zo verliezen we alleen het artikel
        # dat écht kapot is, niet de hele groep.
        log.warning("Groepsembedding faalde (%s) — per artikel opnieuw", exc)
        per_article = []
        for _, chunks, _ in extracted:
            try:
                per_article.append(embedder.embed([chunk.text for chunk in chunks]))
            except Exception as inner:  # noqa: BLE001
                log.warning("Artikel overgeslagen: %s", inner)
                per_article.append(None)

    rows_to_add: list[dict] = []
    for (row, chunks, source), vecs in zip(extracted, per_article, strict=True):
        if vecs is None:
            statuses[row["pmid"]] = "failed"
            continue
        rows_to_add.extend(_chunk_rows(row, chunks, source, vecs))
        statuses[row["pmid"]] = "indexed"

    # Oude chunks weg vóór de nieuwe erin gaan — anders verdubbelt herindexeren.
    for row, _, _ in extracted:
        if statuses.get(row["pmid"]) == "indexed":
            store.delete_pmid(table, row["pmid"])
    store.add_rows(table, rows_to_add)
    return statuses, len(rows_to_add)


def run(
    conn: sqlite3.Connection,
    lancedb_path: Path,
    limit: int | None,
    source: str,
    reindex: bool,
    group_size: int = 32,
    retry_failed: bool = False,
) -> dict[str, int]:
    rows = pending_articles(conn, limit, source, reindex, retry_failed)
    if not rows:
        log.info("Niets te indexeren")
        return {}

    log.info("%d artikels te indexeren", len(rows))
    table = store.open_table(lancedb_path)
    tally: dict[str, int] = {"indexed": 0, "empty": 0, "failed": 0, "chunks": 0}

    with Embedder() as embedder:
        embedder.health_check()
        for offset in range(0, len(rows), group_size):
            group = rows[offset : offset + group_size]
            try:
                statuses, chunk_count = index_group(table, embedder, group)
            except Exception as exc:  # noqa: BLE001 - groep mag de run niet stoppen
                log.warning("Groep vanaf %d faalde: %s", offset, exc)
                statuses = {row["pmid"]: "failed" for row in group}
                chunk_count = 0

            conn.executemany(
                "UPDATE articles SET index_status=?, updated_at=datetime('now') WHERE pmid=?",
                [(status, pmid) for pmid, status in statuses.items()],
            )
            conn.commit()
            for status in statuses.values():
                tally[status] = tally.get(status, 0) + 1
            tally["chunks"] += chunk_count
            log.info("%d/%d — %s", min(offset + group_size, len(rows)), len(rows), tally)

    return tally


def print_stats(conn: sqlite3.Connection, lancedb_path: Path) -> None:
    print("\n  Index-status in de metadata-store")
    print("  " + "-" * 40)
    for status, count in conn.execute(
        "SELECT index_status, COUNT(*) FROM articles GROUP BY index_status ORDER BY 2 DESC"
    ):
        print(f"  {status.ljust(14)} {count:>8,}")

    table = store.open_table(lancedb_path)
    total = table.count_rows()
    print(f"\n  LanceDB: {total:,} chunks in '{store.TABLE_NAME}'\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="index.build", description=__doc__)
    parser.add_argument("command", choices=["run", "stats"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--source", choices=["all", "fulltext", "abstract"], default="all")
    parser.add_argument("--reindex", action="store_true", help="ook al geïndexeerde artikels opnieuw doen")
    parser.add_argument("--group-size", type=int, default=32, help="artikels per embed-batch")
    parser.add_argument("--retry-failed", action="store_true", help="ook status 'failed' opnieuw doen")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    config = load_config()
    config.ensure_dirs()
    with db.session(config.db_path) as conn:
        if args.command == "stats":
            print_stats(conn, config.lancedb_path)
            return 0
        tally = run(
            conn, config.lancedb_path, args.limit, args.source, args.reindex,
            args.group_size, args.retry_failed,
        )
        print(f"\n  Resultaat: {tally}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
