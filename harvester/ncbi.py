"""Rate-limited client voor NCBI E-utilities.

CLAUDE.md regel 2: respecteer de rate-limits. 3 req/s zonder API-key, 10 met.
De limiter is bewust conservatief (90% van het plafond) — NCBI blokkeert IP's
die eroverheen gaan, en een geblokkeerd IP kost meer dan een trage harvest.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """Simpele sequentiële throttle: minimaal N seconden tussen twee calls."""

    def __init__(self, requests_per_second: float) -> None:
        self._min_interval = 1.0 / requests_per_second
        self._last_call = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()


class NCBIClient:
    def __init__(
        self,
        api_key: str | None = None,
        email: str | None = None,
        tool: str = "nerve-lit",
        timeout: float = 120.0,
        max_retries: int = 5,
    ) -> None:
        self.api_key = api_key
        self.email = email
        self.tool = tool
        self.max_retries = max_retries
        # 90% van het plafond als veiligheidsmarge.
        self.limiter = RateLimiter(9.0 if api_key else 2.7)
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": f"{tool}/0.1 (mailto:{email or 'unknown'}) python-httpx"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> NCBIClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _common_params(self) -> dict[str, str]:
        params: dict[str, str] = {"tool": self.tool}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _request(self, endpoint: str, params: dict[str, Any]) -> httpx.Response:
        payload = {**self._common_params(), **{k: str(v) for k, v in params.items()}}
        url = f"{BASE_URL}/{endpoint}.fcgi"
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            self.limiter.wait()
            try:
                # POST: PMID-batches maken GET-URL's te lang.
                response = self._client.post(url, data=payload)
                if response.status_code in RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request, response=response
                    )
                response.raise_for_status()
                return response
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_error = exc
                backoff = min(2**attempt, 30)
                log.warning(
                    "%s poging %d/%d faalde (%s) — %ds wachten",
                    endpoint,
                    attempt + 1,
                    self.max_retries,
                    exc,
                    backoff,
                )
                time.sleep(backoff)

        raise RuntimeError(f"{endpoint} faalde na {self.max_retries} pogingen") from last_error

    @staticmethod
    def _date_params(mindate: str | None, maxdate: str | None) -> dict[str, str]:
        if not (mindate and maxdate):
            return {}
        # EDAT = de datum waarop PubMed het record kreeg. Elk record heeft er een,
        # in tegenstelling tot publicatiedatum (die kan ontbreken of vaag zijn).
        return {"datetype": "edat", "mindate": mindate, "maxdate": maxdate}

    def esearch_count(
        self, term: str, mindate: str | None = None, maxdate: str | None = None
    ) -> int:
        """Alleen het aantal treffers — goedkoop, voor sanity checks en partitionering."""
        response = self._request(
            "esearch",
            {
                "db": "pubmed",
                "term": term,
                "retmax": 0,
                "retmode": "json",
                **self._date_params(mindate, maxdate),
            },
        )
        return int(response.json()["esearchresult"]["count"])

    def esearch_pmids(
        self,
        term: str,
        retstart: int = 0,
        retmax: int = 5000,
        mindate: str | None = None,
        maxdate: str | None = None,
    ) -> tuple[int, list[str]]:
        """Eén pagina PMID's. Geeft (totaal aantal treffers, pmids) terug.

        Let op: PubMed weigert retstart + retmax > 10.000 op één zoekopdracht.
        Grotere sets moet je opdelen in datumvensters — zie harvest.collect_pmids.
        """
        response = self._request(
            "esearch",
            {
                "db": "pubmed",
                "term": term,
                "retstart": retstart,
                "retmax": retmax,
                "retmode": "json",
                "sort": "pub_date",
                **self._date_params(mindate, maxdate),
            },
        )
        result = response.json()["esearchresult"]
        return int(result["count"]), list(result.get("idlist", []))

    def efetch_pmc_fulltext(self, pmcid: str) -> bytes:
        """JATS-XML van één PMC-artikel.

        NCBI serveert de <body> alleen voor artikels waarvoor het de rechten
        heeft; bij de rest komt enkel front matter terug. Die grens wordt dus
        server-side afgedwongen — precies wat CLAUDE.md regel 1 vraagt.
        """
        response = self._request(
            "efetch", {"db": "pmc", "id": pmcid.replace("PMC", ""), "retmode": "xml"}
        )
        return response.content

    def efetch_pubmed(self, pmids: list[str]) -> bytes:
        """Volledige PubMed-XML voor een batch PMID's (max ~200 per call)."""
        response = self._request(
            "efetch", {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"}
        )
        return response.content
