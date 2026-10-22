"""Fase 4 — MCP-server `nerve-lit-search` voor JARVIS.

Draait over stdio: JARVIS start dit proces, praat MCP, krijgt chunks + citaten
terug. Geen netwerkservice, geen poort — Tailscale dekt de toegang van buitenaf
al af omdat JARVIS zelf op deze machine draait.

    uv run python mcp-server/server.py          # handmatig starten (stdio)

Registratie in de JARVIS voice-agent-config: zie README.md.

LET OP — de toolnamen beginnen bewust met een leeswerkwoord (`search_`, `get_`).
De Trust Ledger van JARVIS (`voice-agent/backend/src/agent.ts`, READ_VERB) leidt
uit de naam af of een MCP-tool alleen leest. Met `JARVIS_EXPOSED=1` moet elke
tool die níét als read herkend wordt eerst handmatig bevestigd worden — dan zou
Jeroen elke literatuurvraag met de hand moeten goedkeuren. Hernoem deze tools dus
niet naar iets als `nerve_lit_search`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
from pathlib import Path

# De repo-root moet importeerbaar zijn: dit bestand ligt in een submap.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from harvester import db  # noqa: E402
from harvester.config import load_config  # noqa: E402
from index import search as search_module  # noqa: E402

# Logging naar stderr: stdout is het MCP-protocolkanaal en mag niets anders bevatten.
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("nerve-lit-mcp")

CONFIG = load_config()
mcp = FastMCP("nerve-lit-search")


def _warm_up() -> None:
    """Reranker in GPU-geheugen en LanceDB van schijf trekken bij het opstarten.

    Koud kost de eerste zoekopdracht ~30 s, daarna ~1,8 s. In een achtergrond-
    thread betekent dat: de MCP-handshake wordt niet opgehouden, en tegen de tijd
    dat Jeroen zijn eerste vraag stelt staat alles klaar.
    """
    try:
        search_module.search(CONFIG.lancedb_path, query="nerve transfer", k=1)
        log.info("Opgewarmd — zoekopdrachten draaien nu op volle snelheid")
    except Exception as exc:  # noqa: BLE001 - opwarmen mag de server nooit slopen
        log.warning("Opwarmen faalde (%s) — de eerste zoekopdracht wordt trager", exc)


threading.Thread(target=_warm_up, daemon=True, name="nerve-lit-warmup").start()


@mcp.tool()
def search_nerve_lit(
    query: str,
    k: int = 8,
    year_from: int | None = None,
    year_to: int | None = None,
    full_text_only: bool = False,
) -> str:
    """Doorzoek de peripheral-nerve-surgery literatuurdatabank semantisch.

    Args:
        query: de vraag in natuurlijke taal, bv. "supercharged end-to-side AIN naar ulnaris".
        k: aantal passages om terug te geven (default 8).
        year_from: enkel artikels vanaf dit publicatiejaar.
        year_to: enkel artikels tot en met dit publicatiejaar.
        full_text_only: alleen passages uit volledige artikels, niet uit abstracts.

    Returns:
        De relevantste passages met hun citaat (titel, tijdschrift, jaar, PMID, DOI).
    """
    hits = search_module.search(
        CONFIG.lancedb_path,
        query=query,
        k=k,
        year_from=year_from,
        year_to=year_to,
        source="fulltext" if full_text_only else None,
    )
    if not hits:
        return "Geen resultaten. Is de index al gebouwd (`python -m index.build run`)?"

    blocks = [
        f"[{position}] {hit.citation()}\n{hit.text}"
        for position, hit in enumerate(hits, start=1)
    ]
    return (
        f"{len(hits)} passages voor: {query}\n\n"
        + "\n\n---\n\n".join(blocks)
        + "\n\nCiteer altijd met PMID of DOI bij het samenvatten."
    )


@mcp.tool()
def get_nerve_lit_article(pmid: str) -> str:
    """Haal de volledige metadata van één artikel op via zijn PMID."""
    with db.session(CONFIG.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM articles WHERE pmid = ?", (pmid,)).fetchone()

    if row is None:
        return f"PMID {pmid} zit niet in de databank."

    authors = json.loads(row["authors_json"] or "[]")
    mesh = json.loads(row["mesh_json"] or "[]")
    lines = [
        f"PMID {row['pmid']} — {row['title']}",
        f"Auteurs: {', '.join(authors) or 'onbekend'}",
        f"Bron: {row['journal'] or ''} ({row['year'] or '?'})",
        f"DOI: {row['doi'] or 'geen'}   PMC: {row['pmcid'] or 'geen'}",
        f"Full-text: {row['fetch_status']}"
        + (f" ({row['fulltext_format']}) — {row['fulltext_path']}" if row["fulltext_path"] else ""),
        f"MeSH: {', '.join(mesh[:25]) or 'geen'}",
        "",
        row["abstract"] or "(geen abstract)",
    ]
    return "\n".join(lines)


@mcp.tool()
def get_nerve_lit_stats() -> str:
    """Toon de omvang en toestand van de literatuurdatabank."""
    with db.session(CONFIG.db_path) as conn:
        values = db.stats(conn)
        by_fetch = dict(
            conn.execute("SELECT fetch_status, COUNT(*) FROM articles GROUP BY fetch_status")
        )
        by_index = dict(
            conn.execute("SELECT index_status, COUNT(*) FROM articles GROUP BY index_status")
        )

    try:
        from index import store

        chunks = store.open_table(CONFIG.lancedb_path).count_rows()
    except Exception as exc:  # noqa: BLE001
        chunks = f"onbekend ({exc})"

    return (
        f"nerve-lit databank\n"
        f"  artikels met metadata : {values['articles']:,}\n"
        f"  in de werklijst       : {values['queued']:,}\n"
        f"  jaren                 : {values['year_min']}–{values['year_max']}\n"
        f"  full-text status      : {by_fetch}\n"
        f"  index status          : {by_index}\n"
        f"  chunks in LanceDB     : {chunks}"
    )


if __name__ == "__main__":
    mcp.run()
