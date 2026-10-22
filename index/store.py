"""LanceDB-store voor de chunkvectoren."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa

from .embed import EMBED_DIM

TABLE_NAME = "chunks"

SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),  # "<pmid>:<chunk_index>"
        pa.field("pmid", pa.string()),
        pa.field("chunk_index", pa.int32()),
        pa.field("text", pa.string()),
        pa.field("source", pa.string()),  # 'pdf' of 'abstract'
        pa.field("page_start", pa.int32()),
        pa.field("page_end", pa.int32()),
        pa.field("title", pa.string()),
        pa.field("journal", pa.string()),
        pa.field("year", pa.int32()),
        pa.field("doi", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
    ]
)


def open_table(lancedb_path: Path):
    lancedb_path.parent.mkdir(parents=True, exist_ok=True)
    database = lancedb.connect(str(lancedb_path))
    if TABLE_NAME in database.table_names():
        return database.open_table(TABLE_NAME)
    return database.create_table(TABLE_NAME, schema=SCHEMA)


def delete_pmid(table, pmid: str) -> None:
    """Oude chunks weg voor we nieuwe schrijven — houdt herindexeren idempotent."""
    if not pmid.isdigit():
        # PMID's zijn altijd numeriek; de filter gaat als string de query in.
        raise ValueError(f"Ongeldige PMID: {pmid!r}")
    table.delete(f"pmid = '{pmid}'")


def add_rows(table, rows: list[dict[str, Any]]) -> None:
    if rows:
        table.add(rows)


def search(table, vector: list[float], k: int = 50, where: str | None = None) -> list[dict[str, Any]]:
    query = table.search(vector).metric("cosine").limit(k)
    if where:
        query = query.where(where, prefilter=True)
    return query.to_list()
