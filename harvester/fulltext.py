"""Fase 2 — full-text ophalen via legale open-access-routes.

    uv run python -m harvester.fulltext fetch --limit 20
    uv run python -m harvester.fulltext export

Cascade per artikel, stop bij het eerste succes:
  1. PMC full-text (JATS-XML via efetch db=pmc) — alleen wat NCBI mag serveren
  2. Unpaywall (beste OA-PDF-locatie op DOI)
  3. geen OA -> status 'needs-institutional', GEEN download

Waarom XML vóór PDF: PMC geeft gestructureerde JATS terug met echte
sectietitels. Dat chunkt beter dan tekst die uit een tweekolomsPDF gepeuterd is,
en het scheelt de OCR-rommel.

CLAUDE.md regel 1: nooit piraterij-mirrors. Wat niet via 1 of 2 binnenkomt,
belandt in needs-institutional.csv voor Jeroens UZB-toegang.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import db
from .config import Config, load_config
from .ncbi import NCBIClient

log = logging.getLogger("fulltext")

UNPAYWALL_URL = "https://api.unpaywall.org/v2"
PDF_MAGIC = b"%PDF"

STATUS_PMC = "oa_pmc"
STATUS_UNPAYWALL = "oa_unpaywall"
STATUS_INSTITUTIONAL = "needs-institutional"
STATUS_FAILED = "failed"


@dataclass
class FetchResult:
    status: str
    source: str | None = None
    path: str | None = None
    fmt: str | None = None
    error: str | None = None


def _safe_filename(text: str, max_length: int = 24) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-")
    return cleaned[:max_length] or "unknown"


def target_path(root: Path, row: sqlite3.Row, suffix: str) -> Path:
    """Full-text per jaar in submappen — houdt de Drive-map bruikbaar voor een
    mens en vermijdt 40k bestanden in één folder."""
    year = str(row["year"] or "unknown")
    authors = json.loads(row["authors_json"] or "[]")
    first_author = _safe_filename(authors[0].split()[0] if authors else "")
    return root / year / f"{row['pmid']}_{first_author}{suffix}"


def jats_has_body(xml_bytes: bytes) -> bool:
    """Heeft deze PMC-respons echte full-text, of alleen front matter?"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return False
    body = root.find(".//body")
    if body is None:
        return False
    return len("".join(body.itertext()).split()) > 100


class FullTextFetcher:
    def __init__(self, config: Config, ncbi: NCBIClient, timeout: float = 120.0) -> None:
        self.config = config
        self.ncbi = ncbi
        self.root = config.require_pdf_dir()
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    f"nerve-lit/0.1 (mailto:{config.unpaywall_email or 'unknown'}) python-httpx"
                )
            },
        )
        self._last_call = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FullTextFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self, min_interval: float = 0.4) -> None:
        """Unpaywall en uitgeversservers hebben geen harde limiet, maar netjes
        blijven is CLAUDE.md regel 2."""
        elapsed = time.monotonic() - self._last_call
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_call = time.monotonic()

    # --- stap 1: PMC full-text XML --------------------------------------

    def _try_pmc(self, row: sqlite3.Row) -> FetchResult | None:
        xml_bytes = self.ncbi.efetch_pmc_fulltext(row["pmcid"])
        if not jats_has_body(xml_bytes):
            return None
        destination = target_path(self.root, row, ".xml")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(xml_bytes)
        return FetchResult(STATUS_PMC, f"pmc:{row['pmcid']}", str(destination), "jats")

    # --- stap 2: Unpaywall ----------------------------------------------

    def _unpaywall_pdf_url(self, doi: str) -> str | None:
        if not self.config.unpaywall_email:
            raise RuntimeError("UNPAYWALL_EMAIL ontbreekt in .env — Unpaywall vereist die param")
        self._throttle()
        response = self._client.get(
            f"{UNPAYWALL_URL}/{doi}", params={"email": self.config.unpaywall_email}
        )
        if response.status_code in (404, 422):
            return None
        response.raise_for_status()
        data = response.json()

        best = data.get("best_oa_location") or {}
        if best.get("url_for_pdf"):
            return best["url_for_pdf"]
        for location in data.get("oa_locations") or []:
            if location.get("url_for_pdf"):
                return location["url_for_pdf"]
        return None

    def _try_unpaywall(self, row: sqlite3.Row) -> FetchResult | None:
        url = self._unpaywall_pdf_url(row["doi"])
        if not url:
            return None
        destination = target_path(self.root, row, ".pdf")
        if not self._download_pdf(url, destination):
            return None
        return FetchResult(STATUS_UNPAYWALL, url, str(destination), "pdf")

    def _download_pdf(self, url: str, destination: Path) -> bool:
        self._throttle()
        try:
            response = self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.debug("Download faalde %s: %s", url, exc)
            return False
        data = response.content
        # Uitgevers serveren vaak een HTML-landingspagina of cookiewall met
        # status 200. Zonder deze check vult de corpusmap zich met rommel.
        if not data.startswith(PDF_MAGIC):
            log.debug(
                "Geen PDF maar %s (%d bytes) van %s",
                response.headers.get("content-type"), len(data), url,
            )
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return True

    # --- cascade ---------------------------------------------------------

    def fetch_one(self, row: sqlite3.Row) -> FetchResult:
        for suffix, fmt, status in ((".xml", "jats", STATUS_PMC), (".pdf", "pdf", STATUS_UNPAYWALL)):
            existing = target_path(self.root, row, suffix)
            if existing.exists() and existing.stat().st_size > 0:
                return FetchResult(status, "already-present", str(existing), fmt)

        if row["pmcid"]:
            try:
                result = self._try_pmc(row)
                if result:
                    return result
            except (httpx.HTTPError, ET.ParseError, RuntimeError) as exc:
                log.debug("PMC faalde voor %s: %s", row["pmid"], exc)

        if row["doi"]:
            try:
                result = self._try_unpaywall(row)
                if result:
                    return result
            except (httpx.HTTPError, RuntimeError) as exc:
                log.debug("Unpaywall faalde voor %s: %s", row["pmid"], exc)

        return FetchResult(STATUS_INSTITUTIONAL, None, None, None, "geen OA full-text gevonden")


