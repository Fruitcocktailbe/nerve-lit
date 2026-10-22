"""Zoeklaag — gedeeld door de CLI en de MCP-server.

Twee trappen (PLAN.md fase 3): kNN haalt breed op (top-50), de reranker snijdt
terug naar wat echt antwoordt (top-8). Zonder reranker werkt het ook, alleen
minder scherp — daarom is die dependency optioneel.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import store
from .embed import Embedder

log = logging.getLogger(__name__)

RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
# Ontsnappingsluik: reranken op CPU duurt ~45 s per zoekopdracht. Bruikbaar om
# offline resultaten te vergelijken, niet om JARVIS op te laten wachten.
RERANK_ON_CPU = os.getenv("RERANK_ON_CPU", "").strip() in ("1", "true", "yes")


@dataclass
class Hit:
    pmid: str
    title: str
    journal: str
    year: int
    doi: str
    text: str
    source: str
    page_start: int
    page_end: int
    score: float

    def citation(self) -> str:
        parts = [self.title or "(zonder titel)"]
        if self.journal:
            parts.append(self.journal)
        if self.year:
            parts.append(str(self.year))
        reference = ", ".join(parts)
        locator = f"PMID {self.pmid}"
        if self.doi:
            locator += f" · doi:{self.doi}"
        if self.source == "pdf" and self.page_start:
            locator += f" · p.{self.page_start}"
        elif self.source == "abstract":
            locator += " · abstract"
        return f"{reference} [{locator}]"


@lru_cache(maxsize=4)
def _open_table(lancedb_path: str):
    """Tabelhandle vasthouden tussen zoekopdrachten.

    De eerste kNN-zoekopdracht kost ~15 s omdat LanceDB de data van schijf moet
    inlezen; daarna is het ~0,1 s. Opnieuw openen per vraag gooit die opwarming
    telkens weg — funest als JARVIS er via spraak op staat te wachten.
    """
    return store.open_table(Path(lancedb_path))


@lru_cache(maxsize=1)
def _get_embedder() -> Embedder:
    """Eén HTTP-client naar Ollama hergebruiken in plaats van er per vraag een op
    te zetten."""
    return Embedder()


@lru_cache(maxsize=1)
def _load_reranker():
    """Lazy: het model is ~2 GB en mag de eerste import niet blokkeren.

    Op de GPU kost reranken van 50 kandidaten ~1 s, op CPU ~45 s. Dat laatste is
    onbruikbaar als JARVIS er via spraak op wacht, dus zonder CUDA slaan we
    reranking over tenzij er expliciet om gevraagd wordt.
    """
    try:
        import torch
        from sentence_transformers import CrossEncoder
    except ImportError:
        log.info("sentence-transformers niet geinstalleerd — reranking overgeslagen")
        return None

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu" and not RERANK_ON_CPU:
        log.info(
            "Geen CUDA beschikbaar (torch %s) — reranking overgeslagen. "
            "Zet RERANK_ON_CPU=1 om het toch te doen, of installeer een CUDA-build.",
            torch.__version__,
        )
        return None
    try:
        return CrossEncoder(RERANK_MODEL, max_length=512, device=device)
    except Exception as exc:  # noqa: BLE001
        log.warning("Reranker laden faalde (%s) — verder zonder", exc)
        return None


def _build_filter(year_from: int | None, year_to: int | None, source: str | None) -> str | None:
    clauses: list[str] = []
    if year_from:
        clauses.append(f"year >= {int(year_from)}")
    if year_to:
        clauses.append(f"year <= {int(year_to)}")
    if source == "fulltext":
        # 'jats' en 'pdf' zijn allebei volledige artikels; 'abstract' niet.
        clauses.append("source != 'abstract'")
    elif source in ("jats", "pdf", "abstract"):
        clauses.append(f"source = '{source}'")
    return " AND ".join(clauses) if clauses else None


def search(
    lancedb_path: Path,
    query: str,
    k: int = 8,
    candidates: int = 50,
    year_from: int | None = None,
    year_to: int | None = None,
    source: str | None = None,
    rerank: bool = True,
) -> list[Hit]:
    table = _open_table(str(lancedb_path))
    if table.count_rows() == 0:
        return []

    vector = _get_embedder().embed_one(query)

    raw = store.search(
        table, vector, k=max(candidates, k), where=_build_filter(year_from, year_to, source)
    )
    if not raw:
        return []

    hits = [
        Hit(
            pmid=row["pmid"],
            title=row.get("title", ""),
            journal=row.get("journal", ""),
            year=int(row.get("year") or 0),
            doi=row.get("doi", ""),
            text=row["text"],
            source=row.get("source", ""),
            page_start=int(row.get("page_start") or 0),
            page_end=int(row.get("page_end") or 0),
            # LanceDB geeft cosine *distance* terug; kleiner is beter.
            score=1.0 - float(row.get("_distance", 0.0)),
        )
        for row in raw
    ]

    if rerank:
        reranker = _load_reranker()
        if reranker is not None:
            scores = reranker.predict([(query, hit.text) for hit in hits])
            for hit, score in zip(hits, scores, strict=True):
                hit.score = float(score)
            hits.sort(key=lambda hit: hit.score, reverse=True)

    # Max twee chunks per artikel: anders vult één lang review de hele top-8.
    per_pmid: dict[str, int] = {}
    deduped: list[Hit] = []
    for hit in hits:
        seen = per_pmid.get(hit.pmid, 0)
        if seen >= 2:
            continue
        per_pmid[hit.pmid] = seen + 1
        deduped.append(hit)
        if len(deduped) >= k:
            break

    return deduped


def main(argv: list[str] | None = None) -> int:
    """CLI om de index te bevragen zonder JARVIS ertussen:

        uv run python -m index.search "supercharged end-to-side AIN naar ulnaris"
    """
    import argparse
    import textwrap

    from harvester.config import load_config

    parser = argparse.ArgumentParser(prog="index.search")
    parser.add_argument("query")
    parser.add_argument("-k", type=int, default=8)
    parser.add_argument("--year-from", type=int, default=None)
    parser.add_argument("--year-to", type=int, default=None)
    parser.add_argument("--source", choices=["fulltext", "jats", "pdf", "abstract"], default=None)
    parser.add_argument("--no-rerank", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    hits = search(
        load_config().lancedb_path,
        query=args.query,
        k=args.k,
        year_from=args.year_from,
        year_to=args.year_to,
        source=args.source,
        rerank=not args.no_rerank,
    )
    if not hits:
        print("Geen resultaten — is de index al gebouwd?")
        return 1

    for position, hit in enumerate(hits, start=1):
        print(f"\n[{position}] score {hit.score:.3f}  ({hit.source})")
        print(f"    {hit.citation()}")
        print(textwrap.indent(textwrap.fill(hit.text[:500], 72), "    "))
    print()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
