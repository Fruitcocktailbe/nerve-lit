"""Inspectie van de metadata-store — kwaliteitscontrole na een harvest.

    uv run python -m harvester.report sample --limit 3
    uv run python -m harvester.report coverage
    uv run python -m harvester.report export            # per-artikel status-CSV
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import textwrap
from pathlib import Path

from . import db
from .config import Config, load_config

FIELDS = ("doi", "pmcid", "title", "abstract", "journal", "journal_iso", "year", "language")
JSON_FIELDS = ("authors_json", "mesh_json", "keywords_json", "pubtypes_json")


def show_sample(conn: sqlite3.Connection, limit: int, order: str) -> None:
    order_sql = {"newest": "year DESC", "oldest": "year ASC", "random": "RANDOM()"}[order]
    rows = conn.execute(
        f"SELECT * FROM articles ORDER BY {order_sql} LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        print("Geen artikels in de store — draai eerst `harvester.harvest run`.")
        return

    for row in rows:
        print("=" * 78)
        print(f"PMID {row['pmid']}   DOI {row['doi']}   PMC {row['pmcid']}")
        print(f"{row['year']} ({row['pub_date']})  {row['journal_iso']} | {row['journal']}")
        print(f"TITEL    {row['title']}")
        authors = json.loads(row["authors_json"])
        shown = ", ".join(authors[:5]) + (f" ... (+{len(authors) - 5})" if len(authors) > 5 else "")
        print(f"AUTEURS  {shown or '(geen)'}")
        print(f"TYPE     {json.loads(row['pubtypes_json'])}   TAAL {row['language']}")
        print(f"MESH     {json.loads(row['mesh_json'])[:6]}")
        print(f"KEYWORDS {json.loads(row['keywords_json'])[:6]}")
        abstract = row["abstract"]
        print(f"ABSTRACT ({len(abstract) if abstract else 0} tekens)")
        print(textwrap.indent(textwrap.fill((abstract or "(geen)")[:700], 74), "  "))
        print()


def show_coverage(conn: sqlite3.Connection) -> None:
    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    if total == 0:
        print("Geen artikels in de store.")
        return

    print(f"\n  Veldvulling over {total:,} artikels")
    print("  " + "-" * 40)
    for field in FIELDS:
        filled = conn.execute(
            f"SELECT COUNT(*) FROM articles WHERE {field} IS NOT NULL AND {field} != ''"
        ).fetchone()[0]
        print(f"  {field.ljust(14)} {filled:>7,}  {filled / total:>6.1%}")
    for field in JSON_FIELDS:
        filled = conn.execute(
            f"SELECT COUNT(*) FROM articles WHERE {field} NOT IN ('[]', '')"
        ).fetchone()[0]
        print(f"  {field[:-5].ljust(14)} {filled:>7,}  {filled / total:>6.1%}")

    print("\n  Fetch-status")
    print("  " + "-" * 40)
    for status, count in conn.execute(
        "SELECT fetch_status, COUNT(*) FROM articles GROUP BY fetch_status ORDER BY 2 DESC"
    ):
        print(f"  {status.ljust(14)} {count:>7,}")

    print("\n  Per decennium")
    print("  " + "-" * 40)
    for decade, count in conn.execute(
        "SELECT (year/10)*10 AS decade, COUNT(*) FROM articles "
        "WHERE year IS NOT NULL GROUP BY decade ORDER BY decade"
    ):
        bar = "#" * min(40, max(1, round(count / total * 120)))
        print(f"  {decade}s {count:>7,}  {bar}")
    print()


# Kolommen van de status-CSV. Eén rij per artikel — bedoeld om in Excel te
# filteren/sorteren op waar elk artikel in de pijplijn zit.
EXPORT_COLUMNS = (
    "pmid", "year", "journal_iso", "title", "doi", "pmcid",
    "has_abstract", "fetch_status", "fetch_source", "fulltext_format",
    "index_status", "searchable_on", "fulltext_path", "pubmed_url", "doi_url",
)


def _searchable_on(row: sqlite3.Row) -> str:
    """Waarop dit artikel nú doorzoekbaar is: niets, het abstract, of de full-text."""
    if row["index_status"] != "indexed":
        return ""
    return "fulltext" if row["fulltext_path"] else "abstract"


def export_status(conn: sqlite3.Connection, config: Config, destination: Path | None) -> Path:
    """Volledige per-artikel statuslijst naar CSV.

    utf-8-sig + puntkomma zodat Excel (NL) de accenten en kolommen correct toont.
    Snapshot: opnieuw draaien geeft de actuele stand tijdens een lopende fetch/index.
    """
    destination = destination or (config.data_dir / "status.csv")
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        "SELECT pmid, year, journal_iso, title, doi, pmcid, abstract, "
        "fetch_status, fetch_source, fulltext_format, fulltext_path, index_status "
        "FROM articles ORDER BY year DESC, pmid"
    ).fetchall()

    with destination.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "pmid": row["pmid"],
                "year": row["year"] or "",
                "journal_iso": row["journal_iso"] or "",
                "title": row["title"] or "",
                "doi": row["doi"] or "",
                "pmcid": row["pmcid"] or "",
                "has_abstract": "yes" if (row["abstract"] or "").strip() else "no",
                "fetch_status": row["fetch_status"],
                "fetch_source": row["fetch_source"] or "",
                "fulltext_format": row["fulltext_format"] or "",
                "index_status": row["index_status"],
                "searchable_on": _searchable_on(row),
                "fulltext_path": row["fulltext_path"] or "",
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{row['pmid']}/",
                "doi_url": f"https://doi.org/{row['doi']}" if row["doi"] else "",
            })
    print(f"\n  {len(rows):,} artikels weggeschreven naar {destination}\n")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harvester.report", description=__doc__)
    parser.add_argument("command", choices=["sample", "coverage", "export"])
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--order", choices=["newest", "oldest", "random"], default="newest")
    parser.add_argument("--out", type=Path, default=None, help="pad voor de export-CSV")
    args = parser.parse_args(argv)

    config = load_config()
    config.ensure_dirs()
    with db.session(config.db_path) as conn:
        if args.command == "sample":
            show_sample(conn, args.limit, args.order)
        elif args.command == "export":
            export_status(conn, config, args.out)
        else:
            show_coverage(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