def pending_articles(
    conn: sqlite3.Connection, limit: int | None, retry_failed: bool, retry_institutional: bool
) -> list[sqlite3.Row]:
    statuses = ["pending"]
    if retry_failed:
        statuses.append(STATUS_FAILED)
    if retry_institutional:
        statuses.append(STATUS_INSTITUTIONAL)
    placeholders = ",".join("?" * len(statuses))
    sql = (
        f"SELECT * FROM articles WHERE fetch_status IN ({placeholders}) "
        # PMC-artikels eerst: die route slaagt vaakst en kost één call.
        "ORDER BY (pmcid IS NULL), (doi IS NULL), year DESC"
    )
    params: list[object] = list(statuses)
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, tuple(params)))


def record_result(conn: sqlite3.Connection, pmid: str, result: FetchResult) -> None:
    conn.execute(
        "UPDATE articles SET fetch_status=?, fetch_source=?, fulltext_path=?, "
        "fulltext_format=?, fetch_error=?, updated_at=datetime('now') WHERE pmid=?",
        (result.status, result.source, result.path, result.fmt, result.error, pmid),
    )


def run_fetch(
    conn: sqlite3.Connection,
    config: Config,
    limit: int | None,
    retry_failed: bool,
    retry_institutional: bool,
) -> dict[str, int]:
    rows = pending_articles(conn, limit, retry_failed, retry_institutional)
    if not rows:
        log.info("Niets te doen — geen artikels met status 'pending'")
        return {}

    log.info("%d artikels te proberen", len(rows))
    tally: dict[str, int] = {}
    ncbi = NCBIClient(
        api_key=config.ncbi_api_key, email=config.ncbi_email, tool=config.ncbi_tool
    )
    with ncbi, FullTextFetcher(config, ncbi) as fetcher:
        for position, row in enumerate(rows, start=1):
            try:
                result = fetcher.fetch_one(row)
            except Exception as exc:  # noqa: BLE001 - één artikel mag de run niet stoppen
                log.warning("PMID %s onverwacht gefaald: %s", row["pmid"], exc)
                result = FetchResult(STATUS_FAILED, None, None, None, str(exc)[:500])
            record_result(conn, row["pmid"], result)
            tally[result.status] = tally.get(result.status, 0) + 1
            if position % 25 == 0 or position == len(rows):
                conn.commit()
                log.info("%d/%d verwerkt — %s", position, len(rows), tally)
    conn.commit()
    return tally


def export_worklist(conn: sqlite3.Connection, config: Config, destination: Path | None) -> Path:
    """CSV met alles wat niet via OA binnenkwam, voor batch-ophaling via UZB."""
    destination = destination or (config.data_dir / "needs-institutional.csv")
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        "SELECT pmid, doi, title, journal_iso, year FROM articles "
        "WHERE fetch_status=? ORDER BY year DESC, pmid",
        (STATUS_INSTITUTIONAL,),
    ).fetchall()

    # utf-8-sig: anders toont Excel de accenten in auteurs/titels verkeerd.
    with destination.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["pmid", "doi", "titel", "tijdschrift", "jaar", "pubmed_url", "doi_url"])
        for row in rows:
            writer.writerow([
                row["pmid"],
                row["doi"] or "",
                row["title"] or "",
                row["journal_iso"] or "",
                row["year"] or "",
                f"https://pubmed.ncbi.nlm.nih.gov/{row['pmid']}/",
                f"https://doi.org/{row['doi']}" if row["doi"] else "",
            ])
    log.info("%d artikels weggeschreven naar %s", len(rows), destination)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harvester.fulltext", description=__doc__)
    parser.add_argument("command", choices=["fetch", "export"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--retry-institutional", action="store_true",
        help="ook 'needs-institutional' opnieuw proberen (bv. na een embargo-periode)",
    )
    parser.add_argument("--out", type=Path, default=None, help="pad voor de export-CSV")
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
        if args.command == "fetch":
            tally = run_fetch(
                conn, config, args.limit, args.retry_failed, args.retry_institutional
            )
            print(f"\n  Resultaat: {tally}\n")
        else:
            export_worklist(conn, config, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
