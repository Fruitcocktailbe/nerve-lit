"""Embeddings via de lokale Ollama-server (bge-m3, 1024-dim).

Model niet wisselen zonder alles te herembedden — een index met vectoren van twee
verschillende modellen geeft stil onzin terug in plaats van een foutmelding.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
EMBED_DIM = 1024


class Embedder:
    def __init__(
        self,
        base_url: str = OLLAMA_URL,
        model: str = EMBED_MODEL,
        timeout: float = 300.0,
        batch_size: int = 64,
    ) -> None:
        self.model = model
        self.batch_size = batch_size
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Embedder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def health_check(self) -> None:
        """Faalt luid en vroeg als Ollama niet draait of het model ontbreekt —
        beter dan halverwege een run van uren."""
        try:
            response = self._client.get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Ollama niet bereikbaar op {self._client.base_url} — draait `ollama serve`?"
            ) from exc

        names = {model["name"].split(":")[0] for model in response.json().get("models", [])}
        if self.model.split(":")[0] not in names:
            raise RuntimeError(
                f"Model '{self.model}' niet gevonden in Ollama. Draai: ollama pull {self.model}"
            )

    def _embed_batch(self, batch: list[str], attempt: int = 0) -> list[list[float]]:
        """Eén batch, met bisectie als hij faalt.

        Ollama geeft af en toe een 400 terug op een batch waarvan elke tekst
        afzonderlijk wél werkt. Door bij een fout de batch in tweeën te splitsen
        verliezen we hooguit de ene tekst die echt kapot is, in plaats van de
        hele groep artikels.
        """
        try:
            response = self._client.post(
                "/api/embed", json={"model": self.model, "input": batch}
            )
            response.raise_for_status()
            embeddings = response.json()["embeddings"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            if len(batch) > 1:
                middle = len(batch) // 2
                log.warning(
                    "Embed-batch van %d faalde (%s) — opgesplitst in %d + %d",
                    len(batch), exc, middle, len(batch) - middle,
                )
                return self._embed_batch(batch[:middle]) + self._embed_batch(batch[middle:])
            if attempt < 2:
                time.sleep(2**attempt)
                return self._embed_batch(batch, attempt + 1)
            raise RuntimeError(f"Tekst van {len(batch[0])} tekens blijft falen: {exc}") from exc

        if len(embeddings) != len(batch):
            raise RuntimeError(
                f"Ollama gaf {len(embeddings)} vectoren terug voor {len(batch)} teksten"
            )
        for vector in embeddings:
            if len(vector) != EMBED_DIM:
                raise RuntimeError(
                    f"Verwachtte {EMBED_DIM}-dim vectoren, kreeg {len(vector)} — "
                    f"is EMBED_MODEL echt {self.model}?"
                )
        return embeddings

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embeddings in dezelfde volgorde als de input."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self.batch_size]))
        return vectors

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
