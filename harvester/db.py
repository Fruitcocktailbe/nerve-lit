"""SQLite-store voor metadata + pipeline-status.

Eén DB die alle fases dragen: harvest schrijft metadata, fase 2 zet fetch_status
en pdf_path, fase 3 zet index_status. Alles idempotent — een tweede run over
dezelfde PMID's overschrijft metadata maar raakt de status-kolommen niet aan.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    pmid            TEXT PRIMARY KEY,
    doi             TEXT,
    pmcid           TEXT,
    title           TEXT,
    abstract        TEXT,
    journal         TEXT,
    journal_iso     TEXT,
    year            INTEGER,
    pub_date        TEXT,
    authors_json    TEXT NOT NULL DEFAULT '[]',
    mesh_json       TEXT NOT NULL DEFAULT '[]',
    keywords_json   TEXT NOT NULL DEFAULT '[]',
    pubtypes_json   TEXT NOT NULL DEFAULT '[]',
    language        TEXT,
    -- pipeline-status, niet aangeraakt door de harvester bij her-harvest
    fetch_status    TEXT NOT NULL DEFAULT 'pending',
    fetch_source    TEXT,
    fetch_error     TEXT,
    -- Full-text kan XML (PMC JATS) of PDF (Unpaywall) zijn — vandaar het formaat erbij.
    fulltext_path   TEXT,
    fulltext_format TEXT,
    index_status    TEXT NOT NULL DEFAULT 'pending',
    harvested_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_articles_fetch  ON articles(fetch_status);
CREATE INDEX IF NOT EXISTS idx_articles_index  ON articles(index_status);
CREATE INDEX IF NOT EXISTS idx_articles_year   ON articles(year);
CREATE INDEX IF NOT EXISTS idx_articles_pmcid  ON articles(pmcid);
CREATE INDEX IF NOT EXISTS idx_articles_doi    ON articles(doi);

-- Werklijst: PMID's die esearch opleverde maar die nog geen efetch kregen.
CREATE TABLE IF NOT EXISTS pmid_queue (
    pmid        TEXT PRIMARY KEY,
    query_name  TEXT NOT NULL,
    found_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Datumvensters die volledig binnengehaald zijn — maakt esearch resumebaar
-- zonder de vluchtige WebEnv-history van NCBI.
CREATE TABLE IF NOT EXISTS search_windows (
    query_hash  TEXT NOT NULL,
    mindate     TEXT NOT NULL,
    maxdate     TEXT NOT NULL,
    hit_count   INTEGER NOT NULL,
    completed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (query_hash, mindate, maxdate)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    query_name  TEXT,
    query_hash  TEXT,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    notes       TEXT
);
"""

ARTICLE_COLUMNS = (
    "pmid", "doi", "pmcid", "title", "abstract", "journal", "journal_iso",
    "year", "pub_date", "authors_json", "mesh_json", "keywords_json",
    "pubtypes_json", "language",
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Schema-drift wegwerken op bestaande DB's zonder de 41k metadata-records
    opnieuw te moeten harvesten."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
    if not columns:
        return
    if "pdf_path" in columns and "fulltext_path" not in columns:
        # De cascade levert nu ook JATS-XML op, niet alleen PDF's.
        conn.execute("ALTER TABLE articles RENAME COLUMN pdf_path TO fulltext_path")
        columns.add("fulltext_path")
    if "fulltext_format" not in columns:
        conn.execute("ALTER TABLE articles ADD COLUMN fulltext_format TEXT")
    conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(db_path: Path):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- queue ---------------------------------------------------------------

def enqueue_pmids(conn: sqlite3.Connection, pmids: Iterable[str], query_name: str) -> int:
    """Voegt PMID's toe aan de werklijst. Geeft terug hoeveel er nieuw waren."""
    rows = [(pmid, query_name) for pmid in pmids]
    if not rows:
        return 0
    before = conn.execute("SELECT COUNT(*) FROM pmid_queue").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO pmid_queue (pmid, query_name) VALUES (?, ?)", rows
    )
    after = conn.execute("SELECT COUNT(*) FROM pmid_queue").fetchone()[0]
    return after - before


def pending_pmids(
    conn: sqlite3.Connection, limit: int | None = None, query_name: str | None = None
) -> list[str]:
    """PMID's in de werklijst waarvoor nog geen metadata bestaat."""
    sql = """
        SELECT q.pmid FROM pmid_queue q
        LEFT JOIN articles a ON a.pmid = q.pmid
        WHERE a.pmid IS NULL
    """
    params: list[Any] = []
    if query_name is not None:
        sql += " AND q.query_name = ?"
        params.append(query_name)
    sql += " ORDER BY q.found_at, q.pmid"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [row[0] for row in conn.execute(sql, tuple(params))]


# --- search windows ------------------------------------------------------

def window_done(conn: sqlite3.Connection, query_hash: str, mindate: str, maxdate: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM search_windows WHERE query_hash=? AND mindate=? AND maxdate=?",
        (query_hash, mindate, maxdate),
    ).fetchone()
    return row is not None


def mark_window_done(
    conn: sqlite3.Connection, query_hash: str, mindate: str, maxdate: str, hit_count: int
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO search_windows (query_hash, mindate, maxdate, hit_count) "
        "VALUES (?, ?, ?, ?)",
        (query_hash, mindate, maxdate, hit_count),
    )
    conn.commit()


# --- articles ------------------------------------------------------------

def upsert_articles(conn: sqlite3.Connection, articles: Sequence[dict[str, Any]]) -> int:
    """Schrijft metadata weg. Status-kolommen blijven ongemoeid bij een update."""
    if not articles:
        return 0
    rows = []
    for art in articles:
        rows.append(
            (
                art["pmid"],
                art.get("doi"),
                art.get("pmcid"),
                art.get("title"),
                art.get("abstract"),
                art.get("journal"),
                art.get("journal_iso"),
                art.get("year"),
                art.get("pub_date"),
                json.dumps(art.get("authors", []), ensure_ascii=False),
                json.dumps(art.get("mesh", []), ensure_ascii=False),
                json.dumps(art.get("keywords", []), ensure_ascii=False),
                json.dumps(art.get("pubtypes", []), ensure_ascii=False),
                art.get("language"),
            )
        )
    placeholders = ", ".join("?" * len(ARTICLE_COLUMNS))
    updates = ", ".join(f"{col}=excluded.{col}" for col in ARTICLE_COLUMNS if col != "pmid")
    conn.executemany(
        f"INSERT INTO articles ({', '.join(ARTICLE_COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(pmid) DO UPDATE SET {updates}, updated_at=datetime('now')",
        rows,
    )
    conn.commit()
    return len(rows)


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def scalar(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return {
        "queued": scalar("SELECT COUNT(*) FROM pmid_queue"),
        "articles": scalar("SELECT COUNT(*) FROM articles"),
        "pending_fetch": scalar("SELECT COUNT(*) FROM articles WHERE fetch_status='pending'"),
        "with_pmcid": scalar("SELECT COUNT(*) FROM articles WHERE pmcid IS NOT NULL"),
        "with_doi": scalar("SELECT COUNT(*) FROM articles WHERE doi IS NOT NULL"),
        "with_abstract": scalar(
            "SELECT COUNT(*) FROM articles WHERE abstract IS NOT NULL AND abstract != ''"
        ),
        "windows_done": scalar("SELECT COUNT(*) FROM search_windows"),
        "year_min": scalar("SELECT COALESCE(MIN(year), 0) FROM articles"),
        "year_max": scalar("SELECT COALESCE(MAX(year), 0) FROM articles"),
    }
